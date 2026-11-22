# -*- coding: utf-8 -*-
"""v13 长句自然语言转化的专项测试。

用户实际输入是长句，不是短语。这里逐条验收 lead 给的 4 个用例
（用模板路径，**不依赖 LLM**，所以是确定性断言）。

运行：``python tools/check_longshot.py``
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))

import cn_tags  # noqa: E402
import nodes    # noqa: E402

PASS = 0
FAIL = 0

LONG1 = "一个穿着黑色长裙的少女站在雨夜的霓虹街头，回头微笑，长发被风吹起"
LONG2 = "高清画质的全身照，两个女角色在公园里散步，一个穿白裙一个穿黑裙，阳光明媚"
LONG3 = "特写镜头，蓝发少女眼神忧郁地看着窗外，雨滴打在玻璃上，昏暗的室内"
LONG4 = "一个蓝发少女"


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


def flat(core):
    return str(core["positive"]).lower()


def main():
    # ---------------- 1. 覆盖规模 ----------------
    section("1. cn_tags 覆盖规模（长句场景需要 500+）")
    total = len(cn_tags.CN_TAG_MAP)
    check("CN_TAG_MAP 至少 500 条（当前 %d）" % total, total >= 500, str(total))
    check("最长匹配优先表已按长度倒序",
          all(len(cn_tags._SORTED_PHRASES[i]) >= len(cn_tags._SORTED_PHRASES[i + 1])
              for i in range(len(cn_tags._SORTED_PHRASES) - 1)),
          "排序错误")

    # ---------------- 2. 长句话子串扫描 ----------------
    section("2. 长句扫描命中（不是整句精确匹配）")
    expect = {
        LONG1: ("black dress", "long dress", "long hair", "smile",
                "looking back", "neon lights", "rain", "night", "street"),
        LONG2: ("2girls", "full body", "walking", "park",
                "white dress", "black dress", "sunlight"),
        LONG3: ("close-up", "blue hair", "sad", "rain", "window", "glass",
                "dim lighting", "indoors"),
        LONG4: ("blue hair",),
    }
    for text, wanted in expect.items():
        result = cn_tags.translate(text)
        missing = [tag for tag in wanted if tag not in result["matched"]]
        check("%r 命中全部期望标签（%d 个）" % (text[:18], len(wanted)),
              not missing, "缺 %s" % missing)

    # ---------------- 3. 长句模板组装 ----------------
    section("3. 长句 -> 多行 positive（模板路径，确定性）")
    cases = [
        ("长句1", LONG1, ("black_dress", "long_dress", "long_hair", "smile",
                          "looking_back", "neon_lights", "rain", "night", "street")),
        ("长句2", LONG2, ("2girls", "full_body", "walking", "park",
                          "white_dress", "black_dress")),
        ("长句3", LONG3, ("close-up", "blue_hair", "sad", "rain",
                          "window", "glass", "dim_lighting", "indoors")),
    ]
    for label, text, wanted in cases:
        core = nodes._compose_core(text, make_ui())
        lowered = flat(core)
        missing = [tag for tag in wanted if tag not in lowered]
        check("%s：必须出现的标签齐全" % label, not missing, "缺 %s\n%s" % (missing, core["positive"]))
        check("%s：多行结构（>=3 行）" % label,
              len(core["positive"].split("\n")) >= 3, core["positive"])
        check("%s：无重复标签" % label,
              len(lowered.split(",")) == len({t.strip() for t in lowered.split(",")}),
              core["positive"])
        check("%s：debug.cn_mapped 非空" % label,
              bool(core["debug"].get("cn_mapped")), str(core["debug"].get("cn_mapped")))

    # ---------------- 4. walking 强制注入（lead 报的漏洞） ----------------
    section("4. character 段映射必须强制注入（walking 不能丢）")
    core2 = nodes._compose_core(LONG2, make_ui())
    check("walking 出现在 positive（cn_mapped 命中了就必须注入）",
          "walking" in flat(core2), core2["positive"])
    check("walking 在 debug.cn_mapped 里",
          "walking" in (core2["debug"].get("cn_mapped") or []),
          str(core2["debug"].get("cn_mapped")))

    # ---------------- 5. 残余中文不静默丢 ----------------
    section("5. 残余未映射中文透出（debug.zh_unmapped）")
    for label, text in (("长句1", LONG1), ("长句2", LONG2), ("长句3", LONG3)):
        core = nodes._compose_core(text, make_ui())
        check("%s：debug.zh_unmapped 有值（不静默丢）" % label,
              bool(core["debug"].get("zh_unmapped")),
              str(core["debug"].get("zh_unmapped")))

    # ---------------- 6. 回归：短句仍不编造 ----------------
    section("6. 回归：短句不编造（维度豁免的底线）")
    core4 = nodes._compose_core(LONG4, make_ui())
    lowered4 = flat(core4)
    lines4 = core4["positive"].split("\n")
    check("长句4「一个蓝发少女」不冒出 long_hair",
          "long_hair" not in lowered4, core4["positive"])
    check("长句4 不冒出 twintails", "twintails" not in lowered4, core4["positive"])
    check("长句4 不冒出场景（street/city/park/classroom）",
          not any(w in lowered4 for w in ("street", "city", "park", "classroom")),
          core4["positive"])
    check("长句4 不冒出视距（cowboy_shot/full_body/close-up）",
          not any(w in lowered4 for w in ("cowboy_shot", "full_body", "close-up")),
          core4["positive"])
    check("长句4 不冒出动作（standing/sitting/walking）",
          not any(w in lowered4 for w in ("standing", "sitting", "walking")),
          core4["positive"])
    check("长句4 行数很少（<=3 行）", len(lines4) <= 3, repr(lines4))
    check("长句4 保留 blue_hair（用户说了蓝发）",
          "blue_hair" in lowered4, core4["positive"])

    # ---------------- 7. 维度豁免规则本身 ----------------
    section("7. 维度豁免：hair / eyes 永不豁免")
    check("_ELABORATABLE_DIMENSIONS 不含 hair",
          "hair" not in nodes._ELABORATABLE_DIMENSIONS,
          str(sorted(nodes._ELABORATABLE_DIMENSIONS)))
    check("_ELABORATABLE_DIMENSIONS 不含 eyes",
          "eyes" not in nodes._ELABORATABLE_DIMENSIONS,
          str(sorted(nodes._ELABORATABLE_DIMENSIONS)))
    # v13.1：outfit 已被移出豁免名单（方案 C），这里只校验 scene 仍在
    check("_ELABORATABLE_DIMENSIONS 含 scene（场景仍允许细化）",
          "scene" in nodes._ELABORATABLE_DIMENSIONS,
          str(sorted(nodes._ELABORATABLE_DIMENSIONS)))

    # ---------------- 8. 组合词优先级 ----------------
    section("8. 组合词优先（黑色长裙 不被 长裙/黑裙 抢走）")
    result = cn_tags.translate("黑色长裙")
    check("黑色长裙 -> black dress + long dress",
          "black dress" in result["matched"] and "long dress" in result["matched"],
          str(result["matched"]))
    result2 = cn_tags.translate("一袭长裙")
    check("长裙 单独也命中 dress",
          any("dress" in tag for tag in result2["matched"]),
          str(result2["matched"]))
    result3 = cn_tags.translate("白裙")
    check("白裙 单独命中 white dress",
          "white dress" in result3["matched"], str(result3["matched"]))


    # ---------------- 9. v13.1：X色裙只给一个主标签 ----------------
    section("9. X色裙只给一个主标签（不再 dress + skirt 并存）")
    for zh, expect in (("白裙", "white dress"), ("黑裙", "black dress"),
                       ("红裙", "red dress"), ("蓝裙", "blue dress"),
                       ("紫裙", "purple dress"), ("粉裙", "pink dress")):
        result = cn_tags.translate(zh)
        matched = [tag.lower() for tag in result["matched"]]
        check("%s -> 只有 %s" % (zh, expect),
              matched == [expect], str(matched))
        check("%s 不出现 skirt" % zh,
              not any("skirt" in tag for tag in matched), str(matched))

    # 整个表里不能有「同一件衣服既 dress 又 skirt」的矛盾条目
    contradictions = []
    for zh, (_segment, tags) in cn_tags.CN_TAG_MAP.items():
        lowered = [tag.lower() for tag in tags]
        if any("dress" in tag for tag in lowered) and \
                any("skirt" in tag for tag in lowered):
            contradictions.append(zh)
    check("映射表里没有 dress+skirt 并存的矛盾条目",
          not contradictions, str(contradictions[:5]))

    # ---------------- 10. v13.1：outfit 维度取消豁免 ----------------
    section("10. outfit 维度取消豁免（方案 C）")
    check("_ELABORATABLE_DIMENSIONS 不含 outfit",
          "outfit" not in nodes._ELABORATABLE_DIMENSIONS,
          str(sorted(nodes._ELABORATABLE_DIMENSIONS)))
    core2 = nodes._compose_core(LONG2, make_ui())
    lowered2 = flat(core2)
    check("长句2 正片里没有任何 *_skirt（白裙/黑裙只出 dress）",
          "_skirt" not in lowered2, core2["positive"])
    check("长句2 出现 white_dress", "white_dress" in lowered2, core2["positive"])
    check("长句2 出现 black_dress", "black_dress" in lowered2, core2["positive"])
    check("长句2 不出现 pleated_skirt（LLM 编造的）",
          "pleated_skirt" not in lowered2, core2["positive"])

    print()
    print("=" * 68)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
