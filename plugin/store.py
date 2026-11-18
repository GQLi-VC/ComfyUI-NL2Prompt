# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""自然语言提示词插件 —— 数据层（store）。

对外接口见 ``docs/接口契约.md`` §10：

    DATA_DIR
    load_all() -> {"characters": [], "styles": [], "negative_base": str, "settings": dict}
    default_system_prompt() -> str
    resolve_character(name) -> dict | None
    resolve_style(name) -> dict | None
    resolve_outfit(character, hint) -> (dict | None, list[dict])
    validate_lora_base_model(lora_name) -> {"ok": bool, "reason": str}
    catalog() -> dict

设计约束（来自任务书 / 契约 §8 ``store_corrupt``）：

* **任何异常都不得向外抛出**。文件缺失、JSON 损坏、结构怪异一律降级为
  内置最小默认值，并打印 ``[NL2Prompt][store] ...`` 告警。
* 模块级惰性加载 + 进程内缓存；``reload()`` 清缓存后可重新读取（供调试/热更新）。
* 只依赖标准库。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

__all__ = [
    "DATA_DIR",
    "CHARACTERS_FILE",
    "STYLES_FILE",
    "SETTINGS_FILE",
    "SECRETS_FILE",
    "SCHEMA_VERSION",
    "DEFAULT_NEGATIVE_BASE",
    "DEFAULT_LLAMA_SERVER_PATH",
    "DEFAULT_MODEL_ROOTS",
    "DEFAULT_SYSTEM_PROMPT",
    "load_all",
    "load_settings",
    "reload",
    "default_system_prompt",
    "resolve_character",
    "resolve_style",
    "resolve_outfit",
    "validate_lora_base_model",
    "catalog",
]

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #

DATA_DIR = Path(__file__).resolve().parent / "data"

CHARACTERS_FILE = DATA_DIR / "characters.json"
STYLES_FILE = DATA_DIR / "styles.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
#: 运行时由 routes.py 写入的密钥文件；**只读**引用，不在此创建、不入 workflow。
SECRETS_FILE = DATA_DIR / "secrets.json"

SCHEMA_VERSION = 1

#: 契约 §14.5：v2 起负面基础词默认**空串**，推荐值由
#: ``settings.json.default_negative_base`` 或 skill 文件提供（前端「填充推荐值」按钮）。
#: 全局只保留这一份定义；``nodes.py`` 引用本常量，不再自带长串。
DEFAULT_NEGATIVE_BASE = ""

#: v3：正面基础词（与负面基础词对称），默认空串。
#: 推荐值由 ``settings.json.default_positive_base`` 提供。
DEFAULT_POSITIVE_BASE = ""

#: v3：界面语言。``auto`` = 跟随浏览器；也可 ``zh`` / ``en``。
DEFAULT_UI_LOCALE = "auto"

#: v3：本地 LLM 运行偏好（由 llm-runtime 消费，本模块只负责存取）。
DEFAULT_LLM_LOCAL = {"dtype": "fp16", "unload_after_use": False}

DEFAULT_LLAMA_SERVER_PATH = ""  # 开源版留空：在「本地设置」里填你自己的 llama-server 路径

DEFAULT_MODEL_ROOTS = []  # 开源版留空：在「本地设置」里添加你自己的模型根目录

#: 内置**精简版** system prompt：仅在 ``skill_store`` 不可用时兜底。
#: 契约 §14.5「system prompt 内容迁入 skill 文件」——完整文案在
#: ``plugin/skills/anima-tag-master.json``，本常量只是最小可用替代，
#: 保证 skill 体系缺失时插件仍能工作。占位符由 ``llm_client`` 渲染。
DEFAULT_SYSTEM_PROMPT = """你是 Anima 文生图模型的提示词重写器。把用户的中文描述重写为英文 danbooru 标签提示词。

已知可用角色与服装：
{{character_presets}}

已知可用画师/风格：
{{style_presets}}

当前用户选择：
{{selected}}

规则：
1. 只注入当前选中服装的触发词，不要注入同角色其他服装的触发词。
2. 原样保留角色/服装/风格触发词，不增不减。
3. 用具体视觉事实替代空洞形容词。
4. 输出 JSON：{"positive":"...","negative":"...","analysis":"<中文简短说明>"}
5. 不要输出解释性文字，只输出 JSON。"""

#: 底模不兼容黑名单（大小写不敏感的子串判定）。
_BLACKLIST_BASE_TOKENS = ("illustrious", "sdxl", "pony")

_LOG_PREFIX = "[NL2Prompt][store]"

#: 上次告警列表（便于路由/调试接口读取，不影响主流程）。
_WARNINGS: list[str] = []

#: 惰性加载缓存，None = 尚未加载。
_cache: "dict | None" = None

#: skill_store 模块的惰性单例（契约 §14.2；由 skill-author 并行开发，可能缺失）
_SKILL_MODULE = None
_SKILL_MODULE_TRIED = False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _skill_store_module():
    """惰性加载同级 ``skill_store.py``（契约 §14.2）。

    由 skill-author 并行开发，**可能不存在/尚不可用**：任何异常都返回 ``None``，
    由调用方回落到内置精简版文案，绝不影响插件加载。
    """
    global _SKILL_MODULE, _SKILL_MODULE_TRIED
    if _SKILL_MODULE_TRIED:
        return _SKILL_MODULE
    _SKILL_MODULE_TRIED = True

    try:
        import importlib

        package = __package__ or ""
        if package:
            try:
                _SKILL_MODULE = importlib.import_module(".skill_store", package)
                return _SKILL_MODULE
            except Exception:  # noqa: BLE001 - 缺失即回落
                pass
        import importlib.util

        path = DATA_DIR.parent / "skill_store.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(
                "_anima_nl_skill_store_for_store", path
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _SKILL_MODULE = module
    except Exception as exc:  # noqa: BLE001 - skill 体系缺失必须降级
        _warn("skill_store 不可用 store_corrupt: %s" % (type(exc).__name__,))
        _SKILL_MODULE = None
    return _SKILL_MODULE


def _default_skill_dict():
    """取默认 skill 的完整 dict；取不到返回 ``None``。"""
    module = _skill_store_module()
    if module is None:
        return None

    for function_name in ("load_default_skill", "default_skill"):
        function = getattr(module, function_name, None)
        if callable(function):
            try:
                value = function()
                if isinstance(value, dict) and value:
                    return value
            except Exception:  # noqa: BLE001
                pass

    loader = getattr(module, "load_skill", None)
    if callable(loader):
        for argument in (None, "", "anima-tag-master"):
            try:
                value = loader(argument)
                if isinstance(value, dict) and value:
                    return value
            except Exception:  # noqa: BLE001
                continue
    return None


def _default_system_prompt_from_skill() -> str:
    """默认 system prompt：优先 skill_store 的默认 skill，取不到用内置精简版。"""
    module = _skill_store_module()
    if module is not None:
        for function_name in ("default_system_prompt", "default_prompt"):
            function = getattr(module, function_name, None)
            if callable(function):
                try:
                    value = function()
                    if isinstance(value, str) and value.strip():
                        return value
                except Exception:  # noqa: BLE001
                    pass

    skill = _default_skill_dict()
    if isinstance(skill, dict):
        value = skill.get("system_prompt")
        if isinstance(value, str) and value.strip():
            return value
    return DEFAULT_SYSTEM_PROMPT


def _safe_text(text):
    """把文本降级为当前 stdout/stderr 都能编码的字符串（红线：print 必须 GBK 安全）。

    中文 Windows 下 ComfyUI 的 stderr 是 gbk；把无法 gbk 编码的字符写进
    print 会抛 ``UnicodeEncodeError``，可能打死 ``prompt_worker`` 线程。
    """
    raw = text if isinstance(text, str) else str(text)
    for stream_name in ("stderr", "stdout"):
        stream = getattr(__import__("sys"), stream_name, None)
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            raw = raw.encode(encoding, "replace").decode(encoding, "replace")
        except Exception:  # noqa: BLE001 - 未知编码名等，跳过该流继续
            pass
    return raw


def _warn(message: str) -> None:
    """打印告警；自身也必须绝不抛异常，且必须 GBK 可编码。"""
    text = "%s %s" % (_LOG_PREFIX, _safe_text(message))
    try:
        _WARNINGS.append(text)
        # 只保留最近 50 条，避免长时间运行无限增长
        del _WARNINGS[:-50]
    except Exception:
        pass
    try:
        print(text)
    except Exception:
        pass


def _norm(value) -> str:
    """归一化用于匹配：转字符串、去首尾空白、转小写。"""
    try:
        if value is None:
            return ""
        return str(value).strip().lower()
    except Exception:
        return ""


def _text(value, default: str = "") -> str:
    try:
        if value is None:
            return default
        out = str(value).strip()
        return out if out else default
    except Exception:
        return default


def _tokens(value) -> list:
    """把 activation_words 一类字段归一为「去空白、去重」的字符串列表。

    兼容 ``["a", "b"]`` 与 ``"a, b"`` 两种写法（契约里二者都出现过）。
    """
    out: list = []
    try:
        if value is None:
            return out
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        for item in value:
            token = _text(item)
            if token and token not in out:
                out.append(token)
    except Exception:
        return out
    return out


def _strength(value, default: float = 0.0) -> float:
    """把 LoRA 强度转 float，非法值回落默认。"""
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _read_json(path: Path):
    """读 JSON 文件；任何异常都返回 None 并告警（不抛）。"""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        _warn("文件缺失 store_corrupt: %s -> 使用内置默认值" % (path.name,))
    except json.JSONDecodeError as exc:
        _warn(
            "JSON 解析失败 store_corrupt: %s (%s) -> 使用内置默认值"
            % (path.name, exc)
        )
    except OSError as exc:
        _warn("读取失败 store_corrupt: %s (%s) -> 使用内置默认值" % (path.name, exc))
    except Exception as exc:  # noqa: BLE001 - 数据层绝不上抛
        _warn(
            "未知错误 store_corrupt: %s (%r) -> 使用内置默认值" % (path.name, exc)
        )
    return None


def _list_payload(raw, key: str) -> list:
    """从 JSON 顶层取列表：兼容 ``[...]`` 与 ``{"<key>": [...]}``。"""
    try:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            value = raw.get(key)
            if isinstance(value, list):
                return value
    except Exception:
        pass
    return []


# --------------------------------------------------------------------------- #
# 默认值构造（内置最小默认值）
# --------------------------------------------------------------------------- #

def _default_settings() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "llama_server_path": DEFAULT_LLAMA_SERVER_PATH,
        "model_roots": list(DEFAULT_MODEL_ROOTS),
        "default_negative_base": DEFAULT_NEGATIVE_BASE,
        "default_positive_base": DEFAULT_POSITIVE_BASE,
        "default_system_prompt": "",
        "ui_locale": DEFAULT_UI_LOCALE,
        "llm_local": dict(DEFAULT_LLM_LOCAL),
    }


def _empty_data() -> dict:
    """内置最小默认值：空仓但结构完整，保证下游不崩。"""
    return {
        "characters": [],
        "styles": [],
        "negative_base": DEFAULT_NEGATIVE_BASE,
        "settings": _default_settings(),
    }


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

def _normalize_outfit(raw, index: int) -> dict:
    outfit = raw if isinstance(raw, dict) else {}
    default_id = "default" if index == 0 else "outfit%d" % (index + 1)
    outfit_id = _text(outfit.get("id"), default_id)
    lora_name = _text(outfit.get("lora_name")) or None
    return {
        "id": outfit_id,
        "display_name": _text(outfit.get("display_name"), outfit_id),
        "aliases": _tokens(outfit.get("aliases")),
        "activation_words": _tokens(outfit.get("activation_words")),
        "prompt": _text(outfit.get("prompt")),
        "negative_suppress": _tokens(outfit.get("negative_suppress")),
        "lora_name": lora_name,
        "lora_strength": _strength(outfit.get("lora_strength"), 0.0),
    }


def _normalize_character(raw) -> "dict | None":
    char = raw if isinstance(raw, dict) else {}
    display_name = _text(char.get("display_name"))
    char_id = _text(char.get("id"))
    if not char_id and not display_name:
        return None
    if not char_id:
        char_id = display_name
    if not display_name:
        display_name = char_id

    aliases = _tokens(char.get("aliases"))
    if display_name not in aliases:
        aliases.insert(0, display_name)

    outfits_raw = char.get("outfits")
    outfits: list = []
    if isinstance(outfits_raw, list):
        for idx, item in enumerate(outfits_raw):
            outfits.append(_normalize_outfit(item, idx))
    if not outfits:
        # 单套默认装扮：保证 §5 组装里的 default 语义永远成立
        outfits = [_normalize_outfit({"id": "default", "display_name": "默认"}, 0)]

    # 确保第一套（或 id=default 的那套）永远是 default
    if not any(o["id"] == "default" for o in outfits):
        outfits[0]["id"] = "default"

    return {
        "id": char_id,
        "display_name": display_name,
        "aliases": aliases,
        "lora_name": _text(char.get("lora_name")) or None,
        "lora_strength": _strength(char.get("lora_strength"), 0.8),
        "base_model": _text(char.get("base_model"), "anima"),
        "activation_words": _tokens(char.get("activation_words")),
        "outfits": outfits,
    }


def _normalize_style(raw) -> "dict | None":
    style = raw if isinstance(raw, dict) else {}
    display_name = _text(style.get("display_name"))
    style_id = _text(style.get("id"))
    if not style_id and not display_name:
        return None
    if not style_id:
        style_id = display_name
    if not display_name:
        display_name = style_id

    aliases = _tokens(style.get("aliases"))
    if display_name not in aliases:
        aliases.insert(0, display_name)

    return {
        "id": style_id,
        "display_name": display_name,
        "aliases": aliases,
        "base_model": _text(style.get("base_model"), "anima"),
        "lora_name": _text(style.get("lora_name")) or None,
        "lora_strength": _strength(style.get("lora_strength"), 0.0),
        "activation_words": _tokens(style.get("activation_words")),
        "prompt": _text(style.get("prompt")),
        "artist_tags": _tokens(style.get("artist_tags")),
    }


def _normalize_characters(raw) -> list:
    try:
        items = _list_payload(raw, "characters")
    except Exception:
        items = []
    out: list = []
    seen: set = set()
    for item in items:
        try:
            char = _normalize_character(item)
        except Exception as exc:  # noqa: BLE001
            _warn("角色条目归一化失败 store_corrupt: %r" % (exc,))
            char = None
        if not char:
            continue
        key = _norm(char["id"])
        if key in seen:
            _warn("角色 id 重复，已跳过: %s" % (char["id"],))
            continue
        seen.add(key)
        out.append(char)
    return out


def _normalize_styles(raw) -> list:
    try:
        items = _list_payload(raw, "styles")
    except Exception:
        items = []
    out: list = []
    seen: set = set()
    for item in items:
        try:
            style = _normalize_style(item)
        except Exception as exc:  # noqa: BLE001
            _warn("风格条目归一化失败 store_corrupt: %r" % (exc,))
            style = None
        if not style:
            continue
        key = _norm(style["id"])
        if key in seen:
            _warn("风格 id 重复，已跳过: %s" % (style["id"],))
            continue
        seen.add(key)
        out.append(style)
    return out


def _build_settings() -> dict:
    base = _default_settings()
    raw = _read_json(SETTINGS_FILE)
    if not isinstance(raw, dict):
        return base

    llama = _text(raw.get("llama_server_path"))
    if llama:
        base["llama_server_path"] = llama

    roots_raw = raw.get("model_roots")
    if isinstance(roots_raw, str):
        roots_raw = [roots_raw]
    if isinstance(roots_raw, list):
        roots = [r for r in (_text(x) for x in roots_raw) if r]
        if roots:
            base["model_roots"] = roots

    neg = _text(raw.get("default_negative_base"))
    if neg:
        base["default_negative_base"] = neg

    # v3：正面基础词
    pos = raw.get("default_positive_base")
    if isinstance(pos, str):
        base["default_positive_base"] = pos.strip()

    prompt = raw.get("default_system_prompt")
    if isinstance(prompt, str):
        base["default_system_prompt"] = prompt

    # v3：界面语言（auto / zh / en，非法值回落 auto）
    locale = _text(raw.get("ui_locale")).lower()
    if locale in ("auto", "zh", "en"):
        base["ui_locale"] = locale

    # v3：本地 LLM 偏好（缺字段用默认；类型不对也回落，绝不报错）
    llm_local = raw.get("llm_local")
    if isinstance(llm_local, dict):
        merged = dict(DEFAULT_LLM_LOCAL)
        dtype = _text(llm_local.get("dtype"))
        if dtype:
            merged["dtype"] = dtype
        unload = llm_local.get("unload_after_use")
        if isinstance(unload, bool):
            merged["unload_after_use"] = unload
        base["llm_local"] = merged

    return base


def _build_data() -> dict:
    settings = _build_settings()
    negative_base = settings.get("default_negative_base") or DEFAULT_NEGATIVE_BASE
    positive_base = settings.get("default_positive_base") or DEFAULT_POSITIVE_BASE
    return {
        "characters": _normalize_characters(_read_json(CHARACTERS_FILE)),
        "styles": _normalize_styles(_read_json(STYLES_FILE)),
        "negative_base": negative_base,
        "positive_base": positive_base,
        "settings": settings,
    }


# --------------------------------------------------------------------------- #
# 加载与缓存
# --------------------------------------------------------------------------- #

def load_all() -> dict:
    """返回 ``{"characters": [...], "styles": [...], "negative_base": str, "settings": {...}}``。

    首次调用读取磁盘并缓存；任何异常都降级为内置最小默认值（绝不抛出）。
    返回的是缓存本体，**调用方如需修改请自行 deepcopy**。
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = _build_data()
    except Exception as exc:  # noqa: BLE001 - 数据层兜底
        _warn("load_all 内部异常 store_corrupt: %r -> 使用内置默认值" % (exc,))
        _cache = _empty_data()
    return _cache


def load_settings() -> dict:
    """返回 settings.json 归一化后的副本（供 runtime.py 读 llama_server_path 等）。"""
    try:
        return copy.deepcopy(load_all().get("settings") or _default_settings())
    except Exception as exc:  # noqa: BLE001
        _warn("load_settings 异常 store_corrupt: %r" % (exc,))
        return _default_settings()


#: 允许经 ``save_settings`` 写入的键（白名单，防止把任意垃圾写进配置文件）
WRITABLE_SETTINGS = (
    "llama_server_path",
    "model_roots",
    "default_negative_base",
    "default_positive_base",
    "default_system_prompt",
    "ui_locale",
    "llm_local",
)

_UI_LOCALES = ("auto", "zh", "en")


def save_settings(patch: dict) -> dict:
    """把 ``patch`` 合并进 ``settings.json``（原子写）。

    v3 ``/nl2prompt/settings_ui`` 使用；只有 :data:`WRITABLE_SETTINGS` 白名单内的键
    会被写入，非法值一律拒绝并返回 ``ok: False``（不写盘）。

    :returns: ``{"ok": bool, "error": str, "settings": dict}``
    """
    try:
        current = load_settings()
    except Exception:  # noqa: BLE001
        current = _default_settings()

    if not isinstance(patch, dict):
        return {"ok": False, "error": "patch 必须是 JSON 对象", "settings": current}

    incoming = {}
    for key, value in patch.items():
        if key in WRITABLE_SETTINGS:
            incoming[key] = value

    if "ui_locale" in incoming:
        locale = _text(incoming.get("ui_locale")).lower()
        if locale not in _UI_LOCALES:
            return {
                "ok": False,
                "error": "ui_locale 只能是 auto / zh / en",
                "settings": current,
            }
        incoming["ui_locale"] = locale

    for key in (
        "llama_server_path",
        "default_negative_base",
        "default_positive_base",
        "default_system_prompt",
    ):
        if key in incoming and not isinstance(incoming[key], str):
            return {"ok": False, "error": "%s 必须是字符串" % key, "settings": current}

    if "llm_local" in incoming and not isinstance(incoming["llm_local"], dict):
        return {"ok": False, "error": "llm_local 必须是对象", "settings": current}

    if "model_roots" in incoming:
        roots = incoming["model_roots"]
        if isinstance(roots, str):
            roots = [roots]
        if not isinstance(roots, list):
            return {"ok": False, "error": "model_roots 必须是字符串或数组", "settings": current}
        incoming["model_roots"] = [_text(item) for item in roots if _text(item)]
        if not incoming["model_roots"]:
            incoming.pop("model_roots")

    merged = dict(current)
    merged.update(incoming)
    merged["schema"] = SCHEMA_VERSION

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(str(temp), str(SETTINGS_FILE))
    except OSError as exc:
        return {
            "ok": False,
            "error": "写入 settings.json 失败: %s" % (type(exc).__name__,),
            "settings": current,
        }

    # 清进程内缓存，让下一次 load_all() 读到新值
    global _cache
    _cache = None
    try:
        return {"ok": True, "error": "", "settings": load_settings()}
    except Exception:  # noqa: BLE001
        return {"ok": True, "error": "", "settings": merged}


def reload() -> dict:
    """清缓存并重新加载，返回新数据（供调试 / 热更新）。"""
    global _cache
    _cache = None
    return load_all()


def default_system_prompt() -> str:
    """默认 system prompt（契约 §14.5）。

    优先取 ``skill_store`` 的默认 skill 的 ``system_prompt``（完整文案在
    ``plugin/skills/*.json``）；skill 体系不可用时回落内置精简版。
    前端「恢复默认」按钮还原的即此值；占位符由 ``llm_client`` 渲染。
    """
    try:
        return _default_system_prompt_from_skill()
    except Exception as exc:  # noqa: BLE001 - 数据层绝不上抛
        _warn("读取 skill 默认文案失败 store_corrupt: %s" % (type(exc).__name__,))
        return DEFAULT_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def resolve_character(name: str) -> "dict | None":
    """按 ``id`` → ``display_name`` → ``aliases`` 的顺序匹配角色（大小写不敏感、去首尾空白）。

    无匹配（或 name 为空）返回 ``None``。返回深拷贝，避免调用方污染缓存。
    """
    try:
        key = _norm(name)
        if not key:
            return None
        characters = load_all().get("characters") or []

        for char in characters:
            if _norm(char.get("id")) == key:
                return copy.deepcopy(char)

        for char in characters:
            if _norm(char.get("display_name")) == key:
                return copy.deepcopy(char)

        for char in characters:
            for alias in char.get("aliases") or []:
                if _norm(alias) == key:
                    return copy.deepcopy(char)

        return None
    except Exception as exc:  # noqa: BLE001
        _warn("resolve_character 异常: %r" % (exc,))
        return None


def resolve_style(name: str) -> "dict | None":
    """按 ``id`` → ``display_name`` → ``aliases`` 的顺序匹配风格。无匹配返回 ``None``。"""
    try:
        key = _norm(name)
        if not key:
            return None
        styles = load_all().get("styles") or []

        for style in styles:
            if _norm(style.get("id")) == key:
                return copy.deepcopy(style)

        for style in styles:
            if _norm(style.get("display_name")) == key:
                return copy.deepcopy(style)

        for style in styles:
            for alias in style.get("aliases") or []:
                if _norm(alias) == key:
                    return copy.deepcopy(style)

        return None
    except Exception as exc:  # noqa: BLE001
        _warn("resolve_style 异常: %r" % (exc,))
        return None


def resolve_outfit(character: dict, hint: str) -> tuple:
    """解析服装。返回 ``(命中服装 或 默认服装 或 None, 同角色其它服装列表)``。

    * ``hint`` 为空 → ``(默认服装, 其它服装)``；默认服装 = ``id == "default"`` 的那套
      （数据里缺失时退回第一套）。
    * ``hint`` 非空 → 按 outfit 的 ``id`` / ``display_name`` 匹配（大小写不敏感、去空白）：
      * 命中 → ``(该服装, 其它服装)``
      * 未命中 → ``(None, 全部服装)``，调用方会把 hint 当普通提示词词条使用（契约 §5）。
    """
    try:
        char = character if isinstance(character, dict) else {}
        outfits = [o for o in (char.get("outfits") or []) if isinstance(o, dict)]
        if not outfits:
            return (None, [])

        default_outfit = None
        for outfit in outfits:
            if _norm(outfit.get("id")) == "default":
                default_outfit = outfit
                break
        if default_outfit is None:
            default_outfit = outfits[0]

        key = _norm(hint)
        if not key:
            siblings = [o for o in outfits if o is not default_outfit]
            return (copy.deepcopy(default_outfit), copy.deepcopy(siblings))

        for outfit in outfits:
            if _norm(outfit.get("id")) == key or _norm(outfit.get("display_name")) == key:
                siblings = [o for o in outfits if o is not outfit]
                return (copy.deepcopy(outfit), copy.deepcopy(siblings))

        return (None, copy.deepcopy(outfits))
    except Exception as exc:  # noqa: BLE001
        _warn("resolve_outfit 异常: %r" % (exc,))
        return (None, [])


# --------------------------------------------------------------------------- #
# 底模校验
# --------------------------------------------------------------------------- #

def validate_lora_base_model(lora_name: str) -> dict:
    """判定 LoRA 文件名的底模是否可能与 Anima 不兼容。

    * 含 ``(Illustrious)`` / ``(SDXL)`` / ``Illustrious`` / ``SDXL`` / ``pony``
      （大小写不敏感）→ ``{"ok": False, "reason": "base_model_mismatch"}``
    * 含 ``(ANI)`` 或 ``anima`` → ``{"ok": True, "reason": "anima"}``
    * 其余 → ``{"ok": True, "reason": "unknown_base_model"}``（不阻断，但标记待确认）
    """
    try:
        name = _text(lora_name)
        if not name:
            return {"ok": True, "reason": "unknown_base_model"}
        low = name.lower()

        for token in _BLACKLIST_BASE_TOKENS:
            if token in low:
                return {"ok": False, "reason": "base_model_mismatch"}

        if "(ani)" in low or "\uff08ani\uff09" in low or "anima" in low:
            return {"ok": True, "reason": "anima"}

        return {"ok": True, "reason": "unknown_base_model"}
    except Exception as exc:  # noqa: BLE001
        _warn("validate_lora_base_model 异常: %r" % (exc,))
        return {"ok": True, "reason": "unknown_base_model"}


# --------------------------------------------------------------------------- #
# 目录（供 GET /nl2prompt/catalog）
# --------------------------------------------------------------------------- #

def catalog() -> dict:
    """契约 §6 ``/catalog`` 响应体：给前端自动补全用的精简结构。"""
    try:
        data = load_all()
        characters = []
        for char in data.get("characters") or []:
            characters.append(
                {
                    "id": char.get("id"),
                    "display_name": char.get("display_name"),
                    "aliases": list(char.get("aliases") or []),
                    "outfits": [
                        {
                            "id": outfit.get("id"),
                            "display_name": outfit.get("display_name"),
                        }
                        for outfit in (char.get("outfits") or [])
                    ],
                }
            )

        styles = []
        for style in data.get("styles") or []:
            styles.append(
                {
                    "id": style.get("id"),
                    "display_name": style.get("display_name"),
                    "aliases": list(style.get("aliases") or []),
                    "lora_name": style.get("lora_name"),
                    "artist_tags": list(style.get("artist_tags") or []),
                }
            )

        return {"characters": characters, "styles": styles}
    except Exception as exc:  # noqa: BLE001
        _warn("catalog 异常: %r" % (exc,))
        return {"characters": [], "styles": []}
