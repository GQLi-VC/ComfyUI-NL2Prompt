# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""NL2Prompt 插件 - Skill 存储层（契约 14.2）。

职责
----
* ``plugin/skills/index.json`` + ``plugin/skills/<id>.json`` 的只读加载与幂等缓存；
* 编辑保存：写前校验、原子替换、失败回滚 ``.bak``；
* **模块导入绝不抛异常**：文件缺失 / JSON 损坏 / 字段非法一律降级到内置最小 skill，
  并打印 ``[nl2prompt][skills]`` 告警（日志只使用 GBK 可编码字符，v1 曾因 ``U+203A``
  导致 ComfyUI 工作线程死亡）。

对外函数（契约固定签名）
------------------------
``SKILLS_DIR`` / ``list_skills()`` / ``default_skill_id()`` / ``load_skill(skill_id=None)``
/ ``reload()`` / ``save_skill(skill_id, content)`` / ``validate_skill(content)``
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

#: skill 目录：``plugin/skills``。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: 索引文件名。
INDEX_FILE = SKILLS_DIR / "index.json"

#: 缺省 skill id（与 index.json 的 ``default`` 保持一致）。
DEFAULT_SKILL_ID = "anima-tag-master"

#: Anima 标签顺序（契约 14.2 固定 9 槽）。
DEFAULT_TAG_ORDER = [
    "quality",
    "subject",
    "character",
    "outfit",
    "pose",
    "scene",
    "camera",
    "lighting",
    "style",
]

#: 日志前缀（ASCII，GBK 可编码）。
_LOG_PREFIX = "[nl2prompt][skills]"

#: skill id 只允许文件名安全字符，防止目录穿越。
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: 允许写盘的扩展名。
_SKILL_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")

#: 必填字段 -> 期望类型（``int`` 单独判定，避免 ``bool`` 被当成数字）。
_REQUIRED = (
    "id",
    "display_name",
    "target_model",
    "version",
    "system_prompt",
    "tag_order",
    "quality_tags",
    "negative_base",
    "max_tags",
    "forbidden_phrases",
    "checker",
    "examples",
    "analysis_hint",
)

_REQUIRED_STR = (
    "id",
    "display_name",
    "target_model",
    "version",
    "system_prompt",
    "negative_base",
    "checker",
    "analysis_hint",
)

_REQUIRED_LIST = (
    "tag_order",
    "quality_tags",
    "forbidden_phrases",
)

#: 内置最小 skill：文件全坏时仍然可用，且保持与 anima-tag-master 相同的 JSON 输出契约。
FALLBACK_SYSTEM_PROMPT = (
    "你是 Anima 文生图模型的提示词重写器。把用户的自然语言描述重写为 Anima 更容易执行的提示词。\n"
    "\n"
    "已知可用角色与服装：\n"
    "{{character_presets}}\n"
    "\n"
    "已知可用画师/风格：\n"
    "{{style_presets}}\n"
    "\n"
    "当前用户选择：\n"
    "{{selected}}\n"
    "\n"
    "规则：\n"
    "1. 只输出 JSON：{\"positive\":\"...\",\"negative\":\"...\",\"analysis\":\"...\"}，"
    "不要输出解释文字、不要 markdown 代码块。\n"
    "2. positive 与 negative 是全英文 danbooru 标签，逗号分隔；禁止中文。\n"
    "3. analysis 是给用户看的中文短说明（不超过 60 字），绝不混入 positive 或 negative。\n"
    "4. 用户给定的角色/服装/风格触发词原样保留，不增不减不改写。\n"
    "5. 只注入当前选中服装的标签，禁止混入同角色其它服装。\n"
    "6. 用可观察的视觉事实替代空洞形容词（禁止 beautiful/amazing/stunning）。\n"
    "7. 权重写作 (tag:1.2)，取值 0.5 到 1.5，不要滥用。\n"
    "8. 信息不足时按合理默认补全（单人、半身以上构图、清晰光照），不要反问。"
)

FALLBACK_NEGATIVE_BASE = (
    "worst quality, low quality, lowres, blurry, jpeg artifacts, "
    "bad anatomy, bad hands, extra digits, watermark, signature, english text"
)

#: 惰性缓存；``None`` 表示尚未加载。
_CACHE_INDEX = None
_CACHE_SKILLS = {}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _safe_print(text: str) -> None:
    """打印告警：先尝试 GBK，编不进去就退回 ASCII 替换，**绝不抛异常**。"""
    try:
        print(text)
        return
    except Exception:
        pass
    try:
        encoding = getattr(__import__("sys").stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))
    except Exception:
        pass


def _warn(message: str) -> None:
    """统一告警格式；自身绝不抛异常。"""
    try:
        _safe_print("%s %s" % (_LOG_PREFIX, message))
    except Exception:
        pass


def _text(value, default: str = "") -> str:
    try:
        if value is None:
            return default
        out = str(value).strip()
        return out if out else default
    except Exception:
        return default


def _str_list(value) -> "list[str]":
    """把 list/tuple/str 归一成去空白的字符串列表（str 用逗号切分）。"""
    out = []
    try:
        if value is None:
            return out
        if isinstance(value, str):
            value = value.replace("\n", ",").split(",")
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        for item in value:
            token = _text(item)
            if token:
                out.append(token)
    except Exception:
        return []
    return out


def _read_json(path: Path):
    """读取 JSON，返回 ``(data, error)``；任何异常都不抛出。"""
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, "文件缺失: %s" % (Path(path).name,)
    except json.JSONDecodeError as exc:
        return None, "JSON 解析失败: %s (%s)" % (Path(path).name, exc)
    except OSError as exc:
        return None, "读取失败: %s (%s)" % (Path(path).name, exc)
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不抛
        return None, "读取异常: %s (%s)" % (Path(path).name, exc)


def _fallback_skill(skill_id: str = None) -> dict:
    """内置最小 skill（深拷贝使用，调用方可随意修改）。"""
    return {
        "id": _text(skill_id) or DEFAULT_SKILL_ID,
        "display_name": "Anima 标签大师（内置兜底）",
        "target_model": "anima",
        "version": "2.0.0-fallback",
        "system_prompt": FALLBACK_SYSTEM_PROMPT,
        "tag_order": list(DEFAULT_TAG_ORDER),
        "quality_tags": ["masterpiece", "best quality", "highres"],
        "negative_base": FALLBACK_NEGATIVE_BASE,
        "max_tags": 80,
        "forbidden_phrases": [
            "beautiful",
            "amazing",
            "masterpiece of art",
            "stunning",
            "gorgeous",
        ],
        "checker": "anima_v1",
        "examples": [],
        "analysis_hint": "用一句中文（不超过 60 字）说明本轮改了什么。",
        "builtin": True,
    }


def _normalize_skill(content: dict, skill_id: str = "") -> dict:
    """已知合法的 skill 补齐缺省字段，保证下游能稳定 ``dict.get``。"""
    skill = dict(content)
    sid = _text(skill.get("id")) or _text(skill_id) or DEFAULT_SKILL_ID
    skill["id"] = sid
    skill["display_name"] = _text(skill.get("display_name"), sid)
    skill["target_model"] = _text(skill.get("target_model"), "anima")
    skill["version"] = _text(skill.get("version"), "1.0.0")
    skill["system_prompt"] = _text(skill.get("system_prompt"))
    tag_order = _str_list(skill.get("tag_order"))
    skill["tag_order"] = tag_order or list(DEFAULT_TAG_ORDER)
    skill["quality_tags"] = _str_list(skill.get("quality_tags"))
    skill["negative_base"] = _text(skill.get("negative_base"))
    skill["forbidden_phrases"] = _str_list(skill.get("forbidden_phrases"))
    skill["checker"] = _text(skill.get("checker"), "anima_v1")
    skill["analysis_hint"] = _text(skill.get("analysis_hint"))
    try:
        max_tags = int(skill.get("max_tags"))
    except (TypeError, ValueError):
        max_tags = 80
    skill["max_tags"] = max_tags if max_tags > 0 else 80
    examples = skill.get("examples")
    skill["examples"] = examples if isinstance(examples, list) else []
    return skill


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def validate_skill(content: dict) -> dict:
    """校验 skill 结构，返回 ``{"ok": bool, "errors": [str]}``；绝不抛异常。"""
    errors = []

    def _err(message):
        errors.append(message)

    try:
        if not isinstance(content, dict):
            return {"ok": False, "errors": ["content 必须是 JSON 对象"]}

        for field in _REQUIRED:
            if field not in content:
                _err("缺少必填字段: %s" % field)

        for field in _REQUIRED_STR:
            if field not in content:
                continue
            value = content.get(field)
            if not isinstance(value, str):
                _err("%s 必须是字符串" % field)
            elif field in ("id", "system_prompt") and not value.strip():
                _err("%s 不能为空" % field)

        for field in _REQUIRED_LIST:
            if field not in content:
                continue
            value = content.get(field)
            if not isinstance(value, (list, tuple)):
                _err("%s 必须是字符串数组" % field)
                continue
            if field == "tag_order" and not value:
                _err("tag_order 不能为空")
            seen = set()
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    _err("%s 的元素必须是非空字符串" % field)
                    break
                key = item.strip().lower()
                if key in seen and field == "tag_order":
                    _err("tag_order 存在重复槽位: %s" % item.strip())
                    break
                seen.add(key)

        if "max_tags" in content:
            value = content.get("max_tags")
            if isinstance(value, bool) or not isinstance(value, int):
                _err("max_tags 必须是整数")
            elif not 10 <= value <= 400:
                _err("max_tags 必须在 10 到 400 之间")

        if "id" in content and isinstance(content.get("id"), str):
            if not _SKILL_ID_RE.match(content["id"].strip()):
                _err("id 只能包含小写字母/数字/点/下划线/连字符")

        if "examples" in content:
            examples = content.get("examples")
            if not isinstance(examples, list):
                _err("examples 必须是数组")
            elif not examples:
                _err("examples 至少需要 1 条 few-shot")
            else:
                for index, item in enumerate(examples):
                    if not isinstance(item, dict):
                        _err("examples[%d] 必须是对象" % index)
                        continue
                    for key in ("positive", "negative"):
                        if not isinstance(item.get(key), str) or not item.get(key).strip():
                            _err("examples[%d].%s 必须是非空字符串" % (index, key))
                    if "input" in item and not isinstance(item.get("input"), str):
                        _err("examples[%d].input 必须是字符串" % index)

        if "checker" in content and isinstance(content.get("checker"), str):
            if not content["checker"].strip():
                _err("checker 不能为空")

        if "system_prompt" in content and isinstance(content.get("system_prompt"), str):
            if "{{" in content["system_prompt"]:
                for placeholder in ("{{character_presets}}", "{{style_presets}}", "{{selected}}"):
                    if placeholder not in content["system_prompt"]:
                        _warn("system_prompt 占位符不完整，缺少 %s" % placeholder)
    except Exception as exc:  # noqa: BLE001 - 校验器自身绝不抛
        errors.append("校验异常: %s" % exc)

    return {"ok": not errors, "errors": errors}


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #

def _scan_skill_files() -> "list[dict]":
    """index.json 不可用时的兜底扫描：直接读目录里每个 *.json 的头部字段。"""
    entries = []
    try:
        paths = sorted(SKILLS_DIR.glob("*.json"))
    except Exception:
        return entries
    # 索引文件本身不是 skill（同时兼容 INDEX_FILE 被重定向的情形）
    reserved = {"index.json", INDEX_FILE.name}
    for path in paths:
        if path.name in reserved:
            continue
        data, error = _read_json(path)
        if error or not isinstance(data, dict):
            continue
        skill_id = _text(data.get("id"), path.stem)
        entries.append(
            {
                "id": skill_id,
                "display_name": _text(data.get("display_name"), skill_id),
                "target_model": _text(data.get("target_model"), "anima"),
                "file": path.name,
            }
        )
    return entries


def _index_data() -> dict:
    """读取（并缓存）index.json，返回规整后的索引对象；损坏时用兜底结果。"""
    global _CACHE_INDEX
    if _CACHE_INDEX is not None:
        return _CACHE_INDEX

    data, error = _read_json(INDEX_FILE)
    if error:
        _warn("%s -> 尝试扫描目录" % error)
    if not isinstance(data, dict):
        data = {}

    skills = []
    raw = data.get("skills")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            skill_id = _text(item.get("id"))
            if not skill_id:
                continue
            skills.append(
                {
                    "id": skill_id,
                    "display_name": _text(item.get("display_name"), skill_id),
                    "target_model": _text(item.get("target_model"), "anima"),
                    "file": _text(item.get("file"), skill_id + ".json"),
                }
            )
    if not skills:
        skills = _scan_skill_files()
    if not skills:
        _warn("未发现任何 skill 文件 -> 使用内置 fallback")
        skills = [
            {
                "id": DEFAULT_SKILL_ID,
                "display_name": "Anima 标签大师（内置兜底）",
                "target_model": "anima",
                "file": "",
            }
        ]

    default_id = _text(data.get("default"))
    ids = [item["id"] for item in skills]
    if default_id not in ids:
        if default_id:
            _warn("index.default=%s 不存在 -> 回退 %s" % (default_id, ids[0]))
        default_id = ids[0]

    index = {"schema": data.get("schema", 1), "default": default_id, "skills": skills}
    _CACHE_INDEX = index
    return index


def index_data() -> dict:
    """只读副本：``{"schema","default","skills"}``（供路由组装 JSON）。"""
    try:
        return copy.deepcopy(_index_data())
    except Exception:
        return {"schema": 1, "default": DEFAULT_SKILL_ID, "skills": []}


def _skill_path(skill_id: str, index: dict) -> Path:
    """定位 skill 文件；文件名必须落在 SKILLS_DIR 内。"""
    filename = ""
    for item in index.get("skills") or []:
        if item.get("id") == skill_id:
            filename = _text(item.get("file"))
            break
    if not filename:
        filename = skill_id + ".json"
    if not _SKILL_FILE_RE.match(filename):
        raise ValueError("非法 skill 文件名: %s" % filename)
    return SKILLS_DIR / filename


def skill_file(skill_id: str) -> Path:
    """返回某个 skill 的磁盘路径（不存在也返回理论路径）。"""
    try:
        return _skill_path(_text(skill_id) or DEFAULT_SKILL_ID, _index_data())
    except Exception:
        return SKILLS_DIR / ((_text(skill_id) or DEFAULT_SKILL_ID) + ".json")


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #

def list_skills() -> "list[dict]":
    """列出可用 skill：``[{"id","display_name","target_model","file"}]``。"""
    try:
        index = _index_data()
        out = []
        for item in index.get("skills") or []:
            out.append(
                {
                    "id": item["id"],
                    "display_name": item["display_name"],
                    "target_model": item["target_model"],
                    "file": item["file"],
                }
            )
        return out
    except Exception as exc:
        _warn("list_skills 异常: %s -> fallback" % exc)
        return [
            {
                "id": DEFAULT_SKILL_ID,
                "display_name": "Anima 标签大师（内置兜底）",
                "target_model": "anima",
                "file": "",
            }
        ]


def default_skill_id() -> str:
    """返回默认 skill id（index.default，失效时回退内置 id）。"""
    try:
        return _text(_index_data().get("default"), DEFAULT_SKILL_ID) or DEFAULT_SKILL_ID
    except Exception:
        return DEFAULT_SKILL_ID


def load_skill(skill_id: str = None) -> dict:
    """加载 skill；任何失败都返回内置 fallback，**不抛异常**。"""
    try:
        index = _index_data()
        sid = _text(skill_id) or default_skill_id()
        cached = _CACHE_SKILLS.get(sid)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        path = _skill_path(sid, index)
        data, error = _read_json(path)
        if error:
            _warn("%s -> fallback" % error)
            return copy.deepcopy(_fallback_skill(sid))
        if not isinstance(data, dict):
            _warn("%s 不是 JSON 对象 -> fallback" % path.name)
            return copy.deepcopy(_fallback_skill(sid))
        report = validate_skill(data)
        if not report["ok"]:
            _warn(
                "%s 校验失败 (%s) -> fallback"
                % (path.name, "; ".join(report["errors"][:3]))
            )
            return copy.deepcopy(_fallback_skill(sid))

        skill = _normalize_skill(data, sid)
        _CACHE_SKILLS[sid] = skill
        return copy.deepcopy(skill)
    except Exception as exc:  # noqa: BLE001 - 导入与运行期都不得抛
        _warn("load_skill 异常: %s -> fallback" % exc)
        return copy.deepcopy(_fallback_skill(skill_id))


def reload() -> None:
    """丢弃缓存，下次访问重新读盘。"""
    global _CACHE_INDEX
    try:
        _CACHE_INDEX = None
        _CACHE_SKILLS.clear()
    except Exception:
        pass


def save_skill(skill_id: str, content: dict) -> dict:
    """校验并保存 skill，返回 ``{"ok": bool, "error": str}``。

    写入流程：校验 -> 备份 ``.bak`` -> 临时文件原子替换 -> 回读校验。
    回读失败时从 ``.bak`` 回滚。
    """
    try:
        sid = _text(skill_id)
        if not sid or not _SKILL_ID_RE.match(sid):
            return {"ok": False, "error": "非法 skill id: %r" % (skill_id,)}
        if not isinstance(content, dict):
            return {"ok": False, "error": "content 必须是 JSON 对象"}

        payload = copy.deepcopy(content)
        payload["id"] = sid
        report = validate_skill(payload)
        if not report["ok"]:
            return {"ok": False, "error": "; ".join(report["errors"])}

        try:
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            target = _skill_path(sid, _index_data())
        except Exception as exc:
            return {"ok": False, "error": "无法定位目标文件: %s" % exc}

        backup = target.with_name(target.name + ".bak")
        had_previous = target.is_file()
        if had_previous:
            try:
                backup.write_bytes(target.read_bytes())
            except OSError as exc:
                return {"ok": False, "error": "备份失败: %s" % exc}

        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(str(tmp), str(target))
        except OSError as exc:
            _rollback(target, backup, had_previous)
            return {"ok": False, "error": "写入失败: %s" % exc}

        # 回读校验，防止磁盘/编码层面写坏
        written, error = _read_json(target)
        if error or not isinstance(written, dict) or not validate_skill(written)["ok"]:
            _rollback(target, backup, had_previous)
            return {
                "ok": False,
                "error": "写入后校验失败，已回滚: %s" % (error or "字段非法"),
            }

        skill = _normalize_skill(written, sid)
        _CACHE_SKILLS[sid] = skill
        _sync_index(sid, skill)
        return {"ok": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 - 保存接口绝不抛
        return {"ok": False, "error": "保存异常: %s" % exc}


def _rollback(target: Path, backup: Path, had_previous: bool) -> None:
    """把 ``.bak`` 恢复回目标文件；回滚本身也绝不抛异常。"""
    try:
        if had_previous and backup.is_file():
            os.replace(str(backup), str(target))
        elif target.is_file():
            target.unlink()
    except Exception as exc:  # noqa: BLE001
        _warn("回滚失败: %s" % exc)


def _sync_index(skill_id: str, skill: dict) -> None:
    """保存后确保 index.json 收录该 id（新建 skill 时才落盘）。"""
    try:
        index = _index_data()
        ids = [item.get("id") for item in index.get("skills") or []]
        if skill_id in ids:
            return
        data, error = _read_json(INDEX_FILE)
        if error or not isinstance(data, dict):
            return
        skills = data.get("skills")
        if not isinstance(skills, list):
            skills = []
        skills.append(
            {
                "id": skill_id,
                "display_name": skill.get("display_name") or skill_id,
                "target_model": skill.get("target_model") or "anima",
                "file": skill_file(skill_id).name,
            }
        )
        data["skills"] = skills
        INDEX_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        reload()
    except Exception as exc:  # noqa: BLE001
        _warn("更新 index 失败: %s" % exc)
