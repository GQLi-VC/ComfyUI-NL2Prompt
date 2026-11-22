# -*- coding: utf-8 -*-
"""NL2PromptUnloadModels：把 GPU 里已加载的模型全部卸掉（单卡用户友好）。

用途：单卡用户跑完出图后，把图像模型从显存卸掉，腾出显存给本地 LLM 用。

设计要点：

* **输入任意、输出原样透传** —— 它是个「副作用节点」，插在任意两条连线
  之间即可，不改变数据流。输入没连时输出为 ``None``。
* 卸载用 ComfyUI 的标准组合 ``unload_all_models()`` + ``soft_empty_cache()``，
  不自己写 ``torch.cuda.empty_cache()``（那个只清缓存，不卸载模型，
  显存不会真正释放）。
* 卸载失败**也必须原样透传**，绝不能因为卸载异常断掉工作流。
* ``IS_CHANGED`` 返回 ``NaN`` 强制每次都执行 —— 卸载模型的意义就是「每次跑都卸」，
  若被 ComfyUI 的执行缓存跳过就失去作用。

GBK 红线：本文件的日志字符串只用 ASCII + 常见中文字符。
"""

from __future__ import annotations

import sys

#: ComfyUI 的 ``ANY_TYPE``。老版本没有这个常量，用字面量即可（两者等价）。
ANY_TYPE = "*"

_LOG_PREFIX = "[NL2Prompt]"


def _safe_text(text):
    """降级为当前 stdout 能编码的字符串（中文 Windows 下 stderr 是 gbk）。"""
    raw = text if isinstance(text, str) else str(text)
    for stream in (sys.stderr, sys.stdout):
        encoding = getattr(stream, "encoding", None) or "utf-8"
        if not encoding:
            continue
        try:
            raw.encode(encoding)
            return raw
        except (UnicodeEncodeError, LookupError):
            continue
    return raw.encode("ascii", "replace").decode("ascii")


def _log(message):
    try:
        print("%s %s" % (_LOG_PREFIX, _safe_text(message)), flush=True)
    except Exception:  # noqa: BLE001 - 日志绝不能拖垮节点
        pass


class NL2PromptUnloadModels:
    """卸载 GPU 中所有已加载模型；输入原样透传到输出。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {"any_input": (ANY_TYPE,)},
        }

    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("any_output",)
    FUNCTION = "unload"
    CATEGORY = "NL2Prompt"
    OUTPUT_NODE = False

    DESCRIPTION = (
        "卸载 GPU 中所有已加载模型并清理显存缓存。"
        "输入任意、输出原样透传（输入未连接时输出为空）。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """返回 NaN 表示「每次都算变化」，强制节点每轮都执行（卸载必须每次都跑）。"""
        return float("NaN")

    def unload(self, any_input=None):
        """执行卸载，无论成败都把 ``any_input`` 原样返回。"""
        before = 0
        try:
            import comfy.model_management as mm

            loaded = getattr(mm, "current_loaded_models", None) or []
            before = len(loaded)
            _log("卸载模型：卸载前 %d 个已加载模型" % before)

            mm.unload_all_models()

            # 可选：部分版本有 cleanup_models（更彻底），失败无妨
            cleanup = getattr(mm, "cleanup_models", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception as exc:  # noqa: BLE001
                    _log("cleanup_models 跳过：%s: %s" % (type(exc).__name__, exc))

            mm.soft_empty_cache()

            after = len(getattr(mm, "current_loaded_models", None) or [])
            _log("卸载模型：卸载后 %d 个已加载模型" % after)
        except Exception as exc:  # noqa: BLE001 - 卸载失败不能断工作流
            _log("卸载模型失败（数据仍原样透传）：%s: %s"
                 % (type(exc).__name__, exc))
        return (any_input,)


__all__ = ["NL2PromptUnloadModels", "ANY_TYPE"]
