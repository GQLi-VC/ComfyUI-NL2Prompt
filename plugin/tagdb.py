# -*- coding: utf-8 -*-
"""NL2Prompt 的 danbooru 标签库访问层（只读）。

定位（务必读一遍，不要把它用成"生成器"）
----------------------------------------
本模块**只做校验与归一**：判断一个 LLM 写出来的标签是否真实存在、拼写是否正确、
是不是某个标准名的别名、要不要用更热门的写法。**它不负责"该画什么"**——
选哪些标签仍然只能由 LLM 理解语义后决定。

数据库由 ``tools/build_tagdb.py`` 构建，位于 ``plugin/assets/tagdb/danbooru.db``。

对外接口
--------
``available()`` / ``status()`` / ``reload()``
``lookup(token)``
``normalize_token(token, whitelist=(), min_conf=...)``
``normalize_text(text, whitelist=(), min_conf=...)``
``search_zh(query, limit=10)``

硬约束
------
1. **绝不抛异常**：DB 缺失 / 损坏 / 磁盘只读 / SQL 失败，一律降级为
   "该功能不可用"（``skipped=True`` 或返回原文），并打一行 GBK 安全告警。
   标签库是增强功能，任何时候都不允许它导致出图失败。
2. **只读**：``file:...?mode=ro&immutable=1``，不做任何写入。
3. **线程本地连接**：ComfyUI 是多线程执行的，每个线程各自持一个连接。
4. **GBK 红线**：所有日志只有 ASCII 与中文，禁止箭头 / 对勾 / emoji
   （ComfyUI 的 stderr 是 GBK，非 GBK 字符会打死工作线程）。
5. **永不静默丢弃**：``normalize_token`` 只会给出 keep / replace / suggest / unknown
   四种动作，未知标签原样保留，由调用方决定怎么处理。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote

__all__ = [
    "DATA_DB",
    "SCHEMA_VERSION",
    "CATEGORY_NAMES",
    "CATEGORY_PRIORITY",
    "CONF_EXACT",
    "CONF_ALIAS",
    "CONF_ALIAS_AMBIGUOUS",
    "CONF_ALIAS_PROPER_NOUN",
    "CONF_PLURAL",
    "CONF_FTS",
    "CONF_FUZZY",
    "AGENT_MIN_RATIO",
    "available",
    "status",
    "reload",
    "lookup",
    "normalize_token",
    "normalize_text",
    "search_zh",
    "suggest_candidates",
    "build_agent_feedback",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 数据库路径（测试可覆盖后调用 reload()）。
#: 放在 ``plugin/assets/`` 而不是 ``plugin/data/``：后者是**运行时数据**
#: （settings.json / secrets.json / logs/），静态只读资源必须与它物理隔离，
#: 否则打包发布时很容易把用户凭据一起带出去。
DATA_DB = Path(__file__).resolve().parent / "assets" / "tagdb" / "danbooru.db"

#: 支持的 schema 版本（DB 里 meta.schema_version 超过它则视为不兼容）。
SCHEMA_VERSION = 1

CATEGORY_NAMES = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}

#: 别名消歧的类别优先级（数字越小越优先）：属性词 > 角色 > 作品 > 画师 > 元标签。
CATEGORY_PRIORITY = {0: 0, 4: 1, 3: 2, 1: 3, 5: 4}

#: 置信度阈值（与 docs/danbooru-tagdb-设计.md §4.3 一致）。
CONF_EXACT = 1.00
CONF_ALIAS = 0.95
CONF_ALIAS_AMBIGUOUS = 0.70
CONF_PLURAL = 0.85
CONF_FTS = 0.70
CONF_FUZZY = 0.50

#: 别名只指向专有名词（artist / copyright / character）时的置信度。
#: 低于 DEFAULT_MIN_CONFIDENCE，所以只会变成 suggest，不会被自动替换。
#:
#: 背景：库里 149,093 个别名（89.6%）**没有**同名 general 类候选，只挂在
#: artist / character / copyright 上。此时把通用描述词改写成专有名词是错的
#: （用户写 "neon" 想要霓虹光效，却被改成 Nikke 的角色名 neon_(nikke)），
#: 所以降级为只建议。详见 _lookup_alias()。
CONF_ALIAS_PROPER_NOUN = 0.50

#: 默认替换阈值：高于它才自动改写，低于它只给建议。
DEFAULT_MIN_CONFIDENCE = CONF_PLURAL

#: 白名单：这些词即便不在 danbooru 里也属于合法提示词（扩散模型生态通用质量词）。
DEFAULT_WHITELIST = (
    # skill.quality_tags 的默认值
    "masterpiece", "best quality", "very aesthetic", "highres",
    # 常见 SD / NovelAI / Anima 质量词
    "best_quality", "good quality", "normal quality", "worst quality", "low quality",
    "absurdres", "high resolution", "ultra detailed", "highly detailed",
    "artist name", "signature", "watermark", "jpeg artifacts",
    # 负面基础词里常用的非 danbooru 词
    "bad anatomy", "bad hands", "extra digits", "missing fingers",
    "worst_quality", "low_quality", "blurry", "bar censor", "cropped",
    "english text", "text",
)

#: 疑似幻觉：这些词既不在库里、又不像 danbooru 标签，多半是 AI 编的。
HALLUCINATION_TOKENS = (
    "ethereal", "cinematic lighting", "volumetric lighting", "hyperrealistic",
    "ultra detailed", "hyperdetailed", "intricate details", "octane render",
    "ray tracing", "unreal engine", "artstation", "trending on artstation",
    "8k", "4k", "1080p", "4k resolution", "highly detailed", "extremely detailed",
    "masterpiece of art", "best art", "award winning", "professional photography",
    "sharp focus", "depth of field",
)

#: 纯评价性形容词（既不是 danbooru 标签，也没有具体视觉信息）。
EMPTY_ADJECTIVES = (
    "beautiful", "amazing", "stunning", "gorgeous", "elegant", "pretty", "cute",
    "lovely", "nice", "wonderful", "perfect", "awesome", "charming",
)

_LOG_PREFIX = "[nl2prompt][tagdb]"

#: 只读 URI 打开时用的参数。
_URI_SUFFIX = "?mode=ro&immutable=1"

#: 单次查询超时（秒）——只读库正常都在毫秒级，这里只防极端情况。
_QUERY_TIMEOUT = 5.0

#: 白名单归一化集合（小写、下划线化），惰性构建。
_WHITELIST_SET = set()
_WHITELIST_LOCK = threading.Lock()

#: 线程本地连接。
_LOCAL = threading.local()

#: status 缓存（path, mtime, size）-> status dict。
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()

#: 告警去重（避免每帧刷屏）。
_WARNED = set()


# ---------------------------------------------------------------------------
# 日志（GBK 安全）
# ---------------------------------------------------------------------------

def _safe_log(prefix, message):
    try:
        print("%s %s%s" % (_LOG_PREFIX, prefix, message))
    except Exception:
        # 连 print 都失败（编码/管道断裂）也不能让调用方崩
        try:
            print("%s warning suppressed" % _LOG_PREFIX)
        except Exception:
            pass


def _warn(message):
    try:
        key = str(message)[:120]
        if key in _WARNED:
            return
        if len(_WARNED) > 200:
            _WARNED.clear()
        _WARNED.add(key)
    except Exception:
        pass
    _safe_log("WARN ", message)


def _info(message):
    _safe_log("INFO ", message)


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------

#: ``(white thighhighs:1.2)`` 形式的权重语法。
_WEIGHT_RE = re.compile(r"^\(\s*(?P<name>[^:()]+?)\s*:\s*(?P<weight>[0-9]*\.?[0-9]+)\s*\)$")
#: ``(white thighhighs)`` 形式的纯包裹。
_PAREN_RE = re.compile(r"^\(\s*(?P<name>[^()]+?)\s*\)$")
#: 转义字符 ``\(`` ``\)`` ``\\``。
_ESCAPE_RE = re.compile(r"\\(.)")
#: 空白与连字符统一成下划线。
_SEPARATOR_RE = re.compile(r"[\s\-]+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def split_decoration(token):
    """拆出权重包裹，返回 ``(bare, prefix, suffix)``，便于替换后原样还原。"""
    try:
        text = str(token or "").strip()
        if not text:
            return ("", "", "")
        match = _WEIGHT_RE.match(text)
        if match:
            return (match.group("name"), "(", ":%s)" % match.group("weight"))
        match = _PAREN_RE.match(text)
        if match:
            return (match.group("name"), "(", ")")
        return (text, "", "")
    except Exception:
        return (str(token or ""), "", "")


def normalization_key(token):
    """L0 归一化：去权重、去转义、空格/连字符转下划线、小写、去首尾标点。"""
    try:
        bare, _prefix, _suffix = split_decoration(token)
        text = _ESCAPE_RE.sub(r"\1", bare)
        text = text.strip().lower()
        text = _SEPARATOR_RE.sub("_", text)
        text = _MULTI_UNDERSCORE_RE.sub("_", text)
        text = text.strip("_,.;:!?。，")
        return text
    except Exception:
        return ""


def _whitelist_keys(extra_whitelist):
    """构建本词白名单（含内置默认 + 调用方传入的本次注入词）。"""
    keys = set()
    keys.update(_builtin_whitelist())
    for item in extra_whitelist or ():
        key = normalization_key(item)
        if key:
            keys.add(key)
    return keys


def _builtin_whitelist():
    global _WHITELIST_SET
    if _WHITELIST_SET:
        return _WHITELIST_SET
    with _WHITELIST_LOCK:
        if not _WHITELIST_SET:
            keys = set()
            for item in DEFAULT_WHITELIST:
                key = normalization_key(item)
                if key:
                    keys.add(key)
            _WHITELIST_SET = keys
    return _WHITELIST_SET


# ---------------------------------------------------------------------------
# 连接管理（线程本地、只读、绝不抛）
# ---------------------------------------------------------------------------

def _db_path():
    try:
        return Path(DATA_DB)
    except Exception:
        return None


def _open_connection(path):
    """打开只读连接；失败返回 (None, error_text)。"""
    try:
        uri = "file:%s%s" % (quote(path.as_posix(), safe="/:"), _URI_SUFFIX)
        connection = sqlite3.connect(uri, uri=True, timeout=_QUERY_TIMEOUT)
        connection.row_factory = sqlite3.Row
        # 冒烟查询：确认表真的在（防止拿到一个空文件/半截文件）
        connection.execute("SELECT 1 FROM tag LIMIT 1").fetchone()
        return (connection, "")
    except Exception as exc:  # noqa: BLE001 - 任何异常都不许外抛
        return (None, "%s: %s" % (type(exc).__name__, exc))


def _connection():
    """取当前线程的连接；不可用时返回 None（并只告警一次）。"""
    existing = getattr(_LOCAL, "connection", None)
    if existing is not None:
        return existing
    path = _db_path()
    if path is None or not path.is_file():
        _warn("标签库文件不存在，标签校验功能关闭：%s" % (path,))
        _LOCAL.failed = True
        return None
    connection, error = _open_connection(path)
    if connection is None:
        _warn("标签库打开失败，标签校验功能关闭：%s" % error)
        _LOCAL.failed = True
        return None
    _LOCAL.connection = connection
    _LOCAL.failed = False
    return connection


def _query(sql, params=(), fetchall=True):
    """执行只读查询。任何失败都返回空结果，不抛。"""
    connection = _connection()
    if connection is None:
        return [] if fetchall else None
    try:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if fetchall else cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        _warn("查询失败（返回空结果）：%s" % exc)
        return [] if fetchall else None


def _has_object(connection, name):
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
        return row is not None
    except Exception:
        return False


def reload():
    """清空当前线程的连接与 status 缓存（换库/重建 DB 后调用）。"""
    try:
        connection = getattr(_LOCAL, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        _LOCAL.connection = None
        _LOCAL.failed = False
    except Exception:
        pass
    with _STATUS_LOCK:
        _STATUS_CACHE.clear()


def available():
    """标签库是否可用（文件存在 + 能打开 + 表结构完整）。"""
    return bool(status().get("available"))


def status():
    """返回标签库状态字典；任何异常都降级为 ``available=False``。"""
    result = {
        "ok": True,
        "available": False,
        "path": "",
        "exists": False,
        "bytes": 0,
        "mib": 0.0,
        "schema": 0,
        "built_at": "",
        "tag_count": 0,
        "alias_count": 0,
        "zh_count": 0,
        "features": {"tag_fts": False, "zh_fts_trigram": False},
        "error": "",
    }
    try:
        path = _db_path()
        if path is None:
            result["error"] = "bad_path"
            return result
        result["path"] = str(path)
        if not path.is_file():
            result["error"] = "db_missing"
            return result
        result["exists"] = True
        try:
            stat = path.stat()
            result["bytes"] = int(stat.st_size)
            result["mib"] = round(stat.st_size / 1048576.0, 2)
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        except Exception:
            cache_key = None

        if cache_key is not None:
            with _STATUS_LOCK:
                cached = _STATUS_CACHE.get(cache_key)
            if cached is not None:
                merged = dict(cached)
                merged.update({"ok": True, "path": result["path"], "exists": True})
                merged["bytes"] = result["bytes"]
                merged["mib"] = result["mib"]
                return merged

        connection, error = _open_connection(path)
        if connection is None:
            result["error"] = error or "open_failed"
            return result
        try:
            meta = {}
            try:
                for row in connection.execute("SELECT key, value FROM meta"):
                    meta[str(row["key"])] = str(row["value"])
            except Exception:
                pass
            schema = 0
            try:
                schema = int(meta.get("schema_version") or 0)
            except Exception:
                schema = 0
            result["schema"] = schema
            result["built_at"] = meta.get("built_at", "")
            result["tag_count"] = _to_int(meta.get("tag_count"))
            result["alias_count"] = _to_int(meta.get("alias_count"))
            result["zh_count"] = _to_int(meta.get("zh_count"))
            result["features"]["tag_fts"] = _has_object(connection, "tag_fts")
            result["features"]["zh_fts_trigram"] = _has_object(connection, "zh_fts")

            if schema and schema > SCHEMA_VERSION:
                result["error"] = "schema_newer_than_client"
                return result
            if result["tag_count"] <= 0:
                # meta 缺记录时退化为直接 COUNT，仍以真实数据为准
                try:
                    result["tag_count"] = int(
                        connection.execute("SELECT COUNT(*) FROM tag").fetchone()[0])
                except Exception:
                    result["tag_count"] = 0
            result["available"] = result["tag_count"] > 0
        finally:
            try:
                connection.close()
            except Exception:
                pass

        if cache_key is not None:
            with _STATUS_LOCK:
                if len(_STATUS_CACHE) > 8:
                    _STATUS_CACHE.clear()
                _STATUS_CACHE[cache_key] = dict(result)
        return result
    except Exception as exc:  # noqa: BLE001
        result["available"] = False
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        return result


def _to_int(value):
    try:
        return int(float(str(value)))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# lookup：L1 - L5 五级匹配
# ---------------------------------------------------------------------------

def _row_to_result(row, via, confidence):
    category = _to_int(row["category"])
    post_count = _to_int(row["post_count"])
    # meta 类且 0 热度：这类词是**打标器词汇 / SD 模型名 / 已废弃标签**，不是 danbooru
    # 实义标签（实测 37 个：AbyssOrangeMix、Counterfeit、doujinshi(已废弃)、
    # best_quality(count=0) 等）。命中它们不能当正常结果静默替换，一律降置信度
    # 到 CONF_FUZZY（只建议不替换）并打 meta_only 标记。
    meta_only = (category == 5 and post_count <= 0)
    if meta_only and confidence > CONF_FUZZY:
        confidence = CONF_FUZZY
    return {
        "hit": True,
        "name": str(row["name"]),
        "zh": str(row["zh"] or "") if "zh" in row.keys() else "",
        "category": category,
        "category_name": CATEGORY_NAMES.get(category, str(category)),
        "post_count": post_count,
        "via": via,
        "confidence": confidence,
        "meta_only": meta_only,
    }


def _lookup_exact(key):
    row = _query(
        "SELECT name, category, post_count, zh FROM tag WHERE name = ? LIMIT 1",
        (key,), fetchall=False)
    return _row_to_result(row, "exact", CONF_EXACT) if row is not None else None


def _lookup_alias(key):
    """别名查：直接读构建时物化的 is_best，无需自己排序。

    但「只指向专有名词」的别名**不自动替换**：库里 89.6% 的别名没有同名
    general 类候选，只挂在 artist / character / copyright 上。这类别名一旦
    自动替换，会把通用描述词改写成专有名词（"neon" 变成 Nikke 的角色名
    ``neon_(nikke)``），画面会直接跑偏。所以此时把置信度压到
    ``CONF_ALIAS_PROPER_NOUN``，交给上层走 suggest（只提示不改写）。

    「候选里是否存在 general 类」不再额外查一次：``is_best`` 是构建时按
    ``CATEGORY_PRIORITY`` 物化的，general(0) 优先级最高，因此 best 行的
    ``category == 0`` 等价于「候选里存在 general 类标签」。
    """
    row = _query(
        "SELECT a.tag_name AS name, a.lang AS lang, a.ambiguous AS ambiguous, "
        "       t.category AS category, t.post_count AS post_count, t.zh AS zh "
        "FROM tag_alias a JOIN tag t ON t.name = a.tag_name "
        "WHERE a.alias = ? AND a.is_best = 1 LIMIT 1",
        (key,), fetchall=False)
    if row is None:
        return None
    category = _to_int(row["category"])
    ambiguous = _to_int(row["ambiguous"]) if "ambiguous" in row.keys() else 0
    proper_noun_only = category in (1, 3, 4)
    if proper_noun_only:
        confidence = CONF_ALIAS_PROPER_NOUN
    else:
        confidence = CONF_ALIAS_AMBIGUOUS if ambiguous else CONF_ALIAS
    result = _row_to_result(row, "alias", confidence)
    result["alias_lang"] = str(row["lang"] or "en") if "lang" in row.keys() else "en"
    result["ambiguous"] = bool(ambiguous)
    result["proper_noun_only"] = proper_noun_only
    return result


def _plural_variants(key):
    """L3 词形候选：单复数互转（danbooru 既有单数也有复数标准名）。"""
    variants = []
    try:
        if not key:
            return variants
        if key.endswith("ies") and len(key) > 4:
            variants.append(key[:-3] + "y")
        if key.endswith("ves") and len(key) > 4:
            variants.append(key[:-3] + "f")
            variants.append(key[:-3] + "fe")
        if key.endswith("es") and len(key) > 3:
            variants.append(key[:-2])
        if key.endswith("s") and len(key) > 2:
            variants.append(key[:-1])
        variants.append(key + "s")
        if key.endswith(("s", "x", "z", "ch", "sh")):
            variants.append(key + "es")
        if key.endswith("y") and len(key) > 2:
            variants.append(key[:-1] + "ies")
        if key.endswith("f"):
            variants.append(key[:-1] + "ves")
    except Exception:
        return []
    unique = []
    for variant in variants:
        if variant and variant != key and variant not in unique:
            unique.append(variant)
    return unique


def _lookup_plural(key):
    for variant in _plural_variants(key):
        hit = _lookup_exact(variant)
        if hit is not None:
            hit["via"] = "plural"
            hit["confidence"] = CONF_PLURAL
            hit["variant"] = variant
            return hit
        hit = _lookup_alias(variant)
        if hit is not None and hit.get("confidence") >= CONF_ALIAS:
            hit["via"] = "plural_alias"
            hit["confidence"] = CONF_PLURAL
            hit["variant"] = variant
            return hit
    return None


def _fts_prefix_expression(key):
    """FTS5 前缀查询表达式；词干做最小转义避免语法错误。"""
    safe = str(key).replace('"', '""')
    return '"%s"*' % safe


def _lookup_fts(key):
    rows = _query(
        "SELECT t.name AS name, t.category AS category, t.post_count AS post_count, "
        "       t.zh AS zh "
        "FROM tag_fts f JOIN tag t ON t.id = f.rowid "
        "WHERE tag_fts MATCH ? ORDER BY t.post_count DESC LIMIT 5",
        (_fts_prefix_expression(key),))
    if not rows:
        return None
    best = rows[0]
    best_count = _to_int(best["post_count"])
    # 歧义判定：top1 与 top2 热度差不足 5 倍则不自动替换
    if len(rows) > 1:
        second = _to_int(rows[1]["post_count"])
        if second > 0 and best_count < second * 5:
            result = _row_to_result(best, "fts_ambiguous", CONF_FUZZY)
            result["candidates"] = [str(row["name"]) for row in rows]
            return result
    if best_count < 1000:
        # 冷门结果不足以支撑自动改写
        result = _row_to_result(best, "fts_lowfreq", CONF_FUZZY)
        result["candidates"] = [str(row["name"]) for row in rows]
        return result
    return _row_to_result(best, "fts", CONF_FTS)


def _edit_distance(left, right, limit=3):
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1]


def _lookup_fuzzy(key):
    """L5 编辑距离：先用前 3 字符前缀把候选压到几十个，再算距离。"""
    if len(key) < 4:
        return None
    prefix = key[:3]
    rows = _query(
        "SELECT name, category, post_count, zh FROM tag "
        "WHERE name LIKE ? ORDER BY post_count DESC LIMIT 80",
        (prefix + "%",))
    if not rows:
        return None
    best = None
    best_distance = 99
    for row in rows:
        name = str(row["name"])
        distance = _edit_distance(key, name, limit=2)
        if distance <= 2 and distance < best_distance:
            best = row
            best_distance = distance
    if best is None:
        return None
    if _to_int(best["post_count"]) < 10000:
        result = _row_to_result(best, "fuzzy_lowfreq", CONF_FUZZY)
        result["distance"] = best_distance
        return result
    result = _row_to_result(best, "fuzzy", CONF_FUZZY)
    result["distance"] = best_distance
    return result


def _is_obvious_non_tag(key):
    """明显不是 danbooru 标签写法的词（评价词 / 分辨率 / 典型幻觉词）。

    这类词**不做前缀模糊匹配**：实测 ``ethereal`` 的前缀会命中角色名
    ``ethereal_(zenless_zone_zero)``、``8k`` 会命中 ``kantarou_(8kan)``，
    这种"近似"对提示词只有害处。L1/L2/L3 仍然会查（万一库里真有同名标签）。
    """
    try:
        if not key:
            return False
        if re.match(r"^\d+[kKpP]$", key) or re.match(r"^\d+x\d+$", key):
            return True
        flattened = key.replace("_", " ")
        for item in HALLUCINATION_TOKENS:
            if normalization_key(item) == key or item == flattened:
                return True
        for item in EMPTY_ADJECTIVES:
            if item == key:
                return True
        return False
    except Exception:
        return False


def lookup(token):
    """对单个 token 做 L1-L5 匹配（纯查询，不判断白名单）。

    返回 ``{"hit": bool, "name", "zh", "category", "post_count", "via", "confidence", ...}``。
    ``via`` 取值：exact / alias / plural / plural_alias / fts / fts_ambiguous /
    fts_lowfreq / fuzzy / fuzzy_lowfreq / zh / none。
    """
    result = {
        "hit": False, "name": None, "zh": "", "category": None,
        "category_name": "", "post_count": 0, "via": "none", "confidence": 0.0,
    }
    try:
        key = normalization_key(token)
        if not key:
            return result
        if not available():
            result["skipped"] = True
            result["reason"] = "db_unavailable"
            return result

        # 明显不是标签写法的词：只做确定性匹配，不做前缀/编辑距离猜测
        if _is_obvious_non_tag(key):
            for finder in (_lookup_exact, _lookup_alias, _lookup_plural):
                hit = finder(key)
                if hit is not None:
                    result.update(hit)
                    return result
            result["reason"] = "not_a_tag_style"
            return result

        for finder in (_lookup_exact, _lookup_alias, _lookup_plural,
                       _lookup_fts, _lookup_fuzzy):
            hit = finder(key)
            if hit is not None:
                result.update(hit)
                return result

        # 中文兜底：token 本身是中文时，走 zh 列与中文别名
        if _is_chinese(token):
            zh_rows = search_zh(str(token).strip(), limit=5)
            if zh_rows:
                top = zh_rows[0]
                category = _to_int(top.get("category"))
                result.update({
                    "hit": True,
                    "name": top.get("name"),
                    "zh": top.get("zh", ""),
                    "category": category,
                    "category_name": CATEGORY_NAMES.get(category, str(category)),
                    "post_count": _to_int(top.get("post_count")),
                    "via": "zh",
                    "confidence": CONF_ALIAS if top.get("exact") else CONF_FTS,
                })
                return result
        return result
    except Exception as exc:  # noqa: BLE001
        _warn("lookup 异常（按未命中处理）：%s" % exc)
        return result


def _is_chinese(text):
    try:
        return any("\u4e00" <= char <= "\u9fff" for char in str(text))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 白名单与 unknown 分类
# ---------------------------------------------------------------------------

def _looks_like_hallucination(key):
    """疑似幻觉：既不在库里、又不像 danbooru 标签。"""
    try:
        if not key:
            return False
        # 8k / 4k / 1080p / 4x4 这类分辨率写法
        if re.match(r"^\d+[kKpP]$", key) or re.match(r"^\d+x\d+$", key):
            return True
        flattened = key.replace("_", " ")
        for item in HALLUCINATION_TOKENS:
            if normalization_key(item) == key or item == flattened:
                return True
        for item in EMPTY_ADJECTIVES:
            if item == key:
                return True
        # "xxx quality" 且不在库 → 典型质量词幻觉
        if key.endswith("_quality") or key.endswith("_detailed"):
            return True
        return False
    except Exception:
        return False


def classify_token(token, whitelist_keys):
    """给一个 token 分类：whitelist / hallucination / unknown / known。"""
    try:
        raw = str(token or "").strip()
        if not raw:
            return "empty"
        if raw.startswith("@"):
            return "whitelist"
        key = normalization_key(raw)
        if key in whitelist_keys:
            return "whitelist"
        if not available():
            return "skipped"
        if lookup(raw).get("hit"):
            return "known"
        if _looks_like_hallucination(key):
            return "hallucination"
        return "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# normalize_token / normalize_text
# ---------------------------------------------------------------------------

def normalize_token(token, whitelist=(), min_conf=DEFAULT_MIN_CONFIDENCE):
    """校验并（可能）改写单个 token。

    :returns: ``{"input","output","changed","action","via","confidence",
                 "name","category","post_count","reason"}``
        ``action`` 取值：
        * ``keep``   - 保持原样（已在白名单 / 跳过 / 本来就是标准名）
        * ``replace``- 高置信改写为标准名（调用方应直接采用 ``output``）
        * ``suggest``- 低置信，仅建议（调用方**不要**自动采用）
        * ``unknown``- 库里没有，原样保留（由调用方按 unknown_policy 处理）
    """
    result = {
        "input": token, "output": token, "changed": False, "action": "keep",
        "via": "none", "confidence": 0.0, "name": None, "category": None,
        "post_count": 0, "reason": "", "meta_only": False,
    }
    try:
        raw = str(token or "").strip()
        if not raw:
            result["action"] = "keep"
            result["reason"] = "empty"
            return result
        # 画师标签形态：@ 开头一律免校验（插件 styles.json 用的就是这种）
        if raw.startswith("@"):
            result["action"] = "keep"
            result["via"] = "whitelist"
            result["confidence"] = 1.0
            result["reason"] = "artist_prefix"
            return result

        key = normalization_key(raw)
        if not key:
            result["action"] = "keep"
            result["reason"] = "unnormalizable"
            return result

        whitelist_keys = _whitelist_keys(whitelist)
        if key in whitelist_keys:
            result["action"] = "keep"
            result["via"] = "whitelist"
            result["confidence"] = 1.0
            result["reason"] = "whitelisted"
            return result

        if not available():
            result["action"] = "keep"
            result["via"] = "skipped"
            result["reason"] = "db_unavailable"
            return result

        hit = lookup(raw)
        if not hit.get("hit"):
            result["action"] = "unknown"
            result["via"] = "none"
            result["reason"] = ("hallucination_suspect"
                               if _looks_like_hallucination(key) else "not_in_db")
            return result

        name = str(hit.get("name") or "")
        confidence = float(hit.get("confidence") or 0.0)
        result.update({
            "name": name,
            "category": hit.get("category"),
            "post_count": hit.get("post_count", 0),
            "via": hit.get("via", "none"),
            "confidence": confidence,
            "meta_only": bool(hit.get("meta_only")),
        })

        bare, prefix, suffix = split_decoration(raw)
        replacement = (prefix + name + suffix) if (prefix or suffix) else name

        if name == key:
            # 命中的就是标准名本身。但原写法可能不规范（空格 / 大小写 / 连字符），
            # 此时也应该规范化成 danbooru 的标准写法。
            if raw.strip() == (prefix + key + suffix):
                result["action"] = "keep"
                # meta 类 0 热度（打标器词汇 / 模型名 / 废弃标签）即使拼写正确也要
                # 把原因标出来，让上层按 unknown_policy 处理，而不是当成有效标签。
                result["reason"] = ("meta_only_tag"
                                    if hit.get("meta_only") else "already_standard")
                return result
            result["output"] = replacement
            result["changed"] = True
            result["action"] = "replace"
            result["reason"] = "canonicalized"
            return result

        if confidence >= float(min_conf):
            result["output"] = replacement
            result["changed"] = True
            result["action"] = "replace"
        else:
            result["action"] = "suggest"
            if hit.get("meta_only"):
                # 打标器词汇 / 模型名 / 废弃标签：只建议，绝不自动替换
                result["reason"] = "meta_only_tag"
            elif hit.get("proper_noun_only"):
                # 别名只指向 artist / copyright / character：通用描述词不该被
                # 自动改写成专有名词（neon -> neon_(nikke)），只给建议。
                result["reason"] = "alias_only_proper_noun"
            elif _looks_like_hallucination(key):
                result["reason"] = "hallucination_suspect"
            else:
                result["reason"] = "low_confidence"
            result["candidates"] = [name]
            result["meta_only"] = bool(hit.get("meta_only"))
        return result
    except Exception as exc:  # noqa: BLE001
        _warn("normalize_token 异常（原样保留）：%s" % exc)
        result["output"] = token
        result["action"] = "keep"
        result["reason"] = "internal_error"
        return result


#: 角色 tag 解析的默认最低使用量（过滤掉几乎没人用的同名角色）
CHARACTER_MIN_POSTS = 200


def resolve_character_tag(candidates, min_posts=None):
    """把角色的若干候选名解析成 danbooru 库里的**标准角色 tag**（v15）。

    为什么需要这个（lead 核实）：
      * ``castorice`` 裸 id 在库里 NOT FOUND（只有 ``castorice_(honkai:_star_rail)``）
      * ``firefly`` 裸 id 是 general「萤火虫」（510 posts）
      * ``sparkle`` 裸 id 是 general「闪光」（228739 posts）
    所以直接拿 id 当 trigger 会匹配到空 / 完全无关的概念。

    解析链：多候选 + ``category == 4`` 校验 + 按 ``post_count`` 取最大。
    这样 ``miyabi`` 的候选里「星见雅」-> ``hoshimi_miyabi``(7794) 会击败
    alias 表里错误的 ``hanasaki_miyabi``(675)。

    :param candidates: 候选名集合（角色 id / aliases / activation_words[0]）
    :param min_posts: 最低使用量，默认 :data:`CHARACTER_MIN_POSTS`
    :returns: ``(tag_name 或 "", info)``；解析失败时 ``tag_name`` 为空串
    """
    info = {"candidates": [], "found": {}, "via": "", "reason": ""}
    try:
        threshold = CHARACTER_MIN_POSTS if min_posts is None else int(min_posts)
    except Exception:  # noqa: BLE001
        threshold = CHARACTER_MIN_POSTS

    names = []
    try:
        for candidate in (candidates or ()):
            text = str(candidate if candidate is not None else "").strip()
            if text and text not in names:
                names.append(text)
        info["candidates"] = list(names)
    except Exception:  # noqa: BLE001
        return "", info

    if not names:
        info["reason"] = "no_candidates"
        return "", info
    if not available():
        info["reason"] = "db_unavailable"
        return "", info

    found = {}

    def _take(name, category, posts):
        try:
            if category != 4:
                return
            posts = int(posts or 0)
        except Exception:  # noqa: BLE001
            return
        if posts < threshold:
            return
        if posts > found.get(name, -1):
            found[name] = posts

    try:
        for text in names:
            # 1) alias 表（is_best=1 AND ambiguous=0）
            try:
                rows = _query(
                    "SELECT a.tag_name, t.category, t.post_count "
                    "FROM tag_alias a LEFT JOIN tag t ON t.name = a.tag_name "
                    "WHERE a.alias = ? COLLATE NOCASE "
                    "AND a.is_best = 1 AND a.ambiguous = 0", (text,))
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                if row and len(row) >= 3:
                    _take(row[0], row[1], row[2])
                    if row[1] == 4:
                        info["via"] = info["via"] or "tag_alias"
            # 2) 直接查 tag 表（空格 -> 下划线）
            key = "_".join(text.split())
            if not key:
                continue
            try:
                direct = _query(
                    "SELECT name, category, post_count FROM tag "
                    "WHERE name = ? COLLATE NOCASE", (key,))
            except Exception:  # noqa: BLE001
                direct = []
            for row in direct:
                if row and len(row) >= 3:
                    before = len(found)
                    _take(row[0], row[1], row[2])
                    if len(found) > before:
                        info["via"] = info["via"] or "tag_exact"
    except Exception as exc:  # noqa: BLE001
        _warn("resolve_character_tag 异常（返回空）：%s" % exc)
        info["reason"] = "internal_error"
        return "", info

    info["found"] = dict(found)
    if not found:
        info["reason"] = "not_found"
        return "", info
    best_name = max(found.items(), key=lambda item: item[1])[0]
    info["posts"] = found[best_name]
    return best_name, info


def escape_tag_brackets(tag):
    """把标签里的字面括号转义成 ``\\(`` ``\\)``（v15）。

    ComfyUI 的 ``escape_important`` / ``unescape_important`` 会在
    ``parse_parentheses`` 之前把 ``\\(`` ``\\)`` 换成占位符，
    分词后再还原成字面括号 —— 所以转义后既不会被当权重语法，CLIP 又能收到
    正确的字符串。这是 danbooru tag 转 prompt 的标准做法。
    """
    try:
        text = str(tag if tag is not None else "")
        if not text:
            return text
        # 已转义的不重复加
        text = text.replace("\\(", "\0").replace("\\)", "\1")
        text = text.replace("(", "\\(").replace(")", "\\)")
        text = text.replace("\0", "\\(").replace("\1", "\\)")
        return text
    except Exception:  # noqa: BLE001
        return tag


def split_tags(text):
    """按英文逗号**或换行**切标签（保留顺序、丢弃空项）。

    v10：positive 可以是多行分段结构（每行一个语义段），所以换行也是分隔符。
    单行输入的行为与旧版完全一致。
    """
    try:
        if not isinstance(text, str):
            return []
        flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ",")
        return [piece.strip() for piece in flat.split(",") if piece.strip()]
    except Exception:
        return []


def split_tag_lines(text):
    """按行切分、每行再按逗号切；返回 ``[[tag, ...], ...]``（保留行结构）。

    v10 新增：``normalize_text`` 用它**保持原来的分段结构**，不拍平成一个长串。
    """
    rows = []
    try:
        if not isinstance(text, str):
            return rows
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for line in normalized.split("\n"):
            tags = [piece.strip() for piece in line.split(",") if piece.strip()]
            if tags:
                rows.append(tags)
        return rows
    except Exception:
        return rows


def normalize_text(text, whitelist=(), min_conf=DEFAULT_MIN_CONFIDENCE):
    """校验并归一整串标签。

    :returns: ``{"ok":True, "skipped":bool, "reason":str, "text":str,
                 "corrected":[...], "suggestions":[...], "unknown":[...],
                 "stats":{...}}``
        ``text`` 是可以直接用的归一结果（skipped 时等于原文）。
    """
    summary = {
        "ok": True,
        "skipped": True,
        "reason": "",
        "text": text if isinstance(text, str) else "",
        "corrected": [],
        "suggestions": [],
        "unknown": [],
        "stats": {"total": 0, "kept": 0, "corrected": 0, "suggestions": 0,
                  "unknown": 0, "whitelisted": 0},
    }
    try:
        if not isinstance(text, str) or not text.strip():
            summary["reason"] = "empty_input"
            return summary
        if not available():
            summary["reason"] = "db_unavailable"
            return summary

        rows = split_tag_lines(text)
        if not rows:
            summary["reason"] = "no_tokens"
            return summary
        tokens = [token for row in rows for token in row]
        # v10：多行输入要**保持分段结构**，否则用户看不到「质量/视距/角色/场景」的分层
        multi_line = len(rows) > 1 or ("\n" in text)

        whitelist_keys = _whitelist_keys(whitelist)
        outputs = []
        stats = summary["stats"]
        stats["total"] = len(tokens)
        for token in tokens:
            token_result = normalize_token(token, whitelist_keys, min_conf)
            action = token_result["action"]
            outputs.append(token_result["output"])

            if action == "replace":
                stats["corrected"] += 1
                summary["corrected"].append({
                    "from": token_result["input"],
                    "to": token_result["output"],
                    "via": token_result["via"],
                    "confidence": token_result["confidence"],
                })
            elif action == "suggest":
                stats["suggestions"] += 1
                summary["suggestions"].append({
                    "input": token_result["input"],
                    "candidates": token_result.get("candidates") or [],
                    "confidence": token_result["confidence"],
                    "via": token_result["via"],
                })
            elif action == "unknown":
                stats["unknown"] += 1
                summary["unknown"].append({
                    "input": token_result["input"],
                    "reason": token_result["reason"],
                })
            else:
                if token_result["via"] == "whitelist":
                    stats["whitelisted"] += 1
                else:
                    stats["kept"] += 1

        if multi_line:
            # 按原行分组重新拼回，**行结构一字不改**
            rebuilt = []
            cursor = 0
            for row in rows:
                size = len(row)
                rebuilt.append(", ".join(outputs[cursor:cursor + size]))
                cursor += size
            summary["text"] = "\n".join(rebuilt)
        else:
            summary["text"] = ", ".join(outputs)
        summary["skipped"] = False
        if stats["total"]:
            summary["unknown_ratio"] = round(stats["unknown"] / float(stats["total"]), 4)
        return summary
    except Exception as exc:  # noqa: BLE001
        _warn("normalize_text 异常（原样返回）：%s" % exc)
        summary["text"] = text if isinstance(text, str) else ""
        summary["ok"] = False
        summary["reason"] = "internal_error"
        return summary


# ---------------------------------------------------------------------------
# 中文检索（阶段 0 的可选增强，也可供 /tagdb 路由调试）
# ---------------------------------------------------------------------------

def search_zh(query, limit=10):
    """按中文查询标签：先 zh 精确，再 zh 子串，最后（若有）trigram。

    :returns: ``[{"name","zh","category","post_count","exact","via"}]``
    """
    results = []
    try:
        text = str(query or "").strip()
        if not text:
            return results
        if not available():
            return results
        try:
            limit = max(1, min(int(limit), 50))
        except Exception:
            limit = 10

        rows = _query(
            "SELECT name, zh, category, post_count FROM tag WHERE zh = ? "
            "ORDER BY post_count DESC LIMIT ?", (text, limit))
        if rows:
            for row in rows:
                results.append({
                    "name": str(row["name"]),
                    "zh": str(row["zh"] or ""),
                    "category": _to_int(row["category"]),
                    "post_count": _to_int(row["post_count"]),
                    "exact": True,
                    "via": "zh_exact",
                })
            return results

        # 中文别名（tag_alias.lang='zh'）
        rows = _query(
            "SELECT t.name AS name, t.zh AS zh, t.category AS category, "
            "       t.post_count AS post_count "
            "FROM tag_alias a JOIN tag t ON t.name = a.tag_name "
            "WHERE a.alias = ? AND a.lang = 'zh' AND a.is_best = 1 "
            "ORDER BY t.post_count DESC LIMIT ?", (text.lower(), limit))
        if rows:
            for row in rows:
                results.append({
                    "name": str(row["name"]),
                    "zh": str(row["zh"] or ""),
                    "category": _to_int(row["category"]),
                    "post_count": _to_int(row["post_count"]),
                    "exact": True,
                    "via": "zh_alias",
                })
            return results

        # trigram（仅当建了 zh_fts；要求查询串 >= 3 字符）
        if len(text) >= 3:
            status_info = status()
            if status_info.get("features", {}).get("zh_fts_trigram"):
                safe = text.replace('"', '""')
                rows = _query(
                    "SELECT t.name AS name, t.zh AS zh, t.category AS category, "
                    "       t.post_count AS post_count "
                    "FROM zh_fts f JOIN tag t ON t.id = f.rowid "
                    "WHERE zh_fts MATCH ? ORDER BY t.post_count DESC LIMIT ?",
                    ('"%s"' % safe, limit))
                for row in rows:
                    results.append({
                        "name": str(row["name"]),
                        "zh": str(row["zh"] or ""),
                        "category": _to_int(row["category"]),
                        "post_count": _to_int(row["post_count"]),
                        "exact": False,
                        "via": "zh_trigram",
                    })
                if results:
                    return results

        # 子串兜底（中文 2 字词走这里；15.5 万行约 2-3ms）
        rows = _query(
            "SELECT name, zh, category, post_count FROM tag WHERE zh LIKE ? "
            "ORDER BY post_count DESC LIMIT ?", ("%" + text + "%", limit))
        for row in rows:
            results.append({
                "name": str(row["name"]),
                "zh": str(row["zh"] or ""),
                "category": _to_int(row["category"]),
                "post_count": _to_int(row["post_count"]),
                "exact": False,
                "via": "zh_like",
            })
        return results
    except Exception as exc:  # noqa: BLE001
        _warn("search_zh 异常（返回空）：%s" % exc)
        return []


# ---------------------------------------------------------------------------
# agent 工具：候选查询与反馈文本（v7）
# ---------------------------------------------------------------------------
#
# 用户要的形态是「类似 agent 的功能」：LLM 出草稿 -> 标签库把**具体问题词 +
# 库中真实合法的候选**反馈回去 -> LLM 自己决定采纳 / 换说法 / 保留。
# 而不是插件在背后静默改写（那样 LLM 永远不知道自己的词有问题，
# ``neon -> neon_(nikke)`` 就是静默改写的必然产物）。
#
# 关键约束：**候选必须只从 general(0) 类里找**。
# ``neon`` 的别名候选是 ``neon_(nikke)``（角色名），把这种候选反馈给 LLM
# 只会让它越改越错；它真正需要的候选是 ``neon_lights`` / ``neon_sign``。

#: 触发 agent 第 2 轮的问题词占比下限（可被调用方覆盖）。
AGENT_MIN_RATIO = 0.10

#: 原词 + 这些后缀直接拼出来的通用标签（danbooru 里高频的组合写法）。
_TAG_SUFFIXES = ("_lights", "_light", "_sign", "_signs", "_trim", "_news")

#: 候选里要求的最低热度（避免把极冷门的词推荐给 LLM）。
AGENT_MIN_POST_COUNT = 100

#: 拆词时忽略的虚词（不参与覆盖率判断）。
_AGENT_STOP_WORDS = frozenset((
    "with", "and", "of", "the", "a", "an", "in", "on", "at", "for", "to",
    "from", "by", "or", "as", "into", "over", "under", "very", "highly",
))

#: **长短语阈值**：拆词后有效词数达到这个值就**不给替换候选**。
#:
#: 为什么（真实事故）：``white thigh socks with gradient leg texture`` 这种自然
#: 语言长短语，danbooru **本来就没有**对应的长标签。硬拆成 6 个词后随便命中
#: 一个高频词就把候选塞进反馈，实测给出的是 ``white_background``（230 万热度）、
#: ``gradient_hair``（22 万）、``texture`` —— 跟「白色渐变纹理大腿袜」毫无关系，
#: LLM 一旦采纳画面彻底跑偏。这类词正确处理是明确告诉 LLM
#: 「改用 2-3 个简短标准标签表达」。
AGENT_MAX_CANDIDATE_WORDS = 4

#: 短短语拆词时的**覆盖率**下限：候选至少要覆盖原词集合的这么多比例。
AGENT_MIN_COVERAGE_RATIO = 0.5

_AGENT_HEADER = "你输出的以下标签在 danbooru 标签库中不存在或写法不规范："
_AGENT_FOOTER = "请只修正上述标签，其余标签保持不变，重新输出完整的 JSON。"
_AGENT_TOO_LONG_HINT = (
    "这是一个长短语，danbooru 没有与之对应的长标签。"
    "请改用 2-3 个简短的标准标签来表达同样的意思，或直接删除它")


def _agent_significant_words(key):
    """把标签拆成「有信息量」的词（去停用词、去过短的词）。"""
    words = []
    for word in str(key).split("_"):
        if len(word) >= 3 and word not in _AGENT_STOP_WORDS:
            words.append(word)
    return words


def _agent_is_long_phrase(key):
    """是不是长短语（长到不该给替换候选）。入参可以是原始 token 或已归一 key。"""
    try:
        normalized = normalization_key(key)
    except Exception:  # noqa: BLE001 - 归一失败就按原串判断
        normalized = str(key)
    return len(_agent_significant_words(normalized)) >= AGENT_MAX_CANDIDATE_WORDS


#: 主键自动索引名（``tag.name`` 是 PRIMARY KEY）。候选查询显式指定它，
#: 否则优化器会选 ``(category, post_count)`` 索引从最热标签往下扫（慢 3 个数量级）。
_PK_INDEX_NAME = "sqlite_autoindex_tag_1"
_PK_INDEX_OK = None


def _pk_index_available():
    """主键自动索引是否可用（只查一次，结果缓存）。"""
    global _PK_INDEX_OK
    if _PK_INDEX_OK is None:
        rows = _query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_PK_INDEX_NAME,))
        _PK_INDEX_OK = bool(rows)
    return bool(_PK_INDEX_OK)


def _agent_rows(rows, limit, via=None):
    """把查询结果统一成 ``[{"name","post_count","via"}]``，按热度降序。"""
    result = []
    for row in rows:
        result.append({
            "name": str(row["name"]),
            "post_count": _to_int(row["post_count"]),
        })
    result.sort(key=lambda item: -item["post_count"])
    if via:
        for item in result:
            item["via"] = via
    return result[:max(int(limit), 1)]


def _agent_prefix_candidates(key, limit, min_post_count, via):
    """原词作为前缀（general 类）：``neon`` -> ``neon_lights`` / ``neon_trim``。

    用**主键索引上的 name 范围扫描**，刻意不用 ``ORDER BY post_count``：
    后者会让 SQLite 走 ``(category, post_count)`` 索引，从最热标签开始往下
    扫、一直扫到凑够几个 ``neon_*`` 为止（实测 55 ms）；范围扫描只碰
    ``neon_`` 开头的那几行（实测 0.004 ms），排序交给 Python 做。
    """
    params = (str(key) + "_", str(key) + "_\uffff", int(min_post_count))
    if _pk_index_available():
        rows = _query(
            "SELECT name, post_count FROM tag INDEXED BY %s "
            "WHERE name >= ? AND name < ? AND category = 0 AND post_count >= ? "
            "LIMIT 200" % _PK_INDEX_NAME,
            params)
    else:
        rows = _query(
            "SELECT name, post_count FROM tag "
            "WHERE name >= ? AND name < ? AND category = 0 AND post_count >= ? "
            "LIMIT 200",
            params)
    return _agent_rows(rows, limit, via)


def _agent_exact_candidates(names, via):
    """一组精确标签名（general 类），按热度降序。

    同样强制走主键索引：不加 ``INDEXED BY`` 时优化器会选
    ``idx_tag_cat_count``（category + post_count），变成扫描全部 general
    标签（实测 130 ms），加索引提示后是 0.01 ms 量级。
    """
    clean = [name for name in names if name]
    if not clean:
        return []
    placeholders = ", ".join("?" for _ in clean)
    hint = (" INDEXED BY %s" % _PK_INDEX_NAME) if _pk_index_available() else ""
    rows = _query(
        "SELECT name, post_count FROM tag%s "
        "WHERE name IN (%s) AND category = 0" % (hint, placeholders),
        tuple(clean))
    return _agent_rows(rows, len(clean), via)


def _agent_word_candidates(key, limit, min_post_count):
    """多词标签**拆词**后逐词找合法候选：``steampunk elements`` -> ``steampunk``。

    为什么需要：整串 ``steampunk_elements`` 在库里当然不存在，前缀、后缀、
    词形、FTS 全部落空 —— 实测这就导致 5 个问题词一个候选都没有，反馈变成
    干巴巴的"没有相近标签"，LLM 只能瞎换（``mechanical prosthetic arm`` 被
    换成同样不存在的 ``cybernetic arm``）。拆成单词后 ``steampunk`` /
    ``factory`` / ``arm`` 往往本身就是合法 general 标签，LLM 一眼就知道该往
    哪个方向改。

    每个词最多贡献 1 个候选，按「词在字符串里的长度」从长到短取，长的词信息
    量更大（``prosthetic`` 比 ``arm`` 更能定位语义）。

    **两条硬约束**（真实事故换来的）：

    * **长短语直接放弃**：有效词数 >= ``AGENT_MAX_CANDIDATE_WORDS`` 时返回空。
      ``white thigh socks with gradient leg texture`` 硬拆后会给
      ``white_background``（230 万热度）这种泛词，跟原意毫无关系。
    * **覆盖率约束**：候选必须覆盖原词集合的至少 ``AGENT_MIN_COVERAGE_RATIO``。
      ``gradient leg texture`` 拆出来的 ``gradient_hair`` 只覆盖 ``gradient``
      （1/3），会被过滤掉。
    """
    words = _agent_significant_words(key)
    if len(words) < 2:
        return []
    if len(words) >= AGENT_MAX_CANDIDATE_WORDS:
        return []
    word_set = set(words)
    words.sort(key=lambda word: -len(word))
    # 覆盖率下限：2 词短语只需覆盖 1 个词，3 词及以上要求覆盖 2 个。
    # （2 词短语如果也要求 2 个，steampunk elements -> steampunk 这种正确候选
    #  就会被误杀 —— 实测踩过。）
    required = max(1, int(round(len(word_set) * AGENT_MIN_COVERAGE_RATIO)))
    required = min(required, len(word_set))
    collected = []
    seen = set()
    for word in words:
        if len(collected) >= max(int(limit), 1):
            break
        # 精确匹配原词里的某个词（优先级最高、语义最稳）
        batch = [(item, True) for item in _agent_exact_candidates([word], "word")]
        if not batch:
            # 退而求其次：该词作为前缀扩展
            batch = [(item, False)
                     for item in _agent_prefix_candidates(word, 1,
                                                          min_post_count, "word")]
        for item, is_exact in batch:
            name = item["name"]
            if name in seen:
                continue
            covered = len(set(name.split("_")) & word_set)
            if covered < required:
                continue
            # **前缀扩展必须覆盖 >= 2 个原词**：覆盖 1 个词的前缀扩展基本都是
            # 语义跑偏的（cyborg arm -> arm_up「举手」、aurora lighting ->
            # lighting_cigarette「点烟」），只有精确命中原词里那个词的才可信。
            if not is_exact and covered < 2:
                continue
            seen.add(name)
            collected.append(item)
            break
    return collected[:max(int(limit), 1)]


def _agent_fts_candidates(key, limit):
    """FTS5 前缀（**限 general 类**）：``thighhigh`` -> ``thighhighs``。"""
    rows = _query(
        "SELECT t.name AS name, t.post_count AS post_count "
        "FROM tag_fts f JOIN tag t ON t.id = f.rowid "
        "WHERE tag_fts MATCH ? AND t.category = 0 "
        "ORDER BY t.post_count DESC LIMIT ?",
        (_fts_prefix_expression(key), max(int(limit), 1) * 8))
    return _agent_rows(rows, limit, "fts")


def _agent_fuzzy_candidates(key, limit, min_post_count=1000):
    """编辑距离 <= 2（**限 general 类且热度达标**），按距离、热度排序。

    候选池用「首字符 + 长度区间」而**不是**前缀：拼写错误经常改掉第 2、3 个
    字符（``purple_hair`` 写成 ``pruple_hair``），前缀预筛会直接把正确答案
    漏掉。实测这个池子约 500 行、0.7 ms，足够便宜。
    """
    text = str(key)
    if len(text) < 4:
        return []
    first = text[0]
    if not ("a" <= first <= "z"):
        return []
    low = max(len(text) - 3, 3)
    high = len(text) + 3
    params = (first, chr(ord(first) + 1), int(min_post_count), low, high)
    hint = (" INDEXED BY %s" % _PK_INDEX_NAME) if _pk_index_available() else ""
    rows = _query(
        "SELECT name, post_count FROM tag%s "
        "WHERE name >= ? AND name < ? AND category = 0 AND post_count >= ? "
        "AND length(name) BETWEEN ? AND ?" % hint,
        params)
    scored = []
    for row in rows:
        name = str(row["name"])
        distance = _edit_distance(text, name, limit=2)
        if distance <= 2:
            scored.append((distance, -_to_int(row["post_count"]), name,
                           _to_int(row["post_count"])))
    if not scored:
        return []
    scored.sort()
    return [{"name": item[2], "post_count": item[3], "via": "fuzzy"}
            for item in scored[:max(int(limit), 1)]]


def suggest_candidates(token, limit=3, min_post_count=AGENT_MIN_POST_COUNT):
    """为「库里不存在 / 写法可疑」的词找 **LLM 能采纳的**合法候选。

    只从 ``category = 0``（general）里找，理由见本节开头的说明。按以下顺序
    依次尝试，**先命中的档位优先**（冷门档位只在前面的档位没结果时才用）：

    ====== ================================================== ==========
    档位   规则                                               示例
    ====== ================================================== ==========
    C1     原词 + ``_`` 作为前缀（主键范围扫描）               ``neon`` -> ``neon_lights``
    C2     原词 + 常见后缀（``_lights`` / ``_sign`` ...）       ``neon`` -> ``neon_lights``
    C3     词形归一（单复数互转）                              ``blue eye`` -> ``blue_eyes``
    C4     FTS5 前缀（限 general）                             ``thighhigh`` -> ``thighhighs``
    C5     编辑距离 <= 2（限 general 且 post_count >= 1000）   ``pruple_hair`` -> ``purple_hair``
    ====== ================================================== ==========

    C1/C4/C5 都带**近似匹配**性质，所以对「明显不是标签写法」的词
    （``ethereal`` / ``8k`` / 典型幻觉词）一律不启用 —— 与 :func:`lookup`
    里 L4/L5 的排除规则保持一致。否则会把 ``ethereal_(zenless_zone_zero)``
    这种角色名当成"最接近的合法标签"推荐给 LLM，等于把老 bug 换个形式带回来。

    :param token: 原始标签（可带权重语法 ``(x:1.2)``）
    :param limit: 最多返回几个候选
    :param min_post_count: C1/C2 的最低热度门槛
    :returns: ``[{"name","post_count","via"}]``；找不到返回 ``[]``（绝不抛异常）
    """
    try:
        if not token:
            return []
        key = normalization_key(token)
        if not key:
            return []
        limit = max(int(limit), 1)

        collected = []
        seen = set()

        def take(items):
            for item in items:
                name = item.get("name")
                if not name or name in seen:
                    continue
                seen.add(name)
                collected.append(item)

        # 长短语：danbooru 没有对应的长标签，任何"最接近"的候选都只能是噪音
        # （实测 white thigh socks with gradient leg texture ->
        # white_background(230 万热度)）。直接不给候选，让上层提示改用简短标签。
        if _agent_is_long_phrase(key):
            return []

        # 明显不是标签写法的词：只查确定性档位（词形），不做前缀/近似猜测
        if _is_obvious_non_tag(key):
            take(_agent_exact_candidates(_plural_variants(key), "plural"))
            return collected[:limit]

        # C1-C4 都很便宜（每个 0.01-0.1 ms），**全部跑完再按热度合并**。
        # 不能"第一个命中的档位就返回"：thighhigh 的 C1 前缀会先命中冷门的
        # thighhigh_gaiters(186)，而真正想要的 thighhighs(1.5M) 在 C3 词形里。
        take(_agent_prefix_candidates(key, limit, min_post_count, "prefix"))
        take(_agent_exact_candidates(
            [key + suffix for suffix in _TAG_SUFFIXES], "suffix"))
        take(_agent_exact_candidates(_plural_variants(key), "plural"))
        take(_agent_fts_candidates(key, limit))
        if collected:
            collected.sort(key=lambda item: -_to_int(item.get("post_count")))
            return collected[:limit]

        # C5 编辑距离：整串拼写错误（pruple_hair -> purple_hair）。
        # 必须排在拆词之前，否则 pruple_hair 会被拆词结果（hair_ornament，
        # 热度 2M）抢先，而正确答案其实只差 2 个字符。
        take(_agent_fuzzy_candidates(key, limit))
        if collected:
            collected.sort(key=lambda item: -_to_int(item.get("post_count")))
            return collected[:limit]

        # C4b 多词标签拆词兜底（steampunk elements -> steampunk / factory）
        take(_agent_word_candidates(key, limit, min_post_count))
        collected.sort(key=lambda item: -_to_int(item.get("post_count")))
        return collected[:limit]
    except Exception as exc:  # noqa: BLE001 - 候选查询失败不能影响重写
        _warn("suggest_candidates 异常（返回空）：%s" % exc)
        return []


def _render_agent_feedback(problems):
    """把问题清单渲染成给 LLM 看的中文反馈（标签名保持英文原名）。"""
    lines = [_AGENT_HEADER, ""]
    for item in problems:
        token = item.get("token") or ""
        kind = item.get("kind") or "unknown"
        candidates = item.get("candidates") or []
        if item.get("too_long"):
            # 长短语单独措辞：明确说"没有对应长标签"，不要给误导性候选
            head = "- `%s` %s。" % (
                token, "的写法不规范" if kind == "suggest" else "不在库中")
            lines.append(head + _AGENT_TOO_LONG_HINT)
            continue
        if kind == "suggest":
            head = "- `%s` 的写法不规范。" % token
        else:
            head = "- `%s` 不在库中。" % token
        if candidates:
            described = "、".join(
                "`%s`（%d 次使用）" % (candidate["name"],
                                      _to_int(candidate.get("post_count")))
                for candidate in candidates)
            lines.append(head + "最接近的合法标签：" + described)
        else:
            lines.append(
                head + "也没有相近的合法标签。请改用能表达相同含义的常见 "
                "danbooru 标签，或直接删除它")
    lines.append("")
    lines.append(_AGENT_FOOTER)
    return "\n".join(lines)


def build_agent_feedback(summary, min_ratio=AGENT_MIN_RATIO, limit_per_token=3):
    """根据 :func:`normalize_text` 的汇总，决定是否要发起 agent 第 2 轮。

    触发条件（两条同时满足）：

    1. 存在**问题词**：``unknown`` 里的词（库中不存在）或 ``suggestions``
       里的词（库里有更标准的写法，但置信度不足以自动替换）。白名单里的词
       （LoRA 触发词 / ``@画师`` / skill 质量词 / 本次注入词）根本不会进入这
       两个列表，因此不会被当成"错误"反馈给 LLM。
    2. 问题词占比 ``>= min_ratio``（默认 10%）。绝大多数正常输出（全对、
       或只有一两个冷门词）都低于这个阈值，**不会**触发第二轮，
       所以常态开销仍然是"只调用一次 LLM"。

    :param summary: :func:`normalize_text` 的返回值
    :param min_ratio: 问题词占比阈值
    :param limit_per_token: 每个问题词最多给几个候选
    :returns: ``{"triggered","reason","problem_ratio","problem_count",
                 "total","problems","feedback"}``
    """
    result = {
        "triggered": False,
        "reason": "",
        "problem_ratio": 0.0,
        "problem_count": 0,
        "total": 0,
        "problems": [],
        "feedback": "",
    }
    try:
        if not isinstance(summary, dict):
            result["reason"] = "bad_summary"
            return result
        stats = summary.get("stats") if isinstance(summary.get("stats"), dict) else {}
        total = _to_int(stats.get("total"))
        result["total"] = total
        if total <= 0:
            result["reason"] = "no_tokens"
            return result

        problems = []
        seen = set()
        for kind, bucket in (("unknown", summary.get("unknown")),
                             ("suggest", summary.get("suggestions"))):
            for item in bucket or []:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("input") or "").strip()
                key = normalization_key(token)
                if not key or key in seen:
                    continue
                seen.add(key)
                too_long = _agent_is_long_phrase(key)
                problems.append({
                    "token": token,
                    "kind": kind,
                    "reason": str(item.get("reason") or ""),
                    # 长短语不给候选：任何"最接近"的候选都只能是噪音
                    "too_long": too_long,
                    "candidates": ([] if too_long else
                                   suggest_candidates(token, limit=limit_per_token)),
                })

        result["problem_count"] = len(problems)
        result["problems"] = problems
        if not problems:
            result["reason"] = "no_problem"
            return result

        ratio = len(problems) / float(total)
        result["problem_ratio"] = round(ratio, 4)
        try:
            threshold = float(min_ratio)
        except (TypeError, ValueError):
            threshold = AGENT_MIN_RATIO
        if ratio < threshold:
            result["reason"] = "below_ratio"
            return result

        result["triggered"] = True
        result["reason"] = "triggered"
        result["feedback"] = _render_agent_feedback(problems)
        return result
    except Exception as exc:  # noqa: BLE001 - 反馈生成失败最多就是不触发
        _warn("build_agent_feedback 异常（不触发）：%s" % exc)
        result["reason"] = "internal_error"
        return result
