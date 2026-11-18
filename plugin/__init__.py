# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""ComfyUI 自然语言提示词（插件入口）。

契约：``docs/接口契约.md``
    * §0 文件与写作用域
    * §1 ``NL2PromptComposer`` → 「自然语言提示词」
    * §2 ``NL2PromptLoadLLM`` → 「加载LLM」
    * §5 ``NL2PromptLoRAStack`` → 「LoRA 堆栈加载」
    * §6 HTTP 路由

**容错红线**：本文件绝不能因为 ``store.py`` / ``runtime.py`` / ``providers.py``
缺失或损坏而抛异常——否则整个 ComfyUI 启动会失败。所有导入与注册都包在
``try/except`` 里，失败只打印告警并降级。
"""

from __future__ import annotations

import atexit
import sys
import traceback

_LOG_PREFIX = "[NL2Prompt]"

#: 契约 §9：前端资源目录
WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

#: 期望注册的节点（key → 显示名）
_EXPECTED_NODES = (
    ("NL2PromptComposer", "自然语言提示词"),
    # v3：LLM 加载拆成三个节点（GGUF / safetensors / 云端）
    ("NL2PromptLLMGGUF", "加载LLM（GGUF）"),
    ("NL2PromptLLMSafetensors", "加载LLM（safetensors）"),
    ("NL2PromptLLMCloud", "加载云端LLM"),
    # v2 合并版保留（旧工作流兼容），语义上等价于 GGUF+云端 切换
    ("NL2PromptLoadLLM", "加载LLM（旧·本地/云端合一）"),
    ("NL2PromptLoRAStack", "LoRA 堆栈加载"),
)

#: v2 旧类名 → v3 新类名。
#: ComfyUI 反序列化工作流时按 **class name** 查表，改名后旧工作流会找不到节点；
#: 这里把旧名一并注册到同一个类对象上（显示名加「（旧）」），旧图可直接打开。
_LEGACY_NODE_ALIASES = (
    ("AnimaNLComposer", "NL2PromptComposer", "（旧）自然语言提示词"),
    ("AnimaLoadLLM", "NL2PromptLoadLLM", "（旧）加载LLM"),
    ("AnimaLoRAStackLoader", "NL2PromptLoRAStack", "（旧）LoRA 堆栈加载"),
)


def _safe_text(text):
    """把文本降级为当前 stdout/stderr 都能编码的字符串（P0 级防护）。

    中文 Windows 下 ComfyUI 的 stderr 是 gbk；插件加载期的告警若含无法 gbk
    编码的字符（emoji、U+203A 角引号等），print 会抛 ``UnicodeEncodeError``，可能
    导致 ``lost sys.stderr`` 并影响 ComfyUI 的 worker 线程。
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


def _import_nodes():
    """加载 ``nodes.py``：先包内相对导入，再按文件路径兜底。"""
    try:
        from . import nodes as module  # noqa: PLC0415

        return module
    except Exception as exc:  # noqa: BLE001 - 包内导入失败时换文件路径
        _log(f"相对导入 nodes 失败（{type(exc).__name__}: {exc}），改用文件路径加载")

    try:
        import importlib.util
        import sys
        from pathlib import Path

        path = Path(__file__).resolve().parent / "nodes.py"
        if not path.is_file():
            return None
        module_name = "_anima_nl_nodes_entry"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - 节点加载失败也必须让 ComfyUI 继续启动
        _log(f"按文件路径加载 nodes.py 失败：{traceback.format_exc(limit=4)}")
        return None


def _register_nodes():
    module = _import_nodes()
    if module is None:
        _log("nodes.py 不可用，本插件无可用节点（ComfyUI 仍可正常启动）")
        return

    for class_name, display_name in _EXPECTED_NODES:
        node_class = getattr(module, class_name, None)
        if node_class is None:
            _log(f"nodes.py 缺少节点类 {class_name}，已跳过")
            continue
        NODE_CLASS_MAPPINGS[class_name] = node_class
        NODE_DISPLAY_NAME_MAPPINGS[class_name] = display_name

    # 旧类名兼容：让 v2 时期的工作流仍能反序列化
    for legacy_name, new_name, legacy_display in _LEGACY_NODE_ALIASES:
        node_class = NODE_CLASS_MAPPINGS.get(new_name)
        if node_class is None:
            continue
        NODE_CLASS_MAPPINGS[legacy_name] = node_class
        NODE_DISPLAY_NAME_MAPPINGS[legacy_name] = legacy_display

    if NODE_CLASS_MAPPINGS:
        _log(
            "已注册节点：" + ", ".join(sorted(NODE_CLASS_MAPPINGS))
        )


def _register_routes():
    module = None
    try:
        from . import routes as module  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _log(f"相对导入 routes 失败（{type(exc).__name__}: {exc}），改用文件路径加载")
        try:
            import importlib.util
            import sys
            from pathlib import Path

            path = Path(__file__).resolve().parent / "routes.py"
            if path.is_file():
                spec = importlib.util.spec_from_file_location(
                    "_anima_nl_routes_entry", path
                )
                if spec is not None and spec.loader is not None:
                    candidate = importlib.util.module_from_spec(spec)
                    sys.modules["_anima_nl_routes_entry"] = candidate
                    spec.loader.exec_module(candidate)
                    module = candidate
        except Exception:  # noqa: BLE001
            module = None

    if module is None:
        _log("routes.py 不可用，HTTP 路由未注册")
        return

    register = getattr(module, "register_routes", None)
    if not callable(register):
        _log("routes.py 缺少 register_routes()，HTTP 路由未注册")
        return

    try:
        register()
    except Exception:  # noqa: BLE001 - 路由注册失败不得阻断 ComfyUI
        _log(f"注册 HTTP 路由失败：{traceback.format_exc(limit=4)}")


def _stop_runtime():
    """``atexit`` 兜底停止本地 llama-server（契约 §11：双保险之一）。"""
    try:
        from . import runtime as runtime_module  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        try:
            import importlib.util
            import sys
            from pathlib import Path

            path = Path(__file__).resolve().parent / "runtime.py"
            if not path.is_file():
                return
            spec = importlib.util.spec_from_file_location(
                "_anima_nl_runtime_exit", path
            )
            if spec is None or spec.loader is None:
                return
            runtime_module = importlib.util.module_from_spec(spec)
            sys.modules["_anima_nl_runtime_exit"] = runtime_module
            spec.loader.exec_module(runtime_module)
        except Exception:  # noqa: BLE001
            return

    stop_all = getattr(runtime_module, "stop_all", None)
    if not callable(stop_all):
        return
    try:
        stopped = stop_all()
        if stopped:
            _log(f"退出时已停止本地 LLM 运行时：{stopped}")
    except Exception:  # noqa: BLE001
        pass


_register_nodes()
_register_routes()
atexit.register(_stop_runtime)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
