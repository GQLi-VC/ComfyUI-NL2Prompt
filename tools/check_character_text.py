# -*- coding: utf-8 -*-
"""v14：自然语言角色识别 + 括号触发词护栏的专项测试。

lead 报的两个严重 bug：
  A. 长句里的角色名完全不被识别（只认 ui.character_rows）
  B. 英文 trigger 被规范化成 `castorice_(honkai:_star_rail)` ——
     **圆括号在 prompt 里是权重语法**，会直接拆坏角色名，导致出图错误。

运行：``python tools/check_character_text.py``
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))

import nodes  # noqa: E402

PASS = 0
FAIL = 0

#: v15：角色 trigger 必须是**库标准 tag + 括号转义**
CAST = "castorice_\\(honkai:_star_rail\\)"
FLY = "firefly_\\(honkai:_star_rail\\)"

CASES = [
    ("中文名+长句", "遐蝶穿着黑色长裙站在雨夜的霓虹街头，回头微笑，长发被风吹起",
     (CAST, "purple_hair", "purple_eyes", "twintails")),
    ("英文名+长句",
     "Castorice is standing on a neon-lit street in the rain at night, looking back with a smile",
     (CAST, "purple_hair", "purple_eyes")),
    # 注：`silver hair` 在库里只有模糊匹配（silver_hairband），按「只信 exact/alias」
    # 原则保持空格形式，所以这里断言用 `silver hair` 而不是 `silver_hair`。
    ("中文名+换装", "流萤换上一袭白色长裙，在阳光明媚的公园里散步，回眸一笑",
     (FLY, "silver hair", "purple_eyes")),
    ("双角色名+长句", "遐蝶和流萤在公园里散步，遐蝶穿黑色长裙，流萤穿白色长裙，阳光明媚",
     (CAST, FLY, "2girls")),
    ("中文名+特写情绪", "遐蝶眼神忧郁地看着窗外，雨滴打在玻璃上，昏暗的室内，特写镜头",
     (CAST, "purple_eyes", "sad")),
    ("别名（卡斯特里斯）", "卡斯特里斯在雨夜街头回头微笑",
     (CAST, "purple_hair")),
]

#: 常规颜文字白名单（这些括号是内容本身）
SAFE_EMOTICONS = {"^_^", "^_^;", ">_<", "o_o", "t_t", ";_;", ":3", ":d", ":p"}


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def section(title):
    print()
    print("-" * 68)
    print(title)
    print("-" * 68)


def make_ui(character_rows=None):
    return {"schema": 1, "character_rows": character_rows or [],
            "style_rows": [], "text_map": [], "system_prompt": "",
            "negative_base": "",
            "sampling": {"top_k": 40, "top_p": 0.9, "seed": -1},
            "ui": {"skill_id": "anima-tag-master"}}


def find_brackets(positive):
    """返回含括号的标签（排除颜文字）。"""
    bad = []
    for line in str(positive).split("\n"):
        for tag in line.split(","):
            text = tag.strip()
            if not text:
                continue
            if text.lower() in SAFE_EMOTICONS:
                continue
            # 合法权重语法 (tag:1.2) 允许
            if re.match(r"^\(.*:[0-9.]+\)$", text):
                continue
            # v15：**转义后的括号是合法的**（castorice_\(...\)），
            # 去掉转义后再看有没有裸括号
            plain = text.replace("\\(", "").replace("\\)", "")
            if "(" in plain or ")" in plain:
                bad.append(text)
    return bad


def main():
    # ---------------- 1. 六个用例 ----------------
    section("1. 自然语言角色识别（6 个用例）")
    for label, text, wanted in CASES:
        core = nodes._compose_core(text, make_ui())
        lowered = str(core["positive"]).lower()
        missing = [tag for tag in wanted if tag not in lowered]
        check("%s：必须出现 %s" % (label, ", ".join(wanted)),
              not missing, "缺 %s\n%s" % (missing, core["positive"]))
        check("%s：debug.characters_from_text 非空" % label,
              bool(core["debug"].get("characters_from_text")),
              str(core["debug"].get("characters_from_text")))

    # ---------------- 2. 无括号护栏 ----------------
    section("2. 输出里绝不能有括号（圆括号是权重语法）")
    for label, text, _wanted in CASES:
        core = nodes._compose_core(text, make_ui())
        bad = find_brackets(core["positive"])
        check("%s：正片无括号标签" % label, not bad, str(bad))

    # ---------------- 3. 双角色结构 ----------------
    section("3. 双角色：各一行 + 2girls + 服装不串台")
    core4 = nodes._compose_core(CASES[3][1], make_ui())
    lines4 = core4["positive"].split("\n")
    check("2girls 在 positive", "2girls" in core4["positive"].lower(), core4["positive"])
    char_lines = [line for line in lines4 if CAST in line or FLY in line]
    check("两个角色各占一行", len(char_lines) == 2, repr(lines4))
    check("两个角色不在同一行",
          all(not (CAST in line and FLY in line) for line in lines4),
          repr(lines4))
    check("遐蝶那行没有 white_dress（不串台）",
          "white_dress" not in char_lines[0].lower(), char_lines[0][:200])
    check("流萤那行没有 black_dress（不串台）",
          "black_dress" not in char_lines[1].lower(), char_lines[1][:200])
    check("遐蝶那行有 black_dress", "black_dress" in char_lines[0].lower(),
          char_lines[0][:200])
    check("流萤那行有 white_dress", "white_dress" in char_lines[1].lower(),
          char_lines[1][:200])

    # ---------------- 4. 与 ui.character_rows 去重 ----------------
    section("4. 正文识别与 ui.character_rows 去重")
    ui_both = make_ui([{"id": "r1", "name": "遐蝶", "trigger": "castorice",
                        "outfit_hint": "原皮"}])
    core5 = nodes._compose_core("遐蝶站在雨夜的街头", ui_both)
    check("同一角色不重复注入（转义全名只出现一次）",
          core5["positive"].count(CAST) == 1, core5["positive"])
    check("UI 已选角色时不标记 characters_from_text",
          not core5["debug"].get("characters_from_text"),
          str(core5["debug"].get("characters_from_text")))

    # ---------------- 5. _sanitize_brackets 单测 ----------------
    section("5. _sanitize_brackets 单元测试")
    ids = ["castorice", "firefly"]
    cases = [
        ("castorice_(honkai:_star_rail)", "castorice"),
        ("firefly (honkai: star rail)", "firefly"),
        ("robin_(honkai:_star_rail), smile", None),
    ]
    for source, expect in cases:
        new, changed = nodes._sanitize_brackets(source, ids)
        if expect:
            check("%r -> %r" % (source, expect),
                  new == expect, repr(new))
        else:
            check("%r 不含 castorice/firefly 时不动" % source,
                  new == source, repr(new))
    # 权重语法不能被动
    new, _ = nodes._sanitize_brackets("(smile:1.2)", ids)
    check("权重语法 (smile:1.2) 保持原样", new == "(smile:1.2)", repr(new))
    # 颜文字不能被动
    new, _ = nodes._sanitize_brackets("^_^", ids)
    check("颜文字 ^_^ 保持原样", new == "^_^", repr(new))

    # ---------------- 6. trigger 用纯 id ----------------
    section("6. trigger 是库标准 tag + 括号转义，且能在库里命中 character")
    core6 = nodes._compose_core(CASES[2][1], make_ui())
    check("流萤的输出是转义全名 firefly_\\(honkai:_star_rail\\)",
          FLY in core6["positive"], core6["positive"])

    # **lead 指定的硬保证**：每个 trigger 去转义后必须在 tag 表精确命中 category=4
    import sqlite3
    db_path = os.path.join(ROOT, "plugin", "assets", "tagdb", "danbooru.db")
    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()
    verified = []
    for line in core6["positive"].split("\n"):
        for tag in line.split(","):
            text = tag.strip()
            if "\\(" not in text and "\\(" not in text:
                continue
            plain = text.replace("\\(", "(").replace("\\)", ")")
            row = list(cursor.execute(
                "SELECT category FROM tag WHERE name = ? COLLATE NOCASE", (plain,)))
            verified.append((plain, row))
            check("%r 在库里命中 category=4（character）" % plain,
                  bool(row) and row[0][0] == 4, str(row))
    connection.close()
    check("至少校验了一个 trigger", bool(verified), str(len(verified)))

    # ---------------- 7. 回归：无角色名时不乱识别 ----------------
    section("7. 回归：没有角色名时不凭空识别角色")
    core7 = nodes._compose_core("一个蓝发少女穿着白色长裙", make_ui())
    check("没提角色名 -> characters_from_text 为空",
          not core7["debug"].get("characters_from_text"),
          str(core7["debug"].get("characters_from_text")))
    check("没提角色名 -> positive 里没有 castorice/firefly",
          "castorice" not in core7["positive"].lower()
          and "firefly" not in core7["positive"].lower(),
          core7["positive"])

    print()
    print("=" * 68)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
