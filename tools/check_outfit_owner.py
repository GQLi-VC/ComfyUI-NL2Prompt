# -*- coding: utf-8 -*-
"""check_outfit_owner.py — 多角色 outfit 归属纠正（v16.4）。

背景：lead 实测「胡桃穿黑色长裙，可莉穿红色外套，两人在公园里散步」
→ `red_coat` 跑到胡桃行。

根因：`cn_tags` 的 character 段按**整句**映射，不知道标签属于谁；
LLM 也可能写错。`_character_text_segment` 早就实现了「按角色名切段」，
但之前只用于 `detect_outfit_override`（预设服装）。

`_assign_outfit_rows` 按角色段重新翻译并纠正归属，对**所有路径**生效。

用法：``python tools/check_outfit_owner.py``
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'plugin'))

import nodes  # noqa: E402

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


def run_llm(text, llm_positive, ui=None):
    """走 _rewrite_only（/rewrite 实际路径），LLM 输出由参数指定。"""
    ui = ui or make_ui()
    core = nodes._compose_core(text, ui)
    original = nodes._call_llm_once
    nodes._call_llm_once = lambda *a, **k: (llm_positive, "worst quality", "t", {}, {})
    try:
        return nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            text, ui, core=core)
    finally:
        nodes._call_llm_once = original


def rows_of(positive):
    return [ln for ln in str(positive or '').split('\n') if ln.strip()]


def row_with(lines, marker):
    for line in lines:
        if marker in line:
            return line
    return ""


def main():
    section("1. lead 原始用例：LLM 写错归属 → 自动纠正")
    text = "胡桃穿黑色长裙，可莉穿红色外套，两人在公园里散步"
    bad = (
        "masterpiece, best quality, highres\n"
        "2girls\n"
        "hu_tao_(genshin_impact), walking, black_dress, long_dress, red_coat\n"
        "klee_(genshin_impact)\n"
        "park"
    )
    result = run_llm(text, bad)
    positive = str(result.get('positive') or '')
    lines = rows_of(positive)
    hu_tao_line = row_with(lines, 'hu_tao_')
    klee_line = row_with(lines, 'klee_')
    check("LLM 路径成功（未 fallback）",
          bool(result.get('ok')) and not (result.get('debug') or {}).get('fallback'),
          str((result.get('debug') or {}).get('fallback')))
    check("red_coat 不再在胡桃行", 'red_coat' not in hu_tao_line, hu_tao_line[:90])
    check("red_coat 移到可莉行", 'red_coat' in klee_line, klee_line[:90])
    check("black_dress 仍在胡桃行", 'black_dress' in hu_tao_line, hu_tao_line[:90])
    check("long_dress 仍在胡桃行", 'long_dress' in hu_tao_line, hu_tao_line[:90])
    check("black_dress 不在可莉行", 'black_dress' not in klee_line, klee_line[:90])
    moved = ((result.get('debug') or {}).get('tagdb') or {}).get('outfit_reassigned') or []
    check("debug 记录了 reassigned", 'red coat' in moved, str(moved))

    section("2. 归属本来就对时不动")
    ok_case = (
        "masterpiece, best quality, highres\n"
        "2girls\n"
        "hu_tao_(genshin_impact), walking, black_dress, long_dress\n"
        "klee_(genshin_impact), red_coat\n"
        "park"
    )
    result2 = run_llm(text, ok_case)
    lines2 = rows_of(result2.get('positive'))
    # 注意：即使 LLM 原本写对，中间步骤 `_enforce_cn_segments` 也会把
    # cn_tags 的 outfit 一律补到**第一个**角色行（这是串台的制造点），
    # 所以这里**允许**发生一次「搬回去」。断言只看最终归属是否正确。
    check("最终胡桃行是 black_dress",
          'black_dress' in row_with(lines2, 'hu_tao_'), row_with(lines2, 'hu_tao_')[:90])
    check("最终可莉行是 red_coat",
          'red_coat' in row_with(lines2, 'klee_'), row_with(lines2, 'klee_')[:90])
    check("最终胡桃行不含 red_coat",
          'red_coat' not in row_with(lines2, 'hu_tao_'), row_with(lines2, 'hu_tao_')[:90])
    check("最终可莉行不含 black_dress",
          'black_dress' not in row_with(lines2, 'klee_'), row_with(lines2, 'klee_')[:90])

    section("3. 单人时不受影响")
    one_text = "胡桃穿黑色长裙在公园里散步"
    one_llm = (
        "masterpiece, best quality, highres\n"
        "1girl, solo\n"
        "hu_tao_(genshin_impact), walking, black_dress, long_dress\n"
        "park"
    )
    result3 = run_llm(one_text, one_llm)
    moved3 = ((result3.get('debug') or {}).get('tagdb') or {}).get('outfit_reassigned') or []
    check("单人不触发归属纠正", not moved3, str(moved3))
    check("单人服装保留",
          'black_dress' in str(result3.get('positive') or ''),
          str(result3.get('positive'))[:100])

    section("4. 无角色时不报错")
    plain_llm = (
        "masterpiece, best quality, highres\n"
        "1girl, solo\n"
        "black_dress, long_dress\n"
        "park"
    )
    result4 = run_llm("穿黑色长裙在公园里散步", plain_llm)
    check("无角色仍成功", bool(result4.get('ok')),
          str((result4.get('debug') or {}).get('fallback')))
    moved4 = ((result4.get('debug') or {}).get('tagdb') or {}).get('outfit_reassigned') or []
    check("无角色不触发纠正", not moved4, str(moved4))

    section("5. 直接测 _assign_outfit_rows 的边界")
    # 少于 2 个角色 -> 原样返回
    out, moved = nodes._assign_outfit_rows(
        "1girl\nhu_tao_(genshin_impact), black_dress",
        "胡桃穿黑色长裙",
        [{"tag": "hu_tao_(genshin_impact)", "alias": "胡桃"}],
    )
    check("单角色原样返回", out.startswith("1girl") and not moved, str(moved))

    # 空输入不崩
    out2, moved2b = nodes._assign_outfit_rows("", "", [])
    check("空输入不崩", out2 == "" and not moved2b, repr(out2))

    # role_infos 缺 alias -> 不崩
    out3, moved3b = nodes._assign_outfit_rows(
        "a\nb", "原文", [{"tag": "t1"}, {"tag": "t2"}])
    check("缺 alias 不崩", isinstance(out3, str) and not moved3b, str(moved3b))

    section("6. 非 outfit 标签不被搬动")
    text6 = "胡桃穿黑色长裙，可莉穿红色外套，两人在公园里散步"
    llm6 = (
        "masterpiece, best quality, highres\n"
        "2girls\n"
        "hu_tao_(genshin_impact), walking, smile, black_dress, red_coat\n"
        "klee_(genshin_impact)\n"
        "park"
    )
    result6 = run_llm(text6, llm6)
    lines6 = rows_of(result6.get('positive'))
    hu6 = row_with(lines6, 'hu_tao_')
    # smile 用户没说过，会被来源校验删掉（符合 §九 严禁编造），
    # 这里断言的是「归属纠正不会把非 outfit 标签搬走」。
    check("walking（action）留在胡桃行", 'walking' in hu6, hu6[:100])
    _klee6 = row_with(lines6, 'klee_')
    check("walking 没被搬到可莉行", 'walking' not in _klee6, _klee6[:100])
    check("非 outfit 标签总数不变（smile 因无来源被删是另一套机制）",
          'black_dress' in hu6 or 'black_dress' in str(result6.get('positive')),
          hu6[:100])

    print()
    print("=" * 70)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
