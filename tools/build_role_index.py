# -*- coding: utf-8 -*-
"""build_role_index.py — 从 danbooru.db 构建「热门游戏角色名索引」。

为什么需要：用户长句里写「胡桃在璃月港散步」「斯卡蒂穿泳装」，
插件必须能识别成 danbooru 的 category=4 角色 tag（``hu_tao_(genshin_impact)``）。
``characters.json`` 只有 9 个用户自配的 Lora 角色，不能往里塞几千条。

产物：``plugin/assets/tagdb/role_index.json``

结构（为运行时**快速子串匹配**设计）：

```json
{
  "meta": {"min_posts": 50, "count": 36410, "built": "..."},
  "max_key_len": 24,
  "zh": {"胡桃": [["hu_tao_(genshin_impact)", 5000], ...]},   # 中文名
  "en": {"hu tao": [["hu_tao_(genshin_impact)", 5000], ...]},  # 英文名（下划线->空格）
  "zh_buckets": {"胡": ["胡桃", ...]},     # 中文首字 -> key 列表（长句扫描加速）
  "en_buckets": {"hu": ["hu tao", ...]}    # 英文前 2 字符 -> key 列表
}
```

运行时只按「文本当前位置的首字」查桶，桶内候选一般 < 50，避免 36k 次子串比较。

用法：
    python tools/build_role_index.py [--min-posts 50]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, 'plugin', 'assets', 'tagdb', 'danbooru.db')
OUT = os.path.join(ROOT, 'plugin', 'assets', 'tagdb', 'role_index.json')

#: 英文虚词 / 冠词。角色 tag 常以它们开头（``the_herta`` / ``is_(kamen_rider)``），
#: 但这些词在自然语言里遍地都是，绝不能单独作为角色 key。
EN_STOP_WORDS = frozenset("""
the and for you her his she he it is are was were with from that this
not but all any can has have one two out now get its who what when where
why how she him they them their our your my me we us do does did been
""".split())

#: 太短的名字（1 个字符）几乎一定是误伤，直接跳过
MIN_KEY_LEN = 2
#: 每个 key 最多保留几个候选（同名角色）；超出按 post_count 截断
MAX_CANDIDATES = 6
#: 英文 key 的桶前缀长度
EN_BUCKET = 2


#: 中文名的名字分隔符（安比·德玛拉 / 芙兰朵露・斯卡雷特 …）
_ZH_SEPARATORS = ('·', '・', '‧', '•')
#: 只有 post_count 达到这个值的角色，才给它的中文名生成「后 2 字」弱 key，
#: 避免长尾角色的碎片 name 污染匹配（如把「白子」这样常见的两字词全抢走）
SUFFIX_KEY_MIN_POSTS = 1000


def _is_zh(text):
    """是否含中日文字符（用来判断要不要生成后缀弱 key）。"""
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF:
            return True
    return False


def _strip_bracket_suffix(zh):
    """「散兵（原神）」-> 「散兵」；「安比·德玛拉」-> 「安比」；返回全部变体。"""
    text = (zh or '').strip()
    if not text:
        return []
    candidates = [text]
    # 1) 剥括号后缀（可能有多层：「砂狼白子（泳装）（蔚蓝档案）」）
    head = text
    for left, right in (('（', '）'), ('(', ')')):
        while left in head:
            head = head.split(left)[0].strip()
    if head and head != text:
        candidates.append(head)
    # 2) 按名字分隔符取首段（「安比·德玛拉」-> 「安比」）
    for key in list(candidates):
        for sep in _ZH_SEPARATORS:
            if sep in key:
                piece = key.split(sep)[0].strip()
                if piece and piece not in candidates:
                    candidates.append(piece)
    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-posts', type=int, default=50)
    parser.add_argument('--out', default=OUT)
    args = parser.parse_args()

    started = time.time()
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT name, post_count, zh FROM tag "
        "WHERE category=4 AND post_count >= ? AND is_deprecated=0 "
        "ORDER BY post_count DESC",
        (args.min_posts,),
    ).fetchall()
    conn.close()

    zh_map = {}
    en_map = {}

    def add(mapping, key, tag, posts):
        key = (key or '').strip()
        if len(key) < MIN_KEY_LEN:
            return
        bucket = mapping.setdefault(key, [])
        for existing in bucket:
            if existing[0] == tag:
                return
        bucket.append([tag, posts])

    for name, posts, zh in rows:
        if not name:
            continue
        posts_value = int(posts or 0)
        # ---- 英文名 -------------------------------------------------------
        # tag 形如 `hu_tao_(genshin_impact)` / `anby_demara`，
        # 用户写的是 `hu tao` / `anby demara` —— 必须剥掉 (作品名) 后缀再转空格。
        base = name
        while '(' in base:
            base = base.split('(')[0].strip()
        base = base.rstrip('_').strip() or name
        for variant in {base, name}:
            en_spaced = variant.replace('_', ' ').strip().lower()
            add(en_map, en_spaced, name, posts_value)
            add(en_map, variant.strip().lower(), name, posts_value)
            # 用户常只写名不写姓（「Anby」而不是「Anby Demara」）：
            # 只对热门角色生成首词 key，避免 `white` 这种通用单字污染。
            if posts_value >= SUFFIX_KEY_MIN_POSTS and ' ' in en_spaced:
                head = en_spaced.split(' ')[0]
                # 首词太短或正好是英文虚词时绝不单独成 key ——
                # `the_herta_(honkai:_star_rail)` 会把英文里的 "the" 识别成黑塔。
                if len(head) >= 4 and head not in EN_STOP_WORDS:
                    add(en_map, head, name, posts_value)
        # ---- 中文名 -------------------------------------------------------
        for key in _strip_bracket_suffix(zh):
            add(zh_map, key, name, posts_value)
            # 中文本名常是「姓+名」复合（砂狼白子），用户只写名（白子）。
            # 只对热门角色生成「后 2 字」，避免长尾碎片污染。
            if posts_value >= SUFFIX_KEY_MIN_POSTS and _is_zh(key) and len(key) >= 3:
                add(zh_map, key[-2:], name, posts_value)

    # 每个 key 只留前 MAX_CANDIDATES 个（按 posts 降序；rows 已按 posts 排序）
    def trim(mapping):
        for key, bucket in list(mapping.items()):
            bucket.sort(key=lambda item: -item[1])
            del bucket[MAX_CANDIDATES:]

    trim(zh_map)
    trim(en_map)

    max_key_len = 0
    zh_buckets = {}
    for key in zh_map:
        max_key_len = max(max_key_len, len(key))
        zh_buckets.setdefault(key[0], []).append(key)
    en_buckets = {}
    for key in en_map:
        max_key_len = max(max_key_len, len(key))
        en_buckets.setdefault(key[:EN_BUCKET], []).append(key)

    # 桶内按长度降序：最长匹配优先（「博丽灵梦」先于「灵梦」）
    for buckets in (zh_buckets, en_buckets):
        for key in buckets:
            buckets[key].sort(key=len, reverse=True)

    payload = {
        'meta': {
            'min_posts': args.min_posts,
            'count': len(rows),
            'zh_keys': len(zh_map),
            'en_keys': len(en_map),
            'built': time.strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'danbooru.db',
        },
        'max_key_len': max_key_len,
        'zh': zh_map,
        'en': en_map,
        'zh_buckets': zh_buckets,
        'en_buckets': en_buckets,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))

    size = os.path.getsize(args.out)
    print('已生成 %s' % args.out)
    print('  角色 %d 条 / 中文 key %d / 英文 key %d / 最大 key 长度 %d'
          % (len(rows), len(zh_map), len(en_map), max_key_len))
    print('  文件大小 %.2f MB / 耗时 %.2fs' % (size / 1048576.0, time.time() - started))
    return 0


if __name__ == '__main__':
    sys.exit(main())
