# -*- coding: utf-8 -*-
"""v16：从 danbooru tagdb 识别长句里的热门游戏角色名。

与 ``characters.json`` 的区别：

* ``characters.json`` 是**用户自配的 Lora 角色**（9 个），带 activation_words /
  outfit / lora_name，能注入完整特征；
* 这里是**从 tag 库（category=4）自动建的角色名索引**（36k 条），
  只有「名字 → 标准 tag」，**没有任何特征标签**。

所以从 tagdb 认出来的角色：

* **只输出角色 tag 本身**（``hu_tao_(genshin_impact)``），绝不编造发色瞳色服装；
* 角色 tag 本身就是 danbooru 的完整概念，模型认识它就够；
* ``debug.role_source = "tagdb_role_index"``，让用户知道这不是本地 Lora。

索引文件由 ``tools/build_role_index.py`` 生成（``role_index.json``），
本模块只负责**加载（进程内单例）** 与 **长句扫描**。
"""
from __future__ import annotations

import json
import os
import threading
import time

#: 索引文件路径（与 danbooru.db 同目录）
ROLE_INDEX_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "tagdb", "role_index.json"
)

#: 一次扫描最多返回几个角色（防止长句里全是角色名导致 positive 爆炸）
ROLE_SCAN_LIMIT = 4

#: 运行时**最低 post_count**。索引里保留 posts>=50 的全量数据（供作品名过滤时下探），
#: 但默认匹配要求角色足够热门 —— 否则「白色」「光明」「海风」「黄昏」这类
#: 恰好等于某个长尾角色中文名的通用词会被误判成角色。
ROLE_MIN_POSTS = 2000

#: 中英文分界：这些字符不算英文单词字符
_WORD_BOUNDARY = set(" \t\n,.;:!?()[]{}<>\"'`~@#$%^&*-=+|/\\")

#: 英文常见「时间 / 天气 / 颜色 / 方位」词。这些词几乎总有某个角色 tag 拿它当名字
#: （``dusk_(arknights)``、``neon_(nikke)``…），但它们在长句里几乎总是场景描述。
#: ``cn_tags`` 的中文键能盖掉大部分，英文侧再用这张小表兜底。
_EN_COMMON_WORDS = (
    "dusk", "dawn", "twilight", "night", "day", "morning", "evening", "noon",
    "midnight", "sunset", "sunrise", "sky", "rain", "snow", "wind", "cloud",
    "storm", "fog", "mist", "sunlight", "moonlight", "star", "stars",
    "white", "black", "red", "blue", "green", "yellow", "purple", "pink",
    "orange", "brown", "grey", "gray", "silver", "gold", "golden",
    "north", "south", "east", "west", "left", "right", "front", "back",
    "light", "dark", "shadow", "fire", "water", "ice", "flower", "tree",
)

_INDEX = None
_INDEX_LOCK = threading.Lock()
_INDEX_LOAD_MS = None
_EXCLUDE = None


def _load_exclude_sets():
    """用 ``cn_tags`` 的词表建「场景/通用词」排除集。

    问题的来历：很多通用词恰好等于某个长尾角色的中文名 ——
    「黄昏」= ``twilight_(spy_x_family)``（4809 posts）、
    「光明」= ``mejiro_bright_(umamusume)``、``dusk`` = ``dusk_(arknights)``。
    这些词首先是**场景词**，识别成角色会直接画错人。

    ``cn_tags.CN_TAG_MAP`` 的 565 个词条正是「我们已经会翻译的场景/动作/服装词」，
    拿它的中文键和英文值做排除，误伤立刻消失，而真实角色名（胡桃/白子/安比/
    流萤/斯卡蒂/企业）一个都不在里面。
    """
    global _EXCLUDE
    if _EXCLUDE is not None:
        return _EXCLUDE
    zh_keys = set()
    en_keys = set(_EN_COMMON_WORDS)
    try:
        from . import cn_tags as _cn_tags  # 包内导入
    except ImportError:
        try:
            import cn_tags as _cn_tags  # type: ignore  # 裸导入
        except Exception:  # noqa: BLE001 - 没有词表就只靠 posts 阈值
            _EXCLUDE = (zh_keys, en_keys)
            return _EXCLUDE
    try:
        mapping = getattr(_cn_tags, "CN_TAG_MAP", {}) or {}
        for key, value in mapping.items():
            text = str(key or "").strip()
            if text:
                zh_keys.add(text.lower())
            for piece in str(value or "").replace("_", " ").split(","):
                piece = piece.strip().lower()
                if piece:
                    en_keys.add(piece)
                    # 多词短语取首词也排除（"dusk" 之于 "dusk sky"）
                    if " " in piece:
                        en_keys.add(piece.split(" ")[0])
    except Exception:  # noqa: BLE001
        pass
    _EXCLUDE = (zh_keys, en_keys)
    return _EXCLUDE


def _warn(message):
    try:
        print("[NL2Prompt.tagdb] %s" % message, flush=True)
    except Exception:  # noqa: BLE001
        pass


def load_role_index(force=False):
    """加载角色索引（进程内单例）。失败返回 ``None``，调用方按「无索引」降级。"""
    global _INDEX, _INDEX_LOAD_MS
    if _INDEX is not None and not force:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None and not force:
            return _INDEX
        started = time.time()
        try:
            if not os.path.isfile(ROLE_INDEX_PATH):
                _warn("role_index.json 不存在，跳过 tagdb 角色识别")
                return None
            with open(ROLE_INDEX_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or "zh" not in data:
                _warn("role_index.json 结构异常，跳过 tagdb 角色识别")
                return None
            data.setdefault("en", {})
            data.setdefault("zh_buckets", {})
            data.setdefault("en_buckets", {})
            _INDEX = data
            _INDEX_LOAD_MS = round((time.time() - started) * 1000.0, 1)
            return _INDEX
        except Exception as exc:  # noqa: BLE001 - 索引坏了不能拖垮节点
            _warn("加载 role_index.json 失败：%s: %s" % (type(exc).__name__, exc))
            return None


def _find_hits(text, table, buckets, bucket_len, exact_boundary):
    """在 ``text`` 里找 ``table`` 的所有 key 命中，返回 ``[(start, key)]``。

    ``buckets`` 是「首 N 字符 -> key 列表（已按长度降序）」，只用来缩小候选集。
    ``exact_boundary=True`` 时要求命中两端是词边界（英文必须，避免
    ``enterprise`` 命中 ``enterpriseless`` 这类）。
    """
    hits = []
    if not text or not table:
        return hits
    length = len(text)
    for index in range(length):
        bucket_key = text[index:index + bucket_len]
        if not bucket_key:
            continue
        for key in buckets.get(bucket_key, ()):
            key_len = len(key)
            if index + key_len > length:
                continue
            if not text.startswith(key, index):
                continue
            if exact_boundary:
                before = text[index - 1] if index > 0 else " "
                after = text[index + key_len] if index + key_len < length else " "
                if before not in _WORD_BOUNDARY or after not in _WORD_BOUNDARY:
                    continue
            hits.append((index, key))
    return hits


def _pick_non_overlapping(hits, text):
    """最长优先 + 不重叠：避免「博丽灵梦」被「灵梦」切碎。"""
    ordered = sorted(hits, key=lambda item: (-len(item[1]), item[0]))
    taken = []
    for index, key in ordered:
        end = index + len(key)
        conflict = False
        for used_start, used_end in taken:
            if index < used_end and used_start < end:
                conflict = True
                break
        if not conflict:
            taken.append((index, end))
    taken.sort()
    return [text[start:end] for start, end in taken]


def _resolve_candidate(candidates, work_filter=None, min_posts=ROLE_MIN_POSTS):
    """从候选里选一个；返回 ``(tag, posts, ambiguous)``。

    策略（lead 指定）：
      1. 有作品过滤（长句里出现「原神的胡桃」）时先按作品筛；
      2. 否则取 ``post_count`` 最高的；
      3. 同名且最高分**与其他候选差距很小**时判为歧义 -> 不注入。
    """
    if not candidates:
        return None, 0, False
    pool = candidates
    if work_filter:
        filtered = [item for item in candidates if work_filter in item[0]]
        if filtered:
            pool = filtered
    pool = sorted(pool, key=lambda item: -int(item[1] or 0))
    # 显式作品名过滤时放宽阈值（用户已经指明作品，长尾角色也该认）
    floor = 50 if (work_filter and len(pool) < len(candidates)) else min_posts
    pool = [item for item in pool if int(item[1] or 0) >= floor]
    if not pool:
        return None, 0, False
    best = pool[0]
    if len(pool) > 1 and len(candidates) > 1:
        best_posts = int(best[1] or 0)
        second_posts = int(pool[1][1] or 0)
        # 最高分不够突出（不到第二名的 2 倍）且绝对值也不高 -> 歧义
        if best_posts < 2000 and second_posts > best_posts * 0.5:
            return best[0], best_posts, True
    return best[0], int(best[1] or 0), False


def scan_roles_in_text(text, limit=ROLE_SCAN_LIMIT, min_posts=ROLE_MIN_POSTS):
    """扫描长句里的角色名，返回 ``[dict]``。

    每项：``{"tag", "posts", "matched", "source", "ambiguous", "candidates"}``

    * 中文名与英文名都扫；
    * 最长优先、互不重叠；
    * 命中后按 ``post_count`` 选一个（含歧义判定）。
    """
    index = load_role_index()
    if not index:
        return []
    raw = str(text or "")
    if not raw.strip():
        return []

    results = []
    seen_tags = set()

    zh_exclude, en_exclude = _load_exclude_sets()

    # ---- 中文：按首字分桶（单字桶足够，中文没有单词边界问题）
    zh_hits = [
        (pos, key) for pos, key in
        _find_hits(raw, index["zh"], index["zh_buckets"], 1, False)
        if key.lower() not in zh_exclude
    ]
    # ---- 英文：小写后按前 2 字符分桶，必须词边界
    lowered = raw.lower()
    en_hits = [
        (pos, key) for pos, key in
        _find_hits(lowered, index["en"], index["en_buckets"], 2, True)
        if key.lower() not in en_exclude and len(key) >= 3
    ]

    # 中文优先：中文 key 出现时，同一位置上英文 key 一般会重复命中
    for source, hits, table in (("zh", zh_hits, index["zh"]),
                                ("en", en_hits, index["en"])):
        for matched in _pick_non_overlapping(hits, raw if source == "zh" else lowered):
            key = matched if source == "zh" else matched.lower()
            candidates = table.get(key) or []
            if not candidates:
                continue
            tag, posts, ambiguous = _resolve_candidate(candidates, min_posts=min_posts)
            if not tag or tag in seen_tags:
                continue
            seen_tags.add(tag)
            results.append({
                "tag": tag,
                "posts": posts,
                "matched": matched,
                "source": "tagdb_role_index",
                "ambiguous": ambiguous,
                "candidates": [item[0] for item in candidates[:3]],
            })
            if len(results) >= limit:
                return results
    return results


def role_index_stats():
    """给 debug 用：索引规模与加载耗时。"""
    index = load_role_index()
    if not index:
        return {"loaded": False}
    meta = index.get("meta") or {}
    return {
        "loaded": True,
        "count": meta.get("count"),
        "zh_keys": meta.get("zh_keys"),
        "en_keys": meta.get("en_keys"),
        "min_posts": meta.get("min_posts"),
        "load_ms": _INDEX_LOAD_MS,
    }


__all__ = [
    "ROLE_INDEX_PATH",
    "load_role_index",
    "scan_roles_in_text",
    "role_index_stats",
]
