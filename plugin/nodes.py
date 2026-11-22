"""自然语言提示词 —— 三个 ComfyUI 节点（backend-core）。

契约：``docs/接口契约.md``（唯一真源）
    * §1 ``NL2PromptComposer``
    * §2 ``NL2PromptLoadLLM``
    * §3 主节点 holder JSON
    * §5 组装算法
    * §7 / §8 LLM 调用与错误码
    * §12 纯函数 ``compose_plain``

容错原则
--------
``store.py`` / ``runtime.py`` / ``providers.py`` 由他人并行开发，**开发期可能不存在**。
本模块用 :func:`_load_sibling` 做三级容错加载（包内相对导入 → 按文件路径加载 → 放弃），
任一模块缺失都降级为内置最小实现，保证 ComfyUI 启动与节点加载不受影响。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

# --------------------------------------------------------------------------- 基础

_PLUGIN_DIR = Path(__file__).resolve().parent
_LOG_PREFIX = "[NL2Prompt]"

#: 契约 §14.5：负面基础词**只保留一份**（在 ``store.py``）。
#: 此处仅是 ``store`` 不可用时的兜底，v2 起默认空串。
FALLBACK_NEGATIVE_BASE = ""

#: 契约 §14.5：system prompt 完整文案已迁入 ``plugin/skills/*.json``。
#: 这里只保留**极简兜底**（不是第二份完整文案），供 store/skill 双双缺失时使用。
FALLBACK_SYSTEM_PROMPT = (
    "你是 Anima 文生图模型的提示词重写器。把用户的中文描述重写为英文 danbooru 标签。\n"
    "已知角色与服装：\n{{character_presets}}\n\n"
    "已知画师/风格：\n{{style_presets}}\n\n"
    "当前用户选择：\n{{selected}}\n\n"
    "规则：只注入当前选中服装的标签；原样保留角色/服装/风格触发词；"
    "用具体视觉事实替代空洞形容词。\n"
    '只输出 JSON：{"positive":"...","negative":"...","analysis":"<中文简短说明>"}'
)

#: 契约 §14.2 默认 skill id
DEFAULT_SKILL_ID = "anima-tag-master"

#: v1 遗留兜底表：**仅**在 ``providers.py`` 不可用时使用。
#: 契约 §14.1 要求供应商数据单一真源在 ``providers.py``，本表只是启动期兜底。
_FALLBACK_PROVIDERS = {
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "label": "深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-latest",
    },
    "custom": {
        "id": "custom",
        "name": "Custom",
        "label": "自定义",
        "base_url": "",
        "default_model": "",
    },
}

#: 契约 §2.1 ``model_file`` COMBO 的占位项。中英各一份——下拉显示跟随前端语言，
#: 而存进工作流的可能是任一种，所以后端判断必须两种都认（见 is_model_placeholder）。
#: 与 ``nodes_llm.py`` 的同名常量必须保持一致（两模块刻意不互相 import）。
MODEL_FILE_PLACEHOLDER_ZH = "(请先选择路径后刷新)"
MODEL_FILE_PLACEHOLDER_EN = "(select a folder, then refresh)"
MODEL_FILE_PLACEHOLDERS = (MODEL_FILE_PLACEHOLDER_ZH, MODEL_FILE_PLACEHOLDER_EN)
#: 兼容既有引用与 INPUT_TYPES 默认值（以中文为准）
MODEL_FILE_PLACEHOLDER = MODEL_FILE_PLACEHOLDER_ZH


def is_model_placeholder(value) -> bool:
    """是否为「尚未选择模型」的占位值（空串 / 中文占位 / 英文占位都算）。"""
    text = (value or "").strip()
    return not text or text in MODEL_FILE_PLACEHOLDERS

#: 契约 §6 GPU COMBO 兜底
GPU_FALLBACK = ["0", "1"]

#: 契约 §8 错误码
ERR_NOT_CONNECTED = "llm_not_connected"
ERR_KEY_MISSING = "llm_key_missing"
ERR_LOCAL_MISSING = "local_model_missing"
ERR_RUNTIME_FAILED = "runtime_start_failed"
#: 契约 §14.2 校验两次都失败（契约 §8 未列，建议 lead 补录）
ERR_SKILL_CHECK_FAILED = "llm_skill_check_failed"
#: v3：safetensors 运行时模块缺失（llm_local_hf.py 不可用 / 缺 chat）
ERR_SAFETENSORS_UNAVAILABLE = "safetensors_unavailable"
#: v3：safetensors 运行时内部失败（无 code 的异常统一归到这里）
ERR_SAFETENSORS_FAILED = "safetensors_failed"
#: v3：云端配置缺失（本地校验错误，**不是** HTTP 故障，不可归入 llm_http_*）
ERR_CLOUD_BASE_URL_EMPTY = "cloud_base_url_empty"
ERR_CLOUD_MODEL_EMPTY = "cloud_model_empty"

#: LoRA 栈元素取值字段（契约 §5 点名的字段名优先）
_WORD_KEYS = (
    "activation_words",
    "activation",
    "trigger",
    "triggers",
    "trained_words",
)
_PROMPT_KEYS = ("prompt", "positive_prompt", "prompt_text")
_LORA_KEYS = ("lora_name", "lora", "lora_file")
_NEG_KEYS = ("negative_suppress", "negative", "negative_prompt")
_ARTIST_KEYS = ("artist_tags", "artists", "artist")


def _safe_text(text):
    """把文本降级为当前 stdout/stderr 都能编码的字符串。

    **这是 P0 级防护**：中文 Windows 下 ComfyUI 的 stderr 是 gbk。若把无法
    gbk 编码的字符（emoji、U+203A 角引号、部分箭头符号）直接 ``print``，
    会抛 ``UnicodeEncodeError`` 并可能触发 ``lost sys.stderr``，进而打死
    ``prompt_worker`` 线程 —— 出图队列永久卡死且无法用 ``/interrupt`` 恢复。

    触发源不只是源码字面量，还包括**运行时动态字符串**：用户自然语言、
    角色名、LoRA 文件名、HTTP 响应片段、异常 message、traceback 文本。
    因此所有日志与异常文案都必须先过本函数。
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


def _load_sibling(name):
    """加载同级模块；缺失或损坏时返回 ``None``（绝不抛异常）。"""
    package = __package__ or ""
    if package:
        try:
            return importlib.import_module("." + name, package)
        except Exception:  # noqa: BLE001 - 缺失即降级
            pass
    try:
        path = _PLUGIN_DIR / (name + ".py")
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(f"_anima_nl_{name}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - 损坏的同级模块不得拖垮节点加载
        _log(f"加载 {name}.py 失败，降级为内置实现：{traceback.format_exc(limit=2)}")
        return None


_llm_client = _load_sibling("llm_client")
_store_mod = _load_sibling("store")
_providers_mod = _load_sibling("providers")
# v5：danbooru 标签库访问层。模块导入时不打开数据库（惰性），
# 缺失/损坏时 _load_sibling 返回 None，全链路自动跳过校验。
_tagdb_mod = _load_sibling("tagdb")
# v10：中文 -> 英文的**确定性映射**（不经过 LLM）。缺失时全部降级为空映射，
# 行为退回 v9（中文仍由 strip_cjk_segments 清掉）。
_cn_tags_mod = _load_sibling("cn_tags")


def _seg_constants():
    """取段名常量；``cn_tags`` 缺失时用本地兜底。"""
    return {
        "quality": getattr(_cn_tags_mod, "SEGMENT_QUALITY", _SEG_QUALITY),
        "framing": getattr(_cn_tags_mod, "SEGMENT_FRAMING", _SEG_FRAMING),
        "character": getattr(_cn_tags_mod, "SEGMENT_CHARACTER", _SEG_CHARACTER),
        "scene": getattr(_cn_tags_mod, "SEGMENT_SCENE", _SEG_SCENE),
    }


def _translate_cn(text):
    """调用 ``cn_tags.translate``，任何异常都退化成「什么都没翻译」。"""
    empty = {
        _SEG_QUALITY: [],
        _SEG_FRAMING: [],
        _SEG_CHARACTER: [],
        _SEG_SCENE: [],
        "matched": [],
        "dropped": [],
        "remaining": text if isinstance(text, str) else "",
    }
    if _cn_tags_mod is None:
        return empty
    translator = getattr(_cn_tags_mod, "translate", None)
    if not callable(translator):
        return empty
    try:
        result = translator(text)
        if not isinstance(result, dict):
            return empty
        consts = _seg_constants()
        for key, alias in (("quality", "quality"), ("framing", "framing"),
                           ("character", "character"), ("scene", "scene")):
            value = result.get(consts[alias])
            if isinstance(value, list) and value:
                empty[key] = value
        empty["matched"] = list(result.get("matched") or [])
        empty["dropped"] = list(result.get("dropped") or [])
        if isinstance(result.get("remaining"), str):
            empty["remaining"] = result["remaining"]
        return empty
    except Exception:  # noqa: BLE001 - 中文映射只是增强，绝不拖垮组装
        return empty


#: 统一异常类型：优先用 ``llm_client.LLMError``，缺失时用本地等价实现
class _FallbackLLMError(Exception):
    def __init__(self, code, detail=""):
        self.code = str(code) if code else "llm_error"
        # 异常 message 会进 ComfyUI 的 stderr traceback，必须先做编码降级
        self.detail = _safe_text(
            (detail if isinstance(detail, str) else str(detail or ""))[:200]
        )
        super().__init__(_safe_text(f"{self.code}: {self.detail}"))


LLMError = getattr(_llm_client, "LLMError", _FallbackLLMError) if _llm_client else _FallbackLLMError


# --------------------------------------------------------------------------- 供应商注册表（契约 §14.1）


def list_providers():
    """供应商全表（契约 §14.1）：优先 ``providers.list_providers()``，容错回落 v1 四家。"""
    if _providers_mod is not None:
        function = getattr(_providers_mod, "list_providers", None)
        if callable(function):
            try:
                rows = function()
                if isinstance(rows, list) and rows:
                    cleaned = [row for row in rows if isinstance(row, dict)]
                    if cleaned:
                        return cleaned
            except Exception:  # noqa: BLE001 - 并行开发期接口未就绪
                _log("providers.list_providers() 不可用，回落内置四家")
    return [dict(item) for item in _FALLBACK_PROVIDERS.values()]


def provider_options():
    """``INPUT_TYPES`` 的 provider COMBO 选项（契约 §14.1）。"""
    if _providers_mod is not None:
        function = getattr(_providers_mod, "provider_options", None)
        if callable(function):
            try:
                options = function()
                if (
                    isinstance(options, list)
                    and options
                    and all(isinstance(item, str) and item for item in options)
                ):
                    return list(options)
            except Exception:  # noqa: BLE001
                _log("providers.provider_options() 不可用，回落内置四家")
    return list(_FALLBACK_PROVIDERS.keys())


def provider_base_urls():
    """``{id: base_url}`` 映射（契约 §14.1 兼容别名 ``PROVIDER_BASE_URLS``）。"""
    if _providers_mod is not None:
        table = getattr(_providers_mod, "PROVIDER_BASE_URLS", None)
        if isinstance(table, dict) and table:
            mapping = {}
            for key, value in table.items():
                if isinstance(key, str) and isinstance(value, str):
                    mapping[key] = value
            if mapping:
                return mapping
        table = getattr(_providers_mod, "PROVIDERS", None)
        if isinstance(table, dict) and table:
            mapping = {}
            for key, value in table.items():
                if isinstance(key, str):
                    if isinstance(value, str):
                        mapping[key] = value
                    elif isinstance(value, dict) and isinstance(
                        value.get("base_url"), str
                    ):
                        mapping[key] = value["base_url"]
            if mapping:
                return mapping
    return {key: item.get("base_url", "") for key, item in _FALLBACK_PROVIDERS.items()}


def provider_label(provider):
    """供应商显示名（契约 §14.1 ``label``）；未知返回原 id。"""
    key = _norm(provider)
    if not key:
        return ""
    for row in list_providers():
        if _norm(row.get("id")) == key:
            label = _s(row.get("label")) or _s(row.get("name"))
            if label:
                return label
    fallback = _FALLBACK_PROVIDERS.get(key)
    if fallback:
        return _s(fallback.get("label")) or key
    return key


# --------------------------------------------------------------------------- 默认值探测（契约 §14.5）

#: 判定「目录里真有模型」的最小 gguf 体积（字节）。空占位文件（0 字节）不算。
_MIN_GGUF_BYTES = 100 * 1024 * 1024

#: 探测时向下扫描的最大子目录层数（避免递归全盘）
_MODEL_SCAN_DEPTH = 2

_MODEL_PATH_CACHE = None
_MODEL_PATH_SCANNED = False


def _dir_has_real_gguf(directory, max_depth=_MODEL_SCAN_DEPTH, min_bytes=_MIN_GGUF_BYTES):
    """目录（含最多 ``max_depth`` 层子目录）内是否存在 ``>= min_bytes`` 的 ``.gguf``。

    只用于「这个模型根目录是不是空的」这种便宜的判断，**不递归全盘**。
    """
    try:
        root = Path(directory)
        if not root.is_dir():
            return False
    except (OSError, ValueError):
        return False

    def walk(current, depth):
        try:
            entries = list(os.scandir(current))
        except OSError:
            return False
        subdirs = []
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    if not entry.name.lower().endswith(".gguf"):
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_size >= min_bytes:
                            return True
                    except OSError:
                        continue
                elif entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
            except OSError:
                continue
        if depth <= 0:
            return False
        for path in subdirs:
            if walk(path, depth - 1):
                return True
        return False

    return walk(str(root), max_depth)


def _settings_snapshot(prefer_file=False):
    """取 settings.json 的 dict。

    :param prefer_file: ``True`` 时**优先直接读盘**（跳过 store 的进程内缓存）。
        注意 ``nodes._store_mod`` 是按文件路径独立加载的模块实例，与测试脚本里
        ``import store`` 得到的不是同一对象——``store.reload()`` 清不到它的缓存。
        因此 ``_detect_model_path(refresh=True)`` 必须走直接读盘才真正反映磁盘现状。
    """
    settings = {}
    if not prefer_file and _store_mod is not None:
        function = getattr(_store_mod, "load_settings", None)
        if callable(function):
            try:
                value = function()
                if isinstance(value, dict):
                    settings = dict(value)
            except Exception:  # noqa: BLE001
                settings = {}
    if not settings:
        try:
            path = _PLUGIN_DIR / "data" / "settings.json"
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    settings = raw
        except (OSError, ValueError, TypeError):
            settings = {}
    return settings if isinstance(settings, dict) else {}


def _detect_model_path(refresh=False):
    """自动探测本地模型根目录（只读；结果模块级缓存）。

    契约 §14.5（含 lead 裁决的「候选必须真有 gguf」）候选顺序：

    1. ``settings.json.llm_model_path``
    2. ``settings.json.model_roots[0]``
    3. 环境变量 ``ANIMA_LLM_MODELS``
    4. ``%USERPROFILE%\\.lmstudio\\models``
    5. ``%USERPROFILE%\\.cache\\lm-studio\\models``
    6. ``%LOCALAPPDATA%\\ollama\\models``
    7. ``""``

    每个候选都必须**含至少 1 个 ``.gguf`` 且体积 > 100MB**（扫 2 层子目录），
    否则跳过 —— 避免把「空目录」当作默认值，害用户点刷新看到 0 个模型。

    在 ``INPUT_TYPES()`` 里会被调用，因此结果缓存；``refresh=True`` 可强制重算。
    """
    global _MODEL_PATH_CACHE, _MODEL_PATH_SCANNED
    if _MODEL_PATH_SCANNED and not refresh:
        return _MODEL_PATH_CACHE

    settings = _settings_snapshot(prefer_file=bool(refresh))

    candidates = []
    # 1) settings.llm_model_path
    for key in ("llm_model_path", "model_path"):
        value = _s(settings.get(key))
        if value:
            candidates.append(value)

    # 2) settings.model_roots[0]（及后续项）
    roots = settings.get("model_roots")
    if isinstance(roots, str):
        roots = [roots]
    if isinstance(roots, list):
        for item in roots:
            value = _s(item)
            if value:
                candidates.append(value)

    # 3) 环境变量
    value = _s(os.environ.get("ANIMA_LLM_MODELS"))
    if value:
        candidates.append(value)

    # 4-6) 本机常见模型目录
    home = _s(os.environ.get("USERPROFILE")) or _s(os.path.expanduser("~"))
    local_app = _s(os.environ.get("LOCALAPPDATA"))
    if home:
        candidates.append(os.path.join(home, ".lmstudio", "models"))
        candidates.append(os.path.join(home, ".cache", "lm-studio", "models"))
    if local_app:
        candidates.append(os.path.join(local_app, "ollama", "models"))

    # 去重（保序、大小写不敏感）
    seen = set()
    ordered = []
    for candidate in candidates:
        text = candidate.strip()
        key = text.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(text)

    # 校验：必须真有 >100MB 的 gguf
    result = ""
    for candidate in ordered:
        try:
            if _dir_has_real_gguf(candidate):
                result = candidate
                break
        except Exception:  # noqa: BLE001 - 探测绝不影响节点加载
            continue

    _MODEL_PATH_CACHE = result
    _MODEL_PATH_SCANNED = True
    return result


# --------------------------------------------------------------------------- 通用小工具


def _s(value):
    """宽松取字符串（去空白）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _norm(value):
    return _s(value).lower()


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dicts(value):
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _first(source, keys, default=None):
    """按 keys 顺序取第一个「有效」值（空串/空列表视为无效）。"""
    if not isinstance(source, dict):
        return default
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue
        return value
    return default


def _split_tokens(value):
    """契约 §5 ``split_tokens``：按逗号切分、去空白、过滤空串。

    额外容忍 list / 换行分隔（对正常数据行为一致）。
    """
    tokens = []
    if value is None:
        return tokens
    if isinstance(value, (list, tuple)):
        for item in value:
            tokens.extend(_split_tokens(item))
        return tokens
    text = value if isinstance(value, str) else str(value)
    for piece in text.replace("\r", "\n").replace("\n", ",").split(","):
        token = piece.strip()
        if token:
            tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# v10：positive 多行分段结构（用户明确要求，参考「正面提示词结构」）
# ---------------------------------------------------------------------------
#: 段名常量（与 ``plugin/cn_tags.py`` 的 ``SEGMENT_*`` 保持一致）
_SEG_QUALITY = "quality"
_SEG_FRAMING = "framing"
_SEG_CHARACTER = "character"
_SEG_SCENE = "scene"

#: 行内分隔符（逗号 + 空格）；**行尾不带逗号** —— 见 skill「输出格式」一节
_TAG_SEP = ", "


def _join_segments(quality, framing, characters, scene):
    """把各段标签拼成**多行** positive。

    行结构（``正面提示词结构``）：

    ==========  ==========================================
    行序         内容
    ==========  ==========================================
    第 1 行      质量标签
    第 2 行      图片视距 + 角色数量 + 多角色的大致动作
    第 3 行起    每个角色一行（角色、动作、着装与表情）
    最后一行     场景 / 背景 / 画风
    ==========  ==========================================

    - 段内与跨段都做大小写不敏感去重（保留首次出现顺序）
    - **空段整行省略**，不产生空行
    - 行内用 ``", "`` 分隔，行尾不带逗号（ComfyUI 的 CLIPTextEncode 里
      换行等价于空格，行尾逗号纯属冗余）
    """
    seen = set()
    lines = []

    def _take(parts):
        out = []
        for part in parts or ():
            token = (part or "").strip() if isinstance(part, str) else _s(part)
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
        return out

    buckets = [_take(quality), _take(framing)]
    for row in characters or ():
        buckets.append(_take(row))
    buckets.append(_take(scene))

    for bucket in buckets:
        if bucket:
            lines.append(_TAG_SEP.join(bucket))
    return "\n".join(lines)


def _split_lines(text):
    """把多行 positive 拆成 ``[[标签, ...], ...]``（按行分组，行内按逗号切）。

    与 ``_join_segments`` 互逆（忽略空行与空标签）。
    """
    rows = []
    if not isinstance(text, str) or not text.strip():
        return rows
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        tokens = _split_tokens(line)
        if tokens:
            rows.append(tokens)
    return rows


def _join_lines(rows):
    """``[[标签...], ...]`` -> 多行文本（``_split_lines`` 的逆操作）。

    **稳定去重（保序，行内 + 跨行）**：上游任何一步产生重复标签
    （重试拼接、``_restructure_rows`` 从多行各收一个同维标签等）
    都不会漏到最终 positive。比较用「小写 + 下划线归一空格」，
    所以 ``cowboy_shot`` 与 ``cowboy shot`` 视为同一个标签。
    """
    seen = set()
    out_rows = []
    for tokens in (rows or ()):
        kept = []
        for tag in tokens:
            text = str(tag if tag is not None else "").strip()
            if not text:
                continue
            key = text.lower().replace("_", " ")
            if key in seen:
                continue
            seen.add(key)
            kept.append(text)
        if kept:
            out_rows.append(kept)
    return "\n".join(_TAG_SEP.join(tokens) for tokens in out_rows)


def _dedupe_join(parts, sep=", "):
    """契约 §5 ``dedupe_join``：大小写不敏感去重，保留首次出现顺序。"""
    seen = set()
    ordered = []
    for part in parts:
        token = (part or "").strip() if isinstance(part, str) else _s(part)
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(token)
    return sep.join(ordered)


#: 服装覆盖意图检测 —— **替换类**信号词（强意图：用户明确要改）
_OUTFIT_REPLACE_WORDS = (
    "换成", "改成", "换上", "换为", "换做", "改为", "替换为", "替成", "变成",
    "change to", "switch to", "replace with", "instead of", "swap to",
)

#: 服装覆盖意图检测 —— **否定类**信号词（强意图：用户明确不要当前这身）
_OUTFIT_NEGATE_WORDS = (
    "不要", "不穿", "别穿", "去掉", "脱掉", "没有这身", "换了这身",
    "without", "not wearing", "remove", "no longer",
)

#: **穿着描述类**词 —— 弱信号：不单独触发，但配合「颜色冲突」判据可以触发。
#:
#: 实测教训（2026-11-21）：把「穿」当**无条件**信号词会误触真实场景
#: 「一个穿白色层叠长裙的银发少女回头看向镜头」—— 用户是在**描述当前选中的
#: 服装**（假日皮肤就是层叠白裙），不是要换装，误触会把白裙标签删掉。
#: 但完全放弃这类句式也不行：用户原话「用自然语言描述角色服装后没有覆盖掉
#: 原本的服装」说的正是「让她穿一件黑色长裙」这种表述。
#: 所以保留为**弱信号**，由「用户说的颜色与当前服装主色冲突」决定是否触发。
_OUTFIT_WEAR_WORDS = (
    "穿", "身着", "身穿", "套着", "披着",
    "wearing", "dressed in", "put on",
)

#: 服装名词（中英）。只有它出现时才可能触发覆盖，避免「换成蓝眼睛」这类误触。
_OUTFIT_NOUN_WORDS = (
    "裙", "长裙", "短裙", "连衣裙", "礼服", "西装", "泳装", "泳衣", "校服", "制服",
    "外套", "大衣", "和服", "睡衣", "女仆装", "旗袍", "汉服", "盔甲", "战袍", "衣服",
    "dress", "skirt", "gown", "suit", "swimsuit", "bikini", "uniform", "serafuku",
    "kimono", "coat", "jacket", "outfit", "clothes", "clothing", "armor", "lingerie",
)

#: 颜色词表（中英双语）-> 标准化英文色名。
_OUTFIT_COLOR_ALIASES = {
    "black": ("黑",),
    "white": ("白",),
    "red": ("红",),
    "blue": ("蓝",),
    "purple": ("紫",),
    "green": ("绿",),
    "yellow": ("黄",),
    "pink": ("粉",),
    "orange": ("橙",),
    "gray": ("灰", "grey"),
    "gold": ("金",),
    "silver": ("银",),
    "brown": ("棕",),
    "cyan": ("青",),
}

#: 主服装核心词：只有含这些词的标签才代表「这套衣服的主色」。
#: 用来排除配饰色与补充细节对判据的干扰 —— 流萤原皮是「白裙 + 黑手套」，
#: 主色必须是 white，否则用户说「穿黑色长裙」时会因为黑手套"颜色一致"而漏检。
_MAIN_GARMENT_WORDS = (
    "dress", "skirt", "gown", "kimono", "suit", "uniform", "coat", "jacket",
    "swimsuit", "bikini", "serafuku", "robe", "clothes", "outfit",
)

#: 排除「发色 / 瞳色」上下文，避免「紫发少女」被误当成"服装是紫色"。
_BODY_COLOR_CONTEXT_RE = re.compile(
    r"(黑|白|红|蓝|紫|绿|黄|粉|橙|灰|金|银|棕|青)色?\s*(?:发|头发|发丝|瞳|眼睛|眼眸|眼)"
    r"|(?:black|white|red|blue|purple|green|yellow|pink|orange|gray|grey|gold"
    r"|silver|brown|cyan)[\s_]+(?:hair|eyes|eyebrows)",
    re.IGNORECASE,
)


#: 英文颜色词按**词边界**匹配：不能直接用子串 ``in``，
#: 否则 ``layered dress`` 里的 ``red``、``orange`` 都会被误判成颜色
#: （实测把遐蝶假日皮肤的主色识别成 {red, white}）。
#: ``(?<![a-z])...(?:![a-z])`` 同时兼容 ``purple_dress`` 这种下划线连接写法。
_EN_COLOR_RE = re.compile(
    r"(?<![a-z])(black|white|red|blue|purple|green|yellow|pink|orange|gray|grey"
    r"|gold|silver|brown|cyan)(?![a-z])")


def extract_colors(text):
    """从文本里提取颜色词，返回标准化英文色名集合。

    会先剔掉「紫发 / 紫瞳 / purple hair / purple eyes」这类**身体部位**的颜色，
    否则「紫发少女，穿黑色长裙」会被当成"用户想要紫色"，把换装意图吞掉。

    :returns: ``{"black", "purple", ...}`` 形式的 set
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    try:
        cleaned = _BODY_COLOR_CONTEXT_RE.sub(" ", text.lower())
        found = set()
        # 英文：词边界匹配（避免 layered 里的 "red" 这种子串误判）
        for match in _EN_COLOR_RE.finditer(cleaned):
            word = match.group(1)
            found.add("gray" if word == "grey" else word)
        # 中文：单字匹配（中文字符不会和拉丁字母混淆）
        for canonical, aliases in _OUTFIT_COLOR_ALIASES.items():
            if any(alias in cleaned for alias in aliases):
                found.add(canonical)
        return found
    except Exception as exc:  # noqa: BLE001
        _log(f"extract_colors 异常（返回空）：{type(exc).__name__}: {exc}")
        return set()


def outfit_main_colors(outfit):
    """提取当前服装的**主色**（只看含主服装核心词的标签）。

    实测依据：流萤原皮的标签是 ``white dress, black hairband, black gloves`` ——
    如果把所有标签的颜色都算进来，主色会变成 {white, black}，用户说
    「穿黑色长裙」时会因为与黑手套"颜色一致"而漏检。只看主服装才对。

    :returns: 颜色集合；这套服装没有可识别的颜色时返回空集
    """
    if not outfit:
        return set()
    try:
        colors = set()
        for word in _split_tokens(_first(outfit, _WORD_KEYS)):
            lowered = str(word).lower()
            if not any(core in lowered for core in _MAIN_GARMENT_WORDS):
                continue
            colors |= extract_colors(lowered)
        return colors
    except Exception as exc:  # noqa: BLE001
        _log(f"outfit_main_colors 异常（返回空）：{type(exc).__name__}: {exc}")
        return set()


def detect_outfit_override(natural_language, outfit=None):
    """检测自然语言里是否有「改服装」意图。

    两条触发路径：

    **路径 A — 强信号（替换 / 否定词）**：同时命中信号词和服装名词就触发。
    ``换成黑色长裙`` / ``不要这身紫色的礼服`` / ``replace with a kimono``。

    **路径 B — 穿着描述 + 颜色冲突**：``让她穿一件黑色长裙`` 这种句式没有
    "替换"字样，但确实是换装要求（用户原话就是这个）。判据是
    **用户说的颜色与当前服装主色完全不同**：

    * ``穿一件黑色长裙`` + 原皮（``purple dress``）-> {black} vs {purple} 冲突 -> 触发
    * ``穿白色层叠长裙`` + 假日皮肤（``white skirt``）-> {white} vs {white} 一致 -> 不触发

    这样既救回用户的真实场景，又不会误删"描述当前服装"。

    无颜色词（``穿一条裙子``）或当前服装识别不出主色时一律**不触发**
    （宁可漏检，不误触）。

    :param natural_language: 用户自然语言
    :param outfit: 当前命中的服装 dict（可选；不传则路径 B 无法判定）
    :returns: ``{"override","reason","matched","user_colors","outfit_colors"}``
    """
    result = {"override": False, "reason": "", "matched": [],
              "user_colors": [], "outfit_colors": []}
    try:
        text = str(natural_language or "").strip().lower()
        if not text:
            result["reason"] = "empty"
            return result
        nouns = [word for word in _OUTFIT_NOUN_WORDS if word in text]
        if not nouns:
            result["reason"] = "no_outfit_noun"
            return result

        # ---- 路径 A：强信号（替换 / 否定）
        signals = [word for word in _OUTFIT_REPLACE_WORDS if word in text]
        signals += [word for word in _OUTFIT_NEGATE_WORDS if word in text]
        if signals:
            result.update({
                "override": True,
                "reason": "signal+noun",
                "matched": signals[:2] + nouns[:2],
            })
            return result

        # ---- 路径 B：穿着描述 + 颜色冲突
        wear = [word for word in _OUTFIT_WEAR_WORDS if word in text]
        if not wear:
            result["reason"] = "no_signal_word"
            result["matched"] = nouns[:2]
            return result

        user_colors = extract_colors(text)
        result["user_colors"] = sorted(user_colors)
        if not user_colors:
            result["reason"] = "wear_no_color"
            result["matched"] = wear[:1] + nouns[:1]
            return result

        current_colors = outfit_main_colors(outfit)
        result["outfit_colors"] = sorted(current_colors)
        if not current_colors:
            result["reason"] = "outfit_no_color"
            result["matched"] = wear[:1] + nouns[:1]
            return result

        if user_colors & current_colors:
            result["reason"] = "color_match"
            result["matched"] = sorted(user_colors & current_colors)
            return result

        result.update({
            "override": True,
            "reason": "color_conflict",
            "matched": sorted(user_colors) + sorted(current_colors),
        })
        return result
    except Exception as exc:  # noqa: BLE001 - 检测失败就按"不覆盖"处理
        _log(f"detect_outfit_override 异常（按不覆盖处理）：{type(exc).__name__}: {exc}")
        result["reason"] = "internal_error"
        return result


def split_body_and_clothing(character, outfit):
    """把当前服装的标签拆成「角色主体特征」和「服装类」。

    判据**不硬编码词表**，跟着数据走：同时出现在**角色** ``activation_words``
    里的词算主体特征（发色 / 瞳色 / 发型 / 发饰），其余算服装类。

    实测印证：流萤原皮的 ``black hairband`` 同时出现在角色 activation_words
    里，所以换装时它会被保留；而遐蝶原皮的 ``strapless dress`` 等只在服装里，
    会被覆盖。这正好符合「换裙子不是换发饰」的语义。

    :returns: ``(body_tags, clothing_tags)``，各自保序去重
    """
    try:
        character_words = {
            token.lower()
            for token in _split_tokens(_first(character, _WORD_KEYS))
        } if character else set()
        outfit_words = _split_tokens(_first(outfit, _WORD_KEYS))
        outfit_words.extend(_split_tokens(_first(outfit, _PROMPT_KEYS)))
        body = []
        clothing = []
        seen = set()
        for word in outfit_words:
            key = word.lower()
            if key in seen:
                continue
            seen.add(key)
            (body if key in character_words else clothing).append(word)
        return body, clothing
    except Exception as exc:  # noqa: BLE001 - 分类失败就当作全部是服装
        _log(f"split_body_and_clothing 异常（按全部覆盖处理）：{type(exc).__name__}: {exc}")
        return [], []


#: 判定「positive 里有没有服装标签」用的核心词。
#:
#: 比 ``_MAIN_GARMENT_WORDS`` 更严格：**不含 clothes / outfit**，
#: 否则 ``wet_clothes``（湿身效果，不是服装款式）会蒙混过关，
#: 让"其实没画衣服"的输出逃过兜底。
_GARMENT_TAG_WORDS = (
    "dress", "skirt", "gown", "kimono", "suit", "uniform", "coat", "jacket",
    "swimsuit", "bikini", "serafuku", "robe", "shirt", "blouse", "sweater",
    "hoodie", "lingerie", "armor", "costume", "qipao", "cheongsam",
)

#: 中文服装名词 -> 英文基础标签（覆盖后兜底"补新服装"用）。
#: 长词在前（``连衣裙`` 要压过 ``裙``）。
_CN_GARMENT_TO_EN = (
    ("连衣裙", "dress"),
    ("长裙", "long dress"),
    ("短裙", "short dress"),
    ("晚礼服", "evening gown"),
    ("礼服", "gown"),
    ("西装", "suit"),
    ("西服", "suit"),
    ("泳装", "swimsuit"),
    ("泳衣", "swimsuit"),
    ("校服", "school uniform"),
    ("制服", "uniform"),
    ("风衣", "trench coat"),
    ("外套", "coat"),
    ("大衣", "coat"),
    ("和服", "japanese clothes"),
    ("睡衣", "pajamas"),
    ("女仆装", "maid outfit"),
    ("旗袍", "chinese clothes"),
    ("汉服", "chinese clothes"),
    ("盔甲", "armor"),
    ("战袍", "armor"),
    ("衬衫", "shirt"),
    ("毛衣", "sweater"),
    ("裙", "dress"),
)

#: 英文服装名词（用户直接写英文时用）。
_EN_GARMENT_WORDS = (
    "dress", "skirt", "gown", "suit", "swimsuit", "uniform", "kimono",
    "coat", "jacket", "bikini", "shirt", "sweater",
)


def _lookup_garment_tag(candidate):
    """用 tagdb 校验一个候选服装标签，返回标准写法（非 general 类拒收）。"""
    lookup = getattr(_tagdb_mod, "lookup", None) if _tagdb_mod is not None else None
    if not callable(lookup):
        return ""
    try:
        hit = lookup(str(candidate))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(hit, dict) or not hit.get("hit"):
        return ""
    if hit.get("category") != 0:
        return ""
    name = _s(hit.get("name"))
    return name if name else ""


def _has_garment_tag(positive):
    """positive 里是否已经出现服装类标签。"""
    try:
        for token in _split_tokens(positive):
            lowered = str(token).lower()
            if any(word in lowered for word in _GARMENT_TAG_WORDS):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def extract_garment_tags(natural_language, limit=2):
    """从用户自然语言里提取「新服装」的英文标签（经 tagdb 校验）。

    **用途（P0 兜底）**：覆盖触发时代码侧会删掉旧服装，如果 LLM 没有再写新服装，
    结果就是"角色光着"—— 比"衣服不对"更糟。所以模板路径直接补、LLM 路径兜底补。

    规则：
      1. 颜色（:func:`extract_colors`）+ 服装名词组合优先：``黑色``+``长裙``
         -> ``black dress`` / ``long dress``
      2. 组合不合法再退回基础词（``dress`` / ``swimsuit`` / ...）
      3. 每个候选都过 tagdb，**只收 general 类**的标准写法
      4. 什么都找不到返回 ``[]``（调用方不要编造标签）

    :returns: 最多 ``limit`` 个标准标签，保序去重
    """
    try:
        text = str(natural_language or "")
        if not text.strip():
            return []
        colors = sorted(extract_colors(text))
        garments = []
        for cn, en in _CN_GARMENT_TO_EN:
            if cn in text:
                garments.append(en)
        lowered = text.lower()
        for word in _EN_GARMENT_WORDS:
            if re.search(r"(?<![a-z])" + word + r"(?![a-z])", lowered):
                garments.append(word)
        ordered = []
        for item in garments:
            if item and item not in ordered:
                ordered.append(item)
        if not ordered:
            return []
        # 长词优先（long dress 比 dress 更具体）
        ordered.sort(key=lambda item: -len(item))

        combinations = []
        for color in colors:
            for garment in ordered:
                combinations.append(color + " " + garment)

        picked = []
        seen = set()
        for candidate in combinations + ordered:
            name = _lookup_garment_tag(candidate)
            if name and name not in seen:
                seen.add(name)
                picked.append(name)
            if len(picked) >= max(int(limit), 1):
                break
        return picked
    except Exception as exc:  # noqa: BLE001 - 兜底失败就返回空，调用方保持原样
        _log(f"extract_garment_tags 异常（返回空）：{type(exc).__name__}: {exc}")
        return []


def _float_or(value, default=1.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


#: 中文 / 全角字符片段：CJK 标点(U+3000-303F)、汉字(U+3400-4DBF / U+4E00-9FFF)、
#: 兼容汉字(U+F900-FAFF)、全角区(U+FF00-FFEF，含 `，` `：` `（）` 等)。
#: 与 ``skill_check._CJK_RE`` 同一套取值，保证"中文"的判断口径一致。
_CJK_SEGMENT_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]+")

#: 清理后可能残留的孤立标点（中英混排时中文被删、标点留在英文旁边）。
_RESIDUAL_PUNCT = " \t,;:|，、。；：,./\\-+*_\"'`()[]{}<>"


def strip_cjk_segments(text):
    """把标签串里的**中文片段**剔除，只保留英文。

    用途（lead 决策 B）：模板路径的 positive 会被直接送进 CLIP，而 CLIP 不懂
    中文 —— 中文是纯噪声 token，还会让用户以为插件坏了。LLM 路径不用这个：
    那边由 ``skill_check`` 的 ``contains_chinese`` 保证输出是英文。

    边界规则：

    * **只删中文，保留英文**：``"雨夜霓虹街头 black dress"`` -> ``"black dress"``
    * 中文判定覆盖全角标点（``，`` ``。`` ``、`` ``（）`` ``：``），
      中英混排不会留下孤立标点
    * 清理收尾：连续逗号合并、首尾去标点、空白折叠（``re.sub(r"\\s+", " ")``）
    * **全中文输入返回空串** —— 兜底由调用方负责（这里不塞默认标签，
      保持函数纯粹、可单测）

    :param text: 逗号分隔的标签串
    :returns: 清理后的标签串；入参不是 ``str`` 时原样返回
    """
    if not isinstance(text, str):
        return text
    if not text.strip():
        return ""
    try:
        pieces = []
        for token in _split_tokens(text):
            cleaned = _CJK_SEGMENT_RE.sub(",", token)
            for piece in cleaned.split(","):
                piece = piece.strip(_RESIDUAL_PUNCT)
                piece = re.sub(r"\s+", " ", piece).strip()
                if piece:
                    pieces.append(piece)
        return ", ".join(pieces)
    except Exception as exc:  # noqa: BLE001 - 清理失败绝不能影响出图
        _log(f"strip_cjk_segments 异常（原样返回）：{type(exc).__name__}: {exc}")
        return text


def _hash_params(payload):
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_json_obj(raw):
    """把 ``_ui_json`` / ``_llm_json`` 之类的 holder 解析成 dict（失败返回 {}）。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- 数据视图（store 容错）


class _DataView:
    """基于纯 dict 的解析器（qa 单测可注入假数据）。

    字段名以契约 §5 点名的 ``activation_words`` / ``prompt`` / ``lora_name`` /
    ``negative_suppress`` / ``artist_tags`` 为主，辅以常见别名兜底。
    """

    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        self._data = data
        self.characters = _dicts(data.get("characters"))
        self.styles = _dicts(data.get("styles"))
        self.negative_base = _s(data.get("negative_base"))

    # -- 名称集合
    @staticmethod
    def _names(obj):
        names = set()
        for key in ("display_name", "name", "id", "title", "slug"):
            value = _norm(obj.get(key))
            if value:
                names.add(value)
        for alias in _as_list(obj.get("aliases")) + _as_list(obj.get("alias")):
            value = _norm(alias)
            if value:
                names.add(value)
        return names

    @classmethod
    def _match(cls, candidates, name):
        key = _norm(name)
        if not key:
            return None
        # 1) 精确匹配
        for candidate in candidates:
            if key in cls._names(candidate):
                return candidate
        # 2) 包含匹配（长度 ≥2 才启用，避免误命中）
        if len(key) >= 2:
            for candidate in candidates:
                for candidate_name in cls._names(candidate):
                    if len(candidate_name) >= 2 and (
                        candidate_name in key or key in candidate_name
                    ):
                        return candidate
        return None

    def resolve_character(self, name):
        return self._match(self.characters, name)

    def resolve_style(self, name):
        return self._match(self.styles, name)

    def resolve_outfit(self, character, hint):
        """契约 §10 ``resolve_outfit``：``(命中服装 或 默认服装 或 None, 同角色其它服装)``。"""
        outfits = _dicts((character or {}).get("outfits"))
        if not outfits:
            return None, []

        key = _norm(hint)
        hit = None
        if key:
            for outfit in outfits:
                if key in self._names(outfit):
                    hit = outfit
                    break
            if hit is None:
                for outfit in outfits:
                    for outfit_name in self._names(outfit):
                        if len(outfit_name) >= 2 and (
                            outfit_name in key or key in outfit_name
                        ):
                            hit = outfit
                            break
                    if hit is not None:
                        break

        if hit is None:
            for outfit in outfits:
                if (
                    outfit.get("is_default")
                    or outfit.get("default")
                    or outfit.get("is_base")
                ):
                    hit = outfit
                    break
            if hit is None and not key:
                # hint 为空且数据层未标默认 → 取第一套作为默认
                hit = outfits[0]

        if hit is None:
            siblings = list(outfits) if key else []
        else:
            siblings = [outfit for outfit in outfits if outfit is not hit]
        return hit, siblings

    def catalog(self):
        characters = []
        for character in self.characters:
            characters.append(
                {
                    "id": _s(character.get("id")),
                    "display_name": _s(
                        _first(character, ("display_name", "name", "id"))
                    ),
                    "aliases": [_s(a) for a in _as_list(character.get("aliases")) if _s(a)],
                    "outfits": [
                        {
                            "id": _s(outfit.get("id")),
                            "display_name": _s(
                                _first(outfit, ("display_name", "name", "id"))
                            ),
                        }
                        for outfit in _dicts(character.get("outfits"))
                    ],
                }
            )
        styles = [
            {
                "id": _s(style.get("id")),
                "display_name": _s(_first(style, ("display_name", "name", "id"))),
            }
            for style in self.styles
        ]
        return {"characters": characters, "styles": styles}


class _StoreView:
    """委托给 ``store.py``；任一调用失败都回退到 :class:`_DataView`。"""

    def __init__(self, store_module):
        self._module = store_module
        try:
            data = store_module.load_all()
        except Exception:  # noqa: BLE001 - store 损坏时用空数据继续（store_corrupt）
            _log(f"store.load_all() 失败，使用内置默认值：{traceback.format_exc(limit=2)}")
            data = {}
        self._fallback = _DataView(data if isinstance(data, dict) else {})
        self.characters = self._fallback.characters
        self.styles = self._fallback.styles
        self.negative_base = self._fallback.negative_base

    def resolve_character(self, name):
        function = getattr(self._module, "resolve_character", None)
        if callable(function):
            try:
                result = function(name)
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                _log(f"store.resolve_character({name!r}) 失败，回退内置解析")
        return self._fallback.resolve_character(name)

    def resolve_style(self, name):
        function = getattr(self._module, "resolve_style", None)
        if callable(function):
            try:
                result = function(name)
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                _log(f"store.resolve_style({name!r}) 失败，回退内置解析")
        return self._fallback.resolve_style(name)

    def resolve_outfit(self, character, hint):
        function = getattr(self._module, "resolve_outfit", None)
        if callable(function):
            try:
                result = function(character, hint)
                if isinstance(result, tuple) and len(result) == 2:
                    hit, siblings = result
                    if hit is None or isinstance(hit, dict):
                        return hit, _dicts(siblings)
            except Exception:  # noqa: BLE001
                _log(f"store.resolve_outfit({hint!r}) 失败，回退内置解析")
        return self._fallback.resolve_outfit(character, hint)

    def catalog(self):
        function = getattr(self._module, "catalog", None)
        if callable(function):
            try:
                result = function()
                if isinstance(result, dict) and (
                    result.get("characters") or result.get("styles")
                ):
                    return {
                        "characters": _as_list(result.get("characters")),
                        "styles": _as_list(result.get("styles")),
                    }
            except Exception:  # noqa: BLE001
                _log("store.catalog() 失败，回退内置 catalog")
        return self._fallback.catalog()


def _make_view(data=None):
    """构造数据视图：``data`` 显式给出时用纯 dict；否则优先委托 ``store``。"""
    if isinstance(data, (_DataView, _StoreView)):
        return data
    if data is not None:
        return _DataView(data)
    if _store_mod is not None and hasattr(_store_mod, "resolve_character"):
        return _StoreView(_store_mod)
    return _DataView({})


def _default_system_prompt():
    """默认 system prompt（契约 §14.5）：store（内部再取 skill）→ 极简兜底。"""
    if _store_mod is not None:
        function = getattr(_store_mod, "default_system_prompt", None)
        if callable(function):
            try:
                value = function()
                if isinstance(value, str) and value.strip():
                    return value
            except Exception:  # noqa: BLE001
                _log("store.default_system_prompt() 失败，使用极简兜底")
    return FALLBACK_SYSTEM_PROMPT


def _default_negative_base():
    """默认负面基础词（契约 §14.5）：只认 store（settings.json），否则空串。"""
    if _store_mod is not None:
        try:
            data = _store_mod.load_all()
            if isinstance(data, dict):
                value = data.get("negative_base")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:  # noqa: BLE001
            _log("store.load_all() 失败，负面基础词用空串")
    return FALLBACK_NEGATIVE_BASE


def _default_positive_base():
    """默认正面基础词（v3 B2，与负面基础词对称）：只认 store（settings.json），否则空串。"""
    if _store_mod is not None:
        try:
            data = _store_mod.load_all()
            if isinstance(data, dict):
                value = data.get("positive_base")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:  # noqa: BLE001
            _log("store.load_all() 失败，正面基础词用空串")
    return ""


def _catalog():
    return _make_view(None).catalog()


# --------------------------------------------------------------------------- holder 解析


def _normalize_ui(ui):
    """补齐 holder JSON 缺字段（契约 §3 / §14.4：绝不因缺字段崩溃）。"""
    source = ui if isinstance(ui, dict) else {}
    sampling = source.get("sampling")
    sampling_out = {}
    if isinstance(sampling, dict):
        for key in ("top_k", "top_p", "temperature", "max_tokens", "seed"):
            if key in sampling:
                sampling_out[key] = sampling[key]

    negative_base = source.get("negative_base")
    # v3 B2：正面基础词（与 negative_base 对称，允许 ``""`` 显式清空）
    positive_base = source.get("positive_base")

    # 契约 §14.4：ui 子对象新字段（缺字段用默认）
    ui_source = source.get("ui")
    ui_source = ui_source if isinstance(ui_source, dict) else {}
    ui_out = {
        "skill_id": _s(ui_source.get("skill_id")) or DEFAULT_SKILL_ID,
        "target_profile": _s(ui_source.get("target_profile")) or "anima",
        "collapsed_main": bool(ui_source.get("collapsed_main", False)),
    }

    # 契约 §14.4：cloud_models（模型下拉缓存）
    cloud_out = {"base_url": "", "models": [], "ts": 0}
    cloud = source.get("cloud_models")
    if isinstance(cloud, dict):
        cloud_out["base_url"] = _s(cloud.get("base_url"))
        if isinstance(cloud.get("models"), list):
            cloud_out["models"] = cloud["models"]
        timestamp = cloud.get("ts")
        if isinstance(timestamp, (int, float)):
            cloud_out["ts"] = int(timestamp)

    # 契约 §14.4：dialog 上限 20 条（超出丢最旧）
    dialog = _dicts(source.get("dialog"))[-20:]

    return {
        "schema": source.get("schema", 1),
        "character_rows": _dicts(source.get("character_rows")),
        "style_rows": _dicts(source.get("style_rows")),
        "text_map": _dicts(source.get("text_map")),
        "system_prompt": _s(source.get("system_prompt")),
        "negative_base": negative_base if isinstance(negative_base, str) else None,
        "positive_base": positive_base if isinstance(positive_base, str) else None,
        "sampling": sampling_out,
        "ui": ui_out,
        "dialog": dialog,
        "cloud_models": cloud_out,
    }


def _build_dialog_entry(natural_language, positive, negative):
    """契约 §14.4：本轮对话条目。

    注意：**实际写入 holder 由前端 ``onExecuted`` 完成**，Python 只回传条目。
    """
    return {
        "role": "user",
        "text": natural_language if isinstance(natural_language, str) else "",
        "positive": positive if isinstance(positive, str) else "",
        "negative": negative if isinstance(negative, str) else "",
        "ts": int(time.time()),
    }


def _next_dialog_len(ui):
    """契约 §14.4：dialog 上限 20 条（超出丢最旧）。"""
    source = ui if isinstance(ui, dict) else {}
    return min(len(_dicts(source.get("dialog"))) + 1, 20)


def _seed_from_ui(ui):
    """返回 ``(原始种子, 实际种子)``；``-1`` 表示随机。"""
    sampling = ui.get("sampling") if isinstance(ui, dict) else None
    raw = -1
    if isinstance(sampling, dict) and "seed" in sampling:
        try:
            raw = int(sampling.get("seed"))
        except (TypeError, ValueError):
            raw = -1
    actual = random.randint(0, 2**63 - 1) if raw < 0 else raw
    return raw, actual


def _sampling_from(holder):
    """LLM 节点 holder 的采样参数（契约 §4）。"""
    defaults = {
        "top_k": 40,
        "top_p": 0.9,
        "temperature": 0.7,
        "max_tokens": 1024,
        "seed": -1,
    }
    source = holder.get("sampling") if isinstance(holder, dict) else None
    if isinstance(source, dict):
        for key, default in defaults.items():
            if key not in source:
                continue
            value = source.get(key)
            try:
                if key in ("top_p", "temperature"):
                    defaults[key] = float(value)
                else:
                    defaults[key] = int(value)
            except (TypeError, ValueError):
                pass
    return defaults


def _runtime_from(holder):
    """LLM 节点 holder 的运行参数（契约 §4）。"""
    defaults = {"ctx": 8192, "timeout": 120, "port": 0}
    source = holder.get("runtime") if isinstance(holder, dict) else None
    if isinstance(source, dict):
        for key, default in defaults.items():
            if key not in source:
                continue
            try:
                defaults[key] = int(source.get(key))
            except (TypeError, ValueError):
                pass
    return defaults


# --------------------------------------------------------------------------- 组装核心（契约 §5）


def _strength_pair(obj):
    base = _float_or(_first(obj, ("strength", "lora_strength"), 1.0), 1.0)
    model_strength = _float_or(_first(obj, ("model_strength",), base), base)
    clip_strength = _float_or(_first(obj, ("clip_strength",), base), base)
    return model_strength, clip_strength


def _dedupe_tokens(tokens):
    """大小写不敏感去重，保留首次出现顺序（返回 list）。"""
    seen = set()
    ordered = []
    for token in tokens:
        text = (token or "").strip() if isinstance(token, str) else _s(token)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _same_outfit(first, second):
    """判断两个 outfit dict 是否同一套。

    ``store.resolve_outfit`` 返回的是深拷贝，``is`` 比较不成立，因此按
    ``id`` / ``display_name`` 比对（契约 §10 的字段）。
    """
    if first is second:
        return True
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    for key in ("id", "display_name"):
        left = _norm(first.get(key))
        right = _norm(second.get(key))
        if left and right and left == right:
            return True
    return False


def _add_lora(stack, name, source):
    """向 LoRA 栈追加 ``(name, model_strength, clip_strength)``，按 name 去重。"""
    identifier = _s(name)
    if not identifier:
        return
    key = identifier.lower()
    for existing in stack:
        if existing[0].lower() == key:
            return
    model_strength, clip_strength = _strength_pair(source)
    stack.append((identifier, model_strength, clip_strength))


def _compose_core(natural_language, ui, data=None):
    """契约 §5 模板组装（纯函数，不联网、不依赖 ComfyUI）。"""
    started = time.perf_counter()
    config = _normalize_ui(ui)
    view = _make_view(data)
    language = natural_language if isinstance(natural_language, str) else ""

    # 契约 §14.5：负面基础词只保留一份（store/settings.json），全空即空串
    fallback_negative = _default_negative_base()
    negative_base = config["negative_base"]
    if not negative_base or not negative_base.strip():
        negative_base = view.negative_base or fallback_negative
    negative_base = negative_base.strip() or fallback_negative

    # v3 B2：正面基础词（与负面对称）。holder 非空优先，否则回退 settings.json。
    positive_base_value = config.get("positive_base")
    if isinstance(positive_base_value, str) and positive_base_value.strip():
        positive_base = positive_base_value.strip()
        positive_base_source = "ui"
    else:
        positive_base = _default_positive_base()
        positive_base_source = "store" if positive_base else ""
    positive_base = (positive_base or "").strip()

    # v10：多行分段。不再用一个扁平列表，而是按「正面提示词结构」分组：
    #   seg_quality      第 1 行：质量标签
    #   seg_framing      第 2 行：视距 + 角色数量 + 多角色动作
    #   seg_characters   第 3 行起：每个角色一行
    #   seg_scene        最后一行：场景 / 背景 / 画风
    seg_quality = []
    seg_framing = []
    seg_characters = []
    seg_scene = []

    # v3 B1：正面基础词是**质量标签**，落在第 1 行最前面
    if positive_base:
        seg_quality.extend(_split_tokens(positive_base))
    negative_parts = [negative_base]

    # v10：中文确定性映射（用户明确要求「按确定性映射，不是靠 LLM 猜」）。
    # 「全身照，城市街道」-> framing: full body / scene: street, city
    # 「高清」-> quality: best quality, masterpiece, highres
    # 「安全」-> **按 CSV 要求不写进 positive**（记进 debug.cn_dropped）
    cn_mapped = _translate_cn(language)
    seg_quality.extend(cn_mapped["quality"])
    seg_framing.extend(cn_mapped["framing"])
    seg_scene.extend(cn_mapped["scene"])
    lora_stack = []

    debug = {
        "schema": 1,
        "used_llm": False,
        "fallback": "",
        "detail": "",
        "mode": "template",
        "seed": -1,
        "characters": [],
        "outfits": [],
        "styles": [],
        "injected_triggers": [],
        "text_map_hits": [],
        "skipped_rows": [],
        "lora_stack": [],
        "degraded": [],
        # 契约 §5 B3 差集抑制的可追溯来源（lead 第三轮裁决）
        "negative_suppress_source": {"outfit": [], "character": []},
        # v3 B2：正面基础词来源（"ui" / "store" / ""）
        "positive_base_source": positive_base_source,
        "positive_base": positive_base,
        "elapsed_ms": 0.0,
    }

    _raw_seed, actual_seed = _seed_from_ui(config)
    debug["seed"] = actual_seed

    # v10：中文确定性映射的追溯信息（哪些短语被翻译、哪些按需求丢弃）
    if cn_mapped["matched"]:
        debug["cn_mapped"] = list(cn_mapped["matched"])
    if cn_mapped["dropped"]:
        debug["cn_dropped"] = list(cn_mapped["dropped"])
    # v11：分段结构要透出给 LLM 路径的来源校验用（扁平 cn_mapped 不够）
    debug["cn_segments"] = {
        "quality": list(cn_mapped.get("quality") or ()),
        "framing": list(cn_mapped.get("framing") or ()),
        "character": list(cn_mapped.get("character") or ()),
        "scene": list(cn_mapped.get("scene") or ()),
    }

    # v14：从**自然语言正文**扫描角色别名（用户输入基本是长句，
    # 长句里带角色名最自然，不能要求每条都去下拉框选）。
    # 与 ui.character_rows 合并去重；命中记 debug.characters_from_text。
    text_rows = _scan_characters_in_text(
        language, view, config["character_rows"], data)
    all_rows = list(config["character_rows"]) + list(text_rows)
    if text_rows:
        debug["characters_from_text"] = [
            _s(row.get("name")) for row in text_rows if _s(row.get("name"))
        ]
        debug["characters_from_text_match"] = [
            _s(row.get("_matched_alias")) for row in text_rows
        ]
    # v14：所有角色纯 id（括号护栏用）
    character_ids = _collect_character_ids(view)
    debug["character_ids"] = list(character_ids)
    # v14.2：**最终**角色数（含正文识别的）—— 多角色时服装映射要留给 LLM 分配
    debug["final_character_count"] = sum(
        1 for _row in all_rows if _s(_row.get("trigger")))
    # v15.3：角色 id -> 库标准 tag（裸 id 补全用；LLM 路径也从 core debug 读）
    character_tag_map = {}
    try:
        _catalog = view.catalog() or {}
        for _char in (_catalog.get("characters") or []):
            _cid = _s(_char.get("id"))
            if not _cid:
                continue
            _tag = _resolve_character_tag(_char)
            if _tag:
                character_tag_map[_cid] = _tag
    except Exception:  # noqa: BLE001
        character_tag_map = {}
    debug["character_tag_map"] = dict(character_tag_map)
    # v15.5：本次**参与**的角色（按行顺序）的库标准 tag，供强制注入用
    _active_tags = []
    for _row in all_rows:
        if not _s(_row.get("trigger")):
            continue
        _char = view.resolve_character(_s(_row.get("name"))) \
            if _s(_row.get("name")) else None
        _tag = _resolve_character_tag(_char) if _char else ""
        if _tag:
            _active_tags.append(_tag)
    debug["active_character_tags"] = _active_tags

    # v12：角色性别表（供人数推断；characters.json 的 gender 字段）
    gender_map = {}
    for row in all_rows:
        name = _s(row.get("name"))
        if not name:
            continue
        character = view.resolve_character(name)
        if character:
            gender_map[name] = _norm(character.get("gender"))
    debug["gender_map"] = gender_map

    # v12：用户手填的 trigger（库名规范化时要跳过，契约 §5 要求原样保留）
    user_triggers = []
    for row in all_rows:
        user_triggers.extend(_split_tokens(row.get("trigger")))
        # v15：解析后的**库标准 tag** 也要保护（否则会被 tagdb 规范化改名）
        _row_character = view.resolve_character(_s(row.get("name"))) \
            if _s(row.get("name")) else None
        if _row_character:
            _resolved = _resolve_character_tag(_row_character)
            if _resolved:
                user_triggers.append(_resolved)
    for row in config["style_rows"]:
        user_triggers.extend(_split_tokens(row.get("trigger")))
    debug["user_triggers"] = _dedupe_tokens(user_triggers)

    # ---------------- 角色行（契约 §5 第一段）
    for row in all_rows:
        hint_consumed_by_preset = False
        # v10：每个角色单独一行（多角色时各占一行，见「正面提示词结构」）
        char_parts = []
        trigger = _s(row.get("trigger"))
        if not trigger:
            if _s(row.get("name")) or _s(row.get("outfit_hint")):
                debug["skipped_rows"].append(
                    {
                        "type": "character",
                        "name": _s(row.get("name")),
                        "reason": "trigger_empty",
                    }
                )
            continue

        name = _s(row.get("name"))
        hint = _s(row.get("outfit_hint"))
        character = view.resolve_character(name) if name else None

        # v15：trigger 一律用**库标准 tag**（不是纯 id —— 裸 id 在库里可能不存在，
        # 或命中「萤火虫」「闪光」这种完全无关的 general 概念）
        resolved_trigger = _resolve_character_tag(character) if character else ""
        # v15.7：**只有** trigger 本身就是角色 id / 别名时才替换成库标准 tag。
        # 用户手填的自定义 trigger（如 `my_custom_trigger`）必须原样保留 ——
        # 契约 §5 触发词保护优先于「库名规范化」。
        _trigger_key = _norm_tag_key(trigger)
        # 注意：**不能**把 trigger 自己放进这个集合，否则永远命中、自定义 trigger 会被覆盖
        _character_keys = set()
        if character:
            _character_keys.add(_norm_tag_key(_s(character.get("id"))))
            for _alias in _as_list(character.get("aliases")):
                _character_keys.add(_norm_tag_key(_s(_alias)))
        if resolved_trigger and _trigger_key in _character_keys:
            trigger_to_use = resolved_trigger
        else:
            trigger_to_use = trigger
            if resolved_trigger:
                debug.setdefault("trigger_kept_custom", []).append(trigger)
        if not resolved_trigger:
            debug["trigger_unresolved"] = debug.get("trigger_unresolved") or []
            debug["trigger_unresolved"].append(name or trigger)
        char_parts.extend(_split_tokens(trigger_to_use))

        if character:
            display_name = _s(_first(character, ("display_name", "name", "id"))) or name
            if display_name not in debug["characters"]:
                debug["characters"].append(display_name)

            outfit, siblings = view.resolve_outfit(character, hint)
            # 命中预设服装时，outfit_hint（预设名，通常是中文如「原皮」「假日皮肤」）
            # 不再当提示词注入，避免中文词污染正向提示词。
            if outfit is not None:
                hint_consumed_by_preset = True
            if hint and outfit is None:
                debug["degraded"].append(
                    {
                        "type": "outfit_not_found",
                        "character": display_name,
                        "hint": hint,
                    }
                )

            activation = _split_tokens(_first(character, _WORD_KEYS))
            # v15：activation_words[0] 是角色名（可能是 `firefly (honkai: star rail)`
            # 这种带括号的写法），统一替换成库标准 tag，避免与 trigger 重复或写法不一致
            if activation and resolved_trigger:
                if _norm_tag_key(activation[0]) in {
                        _norm_tag_key(trigger),
                        _norm_tag_key(resolved_trigger),
                }:
                    activation = [resolved_trigger] + activation[1:]
                elif _norm_tag_key(activation[0]) in {
                        _norm_tag_key(alias)
                        for alias in _as_list(character.get("aliases"))
                }:
                    activation = [resolved_trigger] + activation[1:]
            char_parts.extend(activation)
            debug["injected_triggers"].extend(activation)

            if outfit:
                outfit_name = _s(
                    _first(outfit, ("display_name", "name", "id"))
                )
                if outfit_name and outfit_name not in debug["outfits"]:
                    debug["outfits"].append(outfit_name)

                outfit_activation = _split_tokens(_first(outfit, _WORD_KEYS))
                # v9 服装覆盖：用户自然语言明确描述了新服装时，当前预设服装的
                # **服装类**标签不再注入 positive，改注入 negative 抑制；
                # **角色主体特征**（发色/瞳色/发饰）必须保留 —— 换裙子不是换发饰。
                # 检测不到覆盖意图时，走原有逻辑，行为一字不变。
                # v14.1：正文识别的角色只用自己的那一段做服装检测，
                # 避免「遐蝶穿黑裙，流萤穿白裙」两个角色的服装互相污染
                role_text = natural_language
                if row.get("_from_text"):
                    role_text = _character_text_segment(
                        natural_language, row.get("_matched_alias"),
                        _text_match_aliases(text_rows))
                override = detect_outfit_override(role_text, outfit)
                if override["override"]:
                    body_tags, clothing_tags = split_body_and_clothing(character, outfit)
                    char_parts.extend(body_tags)
                    debug["injected_triggers"].extend(body_tags)
                    # P0：删掉旧服装后**必须补上新服装**，否则角色会"光着"。
                    # 模板路径没法靠 LLM 翻译中文，代码侧直接从用户描述提取。
                    # v15.9：必须**先算新装**——原装里与新装重叠的标签不能压 negative。
                    new_garments = extract_garment_tags(role_text)
                    new_keys = {_norm_tag_key(tag) for tag in new_garments}
                    new_keys.discard("")
                    for word in clothing_tags:
                        # v15.9：原装与新装同色同款（white dress vs white_dress）
                        # 时，压进 negative 会直接压掉用户要的白裙，且会让
                        # skill_check 的 negative_missing 与 positive 自相矛盾。
                        if _norm_tag_key(word) in new_keys:
                            continue
                        negative_parts.append(word)
                        debug["negative_suppress_source"]["outfit"].append(word)
                    char_parts.extend(new_garments)
                    debug["outfit_override"] = {
                        "detected": True,
                        "reason": override["reason"],
                        "matched": override["matched"],
                        "user_colors": override.get("user_colors") or [],
                        "outfit_colors": override.get("outfit_colors") or [],
                        "suppressed_clothing": clothing_tags,
                        "kept_body": body_tags,
                        "new_garments": new_garments,
                    }
                else:
                    char_parts.extend(outfit_activation)
                    char_parts.extend(_split_tokens(_first(outfit, _PROMPT_KEYS)))
                    debug["injected_triggers"].extend(outfit_activation)

                outfit_lora = _s(_first(outfit, _LORA_KEYS))
                if outfit_lora:
                    _add_lora(lora_stack, outfit_lora, outfit)

            # ---- B3 多服装隔离：差集抑制（lead 第三轮裁决）
            # keep = 当前命中服装的激活词 ∪ 角色级激活词
            #   角色身份词（castorice / purple hair / purple eyes 等）必须受保护，
            #   否则会被 sibling 压进 negative 导致人物崩坏。
            character_activation = _split_tokens(_first(character, _WORD_KEYS))
            keep_tokens = list(character_activation)
            if outfit is not None:
                keep_tokens.extend(_split_tokens(_first(outfit, _WORD_KEYS)))
            keep_lower = {token.lower() for token in keep_tokens}
            # v15.9：本次换装新增的服装也要进 keep —— 否则 sibling 的
            # `white dress` 会经由差集抑制把用户要的 `white_dress` 压掉。
            _new_garment_keys = set()
            for _item in (debug.get("outfit_override") or {}).get("new_garments") or ():
                _key = _norm_tag_key(_item)
                if _key:
                    _new_garment_keys.add(_key)
            debug["negative_suppress_source"]["character"].extend(character_activation)

            for sibling in siblings:
                if outfit is not None and _same_outfit(sibling, outfit):
                    continue
                # 顺序稳定：先 sibling.activation_words，再 sibling.negative_suppress
                ordered = _split_tokens(_first(sibling, _WORD_KEYS))
                ordered.extend(_split_tokens(_first(sibling, _NEG_KEYS)))
                for word in ordered:
                    if word.lower() in keep_lower:
                        continue  # 与当前服装/角色共享的词，不自伤
                    if _norm_tag_key(word) in _new_garment_keys:
                        continue  # v15.9：不许压掉用户本次要的新服装
                    negative_parts.append(word)
                    debug["negative_suppress_source"]["outfit"].append(word)

            character_lora = _s(_first(character, _LORA_KEYS))
            if character_lora:
                _add_lora(lora_stack, character_lora, character)
        elif name:
            debug["degraded"].append(
                {"type": "character_not_found", "name": name}
            )

        # 角色装扮提示词（第三列）：用户手填时是「纯英文标签」，紧跟角色 LoRA 触发词注入；
        # 命中预设服装时由预设接管（上面已注入该服装的 activation_words / prompt），
        # 这里不再重复注入，避免预设名（中文）混进提示词。
        if hint and not hint_consumed_by_preset:
            char_parts.extend(_split_tokens(hint))

        if char_parts:
            seg_characters.append(char_parts)

    # ---------------- 风格行（契约 §5 第二段，无 trigger 也继续）
    for row in config["style_rows"]:
        trigger = _s(row.get("trigger"))
        seg_scene.extend(_split_tokens(trigger))

        name = _s(row.get("name"))
        style = view.resolve_style(name) if name else None
        if style:
            display_name = _s(_first(style, ("display_name", "name", "id"))) or name
            if display_name not in debug["styles"]:
                debug["styles"].append(display_name)

            style_activation = _split_tokens(_first(style, _WORD_KEYS))
            seg_scene.extend(style_activation)
            seg_scene.extend(_split_tokens(_first(style, _PROMPT_KEYS)))
            seg_scene.extend(_split_tokens(_first(style, _ARTIST_KEYS)))
            debug["injected_triggers"].extend(style_activation)

            style_lora = _s(_first(style, _LORA_KEYS))
            if style_lora:
                _add_lora(lora_stack, style_lora, style)
        elif name:
            debug["degraded"].append({"type": "style_not_found", "name": name})

    # ---------------- 文字指定提示词（A3，契约 §5 第三段）
    language_lower = language.lower()
    for mapping in config["text_map"]:
        find = _s(mapping.get("find"))
        prompt = _s(mapping.get("prompt"))
        if not find or not prompt:
            continue
        if find.lower() not in language_lower:
            continue
        target = _norm(mapping.get("target")) or "positive"
        if target == "negative":
            negative_parts.append(prompt)
        else:
            # v10：text_map 是用户手填的映射，可能含中文；原行为是统一清掉，
            # 现在 positive 不再整体清理，所以这里单独清，保持行为一致。
            prompt_clean = strip_cjk_segments(prompt)
            if prompt_clean != prompt:
                debug["cjk_cleaned"] = True
            if prompt_clean.strip():
                seg_scene.append(prompt_clean.strip())
        debug["text_map_hits"].append({"find": find, "target": target, "prompt": prompt})

    # ---------------- 原始自然语言：先过确定性映射，剩下的英文进场景行
    # （v9 的做法是整段塞进 positive 再靠 strip_cjk_segments 删中文；
    #  v10 改成先映射、再只保留剩余英文，避免中文进到分段结构里）
    leftover = cn_mapped.get("remaining") or ""
    leftover_raw = leftover
    # v13：未被映射的中文片段**不静默丢弃** —— 透出给用户看「这句我识别不了哪部分」
    _zh_left = " ".join(re.findall(r"[\u4e00-\u9fff]{2,}", leftover_raw))
    if _zh_left.strip():
        debug["zh_unmapped"] = _zh_left.strip()
    leftover_clean = strip_cjk_segments(leftover).strip() if leftover.strip() else ""
    leftover_all_cjk = False
    if leftover_clean != leftover_raw.strip() and leftover_raw.strip():
        debug["cjk_cleaned"] = True
        debug["cjk_removed_chars"] = len(leftover_raw) - len(leftover_clean)
        if not leftover_clean:
            # 剩下的全是中文：没有可用于正向提示词的英文内容
            leftover_all_cjk = True
    if leftover_clean:
        # v14.1：词数 > 5 的英文片段是**句子**不是标签，CLIP 对长句效果差，
        # 塞进 positive 只会稀释权重；记进 debug 供排查。
        _sentence_parts = []
        for _piece in _split_tokens(leftover_clean):
            if len(_piece.split()) > 5:
                _sentence_parts.append(_piece)
                continue
            seg_scene.append(_piece)
        if _sentence_parts:
            debug["en_sentence_skipped"] = _sentence_parts

    # v10：质量标签是模型的**必需渲染参数**，不是"编造用户没描述的内容"。
    # 用户明确说了质量词（高清等）就用用户的；没说就补默认三件套。
    has_content = bool(seg_framing or seg_scene or any(seg_characters))
    if not seg_quality:
        seg_quality = ["best quality", "masterpiece", "highres"]
        debug["quality_default"] = True
    if leftover_all_cjk and not has_content:
        # 全中文又没有角色/风格/场景：只剩默认质量词，标记回退供前端提示
        debug["cjk_fallback"] = True

    # v11 修 2：第 2 行必须有人数（正面提示词结构）。放在 has_content 之后，
    # 避免「补了 1girl 就不算空内容」破坏 cjk_fallback 的判定。
    if not any(_tag_dimension(tag) == "count" for tag in seg_framing):
        # v14：人数要把「正文识别出的角色」一起算进去
        _count_config = dict(config)
        _count_config["character_rows"] = all_rows
        _count_tags, _gender_assumed = _infer_character_count(
            _count_config, debug.get("gender_map"))
        seg_framing.extend(_count_tags)
        debug["count_inferred"] = True
        if _gender_assumed:
            debug["gender_assumed"] = True

    # v11：cn_map 的**角色行标签**（发色/瞳色/表情/动作，如「蓝发」-> blue hair、
    # 「在微笑」-> smile）也要进模板 positive，否则 LLM 路径的来源白名单里没有它们，
    # 会被误判为「无来源」删掉。没有角色行时单独成一行（用户描述的角色特征）。
    cn_character = list(cn_mapped.get("character") or ())
    # v14.2：**多角色时不注入** character 段。
    # 「遐蝶穿黑色长裙，流萤穿白色长裙」扫描出的 character 段同时含
    # black dress 和 white dress，全塞进第一行会让遐蝶一件黑一件白（自相矛盾）。
    # 多角色时服装归属交给各自的 outfit 覆盖检测（已按角色名切段），
    # 共享动作（walking）也交给 LLM 补。
    if cn_character and len(seg_characters) <= 1:
        if seg_characters:
            seg_characters[0].extend(cn_character)
        else:
            seg_characters.append(cn_character)

    positive = _join_segments(
        seg_quality, seg_framing, seg_characters, seg_scene)
    # v12：模板输出也要按库名规范（与 LLM 路径同一套规则）。
    # 触发词保护只覆盖**用户手填的 trigger**（契约 §5「不换拼写」）；
    # 数据层的 activation_words / outfit.prompt 仍要规范成库名。
    _name_info = {}
    positive, _rename_map = _normalize_tag_names(
        positive, _name_info,
        skip_keys={_norm_tag_key(tag) for tag in debug.get("user_triggers") or ()})
    if _name_info.get("renamed"):
        debug["names_normalized"] = list(_name_info["renamed"])
    if _name_info.get("name_unknown"):
        debug["name_unknown"] = list(_name_info["name_unknown"])
    negative = _dedupe_join(negative_parts)

    # v10：空输出的兜底（极端情况），**绝不输出空串**
    if not positive.strip():
        fallback = (positive_base or "").strip() or "1girl"
        positive = fallback
        debug["cjk_fallback"] = True

    # v15.1：**先**写来源表（此时还未转义），再转义，最后按最终写法同步。
    # 顺序反了会出现「debug 里是未转义写法、positive 里是转义写法」对不上。
    _tpl_sources = {
        _norm_tag_key(tag): "template"
        for row in _split_lines(positive) for tag in row
        if _norm_tag_key(tag)
    }
    debug["tag_sources"] = dict(_tpl_sources)
    # 与 LLM 路径同结构：debug["tagdb"]["tag_sources"]，
    # 否则 LLM 回退模板时「标签数 == tag_sources 条数」这条验收不成立
    if not isinstance(debug.get("tagdb"), dict):
        debug["tagdb"] = {
            "enabled": True, "available": True, "applied": False,
            "reason": "template_only", "tag_sources": dict(_tpl_sources),
        }

    # v15：括号**转义**（不是删掉全名）—— ComfyUI 的 escape_important 保证安全
    positive, _bracket_escaped = _escape_brackets(positive, debug)
    if _bracket_escaped:
        debug["bracket_escaped"] = list(_bracket_escaped)
        # 按最终（转义后）写法同步 debug key
        _final_tpl_sources = {
            _norm_tag_key(tag): "template"
            for row in _split_lines(positive) for tag in row
            if _norm_tag_key(tag)
        }
        debug["tag_sources"] = dict(_final_tpl_sources)
        if isinstance(debug.get("tagdb"), dict):
            debug["tagdb"]["tag_sources"] = dict(_final_tpl_sources)

    # 分段结构自检信息（便于前端与日志核对）
    debug["segment_lines"] = len(positive.splitlines())

    # 注入词去重（保留顺序）
    debug["injected_triggers"] = [
        token
        for token in _dedupe_join(debug["injected_triggers"]).split(", ")
        if token
    ]
    # 差集抑制来源去重（保留顺序，便于 B3 验收追溯）
    suppress_source = debug.get("negative_suppress_source") or {}
    debug["negative_suppress_source"] = {
        "outfit": _dedupe_tokens(suppress_source.get("outfit") or []),
        "character": _dedupe_tokens(suppress_source.get("character") or []),
    }
    debug["lora_stack"] = [list(item) for item in lora_stack]
    debug["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)

    return {
        "positive": positive,
        "negative": negative,
        "text": f"Positive:\n{positive}\n\nNegative:\n{negative}",
        "lora_stack": lora_stack,
        "debug": debug,
        "seed": actual_seed,
    }


def compose_plain(natural_language, ui, data=None):
    """契约 §12：无 GPU / 不启动 ComfyUI 可单测的**模板**组装纯函数。

    :param natural_language: 自然语言输入。
    :param ui: holder JSON（契约 §3），缺字段自动补默认值。
    :param data: 可选数据层 ``{"characters": [...], "styles": [...],
        "negative_base": str}``；``None`` 时优先用 ``store.py``，缺失则用空数据。
    :returns: ``{"positive","negative","text","lora_stack","debug","seed"}``
        （前 4 个键为契约 §12 要求，``text``/``seed`` 供节点复用）。
    """
    return _compose_core(natural_language, ui, data)


# --------------------------------------------------------------------------- LLM 调用（契约 §7）


_API_KEY_ENV = "ANIMA_LLM_API_KEY"


def _env_only_api_key(provider=None):
    """降级路径：只读环境变量，返回 ``(key, source)``。

    契约 §13.2：providers 不可用时**不自行读写** ``secrets.json``，只用环境变量。
    优先级：``ANIMA_LLM_API_KEY_<PROVIDER大写>`` > ``ANIMA_LLM_API_KEY``。
    """
    prov = _norm(provider)
    if prov and prov != "custom":
        name = _API_KEY_ENV + "_" + prov.upper().replace("-", "_").replace(" ", "_")
        value = _s(os.environ.get(name))
        if value:
            return value, "env"
    value = _s(os.environ.get(_API_KEY_ENV))
    if value:
        return value, "env"
    return "", "none"


def _resolve_api_key(provider=None):
    """契约 §13.2 / §13.3：密钥解析**唯一入口**是 ``providers.resolve_api_key()``。

    :returns: ``(key, source)``，``key`` 为空串表示未配置；
        ``source`` ∈ ``{"env","file","none"}``。

    这里**不得**自行叠加 env 探测层——那会屏蔽分供应商变量
    ``ANIMA_LLM_API_KEY_<PROVIDER大写>``（契约 §13.3 明确禁止）。
    """
    if _providers_mod is not None:
        function = getattr(_providers_mod, "resolve_api_key", None)
        if callable(function):
            try:
                result = function(_norm(provider))
            except Exception:  # noqa: BLE001 - providers 内部异常也要降级
                _log(
                    "providers.resolve_api_key 异常，降级为仅环境变量："
                    + traceback.format_exc(limit=2)
                )
                result = None

            if isinstance(result, tuple) and len(result) >= 2:
                key = _s(result[0])
                source = _s(result[1])
                if source not in ("env", "file", "none"):
                    source = "file" if key else "none"
                return key, source
            if isinstance(result, str):
                key = _s(result)
                return key, ("file" if key else "none")

    return _env_only_api_key(provider)



def _timeout_from(llm):
    runtime = llm.get("runtime") if isinstance(llm.get("runtime"), dict) else {}
    try:
        value = float(runtime.get("timeout", 120))
    except (TypeError, ValueError):
        value = 120.0
    return max(5.0, min(600.0, value))


def _provider_base_url(provider):
    """provider 的默认 base_url（契约 §14.1：单一真源在 providers.py）。"""
    key = _norm(provider)
    if not key:
        return ""
    return _s(provider_base_urls().get(key))


def _resolve_llm_target(llm):
    """解析出 ``(mode, base_url, model)``；本地模式按需拉起 llama-server。"""
    mode = _norm(llm.get("mode")) or "local"

    if mode == "safetensors":
        # v3：safetensors 不走 OpenAI 兼容端点（ensure_runtime 返回 base_url=""），
        # 由 llm_local_hf.chat() 直接在进程内推理，故这里只回 model 名。
        model = (
            _s(llm.get("model"))
            or _s(llm.get("model_file"))
            or Path(_s(llm.get("local_path")) or _s(llm.get("model_dir"))).name
        )
        return mode, "", model

    if mode == "local":
        local_path = _s(llm.get("local_path"))
        if not local_path:
            raise LLMError(
                ERR_LOCAL_MISSING,
                "本地模式未指定 gguf 模型文件，请在「加载LLM」节点选择 model_file",
            )
        if not Path(local_path).is_file():
            raise LLMError(
                ERR_LOCAL_MISSING, f"本地 gguf 文件不存在：{local_path}"
            )

        runtime = _load_sibling("runtime")
        if runtime is None or not hasattr(runtime, "ensure_runtime"):
            raise LLMError(
                ERR_RUNTIME_FAILED,
                "runtime.py 不可用，无法启动 llama-server；请改用云端模式",
            )
        try:
            result = runtime.ensure_runtime(llm)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - runtime 的异常统一转契约码
            code = getattr(exc, "code", None) or ERR_RUNTIME_FAILED
            raise LLMError(code, str(exc)) from exc

        if not isinstance(result, dict) or not _s(result.get("base_url")):
            raise LLMError(
                ERR_RUNTIME_FAILED, f"ensure_runtime 返回异常：{result!r}"
            )
        model = _s(result.get("model")) or Path(local_path).stem
        return mode, _s(result.get("base_url")), model

    base_url = _s(llm.get("base_url")) or _provider_base_url(llm.get("provider"))
    model = _s(llm.get("model")) or _s(llm.get("cloud_model"))
    # 契约 §8：这两个是**本地配置校验**错误，必须与 llm_http_*（真实 HTTP 故障）区分，
    # 否则前端会把「没填配置」误报成「接口挂了」。
    if not base_url:
        raise LLMError(
            ERR_CLOUD_BASE_URL_EMPTY,
            "云端 base_url 为空，请在「加载LLM」节点填写供应商或 base_url",
        )
    if not model:
        raise LLMError(ERR_CLOUD_MODEL_EMPTY, "云端模型代号为空，请填写 cloud_model")
    return "cloud", base_url, model


def _build_selected(config):
    return {
        "character_rows": config["character_rows"],
        "style_rows": config["style_rows"],
    }


# --------------------------------------------------------------------------- Skill（契约 §14.2）

_SKILL_STORE_MOD = None
_SKILL_STORE_TRIED = False
_SKILL_CHECK_MOD = None
_SKILL_CHECK_TRIED = False


def _load_skill_store():
    """惰性加载 ``skill_store.py``（并行开发，可能缺失）。"""
    global _SKILL_STORE_MOD, _SKILL_STORE_TRIED
    if _SKILL_STORE_TRIED:
        return _SKILL_STORE_MOD
    _SKILL_STORE_TRIED = True
    _SKILL_STORE_MOD = _load_sibling("skill_store")
    return _SKILL_STORE_MOD


def _load_skill_check():
    """惰性加载 ``skill_check.py``（并行开发，可能缺失）。"""
    global _SKILL_CHECK_MOD, _SKILL_CHECK_TRIED
    if _SKILL_CHECK_TRIED:
        return _SKILL_CHECK_MOD
    _SKILL_CHECK_TRIED = True
    _SKILL_CHECK_MOD = _load_sibling("skill_check")
    return _SKILL_CHECK_MOD


def _builtin_skill(skill_id=None):
    """内置最小 skill：与 skill 文件**相同 JSON 契约**（契约 §14.2）。

    仅在 ``skill_store`` 不可用/加载失败时使用，保证注入链路仍可运行。
    """
    return {
        "id": _s(skill_id) or DEFAULT_SKILL_ID,
        "display_name": "内置最小 Skill",
        "target_model": "anima",
        "version": "builtin",
        "system_prompt": FALLBACK_SYSTEM_PROMPT,
        "tag_order": [
            "quality",
            "subject",
            "character",
            "outfit",
            "pose",
            "scene",
            "camera",
            "lighting",
            "style",
        ],
        "quality_tags": [],
        "negative_base": "",
        "max_tags": 80,
        "forbidden_phrases": [],
        "checker": "anima_v1",
        "examples": [],
        "analysis_hint": "生成后给用户看的简短中文说明",
        "_builtin": True,
    }


def _load_skill(skill_id=None):
    """按 id 加载 skill（契约 §14.2）；失败返回内置最小 skill。"""
    target = _s(skill_id) or DEFAULT_SKILL_ID
    module = _load_skill_store()
    if module is not None:
        loader = getattr(module, "load_skill", None)
        if callable(loader):
            for argument in (target,):
                try:
                    skill = loader(argument)
                except Exception:  # noqa: BLE001
                    _log(f"skill_store.load_skill({target!r}) 异常，改用内置最小 skill")
                    break
                if isinstance(skill, dict) and skill:
                    return skill
    return _builtin_skill(target)


def _escape_single_tag(tag):
    """单个标签的括号转义（供 required_tokens 与最终 positive 对齐）。"""
    text = _s(tag)
    if not text or "(" not in text and ")" not in text:
        return text
    if '\\(' in text or '\\)' in text:
        return text
    return text.replace("(", '\\(').replace(")", '\\)')


def _run_skill_check(positive, negative, required_tokens, forbidden_tokens, skill):
    """调用 ``skill_check.check_anima``（契约 §14.2）。

    模块/函数缺失时返回 ``{"ok": True, "issues": [], "skipped": True}``，
    绝不让校验器缺失阻断出图。
    """
    empty = {"ok": True, "issues": [], "stats": {}, "skipped": True}
    module = _load_skill_check()
    if module is None:
        return empty
    function = getattr(module, "check_anima", None)
    if not callable(function):
        return empty

    required = list(required_tokens or [])
    forbidden = list(forbidden_tokens or [])
    variants = (
        lambda: function(positive, negative, required, forbidden, skill=skill),
        lambda: function(positive, negative, required, forbidden, skill),
        lambda: function(positive, negative, required, forbidden),
    )
    for variant in variants:
        try:
            result = variant()
        except TypeError:
            continue
        except Exception:  # noqa: BLE001
            _log("skill_check.check_anima 异常，按跳过处理")
            return empty
        if isinstance(result, dict):
            stats = result.get("stats")
            return {
                "ok": bool(result.get("ok", True)),
                "issues": _dicts(result.get("issues")),
                "stats": stats if isinstance(stats, dict) else {},
                "skipped": False,
            }
    return empty


def _run_quality(positive, negative, skill):
    """调用 ``skill_check.score_prompt``（契约 §14.2）；缺失时 skipped。"""
    empty = {"score": None, "missing": [], "warnings": [], "skipped": True}
    module = _load_skill_check()
    if module is None:
        return empty
    function = getattr(module, "score_prompt", None)
    if not callable(function):
        return empty

    variants = (
        lambda: function(positive, negative, skill),
        lambda: function(positive, negative),
    )
    for variant in variants:
        try:
            result = variant()
        except TypeError:
            continue
        except Exception:  # noqa: BLE001
            _log("skill_check.score_prompt 异常，按跳过处理")
            return empty
        if isinstance(result, dict):
            return {
                "score": result.get("score"),
                "missing": _as_list(result.get("missing")),
                "warnings": _as_list(result.get("warnings")),
                "skipped": False,
            }
    return empty


def _extract_llm_result(parsed, core):
    """从 LLM JSON 结果取 ``(positive, negative, analysis)``；不合法抛 ``LLMError``。"""
    if not isinstance(parsed, dict):
        raise LLMError("llm_bad_json", "LLM 返回不是 JSON 对象")

    positive = _s(parsed.get("positive"))
    if not positive:
        raise LLMError("llm_empty_positive", str(parsed)[:200])

    negative = parsed.get("negative")
    if not isinstance(negative, str) or not negative.strip():
        negative = core["negative"]  # 模型未给负向词时保留模板负向
    else:
        negative = negative.strip()

    analysis = parsed.get("analysis")
    analysis = analysis.strip() if isinstance(analysis, str) else ""
    return positive, negative, analysis


def _option_bool(value, default):
    """把配置开关归一成 bool；无法判断时回落 ``default``。

    注意 ``bool("false")`` 在 Python 里是 ``True``，字符串必须单独判定。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "开", "开启"):
            return True
        if text in ("0", "false", "no", "off", "关", "关闭"):
            return False
    return default


#: ``prompt_opt`` 的默认值：**全开**（与 store.DEFAULT_PROMPT_OPT 保持一致）。
PROMPT_OPT_DEFAULTS = {
    "dedupe_skill": True,
    "static_selected": True,
    "retry_multiturn": True,
    # v7：标签库 agent 反馈。开启后，当 LLM 草稿里的问题词占比超过阈值时，
    # 把「具体问题词 + 库中真实合法的候选」追加成一轮反馈，让 LLM 自己修；
    # 关闭时完全回到旧行为（静默归一，不额外调用 LLM）。
    "tagdb_agent": True,
}


def _prompt_options():
    """读取「提示词优化开关」，**任何异常都回落默认全开**。

    硬要求（lead）：store 不可用 / 读失败 / 键缺失 / 类型错 —— 一律走默认全开。
    **绝不能因为读配置失败就退化成「skill 双份注入」**（那会白烧约 45% token）。
    """
    module = _store_mod
    if module is None:
        return dict(PROMPT_OPT_DEFAULTS)
    options = None
    getter = getattr(module, "prompt_options", None)
    if callable(getter):
        try:
            options = getter()
        except Exception as exc:  # noqa: BLE001 - 读不到就用默认，绝不外抛
            _log(f"prompt_options 读取失败，回落默认全开：{type(exc).__name__}: {exc}")
            options = None
    if not isinstance(options, dict):
        # 兼容旧版 store（没有 prompt_options）：手工从 settings 里取
        loader = getattr(module, "load_settings", None)
        if callable(loader):
            try:
                settings = loader()
                if isinstance(settings, dict):
                    options = settings.get("prompt_opt")
                    if isinstance(options, dict):
                        options = dict(options)
            except Exception:  # noqa: BLE001
                options = None
    if not isinstance(options, dict):
        return dict(PROMPT_OPT_DEFAULTS)
    return {
        key: _option_bool(options.get(key, default), default)
        for key, default in PROMPT_OPT_DEFAULTS.items()
    }


def _build_messages_for(config, core, natural_language, view, skill, forbidden_tokens=()):
    """统一构造 LLM messages（首次调用与重试共用，避免两套逻辑漂移）。

    v4 P0 —— 修复 system 里 skill 双份注入。

    改动前是 ``system_prompt = config["system_prompt"] or _default_system_prompt()``，
    而 ``_default_system_prompt()`` -> ``store.default_system_prompt()`` 返回的
    **正是默认 skill 的 system_prompt 原文**（实测逐字节相同，4525 字符）。
    于是 ``llm_client.build_messages`` 把 system_prompt 与 skill 各渲染一遍再
    ``"\\n\\n".join``，同一份规则书在 system 里出现两次 —— 实测默认配置下
    system 达 10857 字符，其中 4525 字符纯属重复，且**每次调用都烧**。

    修复后的判据（优先级从高到低）：

    1. 用户在 ui 里显式填了 ``system_prompt`` -> 用它（用户意志优先）；
    2. 否则若 ``skill`` 自带非空 ``system_prompt`` -> 传空串，由 ``skill_block``
       单独提供（内容不会丢，因为 ``store.default_system_prompt()`` 返回的就是它）；
    3. 否则（``skill`` 缺失或 skill 正文为空）-> 仍用 ``_default_system_prompt()``
       兜底，**保证契约 §7「skill=None 时与 v1 逐字节一致」不被破坏**。

    安全性：``store.default_system_prompt()`` 只读 skill store、不读 settings 里
    的用户自定义值（自定义值走 ``config["system_prompt"]``），所以第 2 条不会
    吞掉任何用户设置。
    """
    if _llm_client is None:
        raise LLMError(ERR_NOT_CONNECTED, "llm_client.py 不可用")

    user_system_prompt = config["system_prompt"]
    skill_has_prompt = (
        isinstance(skill, dict)
        and isinstance(skill.get("system_prompt"), str)
        and bool(skill.get("system_prompt").strip())
    )
    # v5：去双份注入可由 ``settings.prompt_opt.dedupe_skill`` 关闭（仅用于对照排查）。
    # 关闭时走改动前的老逻辑，恢复 skill 在 system 里出现两次的行为。
    prompt_options = _prompt_options()
    if user_system_prompt:
        system_prompt = user_system_prompt
    elif skill_has_prompt and prompt_options["dedupe_skill"]:
        system_prompt = ""
    else:
        system_prompt = _default_system_prompt()

    build = getattr(_llm_client, "build_messages", None)
    if not callable(build):
        raise LLMError(ERR_NOT_CONNECTED, "llm_client.build_messages 缺失")

    # 参数按「新到旧」逐级兼容：旧版 llm_client 可能不认识较新的可选参数
    attempts = (
        {"skill": skill, "forbidden_tokens": forbidden_tokens,
         "static_selected": prompt_options["static_selected"],
         "dedupe_skill": prompt_options["dedupe_skill"]},
        {"skill": skill, "forbidden_tokens": forbidden_tokens,
         "static_selected": prompt_options["static_selected"]},
        {"skill": skill, "forbidden_tokens": forbidden_tokens},
        {"skill": skill},
    )
    last_error = None
    for kwargs in attempts:
        try:
            return build(
                system_prompt,
                natural_language,
                core["positive"],
                core["negative"],
                view.catalog(),
                _build_selected(config),
                **kwargs
            )
        except TypeError as exc:
            last_error = exc
    raise LLMError(ERR_NOT_CONNECTED, f"llm_client.build_messages 参数不兼容：{last_error}")


def _tagdb_options(ui):
    """读取标签库开关（缺字段用默认；绝不因缺字段改变行为）。

    支持两种写法（holder 里任选其一；**子对象优先于顶层**，因为更具体）：

    * ``{"tagdb_enabled": false, "tagdb_min_confidence": 0.9}``
    * ``{"tagdb": {"enabled": false, "min_confidence": 0.9}}``

    默认 **enabled=True**：DB 不存在时 ``tagdb.available()`` 为 False，
    整条校验自动跳过，等价于关闭，所以默认开启是安全的。
    """
    source = ui if isinstance(ui, dict) else {}
    section = source.get("tagdb")
    section = section if isinstance(section, dict) else {}

    def pick(top_key, sub_key, default):
        for holder, key in ((section, sub_key), (source, top_key)):
            if key in holder:
                return holder.get(key)
        return default

    enabled = pick("tagdb_enabled", "enabled", True)
    min_confidence = pick("tagdb_min_confidence", "min_confidence",
                          _tagdb_default_confidence())
    try:
        min_confidence = float(min_confidence)
    except (TypeError, ValueError):
        min_confidence = _tagdb_default_confidence()
    if min_confidence < 0.0 or min_confidence > 1.0:
        min_confidence = _tagdb_default_confidence()
    return {"enabled": bool(enabled), "min_confidence": min_confidence}


def _tagdb_default_confidence():
    """取 tagdb 模块的默认替换阈值；模块不可用时用 0.85 兜底。"""
    value = getattr(_tagdb_mod, "DEFAULT_MIN_CONFIDENCE", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.85


def _tagdb_agent_min_ratio():
    """取 tagdb 模块的 agent 触发阈值；模块不可用时用 10% 兜底。"""
    value = getattr(_tagdb_mod, "AGENT_MIN_RATIO", None)
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return 0.10
    return ratio if 0.0 < ratio <= 1.0 else 0.10


def _tagdb_whitelist(core, skill):
    """本次「插件自己拼进提示词的词」+ skill 质量词：一律免校验。

    为什么需要：LoRA 触发词、角色/服装 activation_words、``@画师`` 标签、
    ``masterpiece / best quality`` 等质量词都不在 danbooru 里，
    不豁免会把它们误判成 unknown 甚至改写掉。
    """
    words = []
    debug = core.get("debug") if isinstance(core, dict) else {}
    debug = debug if isinstance(debug, dict) else {}

    injected = debug.get("injected_triggers")
    if isinstance(injected, (list, tuple)):
        words.extend(str(item) for item in injected if item)
    suppress_source = debug.get("negative_suppress_source")
    if isinstance(suppress_source, dict):
        for value in suppress_source.values():
            if isinstance(value, (list, tuple)):
                words.extend(str(item) for item in value if item)
    # 模板正向串全部来自数据层（角色/服装/风格/LoRA），整体免校验
    if isinstance(core, dict):
        words.extend(_split_tokens(core.get("positive")))
    if isinstance(skill, dict):
        quality = skill.get("quality_tags")
        if isinstance(quality, (list, tuple)):
            words.extend(str(item) for item in quality if item)
    return [word for word in words if word]


def _apply_tagdb(positive, ui, core, skill):
    """用 danbooru 标签库校验并归一正向提示词，返回 ``(text, debug)``。

    **只做校验与归一，不做生成**：它保证标签真实存在、拼写正确、用标准名，
    但「该画什么」仍由 LLM 决定。任何异常都降级为「原文不动」，绝不影响出图。
    """
    debug = {"enabled": True, "available": False, "applied": False}
    try:
        options = _tagdb_options(ui)
        if not options["enabled"]:
            return positive, {"enabled": False, "available": False,
                              "applied": False, "reason": "disabled"}
        if _tagdb_mod is None:
            return positive, {"enabled": True, "available": False,
                              "applied": False, "reason": "module_missing"}
        available_fn = getattr(_tagdb_mod, "available", None)
        if not callable(available_fn) or not available_fn():
            reason = "db_unavailable"
            status_fn = getattr(_tagdb_mod, "status", None)
            if callable(status_fn):
                try:
                    info = status_fn()
                    if isinstance(info, dict) and info.get("error"):
                        reason = str(info["error"])
                except Exception:  # noqa: BLE001
                    pass
            return positive, {"enabled": True, "available": False,
                              "applied": False, "reason": reason}

        normalize = getattr(_tagdb_mod, "normalize_text", None)
        if not callable(normalize):
            return positive, {"enabled": True, "available": False,
                              "applied": False, "reason": "normalize_text_missing"}

        summary = normalize(
            positive,
            whitelist=_tagdb_whitelist(core, skill),
            min_conf=options["min_confidence"],
        )
        if not isinstance(summary, dict):
            return positive, {"enabled": True, "available": True,
                              "applied": False, "reason": "bad_result"}
        if summary.get("skipped"):
            return positive, {"enabled": True, "available": True,
                              "applied": False,
                              "reason": _s(summary.get("reason")) or "skipped"}

        text = summary.get("text")
        text = text if isinstance(text, str) and text.strip() else positive
        return text, {
            "enabled": True,
            "available": True,
            "applied": text != positive,
            "min_confidence": options["min_confidence"],
            "corrected": summary.get("corrected") or [],
            "suggestions": summary.get("suggestions") or [],
            "unknown": summary.get("unknown") or [],
            "stats": summary.get("stats") or {},
            "unknown_ratio": summary.get("unknown_ratio", 0.0),
        }
    except Exception as exc:  # noqa: BLE001 - 标签库只是增强功能，绝不拖垮出图
        _log(f"tagdb 校验异常（已跳过，提示词保持原样）：{type(exc).__name__}: {exc}")
        debug["reason"] = "internal_error"
        return positive, debug


def _build_tagdb_agent_messages(messages, assistant_text, feedback):
    """标签库 agent 第 2 轮的 messages：**只追加、不改写前缀**。

    结构与 :func:`_build_retry_messages` 完全一致::

        [system, user, assistant(上一轮输出), user(标签库反馈)]

    所以 ``[system, user]`` 这个前缀与首次请求逐字节相同，prompt cache 仍然
    命中（这正是 ``prompt_opt.retry_multiturn`` 那套设计，不另起一套）。

    :returns: 新 messages；入参非法或反馈为空时**原样返回**入参
    """
    if not isinstance(messages, list) or not messages:
        return messages
    if not isinstance(feedback, str) or not feedback.strip():
        return messages
    appended = [dict(message) for message in messages]
    text = assistant_text if isinstance(assistant_text, str) else ""
    if text.strip():
        appended.append({"role": "assistant", "content": text})
    appended.append({"role": "user", "content": feedback})
    return appended


def _agent_token_keys(bucket):
    """把 unknown / suggestions 列表压成小写 key 集合。"""
    keys = set()
    for item in _dicts(bucket):
        key = _s(item.get("input")).strip().lower()
        if key:
            keys.add(key)
    return keys


def _agent_problem_total(tagdb_debug):
    """一个问题清单的规模：``unknown`` + ``suggestions`` 的条数。"""
    if not isinstance(tagdb_debug, dict):
        return 0
    return len(_dicts(tagdb_debug.get("unknown"))) \
        + len(_dicts(tagdb_debug.get("suggestions")))


def _agent_compare(plan, tagdb_after):
    """对比「第 1 轮的问题词」与「第 2 轮的结果」，统计 LLM 的采纳情况。

    ``resolved`` 只表示**原词消失了**，不代表它一定变成了合法标签 —— LLM 很
    可能把 ``mechanical prosthetic arm`` 换成同样不存在的 ``cybernetic arm``
    （实测确实发生过）。所以同时给出 ``new_problems`` 和 ``unknown_delta``，
    看净效果才有意义。
    """
    summary = {
        "resolved": [], "still": [], "new_problems": [],
        "unknown_before": 0, "unknown_after": 0, "unknown_delta": 0,
    }
    try:
        problems = _dicts(plan.get("problems"))
        before = [str(p.get("token") or "") for p in problems]
        before = [token for token in before if token.strip()]
        summary["unknown_before"] = sum(
            1 for item in problems if _s(item.get("kind")) == "unknown")
        after = tagdb_after if isinstance(tagdb_after, dict) else {}
        after_unknown = _dicts(after.get("unknown"))
        summary["unknown_after"] = len(after_unknown)
        summary["unknown_delta"] = summary["unknown_after"] - summary["unknown_before"]
        after_keys = _agent_token_keys(after.get("unknown")) \
            | _agent_token_keys(after.get("suggestions"))
        for token in before:
            (summary["still"] if token.strip().lower() in after_keys
             else summary["resolved"]).append(token)
        before_keys = set(token.strip().lower() for token in before)
        summary["new_problems"] = sorted(
            key for key in after_keys if key not in before_keys)
        return summary
    except Exception as exc:  # noqa: BLE001 - 统计失败只影响 debug
        _log(f"agent 采纳统计失败（不影响结果）：{type(exc).__name__}: {exc}")
        return summary


# ---------------------------------------------------------------------------
# v11：标签来源校验（禁止编造）+ 确定性标签强制注入 + 角色数量补齐
# ---------------------------------------------------------------------------
#: 受控维度：用户**没提到**该维度时，positive 里不允许出现该维度的标签。
#:
#: ``user_terms`` 用于判断「用户是否提到这个维度」（中英双语，子串匹配）；
#: ``tag_words`` 用于把候选标签归到该维度（子串匹配，大小写/下划线不敏感）。
#: 顺序重要：先匹配更具体的维度（gaze 先于 action，expression 先于 action），
#: 避免 ``looking back`` 被 action 抢走。
_CONTROLLED_DIMENSIONS = (
    ("quality", (
        "best quality", "masterpiece", "highres", "high definition",
        "very aesthetic", "absurdres", "highly detailed",
    ), ""),
    ("count", (
        "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
        "multiple girls", "multiple boys", "solo",
    ), ""),
    ("shot", (
        "full body", "upper body", "cowboy shot", "close-up", "close up",
        "wide shot", "extreme close-up", "portrait", "from above",
        "from below", "from side", "from behind", "from front", "profile",
        "dutch angle", "aerial", "low angle", "high angle",
    ), "全身|半身|上半身|特写|远景|广角|中景|大头|全图|机位|俯视|仰视"),
    ("gaze", (
        "looking at viewer", "looking back", "looking away", "looking down",
        "looking up", "eye contact",
    ), "看镜头|看着镜头|望向镜头|回头|看镜头|视线"),
    ("expression", (
        "smile", "grin", "blush", "angry", "crying", "tears", "surprised",
        "closed eyes", "open mouth", "expressionless", "smug", "pout",
        "sad", "happy", "embarrassed",
    ), "微笑|笑|害羞|脸红|生气|哭|流泪|惊讶|闭眼|张嘴|表情|沮丧|开心"),
    ("action", (
        "standing", "sitting", "walking", "running", "lying", "squatting",
        "kneeling", "jumping", "waving", "arm up", "crossed arms",
        "hands on hips", "leaning", "dancing", "holding", "hugging",
    ), "站|坐|走|跑|躺|蹲|跪|跳|挥手|举手|回头|抱|叉腰|跳舞|姿势|动作"),
    ("lighting", (
        "lighting", "backlighting", "sidelighting", "soft lighting",
        "dim lighting", "golden hour", "sunlight", "moonlight", "spotlight",
        "chiaroscuro", "lens flare", "bokeh", "depth of field",
        "underlighting", "camera flash", "rim light", "volumetric lighting",
    ), "光|照明|灯光|逆光|柔光|光照|氛围光|打光"),
    ("scene", (
        "street", "city", "road", "sidewalk", "pavement", "building",
        "skyline", "neon", "alley", "park", "classroom", "school", "beach",
        "ocean", "sea", "forest", "tree", "flower", "field", "garden",
        "room", "bedroom", "indoors", "outdoors", "shrine", "ruins",
        "starry sky", "cloud", "sunset", "sunrise", "night", "day", "rain",
        "snow", "fog", "puddle", "wet", "reflection", "window", "wall",
        "floor", "sky", "water", "pool", "cafe", "library", "temple",
        "background", "scenery", "landscape", "factory",
    ), "街道|城市|校园|学校|教室|海边|海滩|沙滩|森林|树林|公园|房子|建筑|"
       "室内|户外|室外|卧室|咖啡|图书馆|神社|寺庙|废墟|花田|花海|星空|天空|"
       "夕阳|日落|日出|黄昏|夜|雨|雪|雾|水边|泳池|工厂|街|背景|风景"),
    ("hair", (
        "hair", "twintails", "ponytail", "ahoge", "braid", "bangs",
        "bob cut", "crew cut", "side ponytail",
    ), "发|头发|刘海|辫|马尾|呆毛"),
    ("eyes", (
        "eyes", "heterochromia", "eyelashes", "eyebrows",
    ), "眼|瞳|眉|睫毛"),
    ("outfit", (
        "dress", "skirt", "shirt", "blouse", "coat", "jacket", "suit",
        "uniform", "swimsuit", "bikini", "kimono", "armor", "boots",
        "shoes", "gloves", "thighhighs", "socks", "stockings", "hat",
        "ribbon", "necktie", "scarf", "cape", "hood", "vest", "pants",
        "shorts", "clothes", "outfit", "costume", "jewelry", "earrings",
        "necklace", "hairband", "hairpin", "crown", "footwear", "headwear",
        "collar", "sleeves", "apron", "leotard",
    ), "穿|装|衣|裙|裤|鞋|袜|帽|手套|西装|礼服|泳装|制服|和服|盔甲|战袍|"
       "旗袍|汉服|睡衣|外套|大衣|配饰|首饰|耳环|发饰|冠"),
)


def _norm_tag_key(tag):
    """标签的**比较 key**：小写、下划线->空格、剥权重、折叠空白。

    ``blue_hair`` 与 ``blue hair`` 视为同一个 key；``(smile:1.2)`` 与 ``smile``
    也视为同一个 —— 归一化只用于比较，不改变最终输出文本。
    """
    try:
        text = str(tag if tag is not None else "").strip().lower()
        if not text:
            return ""
        match = re.match(r"^\((.*):[0-9.]+\s*\)$", text)
        if match:
            text = match.group(1)
        text = text.replace("_", " ").replace("\\", "")
        return " ".join(text.split())
    except Exception:  # noqa: BLE001
        return ""


#: 允许「用户提到过该维度 + 共享词元」就放行的维度（v13 长句适配）。
#:
#: **故意不含 hair / eyes**：这两个是角色固有属性，一旦维度级放行，
#: 「一个蓝发少女」就会冒出 long_hair / twintails / colored_inner_hair ——
#: 这正是 lead 的 B 用例在守的底线。
#: 注意：**故意不含 outfit**（v13.1，lead 方案 C）。
#:
#: outfit 是编造重灾区 —— LLM 会把「白裙」细化成 `white_dress, white_skirt,
#: pleated_skirt`，连衣裙和半身裙同时出现是硬伤。而「共享词元」这条规则
#: 区分不了 ``wet_street``（合理推断）和 ``pleated_skirt``（纯编造）：
#: 两者结构一样（共享名词 + 一个修饰词）。
#: 所以服装**只认 cn_tags 映射 + 角色库白名单**，LLM 多加的 outfit 细节一律删。
_ELABORATABLE_DIMENSIONS = frozenset((
    "scene", "action", "expression", "lighting", "shot",
    "count", "gaze", "quality",
))


def _tag_dimension(tag):
    """把标签归到受控维度；归不进去返回 ``""``（视为自由标签）。

    特判（v11）：含**独立 hair 词元**的标签先归 ``hair`` ——
    否则 ``hair flower`` 会被 scene 词表里的 ``flower`` 抢走，
    ``hair bow`` 会被 outfit 的 ``bow`` 抢走。``black hairband`` 这种
    ``hair`` 只是一个词根的，不触发特判，仍走 normal 匹配。
    """
    key = _norm_tag_key(tag)
    if not key:
        return ""
    padded = " " + key + " "
    if key == "hair" or key.startswith("hair ") or key.endswith(" hair") \
            or " hair " in padded:
        return "hair"
    for name, tag_words, _user_terms in _CONTROLLED_DIMENSIONS:
        for word in tag_words:
            if key == word or key.endswith(" " + word) or (" " + word + " ") in (" " + key + " "):
                return name
    return ""


def _mentioned_dimensions(natural_language, cn_mapped, ui):
    """用户**提到过**哪些受控维度（返回 set）。

    三条来源任一命中即算提到：
      1. 用户原文里的中文/英文关键词（``_CONTROLLED_DIMENSIONS`` 的 user_terms）
      2. 中文确定性映射产出了该维度的标签
      3. UI 选了角色/风格（hair/eyes/outfit 维度由库提供，天然算提到）
    """
    mentioned = set()
    try:
        text = str(natural_language or "").lower()
        for name, _tag_words, user_terms in _CONTROLLED_DIMENSIONS:
            if text and user_terms:
                for term in user_terms.split("|"):
                    term = term.strip().lower()
                    if term and term in text:
                        mentioned.add(name)
                        break
        # 中文映射直接产出的维度
        dimension_of = {
            "quality": "quality",
            "framing": None,      # framing 里可能混视距/人数，单独判断
            "character": None,
            "scene": "scene",
        }
        for segment in ("quality", "scene"):
            for tag in (cn_mapped.get(segment) or ()):
                dim = _tag_dimension(tag)
                if dim:
                    mentioned.add(dim)
        for segment in ("framing", "character"):
            for tag in (cn_mapped.get(segment) or ()):
                dim = _tag_dimension(tag)
                if dim:
                    mentioned.add(dim)
        # 选了角色/风格 -> 角色固有特征维度算有来源
        config = _normalize_ui(ui)
        has_character = any(_s(row.get("trigger")) for row in config["character_rows"])
        has_style = bool(config["style_rows"])
        if has_character:
            mentioned.update(("hair", "eyes", "outfit"))
        if has_style:
            mentioned.add("scene")
        return mentioned
    except Exception:  # noqa: BLE001
        return mentioned


def _template_allowlist(core):
    """模板 positive 的全部标签 -> ``{key: 原始标签}``（白名单基线）。"""
    allow = {}
    try:
        for row in _split_lines((core or {}).get("positive") or ""):
            for tag in row:
                key = _norm_tag_key(tag)
                if key and key not in allow:
                    allow[key] = tag
        return allow
    except Exception:  # noqa: BLE001
        return allow


# ---------------------------------------------------------------------------
# v12：标签写法以 danbooru 标签库为准
# ---------------------------------------------------------------------------
#: 与库名比较时忽略大小写
_CANONICAL_WEIGHT_RE = re.compile(r"^\s*\((.*):([0-9.]+)\s*\)\s*$")


def _split_weight(tag):
    """``(long hair:1.2)`` -> ``("long hair", ":1.2)")``；无权重时后缀为空。"""
    text = str(tag if tag is not None else "").strip()
    match = _CANONICAL_WEIGHT_RE.match(text)
    if match:
        return match.group(1).strip(), ":%s)" % match.group(2)
    return text, ""


#: 质量类标签**完全不查库**（lead 核实）：
#:
#:   ==================  ==========================  ==========================
#:   词                    库里的情况                    结论
#:   ==================  ==========================  ==========================
#:   ``best_quality``    cat=5(meta) **posts=0**     空壳，当「库里没有」
#:   ``masterpiece``     不存在                      保持原样
#:   ``highres``         posts=8033637               本来就是库名，不变
#:   ``absurdres``       posts=2939616               本来就是库名，不变
#:   ``high definition`` 不存在                      保持原样
#:   ==================  ==========================  ==========================
#:
#: 这类词是**模型侧的质量提示词**，不是 danbooru 概念标签；``best_quality`` 虽然
#: 在库里有记录但 posts=0（danbooru 上从未被实际使用），拿去出图毫无意义。
#: 所以整类走白名单，只做「原样保留」或「用中文映射给的原样」。
_QUALITY_KEEP_AS_IS = frozenset((
    "masterpiece", "best quality", "best_quality", "good quality",
    "normal quality", "worst quality", "low quality", "lowres",
    "highres", "absurdres", "very aesthetic", "high definition",
    "highly detailed", "high aesthetic",
))

#: 只有这两种匹配方式可信：精确命中、或库内明确登记的别名。
#: ``fts_lowfreq`` / ``prefix_*`` 之类的模糊匹配会把语义完全不同的标签换进来。
_TRUSTED_LOOKUP_VIA = frozenset(("exact", "alias"))

#: 库名要求的最低使用量。``posts < 100`` 的标签（尤其 posts=0 的空壳）
#: 等同于「库里没有」，一律保持原样，不拿它去替换用户/映射给的写法。
_CANONICAL_MIN_POSTS = 100


def _lookup_canonical(tag):
    """查库拿标准名；查不到 / 不可信 / 使用量过低时返回空串（调用方保持原样）。"""
    if _tagdb_mod is None:
        return ""
    lookup = getattr(_tagdb_mod, "lookup", None)
    if not callable(lookup):
        return ""
    try:
        probe = _norm_tag_key(tag)
        # 质量类标签整类不查库（原样保留）
        if probe in _QUALITY_KEEP_AS_IS:
            return ""
        result = lookup(tag)
        if not isinstance(result, dict) or not result.get("hit"):
            return ""
        if result.get("via") not in _TRUSTED_LOOKUP_VIA:
            return ""
        # posts=0 的空壳标签没有实际使用价值，视同未命中
        try:
            posts = int(result.get("post_count") or 0)
        except Exception:  # noqa: BLE001
            posts = 0
        if posts < _CANONICAL_MIN_POSTS:
            return ""
        return _s(result.get("name"))
    except Exception:  # noqa: BLE001
        return ""


def _normalize_tag_names(positive, info=None, skip_keys=None):
    """把 positive 里每个标签规范成库里的标准名。

    :param positive: 待规范的多行 positive。
    :param info: 可选的 payload，写 ``renamed`` / ``name_unknown`` / ``skipped``。
    :param skip_keys: 归一 key 集合，命中的标签**原样保留**。
        用于保护触发词（契约 §5：角色/服装/风格触发词不增不减不改写）——
        实测 ``castorice`` 会被规范成 ``castorice_(honkai:_star_rail)``，属改写。
    :returns: ``(new_positive, rename)``，``rename`` 是
        ``{归一 key: 库名}``，供调用方把 debug 里的 key 一起改名。
    """
    payload = info if isinstance(info, dict) else {}
    payload.setdefault("renamed", [])
    payload.setdefault("name_unknown", [])
    payload.setdefault("skipped", [])
    protected = {key for key in (skip_keys or ()) if key}
    rename = {}
    try:
        rows = _split_lines(positive)
        if not rows:
            return positive, rename
        new_rows = []
        for row in rows:
            new_row = []
            for tag in row:
                core, suffix = _split_weight(tag)
                if not core:
                    continue
                # 画师标签（@ 前缀）不是 danbooru 标签，不查库
                if core.startswith("@"):
                    new_row.append(tag)
                    continue
                # 触发词保护：原样保留，不查库
                if _norm_tag_key(core) in protected:
                    payload["skipped"].append(core)
                    new_row.append(tag)
                    continue
                canonical = _lookup_canonical(core)
                if not canonical:
                    # 质量类词统一成可读的**空格**形式：库里的 ``best_quality``
                    # posts=0 是空壳，不能作为输出写法（lead 核实）。
                    # 它们仍然记进 name_unknown —— 库里确实没有可用的对应条目。
                    probe_key = _norm_tag_key(core)
                    if probe_key in _QUALITY_KEEP_AS_IS:
                        canonical = probe_key
                        payload["name_unknown"].append(core)
                    else:
                        payload["name_unknown"].append(core)
                        new_row.append(tag)
                        continue
                new_tag = canonical + suffix
                if new_tag != tag:
                    payload["renamed"].append({"from": tag, "to": new_tag})
                    rename[_norm_tag_key(core)] = canonical
                new_row.append(new_tag)
            if new_row:
                new_rows.append(new_row)
        if not new_rows:
            return positive, rename
        return _join_lines(new_rows), rename
    except Exception as exc:  # noqa: BLE001 - 规范化只是写法增强，绝不拖垮出图
        _log(f"_normalize_tag_names 异常（原样返回）：{type(exc).__name__}: {exc}")
        return positive, rename


def _resync_debug_keys(tag_sources, dropped, cn_enforced, positive):
    """按**最终 positive 的写法**重建 debug 里的标签 key（v12）。

    ``tag_sources`` 的 key 是 ``_norm_tag_key``（空格形式），而 positive 定稿后
    可能是库名（``blue_hair``）。若不做这一步，用户排查时会出现
    「positive 里是 ``blue_hair``、tag_sources 里是 ``blue hair``」对不上的情况。
    """
    try:
        final_form = {}
        for row in _split_lines(positive):
            for tag in row:
                core, _suffix = _split_weight(tag)
                key = _norm_tag_key(core)
                if key and key not in final_form:
                    final_form[key] = core
        if not final_form:
            return tag_sources, dropped, cn_enforced
        new_sources = {}
        for key, value in (tag_sources or {}).items():
            new_sources[final_form.get(key, key)] = value
        new_dropped = []
        for item in (dropped or []):
            if not isinstance(item, dict):
                new_dropped.append(item)
                continue
            tag = item.get("tag")
            new_dropped.append(
                dict(item, tag=final_form.get(_norm_tag_key(tag), tag)))
        new_enforced = [final_form.get(_norm_tag_key(tag), tag)
                        for tag in (cn_enforced or [])]
        return new_sources, new_dropped, new_enforced
    except Exception:  # noqa: BLE001
        return tag_sources, dropped, cn_enforced


def _rename_debug_keys(tag_sources, dropped, cn_enforced, rename):
    """把 debug 里的标签 key 一起改成库名（lead 提醒：必须与 positive 一致）。"""
    if not rename:
        return tag_sources, dropped, cn_enforced
    new_sources = {}
    for key, value in (tag_sources or {}).items():
        new_sources[rename.get(key, key)] = value
    new_dropped = []
    for item in (dropped or []):
        if not isinstance(item, dict):
            new_dropped.append(item)
            continue
        tag = item.get("tag")
        new_dropped.append(dict(item, tag=rename.get(_norm_tag_key(tag), tag)))
    new_enforced = [rename.get(_norm_tag_key(tag), tag)
                    for tag in (cn_enforced or [])]
    return new_sources, new_dropped, new_enforced


def _finalize_positive(positive, tagdb_debug, core, natural_language, ui):
    """positive 定稿前的统一收尾（首轮与重试共用，v12）。

    依次做三件事：
      1. **来源校验**：删无来源标签、补回确定性映射标签、补人数（``_enforce_tag_sources``）
      2. **库名规范化**：写法以 tagdb 为准（``_normalize_tag_names``，触发词跳过）
      3. **debug key 同步**：``tag_sources`` / ``dropped`` / ``cn_enforced`` 的 key
         与最终 positive 写法完全一致（lead 提醒的坑）

    :returns: ``(positive, tagdb_debug)``
    """
    core_debug = core.get("debug") if isinstance(core, dict) else {}
    core_debug = core_debug if isinstance(core_debug, dict) else {}
    info = dict(tagdb_debug) if isinstance(tagdb_debug, dict) else {}
    if isinstance(core_debug.get("final_character_count"), int):
        info["final_character_count"] = core_debug["final_character_count"]

    # v15：来源校验之前先做一次兜底（把语义等价的旧写法对齐到库标准 tag），
    # 真正的**括号转义**放在最后一步（不影响来源校验与去重）
    positive, _early_bracket = _sanitize_brackets(
        positive, core_debug.get("character_ids"), info,
        core_debug.get("character_tag_map"))

    positive, source_info = _enforce_tag_sources(
        positive, core, natural_language, ui,
        core_debug.get("cn_segments") or {})
    info["tag_sources"] = dict(source_info.get("tag_sources") or {})
    info["dropped"] = list(source_info.get("dropped") or [])
    info["cn_enforced"] = list(source_info.get("enforced") or [])
    if source_info.get("gender_assumed"):
        info["gender_assumed"] = True

    # v15.5：角色 trigger **强制注入** —— LLM 经常不写 trigger（只写特征），
    # 而 trigger 不在 cn_map 里，不补就等于「识别到了角色但画面里没有她」。
    positive = _force_character_triggers(
        positive, core_debug.get("active_character_tags"), info,
        core_debug.get("character_tag_map"))

    name_info = {}
    positive, rename_map = _normalize_tag_names(
        positive, name_info,
        skip_keys={_norm_tag_key(tag)
                   for tag in (core_debug.get("user_triggers") or ())})

    (sources, dropped, enforced) = _rename_debug_keys(
        info.get("tag_sources"), info.get("dropped"),
        info.get("cn_enforced"), rename_map)
    info["tag_sources"] = sources
    info["dropped"] = dropped
    info["cn_enforced"] = enforced

    (sources, dropped, enforced) = _resync_debug_keys(
        info.get("tag_sources"), info.get("dropped"),
        info.get("cn_enforced"), positive)
    info["tag_sources"] = sources
    info["dropped"] = dropped
    info["cn_enforced"] = enforced

    if name_info.get("renamed"):
        info["names_normalized"] = list(name_info["renamed"])
    if name_info.get("name_unknown"):
        info["name_unknown"] = list(name_info["name_unknown"])

    # v15.4：规范化之后**再兜一次裸 id 补全**。
    # LLM 有时会直接输出 `sparkle` / `firefly` 这种裸 id（它以为"触发词原样保留"
    # 就是写 id），而裸 id 在库里是「闪光」「萤火虫」这种 general 概念，
    # 或者根本不存在。这里再跑一次 sanitize 把它们补全成库标准 tag。
    positive, _late_sanitize = _sanitize_brackets(
        positive, core_debug.get("character_ids"), info,
        core_debug.get("character_tag_map"))

    # v15.6：同角色全名与裸 id 并存时删掉裸 id（裸 id 可能是「闪光」这类无关概念）
    positive, _bare_dropped = _drop_bare_character_ids(
        positive, core_debug.get("character_tag_map"))
    if _bare_dropped:
        info["bare_id_dropped"] = list(_bare_dropped)

    # v15.8：**最终兜底** —— 把 positive 里残留的裸角色 id 升格成库标准 tag。
    # LLM 路径下 `_force_character_triggers` 有时没生效（core_debug 的来源不同），
    # 结果输出 `castorice` 而不是 `castorice_(honkai:_star_rail)`；裸 id 在库里
    # 要么不存在、要么是「萤火虫/闪光」这类无关概念，必须升格。
    positive, _upgraded = _upgrade_bare_character_ids(
        positive, core_debug.get("character_tag_map"), info)

    # v15.7：用**最终 positive** 重建来源表，保证「标签数 == tag_sources 条数」。
    # 前面的强制注入（角色 trigger）会新增标签，不重建就对不上。
    try:
        _existing = info.get("tag_sources") or {}
        _rebuilt = {}
        for _row in _split_lines(positive):
            for _tag in _row:
                _key = _norm_tag_key(_tag)
                if _key:
                    _rebuilt[_key] = _existing.get(_key, "enforced")
        info["tag_sources"] = _rebuilt
    except Exception:  # noqa: BLE001
        pass

    # v15：**最后**做括号转义（ComfyUI 的 escape_important 保证安全），
    # 然后按最终写法再同步一次 debug key，保持「positive 与 debug 一致」
    positive, _escaped = _escape_brackets(positive, info)
    if _escaped:
        (_rs2_src, _rs2_drop, _rs2_enf) = _resync_debug_keys(
            info.get("tag_sources"), info.get("dropped"),
            info.get("cn_enforced"), positive)
        info["tag_sources"] = _rs2_src
        info["dropped"] = _rs2_drop
        info["cn_enforced"] = _rs2_enf
    return positive, info


# ---------------------------------------------------------------------------
# v14：自然语言角色识别 + 括号触发词护栏
# ---------------------------------------------------------------------------

def _collect_character_ids(view):
    """拿到所有角色的纯 id（用于括号护栏）。"""
    try:
        catalog = view.catalog() or {}
        ids = []
        for character in _dicts(catalog.get("characters")):
            char_id = _s(character.get("id"))
            if char_id:
                ids.append(char_id)
        return ids
    except Exception:  # noqa: BLE001
        return []


def _scan_characters_in_text(natural_language, view, existing_rows, data=None):
    """从自然语言正文里扫描角色别名，返回需要**补充**的虚拟角色行（v14）。

    用户实际输入是长句，长句里带角色名最自然（「遐蝶在雨夜的街头」），
    不能要求每条都去下拉框选一遍。

    规则：
      * 扫描 ``aliases``（中文名 / 英文名 / 其它别名），大小写不敏感、子串命中
      * 已经出现在 ``ui.character_rows`` 里的角色**不重复添加**（按 id / display_name 判重）
      * 同一角色被多个别名命中只加一次
      * 虚拟行的 ``trigger`` 用**纯 id**（绝不带括号，括号是权重语法）
      * ``outfit_hint`` 留空 -> ``resolve_outfit`` 取默认（第一套），
        用户若在正文里描述了不同服装，``detect_outfit_override`` 会接管

    :returns: ``list[dict]``，每项是可直接混进 ``character_rows`` 的行
    """
    added = []
    try:
        text = str(natural_language or "").lower()
        if not text.strip():
            return added
        catalog = view.catalog() or {}
        characters = _dicts(catalog.get("characters"))
        if not characters:
            return added

        existing_keys = set()
        for row in (existing_rows or ()):
            for field in ("name", "trigger", "id"):
                value = _norm(row.get(field))
                if value:
                    existing_keys.add(value)

        matched_ids = set()
        for character in characters:
            char_id = _s(character.get("id"))
            if not char_id or char_id in matched_ids:
                continue
            display = _s(_first(character, ("display_name", "name", "id"))) or char_id
            aliases = [_s(item) for item in _as_list(character.get("aliases")) if _s(item)]
            if not aliases:
                aliases = [display, char_id]
            hit = None
            for alias in aliases:
                if alias and alias.lower() in text:
                    hit = alias
                    break
            if not hit:
                continue
            if _norm(char_id) in existing_keys or _norm(display) in existing_keys:
                # UI 里已经选了这个角色，不重复注入
                matched_ids.add(char_id)
                continue
            matched_ids.add(char_id)
            added.append({
                "id": "text_" + char_id,
                "name": display,
                "trigger": char_id,          # 纯 id，不带括号
                "outfit_hint": "",
                "_from_text": True,
                "_matched_alias": hit,
            })
        return added
    except Exception as exc:  # noqa: BLE001
        _log(f"_scan_characters_in_text 异常（跳过正文识别）：{type(exc).__name__}: {exc}")
        return added


def _character_text_segment(natural_language, matched_alias, other_aliases):
    """从长句里切出「属于该角色」的一段（v14.1）。

    「遐蝶穿黑色长裙，流萤穿白色长裙」按角色名切成两段，
    各自只对自己的那一段做服装覆盖检测，避免两个角色的服装互相污染。

    :param natural_language: 用户原文
    :param matched_alias: 该角色命中的别名
    :param other_aliases: 其它已识别角色的别名（作为分界点）
    """
    text = str(natural_language or "")
    alias = _s(matched_alias)
    if not text or not alias:
        return text
    # v14.2：取**最后一次**出现的位置。
    # 原文「遐蝶和流萤在公园里散步，遐蝶穿黑色长裙，流萤穿白色长裙」里
    # 每个角色出现两次：第一次是并列主语（后面紧跟另一个角色名），
    # 第二次才跟自己的服装描述。用 find 会切到只有名字的那一段。
    start = text.lower().rfind(alias.lower())
    if start < 0:
        return text
    end = len(text)
    for other in (other_aliases or ()):
        other_text = _s(other)
        if not other_text or other_text.lower() == alias.lower():
            continue
        position = text.lower().find(other_text.lower(), start + len(alias))
        if 0 <= position < end:
            end = position
    segment = text[start:end].strip(" ，,。.、；;\n\t")
    return segment or text

def _text_match_aliases(text_rows):
    """把所有正文识别角色的命中别名收集起来（做分界点用）。"""
    aliases = []
    for row in (text_rows or ()):
        alias = _s(row.get("_matched_alias"))
        if alias:
            aliases.append(alias)
    return aliases


#: 括号 + 冒号（prompt 里是权重语法，角色名里出现会拆坏标签）
_BRACKET_RE = re.compile(r"[（(]")
_BRACKET_STRIP_RE = re.compile(r"[（(][^)）]*[)）]")
#: 常规颜文字白名单（这些括号是内容本身，不处理）
_BRACKET_SAFE_EMOTICONS = frozenset((
    "^_^", "^_^;", ">_<", "o_o", "t_t", ";_;;", ":3", ":d", ":p", ":)",
))


def _trigger_base(tag):
    """把 ``castorice_(honkai:_star_rail)`` 剥成可比较的基础名。"""
    stripped = _BRACKET_STRIP_RE.sub("", tag or "")
    stripped = stripped.replace("_", " ").replace(":", " ")
    return " ".join(stripped.split()).strip().lower()


#: 角色 id -> 库标准 tag 的进程内缓存（避免每个角色每次组装都查库）
_CHARACTER_TAG_CACHE = {}


def _resolve_character_tag(character, view=None):
    """把角色解析成 danbooru 库里的**标准角色 tag**（v15，带括号不转义）。

    为什么不能直接用 id（lead 核实）：
      * ``castorice`` 裸 id 在库里 NOT FOUND
      * ``firefly`` 裸 id 是 general「萤火虫」（510 posts）
      * ``sparkle`` 裸 id 是 general「闪光」（228739 posts）
    直接用 id 会匹配到空或完全无关的概念，画面必然出错。

    解析链在 ``tagdb.resolve_character_tag``（多候选 + category=4 校验 + posts 最大）；
    解析失败时**回退纯 id** 并记 ``debug.trigger_unresolved``。
    """
    try:
        char_id = _s((character or {}).get("id"))
        if not char_id:
            return ""
        if char_id in _CHARACTER_TAG_CACHE:
            return _CHARACTER_TAG_CACHE[char_id]
        candidates = [char_id]
        for alias in _as_list((character or {}).get("aliases")):
            text = _s(alias)
            if text and text not in candidates:
                candidates.append(text)
        words = _as_list((character or {}).get("activation_words"))
        if words:
            first = _s(words[0])
            if first and first not in candidates:
                candidates.append(first)
        resolved = ""
        if _tagdb_mod is not None:
            resolver = getattr(_tagdb_mod, "resolve_character_tag", None)
            if callable(resolver):
                try:
                    name, _info = resolver(candidates)
                    resolved = _s(name)
                except Exception:  # noqa: BLE001
                    resolved = ""
        _CHARACTER_TAG_CACHE[char_id] = resolved
        return resolved
    except Exception:  # noqa: BLE001
        return ""


def _upgrade_bare_character_ids(positive, tag_map, info=None):
    """把 positive 里残留的裸角色 id 升格成库标准 tag（v15.8 最终兜底）。

    与 ``_force_character_triggers`` 的区别：那个是「缺了就补」，
    这个只在**裸 id 已经存在**时做原地替换，不新增标签，也不改行结构，
    对模板路径完全无副作用（模板路径本来就没有裸 id）。
    """
    mapping = {}
    for cid, tag in (tag_map or {}).items():
        bare = _s(cid)
        full = _s(tag)
        if bare and full and _norm_tag_key(bare) != _norm_tag_key(full):
            mapping[_norm_tag_key(bare)] = full
    if not mapping:
        return positive, []
    try:
        rows = _split_lines(positive)
    except Exception:  # noqa: BLE001
        return positive, []
    present = {_norm_tag_key(tag) for row in rows for tag in row}
    upgraded = []
    changed = False
    for row in rows:
        for index, item in enumerate(row):
            full = mapping.get(_norm_tag_key(item))
            if not full:
                continue
            # 已经存在同角色的库标准 tag 时，直接删掉裸 id（避免一条角色两种写法）
            if _norm_tag_key(full) in present and index < len(row):
                row[index] = ""
                changed = True
                continue
            row[index] = full
            present.add(_norm_tag_key(full))
            upgraded.append(full)
            changed = True
    if not changed:
        return positive, []
    rows = [[tag for tag in row if tag] for row in rows]
    rows = [row for row in rows if row]
    if isinstance(info, dict):
        sources = info.get("tag_sources")
        if isinstance(sources, dict):
            for tag in upgraded:
                sources[_norm_tag_key(tag)] = "enforced"
    return _join_lines(rows), upgraded


def _force_character_triggers(positive, active_tags, info=None, tag_map=None):
    """确保每个参与角色的库标准 tag 一定出现在 positive 里（v15.5）。

    LLM 成功路径下模型经常只写角色的特征标签（发色/瞳色/服装），
    **不写角色 trigger**；而 trigger 不在 cn_map 里，`_enforce_cn_segments`
    不会补它 —— 结果「识别到了角色，但画面里没有她」。

    :param active_tags: ``["castorice_(honkai:_star_rail)", ...]``（按角色行顺序）
    """
    tags = [_s(tag) for tag in (active_tags or ()) if _s(tag)]
    if not tags:
        return positive
    try:
        rows = _split_lines(positive)
        if not rows:
            return _join_lines([[tag] for tag in tags])
        present = {_norm_tag_key(tag) for row in rows for tag in row}
        missing = [tag for tag in tags if _norm_tag_key(tag) not in present]
        if not missing:
            return positive
        # v15.6：若 positive 里已经有该角色的**裸 id**（LLM 常这么写），
        # 直接把它替换成库标准 tag，而不是再追加一个 —— 否则会出现
        # `sparkle, sparkle_\(honkai:_star_rail\)` 这种一条角色两个写法的重复。
        id_of_tag = {}
        for cid, tag in (tag_map or {}).items():
            key = _norm_tag_key(tag)
            if key and _s(cid):
                id_of_tag[key] = _s(cid)
        still_missing = []
        for tag in missing:
            bare = id_of_tag.get(_norm_tag_key(tag))
            replaced = False
            if bare:
                bare_key = _norm_tag_key(bare)
                for row in rows:
                    for index, item in enumerate(row):
                        if _norm_tag_key(item) == bare_key:
                            row[index] = tag
                            replaced = True
                            break
                    if replaced:
                        break
            if not replaced:
                still_missing.append(tag)
        missing = still_missing
        if not missing:
            payload = info if isinstance(info, dict) else {}
            payload.setdefault("trigger_renamed", []).extend(tags)
            return _join_lines(rows)
        # 追加到对应的角色行（按顺序）；角色行不够就新起一行
        appended = 0
        for index, tag in enumerate(missing):
            char_row = None
            # 找第 index 个「像角色行」的行（含 outfit/hair/eyes/expression/action）
            candidates = [
                i for i, row in enumerate(rows)
                if any(_tag_dimension(item) in
                       ("outfit", "hair", "eyes", "expression", "action", "gaze")
                       for item in row)
            ]
            if index < len(candidates):
                char_row = candidates[index]
            if char_row is None:
                rows.append([tag])
            else:
                rows[char_row].insert(0, tag)
            appended += 1
        if appended:
            payload = info if isinstance(info, dict) else {}
            payload.setdefault("trigger_injected", []).extend(missing)
        return _join_lines(rows)
    except Exception as exc:  # noqa: BLE001
        _log(f"_force_character_triggers 异常（保持原样）：{type(exc).__name__}: {exc}")
        return positive


def _drop_bare_character_ids(positive, tag_map=None):
    """同一角色同时出现「库标准全名」和「裸 id」时，删掉裸 id（v15.6）。

    LLM 有时会同时写 `sparkle, sparkle_(honkai:_star_rail)`。
    裸 `sparkle` 在库里是 general「闪光」（228739 posts），
    和角色名一起送去出图会污染画面。保留全名、丢掉裸 id。
    """
    try:
        pairs = []
        for cid, tag in (tag_map or {}).items():
            bare = _s(cid)
            full = _s(tag)
            if bare and full and _norm_tag_key(bare) != _norm_tag_key(full):
                pairs.append((_norm_tag_key(bare), _norm_tag_key(full)))
        if not pairs:
            return positive, []
        rows = _split_lines(positive)
        present = {_norm_tag_key(item) for row in rows for item in row}
        doomed = {bare for bare, full in pairs
                  if bare in present and full in present}
        if not doomed:
            return positive, []
        dropped = []
        kept_rows = []
        for row in rows:
            kept = []
            for item in row:
                if _norm_tag_key(item) in doomed:
                    dropped.append(_s(item))
                    continue
                kept.append(item)
            if kept:
                kept_rows.append(kept)
        if not dropped:
            return positive, []
        return _join_lines(kept_rows), dropped
    except Exception:  # noqa: BLE001
        return positive, []


def _escape_brackets(positive, info=None):
    """把标签里的字面括号转义成 ``\\(`` ``\\)``（v15）。

    依据（lead 在 ComfyUI 源码里验证）：``comfy/sd1_clip.py`` 的
    ``escape_important`` 会在 ``parse_parentheses`` **之前**把 ``\\(`` ``\\)``
    换成占位符，``unescape_important`` 分词后再还原成字面括号。
    所以转义后：既不会被当权重语法，CLIP 又能收到正确的 ``castorice_(honkai:_star_rail)``。

    **不动**：合法权重语法 ``(smile:1.2)``、常规颜文字 ``^_^``、已转义的标签。
    """
    changed = []
    try:
        rows = _split_lines(positive)
        if not rows:
            return positive, changed
        for row in rows:
            for index, tag in enumerate(row):
                text = _s(tag)
                if not text:
                    continue
                if "(" not in text and ")" not in text:
                    continue
                if _CANONICAL_WEIGHT_RE.match(text):
                    continue
                if text.lower().strip() in _BRACKET_SAFE_EMOTICONS:
                    continue
                if "\\(" in text or "\\)" in text:
                    continue
                escaped = text.replace("(", "\\(").replace(")", "\\)")
                row[index] = escaped
                changed.append({"from": text, "to": escaped})
        if not changed:
            return positive, changed
        payload = info if isinstance(info, dict) else {}
        payload.setdefault("bracket_escaped", []).extend(changed)
        return _join_lines(rows), changed
    except Exception as exc:  # noqa: BLE001
        _log(f"_escape_brackets 异常（保持原样）：{type(exc).__name__}: {exc}")
        return positive, changed


def _sanitize_brackets(positive, character_ids, info=None, tag_map=None):
    """护栏：把带括号的角色触发词还原成**纯 id**（v14）。

    圆括号在 ComfyUI / A1111 的 prompt 语法里表示权重分组，
    ``castorice_(honkai:_star_rail)`` 会被解析成「对 castorice_ 加权」+「对冒号内容加权」，
    等于把角色名拆坏 —— 这是会直接导致出图错误的硬 bug。

    处理范围**只限角色触发词形态**的标签（去括号后的基础名匹配某个角色 id），
    常规标签（含表情符号、权重语法）一律不动。

    :returns: ``(new_positive, changed)``
    """
    changed = []
    try:
        if not character_ids:
            return positive, changed
        rows = _split_lines(positive)
        if not rows:
            return positive, changed
        lowered_ids = {_s(cid).lower(): _s(cid) for cid in character_ids if _s(cid)}
        id_bases = {_trigger_base(cid): _s(cid) for cid in character_ids if _s(cid)}
        # v15.3：裸角色 id -> 库标准 tag。
        # LLM 可能直接输出 `castorice`（纯 id），纯 id 在库里 NOT FOUND，
        # 来源校验又因为它在用户原文里而放行 —— 结果画不出来。必须补全成全名。
        id_to_tag = {}
        for cid, tag in (tag_map or {}).items():
            key = _norm_tag_key(cid)
            target = _s(tag)
            if key and target and _norm_tag_key(target) != key:
                id_to_tag[key] = target
        for row in rows:
            for index, tag in enumerate(row):
                text = _s(tag)
                if not text:
                    continue
                plain_key = _norm_tag_key(text)
                if plain_key in id_to_tag:
                    target_tag = id_to_tag[plain_key]
                    changed.append({"from": text, "to": target_tag})
                    row[index] = target_tag
                    continue
                if not _BRACKET_RE.search(text):
                    continue
                if text.lower().strip() in _BRACKET_SAFE_EMOTICONS:
                    continue
                # 保留权重语法 (tag:1.2)
                if _CANONICAL_WEIGHT_RE.match(text):
                    continue
                base = _trigger_base(text)
                if not base:
                    continue
                target = None
                if base in id_bases:
                    target = id_bases[base]
                else:
                    for alias, cid in lowered_ids.items():
                        alias_base = _trigger_base(alias)
                        if alias_base and (base == alias_base or base.startswith(alias_base)):
                            target = cid
                            break
                if target:
                    changed.append({"from": text, "to": target})
                    row[index] = target
        if not changed:
            return positive, changed
        payload = info if isinstance(info, dict) else {}
        payload.setdefault("bracket_sanitized", []).extend(changed)
        return _join_lines(rows), changed
    except Exception as exc:  # noqa: BLE001
        _log(f"_sanitize_brackets 异常（保持原样）：{type(exc).__name__}: {exc}")
        return positive, changed


def _restructure_rows(rows):
    """按「正面提示词结构」**强制重排**各段（v11）。

    LLM 常把场景标签塞进角色行（``smile, blue_hair, city, street, night``），
    不符合「场景画风单独一行」。这里按标签维度重新分组：

    * ``quality`` -> 第 1 行
    * ``count`` / ``shot`` -> 第 2 行（视距 + 人数 + 多角色动作里的视距人数部分）
    * ``scene`` -> 最后一行
    * 其余（hair/eyes/expression/action/gaze/outfit/自由标签）-> 角色行，保持原行序

    多角色时角色行的**行数与相对顺序**保持不变（每行一个角色），只把场景和
    质量标签抽走，所以不会把两个角色并到一行。
    """
    try:
        quality, framing, scene = [], [], []
        character_rows = []
        seen = set()

        def _fresh(tag):
            key = _norm_tag_key(tag)
            if not key or key in seen:
                return False
            seen.add(key)
            return True

        for row in rows or ():
            keep = []
            for tag in row:
                if not _fresh(tag):
                    continue
                dimension = _tag_dimension(tag)
                if dimension == "quality":
                    quality.append(tag)
                elif dimension in ("count", "shot"):
                    framing.append(tag)
                elif dimension == "scene":
                    scene.append(tag)
                else:
                    keep.append(tag)
            if keep:
                character_rows.append(keep)
        ordered = []
        if quality:
            ordered.append(quality)
        if framing:
            ordered.append(framing)
        ordered.extend(character_rows)
        if scene:
            ordered.append(scene)
        return ordered
    except Exception:  # noqa: BLE001
        return rows


def _enforce_tag_sources(positive, core, natural_language, ui, cn_mapped):
    """v11 核心：删除无来源标签、补回确定性标签、记录来源。

    :returns: ``(new_positive, info)``，``info`` 含 ``tag_sources`` /
        ``dropped`` / ``enforced`` / ``lines``。
    """
    info = {"tag_sources": {}, "dropped": [], "enforced": [], "lines": 0}
    try:
        rows = _split_lines(positive)
        if not rows:
            return positive, info
        allow = _template_allowlist(core)
        # v13：扫描出的英文标签的**全部词元**，用于「维度豁免」的交集判断
        mention_tokens = set()
        for _seg_key in ("quality", "framing", "character", "scene"):
            for _tag in (cn_mapped.get(_seg_key) or ()):
                mention_tokens.update(_norm_tag_key(_tag).split())
        # v12：性别表由模板路径算好放在 core.debug 里，供人数推断使用
        core_debug = core.get("debug") if isinstance(core, dict) else {}
        if isinstance(core_debug, dict) and isinstance(
                core_debug.get("gender_map"), dict):
            info["gender_map"] = dict(core_debug["gender_map"])
        mentioned = _mentioned_dimensions(natural_language, cn_mapped, ui)
        user_tokens = set(
            re.findall(r"[a-z0-9_']+", str(natural_language or "").lower()))
        kept_rows = []
        for row in rows:
            kept = []
            for tag in row:
                key = _norm_tag_key(tag)
                if not key:
                    continue
                if key in allow:
                    kept.append(tag)
                    info["tag_sources"][key] = "template"
                    continue
                # 用户原文英文词元全覆盖 -> 有来源
                words = re.findall(r"[a-z0-9_']+", key)
                if words and all(word in user_tokens for word in words):
                    kept.append(tag)
                    info["tag_sources"][key] = "user_text"
                    continue
                # 收紧（lead 要求「没有任何来源的标签直接删掉」）：
                # 不再因为「用户提到过这个维度」就放行 —— 用户说「蓝发」不代表可以加
                # long_hair / twintails / colored_inner_hair。只有白名单（角色库、
                # 风格库、中文映射、质量默认、模板里的用户英文）和用户原文英文词元
                # 才算有来源。这样「一个蓝发少女」不会冒出任何编造标签。
                # v13：维度豁免（长句适配）。
                # 条件（三条全满足才放行，比早期宽泛版严、比全删版宽）：
                #   1. 该维度属于可细化维度（**hair / eyes 永不放行** ——
                #      「一个蓝发少女」不能冒出 long_hair）
                #   2. 中文扫描在该维度上**有实体**
                #   3. 标签与扫描出的英文标签**共享至少一个词元**
                #      （扫描有 street，允许 wet_street；扫描没有 classroom，就不许凭空加）
                dim = _tag_dimension(tag)
                words = set(key.split())
                if (dim and dim in _ELABORATABLE_DIMENSIONS
                        and dim in mentioned and (words & mention_tokens)):
                    kept.append(tag)
                    info["tag_sources"][key] = "user_text_zh"
                    continue
                reason = "unmentioned_%s" % (dim or "free_tag")
                info["dropped"].append({"tag": tag, "reason": reason})
            if kept:
                kept_rows.append(kept)

        # 补回确定性映射里 LLM 漏掉的标签（按段归位）
        enforced = _enforce_cn_segments(kept_rows, cn_mapped, info, ui)
        rows = enforced

        # 角色数量补齐（结构第 2 行要求有）
        rows = _enforce_character_count(rows, ui, cn_mapped, info)

        if not rows:
            return positive, info
        # 按「正面提示词结构」重排：质量 / 视距+人数 / 每角色一行 / 场景
        rows = _restructure_rows(rows)
        info["lines"] = len(rows)
        return _join_lines(rows), info
    except Exception as exc:  # noqa: BLE001 - 来源校验失败绝不拖垮出图
        _log(f"_enforce_tag_sources 异常（原样返回）：{type(exc).__name__}: {exc}")
        return positive, info


def _find_segment_row(rows, dimension):
    """按维度找目标行；找不到返回 ``None``。

    行定位靠「该行是否含这个维度的标签」，不依赖固定行号（LLM 可能省略某行）。
    """
    for index, row in enumerate(rows):
        for tag in row:
            if _tag_dimension(tag) == dimension:
                return index
    return None


def _append_to_row(rows, index, tags, sources, info):
    """把 ``tags`` 追加到第 ``index`` 行（去重后）；``index`` 为 ``None`` 时新增行。"""
    existing = {_norm_tag_key(tag) for row in rows for tag in row}
    fresh = []
    for tag in tags:
        key = _norm_tag_key(tag)
        if not key or key in existing:
            continue
        existing.add(key)
        fresh.append(tag)
        sources[key] = "enforced_cn_map"
        info["enforced"].append(tag)
    if not fresh:
        return rows
    if index is None or index < 0 or index >= len(rows):
        rows.append(fresh)
    else:
        rows[index].extend(fresh)
    return rows


def _enforce_cn_segments(rows, cn_mapped, info, ui=None):
    """修 1：中文映射命中的标签**强制出现**在最终 positive 里（按段归位）。"""
    sources = info["tag_sources"]
    quality = cn_mapped.get("quality") or ()
    framing = cn_mapped.get("framing") or ()
    scene = cn_mapped.get("scene") or ()
    if quality:
        index = _find_segment_row(rows, "quality")
        if index is None and rows:
            rows.insert(0, [])
            index = 0
        rows = _append_to_row(rows, index, quality, sources, info)
    if framing:
        index = _find_segment_row(rows, "count")
        if index is None:
            index = _find_segment_row(rows, "shot")
        if index is None:
            quality_index = _find_segment_row(rows, "quality")
            insert_at = 1 if quality_index == 0 else 0
            rows.insert(insert_at, [])
            index = insert_at
        rows = _append_to_row(rows, index, framing, sources, info)
    if scene:
        index = _find_segment_row(rows, "scene")
        rows = _append_to_row(rows, index, scene, sources, info)
    # v13：character 段（表情/动作/发色/服装）也要补 —— 之前漏了，
    # 导致「散步」映射出的 walking 命中却不进 positive（lead 实测长句 2）。
    # **多角色时跳过**：像「一个穿白裙一个穿黑裙」这种两套服装要留给 LLM
    # 分配到各自的角色行，代码强行合并会两边都穿同一件。
    character = cn_mapped.get("character") or ()
    # v14.2：优先用「最终角色数」（含正文识别），其次回退到 ui 行数
    _final_count = info.get("final_character_count")
    _multi = (_final_count > 1) if isinstance(_final_count, int) \
        else _is_multi_character_ui(ui)
    if character and not _multi:
        index = None
        for row_index, row in enumerate(rows):
            if any(_tag_dimension(tag) in
                   ("outfit", "hair", "eyes", "expression", "action", "gaze")
                   for tag in row):
                index = row_index
                break
        rows = _append_to_row(rows, index, character, sources, info)
    return rows


def _is_multi_character_ui(ui):
    """UI 里是否选了**多个**角色（多角色时服装映射要留给 LLM 分配）。"""
    try:
        config = _normalize_ui(ui)
        count = sum(1 for row in config["character_rows"]
                    if _s(row.get("trigger")))
        return count > 1
    except Exception:  # noqa: BLE001
        return False


def _infer_character_count(ui, gender_map=None):
    """按角色**性别**推人数标签（v12，lead 指定）。

    ``gender`` 来自 ``plugin/data/characters.json``（本轮已给 9 个角色补上）。

    =======================  =========================
    角色性别组合              人数标签
    =======================  =========================
    1 female                 ``1girl, solo``
    1 male                   ``1boy, solo``
    2 female                 ``2girls``
    1 female + 1 male        ``1girl, 1boy``
    2 male                   ``2boys``
    缺 gender / 无角色        ``1girl, solo``（保守，标记 assumed）
    =======================  =========================

    :returns: ``(tags, assumed)`` —— ``assumed=True`` 表示有角色缺 gender 被兜底，
        调用方应写 ``debug.gender_assumed``。
    """
    try:
        config = _normalize_ui(ui)
        mapping = gender_map if isinstance(gender_map, dict) else {}
        assumed = False
        genders = []
        for row in config["character_rows"]:
            if not _s(row.get("trigger")):
                continue
            name = _s(row.get("name"))
            gender = _norm(mapping.get(name)) if name else ""
            if gender not in ("female", "male"):
                gender = "female"
                assumed = True
            genders.append(gender)
        if not genders:
            genders = ["female"]
            assumed = True
        girls = genders.count("female")
        boys = genders.count("male")
        tags = []
        if girls:
            tags.append("1girl" if girls == 1 else "%dgirls" % girls)
        if boys:
            tags.append("1boy" if boys == 1 else "%dboys" % boys)
        if girls == 1 and not boys:
            tags.append("solo")
        return tags, assumed
    except Exception:  # noqa: BLE001
        return ["1girl", "solo"], True


def _enforce_character_count(rows, ui, cn_mapped, info):
    """修 2：把人数标签补到第 2 行；用户明确说过人数时以用户为准。"""
    try:
        sources = info["tag_sources"]
        explicit = [tag for tag in (cn_mapped.get("framing") or ())
                    if _tag_dimension(tag) == "count"]
        gender_map = info.get("gender_map") or {}
        if explicit:
            targets = explicit
        else:
            targets, gender_assumed = _infer_character_count(ui, gender_map)
            if gender_assumed:
                info["gender_assumed"] = True
        existing = {_norm_tag_key(tag) for row in rows for tag in row}
        # 已经有任何一个 count 标签就不重复补（solo 与 2girls 互斥，只留一个口径）
        has_count = any(
            _tag_dimension(tag) == "count" for row in rows for tag in row)
        if has_count:
            return rows
        index = _find_segment_row(rows, "shot")
        if index is None:
            quality_index = _find_segment_row(rows, "quality")
            insert_at = 1 if quality_index == 0 else 0
            rows.insert(insert_at, [])
            index = insert_at
        fresh = []
        for tag in targets:
            key = _norm_tag_key(tag)
            if key and key not in existing:
                existing.add(key)
                fresh.append(tag)
                sources[key] = "count_inferred"
                info["enforced"].append(tag)
        if fresh:
            rows[index].extend(fresh)
        return rows
    except Exception:  # noqa: BLE001
        return rows


def _ensure_garment_after_override(positive, core):
    """覆盖触发后，保证 positive 至少有一个服装标签（P0 兜底）。

    背景（lead 复验发现）：覆盖会删掉旧服装，如果 LLM 没有写新服装，
    输出就变成"角色光着"——**比"衣服不对"更糟**。所以这里做一层兜底：
    LLM 已经写了服装 -> 不动；没写 -> 用 ``_compose_core`` 提前算好的
    ``new_garments`` 补上（那是经 tagdb 校验的标准写法）。

    :returns: ``(positive, info)``，``info["supplemented"]`` 表示是否补过
    """
    info = {"supplemented": False, "added": []}
    try:
        core_debug = core.get("debug") if isinstance(core, dict) else {}
        core_debug = core_debug if isinstance(core_debug, dict) else {}
        override = core_debug.get("outfit_override")
        if not isinstance(override, dict) or not override.get("detected"):
            return positive, info
        if _has_garment_tag(positive):
            return positive, info
        supplement = [word for word in (override.get("new_garments") or []) if word]
        if not supplement:
            return positive, info
        rows = _split_lines(positive)
        if not rows:
            rows = [list(supplement)]
            info["supplemented"] = True
            info["added"] = supplement
            return _join_lines(rows), info
        # v10：服装必须追加到**角色行**，不能落到最后的场景行。
        # 角色行的判据：该行含 `injected_triggers` 里的角色主体特征/触发词。
        triggers = {
            str(token).strip().lower()
            for token in ((core_debug.get("injected_triggers") or []))
            if str(token).strip()
        }
        target = None
        for index, row in enumerate(rows):
            if any(str(tag).strip().lower() in triggers for tag in row):
                target = index
                break
        if target is None:
            target = len(rows) - 1   # 兜底：无法定位角色行时追加到最后一行
        rows[target].extend(supplement)
        info["supplemented"] = True
        info["added"] = supplement
        info["line_index"] = target
        _log("标签库 agent 前置兜底：覆盖后 LLM 未写服装，已补 %s（第 %d 行）"
             % (", ".join(supplement), target + 1))
        return _join_lines(rows), info
    except Exception as exc:  # noqa: BLE001 - 兜底失败就保持原样
        _log(f"_ensure_garment_after_override 异常（保持原样）："
             f"{type(exc).__name__}: {exc}")
        return positive, info


def _run_tagdb_agent(
    positive, negative, analysis, extra, parsed, tagdb_debug,
    config, core, natural_language, view, skill, llm, ui, first_messages,
    forbidden_tokens, enabled=True, min_ratio=0.10,
):
    """标签库 agent 第 2 轮：把「问题词 + 库中合法候选」反馈给 LLM，让它自己修。

    硬约束（lead）：

    * **只在该修的时候修**：问题词（``unknown`` + ``suggestions``）占比
      低于 ``min_ratio`` 不触发 —— 绝大多数请求仍然只调用一次 LLM；
    * **只追加 messages**：``[system, user, assistant, user]``，前缀逐字节不变，
      prompt cache 有效；
    * **绝不增加失败率**：第 2 轮任何异常 / 超时都回退第 1 轮结果
      （它已经过 tagdb 归一），不因为 agent 环节挂掉导致整体重写失败；
    * **上限 1 轮**，不做循环。

    :returns: ``(positive, negative, analysis, extra, parsed, tagdb_debug,
        agent_debug)``，7 元组与入参前 6 项同构
    """
    agent_debug = {
        "enabled": bool(enabled),
        "triggered": False,
        "applied": False,
    }
    try:
        if not enabled:
            agent_debug["reason"] = "disabled"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug
        if not isinstance(tagdb_debug, dict) or not tagdb_debug.get("available"):
            agent_debug["reason"] = "tagdb_unavailable"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug
        builder = getattr(_tagdb_mod, "build_agent_feedback", None)
        if not callable(builder):
            agent_debug["reason"] = "module_missing"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug

        plan = builder(tagdb_debug, min_ratio=min_ratio)
        plan = plan if isinstance(plan, dict) else {}
        agent_debug["problem_ratio"] = plan.get("problem_ratio", 0.0)
        agent_debug["problem_count"] = plan.get("problem_count", 0)
        if not plan.get("triggered"):
            agent_debug["reason"] = _s(plan.get("reason")) or "not_triggered"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug

        agent_debug["triggered"] = True
        agent_debug["min_ratio"] = min_ratio
        agent_debug["feedback"] = plan.get("feedback") or ""
        agent_debug["problems"] = [
            {
                "token": _s(item.get("token")),
                "kind": _s(item.get("kind")),
                "candidates": [
                    _s(candidate.get("name"))
                    for candidate in _dicts(item.get("candidates"))[:5]
                ],
            }
            for item in _dicts(plan.get("problems"))
        ]

        messages = _build_tagdb_agent_messages(
            first_messages, _retry_assistant_text(parsed), plan.get("feedback") or "")
        if messages is first_messages:
            agent_debug["reason"] = "no_messages"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug

        # ---- 第 2 轮调用：任何失败都回退第 1 轮（绝不拖垮整体）
        started = time.perf_counter()
        try:
            (positive2, negative2, analysis2, extra2, parsed2) = _call_llm_once(
                natural_language, config, core, llm, view, skill,
                messages=messages, forbidden_tokens=forbidden_tokens)
        except LLMError as exc:
            code = getattr(exc, "code", "llm_error")
            _log(f"标签库 agent 第 2 轮失败（回退第 1 轮结果）：{code}")
            agent_debug["fallback"] = True
            agent_debug["error"] = _s(code)
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug
        except Exception as exc:  # noqa: BLE001
            _log(f"标签库 agent 第 2 轮异常（回退第 1 轮结果）：{type(exc).__name__}: {exc}")
            agent_debug["fallback"] = True
            agent_debug["error"] = "llm_unexpected_error"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug

        agent_debug["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)

        # 第 2 轮同样过一遍标签库（与首轮对称），再统计采纳情况
        positive2, tagdb_debug2 = _apply_tagdb(positive2, ui, core, skill)
        agent_debug.update(_agent_compare(plan, tagdb_debug2))

        # ---- P1：**只有确实变好才采纳** ————
        # "第 2 轮调用成功、但结果更差"这种情况必须兜住（真实事故：
        # 某次输入第 1 轮 unknown=1，第 2 轮变成 2）。比较 unknown + suggestions
        # 的总数，没有严格减少就整体回退第 1 轮结果。
        before_total = _agent_problem_total(tagdb_debug)
        after_total = _agent_problem_total(tagdb_debug2)
        agent_debug["problem_total_before"] = before_total
        agent_debug["problem_total_after"] = after_total
        if after_total >= before_total:
            _log("标签库 agent 第 2 轮没有改善（问题词 %d -> %d），已回退第 1 轮结果"
                 % (before_total, after_total))
            agent_debug["fallback"] = True
            agent_debug["fallback_reason"] = "not_improved"
            return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug

        agent_debug["applied"] = True
        return positive2, negative2, analysis2, extra2, parsed2, tagdb_debug2, agent_debug
    except Exception as exc:  # noqa: BLE001 - agent 只是增强，绝不拖垮重写
        _log(f"标签库 agent 异常（跳过反馈，保持第 1 轮结果）：{type(exc).__name__}: {exc}")
        agent_debug["reason"] = "internal_error"
        return positive, negative, analysis, extra, parsed, tagdb_debug, agent_debug


def _merge_suppress_into_negative(llm_negative, forbidden_tokens, template_negative=""):
    """把模板的 outfit 抑制词**强制并入** LLM 的 negative（契约 §14.2 工程保障）。

    为什么需要：``check_anima`` 只校验「positive 不含 forbidden」，**不校验
    「negative 必须含」**，所以 LLM 偷懒漏写抑制词也会被判 ``ok``（真实实跑中
    R2 用例抑制覆盖率 0/11，比模板模式还差）。

    规则（lead 裁决）：
    * **只并 outfit 抑制词**，不把整份模板 negative 塞进去（否则 LLM 的自由发挥全废）；
    * LLM negative 在前，追加的抑制词在后；
    * 去重保序；
    * LLM 完全没给 negative 时，先用模板 negative 兜底再补抑制词。

    :returns: ``(merged_negative, missing_tokens)``；``missing_tokens`` 是
        **合并前** LLM 原始 negative 漏掉的抑制词（供 ``debug.skill_check`` 观测）。
    """
    tokens = []
    for item in forbidden_tokens or ():
        text = item.strip() if isinstance(item, str) else str(item or "").strip()
        if text:
            tokens.append(text)
    if not tokens:
        # 没有压制词要求 -> 原样返回（v1 行为逐字节不变）
        return llm_negative, []

    def split(value):
        if not isinstance(value, str):
            return []
        return [piece.strip() for piece in value.split(",") if piece.strip()]

    def compute_missing(existing_lower, seen_added=None):
        missing = []
        added = []
        local_seen = set(seen_added or ())
        for token in tokens:
            key = token.lower()
            if key in existing_lower or key in local_seen:
                continue
            local_seen.add(key)
            missing.append(token)
            added.append(token)
        return missing, added

    parts = split(llm_negative)
    missing, appended = compute_missing({piece.lower() for piece in parts})

    if not parts and isinstance(template_negative, str) and template_negative.strip():
        # LLM 没给任何 negative：用模板 negative 兜底（再补缺失抑制词）
        parts = split(template_negative)
        missing, appended = compute_missing({piece.lower() for piece in parts})

    merged = _dedupe_join(parts + appended)
    return merged, missing


def _append_retry_issues(messages, issues):
    """【已停用，保留仅为兼容可能的旧调用方】

    v3 及更早的做法：把 issues 直接拼到**第一条 user 消息**末尾，然后整份
    messages 重新发给 LLM。问题是它必须配合「从头重建 messages」使用，
    等于把 skill 全文（约 4.5k 字符）和 few-shot（约 1.1k 字符）再发一遍。

    新实现见 :func:`_build_retry_messages`（多轮追加，system/user 一字不改）。
    """
    if not isinstance(messages, list) or not messages:
        return messages
    lines = []
    for issue in _dicts(issues)[:10]:
        code = _s(issue.get("code")) or "issue"
        detail = _s(issue.get("detail"))
        lines.append(f"- {code}: {detail}" if detail else f"- {code}")
    if not lines:
        return messages
    appended = [dict(message) for message in messages]
    last = appended[-1]
    last["content"] = (
        (_s(last.get("content")) + "\n\n"
         + "上次输出未通过校验，请修正以下问题后重新输出 JSON：\n"
         + "\n".join(lines))
    )
    return appended


#: 重试时回放给模型的 assistant 内容上限（字符）。
#: 正常输出是几百字符的 JSON；设上限只防模型抽风输出超长文本把上下文撑爆。
RETRY_ASSISTANT_MAX_CHARS = 4000

#: 重试时追加的 user 消息开头（与旧行为保持同样的引导语，便于日志对照）
RETRY_USER_PREFIX = "上次输出未通过校验，请修正以下问题后重新输出 JSON："


def _retry_assistant_text(parsed):
    """把上一轮 LLM 的解析结果还原成可回放的 assistant 文本。

    为什么用 ``json.dumps(parsed)`` 而不是「服务端返回的原始字符串」：

    1. 原始字符串可能裹着 ```` ```json ```` 围栏（``parse_json_loose`` 会剥掉），
       原样回放会诱导模型下一轮继续带围栏，反而降低解析成功率；
    2. 规范化后的 JSON 与模型上一轮的**内容**完全一致，多轮对话里足够让模型
       看清自己写了什么，不需要逐字节还原格式；
    3. 不需要改动 ``llm_client.chat_json`` 的返回结构（那条路径还有别的消费方）。

    :returns: JSON 文本；``parsed`` 不是 dict 时返回空串（调用方会跳过 assistant 轮）
    """
    if not isinstance(parsed, dict):
        return ""
    try:
        text = json.dumps(parsed, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    if len(text) > RETRY_ASSISTANT_MAX_CHARS:
        text = text[:RETRY_ASSISTANT_MAX_CHARS] + "...(truncated)"
    return text


def _build_retry_messages(messages, assistant_text, issues):
    """契约 §14.2 重试：改成**多轮追加**，system / user 一字不改。

    为什么要重写（v3 及以前的做法见 :func:`_append_retry_issues`）：

    旧实现调 ``_build_messages_for()`` 从头重建整份 messages 再发一次。而
    system 内容 = 用户 system_prompt + skill.system_prompt(4525 字符) +
    few-shot(1113 字符)，**与第一次逐字节相同** —— 既白烧一遍 token，
    也让服务端的 prompt cache / KV cache 无法复用第一轮已经算好的前缀。

    新结构（只追加、不改写）::

        [system, user, assistant(上一轮输出), user(修正要求)]

    于是 ``[system, user]`` 这个前缀与首次请求**逐字节一致**，llama.cpp 的
    prompt cache、OpenAI / DeepSeek 的 prefix cache 都能直接命中。

    注意：本函数**不修改**传入的 ``messages``（返回的是逐条浅拷贝的新列表），
    保证首次请求的 messages 仍可被调用方原样复用。

    :param messages: 首次请求用的 messages
    :param assistant_text: 上一轮输出（见 :func:`_retry_assistant_text`）
    :param issues: ``check_anima`` 的 issues 列表
    :returns: 新的 messages；无 issues 或 messages 非法时**原样返回**入参
    """
    if not isinstance(messages, list) or not messages:
        return messages

    lines = []
    for issue in _dicts(issues)[:10]:
        code = _s(issue.get("code")) or "issue"
        detail = _s(issue.get("detail"))
        lines.append(f"- {code}: {detail}" if detail else f"- {code}")
    if not lines:
        return messages

    appended = [dict(message) for message in messages]
    text = assistant_text if isinstance(assistant_text, str) else ""
    if text.strip():
        appended.append({"role": "assistant", "content": text})
    appended.append(
        {"role": "user", "content": RETRY_USER_PREFIX + "\n" + "\n".join(lines)}
    )
    return appended


def _call_safetensors_once(llm, messages, model, core):
    """v3：``mode == "safetensors"`` 的在进程内推理路径（**不起 HTTP 服务**）。

    与 :func:`_call_llm_once` 的 HTTP 路径返回完全同构的
    ``(positive, negative, analysis, extra, parsed)``，供 ``_rewrite_only`` 的
    skill 校验 / 重试 / debug 结构原样复用。

    ``llm_local_hf.chat()`` 自身在 ``finally`` 里按 ``unload_after_use`` 卸载，
    外层 ``_apply_unload`` 再调 ``runtime.unload(llm)`` 是幂等兜底。
    """
    if _llm_client is None:
        raise LLMError(ERR_NOT_CONNECTED, "llm_client.py 不可用，无法解析 safetensors 输出")

    module = _load_sibling("llm_local_hf")
    chat = getattr(module, "chat", None) if module is not None else None
    if not callable(chat):
        raise LLMError(
            ERR_SAFETENSORS_UNAVAILABLE,
            "llm_local_hf.py 不可用或缺少 chat()，无法运行 safetensors 模型；"
            "请改用 GGUF 或云端模式",
        )

    started = time.perf_counter()
    try:
        out = chat(llm, messages)
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转契约错误码
        code = getattr(exc, "code", None) or ERR_SAFETENSORS_FAILED
        raise LLMError(code, _safe_text(str(exc))) from exc
    elapsed = round((time.perf_counter() - started) * 1000.0, 2)

    if not isinstance(out, dict):
        raise LLMError(
            ERR_SAFETENSORS_FAILED,
            f"llm_local_hf.chat 返回异常类型：{type(out).__name__}",
        )

    text = out.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LLMError(ERR_SAFETENSORS_FAILED, "llm_local_hf.chat 返回空文本")

    # 与 llm_client.chat_json 尾部完全一致的解析校验（§7 解析规则）
    parse = getattr(_llm_client, "parse_json", None)
    parsed = parse(text) if callable(parse) else None
    if not isinstance(parsed, dict):
        raise LLMError("llm_bad_json", text[:200])
    positive = parsed.get("positive")
    if not isinstance(positive, str) or not positive.strip():
        raise LLMError("llm_empty_positive", text[:200])

    positive, negative, analysis = _extract_llm_result(parsed, core)

    llm_debug = parsed.get("debug") if isinstance(parsed.get("debug"), dict) else {}
    # 透出 transformers 侧观测项（不含权重路径细节以外的敏感信息）
    llm_debug = dict(llm_debug)
    for key in (
        "device",
        "prompt_tokens",
        "completion_tokens",
        "used_chat_template",
        "load_sec",
        "dtype",
        "gpu_index",
    ):
        if key in out:
            llm_debug[key] = out.get(key)

    extra = {
        "llm_base_url": "",
        "llm_model": _s(out.get("model")) or _s(model),
        "llm_provider": "safetensors",
        "llm_mode": "safetensors",
        "llm_elapsed_ms": elapsed,
        "llm_debug": llm_debug,
        # safetensors 在进程内运行，没有任何 API Key
        "api_key_source": "none",
    }
    return positive, negative, analysis, extra, parsed


def _call_llm_once(
    natural_language, config, core, llm, view, skill, messages=None, forbidden_tokens=()
):
    """单次 LLM 调用，返回 ``(positive, negative, analysis, extra, parsed)``。"""
    if _llm_client is None:
        raise LLMError(ERR_NOT_CONNECTED, "llm_client.py 不可用")

    mode, base_url, model = _resolve_llm_target(llm)

    if messages is None:
        messages = _build_messages_for(
            config,
            core,
            natural_language,
            view,
            skill,
            forbidden_tokens,
        )

    # v3：safetensors 在进程内推理，没有 HTTP 端点，也不涉及 api_key
    if mode == "safetensors":
        return _call_safetensors_once(llm, messages, model, core)

    api_key = ""
    api_key_source = "none"
    if mode != "local":
        # 契约 §13.3：直接消费 providers.resolve_api_key 的 (key, source)
        api_key, api_key_source = _resolve_api_key(llm.get("provider"))
        if not api_key:
            raise LLMError(
                ERR_KEY_MISSING,
                "云端模式未配置 api_key（请在「LLM 设置 > 运行设置」中填写，"
                f"或设置 ANIMA_LLM_API_KEY_<供应商大写> / {_API_KEY_ENV}）",
            )

    chat_config = {
        "mode": mode,
        "provider": _norm(llm.get("provider")),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "sampling": llm.get("sampling") if isinstance(llm.get("sampling"), dict) else {},
    }

    started = time.perf_counter()
    parsed = _llm_client.chat_json(chat_config, messages, _timeout_from(llm))
    elapsed = round((time.perf_counter() - started) * 1000.0, 2)

    positive, negative, analysis = _extract_llm_result(parsed, core)

    llm_debug = parsed.get("debug") if isinstance(parsed.get("debug"), dict) else {}
    extra = {
        "llm_base_url": base_url,
        "llm_model": model,
        "llm_provider": _norm(llm.get("provider")),
        "llm_elapsed_ms": elapsed,
        "llm_debug": llm_debug,
        # 契约 §13.3：source ∈ {"env","file","none"}，便于排查密钥来源（绝不带值）
        "api_key_source": api_key_source,
    }
    return positive, negative, analysis, extra, parsed


def _rewrite_only(llm, natural_language, ui, data=None, core=None):
    """**只做 LLM 重写**的完整链路（供 ``compose_with_llm`` 与 ``/nl2prompt/rewrite`` 共用）。

    链路（契约 §14.2）：skill 注入 -> LLM -> 校验 -> 失败带 issues 重试 1 次 ->
    仍失败则 ``ok=False``（调用方回退模板）。**不触碰 lora_stack / 不执行图。**

    :returns: 成功 ``{"ok":True,"positive","negative","analysis","debug":{...}}``；
        失败 ``{"ok":False,"error":code,"detail":str,"debug":{...}}``。
    """
    if core is None:
        core = _compose_core(natural_language, ui, data)
    config = _normalize_ui(ui)
    view = _make_view(data)

    def fail(code, detail="", **extra_debug):
        debug = {"used_llm": False, "fallback": _s(code) or "llm_error", "detail": _s(detail)}
        debug.update(extra_debug)
        return {"ok": False, "error": debug["fallback"], "detail": debug["detail"], "debug": debug}

    if not llm or not isinstance(llm, dict):
        return fail(ERR_NOT_CONNECTED, "llm 输入未连接或格式不合法")

    # ---- skill
    skill_id = _s(config["ui"].get("skill_id")) or DEFAULT_SKILL_ID
    skill = _load_skill(skill_id)

    # v5：提示词优化开关（全开为默认；读失败也全开，绝不退化成双份注入）
    prompt_options = _prompt_options()

    # ---- 本次注入约束（契约 §14.2 校验输入）
    # v15.7：required_tokens 用**未转义**的标准 tag；
    # 括号转义的差异由 skill_check._bare_tag 统一剥掉（双向对齐，见该函数注释）。
    required_tokens = list(core["debug"].get("injected_triggers") or [])
    forbidden_tokens = list(
        (core["debug"].get("negative_suppress_source") or {}).get("outfit") or []
    )

    # 首次请求的 messages **显式构造并保留**：重试要在它后面追加轮次，
    # 而不是像旧实现那样从头重建（那样 system 会重复发送 skill 全文 + few-shot）。
    first_messages = None
    try:
        first_messages = _build_messages_for(
            config, core, natural_language, view, skill, forbidden_tokens
        )
        positive, negative, analysis, extra, parsed = _call_llm_once(
            natural_language,
            config,
            core,
            llm,
            view,
            skill,
            messages=first_messages,
            forbidden_tokens=forbidden_tokens,
        )
    except LLMError as exc:
        return fail(getattr(exc, "code", "llm_error"), getattr(exc, "detail", "") or str(exc))
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须结构化返回
        _log(f"LLM 意外错误：{traceback.format_exc(limit=3)}")
        return fail("llm_unexpected_error", f"{type(exc).__name__}: {exc}")

    # v5 阶段 2：danbooru 标签库校验与归一（**只校验不生成**）。
    # 放在强制并入抑制词之前，让后面的校验看到的就是最终值。
    # 任何失败都降级为「原文不动」，只记录 debug.tagdb。
    positive, tagdb_debug = _apply_tagdb(positive, ui, core, skill)

    # v9 P0 兜底：服装覆盖触发后，若 LLM 没写新服装就补上。
    # 不补的后果是"旧衣服删了、新衣服没有"，角色光着 —— 比衣服不对更糟。
    positive, garment_info = _ensure_garment_after_override(positive, core)
    if garment_info.get("supplemented"):
        if isinstance(tagdb_debug, dict):
            tagdb_debug = dict(tagdb_debug)
        core_override = (core.get("debug") or {}).get("outfit_override") or {}
        if isinstance(core_override, dict):
            tagdb_debug["outfit_override"] = dict(core_override)
            tagdb_debug["outfit_override"]["garment_supplemented"] = True
            tagdb_debug["outfit_override"]["supplemented_tags"] = \
                garment_info.get("added") or []

    # v7：标签库 agent 反馈轮 —— 把「具体问题词 + 库中合法候选」追加成一轮，
    # 让 LLM 自己决定采纳 / 换说法 / 保留。只在问题词占比够高时触发（上限 1 轮），
    # 第 2 轮失败一律回退第 1 轮结果（它已经过 tagdb 归一）。
    (positive, negative, analysis, extra, parsed, tagdb_debug,
     tagdb_agent_debug) = _run_tagdb_agent(
        positive, negative, analysis, extra, parsed, tagdb_debug,
        config, core, natural_language, view, skill, llm, ui, first_messages,
        forbidden_tokens,
        enabled=bool(prompt_options.get("tagdb_agent")),
        min_ratio=_tagdb_agent_min_ratio(),
    )
    if isinstance(tagdb_debug, dict):
        tagdb_debug = dict(tagdb_debug)
        tagdb_debug["agent"] = tagdb_agent_debug

    # v11 + v12：来源校验（禁止编造）+ 库名规范化 + debug key 同步，
    # 放在 agent **之后**（agent 可能回退第 1 轮结果、也可能带进新标签）
    positive, tagdb_debug = _finalize_positive(
        positive, tagdb_debug, core, natural_language, ui)

    # 契约 §14.2（lead 裁决）：**强制并入**模板的 outfit 抑制词，不依赖 LLM 自觉。
    # 必须在 check_anima 之前，让校验看到的就是最终值。
    negative, negative_missing = _merge_suppress_into_negative(
        negative, forbidden_tokens, core["negative"]
    )

    check = _run_skill_check(positive, negative, required_tokens, forbidden_tokens, skill)
    retried = False

    if not check.get("ok", True):
        # 契约 §14.2：带 issues 重试 **1 次**
        #
        # v4 优化（可由 settings.prompt_opt.retry_multiturn 关闭）：
        # 默认改成**多轮追加**（[system, user, assistant, user]），直接复用首次的
        # first_messages，system 前缀逐字节不变 -> 服务端 prompt cache 可命中，
        # 不再重复计算 4.5k+1.1k 字符的 skill 全文。
        retried = True
        if prompt_options["retry_multiturn"]:
            retry_messages = _build_retry_messages(
                first_messages,
                _retry_assistant_text(parsed),
                check.get("issues") or [],
            )
        else:
            # 老路径（v3 及以前）：从头重建 messages，把 issues 拼到 user 末尾。
            # 仅在 prompt_opt.retry_multiturn=false 时使用，用于 A/B 对照排查。
            _log("prompt_opt.retry_multiturn=false，重试退回重建 messages 的老路径")
            retry_messages = _append_retry_issues(
                _build_messages_for(
                    config, core, natural_language, view, skill, forbidden_tokens
                ),
                check.get("issues") or [],
            )
        try:
            positive2, negative2, analysis2, extra2, parsed2 = _call_llm_once(
                natural_language,
                config,
                core,
                llm,
                view,
                skill,
                messages=retry_messages,
                forbidden_tokens=forbidden_tokens,
            )
        except LLMError as exc:
            return fail(
                getattr(exc, "code", "llm_error"),
                getattr(exc, "detail", "") or str(exc),
                skill_check={
                    "ok": False,
                    "issues": check.get("issues") or [],
                    "retried": True,
                    "negative_missing": negative_missing,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"LLM 重试意外错误：{traceback.format_exc(limit=3)}")
            return fail(
                "llm_unexpected_error",
                f"{type(exc).__name__}: {exc}",
                skill_check={
                    "ok": False,
                    "issues": check.get("issues") or [],
                    "retried": True,
                    "negative_missing": negative_missing,
                },
            )

        # 重试结果同样先过标签库校验（与首轮对称），再强制合并抑制词
        positive2, tagdb_debug_retry = _apply_tagdb(positive2, ui, core, skill)
        if isinstance(tagdb_debug_retry, dict):
            tagdb_debug_retry = dict(tagdb_debug_retry)
            # agent 只跑一轮（首轮之后），这里保留它的观测结果不被覆盖
            tagdb_debug_retry["agent"] = tagdb_agent_debug
        tagdb_debug = tagdb_debug_retry

        # 重试结果同样强制合并抑制词
        negative2, negative_missing2 = _merge_suppress_into_negative(
            negative2, forbidden_tokens, core["negative"]
        )

        check2 = _run_skill_check(
            positive2, negative2, required_tokens, forbidden_tokens, skill
        )
        positive, negative, analysis, extra, parsed, check = (
            positive2,
            negative2,
            analysis2,
            extra2,
            parsed2,
            check2,
        )
        negative_missing = negative_missing2

        # 重试结果与首轮对称收尾：来源校验 + 库名规范化 + debug key 同步。
        # 不补这一步，重试成功后 tag_sources 会被 _apply_tagdb 的返回值覆盖成空。
        positive, tagdb_debug = _finalize_positive(
            positive, tagdb_debug, core, natural_language, ui)

    skill_check_debug = {
        "ok": bool(check.get("ok", True)),
        "issues": check.get("issues") or [],
        "retried": retried,
        "stats": check.get("stats") or {},
        # 观测项：本次 LLM **原始** negative 漏掉的抑制词（强制合并前），
        # 供 skill-author 的校验器观测；不需要可忽略。
        "negative_missing": negative_missing,
    }
    if check.get("skipped"):
        skill_check_debug["skipped"] = True

    if not check.get("ok", True):
        # 两次都不合格 -> 契约 §14.2：回退模板拼接
        return fail(
            ERR_SKILL_CHECK_FAILED,
            "skill 校验两次未通过，已回退模板拼接",
            skill_check=skill_check_debug,
            quality=_run_quality(positive, negative, skill),
            analysis=analysis,
            tagdb=tagdb_debug,
        )

    quality = _run_quality(positive, negative, skill)
    debug = {
        "used_llm": True,
        "fallback": "",
        "detail": "",
        "skill_id": skill_id,
        "skill_check": skill_check_debug,
        "quality": quality,
        "analysis": analysis,
        "retried": retried,
        # v5：标签库校验观测项（corrected / suggestions / unknown / stats / unknown_ratio）
        "tagdb": tagdb_debug,
        # v5：本轮生效的提示词优化开关（便于对照排查）
        "prompt_opt": prompt_options,
    }
    # v9：把模板侧的清理/覆盖观测项透出到 /rewrite 的 debug。
    # `_compose_core` 的 debug 只进节点输出，走 /rewrite（_rewrite_only）会丢，
    # 用户排查「我的中文怎么不见了」时需要看得见。
    _core_debug = core.get("debug") if isinstance(core, dict) else {}
    if isinstance(_core_debug, dict):
        for _key in ("cjk_cleaned", "cjk_removed_chars", "cjk_fallback",
                     "outfit_override", "cn_mapped", "cn_dropped",
                     "quality_default", "count_inferred"):
            if _key in _core_debug:
                debug[_key] = _core_debug[_key]
    # 覆盖兜底的信息在 tagdb_debug 里更全（含 garment_supplemented），优先用它
    if isinstance(tagdb_debug, dict) and isinstance(
            tagdb_debug.get("outfit_override"), dict):
        debug["outfit_override"] = tagdb_debug["outfit_override"]
    # v11：多行行数在 LLM 路径也要按**最终** positive 重算（之前只有模板路径设置，
    # LLM 改写后行数会变，debug 里却是模板的数，属观测错误）
    debug["segment_lines"] = len(str(positive or "").splitlines())
    debug.update(extra)
    return {
        "ok": True,
        "positive": positive,
        "negative": negative,
        "analysis": analysis,
        "debug": debug,
    }


def _safetensors_still_loaded(runtime_module):
    """safetensors 模型是否仍驻留显存；取不到状态时返回 ``None``（未知）。"""
    info_function = getattr(runtime_module, "loaded_info", None)
    if not callable(info_function):
        return None
    try:
        info = info_function()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    item = info.get("safetensors")
    if isinstance(item, dict):
        return bool(item.get("loaded"))
    return None


def _apply_unload(llm, debug):
    """v3 B3：按 ``llm.runtime.unload_after_use`` 卸载本地 LLM。

    调用方放在 ``finally`` 里 —— **成功与失败都要执行**。

    v3 收尾：优先 ``runtime.unload(llm)``（按 mode 分派，只卸**对应**的运行时，
    不会误杀其它正在跑的本地模型）；``unload`` 不存在时才回退旧的 ``stop_all()``。
    ``runtime`` 模块缺失或卸载失败都不能影响出图（只记 debug）。

    ``debug["unloaded"]`` 的语义（lead 裁决）是
    **「本次执行结束后该运行时确实不再驻留」**，而不是「卸载函数返回了 True」——
    ``unload()`` 返回 False 常常只是「``llm_local_hf.chat`` 内部已经卸过了」，
    这种情况依然要报 True。safetensors 用 ``runtime.loaded_info()`` 二次确认；
    取不到状态时退化为「动作已调用即视为完成」。
    """
    unloaded = False
    try:
        runtime_cfg = llm.get("runtime") if isinstance(llm, dict) else None
        if isinstance(runtime_cfg, dict) and runtime_cfg.get("unload_after_use"):
            runtime_module = _load_sibling("runtime")
            unload = getattr(runtime_module, "unload", None) if runtime_module else None
            stop_all = (
                getattr(runtime_module, "stop_all", None) if runtime_module else None
            )
            called = False
            if callable(unload):
                returned = bool(unload(llm))
                called = True
                _log(f"已按 unload_after_use 调用 runtime.unload(llm) -> {returned}")
            elif callable(stop_all):
                stop_all()
                called = True
                _log("runtime.unload 不可用，回退 stop_all() 完成卸载")
            else:
                _log("unload_after_use 已开启，但 runtime.unload / stop_all 都不可用（跳过）")

            if called:
                mode = _norm(llm.get("mode")) or "local"
                if mode in ("safetensors", "hf", "st", "transformers"):
                    still = _safetensors_still_loaded(runtime_module)
                    unloaded = True if still is None else (not still)
                else:
                    # cloud 是 no-op；local 已停掉对应进程 —— 动作完成即视为已释放
                    unloaded = True
    except Exception:  # noqa: BLE001 - 卸载失败绝不能中断出图
        _log(f"卸载本地 LLM 异常：{traceback.format_exc(limit=2)}")
        unloaded = False
    try:
        debug["unloaded"] = bool(unloaded)
    except Exception:  # noqa: BLE001
        pass
    return unloaded


def _compose_with_llm_into(core, natural_language, ui, llm, data):
    """``compose_with_llm`` 的主体（便于外层用 ``finally`` 统一执行卸载）。"""
    debug = core["debug"]

    if not llm:
        debug["used_llm"] = False
        debug["fallback"] = ERR_NOT_CONNECTED
        debug["detail"] = "use_llm=true 但 llm 输入未连接，已回退模板"
        return core

    if not isinstance(llm, dict):
        debug["used_llm"] = False
        debug["fallback"] = ERR_NOT_CONNECTED
        debug["detail"] = f"llm 输入类型异常：{type(llm).__name__}，已回退模板"
        return core

    result = _rewrite_only(llm, natural_language, ui, data=data, core=core)
    if not result.get("ok"):
        debug["used_llm"] = False
        debug["fallback"] = _s(result.get("error")) or "llm_error"
        debug["detail"] = _s(result.get("detail"))
        # 契约 §14.2：校验失败信息也要进 debug
        result_debug = result.get("debug") or {}
        if isinstance(result_debug.get("skill_check"), dict):
            debug["skill_check"] = result_debug["skill_check"]
        if isinstance(result_debug.get("quality"), dict):
            debug["quality"] = result_debug["quality"]
        if "analysis" in result_debug:
            debug["analysis"] = result_debug.get("analysis")
        _log(f"LLM 回退：{debug['fallback']} - {debug['detail']}")
        return core

    positive = result["positive"]
    negative = result["negative"]
    core["positive"] = positive
    core["negative"] = negative
    # 契约 A6：text 格式必须保持 v1；analysis 只进 debug，绝不混入 text
    core["text"] = f"Positive:\n{positive}\n\nNegative:\n{negative}"
    debug["used_llm"] = True
    debug["fallback"] = ""
    debug["detail"] = ""
    debug["mode"] = "llm"
    debug.update(result.get("debug") or {})
    return core


def compose_with_llm(natural_language, ui, llm=None, data=None):
    """模板组装 + 可选 LLM 重写；任何失败都**回退模板**并写 ``debug``（契约 §5 / §8 / §14.2）。

    v3 B3：``finally`` 里按 ``llm.runtime.unload_after_use`` 卸载本地 LLM
    （成功与失败都执行），结果写入 ``debug.unloaded``。
    """
    core = _compose_core(natural_language, ui, data)
    debug = core["debug"]
    try:
        return _compose_with_llm_into(core, natural_language, ui, llm, data)
    finally:
        _apply_unload(llm, debug)


# --------------------------------------------------------------------------- GPU 枚举

_GPU_CACHE = None

#: nvidia-smi 查询超时（秒）
_NVIDIA_SMI_TIMEOUT = 5


def _parse_nvidia_smi(output):
    """解析 ``--format=csv,noheader,nounits`` 的 ``"0, NVIDIA GeForce RTX 3060, 12288"``。"""
    gpus = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except (TypeError, ValueError):
            continue
        name = parts[1]
        if not name:
            continue
        total_mb = 0
        if len(parts) >= 3:
            try:
                total_mb = int(float(parts[2]))
            except (TypeError, ValueError):
                total_mb = 0
        gpus.append({"index": index, "name": name, "total_mb": total_mb})
    gpus.sort(key=lambda item: item["index"])
    return gpus


def _gpus_via_torch():
    """回落路径：``torch.cuda`` 枚举（受 ``CUDA_VISIBLE_DEVICES`` 限制）。"""
    gpus = []
    try:
        import torch  # noqa: PLC0415 - 惰性导入：无 torch 环境也要能加载节点

        count = int(torch.cuda.device_count())
        for index in range(count):
            try:
                name = str(torch.cuda.get_device_name(index)).strip()
            except Exception:  # noqa: BLE001
                name = f"GPU {index}"
            total_mb = 0
            try:
                _free, total = torch.cuda.mem_get_info(index)
                total_mb = round(total / (1024 * 1024))
            except Exception:  # noqa: BLE001 - 拿不到显存就只给名字
                total_mb = 0
            gpus.append({"index": index, "name": name, "total_mb": total_mb})
    except Exception:  # noqa: BLE001 - 无 CUDA / 无 torch
        return []
    return gpus


def list_physical_gpus():
    """枚举**物理** GPU（nodes.py 与 routes.py 共用此唯一实现）。

    为什么优先 ``nvidia-smi``：ComfyUI 常以 ``--cuda-device 0`` 启动，进程内
    ``CUDA_VISIBLE_DEVICES`` 被限制为一张卡，``torch.cuda.device_count()`` 只能
    看到一张；而 ``runtime.py`` 是把这里的 ``index`` 当 ``CUDA_VISIBLE_DEVICES``
    传给**子进程**的，子进程能看到全部物理卡 —— 所以必须列物理卡。

    :returns: ``[{"index": int, "name": str, "total_mb": int}]``；失败返回 ``[]``。
    """
    import subprocess  # noqa: PLC0415

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode == 0:
            gpus = _parse_nvidia_smi(completed.stdout)
            if gpus:
                return gpus
    except Exception:  # noqa: BLE001 - 无 nvidia-smi / 超时 → 静默回落
        pass

    return _gpus_via_torch()


def _gpu_choices():
    """契约 §2.1：``"0: NVIDIA GeForce RTX 3060 (12288 MiB)"``；异常回落 ``["0","1"]``。"""
    global _GPU_CACHE
    if _GPU_CACHE is not None:
        return list(_GPU_CACHE)

    choices = []
    for gpu in list_physical_gpus():
        index = gpu.get("index", 0)
        name = _s(gpu.get("name")) or f"GPU {index}"
        total_mb = gpu.get("total_mb") or 0
        if total_mb:
            choices.append(f"{index}: {name} ({int(total_mb)} MiB)")
        else:
            choices.append(f"{index}: {name}")
    _GPU_CACHE = choices or list(GPU_FALLBACK)
    return list(_GPU_CACHE)


def _default_gpu_choice():
    """默认选中**物理 index 1**（LLM 跑 2060，3060 留给出图）；单卡回落 index 0。"""
    choices = _gpu_choices()
    for choice in choices:
        head = choice.split(":", 1)[0].strip()
        try:
            if int(head) == 1:
                return choice
        except (TypeError, ValueError):
            continue
    return choices[0] if choices else "0"



def _parse_gpu(value):
    """把 COMBO 值解析成 ``(gpu_index, gpu_name)``。"""
    text = _s(value)
    if not text:
        return 0, ""
    head, separator, tail = text.partition(":")
    if not separator:
        try:
            return int(head.strip()), ""
        except (TypeError, ValueError):
            return 0, text
    try:
        index = int(head.strip())
    except (TypeError, ValueError):
        return 0, text
    name = tail.strip()
    # 去掉 " (12288 MiB)" 尾巴
    if name.endswith(")") and "(" in name:
        name = name[: name.rfind("(")].strip()
    return index, name


# --------------------------------------------------------------------------- 节点一


def _encode_with_clip(clip, positive_text, negative_text):
    """v3 B1：把 positive / negative 编码成 CONDITIONING。

    * ``clip`` 未连接 → 返回两个**空条件** ``[[], {}]``，绝不抛异常；
    * 任一侧编码失败 → 该侧退化为空条件，原因作为第三个返回值交给 ``debug.cond_error``。

    :returns: ``(positive_cond, negative_cond, cond_error)``
    """
    empty_cond = [[], {}]
    if clip is None:
        return empty_cond, empty_cond, ""

    def encode_one(text):
        tokens = clip.tokenize(text or "")
        return clip.encode_from_tokens_scheduled(tokens)

    try:
        positive_cond = encode_one(
            positive_text if isinstance(positive_text, str) else ""
        )
    except Exception as exc:  # noqa: BLE001 - 编码失败必须退化，不能中断出图
        return empty_cond, empty_cond, f"positive: {type(exc).__name__}: {exc}"

    try:
        negative_cond = encode_one(
            negative_text if isinstance(negative_text, str) else ""
        )
    except Exception as exc:  # noqa: BLE001
        return positive_cond, empty_cond, f"negative: {type(exc).__name__}: {exc}"

    return positive_cond, negative_cond, ""


class NL2PromptComposer:
    """契约 §1：自然语言提示词。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "compose"
    # v3 B1：CONDITIONING / MODEL 只能**追加在尾部**，
    # 否则旧工作流的输出索引会整体错位（绝不能插在中间）。
    RETURN_TYPES = (
        "STRING",
        "STRING",
        "STRING",
        "LORA_STACK",
        "STRING",
        "CONDITIONING",
        "CONDITIONING",
        "MODEL",
    )
    RETURN_NAMES = (
        "positive",
        "negative",
        "text",
        "lora_stack",
        "debug",
        "positive_cond",
        "negative_cond",
        "model",
    )
    OUTPUT_NODE = False
    DESCRIPTION = (
        "把自然语言 + 角色/风格 LoRA 激活词 + 文字替换组装成提示词；"
        "可选调用 LLM 重写，失败自动回退模板。clip 连接时直接输出 CONDITIONING。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "natural_language": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "自然语言描述，例如：银发少女站在雨后的霓虹街道上",
                    },
                ),
                "use_llm": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "label_on": "LLM 重写",
                        "label_off": "仅模板",
                    },
                ),
                "_ui_json": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "{}",
                        "tooltip": "内部 holder：角色行/风格行/文字替换/系统提示词/采样",
                    },
                ),
            },
            "optional": {
                "llm": ("LLM_MODEL",),
                "clip": ("CLIP",),
                # v3 B1：MODEL 直通（用于把 CONDITIONING 接到采样器时保持模型链一致）
                "model": ("MODEL",),
            },
        }

    def compose(
        self,
        natural_language="",
        use_llm=True,
        _ui_json="{}",
        llm=None,
        clip=None,
        model=None,
    ):
        # clip 按契约 §1.2 预留：接受但不参与组装
        # v9：use_llm 默认改为 True（用户要求）。
        # 这是**节点新建时的默认值**；已保存的工作流会把 widgets_values 里的
        # 值（可能是 false）作为参数传进来，ComfyUI 的参数绑定优先于签名默认值，
        # 所以老工作流行为不变。
        # llm 未连接时仍会安全回退模板（见 _compose_with_llm_into）。
        ui = _parse_json_obj(_ui_json)

        try:
            if use_llm:
                core = compose_with_llm(natural_language, ui, llm)
            else:
                core = _compose_core(natural_language, ui)
                if llm:
                    core["debug"]["detail"] = "llm 已连接但 use_llm=false，使用模板模式"
        except Exception as exc:  # noqa: BLE001 - 绝不因组装异常中断出图
            _log(f"组装异常，使用最小兜底：{traceback.format_exc(limit=3)}")
            language = natural_language if isinstance(natural_language, str) else ""
            fallback_negative = _default_negative_base()
            core = {
                "positive": language.strip(),
                "negative": fallback_negative,
                "text": f"Positive:\n{language.strip()}\n\nNegative:\n{fallback_negative}",
                "lora_stack": [],
                "seed": -1,
                "debug": {
                    "schema": 1,
                    "used_llm": False,
                    "fallback": "compose_failed",
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                    "mode": "template",
                    "elapsed_ms": 0.0,
                },
            }

        debug = core.get("debug") or {}

        # v3 B1：clip 连接时编码 CONDITIONING；未连接/失败都退化为空条件，绝不抛异常
        positive_cond, negative_cond, cond_error = _encode_with_clip(
            clip,
            core.get("positive", ""),
            core.get("negative", ""),
        )
        if cond_error:
            debug["cond_error"] = cond_error
            _log(f"CLIP 编码失败，已退化为空条件：{cond_error}")

        try:
            debug_json = json.dumps(debug, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            debug_json = "{}"

        payload = {
            "seed": core.get("seed", -1),
            "used_llm": bool(debug.get("used_llm")),
            "fallback": _s(debug.get("fallback")),
            # 契约 §14.4：本轮对话条目 + 写入后长度（前端 onExecuted 负责落 holder）
            "dialog_entry": _build_dialog_entry(
                natural_language,
                core.get("positive", ""),
                core.get("negative", ""),
            ),
            "dialog_len": _next_dialog_len(ui),
        }
        if cond_error:
            payload["cond_error"] = cond_error

        return {
            "ui": {"anima_nl": [payload]},
            "result": (
                core.get("positive", ""),
                core.get("negative", ""),
                core.get("text", ""),
                core.get("lora_stack", []),
                debug_json,
                # v3 B1：尾部追加，顺序不可变
                positive_cond,
                negative_cond,
                model,
            ),
        }

    @classmethod
    def IS_CHANGED(cls, natural_language="", use_llm=True, _ui_json="{}", **kwargs):
        # v9：use_llm 默认 True，与 compose 签名保持一致（否则新建节点
        # 首帧会被误判为"参数已变化"而重复执行）。
        ui = _parse_json_obj(_ui_json)
        sampling = ui.get("sampling") if isinstance(ui.get("sampling"), dict) else {}
        try:
            seed = int(sampling.get("seed", -1))
        except (TypeError, ValueError):
            seed = -1
        if seed < 0:
            # 契约 §3：seed=-1 表示每次随机 → 必须每次重跑
            return time.time()

        signature = {
            "natural_language": natural_language if isinstance(natural_language, str) else "",
            "use_llm": bool(use_llm),
            "ui": ui,
            "seed": seed,
        }
        llm = kwargs.get("llm")
        if isinstance(llm, dict):
            signature["llm"] = {
                key: llm.get(key)
                for key in ("mode", "provider", "base_url", "model", "local_path", "gpu_index")
            }
        return _hash_params(signature)


# --------------------------------------------------------------------------- 节点二


def _unload_flag_from(holder_json):
    """从 ``_llm_json`` 读 ``runtime.unload_after_use``（合并版 safetensors 分支用）。"""
    holder = _parse_json_obj(holder_json)
    runtime = holder.get("runtime")
    if isinstance(runtime, dict):
        return bool(runtime.get("unload_after_use"))
    return False


def _safetensors_error(code, detail):
    """构造一个只有错误信息的 LLM_MODEL，让 Composer 走统一回退路径。"""
    return {
        "ui": {},
        "result": (
            {
                "mode": "safetensors",
                "provider": "",
                "provider_label": "",
                "base_url": "",
                "model": "",
                "local_path": "",
                "dtype": "",
                "gpu_index": -1,
                "error": code,
                "detail": detail,
            },
        ),
    }


def _load_llm_safetensors(gpu="0", model_dir="", model_file="", dtype="fp16",
                          unload_after_use=False, _llm_json="{}"):
    """合并版节点的 safetensors 分支：**整块委托**给拆分的 ``NL2PromptLLMSafetensors``。

    两套加载逻辑（合并版 / 拆分版）如果各写一份，迟早会飘移；这里只做转发，
    行为完全等同拆分节点。``nodes_llm`` 不可用时返回结构化错误，不抛异常。
    """
    module = _load_sibling("nodes_llm")
    loader = getattr(module, "NL2PromptLLMSafetensors", None) if module is not None else None
    if not callable(loader):
        return _safetensors_error(
            "safetensors_unavailable",
            "nodes_llm.py 不可用或缺少 NL2PromptLLMSafetensors",
        )
    try:
        helper = loader()
        return helper.load(
            gpu=gpu,
            model_dir=model_dir,
            model_file=model_file,
            dtype=dtype,
            unload_after_use=unload_after_use,
            _llm_json=_llm_json,
        )
    except Exception as exc:  # noqa: BLE001 - 加载失败要回退，不能中断出图
        _log(f"safetensors 分支委托失败：{type(exc).__name__}: {exc}")
        return _safetensors_error("safetensors_failed", f"{type(exc).__name__}: {exc}")


class NL2PromptLoadLLM:
    """契约 §2：加载 LLM（本地 llama-server / 云端 OpenAI 兼容）。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "load"
    RETURN_TYPES = ("LLM_MODEL",)
    RETURN_NAMES = ("llm",)
    OUTPUT_NODE = False
    DESCRIPTION = (
        "配置本地 llama-server 或云端 OpenAI 兼容端点，输出 LLM_MODEL（绝不含 api_key 明文）。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        gpus = _gpu_choices()
        default_gpu = _default_gpu_choice()
        options = provider_options()
        return {
            "required": {
                "model_type": (
                    # 第一项保持旧值「本地模型」（= GGUF 走 llama-server），旧工作流零影响；
                    # 新增项让同一个节点也能跑 HuggingFace safetensors
                    ["本地模型", "本地模型（safetensors）", "云端模型"],
                    {"default": "本地模型"},
                ),
                "gpu": (
                    gpus,
                    {
                        "default": default_gpu,
                        "tooltip": "物理 GPU（nvidia-smi 枚举）；默认 1 = 2060 跑 LLM，3060 留给出图",
                    },
                ),
                "model_path": (
                    "STRING",
                    {
                        "multiline": False,
                        # 契约 §14.5：该留空的留空 —— 默认空串，运行时回退自动探测
                        "default": "",
                        "tooltip": "本地模型根目录；留空自动探测 LM Studio / Ollama 默认路径",
                    },
                ),
                "model_file": (
                    [MODEL_FILE_PLACEHOLDER],
                    {"default": MODEL_FILE_PLACEHOLDER},
                ),
                "provider": (
                    options,
                    {"default": "deepseek" if "deepseek" in options else options[0]},
                ),
                "base_url": (
                    "STRING",
                    {
                        "multiline": False,
                        # 契约 §14.5：默认空串，运行时按 provider 解析
                        "default": "",
                        "tooltip": "留空则按 provider 自动填充",
                    },
                ),
                "cloud_model": (
                    "STRING",
                    {"multiline": False, "default": "", "tooltip": "云端模型代号，如 deepseek-chat"},
                ),
                "_llm_json": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "{}",
                        "tooltip": "内部 holder：采样参数 / 运行参数 / 云端模型缓存",
                    },
                ),
                # ---- 以下两个 widget 追加在 holder 之后：旧工作流的 widgets_values
                # 数组按位置恢复，新增项必须放在末尾才不会让旧值错位
                "model_dir": (
                    "STRING",
                    {
                        "multiline": False,
                        # 该留空的留空：仅 safetensors 模式使用
                        "default": "",
                        "tooltip": "HF 模型根目录（含 config.json）——仅 safetensors 模式；留空自动探测",
                    },
                ),
                "dtype": (
                    ["fp16", "bf16", "auto"],
                    {
                        "default": "fp16",
                        "tooltip": "权重精度——仅 safetensors 模式；fp16 最快，显存不足可试 auto",
                    },
                ),
            }
        }

    def load(
        self,
        model_type="本地模型",
        gpu="0",
        model_path="",
        model_file=MODEL_FILE_PLACEHOLDER,
        provider="deepseek",
        base_url="",
        cloud_model="",
        _llm_json="{}",
        model_dir="",
        dtype="fp16",
    ):
        model_type_norm = _norm(model_type)

        # ---- safetensors 模式：整块委托给拆分节点，避免两套加载逻辑飘移
        if "safetensors" in model_type_norm.lower():
            return _load_llm_safetensors(
                gpu=gpu,
                model_dir=model_dir,
                model_file=model_file,
                dtype=dtype,
                unload_after_use=_unload_flag_from(_llm_json),
                _llm_json=_llm_json,
            )

        holder = _parse_json_obj(_llm_json)
        sampling = _sampling_from(holder)
        runtime = _runtime_from(holder)

        gpu_index, gpu_name = _parse_gpu(gpu)
        provider_key = _norm(provider) or "deepseek"

        # 中英选项都认：中文「云端模型」以「云」开头，英文选项用 "Cloud"
        # （与本模块的 COMBO 选项及前端 MODEL_TYPE_EN 保持一致）
        mode = (
            "cloud"
            if (model_type_norm.startswith("云") or model_type_norm.startswith("cloud"))
            else "local"
        )

        resolved_base_url = _s(base_url)
        if not resolved_base_url:
            # 契约 §14.1/§14.5：base_url 默认空串，运行时按 provider 从注册表解析
            resolved_base_url = _provider_base_url(provider_key)

        selected_file = _s(model_file)
        local_path = ""
        if mode == "local" and not is_model_placeholder(selected_file):
            candidate = Path(selected_file)
            if candidate.is_absolute():
                # 用户直接粘贴绝对路径
                local_path = str(candidate)
            else:
                # 前端「刷新模型列表」填的是**相对 model_path** 的路径
                root = _s(model_path)
                local_path = str(Path(root) / selected_file) if root else ""

            # 统一输出绝对路径：runtime.ensure_runtime 要求绝对路径做 os.path.isfile
            # 与命令行拼接；文件不存在时 resolve() 仍返回规范化绝对路径，
            # 由 runtime 报 local_model_missing（契约 §8）
            if local_path:
                try:
                    local_path = str(Path(local_path).resolve())
                except (OSError, ValueError):
                    pass

        if mode == "local":
            model_name = Path(local_path).stem if local_path else ""
        else:
            local_path = ""
            model_name = _s(cloud_model)

        # A5：seed == -1 表示每次随机 → 生成本次真实种子并写回 sampling，
        # 保证「每次生成写入并显示」成立。
        raw_seed = sampling.get("seed", -1)
        try:
            raw_seed = int(raw_seed)
        except (TypeError, ValueError):
            raw_seed = -1
        actual_seed = random.randint(0, 2**63 - 1) if raw_seed < 0 else raw_seed
        sampling["seed"] = actual_seed

        llm = {
            "mode": mode,
            "provider": provider_key,
            # 契约 §14.1：显示名来自 providers 注册表（容错）
            "provider_label": provider_label(provider_key),
            "base_url": resolved_base_url,
            "model": model_name,
            "gpu_index": gpu_index,
            "gpu_name": gpu_name,
            "local_path": local_path,
            "sampling": sampling,
            "runtime": runtime,
        }
        # 契约 §2.2：绝不包含 api_key
        # ui.anima_llm 供前端 onExecuted 回填「上次种子」（A5）
        return {"ui": {"anima_llm": [{"seed": actual_seed}]}, "result": (llm,)}

    @classmethod
    def IS_CHANGED(
        cls,
        model_type="本地模型",
        gpu="0",
        model_path="",
        model_file=MODEL_FILE_PLACEHOLDER,
        provider="deepseek",
        base_url="",
        cloud_model="",
        _llm_json="{}",
        **kwargs,
    ):
        # A5：seed == -1 时每次都要重跑，才能生成新种子并回填显示
        holder = _parse_json_obj(_llm_json)
        sampling = holder.get("sampling") if isinstance(holder.get("sampling"), dict) else {}
        try:
            seed = int(sampling.get("seed", -1))
        except (TypeError, ValueError):
            seed = -1
        if seed < 0:
            return time.time()

        return _hash_params(
            {
                "model_type": model_type,
                "gpu": _s(gpu),  # 只取 index+name 文本，避免显存波动导致每次重跑
                "model_path": model_path,
                "model_file": model_file,
                "provider": provider,
                "base_url": base_url,
                "cloud_model": cloud_model,
                "_llm_json": _llm_json,
                "seed": seed,
            }
        )


# --------------------------------------------------------------------------- 节点三


def _normalize_lora_stack(stack):
    """LORA_STACK 容错归一化成 ``[(name, model_strength, clip_strength), ...]``。"""
    entries = []
    for item in _as_list(stack):
        name = ""
        model_strength = 1.0
        clip_strength = 1.0

        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, (list, tuple)):
            if not item:
                continue
            name = _s(item[0])
            if len(item) >= 2:
                model_strength = _float_or(item[1], 1.0)
            if len(item) >= 3:
                clip_strength = _float_or(item[2], model_strength)
            else:
                clip_strength = model_strength
        elif isinstance(item, dict):
            name = _s(_first(item, _LORA_KEYS))
            model_strength, clip_strength = _strength_pair(item)
        else:
            continue

        if not name:
            continue
        entries.append((name, model_strength, clip_strength))
    return entries


def _resolve_lora_path(lora_name, folder_paths):
    candidate = _s(lora_name)
    if not candidate:
        return None

    absolute = Path(candidate)
    if absolute.is_absolute() and absolute.is_file():
        return str(absolute)

    probes = [candidate]
    if not candidate.lower().endswith((".safetensors", ".ckpt", ".pt", ".bin")):
        probes.extend(
            [
                candidate + ".safetensors",
                candidate + ".ckpt",
                candidate + ".pt",
            ]
        )
    for probe in probes:
        try:
            resolved = folder_paths.get_full_path("loras", probe)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved and Path(resolved).is_file():
            return resolved
    return None


class NL2PromptLoRAStack:
    """契约 §5：按顺序逐个加载 LoRA 栈，单个失败跳过、不中断。"""

    CATEGORY = "NL2Prompt"
    FUNCTION = "load_stack"
    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("model", "clip")
    OUTPUT_NODE = False
    DESCRIPTION = "按 NL2PromptComposer 输出的 lora_stack 依次应用 LoRA；单个失败打印告警后跳过。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "lora_stack": ("LORA_STACK",),
                "enabled": ("BOOLEAN", {"default": True}),
                "strength_scale": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.01},
                ),
            },
            "optional": {
                "clip": ("CLIP",),
            },
        }

    def load_stack(
        self,
        model,
        lora_stack,
        enabled=True,
        strength_scale=1.0,
        clip=None,
    ):
        if not enabled:
            return (model, clip)

        entries = _normalize_lora_stack(lora_stack)
        if not entries:
            return (model, clip)

        try:
            import comfy.sd  # noqa: PLC0415 - 惰性导入，纯函数单测环境无 comfy
            import comfy.utils  # noqa: F401
            import folder_paths  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            _log(f"不在 ComfyUI 环境中，跳过 LoRA 加载：{exc}")
            return (model, clip)

        try:
            scale = float(strength_scale)
        except (TypeError, ValueError):
            scale = 1.0

        current_model = model
        current_clip = clip
        clip_available = clip is not None

        for name, model_strength, clip_strength in entries:
            try:
                path = _resolve_lora_path(name, folder_paths)
                if not path:
                    _log(f"跳过 LoRA「{name}」：在 loras 目录下未找到对应文件")
                    continue

                lora_data = comfy.utils.load_torch_file(path, safe_load=True)
                next_model, next_clip = comfy.sd.load_lora_for_models(
                    current_model,
                    current_clip if clip_available else None,
                    lora_data,
                    model_strength * scale,
                    clip_strength * scale,
                )
                current_model = next_model
                if clip_available:
                    current_clip = next_clip
                _log(f"已加载 LoRA「{name}」(model={model_strength * scale:g}, clip={clip_strength * scale:g})")
            except Exception as exc:  # noqa: BLE001 - 单个 LoRA 失败必须跳过
                _log(f"跳过 LoRA「{name}」：加载失败 {type(exc).__name__}: {exc}")
                continue

        return (current_model, current_clip)


# --------------------------------------------------------------------------- 导出

NODE_CLASS_MAPPINGS = {
    "NL2PromptComposer": NL2PromptComposer,
    "NL2PromptLoadLLM": NL2PromptLoadLLM,
    "NL2PromptLoRAStack": NL2PromptLoRAStack,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NL2PromptComposer": "自然语言提示词",
    "NL2PromptLoadLLM": "加载LLM",
    "NL2PromptLoRAStack": "LoRA 堆栈加载",
}


# --------------------------------------------------------------------------- v3 LLM 节点
# v3 阶段 2：三个 LLM 加载节点拆到 ``nodes_llm.py``（llm-runtime 负责）。
# 这里只做 re-export，让 ``__init__.py`` 能照旧从 nodes 模块取到类；
# 缺失/损坏时只打日志，不影响 ComfyUI 启动与其它节点。

try:  # 包内导入（ComfyUI 正常路径）
    from .nodes_llm import (  # noqa: F401
        NL2PromptLLMGGUF,
        NL2PromptLLMSafetensors,
        NL2PromptLLMCloud,
    )
except ImportError:  # 裸导入（单测 ``sys.path.insert(0, plugin); import nodes``）
    try:
        from nodes_llm import (  # type: ignore  # noqa: F401
            NL2PromptLLMGGUF,
            NL2PromptLLMSafetensors,
            NL2PromptLLMCloud,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"nodes_llm 不可用：{type(exc).__name__}: {exc}")
except Exception as exc:  # noqa: BLE001
    _log(f"nodes_llm 不可用：{type(exc).__name__}: {exc}")

# --------------------------------------------------------------------------- 卸载模型节点
# 单卡用户跑完出图后卸掉显存里的模型，把显存让给本地 LLM。
# 与 nodes_llm 一样：单独文件 + 这里 re-export，让 __init__.py 照旧从
# nodes 模块取到类；缺失/损坏只打日志，不影响其它节点。

try:  # 包内导入（ComfyUI 正常路径）
    from .nodes_unload import NL2PromptUnloadModels  # noqa: F401
except ImportError:  # 裸导入（单测 sys.path.insert(0, plugin); import nodes）
    try:
        from nodes_unload import NL2PromptUnloadModels  # type: ignore  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _log(f"nodes_unload 不可用：{type(exc).__name__}: {exc}")
except Exception as exc:  # noqa: BLE001
    _log(f"nodes_unload 不可用：{type(exc).__name__}: {exc}")
