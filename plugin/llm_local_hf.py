# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""safetensors 本地推理运行时（transformers + accelerate，无 vllm / llama_cpp）。

契约：``docs/接口契约.md``（v3 拆分 LLM 节点）
    * ``NL2PromptLLMSafetensors`` 的运行时后端
    * 错误码：``safetensors_unavailable`` / ``safetensors_need_dir`` /
      ``safetensors_not_found`` / ``safetensors_load_failed`` /
      ``safetensors_not_loaded`` / ``safetensors_generate_failed`` /
      ``local_model_oom``

设计要点
--------
1. **绝不依赖 ComfyUI**：本模块可被纯 Python 直接 import（便于单测）。
   ``torch`` / ``transformers`` 一律**函数内惰性导入**，缺失时节点仍能注册。
2. **单模型驻留**：进程内同一时刻只保留一个 HF 模型；``load()`` 新模型前先
   ``unload()`` 旧模型，避免两张卡/两份权重把显存吃穿。
3. **显存降级链**（2060 只有 12 GB，6B fp16 权重约 12 GB，必然吃紧）：
   a) ``dtype`` 直载 + ``device_map={"": "cuda:N"}``（全卡驻留，最快）
   b) 捕获 OOM 后改 ``device_map="auto"``（accelerate 自动把部分层 offload 到 CPU）
   c) 仍 OOM 抛 ``local_model_oom``，detail 带「可用 / 需要」GiB 数字与建议
4. **编码红线**：所有落到 stdout/stderr 的文案都过 :func:`_safe_text`（中文 Windows
   stderr 是 gbk，含不可编码字符会打死 worker 线程），且本文件不含 GBK 不可编码符号。
5. 不引入 bitsandbytes 等新依赖（原 ComfyUI Python 环境不得改动）。

显存估算口径
------------
safetensors 文件字节数约等于参数量 x 每参数字节数（fp16/bf16 = 2），因此
「权重 GiB = 文件总量 / 1024^3」，再乘 1.25 的 KV cache + 激活安全系数即得
「需要 GiB」。这只是**估算**，用于 OOM 时的 detail 与提前告警，不用于阻断加载。
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import traceback
from pathlib import Path

#: 支持的 dtype 选项（INPUT_TYPES 的 COMBO 顺序）
DTYPE_CHOICES = ("fp16", "bf16", "auto")

#: 权重之外的额外显存安全系数（KV cache + 激活 + CUDA context）
_EXTRA_FACTOR = 1.25

#: 扫描模型时最多下钻的子目录层数
_SCAN_MAX_DEPTH = 3

#: 扫描结果最多返回条数
_SCAN_MAX_ITEMS = 500

#: 允许的权重后缀（分片模型也算一个条目）
_WEIGHT_SUFFIXES = (".safetensors",)

#: 扫描时跳过的目录名（大小写不敏感）
#: ``blobs`` 是 HF 缓存的内部存储（真实权重是哈希文件名、且与 snapshots 重复），
#: 跳过它只列 snapshots 里的可见文件名，避免同一模型出现两次。
_SCAN_SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    "site-packages",
    "windows",
    "$recycle.bin",
    "system volume information",
    "blobs",
    ".no_exist",  # HF 缓存里"该文件下载失败"的标记目录，里面是 0 字节占位文件
}

_STATE: dict = {}
_LOCK = threading.RLock()
_SCAN_CACHE: dict = {}


# --------------------------------------------------------------------------- 基础工具


def _safe_text(text) -> str:
    """把文本降级为当前 stdout/stderr 都能编码的字符串（GBK 红线防护）。"""
    raw = text if isinstance(text, str) else str(text)
    for stream in (sys.stderr, sys.stdout):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            raw = raw.encode(encoding, "replace").decode(encoding, "replace")
        except Exception:  # noqa: BLE001
            pass
    return raw


def _log(message) -> None:
    print(f"[NL2Prompt.local_hf] {_safe_text(message)}", flush=True)


def _warn(message) -> None:
    """告警日志：与 :func:`_log` 同一条输出通道，便于统一过滤。"""
    _log(message)


class LocalModelError(Exception):
    """safetensors 运行时错误；``code`` 沿用契约 §8 风格（供后端转成回退码）。

    若 ``llm_client.LLMError`` 可用则自动继承它，保证后端 ``except LLMError``
    能一网打尽；不可用时退化为普通 Exception（仍带 ``.code`` / ``.detail``）。
    """

    def __init__(self, code, detail=""):
        self.code = str(code) if code else "local_model_error"
        self.detail = _safe_text((detail if isinstance(detail, str) else str(detail or ""))[:400])
        super().__init__(_safe_text(f"{self.code}: {self.detail}" if self.detail else self.code))


def _llm_error_base():
    """尝试取 ``llm_client.LLMError`` 作为基类（失败则用 Exception）。"""
    for loader in (_import_sibling_llm_client, lambda: __import__("llm_client")):
        try:
            module = loader()
            base = getattr(module, "LLMError", None)
            if isinstance(base, type) and issubclass(base, Exception):
                return base
        except Exception:  # noqa: BLE001
            continue
    return Exception


def _import_sibling_llm_client():
    package = __package__ or ""
    if package:
        import importlib  # noqa: PLC0415

        return importlib.import_module("." + "llm_client", package)
    raise ImportError("no package")


_LLM_ERROR_BASE = _llm_error_base()


class LocalModelOOM(LocalModelError):
    """显存不足专用错误（``code == "local_model_oom"``）。"""


# 把 LocalModelError 重挂到 llm_client.LLMError 之下（若可导入）。
# 用一个中间类做 MRO 替换，避免 import 顺序导致的基类不确定。
if _LLM_ERROR_BASE is not Exception:
    try:

        class LocalModelError(_LLM_ERROR_BASE):  # type: ignore[no-redef]
            def __init__(self, code, detail=""):
                self.code = str(code) if code else "local_model_error"
                self.detail = _safe_text(
                    (detail if isinstance(detail, str) else str(detail or ""))[:400]
                )
                message = self.code if not self.detail else f"{self.code}: {self.detail}"
                super().__init__(_safe_text(message))

        class LocalModelOOM(LocalModelError):
            """显存不足专用错误（``code == "local_model_oom"``）。"""

    except Exception:  # noqa: BLE001 - 绑定失败时保留上面的普通实现
        pass


# --------------------------------------------------------------------------- 依赖探测


def _import_torch():
    """惰性导入 torch；失败返回 ``None``。"""
    try:
        import torch  # noqa: PLC0415

        return torch
    except Exception:  # noqa: BLE001
        return None


def _import_transformers():
    """惰性导入 transformers 三个入口；失败返回 ``None``。"""
    try:
        from transformers import (  # noqa: PLC0415
            AutoModelForCausalLM,
            AutoTokenizer,
        )

        return AutoModelForCausalLM, AutoTokenizer
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    """torch + transformers 是否都可用（节点注册期用它决定是否给友好提示）。"""
    return _import_torch() is not None and _import_transformers() is not None


def probe() -> dict:
    """运行时能力快照（供节点/路由展示，绝不抛异常）。"""
    info = {
        "ok": False,
        "torch": "",
        "transformers": "",
        "accelerate": "",
        "safetensors": "",
        "cuda": False,
        "device_count": 0,
        "loaded": bool(_STATE),
    }
    torch = _import_torch()
    if torch is not None:
        try:
            info["torch"] = str(getattr(torch, "__version__", ""))
            info["cuda"] = bool(torch.cuda.is_available())
            if info["cuda"]:
                info["device_count"] = int(torch.cuda.device_count())
        except Exception:  # noqa: BLE001
            pass
    try:
        import transformers  # noqa: PLC0415

        info["transformers"] = str(getattr(transformers, "__version__", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        import accelerate  # noqa: PLC0415

        info["accelerate"] = str(getattr(accelerate, "__version__", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        import safetensors  # noqa: PLC0415

        info["safetensors"] = str(getattr(safetensors, "__version__", ""))
    except Exception:  # noqa: BLE001
        pass
    info["ok"] = bool(
        info["torch"] and info["transformers"] and info["cuda"]
    )
    if _STATE:
        info["loaded_model"] = _STATE.get("model_dir", "")
        info["loaded_device"] = _STATE.get("device", "")
    return info


# --------------------------------------------------------------------------- 显存


def memory_info(gpu_index=1) -> dict:
    """返回 ``{"total_gb", "free_gb", "used_gb"}``；无 CUDA 时全 0。"""
    info = {"total_gb": 0.0, "free_gb": 0.0, "used_gb": 0.0}
    torch = _import_torch()
    if torch is None:
        return info
    try:
        if not torch.cuda.is_available():
            return info
        index = int(gpu_index)
        free, total = torch.cuda.mem_get_info(index)
        info["total_gb"] = round(total / (1024**3), 2)
        info["free_gb"] = round(free / (1024**3), 2)
        info["used_gb"] = round((total - free) / (1024**3), 2)
    except Exception:  # noqa: BLE001
        pass
    return info


def _dir_weight_bytes(model_dir) -> int:
    """目录内（及一级子目录）全部 safetensors 的总字节数。"""
    total = 0
    try:
        root = Path(model_dir)
    except (OSError, TypeError, ValueError):
        return 0
    if not root.is_dir():
        return 0
    candidates = [root]
    try:
        candidates.extend([p for p in root.iterdir() if p.is_dir()])
    except OSError:
        pass
    for directory in candidates:
        try:
            for entry in os.scandir(directory):
                # 跟随符号链接：HF 缓存的 snapshots/<hash>/model.safetensors 是
                # symlink 指向 blobs/<hash>，不跟随会误判成"目录里没有权重"。
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(_WEIGHT_SUFFIXES):
                    continue
                try:
                    total += int(entry.stat().st_size)
                except OSError:
                    continue
        except OSError:
            continue
    return total


def estimate_need_gb(model_dir) -> float:
    """估算加载该目录需要的显存（GiB，权重 x 安全系数）；无法判定返回 0。"""
    weights = _dir_weight_bytes(model_dir)
    if weights <= 0:
        return 0.0
    return round(weights / (1024**3) * _EXTRA_FACTOR, 2)


# --------------------------------------------------------------------------- 扫描


def is_hf_dir(directory) -> bool:
    """目录是否是 HF 可加载目录（含 ``config.json``）。"""
    try:
        return Path(directory, "config.json").is_file()
    except (OSError, TypeError, ValueError):
        return False


def scan_models(root, max_depth=_SCAN_MAX_DEPTH, use_cache=True) -> list:
    """递归扫描 ``root`` 下的 ``.safetensors``。

    :returns: ``[{"name","path","dir","size_mb","has_config"}]``
        ``name`` 是相对 ``root`` 的路径（前端 COMBO 用它）；``path`` 是绝对路径。
        最多 ``_SCAN_MAX_ITEMS`` 条，按体积降序（大模型在前更符合直觉）。
    """
    key = (str(root or ""), int(max_depth))
    if use_cache and key in _SCAN_CACHE:
        return list(_SCAN_CACHE[key])

    results = []
    try:
        base = Path(str(root or "")).expanduser()
    except (OSError, ValueError):
        return []
    if not base.is_dir():
        _SCAN_CACHE[key] = results
        return list(results)

    try:
        base_abs = str(base.resolve())
    except (OSError, ValueError):
        base_abs = str(base)

    def walk(directory, depth):
        if len(results) >= _SCAN_MAX_ITEMS:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        files = []
        subdirs = []
        for entry in entries:
            try:
                if entry.is_file():
                    files.append(entry)
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in _SCAN_SKIP_DIRS:
                        continue
                    subdirs.append(entry)
            except OSError:
                continue

        names = {f.name.lower() for f in files}
        has_config = "config.json" in names
        for entry in files:
            if len(results) >= _SCAN_MAX_ITEMS:
                return
            if not entry.name.lower().endswith(_WEIGHT_SUFFIXES):
                continue
            try:
                size = int(entry.stat().st_size)
            except OSError:
                size = 0
            if size <= 0:
                continue
            try:
                rel = str(Path(entry.path).relative_to(base_abs)).replace("\\", "/")
            except (ValueError, TypeError):
                rel = entry.name
            results.append(
                {
                    "name": rel,
                    "path": entry.path,
                    "dir": directory,
                    "size_mb": round(size / (1024 * 1024)),
                    "has_config": has_config,
                }
            )

        if depth <= 0:
            return
        for sub in subdirs:
            walk(sub.path, depth - 1)

    walk(base_abs, int(max_depth))
    results.sort(key=lambda item: item["size_mb"], reverse=True)
    _SCAN_CACHE[key] = results
    return list(results)


def clear_scan_cache() -> None:
    _SCAN_CACHE.clear()


def detect_model_dir() -> str:
    """自动探测 safetensors 模型根目录（只读；找不到返回空串）。

    候选顺序：settings.json 的 ``safetensors_model_dir`` / ``model_roots[0]``、
    环境变量 ``ANIMA_HF_MODELS``、HF 缓存、LM Studio 目录。
    **必须真的含 .safetensors** 才采用，避免给出空目录。
    """
    candidates = []
    try:
        settings_path = Path(__file__).resolve().parent / "data" / "settings.json"
        if settings_path.is_file():
            import json  # noqa: PLC0415

            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in ("safetensors_model_dir", "hf_model_dir"):
                    value = raw.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())
                roots = raw.get("model_roots")
                if isinstance(roots, list):
                    for item in roots:
                        if isinstance(item, str) and item.strip():
                            candidates.append(item.strip())
                else:
                    value = raw.get("model_roots")
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())
    except Exception:  # noqa: BLE001
        pass

    for env_name in ("ANIMA_HF_MODELS", "HF_HOME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(value)

    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates.extend(
        [
            os.path.join(home, ".cache", "huggingface", "hub"),
            os.path.join(home, ".lmstudio", "models"),
            os.path.join(home, ".cache", "lm-studio", "models"),
        ]
    )

    for candidate in candidates:
        try:
            if not Path(candidate).is_dir():
                continue
        except (OSError, ValueError):
            continue
        try:
            if scan_models(candidate, max_depth=4):
                return str(candidate)
        except Exception:  # noqa: BLE001
            continue
    return ""


# --------------------------------------------------------------------------- 加载


def _resolve_dtype(dtype):
    """把 COMBO 值转成 torch dtype；``auto`` 返回字符串 ``"auto"``。"""
    torch = _import_torch()
    name = str(dtype or "fp16").strip().lower()
    if name in ("auto", "", "default"):
        return "auto"
    if torch is None:
        return name
    if name in ("bf16", "bfloat16"):
        return getattr(torch, "bfloat16", None)
    if name in ("fp32", "float32"):
        return getattr(torch, "float32", None)
    return getattr(torch, "float16", None)


def _is_oom(exc) -> bool:
    """判断异常是否为显存不足。"""
    try:
        torch = _import_torch()
        if torch is not None:
            oom_cls = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
            if isinstance(oom_cls, type) and isinstance(exc, oom_cls):
                return True
    except Exception:  # noqa: BLE001
        pass
    text = str(exc).lower()
    return (
        "out of memory" in text
        or "outofmemory" in text
        or "not enough memory" in text
        or "cuda error: out of memory" in text
    )


def _oom_detail(model_dir, gpu_index, need_gb) -> str:
    """组装 ``local_model_oom`` 的人话 detail（含可用/需要 GiB）。"""
    mem = memory_info(gpu_index)
    free = mem.get("free_gb", 0.0)
    total = mem.get("total_gb", 0.0)
    need = need_gb or estimate_need_gb(model_dir)
    return (
        f"显存不足（GPU{gpu_index} 可用 {free} GiB / 共 {total} GiB，"
        f"估算需要 {need} GiB），建议改用 GGUF 量化模型或换更小的 dtype"
    )


def _make_failure(exc, model_dir, gpu_index, need_gb):
    """把加载异常归类成契约错误。"""
    text = f"{type(exc).__name__}: {exc}"
    if _is_oom(exc):
        return LocalModelOOM("local_model_oom", _oom_detail(model_dir, gpu_index, need_gb))
    lowered = text.lower()
    if "config.json" in lowered or "does not appear to have a file named config.json" in lowered:
        return LocalModelError(
            "safetensors_not_found",
            f"目录不是可加载的 HuggingFace 模型（缺 config.json）：{model_dir}",
        )
    if "no module named" in lowered:
        return LocalModelError("safetensors_unavailable", text[:200])
    return LocalModelError("safetensors_load_failed", text[:200])


def _dtype_kwarg_name() -> str:
    """返回 ``from_pretrained`` 该用的精度参数名。

    transformers 5.x 把 ``torch_dtype`` 改名为 ``dtype``（旧名已告警弃用）。
    按版本号判定而不是"试着传再看异常"——``from_pretrained`` 会把未知 kwargs
    继续传给 config，静默忽略，试错法不可靠。
    """
    try:
        import transformers  # noqa: PLC0415

        version = str(getattr(transformers, "__version__", "0"))
        head = version.split(".")[0]
        return "dtype" if int(head) >= 5 else "torch_dtype"
    except Exception:  # noqa: BLE001
        return "torch_dtype"


def _device_capability(gpu_index):
    """返回 ``(major, minor)`` 计算能力；不可用时 ``(0, 0)``。"""
    torch = _import_torch()
    if torch is None:
        return (0, 0)
    try:
        major, minor = torch.cuda.get_device_capability(int(gpu_index))
        return (int(major), int(minor))
    except Exception:  # noqa: BLE001
        return (0, 0)


def _config_torch_dtype(model_dir) -> str:
    """读 config.json 里的 ``torch_dtype``（小写），失败返回空串。"""
    try:
        import json as _json  # noqa: PLC0415

        raw = _json.loads(Path(str(model_dir), "config.json").read_text(encoding="utf-8"))
        value = raw.get("torch_dtype") or raw.get("dtype") or ""
        text = str(value).lower()
        if text.startswith("torch."):
            text = text.split(".", 1)[1]
        return text
    except Exception:  # noqa: BLE001
        return ""


def _effective_dtype(dtype, model_dir, gpu_index):
    """把用户选的 dtype 修正成该卡真能跑的精度。

    实测红线：RTX 2060 是 Turing（SM 7.5），**不支持 bf16 的 SDPA kernel**，
    bf16 推理会抛 ``cutlassF: no kernel found to launch!``。因此 SM < 8.0 时
    一律退回 fp16 并告警，而不是把一个必炸的配置放过去。
    """
    name = str(dtype or "fp16").strip().lower()
    major, minor = _device_capability(gpu_index)
    capability = f"SM {major}.{minor}"
    if name == "auto":
        cfg = _config_torch_dtype(model_dir)
        if cfg in ("bfloat16", "bf16") and major and major < 8:
            _warn(f"{capability} 不支持 bf16（模型 config 为 bf16），已改用 fp16")
            return "fp16"
        return "auto"
    if name in ("bf16", "bfloat16") and major and major < 8:
        _warn(f"{capability} 不支持 bf16 推理 kernel，已自动改用 fp16（避免 cutlass 报错）")
        return "fp16"
    return name


def load(model_dir, gpu_index=1, dtype="fp16", model_file="", force_device_map=""):
    """加载 HF 模型到指定 GPU（契约 §v3 B6）。

    :param model_dir: HF 模型目录（含 ``config.json`` + ``*.safetensors``）
    :param gpu_index: 物理 GPU 序号（默认 1 = 2060，与 GGUF 节点策略一致）
    :param dtype: ``fp16`` / ``bf16`` / ``auto``
    :param model_file: COMBO 选中的文件名；若 ``model_dir`` 本身含 config.json 可省
    :param force_device_map: 调试用：``""`` 走默认降级链，``"auto"`` 强制 offload

    :returns: ``{"model_dir","model","device","dtype","vram_gb","elapsed_sec","device_map"}``
    :raises LocalModelError: 见模块 docstring 的错误码表
    """
    pairs = _import_transformers()
    torch = _import_torch()
    if pairs is None or torch is None:
        raise LocalModelError(
            "safetensors_unavailable",
            "缺少 transformers 或 torch，无法加载 safetensors；请改用 GGUF 或云端",
        )
    if not model_dir or not str(model_dir).strip():
        raise LocalModelError("safetensors_need_dir", "未指定 safetensors 模型目录")
    AutoModelForCausalLM, AutoTokenizer = pairs

    target = _resolve_model_target(model_dir, model_file)
    try:
        exists = Path(target).is_dir()
    except (OSError, ValueError):
        exists = False
    if not exists:
        raise LocalModelError("safetensors_need_dir", f"模型目录不存在：{target}")
    weights = _dir_weight_bytes(target)
    if weights <= 0:
        raise LocalModelError(
            "safetensors_not_found", f"目录内没有 .safetensors 权重：{target}"
        )

    index = int(gpu_index) if str(gpu_index).lstrip("-").isdigit() else 1
    if index < 0:
        index = 1
    if not torch.cuda.is_available():
        raise LocalModelError("safetensors_unavailable", "当前环境无可用 CUDA")
    try:
        count = int(torch.cuda.device_count())
    except Exception:  # noqa: BLE001
        count = 0
    if count and index >= count:
        raise LocalModelError(
            "safetensors_load_failed",
            f"GPU{index} 不存在（可见设备 {count} 张）",
        )

    need_gb = round(weights / (1024**3) * _EXTRA_FACTOR, 2)
    effective_dtype = _effective_dtype(dtype, target, index)
    torch_dtype = _resolve_dtype(effective_dtype)
    device_map = {"": f"cuda:{index}"}
    if force_device_map == "auto":
        device_map = "auto"

    with _LOCK:
        if _STATE:
            _unload_locked()
        started = time.time()
        _log(
            "加载 safetensors: %s gpu=%s dtype=%s 权重≈%.2f GiB 需要≈%.2f GiB"
            % (target, index, effective_dtype, weights / (1024**3), need_gb)
        )
        before = memory_info(index)

        model = None
        used_map = device_map
        errors = []
        attempts = [device_map]
        if device_map != "auto":
            attempts.append("auto")
        dtype_key = _dtype_kwarg_name()
        for attempt in attempts:
            kwargs = {
                dtype_key: torch_dtype,
                "device_map": attempt,
                "low_cpu_mem_usage": True,
            }
            try:
                model = AutoModelForCausalLM.from_pretrained(target, **kwargs)
                used_map = attempt
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
                if _is_oom(exc) and attempt != "auto":
                    _log("直载 OOM，降级 device_map=auto（部分层 offload 到 CPU，会变慢）")
                    _safe_empty_cache()
                    continue
                if _is_oom(exc):
                    _safe_empty_cache()
                    raise _make_failure(exc, target, index, need_gb) from exc
                raise _make_failure(exc, target, index, need_gb) from exc

        if model is None:
            _safe_empty_cache()
            raise LocalModelError(
                "safetensors_load_failed",
                "加载失败：" + (" | ".join(errors) if errors else "未知原因"),
            )

        tokenizer = None
        try:
            tokenizer = AutoTokenizer.from_pretrained(target)
        except Exception as exc:  # noqa: BLE001
            _log(f"tokenizer 加载失败（将退回朴素拼接）：{type(exc).__name__}")

        try:
            model.eval()
        except Exception:  # noqa: BLE001
            pass

        elapsed = time.time() - started
        after = memory_info(index)
        _STATE.clear()
        _STATE.update(
            {
                "model": model,
                "tokenizer": tokenizer,
                "model_dir": str(target),
                "model_file": model_file or "",
                "device": f"cuda:{index}",
                "gpu_index": index,
                "dtype": effective_dtype,
                "requested_dtype": str(dtype or "fp16"),
                "device_map": used_map if isinstance(used_map, str) else "dict",
                "vram_gb": after.get("used_gb", 0.0),
                "vram_before_gb": before.get("used_gb", 0.0),
                "vram_delta_gb": round(
                    max(0.0, after.get("used_gb", 0.0) - before.get("used_gb", 0.0)), 2
                ),
                "load_sec": round(elapsed, 2),
                "loaded_at": time.time(),
                "needs_gb": need_gb,
            }
        )
        _log(
            "safetensors 就绪: device=cuda:%s dtype=%s 加载 %.1fs 占用 %.2f GiB"
            % (index, effective_dtype, elapsed, _STATE["vram_gb"])
        )
        return status()


def _resolve_model_target(model_dir, model_file=""):
    """把 (model_dir, model_file) 归一到可加载的 HF 目录。

    必须容忍两种真实布局：
    1. ``model_dir`` 本身就是 HF 目录（含 config.json）；
    2. ``model_dir`` 是模型根目录，``model_file`` 是其中的 ``.safetensors``；
    3. HF 缓存布局 ``hub/models--org--name/snapshots/<hash>/model.safetensors``
       且该文件是 symlink —— 此时**不能拿 resolve 后的 blob 路径**做判断，
       要从符号链接所在目录向上找最近的含 config.json 的目录。
    """
    try:
        base = Path(str(model_dir)).expanduser()
    except (OSError, ValueError):
        base = Path(str(model_dir))
    name = str(model_file or "").strip().replace("\\", "/")
    if not name:
        return str(base)
    candidate = Path(name)
    target = candidate if candidate.is_absolute() else base / name

    if is_hf_dir(target):
        return str(target)

    # 从文件所在目录向上找最近的 HF 目录（不越过 base）
    try:
        current = target if target.is_dir() else target.parent
    except OSError:
        current = base
    for _ in range(4):
        try:
            if is_hf_dir(current):
                return str(current)
        except OSError:
            break
        try:
            if current == base or current.parent == current:
                break
        except OSError:
            break
        current = current.parent

    if is_hf_dir(base):
        return str(base)
    return str(target)


def pretty_model_name(path) -> str:
    """把模型路径转成可读名（HF 缓存目录转 repo 名，其余取目录名）。"""
    text = str(path or "")
    marker = "models--"
    index = text.find(marker)
    if index >= 0:
        tail = text[index + len(marker):]
        for sep in ("\\", "/"):
            cut = tail.find(sep)
            if cut > 0:
                tail = tail[:cut]
                break
        parts = [item for item in tail.split("--") if item]
        if parts:
            return "/".join(parts)
    try:
        path_obj = Path(text)
        if path_obj.suffix.lower() in (".safetensors", ".gguf"):
            return path_obj.parent.name or path_obj.name
        return path_obj.name or text
    except (OSError, ValueError):
        return text


def _safe_empty_cache():
    torch = _import_torch()
    if torch is None:
        return
    try:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- 生成


def _fallback_prompt(messages) -> str:
    """无 chat template 时的朴素拼接（中文标签，兼容旧模型）。"""
    system_parts = []
    lines = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            lines.append(f"助手：{text}")
        else:
            lines.append(f"用户：{text}")
    head = ("\n".join(system_parts) + "\n") if system_parts else ""
    return f"{head}" + "\n".join(lines) + "\n助手："


def build_prompt(messages) -> dict:
    """按 chat template 渲染 prompt；返回 ``{"text","used_chat_template"}``。"""
    text = ""
    used = False
    tokenizer = _STATE.get("tokenizer")
    if tokenizer is not None:
        try:
            template = getattr(tokenizer, "chat_template", None)
            if template:
                text = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
                used = bool(text)
        except Exception as exc:  # noqa: BLE001
            _log(f"apply_chat_template 失败，退回朴素拼接：{type(exc).__name__}")
    if not text:
        text = _fallback_prompt(messages)
    return {"text": text, "used_chat_template": used}


def generate(
    messages=None,
    prompt=None,
    max_new_tokens=512,
    temperature=0.7,
    top_p=0.9,
    top_k=40,
    seed=None,
    timeout=120.0,
):
    """生成文本（同步阻塞）。

    :param messages: OpenAI 风格 ``[{"role","content"}]``；与 ``prompt`` 二选一
    :param prompt: 纯文本 prompt（``messages`` 为空时使用）
    :returns: ``{"text","prompt_tokens","completion_tokens","elapsed_sec","device","used_chat_template"}``
    :raises LocalModelError: ``safetensors_need_dir``（未加载）等
    """
    if not _STATE:
        raise LocalModelError("safetensors_not_loaded", "尚未加载 safetensors 模型")
    model = _STATE.get("model")
    tokenizer = _STATE.get("tokenizer")
    if model is None:
        raise LocalModelError("safetensors_not_loaded", "模型句柄已释放，请重新执行加载节点")

    if messages:
        built = build_prompt(messages)
        text = built["text"]
        used_template = built["used_chat_template"]
    else:
        text = str(prompt or "")
        used_template = False

    torch = _import_torch()
    started = time.time()
    try:
        inputs = tokenizer(text, return_tensors="pt") if tokenizer is not None else None
        if inputs is None:
            raise LocalModelError("safetensors_unavailable", "tokenizer 不可用")
        device = _STATE.get("device") or f"cuda:{_STATE.get('gpu_index', 1)}"
        inputs = {key: value.to(device) for key, value in inputs.items()}
    except LocalModelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _make_failure(exc, _STATE.get("model_dir", ""), _STATE.get("gpu_index", 1), 0) from exc

    kwargs = {
        "max_new_tokens": max(1, int(max_new_tokens or 512)),
        "do_sample": float(temperature or 0) > 0,
        "pad_token_id": getattr(tokenizer, "pad_token_id", None),
    }
    if kwargs["pad_token_id"] is None:
        eos = getattr(tokenizer, "eos_token_id", None)
        kwargs["pad_token_id"] = eos
    if kwargs["do_sample"]:
        kwargs["temperature"] = max(0.01, float(temperature))
        kwargs["top_p"] = max(0.01, min(1.0, float(top_p if top_p is not None else 0.9)))
        if top_k:
            kwargs["top_k"] = int(top_k)
        if seed is not None:
            try:
                torch.manual_seed(int(seed) & 0x7FFFFFFF)
            except Exception:  # noqa: BLE001
                pass

    try:
        with torch.no_grad():
            output = model.generate(**inputs, **kwargs)
    except Exception as exc:  # noqa: BLE001
        _safe_empty_cache()
        if _is_oom(exc):
            raise LocalModelOOM(
                "local_model_oom",
                _oom_detail(_STATE.get("model_dir", ""), _STATE.get("gpu_index", 1), 0),
            ) from exc
        raise LocalModelError(
            "safetensors_generate_failed",
            f"{type(exc).__name__}: {exc}"[:200],
        ) from exc

    prompt_len = int(inputs["input_ids"].shape[-1]) if "input_ids" in inputs else 0
    try:
        generated_ids = output[0][prompt_len:]
        answer = tokenizer.decode(generated_ids, skip_special_tokens=True)
    except Exception:  # noqa: BLE001
        answer = ""
    return {
        "text": answer.strip(),
        "prompt_tokens": prompt_len,
        "completion_tokens": int(max(0, output.shape[-1] - prompt_len)) if hasattr(output, "shape") else 0,
        "elapsed_sec": round(time.time() - started, 2),
        "device": _STATE.get("device", ""),
        "used_chat_template": used_template,
        "timeout": timeout,
    }


# --------------------------------------------------------------------------- 卸载 / 状态


def _unload_locked():
    """释放模型（调用方须持锁）。"""
    if not _STATE:
        return False
    try:
        model = _STATE.get("model")
        _STATE.clear()
        del model
    except Exception:  # noqa: BLE001
        pass
    _safe_empty_cache()
    _log("safetensors 模型已卸载，显存已释放")
    return True


def unload() -> bool:
    """卸载当前 HF 模型并释放显存（幂等）。"""
    with _LOCK:
        return _unload_locked()


def is_loaded() -> bool:
    return bool(_STATE)


def status() -> dict:
    """当前驻留模型的状态快照（不含模型对象本身）。"""
    if not _STATE:
        return {"loaded": False}
    return {
        "loaded": True,
        "model_dir": _STATE.get("model_dir", ""),
        "model": pretty_model_name(_STATE.get("model_dir", "")),
        "gpu_index": _STATE.get("gpu_index", 0),
        "device": _STATE.get("device", ""),
        "dtype": _STATE.get("dtype", ""),
        "device_map": _STATE.get("device_map", ""),
        "vram_gb": _STATE.get("vram_gb", 0.0),
        "vram_delta_gb": _STATE.get("vram_delta_gb", 0.0),
        "load_sec": _STATE.get("load_sec", 0.0),
        "needs_gb": _STATE.get("needs_gb", 0.0),
    }


def ensure_loaded(model_dir, gpu_index=1, dtype="fp16", model_file="") -> dict:
    """复用或加载：同目录/同 dtype/同卡已在驻留时直接返回现状。"""
    with _LOCK:
        if _STATE:
            same = (
                str(_STATE.get("model_dir", "")) == str(_resolve_model_target(model_dir, model_file))
                and str(_STATE.get("dtype", "")) == str(dtype or "fp16")
                and int(_STATE.get("gpu_index", -1)) == int(gpu_index)
            )
            if same:
                return status()
    return load(model_dir=model_dir, gpu_index=gpu_index, dtype=dtype, model_file=model_file)


def chat(llm, messages, sampling=None, unload_after_use=None) -> dict:
    """一站式入口：按 ``LLM_MODEL`` dict 加载（如需）+ 生成 + 可选卸载。

    **后端（``llm_client`` / Composer）建议只调这一个函数**，不必自己拼
    ``ensure_loaded`` + ``generate`` + ``unload`` 三步：

    .. code-block:: python

        if llm.get("mode") == "safetensors":
            out = llm_local_hf.chat(llm, messages)   # -> {"text": ..., ...}
            text = out["text"]

    :param llm: 节点产出的 ``LLM_MODEL`` dict（读 ``local_path`` / ``gpu_index`` /
        ``dtype`` / ``sampling`` / ``runtime.unload_after_use``）
    :param messages: OpenAI 风格消息列表
    :param sampling: 覆盖采样参数（默认取 ``llm["sampling"]``）
    :param unload_after_use: 覆盖卸载开关（默认取 ``llm["runtime"]["unload_after_use"]``）
    """
    if not isinstance(llm, dict):
        raise LocalModelError("safetensors_need_dir", "LLM_MODEL 输入不是 dict")
    sampling = sampling if isinstance(sampling, dict) else llm.get("sampling")
    sampling = sampling if isinstance(sampling, dict) else {}
    runtime_cfg = llm.get("runtime") if isinstance(llm.get("runtime"), dict) else {}
    if unload_after_use is None:
        unload_after_use = bool(runtime_cfg.get("unload_after_use", False))

    model_dir = llm.get("local_path") or llm.get("model_dir") or ""
    try:
        gpu_index = int(llm.get("gpu_index", 1))
    except (TypeError, ValueError):
        gpu_index = 1

    try:
        info = ensure_loaded(
            model_dir=model_dir,
            gpu_index=gpu_index,
            dtype=llm.get("dtype") or "fp16",
            model_file=llm.get("model_file") or "",
        )
        result = generate(
            messages=messages,
            max_new_tokens=sampling.get("max_tokens", 1024),
            temperature=sampling.get("temperature", 0.7),
            top_p=sampling.get("top_p", 0.9),
            top_k=sampling.get("top_k", 40),
            seed=sampling.get("seed"),
            timeout=runtime_cfg.get("timeout", 120),
        )
        result["model"] = info.get("model", "")
        result["load_sec"] = info.get("load_sec", 0.0)
        return result
    finally:
        if unload_after_use:
            unload()


__all__ = [
    "DTYPE_CHOICES",
    "LocalModelError",
    "LocalModelOOM",
    "available",
    "probe",
    "memory_info",
    "estimate_need_gb",
    "scan_models",
    "clear_scan_cache",
    "detect_model_dir",
    "is_hf_dir",
    "load",
    "ensure_loaded",
    "chat",
    "generate",
    "build_prompt",
    "unload",
    "is_loaded",
    "status",
]
