# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""LLM 加载节点（v3 拆分版）：GGUF / safetensors / 云端 三个独立节点。

契约：``docs/接口契约.md``（v3 阶段 2 - 后端 B）
    * 节点 ``NL2PromptLLMGGUF``       显示名「加载LLM（GGUF）」
    * 节点 ``NL2PromptLLMSafetensors`` 显示名「加载LLM（safetensors）」
    * 节点 ``NL2PromptLLMCloud``       显示名「加载云端LLM」

三者都输出 ``LLM_MODEL``（Python dict），结构沿用 v2 的 ``llm``：

.. code-block:: python

    {"mode": "local" | "safetensors" | "cloud",
     "provider": "deepseek", "provider_label": "DeepSeek",
     "base_url": "", "model": "...",
     "gpu_index": 1, "gpu_name": "NVIDIA GeForce RTX 2060",
     "local_path": "<gguf 绝对路径或 HF 目录>",
     "sampling": {"top_k":40,"top_p":0.9,"temperature":0.7,"max_tokens":1024,"seed":<实际值>},
     "runtime": {"ctx":8192,"timeout":120,"port":0,"unload_after_use":False},
     "dtype": "fp16"}   # 仅 safetensors

设计约束
--------
1. **本文件不依赖 ComfyUI**：可被纯 Python 直接 import（便于单测）。
   ``torch`` / ``transformers`` 只在 :mod:`llm_local_hf` 内部惰性导入。
2. 复用 ``nodes.py`` 的解析逻辑（``_sampling_from`` / ``_runtime_from`` /
   ``_parse_gpu`` / ``_gpu_choices`` / ``provider_options`` 等），但每个都带
   **本地兜底实现**：``nodes.py`` 正被其它人并行改写，helper 若改名/移除，
   本文件仍能独立工作，不会把节点加载拖崩。
3. ``unload_after_use`` 必须写进 ``llm["runtime"]["unload_after_use"]``，
   由 Composer 读取后调用 ``runtime.unload(llm)`` 执行卸载。
4. **编码红线**：所有输出文案过 :func:`_safe_text`，且本文件不含 GBK
   不可编码字符（中文 Windows 的 stderr 是 gbk）。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_LOG_PREFIX = "[NL2Prompt.llm]"

#: 前端「先选路径后刷新」占位值。中英各一份——下拉显示跟随前端语言，
#: 而存进工作流的可能是任一种，所以后端判断必须两种都认（见 is_model_placeholder）。
#: 与 ``nodes.py`` 的同名常量必须保持一致（两模块刻意不互相 import）。
MODEL_FILE_PLACEHOLDER_ZH = "(请先选择路径后刷新)"
MODEL_FILE_PLACEHOLDER_EN = "(select a folder, then refresh)"
MODEL_FILE_PLACEHOLDERS = (MODEL_FILE_PLACEHOLDER_ZH, MODEL_FILE_PLACEHOLDER_EN)
#: 兼容既有引用与 INPUT_TYPES 默认值（以中文为准）
MODEL_FILE_PLACEHOLDER = MODEL_FILE_PLACEHOLDER_ZH


def is_model_placeholder(value) -> bool:
    """是否为「尚未选择模型」的占位值（空串 / 中文占位 / 英文占位都算）。"""
    text = (value or "").strip()
    return not text or text in MODEL_FILE_PLACEHOLDERS

#: 无 GPU 可枚举时的兜底选项
GPU_FALLBACK = ["0", "1"]

#: safetensors 节点 dtype 选项
DTYPE_CHOICES = ("fp16", "bf16", "auto")

#: 扫描 .safetensors 的最大子目录层数（过深会很慢）
SAFETENSORS_SCAN_DEPTH = 3


# --------------------------------------------------------------------------- 编码防护


def _safe_text(text) -> str:
    """把文本降级为当前 stdout/stderr 都能编码的字符串（P0 级防护）。"""
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
    print(f"{_LOG_PREFIX} {_safe_text(message)}", flush=True)


# --------------------------------------------------------------------------- 复用 nodes.py


def _load_nodes_module():
    """加载同级 ``nodes.py``；失败返回 ``None``（绝不抛异常）。"""
    package = __package__ or ""
    if package:
        try:
            return importlib.import_module("." + "nodes", package)
        except Exception:  # noqa: BLE001 - 并行开发期可能半成品
            pass
    try:
        return importlib.import_module("nodes")
    except Exception:  # noqa: BLE001
        pass
    try:
        path = _PLUGIN_DIR / "nodes.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_nl2prompt_nodes_for_llm", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


_NODES = None
_NODES_TRIED = False


def _nodes_module():
    """惰性加载同级 ``nodes.py``（**不用模块级 import**，否则循环导入）。

    为什么惰性：``nodes.py`` 末尾会 ``from nodes_llm import ...`` 做 re-export。
    若本模块在顶层就 ``import nodes``，裸导入（``import nodes_llm`` 优先）时
    ``nodes_llm`` 尚未定义类，nodes 的 re-export 会拿到 partially initialized
    module 并报 ImportError。推迟到第一次真正调用 helper 时再取，
    此时 ``nodes`` 一定已完整初始化。
    """
    global _NODES, _NODES_TRIED
    if _NODES_TRIED:
        return _NODES
    _NODES_TRIED = True
    _NODES = _load_nodes_module()
    return _NODES


def _node_fn(name):
    """取 nodes.py 里的可调用 helper；缺失返回 ``None``。"""
    module = _nodes_module()
    if module is None:
        return None
    candidate = getattr(module, name, None)
    return candidate if callable(candidate) else None


def _node_value(name, default=None):
    module = _nodes_module()
    if module is None:
        return default
    return getattr(module, name, default)


class _LocalLLMError(Exception):
    """``nodes.LLMError`` / ``llm_client.LLMError`` 都不可用时的兜底异常。"""

    def __init__(self, code, detail=""):
        self.code = str(code) if code else "llm_error"
        self.detail = _safe_text(
            (detail if isinstance(detail, str) else str(detail or ""))[:200]
        )
        super().__init__(_safe_text(f"{self.code}: {self.detail}" if self.detail else self.code))


def llm_error_class():
    """运行时解析应使用的 LLM 异常类（优先 nodes.LLMError）。

    注意不能用模块级 ``LLMError = ...`` 缓存：模块顶层此时 ``nodes`` 还没就绪，
    且模块级赋值会遮蔽下面的 ``__getattr__``。
    """
    module = _nodes_module()
    candidate = getattr(module, "LLMError", None) if module is not None else None
    if isinstance(candidate, type) and issubclass(candidate, Exception):
        return candidate
    return _LocalLLMError


def __getattr__(name):
    """PEP 562：``nodes_llm.LLMError`` 惰性解析成 ``nodes.LLMError``（若有）。"""
    if name == "LLMError":
        return llm_error_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------- 本地兜底 helper


def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _norm(value) -> str:
    return _s(value).lower()


def _parse_json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hash_params(payload):
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_gpu(value):
    """``"1: NVIDIA GeForce RTX 2060 (12288 MiB)"`` 变成 ``(1, "NVIDIA GeForce RTX 2060")``。"""
    fn = _node_fn("_parse_gpu")
    if fn is not None:
        try:
            index, name = fn(value)
            return int(index), _s(name)
        except Exception:  # noqa: BLE001
            pass
    text = _s(value)
    if not text:
        return 0, ""
    head, sep, tail = text.partition(":")
    if not sep:
        try:
            return int(head.strip()), ""
        except (TypeError, ValueError):
            return 0, text
    try:
        index = int(head.strip())
    except (TypeError, ValueError):
        return 0, text
    name = tail.strip()
    if name.endswith(")") and "(" in name:
        name = name[: name.rfind("(")].strip()
    return index, name


def sampling_from(holder):
    """契约 §4 采样参数（缺字段补默认）。"""
    fn = _node_fn("_sampling_from")
    if fn is not None:
        try:
            value = fn(holder)
            if isinstance(value, dict) and value:
                merged = {
                    "top_k": 40,
                    "top_p": 0.9,
                    "temperature": 0.7,
                    "max_tokens": 1024,
                    "seed": -1,
                }
                merged.update(value)
                return merged
        except Exception:  # noqa: BLE001
            pass
    defaults = {
        "top_k": 40,
        "top_p": 0.9,
        "temperature": 0.7,
        "max_tokens": 1024,
        "seed": -1,
    }
    source = holder.get("sampling") if isinstance(holder, dict) else None
    if isinstance(source, dict):
        for key in list(defaults):
            if key not in source:
                continue
            try:
                defaults[key] = float(source[key]) if key in ("top_p", "temperature") else int(source[key])
            except (TypeError, ValueError):
                pass
    return defaults


def runtime_from(holder):
    """契约 §4 运行参数（ctx / timeout / port / unload_after_use）。"""
    fn = _node_fn("_runtime_from")
    defaults = {"ctx": 8192, "timeout": 120, "port": 0, "unload_after_use": False}
    if fn is not None:
        try:
            value = fn(holder)
            if isinstance(value, dict) and value:
                defaults.update(value)
                defaults["unload_after_use"] = bool(defaults.get("unload_after_use", False))
                return defaults
        except Exception:  # noqa: BLE001
            pass
    source = holder.get("runtime") if isinstance(holder, dict) else None
    if isinstance(source, dict):
        for key in ("ctx", "timeout", "port"):
            if key not in source:
                continue
            try:
                defaults[key] = int(source[key])
            except (TypeError, ValueError):
                pass
        defaults["unload_after_use"] = bool(source.get("unload_after_use", False))
    return defaults


def gpu_choices():
    """物理 GPU COMBO 选项（复用 nodes.py，失败兜底）。"""
    fn = _node_fn("_gpu_choices")
    if fn is not None:
        try:
            value = fn()
            if isinstance(value, list) and value:
                return [str(item) for item in value]
        except Exception:  # noqa: BLE001
            pass
    return list(GPU_FALLBACK)


def default_gpu_choice():
    """默认选中物理 index 1（2060 跑 LLM，3060 留给出图）。"""
    fn = _node_fn("_default_gpu_choice")
    if fn is not None:
        try:
            value = fn()
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001
            pass
    choices = gpu_choices()
    for choice in choices:
        head = choice.split(":", 1)[0].strip()
        try:
            if int(head) == 1:
                return choice
        except (TypeError, ValueError):
            continue
    return choices[0] if choices else "0"


def provider_options():
    """供应商 COMBO 选项（复用 nodes.py 注册表，失败兜底四家）。"""
    fn = _node_fn("provider_options")
    if fn is not None:
        try:
            value = fn()
            if isinstance(value, list) and value:
                return [str(item) for item in value]
        except Exception:  # noqa: BLE001
            pass
    return ["deepseek", "openai", "anthropic", "custom"]


def provider_label(provider):
    """供应商显示名（失败时返回原 id）。"""
    fn = _node_fn("provider_label")
    if fn is not None:
        try:
            value = fn(provider)
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001
            pass
    return _s(provider)


def provider_base_url(provider):
    """按 provider 取默认 base_url（空串表示需要用户填）。"""
    fn = _node_fn("_provider_base_url")
    if fn is not None:
        try:
            value = fn(provider)
            if isinstance(value, str):
                return value
        except Exception:  # noqa: BLE001
            pass
    table = {
        "deepseek": "https://api.deepseek.com/v1",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "custom": "",
    }
    return table.get(_norm(provider), "")


def detect_gguf_path():
    """GGUF 模型根目录默认值（复用 nodes.py 的自动探测）。"""
    fn = _node_fn("_detect_model_path")
    if fn is not None:
        try:
            value = fn()
            if isinstance(value, str):
                return value
        except Exception:  # noqa: BLE001
            pass
    return ""


# --------------------------------------------------------------------------- safetensors 扫描


def _hf_module():
    """加载同级 ``llm_local_hf.py``；失败返回 ``None``。"""
    package = __package__ or ""
    if package:
        try:
            return importlib.import_module("." + "llm_local_hf", package)
        except Exception:  # noqa: BLE001
            pass
    try:
        return importlib.import_module("llm_local_hf")
    except Exception:  # noqa: BLE001
        pass
    try:
        path = _PLUGIN_DIR / "llm_local_hf.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_nl2prompt_llm_local_hf", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def detect_safetensors_dir():
    """safetensors 模型根目录默认值（空串表示自动探测失败）。"""
    module = _hf_module()
    if module is not None:
        fn = getattr(module, "detect_model_dir", None)
        if callable(fn):
            try:
                value = fn()
                if isinstance(value, str):
                    return value
            except Exception:  # noqa: BLE001
                pass
    return ""


def scan_safetensors(root):
    """扫 ``root`` 下的 ``.safetensors``，返回 COMBO 选项（相对路径）。"""
    module = _hf_module()
    if module is not None:
        fn = getattr(module, "scan_models", None)
        if callable(fn):
            try:
                rows = fn(root, max_depth=SAFETENSORS_SCAN_DEPTH)
                if isinstance(rows, list):
                    names = [row.get("name") for row in rows if isinstance(row, dict)]
                    cleaned = [name for name in names if isinstance(name, str) and name]
                    if cleaned:
                        return cleaned
            except Exception:  # noqa: BLE001
                pass
    # 兜底：本文件内置最小扫描（llm_local_hf 不可用时仍可用）
    results = []
    try:
        base = Path(_s(root)).expanduser()
    except (OSError, ValueError):
        return [MODEL_FILE_PLACEHOLDER]
    if not base.is_dir():
        return [MODEL_FILE_PLACEHOLDER]

    def walk(directory, depth):
        if len(results) >= 500:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower().endswith(".safetensors"):
                    results.append(str(entry.relative_to(base)).replace("\\", "/"))
                elif entry.is_dir() and depth > 0:
                    walk(entry, depth - 1)
            except OSError:
                continue

    walk(base, SAFETENSORS_SCAN_DEPTH)
    return results[:500] or [MODEL_FILE_PLACEHOLDER]


# --------------------------------------------------------------------------- 共用构建


def _actual_seed(sampling):
    """A5：seed == -1 表示每次随机，生成本次真实种子并写回 sampling。"""
    raw = sampling.get("seed", -1)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        raw = -1
    actual = random.randint(0, 2**63 - 1) if raw < 0 else raw
    sampling["seed"] = actual
    return actual


def _holder_parts(_llm_json):
    holder = _parse_json_obj(_llm_json)
    sampling = sampling_from(holder)
    runtime = runtime_from(holder)
    return holder, sampling, runtime


def _result(llm, seed):
    """统一返回结构：``ui.anima_llm`` 供前端 onExecuted 回填「上次种子」。"""
    return {"ui": {"anima_llm": [{"seed": seed}]}, "result": (llm,)}


def _seed_only_changed(holder, **kwargs):
    """``IS_CHANGED`` 共用实现：seed < 0 时每次都要重跑（否则种子不变）。"""
    sampling = holder.get("sampling") if isinstance(holder.get("sampling"), dict) else {}
    try:
        seed = int(sampling.get("seed", -1))
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        return time.time()
    payload = {"seed": seed}
    payload.update(kwargs)
    return _hash_params(payload)


# --------------------------------------------------------------------------- 节点 1：GGUF


class NL2PromptLLMGGUF:
    """加载 LLM（GGUF）：走本地 llama-server（``runtime.ensure_runtime``）。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "load"
    RETURN_TYPES = ("LLM_MODEL",)
    RETURN_NAMES = ("llm",)
    OUTPUT_NODE = False
    DESCRIPTION = (
        "本地 GGUF 模型：由插件自动拉起 llama-server 并绑定指定 GPU，输出 LLM_MODEL"
        "（绝不含 api_key 明文）。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        gpus = gpu_choices()
        return {
            "required": {
                "gpu": (
                    gpus,
                    {
                        "default": default_gpu_choice(),
                        "tooltip": "物理 GPU（默认 1 = 2060 跑 LLM，3060 留给出图）",
                    },
                ),
                "model_path": (
                    "STRING",
                    {
                        "multiline": False,
                        # 该留空的留空：默认空串，运行时回退自动探测（见 load()）
                        "default": "",
                        "tooltip": "GGUF 模型根目录；留空自动探测 LM Studio / Ollama 默认路径",
                    },
                ),
                "model_file": (
                    [MODEL_FILE_PLACEHOLDER],
                    {"default": MODEL_FILE_PLACEHOLDER},
                ),
                "unload_after_use": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "本次生成后立即停止 llama-server 并释放显存（交互式微调时再打开）",
                    },
                ),
                "_llm_json": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "{}",
                        "tooltip": "内部 holder：采样参数 / 运行参数（不占节点正面空间）",
                    },
                ),
            }
        }

    def load(
        self,
        gpu="0",
        model_path="",
        model_file=MODEL_FILE_PLACEHOLDER,
        unload_after_use=False,
        _llm_json="{}",
    ):
        holder, sampling, runtime = _holder_parts(_llm_json)
        gpu_index, gpu_name = parse_gpu(gpu)

        selected = _s(model_file)
        local_path = ""
        if not is_model_placeholder(selected):
            candidate = Path(selected)
            if candidate.is_absolute():
                local_path = str(candidate)
            else:
                # model_path 默认空串（该留空的留空）；空时回退自动探测，行为与旧版一致
                root = _s(model_path) or detect_gguf_path()
                local_path = str(Path(root) / selected) if root else ""
            if local_path:
                try:
                    local_path = str(Path(local_path).resolve())
                except (OSError, ValueError):
                    pass

        runtime["unload_after_use"] = bool(unload_after_use)
        seed = _actual_seed(sampling)

        llm = {
            "mode": "local",
            "provider": "",
            "provider_label": "",
            "base_url": "",
            "model": Path(local_path).stem if local_path else "",
            "gpu_index": gpu_index,
            "gpu_name": gpu_name,
            "local_path": local_path,
            "sampling": sampling,
            "runtime": runtime,
        }
        return _result(llm, seed)

    @classmethod
    def IS_CHANGED(cls, gpu="0", model_path="", model_file=MODEL_FILE_PLACEHOLDER,
                   unload_after_use=False, _llm_json="{}", **kwargs):
        return _seed_only_changed(
            _parse_json_obj(_llm_json),
            gpu=_s(gpu),
            model_path=_s(model_path),
            model_file=_s(model_file),
            unload_after_use=bool(unload_after_use),
        )


# --------------------------------------------------------------------------- 节点 2：safetensors


class NL2PromptLLMSafetensors:
    """加载 LLM（safetensors）：transformers + accelerate 直接推理（无 vllm）。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "load"
    RETURN_TYPES = ("LLM_MODEL",)
    RETURN_NAMES = ("llm",)
    OUTPUT_NODE = False
    DESCRIPTION = (
        "本地 HuggingFace safetensors 模型（需目录内含 config.json）："
        "transformers 直接加载到指定 GPU，显存不足时自动降级并报 local_model_oom。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        gpus = gpu_choices()
        default_dir = detect_safetensors_dir()
        files = scan_safetensors(default_dir) if default_dir else [MODEL_FILE_PLACEHOLDER]
        return {
            "required": {
                "gpu": (
                    gpus,
                    {
                        "default": default_gpu_choice(),
                        "tooltip": "物理 GPU（默认 1 = 2060 跑 LLM，3060 留给出图）",
                    },
                ),
                "model_dir": (
                    "STRING",
                    {
                        "multiline": False,
                        # 该留空的留空：默认空串，运行时回退自动探测（见 load()）
                        "default": "",
                        "tooltip": "HF 模型根目录（含 config.json 的目录）；留空自动探测",
                    },
                ),
                "model_file": (
                    [MODEL_FILE_PLACEHOLDER],
                    {
                        # 该留空的留空：不预扫描磁盘，初始只有哨兵值；点「刷新模型列表」后再填
                        "default": MODEL_FILE_PLACEHOLDER,
                        "tooltip": "选中 .safetensors；加载时用其所在目录（需有 config.json）",
                    },
                ),
                "dtype": (
                    list(DTYPE_CHOICES),
                    {
                        "default": "fp16",
                        "tooltip": "权重精度：fp16 最快；显存不足可试 bf16 或 auto",
                    },
                ),
                "unload_after_use": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "本次生成后立即卸载模型并释放显存",
                    },
                ),
                "_llm_json": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "{}",
                        "tooltip": "内部 holder：采样参数 / 运行参数（不占节点正面空间）",
                    },
                ),
            }
        }

    def load(
        self,
        gpu="0",
        model_dir="",
        model_file=MODEL_FILE_PLACEHOLDER,
        dtype="fp16",
        unload_after_use=False,
        _llm_json="{}",
    ):
        holder, sampling, runtime = _holder_parts(_llm_json)
        gpu_index, gpu_name = parse_gpu(gpu)

        root = _s(model_dir) or detect_safetensors_dir()
        selected = _s(model_file)
        local_path = ""
        if root and not is_model_placeholder(selected):
            candidate = Path(selected)
            raw = str(candidate if candidate.is_absolute() else Path(root) / selected)
            # 注意：这里**不能**用 Path.resolve()。HF 缓存的
            # ``snapshots/<hash>/model.safetensors`` 是符号链接，resolve 会把它
            # 变成无扩展名的 blob 路径，加载时再也找不到 config.json。
            try:
                local_path = os.path.normpath(raw)
            except (OSError, ValueError):
                local_path = raw
        elif root:
            local_path = str(Path(root))

        dtype_name = _norm(dtype)
        if dtype_name not in DTYPE_CHOICES:
            dtype_name = "fp16"

        runtime["unload_after_use"] = bool(unload_after_use)
        seed = _actual_seed(sampling)

        model_name = ""
        if local_path:
            module = _hf_module()
            pretty = getattr(module, "pretty_model_name", None) if module is not None else None
            if callable(pretty):
                try:
                    model_name = _s(pretty(local_path))
                except Exception:  # noqa: BLE001
                    model_name = ""
            if not model_name:
                path_obj = Path(local_path)
                model_name = (
                    path_obj.parent.name
                    if path_obj.suffix.lower() == ".safetensors"
                    else path_obj.name
                )

        llm = {
            "mode": "safetensors",
            "provider": "",
            "provider_label": "",
            "base_url": "",
            "model": model_name,
            "gpu_index": gpu_index,
            "gpu_name": gpu_name,
            "local_path": local_path,
            "dtype": dtype_name,
            "sampling": sampling,
            "runtime": runtime,
        }
        return _result(llm, seed)

    @classmethod
    def IS_CHANGED(cls, gpu="0", model_dir="", model_file=MODEL_FILE_PLACEHOLDER,
                   dtype="fp16", unload_after_use=False, _llm_json="{}", **kwargs):
        return _seed_only_changed(
            _parse_json_obj(_llm_json),
            gpu=_s(gpu),
            model_dir=_s(model_dir),
            model_file=_s(model_file),
            dtype=_s(dtype),
            unload_after_use=bool(unload_after_use),
        )


# --------------------------------------------------------------------------- 节点 3：云端


class NL2PromptLLMCloud:
    """加载云端 LLM：OpenAI 兼容端点（deepseek / openai / anthropic / 自定义）。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "load"
    RETURN_TYPES = ("LLM_MODEL",)
    RETURN_NAMES = ("llm",)
    OUTPUT_NODE = False
    DESCRIPTION = (
        "云端 OpenAI 兼容端点：只产出 LLM_MODEL（base_url / model / 采样参数），"
        "api_key 由 providers 在调用时解析，绝不明文进入工作流。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        options = provider_options()
        return {
            "required": {
                "provider": (
                    options,
                    {
                        "default": "deepseek" if "deepseek" in options else options[0],
                        "tooltip": "供应商；base_url 留空时按供应商自动填充",
                    },
                ),
                "base_url": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "tooltip": "留空则按 provider 自动填充（custom 必须手动填）",
                    },
                ),
                "cloud_model": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "tooltip": "云端模型代号，如 deepseek-chat / gpt-4o-mini",
                    },
                ),
                "_llm_json": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "{}",
                        "tooltip": "内部 holder：采样参数 / 运行参数（不占节点正面空间）",
                    },
                ),
            }
        }

    def load(self, provider="deepseek", base_url="", cloud_model="", _llm_json="{}"):
        holder, sampling, runtime = _holder_parts(_llm_json)
        provider_key = _norm(provider) or "deepseek"

        resolved_base_url = _s(base_url) or provider_base_url(provider_key)
        # 云端不使用 GPU / 卸载开关
        runtime["unload_after_use"] = False
        runtime["port"] = 0
        seed = _actual_seed(sampling)

        llm = {
            "mode": "cloud",
            "provider": provider_key,
            "provider_label": provider_label(provider_key),
            "base_url": resolved_base_url,
            "model": _s(cloud_model),
            "gpu_index": -1,
            "gpu_name": "",
            "local_path": "",
            "sampling": sampling,
            "runtime": runtime,
        }
        return _result(llm, seed)

    @classmethod
    def IS_CHANGED(cls, provider="deepseek", base_url="", cloud_model="", _llm_json="{}", **kwargs):
        return _seed_only_changed(
            _parse_json_obj(_llm_json),
            provider=_norm(provider),
            base_url=_s(base_url),
            cloud_model=_s(cloud_model),
        )


__all__ = [
    "NL2PromptLLMGGUF",
    "NL2PromptLLMSafetensors",
    "NL2PromptLLMCloud",
    "MODEL_FILE_PLACEHOLDER",
    "MODEL_FILE_PLACEHOLDER_ZH",
    "MODEL_FILE_PLACEHOLDER_EN",
    "MODEL_FILE_PLACEHOLDERS",
    "is_model_placeholder",
    "DTYPE_CHOICES",
    "scan_safetensors",
    "detect_safetensors_dir",
    "detect_gguf_path",
]
