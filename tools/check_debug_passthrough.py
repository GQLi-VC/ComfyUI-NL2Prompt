# -*- coding: utf-8 -*-
"""check_debug_passthrough.py — debug 字段透出的回归测试（v16.3）。

背景：同一类「手写字段白名单」的反模式出现过 6 次
（tagdb → cjk_cleaned → tag_sources → role_source → final_character_count
→ 最后是 `_rewrite_only` 里的 8 字段白名单）。每次表现都是
「功能正常但 debug 里是 None」，排查成本极高。

本测试锁死三件事：
1. **模板路径**（`_compose_core`）的 6 个角色字段有值；
2. **LLM 路径**（`_rewrite_only`，即 `/rewrite` 实际走的路径）同样有值；
3. `routes._build_debug_payload` 全量透出且**不泄漏敏感字段**。
另附带 `lighting` 维度必须落在场景行（`golden_hour` 不能留角色行）。

用法：``python tools/check_debug_passthrough.py``
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'plugin'))

import nodes  # noqa: E402
import routes  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def section(title):
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


def make_ui():
    return {
        'schema': 1, 'character_rows': [], 'style_rows': [], 'text_map': [],
        'system_prompt': '', 'negative_base': '',
        'sampling': {'top_k': 40, 'top_p': 0.9, 'seed': -1},
        'ui': {'skill_id': 'anima-tag-master'},
    }


TEXT = "胡桃和可莉在璃月港的街道上散步，黄昏时分，两个人手牵手"
ROLE_FIELDS = (
    "active_character_tags", "character_tag_map", "characters_from_text",
    "tagdb_roles", "role_source", "final_character_count",
)
LLM_POSITIVE = (
    "best quality, masterpiece, highres\n"
    "2girls\n"
    "hu_tao_(genshin_impact), walking\n"
    "klee_(genshin_impact), holding_hands, golden_hour\n"
    "sunset, street"
)


def _has_value(debug, key):
    value = (debug or {}).get(key)
    if value is None:
        return False
    if isinstance(value, (list, dict, str, tuple)) and not value:
        return False
    return True


def main():
    section("1. 模板路径（_compose_core）的 6 个角色字段")
    core = nodes._compose_core(TEXT, make_ui())
    debug = core['debug']
    for key in ROLE_FIELDS:
        check("%s 有值" % key, _has_value(debug, key), repr(debug.get(key)))
    check("识别出 2 个角色", debug.get('final_character_count') == 2,
          str(debug.get('final_character_count')))
    check("role_source 是 tagdb_role_index",
          debug.get('role_source') == 'tagdb_role_index', str(debug.get('role_source')))

    section("2. LLM 路径（_rewrite_only，/rewrite 实际路径）的 6 个角色字段")
    original = nodes._call_llm_once
    nodes._call_llm_once = lambda *a, **k: (
        LLM_POSITIVE, "worst quality, lowres", "测试", {}, {})
    try:
        result = nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            TEXT, make_ui(), core=core)
    finally:
        nodes._call_llm_once = original

    check("LLM 路径成功（未 fallback）", bool(result.get('ok'))
          and not (result.get('debug') or {}).get('fallback'),
          str((result.get('debug') or {}).get('fallback')))
    llm_debug = result.get('debug') or {}
    for key in ROLE_FIELDS:
        check("LLM 路径 %s 有值" % key, _has_value(llm_debug, key),
              repr(llm_debug.get(key)))

    section("3. routes 透出（黑名单全量透传）")
    payload = routes._build_debug_payload(llm_debug)
    for key in ROLE_FIELDS:
        check("透出 %s" % key, _has_value(payload, key), repr(payload.get(key)))
    # 老字段形状不变
    check("tag_sources 仍存在", isinstance(payload.get('tag_sources'), dict),
          type(payload.get('tag_sources')).__name__)
    check("tag_sources 含 enforced（证明强制注入生效）",
          any(v == 'enforced' for v in (payload.get('tag_sources') or {}).values()),
          str(list((payload.get('tag_sources') or {}).items())[:3]))

    section("4. 敏感字段不泄漏 / 内部字段不透出")
    fake = {
        'api_key': 'SECRET', 'token': 'T', 'secret': 'S', 'password': 'P',
        'llm_config': {'k': 1}, 'config': {}, 'provider': 'x', 'base_url': 'u',
        '_internal': 1, 'raw_text': 'raw',
        'final_character_count': 2, 'object_thing': object(),
    }
    out = routes._build_debug_payload(fake)
    leaked = [k for k in ('api_key', 'token', 'secret', 'password', 'llm_config',
                          'config', 'provider', 'base_url', '_internal', 'raw_text')
              if k in out]
    check("敏感/内部字段无泄漏", not leaked, str(leaked))
    check("普通字段仍透出", out.get('final_character_count') == 2,
          str(out.get('final_character_count')))
    check("不可序列化对象被跳过", 'object_thing' not in out)

    section("5. lighting 维度落在场景行")
    lines = [ln for ln in str(result.get('positive') or '').split('\n') if ln.strip()]
    check("positive 至少 4 行", len(lines) >= 4, str(len(lines)))
    if len(lines) >= 4:
        char_lines = lines[2:-1]
        scene_line = lines[-1]
        check("golden_hour 在场景行",
              'golden' in scene_line.replace('_', ' ').lower(), scene_line)
        check("golden_hour 不在角色行",
              not any('golden' in ln.replace('_', ' ').lower() for ln in char_lines),
              str(char_lines))
        check("角色行各自只有一个角色 tag",
              all(ln.count('genshin_impact') <= 1 for ln in char_lines),
              str(char_lines))

    section("6. _CORE_DEBUG_SKIP 里的字段不被模板值盖掉")
    check("_CORE_DEBUG_SKIP 包含 tag_sources",
          'tag_sources' in nodes._CORE_DEBUG_SKIP, str(sorted(nodes._CORE_DEBUG_SKIP)))
    check("_CORE_DEBUG_SKIP 包含 segment_lines 语义等价项" if False else
          "_CORE_DEBUG_SKIP 不含角色字段",
          all(k not in nodes._CORE_DEBUG_SKIP for k in ROLE_FIELDS),
          str(sorted(nodes._CORE_DEBUG_SKIP)))

    print()
    print("=" * 70)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
