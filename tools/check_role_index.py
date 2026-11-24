# -*- coding: utf-8 -*-
"""check_role_index.py — v16 tagdb 角色索引自检。

覆盖：
1. 索引文件存在且 meta 合理
2. 7 个 lead 指定用例（原神/崩铁/明日方舟/碧蓝航线/蔚蓝档案/绝区零/东方）命中
3. **零误伤**：通用场景词（黄昏/白色/光明/海风/neon/dusk）不得识别成角色
4. 括号转义：输出必须是 ``hu_tao_\\(genshin_impact\\)`` 形式，无裸括号
5. **不编造特征**：tagdb 角色只输出角色 tag，不带发色/瞳色/服装
6. 优先级：characters.json 里的角色仍走原路径（role_source 不是 tagdb）
7. 歧义：同名角色分差不足时标记 ambiguous
8. 英文识别：``Hu Tao`` / ``Anby``

用法：``python tools/check_role_index.py``
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'plugin'))

import nodes  # noqa: E402
import role_index  # noqa: E402

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


def tags_of(positive):
    return [t.strip() for line in positive.split('\n')
            for t in line.split(',') if t.strip()]


#: lead 指定的 7 个用例 -> 期望的转义后 trigger
CASES = [
    ("原神-胡桃", "胡桃在璃月港的街道上散步，黄昏时分，夕阳把街道染成橘红色",
     "hu_tao_\\(genshin_impact\\)"),
    ("崩铁-流萤", "流萤换上一袭白色长裙，在阳光明媚的公园里散步",
     "firefly_\\(honkai:_star_rail\\)"),
    ("明日方舟-斯卡蒂", "斯卡蒂穿着泳装站在海边，阳光明媚",
     "skadi_\\(arknights\\)"),
    ("碧蓝航线-企业", "企业站在甲板上，海风吹起她的头发",
     "enterprise_\\(azur_lane\\)"),
    ("蔚蓝档案-白子", "白子在教室里看书，午后的阳光从窗户照进来",
     "shiroko_\\(blue_archive\\)"),
    ("绝区零-安比", "安比在霓虹闪烁的街头，回头看了一眼",
     "anby_demara"),
    ("东方-灵梦", "博丽灵梦站在神社前，红色的蝴蝶结很显眼",
     "hakurei_reimu"),
]

#: 这些通用词绝不能变成角色
FALSE_POSITIVES = ["黄昏", "白色", "光明", "海风", "天空", "阳光", "红色"]


def main():
    section("1. 索引文件与元数据")
    index = role_index.load_role_index()
    check("role_index.json 可加载", index is not None)
    if not index:
        print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
        return 1
    stats = role_index.role_index_stats()
    check("角色数 > 30000", (stats.get("count") or 0) > 30000, str(stats.get("count")))
    check("中文 key 数 > 40000", (stats.get("zh_keys") or 0) > 40000, str(stats.get("zh_keys")))
    check("英文 key 数 > 60000", (stats.get("en_keys") or 0) > 60000, str(stats.get("en_keys")))
    check("加载耗时 < 2000ms", (stats.get("load_ms") or 0) < 2000, str(stats.get("load_ms")))

    section("2. lead 指定的 7 个用例")
    for label, text, want in CASES:
        core = nodes._compose_core(text, make_ui())
        positive = core['positive']
        debug = core['debug']
        tags = tags_of(positive)
        check("%s 命中 %s" % (label, want), want in tags,
              str([t for t in tags if '(' in t or '_' in t][:6]))
        # 转义正确：不允许裸括号（出现 ( 但不是 \( 的标签）
        bare = [t for t in tags if '(' in t and '\\(' not in t]
        check("%s 无裸括号" % label, not bare, str(bare[:3]))

    section("3. 零误伤：通用场景词不得识别成角色")
    for probe in FALSE_POSITIVES:
        hits = role_index.scan_roles_in_text("她站在%s的地方" % probe)
        check("「%s」不当角色" % probe, not hits, str([h['tag'] for h in hits]))
    for probe in ("dusk", "neon", "white"):
        hits = role_index.scan_roles_in_text("she stands in the %s light" % probe)
        check("英文「%s」不当角色" % probe, not hits,
              str([h['tag'] for h in hits]))

    section("4. 不编造特征（tagdb 角色只输出角色 tag）")
    core = nodes._compose_core(CASES[0][1], make_ui())   # 胡桃
    tags = tags_of(core['positive'])
    check("胡桃用例有角色 tag", "hu_tao_\\(genshin_impact\\)" in tags, str(tags[:6]))
    # 用户没说发色/瞳色，就不该出现 *_hair / *_eyes
    invented = [t for t in tags if t.endswith('_hair') or t.endswith('_eyes')
                or 'hair' in t or 'eyes' in t]
    check("没有编造发色/瞳色", not invented, str(invented))
    check("debug.role_source 标记为 tagdb_role_index",
          core['debug'].get('role_source') == 'tagdb_role_index',
          str(core['debug'].get('role_source')))
    roles = core['debug'].get('tagdb_roles') or []
    check("debug.tagdb_roles 记录了命中信息",
          bool(roles) and roles[0].get('tag') == 'hu_tao_(genshin_impact)', str(roles))

    section("5. 优先级：characters.json 角色仍走原路径")
    core = nodes._compose_core(CASES[1][1], make_ui())   # 流萤（Lora 角色）
    debug = core['debug']
    check("流萤走 characters.json（role_source 不是 tagdb）",
          debug.get('role_source') != 'tagdb_role_index',
          str(debug.get('role_source')))
    check("流萤仍带角色特征标签（来自 characters.json）",
          any('silver' in t or 'hair' in t for t in tags_of(core['positive'])),
          str(tags_of(core['positive'])[:8]))

    section("6. 英文识别")
    for text, want in (("Hu Tao is walking through Liyue Harbor at dusk",
                        "hu_tao_(genshin_impact)"),
                       ("Anby glances back on a neon street", "anby_demara"),
                       ("Skadi stands on the beach", "skadi_(arknights)")):
        hits = role_index.scan_roles_in_text(text)
        found = [h['tag'] for h in hits]
        check("%r -> %s" % (text[:26], want), want in found, str(found))

    section("7. 多角色同句")
    hits = role_index.scan_roles_in_text("胡桃和流萤在公园里散步")
    found = [h['tag'] for h in hits]
    check("同句识别出 2 个角色", len(found) == 2, str(found))
    check("含 hu_tao", "hu_tao_(genshin_impact)" in found, str(found))
    check("含 firefly", "firefly_(honkai:_star_rail)" in found, str(found))

    section("8. 歧义标记")
    # 「斯卡蒂」有 skadi_(arknights) 6875 与若干变体，最高分突出 -> 不算歧义
    hits = role_index.scan_roles_in_text("斯卡蒂站在海边")
    check("高热度角色不判歧义", hits and not hits[0]['ambiguous'],
          str(hits[:1]))
    # 长句里没有角色时应返回空
    check("无角色长句返回空",
          not role_index.scan_roles_in_text("一个少女站在雨夜的街头"),
          str(role_index.scan_roles_in_text("一个少女站在雨夜的街头")))

    section("8b. 泛称不得识别成角色（lead 实测的 inkling 误伤）")
    for probe in ("两个女角色在公园里散步", "一个男角色站在海边",
                  "三个人物在房间里", "一个女孩站在树下"):
        hits = role_index.scan_roles_in_text(probe)
        check("「%s」不产生角色" % probe, not hits,
              str([h['tag'] for h in hits]))
    # 真实角色名仍要正常识别
    hits = role_index.scan_roles_in_text("胡桃在璃月港散步")
    check("泛称排除不影响真实角色", hits and hits[0]['tag'] == 'hu_tao_(genshin_impact)',
          str([h['tag'] for h in hits]))

    section("8c. 多角色强制每人一行（2girls 不能与两个角色 tag 同行）")
    # 直接测 _restructure_rows：模拟 LLM 把两个角色挤进一行
    _rows = [['best quality'], ['2girls'],
             ['klee_(genshin_impact)', 'hu_tao_(genshin_impact)',
              'holding_hands', 'walking'],
             ['street', 'sunset']]
    _tags = ['klee_(genshin_impact)', 'hu_tao_(genshin_impact)']
    _out = nodes._restructure_rows([list(r) for r in _rows], _tags)
    _joined = [', '.join(r) for r in _out]
    _char_lines = [line for line in _joined
                   if 'genshin_impact' in line]
    check("两个角色被拆成两行", len(_char_lines) == 2, str(_char_lines))
    check("每行只有一个角色 tag",
          all(line.count('genshin_impact') == 1 for line in _char_lines),
          str(_char_lines))
    _second = _joined[1] if len(_joined) > 1 else ''
    check("共享动作 holding_hands 挪到第 2 行", 'holding_hands' in _second, _second)
    check("共享动作 walking 挪到第 2 行", 'walking' in _second, _second)
    check("第 2 行仍有人数 2girls", '2girls' in _second, _second)
    check("角色行不再带共享动作",
          not any('holding_hands' in line or 'walking' in line
                  for line in _char_lines), str(_char_lines))

    # 单人行为不变
    _rows1 = [['best quality'], ['1girl'],
              ['hu_tao_(genshin_impact)', 'walking'], ['street']]
    _out1 = nodes._restructure_rows([list(r) for r in _rows1], _tags)
    _j1 = [', '.join(r) for r in _out1]
    check("单人：角色行仍保留自己的动作",
          any('hu_tao_(genshin_impact), walking' in line for line in _j1), str(_j1))

    section("9. 性能")
    import time
    text = "胡桃和流萤在璃月港的街道上散步，黄昏时分，夕阳把街道染成橘红色"
    started = time.time()
    for _ in range(20):
        role_index.scan_roles_in_text(text)
    per_call = (time.time() - started) / 20 * 1000
    check("单次扫描 < 20ms（长句）", per_call < 20, "%.2f ms" % per_call)

    print()
    print("=" * 70)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
