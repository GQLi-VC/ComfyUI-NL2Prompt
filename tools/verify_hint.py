"""回归：角色行第三列（outfit_hint）的语义修复验证。

修复内容：
  1. 用户手填的纯英文标签仍追加进 positive，且位置在角色 LoRA 触发词之后
  2. 命中预设服装（如「原皮」「假日皮肤」）时，预设名不再作为提示词注入（避免中文污染）
  3. 角色名不在数据层时，手填标签同样生效

用法：python -X utf8 tools/verify_hint.py
"""

from __future__ import annotations

import json
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT, "plugin"))

from nodes import compose_plain  # noqa: E402

BASE = {
    "style_rows": [],
    "text_map": [],
    "negative_base": "lowres",
    "sampling": {"seed": 20261117},
}
NL = "一个少女站在街道上"


def run(ui):
    merged = dict(BASE)
    merged.update(ui)
    return compose_plain(NL, merged)


def has_token(text: str, token: str) -> bool:
    return token.lower() in [t.strip().lower() for t in text.split(",")]


def main() -> int:
    fails = []
    out = {}

    # --- 场景 1：手填纯英文标签，角色命中数据层，未指定预设
    r1 = run({"character_rows": [{"name": "遐蝶", "trigger": "castorice, purple hair", "outfit_hint": "red dress, bare shoulders"}]})
    p1 = r1["positive"]
    out["S1_positive"] = p1
    out["S1_has_red_dress"] = has_token(p1, "red dress")
    out["S1_has_bare_shoulders"] = has_token(p1, "bare shoulders")
    # 顺序：标签应出现在角色激活词之后（purple eyes 是角色级词，由数据层注入）
    order_ok = p1.lower().find("red dress") > p1.lower().find("purple eyes")
    out["S1_order_after_character_words"] = order_ok
    if not out["S1_has_red_dress"]:
        fails.append("S1: 手填英文标签未注入 positive")
    if not order_ok:
        fails.append("S1: 手填标签位置不在角色触发词之后")

    # --- 场景 2：填预设名（中文），应被预设接管且不把中文名注入提示词
    r2 = run({"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "假日皮肤"}]})
    p2 = r2["positive"]
    n2 = r2["negative"]
    out["S2_positive"] = p2
    out["S2_negative"] = n2
    out["S2_positive_contains_preset_name"] = "假日皮肤" in p2
    out["S2_positive_has_layered_dress"] = has_token(p2, "layered dress")
    out["S2_negative_has_strapless_dress"] = has_token(n2, "strapless dress")
    if out["S2_positive_contains_preset_name"]:
        fails.append("S2: 预设名「假日皮肤」仍被注入 positive（中文污染未修）")
    if not out["S2_positive_has_layered_dress"]:
        fails.append("S2: 预设服装激活词未注入")
    if not out["S2_negative_has_strapless_dress"]:
        fails.append("S2: 差集抑制未把原皮词放进 negative")

    # --- 场景 3：角色名不在数据层，手填标签仍生效
    r3 = run({"character_rows": [{"name": "不存在的角色", "trigger": "my_char_lora", "outfit_hint": "silver jacket, torn jeans"}]})
    p3 = r3["positive"]
    out["S3_positive"] = p3
    out["S3_has_silver_jacket"] = has_token(p3, "silver jacket")
    if not out["S3_has_silver_jacket"]:
        fails.append("S3: 角色未命中时手填标签未注入")

    # --- 场景 4：留空时不注入额外词（回归保护）
    r4 = run({"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": ""}]})
    p4 = r4["positive"]
    out["S4_positive"] = p4
    out["S4_has_default_outfit_words"] = has_token(p4, "strapless dress")
    if not out["S4_has_default_outfit_words"]:
        fails.append("S4: 留空时应回落到默认服装(原皮)")

    result = {
        "result": "ALL PASS" if not fails else "FAIL",
        "fails": fails,
        "details": out,
    }
    print(json.dumps(result, ensure_ascii=True, indent=1))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
