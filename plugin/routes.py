"""HTTP 路由（backend-core）。契约 ``docs/接口契约.md`` §6。

全部注册在 ``server.PromptServer.instance.routes``（aiohttp ``RouteTableDef``），
前缀 ``/nl2prompt``（旧前缀 ``/nl2prompt`` 保留为别名）：

=============================  ======  ======================================
路径                            方法    说明
=============================  ======  ======================================
``/nl2prompt/gpus``              GET     CUDA 设备枚举
``/nl2prompt/scan_gguf``         GET     递归扫描 .gguf（深度 ≤6、上限 500）
``/nl2prompt/default_system_prompt`` GET 默认 system prompt 全文
``/nl2prompt/secret``            POST    写入 / 清除 api_key（**响应绝不回显**）
``/nl2prompt/secret_status``     GET     仅返回 configured / source
``/nl2prompt/runtime/start``     POST    转发 ``runtime.ensure_runtime``
``/nl2prompt/runtime/stop``      POST    转发 ``runtime.stop_all``
``/nl2prompt/runtime/status``    GET     转发 ``runtime.status``
``/nl2prompt/catalog``           GET     角色 / 风格目录（供前端自动补全）
=============================  ======  ======================================

容错：``runtime.py`` / ``store.py`` 由他人并行开发，缺失时返回
``{"ok": false, "error": "...", ...}`` 而不是抛 500。所有异常都被就地捕获。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path

try:  # aiohttp 由 ComfyUI 提供；纯函数单测环境可能没有
    from aiohttp import web
except Exception:  # noqa: BLE001 - 缺失时不注册路由，但不影响其它模块
    web = None

_PLUGIN_DIR = Path(__file__).resolve().parent
_LOG_PREFIX = "[NL2Prompt.routes]"

#: 契约 §6：扫描上限
MAX_SCAN_FILES = 500
MAX_SCAN_DEPTH = 6

_REGISTERED = False


def _safe_text(text):
    """把文本降级为当前 stdout/stderr 都能编码的字符串（P0 级防护）。

    请求路径、异常文本、traceback 片段都可能含 gbk 无法编码的字符；直接
    print 到 gbk stderr 会抛 ``UnicodeEncodeError`` 并可能打死 ComfyUI 的
    ``prompt_worker`` 线程。所有路由日志必须先过本函数。
    """
    raw = text if isinstance(text, str) else str(text)
    for stream in (sys.stderr, sys.stdout):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            raw = raw.encode(encoding, "replace").decode(encoding, "replace")
        except Exception:  # noqa: BLE001 - 未知编码名等，跳过该流继续
            pass
    return raw


def _log(message):
    """打印日志；输出先过 :func:`_safe_text`，绝不因编码问题打死 worker 线程。"""
    print(f"{_LOG_PREFIX} {_safe_text(message)}", flush=True)


# --------------------------------------------------------------------------- 同级模块加载


def _load_sibling(name):
    """三级容错加载同级模块：包内相对导入 → 按文件路径加载 → 放弃。"""
    package = __package__ or ""
    if package:
        try:
            return importlib.import_module("." + name, package)
        except Exception:  # noqa: BLE001
            pass
    try:
        path = _PLUGIN_DIR / (name + ".py")
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(f"_anima_nl_routes_{name}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - 损坏模块不得影响路由注册
        _log(f"加载 {name}.py 失败：{traceback.format_exc(limit=2)}")
        return None


_llm_client = _load_sibling("llm_client")
_nodes_mod = _load_sibling("nodes")


# --------------------------------------------------------------------------- 响应工具


def _dumps(payload):
    return json.dumps(payload, ensure_ascii=False, default=str)


def _json(payload, status=200):
    """统一的 JSON 响应（中文不转义；``web`` 缺失时返回 dict 便于单测）。"""
    if web is None:
        return payload
    return web.json_response(payload, status=status, dumps=_dumps)


#: 错误码 -> 英文通用文案（i18n）。
#:
#: 有了这张表，**所有** ``_error(code, "中文详情")`` 调用点都会自动获得 ``detail_en``，
#: 不需要逐个调用点传第三个参数，新加的调用点也自动受益。
#: 只覆盖高频 / 前端字典里没有的 code；查不到的 code 回落中文，
#: 前端再按 ``err.<code>`` 字典兜底（不会显示空白）。
#: 约束：本表全部为纯 ASCII，满足 ComfyUI 的 GBK stderr 红线。
_ERR_EN = {
    # ---- LLM 连接与调用
    "llm_not_connected": (
        "LLM is not connected. Wire an LLM node into the llm input, "
        "or set use_llm=false to use template composition."
    ),
    "llm_key_missing": (
        "No API key configured. Set it in the LLM settings panel, "
        "or via the ANIMA_LLM_API_KEY environment variable."
    ),
    "llm_timeout": (
        "The LLM request timed out. Try a smaller max_tokens, "
        "a faster model, or raise the timeout."
    ),
    "llm_bad_json": (
        "The LLM reply was not valid JSON. Retry, or switch to a model "
        "that follows the JSON format more reliably."
    ),
    "llm_skill_check_failed": (
        "The LLM output failed skill validation twice; "
        "fell back to template composition."
    ),
    "llm_empty_positive": "The LLM returned an empty positive prompt.",
    "llm_unexpected_error": "Unexpected error while calling the LLM.",
    "llm_error": "The LLM call failed.",
    # ---- 本地运行时
    "local_model_missing": (
        "Local GGUF model file is missing. Pick a model file in the LLM node."
    ),
    "local_model_oom": (
        "Out of VRAM while loading the model. Try a smaller quantization "
        "(Q4/Q5), or a GGUF model instead of safetensors."
    ),
    "runtime_start_failed": (
        "Failed to start the local LLM runtime. Check the llama-server path "
        "in settings, or switch to another mode."
    ),
    "safetensors_unavailable": (
        "The safetensors runtime is unavailable (llm_local_hf.py missing or "
        "has no chat()). Use GGUF or cloud mode instead."
    ),
    "safetensors_failed": (
        "The safetensors runtime failed. See detail for the original error."
    ),
    # ---- 云端配置缺失（本地校验错误，不是 HTTP 故障）
    "cloud_base_url_empty": (
        "Cloud base_url is empty. Pick a provider or fill in base_url "
        "in the LLM node."
    ),
    "cloud_model_empty": "Cloud model name is empty; please fill in cloud_model.",
    # ---- 请求与设置
    "bad_request": "Invalid request body.",
    "bad_locale": "ui_locale must be one of auto / zh / en.",
    "store_unavailable": "The settings store is unavailable.",
    "settings_save_failed": "Failed to write the settings.",
    "secret_write_failed": "Failed to write the secret.",
    # ---- skill
    "skill_not_found": "Skill not found.",
    "skill_protected": "The default skill is protected and cannot be deleted.",
    "bad_skill_id": (
        "Invalid skill id. Only lowercase letters, digits and hyphens are allowed."
    ),
    "skill_exists": "A skill with this id already exists.",
    "skill_unavailable": "The skill store is unavailable.",
    "skill_invalid": "The skill content failed validation.",
    "skill_save_failed": "Failed to save the skill.",
    "skill_load_failed": "Failed to load the skill.",
    "skill_delete_failed": "Failed to delete the skill.",
    "id_mismatch": "Skill id in the body does not match the requested id.",
    # ---- 运行时状态与兜底
    "runtime_unavailable": "The runtime module is unavailable.",
    "runtime_status_failed": "Failed to read the runtime status.",
    "internal_error": "Internal server error.",
}


def _error_en_for(code):
    """按错误码取英文通用文案；支持动态码前缀，取不到返回空串。"""
    key = str(code or "")
    text = _ERR_EN.get(key)
    if text:
        return text
    if key.startswith("llm_http_"):
        status = key[len("llm_http_") :] or "0"
        return (
            f"The LLM endpoint returned an error (status {status}). "
            "Check base_url, provider and model."
        )
    if key.startswith("safetensors_"):
        return "The safetensors runtime failed. See detail for the original error."
    return ""


def _error(code, detail="", detail_en="", **extra):
    """统一错误体（v3：双语）。

    * ``detail`` 保留给旧前端（等于中文详情）；
    * ``detail_zh`` / ``detail_en`` 永远存在；
    * ``detail_en`` 未显式传入时自动查 :data:`_ERR_EN`（含 ``llm_http_<n>`` 动态码），
      仍查不到才回落中文，由前端 ``err.<code>`` 字典兜底。
    """
    payload = {"ok": False, "error": str(code) if code else "internal_error"}
    zh = str(detail)[:500] if detail else ""
    if detail_en:
        en = str(detail_en)[:500]
    else:
        en = _error_en_for(code)[:500] or zh
    if zh:
        payload["detail"] = zh
    payload["detail_zh"] = zh
    payload["detail_en"] = en
    payload.update(extra)
    return payload


async def _read_json(request):
    try:
        raw = await request.text()
    except Exception:  # noqa: BLE001
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _guard(handler):
    """把 handler 包成「永不抛异常」的 aiohttp 视图。"""

    async def wrapper(request):
        try:
            result = await handler(request)
            if web is not None and isinstance(result, web.Response):
                return result
            return _json(result)
        except Exception as exc:  # noqa: BLE001 - 契约：绝不让路由抛 500 未捕获
            _log(f"路由 {getattr(request, 'path', '?')} 未捕获异常：{traceback.format_exc(limit=4)}")
            return _json(
                _error("internal_error", f"{type(exc).__name__}: {exc}")
            )

    return wrapper


# --------------------------------------------------------------------------- GPU


def _enumerate_gpus():
    """返回 ``[{"index":0,"name":"...","total_mb":12288}]``（契约 §6）。

    与 ``NL2PromptLoadLLM.gpu`` 的 COMBO **共用 ``nodes.list_physical_gpus()``**，
    保证两处一致；该实现优先 nvidia-smi（列物理卡），torch 兜底。
    """
    if _nodes_mod is not None:
        function = getattr(_nodes_mod, "list_physical_gpus", None)
        if callable(function):
            try:
                result = function()
            except Exception:  # noqa: BLE001
                _log(f"list_physical_gpus 失败：{traceback.format_exc(limit=2)}")
                result = None
            if isinstance(result, list):
                gpus = []
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    try:
                        index = int(item.get("index", 0))
                    except (TypeError, ValueError):
                        continue
                    try:
                        total_mb = int(item.get("total_mb") or 0)
                    except (TypeError, ValueError):
                        total_mb = 0
                    gpus.append(
                        {
                            "index": index,
                            "name": str(item.get("name") or f"GPU {index}"),
                            "total_mb": total_mb,
                        }
                    )
                return gpus

    # 兜底：nodes 不可用时用最小 torch 实现（仅在节点模块彻底坏掉时走到）
    return _enumerate_gpus_fallback()


def _enumerate_gpus_fallback():
    result = []
    try:
        import torch  # noqa: PLC0415

        count = int(torch.cuda.device_count())
        for index in range(count):
            try:
                name = str(torch.cuda.get_device_name(index)).strip()
            except Exception:  # noqa: BLE001
                name = f"GPU {index}"
            total_mb = 0
            try:
                _free, total = torch.cuda.mem_get_info(index)
                # 四舍五入到 MiB（契约 §6 示例 total_mb: 12288）
                total_mb = round(total / (1024 * 1024))
            except Exception:  # noqa: BLE001
                total_mb = 0
            result.append({"index": index, "name": name, "total_mb": total_mb})
    except Exception:  # noqa: BLE001 - 无 torch / 无 CUDA
        return []
    return result


async def _handle_gpus(request):
    return {"gpus": _enumerate_gpus()}


# --------------------------------------------------------------------------- 扫描 GGUF


def _scan_by_extension(root_path, extension):
    """通用递归扫描：深度 ≤6、上限 500、只返回指定扩展名。"""
    payload = {"files": [], "root": "", "error": None}

    raw = root_path if isinstance(root_path, str) else ""
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        payload["error"] = "path_not_found"
        return payload

    try:
        root = Path(raw).expanduser()
    except (OSError, ValueError):
        payload["error"] = "path_not_found"
        return payload

    try:
        if not root.exists() or not root.is_dir():
            payload["error"] = "path_not_found"
            return payload
        root = root.resolve()
    except (OSError, ValueError):
        payload["error"] = "path_not_found"
        return payload

    payload["root"] = str(root)
    lowered = extension.lower()
    files = []

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            try:
                relative = os.path.relpath(dirpath, root)
            except ValueError:
                dirnames[:] = []
                continue
            depth = 0 if relative in (".", "") else len(Path(relative).parts)
            if depth > MAX_SCAN_DEPTH:
                dirnames[:] = []
                continue

            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.lower().endswith(lowered):
                    continue
                full_path = Path(dirpath) / filename
                try:
                    size_bytes = full_path.stat().st_size
                except OSError:
                    size_bytes = 0
                files.append(
                    {
                        "name": filename,
                        "path": str(full_path),
                        "size_mb": round(size_bytes / (1024 * 1024), 1),
                        "dir": "" if depth == 0 else str(Path(relative)).replace("\\", "/"),
                    }
                )
                if len(files) >= MAX_SCAN_FILES:
                    break
            if len(files) >= MAX_SCAN_FILES:
                break
    except Exception as exc:  # noqa: BLE001 - 扫描中断也返回已找到的部分
        payload["files"] = files
        payload["error"] = f"scan_failed: {exc}"
        return payload

    files.sort(key=lambda item: item["path"].lower())
    payload["files"] = files
    return payload


def scan_gguf(root_path):
    """契约 §6 ``/scan_gguf``：递归深度 ≤6、上限 500、只返回 ``.gguf``。"""
    return _scan_by_extension(root_path, ".gguf")


def scan_safetensors(root_path):
    """``/scan_safetensors``：只返回**所在目录含 config.json** 的 ``.safetensors``。

    HuggingFace 模型目录必须带 ``config.json`` 才能被 ``AutoModelForCausalLM`` 加载，
    所以这里直接过滤掉裸权重文件，避免用户选到加载不了的东西。
    """
    payload = _scan_by_extension(root_path, ".safetensors")
    files = payload.get("files") or []
    if payload.get("error"):
        payload["total_found"] = len(files)
        payload["files"] = []
        return payload

    kept = []
    for item in files:
        try:
            parent = Path(item.get("path") or "").parent
        except (OSError, ValueError):
            continue
        try:
            if (parent / "config.json").is_file():
                kept.append(item)
        except OSError:
            continue

    payload["total_found"] = len(files)
    payload["files"] = kept
    payload["require_config"] = True
    return payload


async def _handle_scan_gguf(request):
    path = ""
    try:
        path = request.query.get("path", "") or ""
    except Exception:  # noqa: BLE001
        path = ""
    return scan_gguf(path)


async def _handle_scan_safetensors(request):
    path = ""
    try:
        path = request.query.get("path", "") or ""
    except Exception:  # noqa: BLE001
        path = ""
    return scan_safetensors(path)


# --------------------------------------------------------------------------- 默认 system prompt


async def _handle_default_prompt(request):
    prompt = ""
    if _nodes_mod is not None:
        function = getattr(_nodes_mod, "_default_system_prompt", None)
        if callable(function):
            try:
                prompt = function()
            except Exception:  # noqa: BLE001
                prompt = ""
        if not prompt:
            prompt = getattr(_nodes_mod, "DEFAULT_SYSTEM_PROMPT", "")
    return {"prompt": prompt or ""}


# --------------------------------------------------------------------------- 密钥


async def _handle_secret(request):
    body = await _read_json(request)

    # 安全：必须**显式**提供 api_key 字段。
    # 请求体损坏 / 字段缺失时绝不能当成「空串」处理，否则一个畸形请求
    # 会静默清除用户已保存的密钥。清除只能由 {"api_key": ""} 显式触发。
    if not isinstance(body, dict) or "api_key" not in body:
        return _error(
            "bad_request",
            '请求体必须是 JSON 对象且显式包含 api_key 字段（清除请传 {"api_key": ""}）',
        )

    key = body.get("api_key")
    if key is None:
        key = ""
    if not isinstance(key, str):
        return _error("bad_request", "api_key 必须是字符串")

    # provider 可选：缺省 / custom → providers 写入 _default 槽位（契约 §13.2）
    provider = body.get("provider", "")
    if provider is None:
        provider = ""
    if not isinstance(provider, str):
        return _error("bad_request", "provider 必须是字符串")

    if _llm_client is None:
        return _error("secret_write_failed", "llm_client.py 不可用，无法写入密钥")

    function = getattr(_llm_client, "save_api_key", None)
    if not callable(function):
        return _error("secret_write_failed", "llm_client.save_api_key 缺失")

    try:
        # 契约 §13.2：写入统一由 providers.save_api_key 完成（llm_client 只转发）
        function(provider, key)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None) or "secret_write_failed"
        return _error(code, str(exc))

    # 回读状态；响应严格按契约 §6：``{"ok":true,"configured":true}``，绝不回显 api_key
    configured, _source = _read_secret_state(provider)
    return {"ok": True, "configured": bool(configured)}


def _read_secret_state(provider=""):
    """返回 ``(configured, source)``；任何异常都降级为 ``(False, "none")``。"""
    if _llm_client is None:
        return False, "none"
    function = getattr(_llm_client, "secret_status", None)
    if not callable(function):
        return False, "none"
    try:
        result = function(provider)
    except Exception:  # noqa: BLE001
        return False, "none"

    # 兼容 providers.secret_status 直接返回 dict 的实现
    if isinstance(result, dict):
        configured = bool(result.get("configured"))
        source = result.get("source")
    elif isinstance(result, (tuple, list)) and len(result) >= 2:
        configured = bool(result[0])
        source = result[1]
    else:
        return False, "none"

    source = source if source in ("env", "file", "none") else "none"
    return configured, (source if configured else "none")


async def _handle_secret_status(request):
    provider = ""
    try:
        provider = request.query.get("provider", "") or ""
    except Exception:  # noqa: BLE001 - 无 query 的请求对象
        provider = ""
    configured, source = _read_secret_state(provider)
    return {"configured": bool(configured), "source": source}


# --------------------------------------------------------------------------- 运行时转发


def _runtime_module():
    return _load_sibling("runtime")


async def _handle_runtime_start(request):
    body = await _read_json(request)
    if not isinstance(body, dict) or not body:
        return _error("bad_request", "请求体必须是 LLM_MODEL JSON 对象")

    runtime = _runtime_module()
    if runtime is None or not hasattr(runtime, "ensure_runtime"):
        return _error(
            "runtime_unavailable",
            "runtime.py 不可用（尚未就绪或导入失败），无法启动本地 llama-server",
        )

    try:
        result = await asyncio.to_thread(runtime.ensure_runtime, body)
    except Exception as exc:  # noqa: BLE001 - 启动失败必须结构化返回
        code = getattr(exc, "code", None) or "runtime_start_failed"
        return _error(code, str(exc))

    if not isinstance(result, dict):
        return _error("runtime_start_failed", f"ensure_runtime 返回异常：{result!r}")

    try:
        port = int(result.get("port") or 0)
    except (TypeError, ValueError):
        port = 0

    return {
        "ok": True,
        "base_url": result.get("base_url") or "",
        "model": result.get("model") or body.get("model") or "",
        "port": port,
    }


async def _handle_runtime_stop(request):
    runtime = _runtime_module()
    if runtime is None or not hasattr(runtime, "stop_all"):
        return _error("runtime_unavailable", "runtime.py 不可用，无运行时可停止")

    try:
        stopped = await asyncio.to_thread(runtime.stop_all)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None) or "runtime_stop_failed"
        return _error(code, str(exc))

    if not isinstance(stopped, (list, tuple)):
        stopped = []
    return {"ok": True, "stopped": list(stopped)}


async def _handle_runtime_status(request):
    runtime = _runtime_module()
    if runtime is None or not hasattr(runtime, "status"):
        return {"running": []}

    try:
        running = await asyncio.to_thread(runtime.status)
    except Exception as exc:  # noqa: BLE001
        return _error("runtime_status_failed", str(exc))

    if not isinstance(running, (list, tuple)):
        running = []
    return {"running": list(running)}


# --------------------------------------------------------------------------- 目录


async def _handle_catalog(request):
    characters = []
    styles = []

    if _nodes_mod is not None:
        function = getattr(_nodes_mod, "_catalog", None)
        if callable(function):
            try:
                catalog = function()
                if isinstance(catalog, dict):
                    characters = catalog.get("characters") or []
                    styles = catalog.get("styles") or []
            except Exception:  # noqa: BLE001
                _log(f"读取 catalog 失败：{traceback.format_exc(limit=2)}")

    return {"characters": characters, "styles": styles}


# --------------------------------------------------------------------------- v2 新路由（契约 §14.3）


async def _handle_providers(request):
    """``GET /nl2prompt/providers``：供应商全表（契约 §14.1 / §14.3）。"""
    rows = []
    if _nodes_mod is not None:
        function = getattr(_nodes_mod, "list_providers", None)
        if callable(function):
            try:
                rows = function()
            except Exception:  # noqa: BLE001
                _log(f"list_providers 失败：{traceback.format_exc(limit=2)}")
                rows = []
    providers = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        providers.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "label": row.get("label") or row.get("name"),
                "base_url": row.get("base_url"),
                "openai_compat": row.get("openai_compat", True),
                "default_model": row.get("default_model", ""),
                "verified": row.get("verified", None),
                "note": row.get("note", ""),
            }
        )
    return {"ok": True, "providers": providers}


async def _handle_list_models(request):
    """``GET /nl2prompt/list_models?base_url=&provider=``（契约 §14.3）。

    响应**绝不回显 key**；错误码 ``bad_url|no_key|http_<code>|timeout|dns|bad_json``。
    """
    base_url = ""
    provider = ""
    try:
        base_url = request.query.get("base_url", "") or ""
        provider = request.query.get("provider", "") or ""
    except Exception:  # noqa: BLE001 - 无 query 的请求对象
        base_url = ""
        provider = ""

    if _llm_client is None:
        return {
            "ok": False,
            "models": [],
            "count": 0,
            "elapsed_ms": 0.0,
            "error": "bad_url",
            "detail": "llm_client.py 不可用",
        }

    function = getattr(_llm_client, "list_models", None)
    if not callable(function):
        return {
            "ok": False,
            "models": [],
            "count": 0,
            "elapsed_ms": 0.0,
            "error": "bad_url",
            "detail": "llm_client.list_models 缺失",
        }

    try:
        # 网络请求放线程池，避免阻塞 aiohttp 事件循环
        result = await asyncio.to_thread(function, base_url, provider, 10.0)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "models": [],
            "count": 0,
            "elapsed_ms": 0.0,
            "error": "dns",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "models": [],
            "count": 0,
            "elapsed_ms": 0.0,
            "error": "bad_json",
            "detail": "list_models 返回结构异常",
        }
    return result


def _skill_store_module():
    return _load_sibling("skill_store")


def _store_module():
    """``store.py``（settings 读写）模块；不可用时返回 ``None``。"""
    return _load_sibling("store")


async def _handle_skills(request):
    """``GET /nl2prompt/skills``（契约 §14.3）。"""
    module = _skill_store_module()
    if module is None:
        return {
            "ok": False,
            "error": "skill_unavailable",
            "detail": "skill_store.py 不可用",
            "skills": [],
            "default": "",
            "content": {},
        }

    skills = []
    default_id = ""
    content = {}

    try:
        list_function = getattr(module, "list_skills", None)
        if callable(list_function):
            rows = list_function()
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        skills.append(
                            {
                                "id": row.get("id"),
                                "display_name": row.get("display_name"),
                                "target_model": row.get("target_model"),
                            }
                        )

        default_function = getattr(module, "default_skill_id", None)
        if callable(default_function):
            default_id = str(default_function() or "")
        if not default_id:
            index_function = getattr(module, "load_index", None)
            if callable(index_function):
                index = index_function()
                if isinstance(index, dict) and index.get("default"):
                    default_id = str(index.get("default"))
                    if not skills:
                        for row in index.get("skills") or []:
                            if isinstance(row, dict):
                                skills.append(
                                    {
                                        "id": row.get("id"),
                                        "display_name": row.get("display_name"),
                                        "target_model": row.get("target_model"),
                                    }
                                )

        loader = getattr(module, "load_skill", None)
        if callable(loader):
            for item in skills:
                skill_id = item.get("id")
                if not skill_id:
                    continue
                try:
                    skill = loader(skill_id)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(skill, dict):
                    content[str(skill_id)] = skill
    except Exception as exc:  # noqa: BLE001
        _log(f"读取 skills 失败：{traceback.format_exc(limit=2)}")
        return {
            "ok": False,
            "error": "skill_load_failed",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "skills": [],
            "default": "",
            "content": {},
        }

    return {
        "ok": True,
        "skills": skills,
        "default": default_id,
        "content": content,
    }


def _validate_skill_body(skill_id, content):
    """路由层最小校验：返回错误明细或 ``None``。

    深度校验交给 ``skill_store.validate_skill``（见 :func:`_handle_skills_save`），
    这里只保留**契约 §14.3 明确要求**的 ``id`` 一致性检查：``skill_store.save_skill``
    会强制 ``content["id"] = skill_id`` 静默覆盖，因此必须在路由层先拒绝不一致。
    """
    if not isinstance(content, dict):
        return "content 必须是 JSON 对象"
    content_id = content.get("id")
    if content_id is not None and str(content_id) != str(skill_id):
        return (
            "id_mismatch: body.id=%r 与 content.id=%r 不一致"
            % (skill_id, content_id)
        )
    return None


async def _handle_skills_save(request):
    """``POST /nl2prompt/skills/save``（契约 §14.3）。

    写盘**统一委托** ``skill_store.save_skill``（它做完整 schema 校验 +
    ``.bak`` 备份 + 原子替换 + 回读校验 + 失败回滚），本路由只做：
    请求体校验、``id`` 一致性、错误码映射。
    """
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error("bad_request", "请求体必须是 JSON 对象")

    skill_id = body.get("id")
    skill_id = skill_id.strip() if isinstance(skill_id, str) else ""
    if not skill_id:
        return _error("bad_request", "缺少 id")

    content = body.get("content")
    problem = _validate_skill_body(skill_id, content)
    if problem:
        return _error("skill_invalid", problem)

    module = _skill_store_module()
    if module is None:
        return _error(
            "skill_unavailable",
            "skill_store.py 不可用，无法保存 skill（不自行写盘，避免两套逻辑漂移）",
        )

    save_function = getattr(module, "save_skill", None)
    if not callable(save_function):
        return _error("skill_unavailable", "skill_store.save_skill 缺失")

    # 先用 validate_skill 预判，好把「字段非法」与「写盘失败」分成两个错误码
    validate_function = getattr(module, "validate_skill", None)
    if callable(validate_function):
        try:
            candidate = dict(content)
            candidate["id"] = skill_id
            report = validate_function(candidate)
        except Exception:  # noqa: BLE001 - 校验器异常时交给 save_skill 判定
            report = None
        if isinstance(report, dict) and report.get("ok") is False:
            errors = report.get("errors")
            message = (
                "; ".join(str(item) for item in errors)
                if isinstance(errors, list) and errors
                else "skill 校验未通过"
            )
            return _error("skill_invalid", message)

    try:
        result = await asyncio.to_thread(save_function, skill_id, content)
    except Exception as exc:  # noqa: BLE001 - 绝不让路由抛 500
        _log(f"/skills/save 未捕获异常：{traceback.format_exc(limit=3)}")
        return _error("skill_save_failed", f"{type(exc).__name__}: {exc}")

    if isinstance(result, dict) and result.get("ok"):
        return {"ok": True, "id": skill_id}

    message = ""
    if isinstance(result, dict):
        message = str(result.get("error") or "")
    return _error("skill_save_failed", message or "skill_store.save_skill 返回失败")


async def _handle_rewrite(request):
    """``POST /nl2prompt/rewrite``（R4 一键重写）。

    只调 LLM 改写并回填，**不执行图、不碰 lora_stack**。
    请求体 ``{"llm": {...}, "natural_language": "...", "ui": {...}}``。
    """
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error("llm_not_connected", "请求体必须是 JSON 对象")

    llm = body.get("llm")
    if not isinstance(llm, dict) or not llm:
        return _error("llm_not_connected", "缺少 llm（LLM_MODEL 对象）")

    natural_language = body.get("natural_language")
    natural_language = natural_language if isinstance(natural_language, str) else ""
    ui = body.get("ui")
    ui = ui if isinstance(ui, dict) else {}

    if _nodes_mod is None:
        return _error("llm_not_connected", "nodes.py 不可用")

    rewrite = getattr(_nodes_mod, "_rewrite_only", None)
    if not callable(rewrite):
        return _error("llm_not_connected", "nodes._rewrite_only 缺失")

    try:
        # LLM 调用是阻塞网络请求，放线程池
        result = await asyncio.to_thread(rewrite, llm, natural_language, ui)
    except Exception as exc:  # noqa: BLE001 - 绝不让路由抛 500
        _log(f"/rewrite 未捕获异常：{traceback.format_exc(limit=3)}")
        return _error("llm_unexpected_error", f"{type(exc).__name__}: {exc}")

    if not isinstance(result, dict):
        return _error("llm_unexpected_error", "重写返回结构异常")

    if not result.get("ok"):
        code = str(result.get("error") or "llm_error")
        detail = str(result.get("detail") or "")[:200]
        # 校验失败时把 issues 带给前端（契约 §14.2）
        debug_payload = {
            key: value
            for key, value in (result.get("debug") or {}).items()
            if key in ("skill_check", "quality", "analysis", "retried")
        }
        # 走 _error 才能拿到 detail_zh / detail_en（code 由 nodes 决定，
        # 例如 cloud_model_empty / llm_key_missing / llm_timeout）
        return _error(code, detail, debug=debug_payload)

    debug = result.get("debug") or {}
    # 把观测字段透出给前端/验收脚本（契约 §14.2 / §14.4）
    skill_check = debug.get("skill_check")
    negative_missing = []
    if isinstance(skill_check, dict):
        negative_missing = skill_check.get("negative_missing") or []
    retried = bool(debug.get("retried"))
    if isinstance(skill_check, dict) and skill_check.get("retried"):
        retried = True
    _dbg_tagdb = debug.get("tagdb") if isinstance(debug.get("tagdb"), dict) else {}
    return {
        "ok": True,
        "positive": result.get("positive", ""),
        "negative": result.get("negative", ""),
        "analysis": result.get("analysis", ""),
        "debug": {
            "used_llm": bool(debug.get("used_llm", True)),
            "skill_check": skill_check,
            "quality": debug.get("quality"),
            "fallback": str(debug.get("fallback") or ""),
            "retried": retried,
            "negative_missing": negative_missing,
            # 标签库归一结果：用户必须能看到哪些标签被改写，否则改动是隐形的
            "tagdb": debug.get("tagdb") if isinstance(debug.get("tagdb"), dict) else None,
            # 多行分段（v11）：行数、中文映射命中、被删的无来源标签
            "segment_lines": debug.get("segment_lines"),
            "cn_mapped": debug.get("cn_mapped") or [],
            "quality_default": bool(debug.get("quality_default")),
            # 标签来源校验：用户排查"为什么某个标签不见了"时需要
            "tag_sources": _dbg_tagdb.get("tag_sources") or {},
            "dropped": _dbg_tagdb.get("dropped") or [],
            "cn_enforced": _dbg_tagdb.get("cn_enforced") or [],
            # 本次生效的 token 优化开关，便于排查"为什么没有省 token"
            "prompt_opt": debug.get("prompt_opt") if isinstance(debug.get("prompt_opt"), dict) else None,
            # 中文清理：用户排查"为什么我的中文不见了"时需要
            "cjk_cleaned": debug.get("cjk_cleaned"),
            "cjk_removed_chars": debug.get("cjk_removed_chars"),
            # 换装覆盖：用户排查"为什么原来的衣服没了/还在"时需要
            "outfit_override": debug.get("outfit_override") if isinstance(debug.get("outfit_override"), dict) else None,
        },
    }


# --------------------------------------------------------- v3 B4：skill CRUD / settings_ui

#: skill id 允许的字符集（契约 §14.3：只允许小写字母 / 数字 / 连字符）
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9-]+$")
#: skill id 长度上限（避免写出超长文件名）
_SKILL_ID_MAX = 64


def _skill_id_from_request(request):
    """从路径参数取 skill id（``GET /nl2prompt/skills/{id}``）。"""
    try:
        value = request.match_info.get("id")
    except Exception:  # noqa: BLE001
        value = None
    return value.strip() if isinstance(value, str) else ""


def _skill_ids(module):
    """返回 ``(全部 id 列表, 默认 id)``；任何失败都退化为空，不抛异常。"""
    ids = []
    default_id = ""
    try:
        list_function = getattr(module, "list_skills", None)
        if callable(list_function):
            for row in list_function() or []:
                if isinstance(row, dict) and row.get("id"):
                    ids.append(str(row["id"]))
    except Exception:  # noqa: BLE001
        _log(f"读取 skill 列表失败：{traceback.format_exc(limit=2)}")
    try:
        default_function = getattr(module, "default_skill_id", None)
        if callable(default_function):
            default_id = str(default_function() or "")
    except Exception:  # noqa: BLE001
        default_id = ""
    return ids, default_id


def _skill_loader(module):
    """优先 ``load_skill_by_id``（skill-store 作者新增），回退 ``load_skill``。"""
    for name in ("load_skill_by_id", "load_skill"):
        function = getattr(module, name, None)
        if callable(function):
            return function
    return None


def _drop_skill_index_entry(module, skill_id):
    """从 ``index.json`` 移除该 skill 条目（只动索引，不删其它文件）。"""
    index_file = getattr(module, "INDEX_FILE", None)
    if index_file is None:
        return False
    try:
        data = json.loads(Path(index_file).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    rows = data.get("skills")
    if not isinstance(rows, list):
        return False
    kept = [
        row
        for row in rows
        if not (isinstance(row, dict) and row.get("id") == skill_id)
    ]
    if len(kept) == len(rows):
        return False
    data["skills"] = kept
    try:
        Path(index_file).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:  # noqa: BLE001
        _log(f"写回 index.json 失败：{traceback.format_exc(limit=2)}")
        return False
    return True


async def _handle_skill_detail(request):
    """``GET /nl2prompt/skills/{id}``：读取单个 skill 全文。"""
    skill_id = _skill_id_from_request(request)
    if not skill_id:
        return _error("bad_request", "缺少 skill id", "Missing skill id")
    if not _SKILL_ID_PATTERN.match(skill_id) or len(skill_id) > _SKILL_ID_MAX:
        # 非法 id 与「不存在」是两回事：前者是调用方写错了，后者是资源没了
        return _error(
            "bad_skill_id",
            f"id 非法（只允许小写字母/数字/连字符，最长 {_SKILL_ID_MAX}）：{skill_id}",
            f"Invalid id (lowercase letters, digits and hyphens only, max {_SKILL_ID_MAX}): {skill_id}",
        )

    module = _skill_store_module()
    if module is None:
        return _error(
            "skill_unavailable",
            "skill_store.py 不可用",
            "skill_store.py is unavailable",
        )

    ids, default_id = _skill_ids(module)
    if skill_id not in ids:
        return _error(
            "skill_not_found",
            f"skill 不存在：{skill_id}",
            f"Skill not found: {skill_id}",
        )

    loader = _skill_loader(module)
    if loader is None:
        return _error(
            "skill_unavailable",
            "skill_store 缺少 load_skill_by_id / load_skill",
            "skill_store is missing load_skill_by_id / load_skill",
        )

    try:
        skill = await asyncio.to_thread(loader, skill_id)
    except Exception as exc:  # noqa: BLE001 - 绝不让路由抛 500
        _log(f"/skills/{{id}} 未捕获异常：{traceback.format_exc(limit=3)}")
        return _error(
            "skill_load_failed",
            f"{type(exc).__name__}: {exc}",
            f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(skill, dict) or not skill:
        return _error(
            "skill_not_found",
            f"skill 不存在：{skill_id}",
            f"Skill not found: {skill_id}",
        )

    return {
        "ok": True,
        "id": skill_id,
        "is_default": skill_id == default_id,
        "skill": skill,
    }


async def _handle_skill_new(request):
    """``POST /nl2prompt/skills/new``：从已有 skill 复制出一个新 skill。"""
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error(
            "bad_request", "请求体必须是 JSON 对象", "Body must be a JSON object"
        )

    skill_id = body.get("id")
    skill_id = skill_id.strip() if isinstance(skill_id, str) else ""
    if not skill_id:
        return _error("bad_request", "缺少 id", "Missing id")
    if len(skill_id) > _SKILL_ID_MAX or not _SKILL_ID_PATTERN.match(skill_id):
        return _error(
            "bad_skill_id",
            f"id 非法（只允许小写字母/数字/连字符，最长 {_SKILL_ID_MAX}）：{skill_id}",
            "Invalid id (lowercase letters, digits and hyphens only, "
            f"max {_SKILL_ID_MAX}): {skill_id}",
        )

    display_name = body.get("display_name")
    display_name = display_name.strip() if isinstance(display_name, str) else ""
    if not display_name:
        display_name = skill_id

    target_model = body.get("target_model")
    target_model = target_model.strip() if isinstance(target_model, str) else ""

    copy_from = body.get("copy_from")
    copy_from = copy_from.strip() if isinstance(copy_from, str) else ""

    module = _skill_store_module()
    if module is None:
        return _error(
            "skill_unavailable",
            "skill_store.py 不可用，无法新建 skill",
            "skill_store.py is unavailable; cannot create skill",
        )

    ids, default_id = _skill_ids(module)
    if skill_id in ids:
        return _error(
            "skill_exists",
            f"skill 已存在：{skill_id}",
            f"Skill already exists: {skill_id}",
        )
    if copy_from and copy_from not in ids:
        return _error(
            "skill_not_found",
            f"copy_from 指定的 skill 不存在：{copy_from}",
            f"copy_from skill not found: {copy_from}",
        )

    save_function = getattr(module, "save_skill", None)
    loader = _skill_loader(module)
    if not callable(save_function) or loader is None:
        return _error(
            "skill_unavailable",
            "skill_store 缺少 save_skill / load_skill",
            "skill_store is missing save_skill / load_skill",
        )

    source_id = copy_from or default_id or (ids[0] if ids else "")
    template = None
    if source_id:
        try:
            template = await asyncio.to_thread(loader, source_id)
        except Exception:  # noqa: BLE001
            _log(
                f"/skills/new 读取模板 {source_id} 失败：{traceback.format_exc(limit=2)}"
            )
            template = None
    if not isinstance(template, dict) or not template:
        return _error(
            "skill_unavailable",
            "找不到可用作模板的 skill",
            "No skill is available as a template",
        )

    content = dict(template)
    content["id"] = skill_id
    content["display_name"] = display_name
    if target_model:
        content["target_model"] = target_model
    content["version"] = "1.0.0"

    # 先用 validate_skill 预判，把「字段非法」与「写盘失败」分成两个错误码
    validate_function = getattr(module, "validate_skill", None)
    if callable(validate_function):
        try:
            report = validate_function(dict(content))
        except Exception:  # noqa: BLE001 - 校验器异常时交给 save_skill 判定
            report = None
        if isinstance(report, dict) and report.get("ok") is False:
            errors = report.get("errors")
            message = (
                "; ".join(str(item) for item in errors)
                if isinstance(errors, list) and errors
                else "skill 校验未通过"
            )
            return _error("skill_invalid", message)

    try:
        result = await asyncio.to_thread(save_function, skill_id, content)
    except Exception as exc:  # noqa: BLE001 - 绝不让路由抛 500
        _log(f"/skills/new 未捕获异常：{traceback.format_exc(limit=3)}")
        return _error(
            "skill_save_failed",
            f"{type(exc).__name__}: {exc}",
            f"{type(exc).__name__}: {exc}",
        )

    if isinstance(result, dict) and result.get("ok"):
        return {
            "ok": True,
            "id": skill_id,
            "display_name": display_name,
            "target_model": str(content.get("target_model") or ""),
            "copied_from": source_id,
            "skill": content,
        }

    message = str(result.get("error") or "") if isinstance(result, dict) else ""
    return _error(
        "skill_save_failed",
        message or "skill_store.save_skill 返回失败",
        message or "skill_store.save_skill returned failure",
    )


async def _handle_skill_delete(request):
    """``POST /nl2prompt/skills/delete``：删除 skill（默认 skill 受保护）。"""
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error(
            "bad_request", "请求体必须是 JSON 对象", "Body must be a JSON object"
        )

    skill_id = body.get("id")
    skill_id = skill_id.strip() if isinstance(skill_id, str) else ""
    if not skill_id:
        return _error("bad_request", "缺少 id", "Missing id")
    if not _SKILL_ID_PATTERN.match(skill_id) or len(skill_id) > _SKILL_ID_MAX:
        return _error(
            "bad_skill_id",
            f"id 非法（只允许小写字母/数字/连字符，最长 {_SKILL_ID_MAX}）：{skill_id}",
            f"Invalid id (lowercase letters, digits and hyphens only, max {_SKILL_ID_MAX}): {skill_id}",
        )

    module = _skill_store_module()
    if module is None:
        return _error(
            "skill_unavailable",
            "skill_store.py 不可用，无法删除 skill",
            "skill_store.py is unavailable; cannot delete skill",
        )

    ids, default_id = _skill_ids(module)
    if skill_id not in ids:
        return _error(
            "skill_not_found",
            f"skill 不存在：{skill_id}",
            f"Skill not found: {skill_id}",
        )
    if skill_id == default_id:
        return _error(
            "skill_protected",
            f"默认 skill 受保护，不可删除：{skill_id}",
            f"The default skill is protected and cannot be deleted: {skill_id}",
        )

    path = None
    file_function = getattr(module, "skill_file", None)
    if callable(file_function):
        try:
            path = Path(file_function(skill_id))
        except Exception:  # noqa: BLE001
            path = None

    backup = None
    try:
        if path is not None and path.exists():
            # 先改名备份（保留可恢复），再动索引
            backup = path.with_name(path.name + ".deleted")
            path.replace(backup)
        _drop_skill_index_entry(module, skill_id)
        reload_function = getattr(module, "reload", None)
        if callable(reload_function):
            reload_function()
    except Exception as exc:  # noqa: BLE001 - 失败要回滚文件
        _log(f"/skills/delete 失败：{traceback.format_exc(limit=3)}")
        if backup is not None and path is not None:
            try:
                if backup.exists() and not path.exists():
                    backup.replace(path)
            except Exception:  # noqa: BLE001
                _log(f"/skills/delete 回滚失败：{traceback.format_exc(limit=2)}")
        return _error(
            "skill_delete_failed",
            f"{type(exc).__name__}: {exc}",
            f"{type(exc).__name__}: {exc}",
        )

    return {"ok": True, "id": skill_id, "deleted": True}


async def _handle_settings_ui_get(request):
    """``GET /nl2prompt/settings_ui``：前端语言 + 正向/负向基础词。"""
    module = _store_module()
    if module is None:
        return _error(
            "store_unavailable", "store.py 不可用", "store.py is unavailable"
        )

    settings = {}
    loader = getattr(module, "load_settings", None)
    if callable(loader):
        try:
            settings = await asyncio.to_thread(loader) or {}
        except Exception:  # noqa: BLE001
            _log(f"读取 settings 失败：{traceback.format_exc(limit=2)}")
            settings = {}
    if not isinstance(settings, dict):
        settings = {}

    # prompt_opt：三项 token 优化开关，由 store.save_settings 归一成 bool 并只取
    # 已知的 dedupe_skill / static_selected / retry_multiturn 三个键，这里只做透传。
    prompt_opt = settings.get("prompt_opt")
    if not isinstance(prompt_opt, dict):
        prompt_opt = {}

    return {
        "ok": True,
        "ui_locale": str(settings.get("ui_locale") or "auto"),
        "default_positive_base": str(settings.get("default_positive_base") or ""),
        "default_negative_base": str(settings.get("default_negative_base") or ""),
        "prompt_opt": prompt_opt,
        # thinking_mode：off = 对本地端点发 reasoning_effort=none 关掉思考，
        # auto = 不发送该字段。缺键按 off 返回，与 store 的默认值一致。
        "thinking_mode": str(settings.get("thinking_mode") or "off"),
    }


async def _handle_settings_ui_post(request):
    """``POST /nl2prompt/settings_ui``：更新 ``ui_locale`` / ``default_positive_base``。"""
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error(
            "bad_request", "请求体必须是 JSON 对象", "Body must be a JSON object"
        )

    patch = {}
    if "ui_locale" in body:
        locale = body.get("ui_locale")
        locale = locale.strip().lower() if isinstance(locale, str) else ""
        if locale not in ("auto", "zh", "en"):
            return _error("bad_locale", "ui_locale 只能是 auto / zh / en")
        patch["ui_locale"] = locale
    if "default_positive_base" in body:
        value = body.get("default_positive_base")
        if not isinstance(value, str):
            return _error(
                "bad_request",
                "default_positive_base 必须是字符串",
                "default_positive_base must be a string",
            )
        patch["default_positive_base"] = value
    if "prompt_opt" in body:
        value = body.get("prompt_opt")
        if not isinstance(value, dict):
            return _error(
                "bad_request",
                "prompt_opt 必须是对象",
                "prompt_opt must be an object",
            )
        # 只透传，逐键的类型归一与已知键过滤由 store.save_settings 负责
        patch["prompt_opt"] = value
    if "thinking_mode" in body:
        value = body.get("thinking_mode")
        value = value.strip().lower() if isinstance(value, str) else ""
        if value not in ("off", "auto"):
            return _error(
                "bad_request",
                "thinking_mode 只能是 off / auto",
                "thinking_mode must be off or auto",
            )
        patch["thinking_mode"] = value

    if not patch:
        return _error(
            "bad_request",
            "没有可更新的字段（支持 ui_locale / default_positive_base / prompt_opt / thinking_mode）",
            "No updatable fields (expected ui_locale / default_positive_base / prompt_opt / thinking_mode)",
        )

    module = _store_module()
    if module is None:
        return _error(
            "store_unavailable", "store.py 不可用", "store.py is unavailable"
        )

    save_function = getattr(module, "save_settings", None)
    if not callable(save_function):
        return _error(
            "store_unavailable",
            "store.save_settings 缺失",
            "store.save_settings is missing",
        )

    try:
        result = await asyncio.to_thread(save_function, patch)
    except Exception as exc:  # noqa: BLE001 - 绝不让路由抛 500
        _log(f"/settings_ui 未捕获异常：{traceback.format_exc(limit=3)}")
        return _error(
            "settings_save_failed",
            f"{type(exc).__name__}: {exc}",
            f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(result, dict) or not result.get("ok"):
        message = str(result.get("error") or "") if isinstance(result, dict) else ""
        return _error(
            "settings_save_failed",
            message or "settings 写入失败",
            message or "Failed to write settings",
        )

    settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
    return {
        "ok": True,
        "ui_locale": str(settings.get("ui_locale") or ""),
        "default_positive_base": str(settings.get("default_positive_base") or ""),
    }


# --------------------------------------------------------------------------- 注册


def _iter_route_definitions(table):
    """遍历 ``RouteTableDef`` 中已注册的路由条目。

    兼容 aiohttp 各版本：3.x 用私有 ``_items``，部分版本暴露 ``routes``；
    ``RouteTableDef`` 本身是 Sequence，最后兜底用 ``list(table)``。
    """
    for attribute in ("_items", "routes", "_routes"):
        items = getattr(table, attribute, None)
        if isinstance(items, (list, tuple)):
            return items
    try:
        return list(table)
    except Exception:  # noqa: BLE001
        return []


def _route_exists(table, method, path):
    """幂等检查：避免 ComfyUI 热重载时重复注册同一路由导致 add_routes 冲突。"""
    wanted = (getattr(method, "upper", lambda: method)() if method else method, path)
    for definition in _iter_route_definitions(table):
        definition_path = getattr(definition, "path", None)
        definition_method = getattr(definition, "method", None)
        if definition_path == wanted[1] and definition_method == wanted[0]:
            return True
    return False


def _get_prompt_server():
    try:
        import server as comfy_server  # noqa: PLC0415 - ComfyUI 注入的模块

        instance = getattr(
            getattr(comfy_server, "PromptServer", None), "instance", None
        )
        if instance is not None:
            return instance
    except Exception:  # noqa: BLE001
        pass

    import sys

    for module_name in ("server", "comfy_server", "comfy.server"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        instance = getattr(getattr(module, "PromptServer", None), "instance", None)
        if instance is not None:
            return instance
    return None


#: 主路由前缀（v3 改名后）
_NEW_PREFIX = "/nl2prompt/"
#: 旧前缀别名（v2 及更早的前端/脚本继续可用，一个版本周期后移除）
_OLD_PREFIX = "/anima_nl/"

_ROUTES = (
    ("get", "/nl2prompt/gpus", _handle_gpus),
    ("get", "/nl2prompt/scan_gguf", _handle_scan_gguf),
    ("get", "/nl2prompt/scan_safetensors", _handle_scan_safetensors),
    ("get", "/nl2prompt/default_system_prompt", _handle_default_prompt),
    ("post", "/nl2prompt/secret", _handle_secret),
    ("get", "/nl2prompt/secret_status", _handle_secret_status),
    ("post", "/nl2prompt/runtime/start", _handle_runtime_start),
    ("post", "/nl2prompt/runtime/stop", _handle_runtime_stop),
    ("get", "/nl2prompt/runtime/status", _handle_runtime_status),
    ("get", "/nl2prompt/catalog", _handle_catalog),
    # ---- v2 新增（契约 §14.3）：9 -> 14
    ("get", "/nl2prompt/providers", _handle_providers),
    ("get", "/nl2prompt/list_models", _handle_list_models),
    ("get", "/nl2prompt/skills", _handle_skills),
    ("post", "/nl2prompt/skills/save", _handle_skills_save),
    ("post", "/nl2prompt/rewrite", _handle_rewrite),
    # ---- v3 B4（14 -> 19）：skill CRUD + 前端 settings
    ("get", "/nl2prompt/settings_ui", _handle_settings_ui_get),
    ("post", "/nl2prompt/settings_ui", _handle_settings_ui_post),
    ("post", "/nl2prompt/skills/new", _handle_skill_new),
    ("post", "/nl2prompt/skills/delete", _handle_skill_delete),
    # 路径参数放最后，避免抢到上面的精确路径
    ("get", "/nl2prompt/skills/{id}", _handle_skill_detail),
)


def register_routes(server_module=None):
    """向 ``PromptServer.instance.routes`` 注册全部路由。

    :returns: ``True`` 表示注册成功（或此前已注册），``False`` 表示环境不支持。
    """
    global _REGISTERED
    if _REGISTERED:
        return True
    if web is None:
        _log("aiohttp 不可用，跳过全部路由注册")
        return False

    instance = None
    if server_module is not None:
        instance = getattr(
            getattr(server_module, "PromptServer", None), "instance", None
        )
    if instance is None:
        instance = _get_prompt_server()
    if instance is None:
        _log("未找到 ComfyUI PromptServer.instance，跳过路由注册")
        return False

    table = getattr(instance, "routes", None)
    if table is None:
        _log("PromptServer.instance 缺少 routes 属性，跳过路由注册")
        return False

    registered = 0
    for method, path, handler in _ROUTES:
        try:
            if _route_exists(table, method, path):
                continue
            getattr(table, method)(path)(_guard(handler))
            registered += 1
        except Exception as exc:  # noqa: BLE001 - 单条路由失败不影响其它
            _log(f"注册 {method.upper()} {path} 失败：{exc}")

    # 旧前缀别名（v2 及更早的前端探针/脚本仍可用）；一个版本周期后可移除
    legacy_registered = 0
    for method, path, handler in _ROUTES:
        if not path.startswith(_NEW_PREFIX):
            continue
        legacy_path = _OLD_PREFIX + path[len(_NEW_PREFIX):]
        try:
            if _route_exists(table, method, legacy_path):
                continue
            getattr(table, method)(legacy_path)(_guard(handler))
            legacy_registered += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"注册别名 {method.upper()} {legacy_path} 失败：{exc}")

    _REGISTERED = True
    _log(
        f"路由注册完成（新增 {registered} 条 + 旧前缀别名 {legacy_registered} 条 / "
        f"共 {len(_ROUTES)} 条主路由）"
    )
    return True


__all__ = [
    "register_routes",
    "scan_gguf",
    "scan_safetensors",
    "MAX_SCAN_FILES",
    "MAX_SCAN_DEPTH",
]
