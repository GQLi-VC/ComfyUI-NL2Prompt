# -*- coding: utf-8 -*-
"""v10 多行分段结构 + 中文确定性映射的单测。

对照用户的「正面提示词结构」与「示例内容.csv」，逐条验收：

1. 单角色输出行结构正确（质量 / 视距 / 角色 / 场景）
2. 多角色每个角色单独一行
3. 极简输入不编造用户没描述的内容
4. 中文转译按确定性映射（全身照 -> full body、城市街道 -> street, city）
5. safe **不出现**在 positive
6. tagdb 归一在多行结构下保持该行（high heels -> high_heels 且不跨行）
7. skill_check 不在多行输出上误报
8. 不产生空行、行尾没有多余逗号

运行：``python tools/check_multiline.py``
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))

import io          # noqa: E402
import json        # noqa: E402

import cn_tags     # noqa: E402
import nodes       # noqa: E402
import skill_check # noqa: E402
import tagdb       # noqa: E402

PASS = 0
FAIL = 0


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
    print("-" * 66)
    print(title)
    print("-" * 66)


def make_ui(character_rows=None, style_rows=None, text_map=None):
    return {
        "schema": 1,
        "character_rows": character_rows or [],
        "style_rows": style_rows or [],
        "text_map": text_map or [],
        "system_prompt": "",
        "negative_base": "",
        "sampling": {"top_k": 40, "top_p": 0.9, "seed": -1},
        "ui": {"skill_id": "anima-tag-master"},
    }


CAST = [{"id": "r1", "name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}]
TWO = [
    {"id": "r1", "name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"},
    {"id": "r2", "name": "流萤", "trigger": "firefly (honkai: star rail)",
     "outfit_hint": "原皮"},
]


def lines_of(positive):
    return [line for line in str(positive).split("\n")]


def main():
    # ---------------- 1. 单角色行结构 ----------------
    section("1. 单角色：行结构符合「正面提示词结构」")
    core = nodes._compose_core("高清，全身照，城市街道，夜晚", make_ui(CAST))
    lines = lines_of(core["positive"])
    check("正好 4 行", len(lines) == 4, repr(lines))
    check("第 1 行是质量标签",
          "best quality" in lines[0] and "masterpiece" in lines[0], lines[0])
    check("第 2 行是图片视距", "full body" in lines[1], lines[1])
    check("第 3 行是角色行（含角色触发词）", "castorice" in lines[2], lines[2])
    check("最后一行是场景", "street" in lines[-1] and "city" in lines[-1], lines[-1])
    check("角色行含着装标签", nodes._has_garment_tag(lines[2]), lines[2])

    # ---------------- 2. 多角色每角色一行 ----------------
    section("2. 多角色：每个角色单独一行")
    core2 = nodes._compose_core("两个女角色在公园散步，高清，中景", make_ui(TWO))
    lines2 = lines_of(core2["positive"])
    char_lines = [line for line in lines2 if "castorice" in line or "firefly" in line]
    check("两个角色各占一行（而不是挤在一行）", len(char_lines) == 2, repr(lines2))
    check("castorice 与 firefly 不在同一行",
          all(not ("castorice" in line and "firefly" in line) for line in lines2),
          repr(lines2))
    check("第 2 行含角色数量 2girls", "2girls" in lines2[1], lines2[1])
    check("最后一行是场景 park", "park" in lines2[-1], lines2[-1])
    check("角色行顺序与 UI 选择顺序一致",
          "castorice" in char_lines[0] and "firefly" in char_lines[1],
          repr(char_lines))

    # ---------------- 3. 极简输入不编造 ----------------
    section("3. 极简输入：不编造用户没描述的内容")
    core3 = nodes._compose_core("一个蓝发少女", make_ui(CAST))
    flat3 = core3["positive"].lower()
    check("没有编造视距（不出现 full body / cowboy shot）",
          "full body" not in flat3 and "cowboy shot" not in flat3, flat3[:200])
    check("没有编造场景（不出现 street / classroom / park）",
          not any(word in flat3 for word in ("street", "classroom", "park")), flat3[:200])
    check("没有编造角色数量（不出现 2girls）", "2girls" not in flat3, flat3[:200])
    check("角色行里的标签全部来自角色预设",
          "castorice" in flat3 and "purple hair" in flat3, flat3[:200])

    # ---------------- 4. 中文确定性转译 ----------------
    section("4. 中文转译按确定性映射（不是 LLM 猜）")
    result = cn_tags.translate("全身照，城市街道")
    check("全身照 -> full body", result["framing"] == ["full body"], str(result["framing"]))
    check("城市街道 -> street, city",
          result["scene"] == ["street", "city"], str(result["scene"]))
    core4 = nodes._compose_core("全身照，城市街道", make_ui(CAST))
    flat4 = core4["positive"]
    check("组装结果里出现 full body", "full body" in flat4, flat4[:200])
    check("组装结果里出现 street, city",
          "street" in flat4 and "city" in flat4, flat4[:200])
    check("debug.cn_mapped 记录了映射结果",
          bool(core4["debug"].get("cn_mapped")), str(core4["debug"].get("cn_mapped")))

    section("4b. 质量 / 人数映射")
    quality = nodes._compose_core("高清", make_ui([]))
    check("高清 -> best quality, masterpiece, highres",
          all(tag in quality["positive"]
              for tag in ("best quality", "masterpiece", "highres")),
          quality["positive"])
    check("用户说了质量词时不标记 quality_default",
          not quality["debug"].get("quality_default"), str(quality["debug"].keys()))
    count = nodes._compose_core("两个女角色，一个男角色", make_ui([]))
    check("x个女角色 y个男角色 -> 2girls, 1boy",
          "2girls" in count["positive"] and "1boy" in count["positive"],
          count["positive"])

    # ---------------- 5. safe 不进 positive ----------------
    section("5. safe 不写进 positive（CSV 明确要求）")
    for source in ("安全", "安全，一个蓝发少女", "全身照，安全"):
        core5 = nodes._compose_core(source, make_ui(CAST))
        check("%r -> positive 里没有 safe" % source,
              "safe" not in core5["positive"].lower(), core5["positive"][:160])
    dropped = nodes._compose_core("安全，全身照", make_ui(CAST))
    check("debug.cn_dropped 记录了 safe",
          "safe" in (dropped["debug"].get("cn_dropped") or []),
          str(dropped["debug"].get("cn_dropped")))

    # ---------------- 6. tagdb 归一保持行结构 ----------------
    section("6. tagdb 归一：多行结构保持、空格词改下划线且不跨行")
    multi = ("best quality, masterpiece, highres,\n"
             "castorice, high heels, long dress,\n"
             "street, city")
    normalized = tagdb.normalize_text(multi)
    text = normalized.get("text") or ""
    out_lines = text.split("\n")
    check("行数保持 3 行", len(out_lines) == 3, repr(out_lines))
    check("high heels -> high_heels 且仍在第 2 行",
          "high_heels" in out_lines[1], repr(out_lines))
    check("long dress -> long_dress 且仍在第 2 行",
          "long_dress" in out_lines[1], repr(out_lines))
    check("第 1 行仍是质量标签", "best quality" in out_lines[0], repr(out_lines))
    check("第 3 行仍是场景标签",
          "street" in out_lines[2] and "city" in out_lines[2], repr(out_lines))
    check("没有标签带换行前缀（不出现 \\nxxx）",
          all(not line.startswith("\\n") for line in out_lines), repr(out_lines))

    # ---------------- 7. skill_check 多行不误报 ----------------
    section("7. skill_check 在多行输出上不误报")
    skill = json.load(io.open(
        os.path.join(ROOT, "plugin", "skills", "anima-tag-master.json"),
        encoding="utf-8"))
    for label, positive in (
        ("模板四行", "best quality, masterpiece, highres\nfull body\n"
                     "castorice, black_dress, long_dress, standing\nstreet, city, night"),
        ("极简两行", "best quality, masterpiece, highres\ncastorice, purple hair"),
        ("多角色五行", "best quality, masterpiece\ncowboy shot, 2girls, walking together\n"
                       "girl A, looking at viewer, red dress\n"
                       "girl B, holding hands, blue dress\npark, sunset, outdoors"),
    ):
        verdict = skill_check.check_anima(
            positive, "worst quality, lowres, watermark", skill=skill)
        bad = [issue for issue in (verdict.get("issues") or [])
               if "\\n" in str(issue.get("detail")) or "newline" in str(issue.get("code"))]
        check("%s 无「换行导致非法标签」问题" % label, not bad, str(bad))
        check("%s 未因换行被判失败" % label, verdict.get("ok"), str(verdict.get("issues")))
    check("_split_tags 把换行当分隔符",
          skill_check._split_tags("a, b\nc")== ["a", "b", "c"],
          str(skill_check._split_tags("a, b\nc")))

    # ---------------- 8. 格式卫生 ----------------
    section("8. 格式卫生：无空行、行尾无逗号")
    samples = [
        ("完整输入", "高清，全身照，城市街道，夜晚，走到镜头前", make_ui(CAST)),
        ("多角色", "两个女角色在公园散步，高清，中景", make_ui(TWO)),
        ("极简", "一个蓝发少女", make_ui(CAST)),
        ("换装", "换成黑色长裙", make_ui(CAST)),
        ("空输入", "", make_ui(CAST)),
        ("纯中文无结构", "一个蓝发少女", make_ui([])),
    ]
    for label, text, ui in samples:
        core8 = nodes._compose_core(text, ui)
        lines8 = lines_of(core8["positive"])
        check("%s：没有空行" % label, all(line.strip() for line in lines8),
              repr(lines8))
        check("%s：每行不以逗号结尾" % label,
              all(not line.rstrip().endswith(",") for line in lines8), repr(lines8))
        check("%s：没有连续两个逗号" % label,
              all(",," not in line.replace(", ", ",") for line in lines8), repr(lines8))
        check("%s：debug.segment_lines 与实际行数一致" % label,
              core8["debug"].get("segment_lines") == len(lines8),
              "%s vs %d" % (core8["debug"].get("segment_lines"), len(lines8)))

    # ---------------- 9. LLM 路径保持多行 ----------------
    section("9. LLM 路径：多行 positive 原样保留、不拍平")
    original = nodes._call_llm_once
    try:
        core9 = nodes._compose_core("换成黑色长裙", make_ui(CAST))
        # mock 输出必须包含全部必填触发词，否则会先触发 llm_skill_check_failed，
        # 测不到「多行是否被拍平」这件事。
        # v11：来源校验会删掉「用户没说过」的标签（full body / standing / street…），
        # 所以这里只 mock 白名单内的标签，专门验证「多行结构是否被保留」。
        required = core9["debug"].get("injected_triggers") or []
        llm_positive = (
            "best quality, masterpiece, highres\n"
            "1girl, solo\n"
            + ", ".join(required))
        nodes._call_llm_once = lambda *a, **k: (llm_positive, "lowres", "", {}, {})
        result9 = nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            "换成黑色长裙", make_ui(CAST), core=core9)
        positive9 = str(result9.get("positive") or "")
        check("LLM 多行输出仍保持多行", len(positive9.split("\n")) == 3,
              repr(positive9))
        check("LLM 行内的 black_dress 保留", "black_dress" in positive9, positive9)
        check("tagdb 归一没有把行拍平",
              len(positive9.split("\n")) > 1
              and "castorice" in positive9.split("\n")[-1], repr(positive9))
    finally:
        nodes._call_llm_once = original

    # ---------------- 10. negative 保持单行 ----------------
    section("10. negative 保持单行（结构需求只针对 positive）")
    neg_core = nodes._compose_core("换成黑色长裙", make_ui(CAST))
    check("negative 不含换行", "\n" not in neg_core["negative"], repr(neg_core["negative"]))
    check("negative 有内容", bool(neg_core["negative"].strip()), repr(neg_core["negative"]))

    # ---------------- 汇总 ----------------
    print()
    print("=" * 66)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
