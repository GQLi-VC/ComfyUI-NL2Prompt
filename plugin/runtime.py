# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""本地 llama-server 生命周期管理（llm-runtime 负责）。

契约参考：docs/接口契约.md §2.2 / §6 / §8 / §11。

对外接口
--------
``llama_server_path() -> str``
    读 ``plugin/data/settings.json`` 的 ``llama_server_path``，缺失回落默认路径。
``ensure_runtime(llm: dict) -> dict``
    本地模式确保 llama-server 在跑，返回 ``{"base_url","model","port"}``；
    失败抛 :class:`RuntimeStartError`。
``stop_all() -> list[int]``
    terminate → 等 5s → kill，返回被停端口列表；``atexit`` 已注册。
``status() -> list[dict]``
    契约 §6 ``running`` 结构，顺带剔除已死进程。

关键约定
--------
* 端口从 **8081** 起自动挑空闲，跳过黑名单 ``{8080, 8188, 8199}``
  （8080 用户启动器占用、8188 正式 ComfyUI、8199 测试环境）。
* **LLM 一律绑 GPU1（2060），绝不占用 GPU0（留给 ComfyUI 出图）**。
  gpu_index 解析成 0 时会被强制改绑 1 并告警；确需放开可设
  ``ANIMA_LLM_ALLOW_GPU0=1``。
* 只用标准库：``subprocess`` / ``socket`` / ``urllib`` / ``threading``。
* 注册表由 :class:`threading.Lock` 保护，``stop_all`` 幂等。

Thinking 模型（实测关键项）
--------------------------
Qwen3.5-9B 默认带思考链，llama-server 会把思考写进 ``message.reasoning_content``，
``message.content`` **恒为空**；2048 max_tokens 也全部被思考吃掉（实测 44.8s / 2048 tok /
content 长度 0），契约 §7 的 JSON 任务必然失败。
llama.cpp 支持 ``--reasoning off``（本机 help 已确认），实测同任务 3.35s 就返回
**可直接 json.loads 的 content**。因此本模块默认附加 ``--reasoning off``，
可用 ``llm["runtime"]["reasoning"]``（``off|on|auto``）或环境变量
``ANIMA_LLM_REASONING`` 覆盖；启动前用 ``llama-server --help`` 探测该参数是否存在，
不存在则自动省略（兼容旧版 llama.cpp）。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:  # 包内导入（``import plugin.runtime`` / ComfyUI 的 ``.runtime``）
    from .providers import resolve_base_url  # type: ignore
except ImportError:  # 裸导入（``sys.path.insert(0, plugin_dir); import runtime``）
    from providers import resolve_base_url  # type: ignore

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_LLAMA_SERVER = ""  # 开源版留空：优先读 settings.json，未配置时由调用方提示
_DEFAULT_CTX = 8192
_DEFAULT_TIMEOUT = 120.0
_PORT_START = 8081
_PORT_END = 18000
_PORT_RESERVED = {8080, 8188, 8199}
_REUSE_WAIT = 5.0
_LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"
_SETTINGS_PATH = Path(__file__).resolve().parent / "data" / "settings.json"

_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0

#: port -> {
#:   "proc": Popen, "log_fh": file, "log_path": str, "key": (gguf, gpu, ctx),
#:   "model": str, "gpu_index": int, "ctx": int, "base_url": str, "started_at": float
#: }
_REGISTRY: dict = {}
_LOCK = threading.RLock()
_STOPPED = False

#: (server, flag) -> bool，缓存 ``llama-server --help`` 的能力探测结果
_FLAG_CACHE: dict = {}


class RuntimeStartError(Exception):
    """运行时启动失败。``code`` 对应契约 §8 错误码，``detail`` 为人话+日志尾部。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code)
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _warn(msg: str) -> None:
    try:
        print(f"[nl2prompt][runtime] {msg}", file=sys.stderr)
    except Exception:
        pass


def _read_settings() -> dict:
    """读 settings.json，异常返回空 dict（绝不因配置损坏崩溃）。"""
    try:
        if not _SETTINGS_PATH.is_file():
            return {}
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        _warn(f"读取 settings.json 失败（用默认值）：{type(exc).__name__}")
        return {}


def _parse_gpu_index(raw, default: int = 1) -> int:
    """解析 gpu_index。

    接受 ``1`` / ``"1"`` / ``"1: NVIDIA GeForce RTX 2060 (12288 MiB)"``。
    """
    try:
        if isinstance(raw, bool):
            return default
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        if not text:
            return default
        head = text.split(":", 1)[0].strip()
        return int(head)
    except Exception:  # noqa: BLE001
        return default


def _enforce_gpu_policy(gpu_index: int) -> int:
    """LLM 一律绑 GPU1（用户硬约束：GPU0 留给 ComfyUI 出图）。"""
    try:
        if gpu_index != 0:
            return gpu_index
        if os.environ.get("ANIMA_LLM_ALLOW_GPU0", "").strip() in ("1", "true", "True"):
            _warn("ANIMA_LLM_ALLOW_GPU0=1，允许 LLM 使用 GPU0（不推荐，会与 ComfyUI 抢显存）")
            return 0
        _warn("gpu_index=0 被策略改绑到 GPU1（GPU0 保留给 ComfyUI；"
              "如需放开请设环境变量 ANIMA_LLM_ALLOW_GPU0=1）")
        return 1
    except Exception:  # noqa: BLE001
        return 1


def _is_port_free(port: int) -> bool:
    """用 127.0.0.1 探测端口是否可绑定。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if _IS_WINDOWS:
            # 避免 TIME_WAIT 影响探测
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _pick_port(exclude=()) -> int:
    """从 8081 起挑一个空闲且未被本注册表占用的端口。"""
    taken = set(exclude) | set(_PORT_RESERVED)
    with _LOCK:
        taken |= set(_REGISTRY.keys())
    for port in range(_PORT_START, _PORT_END):
        if port in taken:
            continue
        if _is_port_free(port):
            return port
    raise RuntimeStartError("runtime_start_failed", "找不到可用端口（8081 起全部占用）")


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    """``GET http://127.0.0.1:<port>/health`` 是否 200。"""
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", resp.getcode())) == 200
    except Exception:  # noqa: BLE001
        return False


def _supports_flag(server: str, flag: str) -> bool:
    """用 ``llama-server --help`` 探测是否支持某参数（结果缓存，失败视为不支持）。"""
    key = (server, flag)
    cached = _FLAG_CACHE.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        proc = subprocess.run(
            [server, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        ok = flag in text
    except Exception as exc:  # noqa: BLE001
        _warn(f"探测 {flag} 失败（按不支持处理）：{type(exc).__name__}")
        ok = False
    _FLAG_CACHE[key] = ok
    return ok


def _resolve_reasoning_mode(runtime_cfg: dict) -> str:
    """思考模式：默认 ``off``（thinking 模型会把 content 吃空，见模块 docstring）。"""
    raw = None
    try:
        raw = runtime_cfg.get("reasoning")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        raw = os.environ.get("ANIMA_LLM_REASONING")
    mode = str(raw or "off").strip().lower()
    if mode in ("0", "false", "no", "off", "关闭", "禁用"):
        return "off"
    if mode in ("1", "true", "yes", "on", "开启"):
        return "on"
    if mode in ("auto", "默认"):
        return "auto"
    _warn(f"未知 reasoning 取值 {raw!r}，回落 off")
    return "off"


def _tail(path: Path, max_chars: int = 500) -> str:
    """读日志尾部 ``max_chars`` 字（失败返回空串）。"""
    try:
        if not path.is_file():
            return ""
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_chars * 4))
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        return text[-max_chars:]
    except Exception:  # noqa: BLE001
        return ""


def _resolve_gguf(llm: dict) -> str:
    """取 gguf 绝对路径：优先 ``local_path``，兼容 ``model`` 是路径的情况。"""
    for field in ("local_path", "model_file", "model", "gguf"):
        val = llm.get(field)
        if isinstance(val, str) and val.strip().lower().endswith(".gguf"):
            try:
                return str(Path(val.strip()).expanduser().resolve())
            except Exception:  # noqa: BLE001
                return val.strip()
    val = llm.get("local_path")
    if isinstance(val, str) and val.strip():
        try:
            return str(Path(val.strip()).expanduser().resolve())
        except Exception:  # noqa: BLE001
            return val.strip()
    return ""


def _model_name(gguf_path: str) -> str:
    try:
        return Path(gguf_path).stem or gguf_path
    except Exception:  # noqa: BLE001
        return gguf_path or "local"


def _terminate(entry: dict, wait: float = _REUSE_WAIT) -> None:
    """terminate → 等 wait 秒 → kill；同时关闭日志句柄。"""
    proc = entry.get("proc")
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=wait)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            _warn(f"停止进程异常：{type(exc).__name__}")
    fh = entry.get("log_fh")
    if fh is not None:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 对外接口（契约 §11）
# ---------------------------------------------------------------------------


def llama_server_path() -> str:
    """llama-server 可执行文件路径（settings.json 可覆盖，缺失用默认）。"""
    try:
        settings = _read_settings()
        val = settings.get("llama_server_path")
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception as exc:  # noqa: BLE001
        _warn(f"读取 llama_server_path 失败：{type(exc).__name__}")
    return _DEFAULT_LLAMA_SERVER


def ensure_runtime(llm: dict) -> dict:
    """确保 LLM 运行时可用（契约 §11 + v3 safetensors）。

    本地 GGUF：复用/启动 llama-server，返回
    ``{"base_url": "http://127.0.0.1:<port>/v1", "model": "<stem>", "port": <port>}``
    并在返回值里附带实际 ``gpu_index`` / ``ctx`` / ``reasoning`` 便于 debug。

    safetensors（v3）：委托 :mod:`llm_local_hf` 加载 HF 模型，返回
    ``{"base_url": "", "model": "<目录名>", "port": 0, "mode": "safetensors",
    "device": "cuda:N", "vram_gb": <float>}``。
    **该模式没有 HTTP 端点**，调用方必须走 ``llm_local_hf.generate()``，
    不能拿 base_url 发请求（详见 ``release_notes`` / lead 集成说明）。

    云端：不起子进程，返回
    ``{"base_url": <resolve_base_url>, "model": <cloud_model>, "port": 0}``。

    :raises RuntimeStartError: ``local_model_missing`` / ``runtime_start_failed``
    :raises llm_local_hf.LocalModelError: safetensors 各错误码
    """
    if not isinstance(llm, dict):
        raise RuntimeStartError("runtime_start_failed", "LLM_MODEL 输入不是 dict")

    # ---------------- 云端 ----------------
    mode = str(llm.get("mode") or "local").strip().lower()
    if mode in ("cloud", "云端", "云端模型"):
        provider = llm.get("provider") or "deepseek"
        base_url = resolve_base_url(provider, llm.get("base_url"))
        model = (llm.get("cloud_model") or llm.get("model") or "").strip()
        if not base_url:
            _warn(f"云端模式 base_url 为空（provider={provider!r}），交由调用方判定 llm_key_missing/参数错误")
        return {"base_url": base_url or "", "model": model, "port": 0}

    # ---------------- safetensors（v3） ----------------
    if mode in ("safetensors", "hf", "st", "transformers"):
        result = _ensure_safetensors(llm)
        return result

    # ---------------- 本地 GGUF ----------------
    gguf = _resolve_gguf(llm)
    if not gguf:
        raise RuntimeStartError("local_model_missing", "<未提供 local_path>")
    if not Path(gguf).is_file():
        raise RuntimeStartError("local_model_missing", gguf)

    runtime_cfg = llm.get("runtime") if isinstance(llm.get("runtime"), dict) else {}
    try:
        ctx = int(runtime_cfg.get("ctx") or _DEFAULT_CTX)
    except Exception:  # noqa: BLE001
        ctx = _DEFAULT_CTX
    if ctx < 512:
        ctx = 512
    try:
        timeout = float(runtime_cfg.get("timeout") or _DEFAULT_TIMEOUT)
    except Exception:  # noqa: BLE001
        timeout = _DEFAULT_TIMEOUT
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT
    try:
        ngl = int(runtime_cfg.get("ngl") or 99)
    except Exception:  # noqa: BLE001
        ngl = 99

    gpu_index = _enforce_gpu_policy(_parse_gpu_index(llm.get("gpu_index"), default=1))
    reasoning_mode = _resolve_reasoning_mode(runtime_cfg)
    reuse_key = (gguf, gpu_index, ctx, reasoning_mode)

    # ---- 复用已就绪实例 ----
    with _LOCK:
        stale = []
        for port, entry in _REGISTRY.items():
            if entry.get("key") != reuse_key:
                continue
            proc = entry.get("proc")
            if proc is not None and proc.poll() is None and _health_ok(port, timeout=1.5):
                _warn(f"复用已有 llama-server：port={port} model={entry.get('model')} gpu={gpu_index}")
                return {
                    "base_url": entry.get("base_url") or f"http://127.0.0.1:{port}/v1",
                    "model": entry.get("model") or _model_name(gguf),
                    "port": port,
                    "gpu_index": gpu_index,
                    "ctx": ctx,
                    "reasoning": reasoning_mode,
                }
            stale.append(port)
        for port in stale:
            _warn(f"清理失效实例：port={port}")
            _terminate(_REGISTRY.pop(port, {}), wait=2.0)

    server = llama_server_path()
    if not Path(server).is_file():
        raise RuntimeStartError("runtime_start_failed", f"llama-server 不存在：{server}")

    # ---- 选端口 ----
    want_port = 0
    try:
        want_port = int(runtime_cfg.get("port") or 0)
    except Exception:  # noqa: BLE001
        want_port = 0
    with _LOCK:
        occupied = set(_REGISTRY.keys())
    if want_port > 0 and want_port not in _PORT_RESERVED and want_port not in occupied and _is_port_free(want_port):
        port = want_port
    else:
        port = _pick_port(exclude=occupied)

    # ---- 起进程 ----
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _warn(f"创建日志目录失败：{type(exc).__name__}")
    log_path = _LOG_DIR / f"llama_{port}.log"

    cmd = [
        server,
        "-m", gguf,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-ngl", str(ngl),
        "-c", str(ctx),
    ]
    # 关思考：否则 content 恒空（实测见模块 docstring）。旧版不支持时自动省略。
    if _supports_flag(server, "--reasoning"):
        cmd += ["--reasoning", reasoning_mode]
    else:
        _warn("当前 llama-server 不支持 --reasoning，思考模式无法关闭；"
              "thinking 模型将只返回 reasoning_content，content 可能为空")

    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    # 降低显存碎片/日志噪声：不额外加未验证参数，保持任务书基线

    log_fh = None
    try:
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        log_fh.write("# cmd: " + " ".join(cmd) + "\n")
        log_fh.write(f"# CUDA_VISIBLE_DEVICES={gpu_index}\n")
        log_fh.flush()
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(Path(server).parent),
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass
        raise RuntimeStartError(
            "runtime_start_failed", f"启动 llama-server 失败：{type(exc).__name__}: {exc}"
        ) from exc

    entry = {
        "proc": proc,
        "log_fh": log_fh,
        "log_path": str(log_path),
        "key": reuse_key,
        "model": _model_name(gguf),
        "gpu_index": gpu_index,
        "ctx": ctx,
        "ngl": ngl,
        "reasoning": reasoning_mode,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "started_at": time.time(),
    }
    with _LOCK:
        _REGISTRY[port] = entry

    _warn(f"启动 llama-server：port={port} gpu={gpu_index} ctx={ctx} ngl={ngl} "
          f"reasoning={reasoning_mode} log={log_path}")

    # ---- 就绪轮询 ----
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            detail = (
                f"llama-server 退出（exit={proc.returncode}） log={log_path}\n"
                + _tail(log_path, 500)
            )
            with _LOCK:
                _REGISTRY.pop(port, None)
            _terminate(entry, wait=2.0)
            raise RuntimeStartError("runtime_start_failed", detail)
        if _health_ok(port, timeout=2.0):
            elapsed = time.time() - entry["started_at"]
            _warn(f"llama-server 就绪：port={port} 耗时 {elapsed:.1f}s")
            return {
                "base_url": entry["base_url"],
                "model": entry["model"],
                "port": port,
                "gpu_index": gpu_index,
                "ctx": ctx,
                "reasoning": reasoning_mode,
            }
        time.sleep(0.5)

    detail = f"llama-server {timeout:.0f}s 内未就绪 log={log_path}\n" + _tail(log_path, 500)
    with _LOCK:
        _REGISTRY.pop(port, None)
    _terminate(entry, wait=2.0)
    raise RuntimeStartError("runtime_start_failed", detail)


def stop_all() -> list[int]:
    """停止全部 llama-server，返回被停端口列表（幂等，可重复调用）。

    v3 起同时卸载驻留的 safetensors 模型（``llm_local_hf.unload()``），
    保证 ``atexit`` / ``POST /runtime/stop`` 一条路径就能释放全部显存。
    """
    stopped: list[int] = []
    with _LOCK:
        entries = list(_REGISTRY.items())
        _REGISTRY.clear()
        _STOPPED = True
    for port, entry in entries:
        try:
            _terminate(entry, wait=_REUSE_WAIT)
            stopped.append(port)
        except Exception as exc:  # noqa: BLE001
            _warn(f"stop_all 处理 port={port} 异常：{type(exc).__name__}")
    if stopped:
        _warn(f"已停止 llama-server 端口：{stopped}")
    # safetensors 驻留模型一并卸载（失败不影响端口清理结果）
    try:
        unload_safetensors()
    except Exception as exc:  # noqa: BLE001
        _warn(f"卸载 safetensors 模型异常：{type(exc).__name__}")
    return stopped


def status() -> list[dict]:
    """契约 §6 ``running``：``[{"port":8081,"model":"...","gpu_index":1}, ...]``。

    v3 起 safetensors 驻留模型也出现在列表里（``port=0`` + ``mode="safetensors"``）。
    """
    out: list[dict] = []
    dead: list[int] = []
    with _LOCK:
        for port, entry in list(_REGISTRY.items()):
            proc = entry.get("proc")
            if proc is None or proc.poll() is not None:
                dead.append(port)
                continue
            item = {
                "port": port,
                "model": entry.get("model") or "",
                "gpu_index": entry.get("gpu_index", 0),
            }
            # 附加字段（契约允许多余字段，前端只取前三个）
            item["ctx"] = entry.get("ctx")
            item["reasoning"] = entry.get("reasoning")
            item["mode"] = "local"
            item["pid"] = getattr(proc, "pid", None)
            item["base_url"] = entry.get("base_url")
            item["uptime_sec"] = round(time.time() - float(entry.get("started_at") or time.time()), 1)
            out.append(item)
        for port in dead:
            entry = _REGISTRY.pop(port, None)
            if entry:
                _warn(f"status 清理已退出进程：port={port}")
                try:
                    _terminate(entry, wait=0.5)
                except Exception:  # noqa: BLE001
                    pass
    out.sort(key=lambda d: d["port"])
    # safetensors 驻留条目（port=0，不占端口）
    try:
        hf_status = _safetensors_status()
    except Exception:  # noqa: BLE001
        hf_status = {"loaded": False}
    if isinstance(hf_status, dict) and hf_status.get("loaded"):
        out.append(
            {
                "port": 0,
                "model": hf_status.get("model") or "",
                "gpu_index": hf_status.get("gpu_index", 0),
                "mode": "safetensors",
                "device": hf_status.get("device", ""),
                "dtype": hf_status.get("dtype", ""),
                "vram_gb": hf_status.get("vram_gb", 0.0),
                "base_url": "",
            }
        )
    return out


def health(port: int) -> bool:
    """单端口健康探针（给 /runtime/status 或调试用）。"""
    return _health_ok(int(port), timeout=1.5)


# ---------------------------------------------------------------------------
# safetensors 运行时（v3）：复用 / 卸载
# ---------------------------------------------------------------------------


def _hf_module():
    """惰性加载同级 ``llm_local_hf``；不可用返回 ``None``（绝不抛异常）。"""
    package = __package__ or ""
    if package:
        try:
            import importlib

            return importlib.import_module("." + "llm_local_hf", package)
        except Exception:  # noqa: BLE001
            pass
    try:
        import llm_local_hf  # noqa: PLC0415

        return llm_local_hf
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.util

        path = Path(__file__).resolve().parent / "llm_local_hf.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_nl2prompt_llm_local_hf_rt", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def _ensure_safetensors(llm: dict) -> dict:
    """加载/复用 safetensors 模型，返回与 ``ensure_runtime`` 统一的 dict。"""
    module = _hf_module()
    if module is None:
        raise RuntimeStartError(
            "safetensors_unavailable",
            "llm_local_hf.py 不可用（尚未就绪或导入失败）",
        )
    model_dir = llm.get("local_path") or llm.get("model_dir") or ""
    if not isinstance(model_dir, str) or not model_dir.strip():
        raise RuntimeStartError("safetensors_need_dir", "未指定 safetensors 模型目录")
    gpu_index = _enforce_gpu_policy(_parse_gpu_index(llm.get("gpu_index"), default=1))
    dtype = str(llm.get("dtype") or "fp16")
    ensure = getattr(module, "ensure_loaded", None)
    try:
        if callable(ensure):
            info = ensure(
                model_dir=model_dir,
                gpu_index=gpu_index,
                dtype=dtype,
                model_file=llm.get("model_file") or "",
            )
        else:  # 旧版模块没有 ensure_loaded
            info = module.load(
                model_dir=model_dir,
                gpu_index=gpu_index,
                dtype=dtype,
                model_file=llm.get("model_file") or "",
            )
    except Exception as exc:  # noqa: BLE001 - 错误码原样透传给调用方
        code = getattr(exc, "code", None) or "safetensors_load_failed"
        detail = getattr(exc, "detail", None) or str(exc)
        raise RuntimeStartError(code, detail) from exc
    if not isinstance(info, dict):
        raise RuntimeStartError("safetensors_load_failed", f"加载返回异常：{info!r}")
    return {
        "base_url": "",
        "model": info.get("model") or Path(model_dir).name,
        "port": 0,
        "mode": "safetensors",
        "device": info.get("device", ""),
        "gpu_index": info.get("gpu_index", gpu_index),
        "dtype": info.get("dtype", dtype),
        "vram_gb": info.get("vram_gb", 0.0),
        "load_sec": info.get("load_sec", 0.0),
    }


def _safetensors_status() -> dict:
    module = _hf_module()
    if module is None:
        return {"loaded": False}
    fn = getattr(module, "status", None)
    if not callable(fn):
        return {"loaded": False}
    try:
        value = fn()
        return value if isinstance(value, dict) else {"loaded": False}
    except Exception:  # noqa: BLE001
        return {"loaded": False}


def unload_safetensors() -> bool:
    """卸载驻留的 safetensors 模型并释放显存（幂等）。"""
    module = _hf_module()
    if module is None:
        return False
    fn = getattr(module, "unload", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception as exc:  # noqa: BLE001
        _warn(f"unload_safetensors 失败：{type(exc).__name__}")
        return False


def unload(llm=None) -> bool:
    """按 ``llm["mode"]`` 卸载对应运行时（Composer 读 ``unload_after_use`` 后调用）。

    * ``safetensors``：卸载 HF 模型（释放显存）
    * ``local``：停止该模型对应的 llama-server 进程
    * ``cloud``：无状态，返回 False

    :returns: 是否真的释放了资源
    """
    mode = ""
    if isinstance(llm, dict):
        mode = str(llm.get("mode") or "").strip().lower()

    if mode in ("safetensors", "hf", "st", "transformers"):
        return unload_safetensors()

    if mode in ("cloud", "云端", "云端模型"):
        return False

    if mode in ("local", "", "gguf"):
        target = ""
        if isinstance(llm, dict):
            target = _resolve_gguf(llm) if hasattr(llm, "get") else ""
        released = False
        with _LOCK:
            ports = list(_REGISTRY.items())
        for port, entry in ports:
            if target and entry.get("key") and entry["key"][0] != target:
                continue
            with _LOCK:
                _REGISTRY.pop(port, None)
            try:
                _terminate(entry, wait=_REUSE_WAIT)
                released = True
                _warn(f"已停止 llama-server 端口 {port}（unload_after_use）")
            except Exception as exc:  # noqa: BLE001
                _warn(f"停止端口 {port} 异常：{type(exc).__name__}")
        return released

    return False


def loaded_info() -> dict:
    """当前运行时状态汇总（供调试 / ``/runtime/status`` 扩展）。

    ``llama_servers`` 只含真实的 llama-server 进程；safetensors 驻留模型单独一项。
    """
    servers = [item for item in status() if item.get("mode") != "safetensors"]
    return {
        "llama_servers": servers,
        "safetensors": _safetensors_status(),
    }


# ---------------------------------------------------------------------------
# 退出兜底
# ---------------------------------------------------------------------------

atexit.register(stop_all)


__all__ = [
    "RuntimeStartError",
    "llama_server_path",
    "ensure_runtime",
    "stop_all",
    "status",
    "health",
    "unload",
    "unload_safetensors",
    "loaded_info",
]
