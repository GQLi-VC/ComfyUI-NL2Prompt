# -*- coding: utf-8 -*-
"""v11 来源校验（禁止编造）的单测。

对应 lead 报的三个 bug：
  A. 用户说的场景/视距/人数丢在 LLM 手里 -> 改成代码强制注入
  B. 第 2 行缺角色数量 -> 代码按 character_rows 推
  C. 整行编造（cowboy_shot / standing / neon_lights）-> 来源校验删除

运行：``python tools/check_tag_sources.py``
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))

import nodes  # noqa: E402

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
    print("-" * 68)
    print(title)
    print("-" * 68)


def make_ui(character_rows=None, style_rows=None):
    return {"schema": 1, "character_rows": character_rows or [],
            "style_rows": style_rows or [], "text_map": [],
            "system_prompt": "", "negative_base": "",
            "sampling": {"top_k": 40, "top_p": 0.9, "seed": -1},
            "ui": {"skill_id": "anima-tag-master"}}


CAST = [{"id": "r1", "name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}]


def main():
    # ---------------- 1. 归一 key ----------------
    section("1. _norm_tag_key 归一（下划线/空格/权重等价）")
    check("blue_hair == blue hair",
          nodes._norm_tag_key("blue_hair") == nodes._norm_tag_key("blue hair"),
          nodes._norm_tag_key("blue_hair"))
    check("(smile:1.2) == smile",
          nodes._norm_tag_key("(smile:1.2)") == nodes._norm_tag_key("smile"),
          nodes._norm_tag_key("(smile:1.2)"))
    check("大写不敏感",
          nodes._norm_tag_key("Blue Hair") == nodes._norm_tag_key("blue hair"))
    check("空值返回空串", nodes._norm_tag_key("") == "")

    # ---------------- 2. 维度分类 ----------------
    section("2. _tag_dimension 分类")
    cases = [
        ("best quality", "quality"), ("1girl", "count"), ("solo", "count"),
        ("cowboy_shot", "shot"), ("full body", "shot"),
        ("smile", "expression"), ("standing", "action"),
        ("looking at viewer", "gaze"), ("soft lighting", "lighting"),
        ("neon_lights", "scene"), ("street", "scene"), ("night", "scene"),
        ("blue hair", "hair"), ("blue eyes", "eyes"), ("black dress", "outfit"),
    ]
    for tag, expected in cases:
        check("%r -> %s" % (tag, expected),
              nodes._tag_dimension(tag) == expected, nodes._tag_dimension(tag))

    # ---------------- 3. 重排 ----------------
    section("3. _restructure_rows 按结构重排")
    mixed = [["masterpiece", "best quality"],
             ["1girl", "solo", "full body"],
             ["smile", "blue hair", "city", "street", "night"]]
    restructured = nodes._restructure_rows(mixed)
    check("变成 4 行", len(restructured) == 4, repr(restructured))
    check("第 1 行质量", restructured[0] == ["masterpiece", "best quality"], repr(restructured[0]))
    check("第 2 行视距+人数",
          set(restructured[1]) == {"1girl", "solo", "full body"}, repr(restructured[1]))
    check("第 3 行只剩角色标签",
          set(restructured[2]) == {"smile", "blue hair"}, repr(restructured[2]))
    check("第 4 行场景",
          set(restructured[3]) == {"city", "street", "night"}, repr(restructured[3]))

    # ---------------- 4. 人数推断 ----------------
    section("4. _infer_character_count")
    # v12：返回值是 (tags, assumed)；assumed 表示有角色缺 gender 被保守兜底
    tags, assumed = nodes._infer_character_count(make_ui())
    check("无角色 -> 1girl, solo", tags == ["1girl", "solo"], str(tags))
    check("无角色 -> gender_assumed=True", assumed is True, str(assumed))
    tags, assumed = nodes._infer_character_count(
        make_ui(CAST), {"遐蝶": "female"})
    check("1 个 female -> 1girl, solo", tags == ["1girl", "solo"], str(tags))
    check("1 个 female -> 不标记假设", assumed is False, str(assumed))
    two = CAST + [{"id": "r2", "name": "流萤",
                   "trigger": "firefly (honkai: star rail)", "outfit_hint": "原皮"}]
    tags, assumed = nodes._infer_character_count(
        make_ui(two), {"遐蝶": "female", "流萤": "female"})
    check("2 个 female -> 2girls", tags == ["2girls"], str(tags))
    tags, assumed = nodes._infer_character_count(
        make_ui(CAST), {"遐蝶": "male"})
    check("1 个 male -> 1boy", tags == ["1boy"], str(tags))
    tags, assumed = nodes._infer_character_count(
        make_ui(two), {"遐蝶": "female", "流萤": "male"})
    check("1 female + 1 male -> 1girl, 1boy",
          tags == ["1girl", "1boy"], str(tags))
    tags, assumed = nodes._infer_character_count(make_ui(CAST), {})
    check("缺 gender -> 保守 1girl 且标记 assumed",
          tags == ["1girl", "solo"] and assumed is True, str((tags, assumed)))

    # ---------------- 5. 模板路径：人数进第 2 行 ----------------
    section("5. 模板路径：角色数量强制进第 2 行")
    core = nodes._compose_core("一个蓝发少女", make_ui())
    lines = core["positive"].split("\n")
    check("第 2 行含 1girl", "1girl" in lines[1], repr(lines))
    check("debug.count_inferred=True", core["debug"].get("count_inferred") is True)
    check("中文映射的 blue_hair 进了角色行（库名形式）",
          any("blue_hair" in line for line in lines), repr(lines))

    # ---------------- 6. 端到端：编造被删 ----------------
    section("6. 端到端：LLM 编造被来源校验删除")
    original = nodes._call_llm_once
    try:
        ui = make_ui()
        core = nodes._compose_core("一个蓝发少女", ui)
        required = core["debug"].get("injected_triggers") or []
        fabricated = (
            "masterpiece, best quality\n"
            "1girl, solo, cowboy_shot\n"
            "blue_hair, " + ", ".join(required) + ", standing, looking_at_viewer\n"
            "neon_lights, wet_pavement, indoors"
            if required else
            "masterpiece, best quality\n"
            "1girl, solo, cowboy_shot\n"
            "blue_hair, standing, looking_at_viewer\n"
            "neon_lights, wet_pavement, indoors")
        nodes._call_llm_once = lambda *a, **k: (fabricated, "lowres", "", {}, {})
        result = nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            "一个蓝发少女", ui, core=core)
        positive = str(result.get("positive") or "")
        lowered = positive.lower()
        debug = result.get("debug") or {}
        tagdb = debug.get("tagdb") or {}
        dropped = tagdb.get("dropped") or []
        dropped_tags = {item.get("tag", "").lower() for item in dropped}

        check("cowboy_shot 被删除", "cowboy_shot" not in lowered, positive)
        check("standing 被删除", "standing" not in lowered, positive)
        check("looking_at_viewer 被删除", "looking_at_viewer" not in lowered, positive)
        check("neon_lights 被删除", "neon_lights" not in lowered, positive)
        check("wet_pavement 被删除", "wet_pavement" not in lowered, positive)
        check("indoors 被删除", "indoors" not in lowered, positive)
        check("blue_hair 保留（用户说了蓝发）", "blue_hair" in lowered, positive)
        check("1girl 保留", "1girl" in lowered, positive)
        check("dropped 里记录了原因",
              bool(dropped) and all(item.get("reason") for item in dropped),
              json.dumps(dropped, ensure_ascii=False))
        check("dropped 覆盖了 cowboy_shot",
              "cowboy_shot" in dropped_tags, str(sorted(dropped_tags)))
        check("tag_sources 记录了每个保留标签的来源",
              bool(tagdb.get("tag_sources")), str(tagdb.get("tag_sources")))
        check("来源值在允许集合内",
              all(value in ("template", "user_text", "enforced_cn_map",
                            "count_inferred", "enforced")
                  for value in (tagdb.get("tag_sources") or {}).values()),
              str(set((tagdb.get("tag_sources") or {}).values())))
        check("仍是多行结构", len(positive.split("\n")) >= 2, repr(positive))
        check("场景行未被编造内容污染（没有残留行）",
              not any(word in lowered for word in
                      ("neon", "pavement", "indoors", "cowboy")), positive)
    finally:
        nodes._call_llm_once = original

    # ---------------- 7. 角色库特征不被误删 ----------------
    section("7. 角色库特征不被来源校验误删（方案 A 最大的风险点）")
    original = nodes._call_llm_once
    try:
        ui = make_ui(CAST)
        core = nodes._compose_core("一个蓝发少女", ui)
        required = core["debug"].get("injected_triggers") or []
        nodes._call_llm_once = lambda *a, **k: (
            "masterpiece, best quality\n1girl, solo\n" + ", ".join(required),
            "lowres", "", {}, {})
        result = nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            "一个蓝发少女", ui, core=core)
        positive = str(result.get("positive") or "")
        lowered = positive.lower()
        for tag in ("castorice", "purple_hair", "purple_eyes", "twintails",
                    "pointy_ears", "earrings"):
            check("角色库特征 %r 保留" % tag, tag in lowered, positive[:200])
    finally:
        nodes._call_llm_once = original

    # ---------------- 8. 用户原文英文有来源 ----------------
    section("8. 用户原文里的英文标签有来源（不会被误删）")
    original = nodes._call_llm_once
    try:
        ui = make_ui()
        core = nodes._compose_core("rainy neon street", ui)
        nodes._call_llm_once = lambda *a, **k: (
            "masterpiece, best quality\n1girl, solo\nneon street",
            "lowres", "", {}, {})
        result = nodes._rewrite_only(
            {"mode": "cloud", "provider": "custom",
             "base_url": "http://127.0.0.1:1/v1", "model": "mock"},
            "rainy neon street", ui, core=core)
        positive = str(result.get("positive") or "")
        check("英文词元在原文里 -> neon street 有来源",
              "neon" in positive.lower(), positive)
    finally:
        nodes._call_llm_once = original


    # ---------------- 9. 幂等 / 去重（lead 发现的 bug） ----------------
    section("9. _finalize_positive 幂等 + 无重复标签")
    ui9 = make_ui(CAST)
    core9 = nodes._compose_core("两个女角色在公园散步，中景，高清", ui9)
    raw9 = core9["positive"]

    first, info1 = nodes._finalize_positive(raw9, {}, core9, "两个女角色在公园散步", ui9)
    second, info2 = nodes._finalize_positive(first, {}, core9, "两个女角色在公园散步", ui9)
    check("同一份 positive 调两次结果一致（幂等）", first == second,
          "%r vs %r" % (first, second))

    for label, text in (("模板输出", raw9), ("finalize 一次", first),
                        ("finalize 两次", second)):
        tags = [t.strip() for line in text.split("\n")
                for t in line.split(",") if t.strip()]
        lowered = [t.lower().replace("_", " ") for t in tags]
        duplicates = sorted({t for t in lowered if lowered.count(t) > 1})
        check("%s：没有重复标签" % label, not duplicates, str(duplicates))

    # 行内也不能有重复（用例 3 的具体症状）
    for label, text in (("finalize 一次", first), ("finalize 两次", second)):
        bad_lines = []
        for line in text.split("\n"):
            items = [t.strip().lower().replace("_", " ")
                     for t in line.split(",") if t.strip()]
            if len(items) != len(set(items)):
                bad_lines.append(line)
        check("%s：没有行内重复标签" % label, not bad_lines, str(bad_lines))

    # 来源表条数应与最终标签数一致（lead 用 38 vs 37 定位的）
    tags_final = [t.strip() for line in first.split("\n")
                  for t in line.split(",") if t.strip()]
    sources_final = info1.get("tag_sources") or {}
    check("标签数与 tag_sources 条数一致",
          len(tags_final) == len(sources_final),
          "%d vs %d" % (len(tags_final), len(sources_final)))

    print()
    print("=" * 68)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
