# -*- coding: utf-8 -*-
"""任务 A：NL2PromptComposer 纯函数验收（不需要启动 ComfyUI、不占显存）。

运行（用 ComfyUI 原 Python，不装任何包）：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\test_composer.py

覆盖条目
--------
B2 角色 LoRA 注入
B3 多服装隔离（原皮 / 假日皮肤 双向 + 零交叉）
B4 风格注入
A3 文字指定提示词（正/反/target=negative）
A6 文本输出端格式
A5 种子回填（节点 ui payload，API 侧可验证部分）
B5 LLM 可关闭
B7 异常 holder 健壮性
B8 底模校验

退出码：0 = 全部 PASS；1 = 存在 FAIL。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
from pathlib import Path

try:  # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PLUGIN_DIR = PROJECT_ROOT / "plugin"
if len(sys.argv) > 1:
    PLUGIN_DIR = Path(sys.argv[1]).resolve()

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# ---------------------------------------------------------------- 断言框架

RESULTS = []  # [(条目, 断言名, ok, 证据, observe)]

#: v3 / B1：Composer 的 RETURN_TYPES 由 5 项扩为 8 项
#: 0 positive / 1 negative / 2 text / 3 lora_stack / 4 debug
#: 5 positive_cond / 6 negative_cond / 7 model
#: 未接 clip 时两个 CONDITIONING 应为 [[], {}]；未接 model 时 MODEL 为 None。
EXPECTED_OUTPUTS = 8


def check(item: str, name: str, ok: bool, evidence: str = "", observe: bool = False) -> bool:
    """登记一条断言。``observe=True`` 为**观测项**：契约未强制，不计入 PASS/FAIL 判定，
    但如实打印「满足/不满足」，绝不隐藏。"""
    RESULTS.append((item, name, bool(ok), evidence, observe))
    if observe:
        flag = "观测满足" if ok else "观测不满足"
        print(f"[{flag}] {item} | {name}")
    else:
        print(f"[{'PASS' if ok else 'FAIL'}] {item} | {name}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"       {line}")
    return bool(ok)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def tokens(text: str) -> set:
    """按契约 split_tokens 语义切分（逗号/换行），用于两侧零交叉比较。"""
    if not isinstance(text, str):
        return set()
    raw = text.replace("\r", "\n").replace("\n", ",").split(",")
    return {piece.strip().lower() for piece in raw if piece.strip()}


def truncate(text: str, limit: int = 600) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


# ---------------------------------------------------------------- 导入被测模块

print(f"plugin dir : {PLUGIN_DIR}")
print(f"python     : {sys.version.split()[0]} ({sys.executable})")

compose_plain = None
NL2PromptComposer = None
store = None
IMPORT_ERROR = None
try:
    from nodes import compose_plain, NL2PromptComposer  # noqa: E402
except Exception:  # noqa: BLE001
    IMPORT_ERROR = traceback.format_exc()
    print("!! 导入 nodes 失败:\n" + IMPORT_ERROR)

try:
    import store  # noqa: E402
except Exception:  # noqa: BLE001
    store = None
    print("!! 导入 store 失败（B8 将判 FAIL）:\n" + traceback.format_exc())

if compose_plain is None:
    section("导入失败，无法继续")
    check("A-导入", "from nodes import compose_plain", False, IMPORT_ERROR or "")
    print("\n汇总: PASS=0 FAIL=1")
    sys.exit(1)

check("A-导入", "from nodes import compose_plain / NL2PromptComposer", True,
      f"compose_plain={compose_plain.__name__}, NL2PromptComposer 可用={NL2PromptComposer is not None}")


# ================================================================ B2 角色 LoRA 注入
section("B2 角色 LoRA 注入：character_rows=[{name:遐蝶, trigger:castorice, outfit_hint:''}]")

B2_UI = {"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": ""}]}
B2_NL = "一个少女站在街道上"
b2 = compose_plain(B2_NL, B2_UI)
b2_pos, b2_neg, b2_stack = b2["positive"], b2["negative"], b2["lora_stack"]
b2_lora_names = [item[0] for item in b2_stack]

check("B2", "positive 含 'castorice'", "castorice" in b2_pos, "positive=" + truncate(b2_pos, 400))
check("B2", "lora_stack 含 '遐蝶(ANI).safetensors'",
      "遐蝶(ANI).safetensors" in b2_lora_names,
      "lora_stack=" + json.dumps(b2_stack, ensure_ascii=False))
check("B2", "角色激活词一并注入（purple hair）", "purple hair" in b2_pos)
check("B2", "debug.characters 记录 ['遐蝶']",
      b2["debug"].get("characters") == ["遐蝶"],
      "debug.characters=" + json.dumps(b2["debug"].get("characters"), ensure_ascii=False))


# ================================================================ B3 多服装隔离
section("B3 多服装隔离（真实数据，双向 + 零交叉）")

# 从 plugin/data/characters.json 读取遐蝶真实字段，避免硬编码猜测
DATA_PATH = PLUGIN_DIR / "data" / "characters.json"
raw_chars = json.loads(DATA_PATH.read_text(encoding="utf-8"))
castorice = next(c for c in raw_chars["characters"] if c["display_name"] == "遐蝶")
OUTFIT_BY_NAME = {o["display_name"]: o for o in castorice["outfits"]}
print("遐蝶服装: " + ", ".join(OUTFIT_BY_NAME))
print("原皮 activation_words   = " + json.dumps(OUTFIT_BY_NAME["原皮"]["activation_words"], ensure_ascii=False))
print("原皮 negative_suppress  = " + json.dumps(OUTFIT_BY_NAME["原皮"]["negative_suppress"], ensure_ascii=False))
print("假日 activation_words   = " + json.dumps(OUTFIT_BY_NAME["假日皮肤"]["activation_words"], ensure_ascii=False))
print("假日 negative_suppress  = " + json.dumps(OUTFIT_BY_NAME["假日皮肤"]["negative_suppress"], ensure_ascii=False))

DEFAULT_WORDS = OUTFIT_BY_NAME["原皮"]["activation_words"]      # 原皮自身标签 → 选原皮时应进 positive
DEFAULT_SUPPRESS = OUTFIT_BY_NAME["原皮"]["negative_suppress"]  # 原皮区分性标签 → 选假时应进 negative
HOLIDAY_WORDS = OUTFIT_BY_NAME["假日皮肤"]["activation_words"]   # 假日自身标签 → 选假日时应进 positive
HOLIDAY_SUPPRESS = OUTFIT_BY_NAME["假日皮肤"]["negative_suppress"]  # 假日区分性标签 → 选原皮时应进 negative

B3_NL = "一个少女站在街道上"


def run_direction(hint: str) -> dict:
    ui = {"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": hint}]}
    return compose_plain(B3_NL, ui)


# ---- 方向 1：outfit_hint = 原皮
b3a = run_direction("原皮")
pos_a, neg_a = b3a["positive"], b3a["negative"]
outs = set(tokens(pos_a))
negs = set(tokens(neg_a))
overlap_a = outs & negs
print("\n--- 方向1 原皮 ---")
print("positive = " + pos_a)
print("negative = " + neg_a)

check("B3-原皮", "positive 含原皮自身词 strapless dress",
      "strapless dress" in pos_a, "命中=" + str("strapless dress" in pos_a))
check("B3-原皮", "positive 含原皮全部 activation_words",
      all(w.lower() in outs for w in DEFAULT_WORDS),
      "缺失=" + json.dumps([w for w in DEFAULT_WORDS if w.lower() not in outs], ensure_ascii=False))
check("B3-原皮", "negative 含假日皮肤区分词（layered dress / white skirt 等）",
      all(w.lower() in negs for w in HOLIDAY_SUPPRESS),
      "缺失=" + json.dumps([w for w in HOLIDAY_SUPPRESS if w.lower() not in negs], ensure_ascii=False))
check("B3-原皮", "positive 不含假日皮肤激活词（隔离）",
      not any(w.lower() in outs for w in HOLIDAY_WORDS),
      "误入 positive=" + json.dumps([w for w in HOLIDAY_WORDS if w.lower() in outs], ensure_ascii=False))
check("B3-原皮", "positive/negative 零交叉 token", not overlap_a,
      "交叉=" + json.dumps(sorted(overlap_a), ensure_ascii=False) + f" (pos={len(outs)} neg={len(negs)})")

# ---- 方向 2：outfit_hint = 假日皮肤
b3b = run_direction("假日皮肤")
pos_b, neg_b = b3b["positive"], b3b["negative"]
outs_b = set(tokens(pos_b))
negs_b = set(tokens(neg_b))
overlap_b = outs_b & negs_b
print("\n--- 方向2 假日皮肤 ---")
print("positive = " + pos_b)
print("negative = " + neg_b)

check("B3-假日", "positive 含假日皮肤自身词 layered dress",
      "layered dress" in pos_b, "命中=" + str("layered dress" in pos_b))
check("B3-假日", "positive 含假日皮肤全部 activation_words",
      all(w.lower() in outs_b for w in HOLIDAY_WORDS),
      "缺失=" + json.dumps([w for w in HOLIDAY_WORDS if w.lower() not in outs_b], ensure_ascii=False))
check("B3-假日", "negative 含原皮区分词（purple dress / clothing cutout 等）",
      all(w.lower() in negs_b for w in DEFAULT_SUPPRESS),
      "缺失=" + json.dumps([w for w in DEFAULT_SUPPRESS if w.lower() not in negs_b], ensure_ascii=False))
check("B3-假日", "positive 不含原皮激活词（隔离）",
      not any(w.lower() in outs_b for w in DEFAULT_WORDS),
      "误入 positive=" + json.dumps([w for w in DEFAULT_WORDS if w.lower() in outs_b], ensure_ascii=False))
check("B3-假日", "positive/negative 零交叉 token", not overlap_b,
      "交叉=" + json.dumps(sorted(overlap_b), ensure_ascii=False) + f" (pos={len(outs_b)} neg={len(negs_b)})")

# ---- 两方向互为镜像（契约 §5 语义：negative_suppress = 该服装自身的「区分性标签」）
# 契约保证的是：被选中的服装，其 negative_suppress 出现在**对侧**的 negative、且不出现在本侧 positive。
# 契约并**不**要求 negative_suppress 覆盖 activation_words 全量，故此处按前者断言。
check("B3-镜像", "原皮 negative_suppress ⊆ 方向2 的 negative（选假日时压制原皮）",
      all(w.lower() in negs_b for w in DEFAULT_SUPPRESS),
      "缺失=" + json.dumps([w for w in DEFAULT_SUPPRESS if w.lower() not in negs_b], ensure_ascii=False))
check("B3-镜像", "原皮 negative_suppress ∩ 方向2 的 positive = ∅",
      not any(w.lower() in outs_b for w in DEFAULT_SUPPRESS),
      "误入 positive=" + json.dumps([w for w in DEFAULT_SUPPRESS if w.lower() in outs_b], ensure_ascii=False))
check("B3-镜像", "假日 negative_suppress ⊆ 方向1 的 negative（选原皮时压制假日）",
      all(w.lower() in negs for w in HOLIDAY_SUPPRESS),
      "缺失=" + json.dumps([w for w in HOLIDAY_SUPPRESS if w.lower() not in negs], ensure_ascii=False))
check("B3-镜像", "假日 negative_suppress ∩ 方向1 的 positive = ∅",
      not any(w.lower() in outs for w in HOLIDAY_SUPPRESS),
      "误入 positive=" + json.dumps([w for w in HOLIDAY_SUPPRESS if w.lower() in outs], ensure_ascii=False))

# ---- 数据质量观察（不影响 B3 判定：契约未要求全覆盖，仅如实记录覆盖率）
uncovered_default = [w for w in DEFAULT_WORDS if w.lower() not in negs_b]
uncovered_holiday = [w for w in HOLIDAY_WORDS if w.lower() not in negs]
meta_ok = (not uncovered_default) and (not uncovered_holiday)
check("B3-数据覆盖", "（观察项）activation_words 是否被对侧 negative 全量压制",
      meta_ok,
      "原皮未被压制=" + json.dumps(uncovered_default, ensure_ascii=False)
      + " | 假日未被压制=" + json.dumps(uncovered_holiday, ensure_ascii=False)
      + " → 契约只要求压制 negative_suppress，此为非阻断观察项",
      observe=True)

excess_default = [w for w in DEFAULT_SUPPRESS if w.lower() not in {x.lower() for x in DEFAULT_WORDS}]
check("B3-数据覆盖", "（观察项）negative_suppress ⊆ activation_words（数据自洽性）",
      not excess_default,
      "原皮 negative_suppress 中不在 activation_words 的词=" + json.dumps(excess_default, ensure_ascii=False),
      observe=True)

# ---- lora_stack 差异：假日皮肤额外挂皮肤 LoRA
names_a = [x[0] for x in b3a["lora_stack"]]
names_b = [x[0] for x in b3b["lora_stack"]]
check("B3-lora", "原皮方向 lora_stack 只含角色 LoRA（无皮肤 LoRA）",
      "遐蝶(ANI).safetensors" in names_a and "遐蝶皮肤(ANI).safetensors" not in names_a,
      "lora_stack=" + json.dumps(names_a, ensure_ascii=False))
check("B3-lora", "假日皮肤方向 lora_stack 含角色 + 皮肤 LoRA",
      "遐蝶(ANI).safetensors" in names_b and "遐蝶皮肤(ANI).safetensors" in names_b,
      "lora_stack=" + json.dumps(names_b, ensure_ascii=False))


# ================================================================ B4 风格注入
section("B4 风格注入：style_rows=[{name:@nixeu, trigger:'@nixeu, soft light'}]")

B4_UI = {"style_rows": [{"name": "@nixeu", "trigger": "@nixeu, soft light"}]}
b4 = compose_plain("一个少女站在街道上", B4_UI)
b4_pos = b4["positive"]
check("B4", "positive 含 '@nixeu'", "@nixeu" in b4_pos, "positive=" + truncate(b4_pos, 300))
check("B4", "positive 含 trigger 附加词 'soft light'", "soft light" in b4_pos)
check("B4", "debug.styles 命中", bool(b4["debug"].get("styles")),
      "debug.styles=" + json.dumps(b4["debug"].get("styles"), ensure_ascii=False))


# ================================================================ A3 文字指定提示词
section("A3 文字指定提示词：text_map=[{find:霓虹街道, prompt:neon-lit street, target:positive}]")

A3_MAP_POS = [{"find": "霓虹街道", "prompt": "neon-lit street", "target": "positive"}]
A3_MAP_NEG = [{"find": "霓虹街道", "prompt": "neon-lit street", "target": "negative"}]

a3_hit = compose_plain("银发少女站在雨后的霓虹街道上", {"text_map": A3_MAP_POS})
check("A3", "NL 含「霓虹街道」→ positive 含 'neon-lit street'",
      "neon-lit street" in a3_hit["positive"],
      "positive=" + truncate(a3_hit["positive"], 300))

a3_miss = compose_plain("银发少女站在街边", {"text_map": A3_MAP_POS})
check("A3", "NL 不含「霓虹街道」→ positive 不含 'neon-lit street'（反向用例）",
      "neon-lit street" not in a3_miss["positive"],
      "positive=" + truncate(a3_miss["positive"], 300))

a3_neg = compose_plain("银发少女站在雨后的霓虹街道上", {"text_map": A3_MAP_NEG})
check("A3", "target=negative → 出现在 negative",
      "neon-lit street" in a3_neg["negative"],
      "negative=" + truncate(a3_neg["negative"], 300))
check("A3", "target=negative 时 positive 不含该 prompt",
      "neon-lit street" not in a3_neg["positive"])
check("A3", "debug.text_map_hits 有记录",
      len(a3_hit["debug"].get("text_map_hits") or []) == 1,
      "text_map_hits=" + json.dumps(a3_hit["debug"].get("text_map_hits"), ensure_ascii=False))

# 大小写不敏感子串（契约 §5）
a3_case = compose_plain("NEON 霓虹街道 NEON", {"text_map": A3_MAP_POS})
check("A3", "子串匹配（find 大小写不敏感 / 中文子串）", "neon-lit street" in a3_case["positive"])

# 其它 token 不受影响的保护用例
a3_neighbor = compose_plain("霓虹街道", {"text_map": [{"find": "霓虹街", "prompt": "X_TAG", "target": "positive"}]})
check("A3", "子串匹配（find 为更长 NL 的真子串）", "X_TAG" in a3_neighbor["positive"])


# ================================================================ A6 文本输出端格式
section("A6 文本输出端格式：^Positive:\\n.*\\n\\nNegative:\\n.* （re.S）")

TEXT_PATTERN = re.compile(r"^Positive:\n.*\n\nNegative:\n.*$", re.S)
a6_text = b3a["text"]
check("A6", "compose_plain().text 匹配契约格式", bool(TEXT_PATTERN.match(a6_text)),
      "text=" + truncate(a6_text, 300))
check("A6", "text 与 positive/negative 内容一致",
      a6_text == f"Positive:\n{pos_a}\n\nNegative:\n{neg_a}")

# 节点返回的 text（第 3 个输出）同样校验
node_helper = NL2PromptComposer()
a6_node = node_helper.compose("银发少女站在雨后的霓虹街道上", False,
                              json.dumps({"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}]},
                                         ensure_ascii=False))
a6_node_text = a6_node["result"][2]
check("A6", "NL2PromptComposer 节点返回的 text 匹配契约格式",
      bool(TEXT_PATTERN.match(a6_node_text)),
      "text 前 200 字=" + truncate(a6_node_text, 200))
check("A6", "节点 text 恰好 2 个段头（Positive/Negative 各一）",
      a6_node_text.count("Positive:") == 1 and a6_node_text.count("Negative:") == 1)


# ================================================================ A5 种子回填（API 侧可验证部分）
section("A5 种子回填：ui.anima_nl[0].seed / used_llm / fallback")

ui_payload = a6_node.get("ui", {}).get("anima_nl", [{}])[0]
check("A5", "节点 ui.anima_nl[0] 含 seed/used_llm/fallback 三个键",
      set(ui_payload) >= {"seed", "used_llm", "fallback"},
      "payload=" + json.dumps(ui_payload, ensure_ascii=False))
check("A5", "seed 为有效整数（seed=-1 时后端已生成实际种子）",
      isinstance(ui_payload.get("seed"), int) and ui_payload["seed"] >= 0,
      "seed=" + repr(ui_payload.get("seed")))

fixed_seed = 20261115
a5_fixed = node_helper.compose("测试", False, json.dumps({"sampling": {"seed": fixed_seed}}, ensure_ascii=False))
check("A5", "指定 seed 原样回填（sampling.seed=20261115）",
      a5_fixed["ui"]["anima_nl"][0]["seed"] == fixed_seed,
      "seed=" + repr(a5_fixed["ui"]["anima_nl"][0]["seed"]))

# 前端渲染「上轮种子」属 UI 视觉，API 侧只能确认数据到位
check("A5", "【待前端验证】节点正面小字『上轮种子: N』渲染", True,
      "API 侧数据已就绪（ui.anima_nl[0].seed），渲染需浏览器验证 → 见测试记录 A5",
      observe=True)


# ================================================================ B5 LLM 可关闭
section("B5 LLM 可关闭：use_llm=False")

b5 = node_helper.compose("银发少女站在雨后的霓虹街道上", False,
                         json.dumps({"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}]},
                                    ensure_ascii=False))
b5_debug = json.loads(b5["result"][4])
check("B5", f"正常返回 {EXPECTED_OUTPUTS} 个输出（result 长度 {EXPECTED_OUTPUTS}）",
      len(b5["result"]) == EXPECTED_OUTPUTS,
      "result 类型=" + json.dumps([type(x).__name__ for x in b5["result"]]))
# B1 新增输出：未接 clip -> 两个 CONDITIONING 为空条件；未接 model -> MODEL 为 None
check("B5", "B1 新增输出 result[5] 是 CONDITIONING 且未接 clip 时为 [[], {}]",
      b5["result"][5] == [[], {}], "result[5]=" + truncate(json.dumps(b5["result"][5], ensure_ascii=False), 120))
check("B5", "B1 新增输出 result[6] 是 CONDITIONING 且未接 clip 时为 [[], {}]",
      b5["result"][6] == [[], {}], "result[6]=" + truncate(json.dumps(b5["result"][6], ensure_ascii=False), 120))
check("B5", "B1 新增输出 result[7] 是 MODEL 且未接 model 时为 None",
      b5["result"][7] is None, "result[7]=" + repr(b5["result"][7]))
check("B5", "debug.used_llm is False", b5_debug.get("used_llm") is False,
      "debug.used_llm=" + repr(b5_debug.get("used_llm")))
check("B5", "debug.mode == 'template'", b5_debug.get("mode") == "template",
      "debug.mode=" + repr(b5_debug.get("mode")))
check("B5", "未发生 LLM 调用（fallback 为空）", not b5_debug.get("fallback"),
      "fallback=" + repr(b5_debug.get("fallback")))
check("B5", "ui.anima_nl[0].used_llm is False", b5["ui"]["anima_nl"][0]["used_llm"] is False)


# ================================================================ B7 异常 holder 健壮性
section("B7 异常 holder 健壮性：_ui_json 四种非法/边界值不得抛异常")

BAD_HOLDERS = [
    ("'{}'", "{}"),
    ("''", ""),
    ("'not json'", "not json"),
    ("'{\"character_rows\":null}'", '{"character_rows":null}'),
]
for label, raw in BAD_HOLDERS:
    try:
        out = node_helper.compose("一个少女", False, raw)
        ok = (isinstance(out, dict) and len(out.get("result", ())) == EXPECTED_OUTPUTS
              and isinstance(out["result"][0], str) and isinstance(out["result"][1], str)
              and TEXT_PATTERN.match(out["result"][2]) is not None)
        check("B7", f"_ui_json={label} 不抛异常且返回 {EXPECTED_OUTPUTS} 输出", ok,
              "positive=" + truncate(out["result"][0], 160) + " | negative=" + truncate(out["result"][1], 160))
    except Exception as exc:  # noqa: BLE001
        check("B7", f"_ui_json={label} 不抛异常且返回 {EXPECTED_OUTPUTS} 输出", False,
              f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3))

# 额外：None / 非字符串
for label, raw in [("None", None), ("12345", 12345), ("list", [1, 2])]:
    try:
        out = node_helper.compose("一个少女", False, raw)
        check("B7", f"_ui_json={label}（非字符串）不抛异常", len(out["result"]) == EXPECTED_OUTPUTS)
    except Exception as exc:  # noqa: BLE001
        check("B7", f"_ui_json={label}（非字符串）不抛异常", False, f"{type(exc).__name__}: {exc}")

# 极端：character_rows 为 [null] / 行缺字段
for label, holder in [
    ("character_rows=[null]", {"character_rows": [None]}),
    ("style_rows=[{}]", {"style_rows": [{}]}),
    ("text_map=[{find:1,prompt:2}]", {"text_map": [{"find": 1, "prompt": 2}]}),
    ("sampling=null", {"sampling": None}),
    ("unknown keys", {"foo": "bar", "schema": "x"}),
]:
    try:
        out = node_helper.compose("一个少女", False, json.dumps(holder, ensure_ascii=False))
        check("B7", f"holder {label} 不抛异常", len(out["result"]) == EXPECTED_OUTPUTS)
    except Exception as exc:  # noqa: BLE001
        check("B7", f"holder {label} 不抛异常", False, f"{type(exc).__name__}: {exc}")


# ================================================================ B8 底模校验
section("B8 底模校验：store.validate_lora_base_model")

if store is None:
    check("B8", "store 可导入", False, "import store 失败")
else:
    r_ill = store.validate_lora_base_model("昔涟(Illustrious).safetensors")
    check("B8", "『昔涟(Illustrious).safetensors』→ ok=False",
          r_ill.get("ok") is False,
          "返回=" + json.dumps(r_ill, ensure_ascii=False))

    r_sdxl = store.validate_lora_base_model("某某(SDXL).safetensors")
    check("B8", "『某某(SDXL).safetensors』→ ok=False", r_sdxl.get("ok") is False,
          "返回=" + json.dumps(r_sdxl, ensure_ascii=False))

    r_ani = store.validate_lora_base_model("遐蝶(ANI).safetensors")
    check("B8", "『遐蝶(ANI).safetensors』→ ok=True（Anima 兼容）", r_ani.get("ok") is True,
          "返回=" + json.dumps(r_ani, ensure_ascii=False))

    r_empty = store.validate_lora_base_model("")
    check("B8", "空文件名不抛异常（不阻断）", isinstance(r_empty, dict) and "ok" in r_empty,
          "返回=" + json.dumps(r_empty, ensure_ascii=False))

    # 交叉校验：characters.json 里所有 lora 都应通过底模校验
    all_lora = []
    for char in raw_chars["characters"]:
        if char.get("lora_name"):
            all_lora.append((char["display_name"], char["lora_name"]))
        for outfit in char.get("outfits") or []:
            if outfit.get("lora_name"):
                all_lora.append((f"{char['display_name']}/{outfit['display_name']}", outfit["lora_name"]))
    bad = [(owner, name, store.validate_lora_base_model(name))
           for owner, name in all_lora if not store.validate_lora_base_model(name).get("ok")]
    check("B8", "characters.json 全部 LoRA 通过底模校验", not bad,
          f"共 {len(all_lora)} 条；不合格=" + json.dumps(bad, ensure_ascii=False))


# ================================================================ 汇总
section("汇总")
core = [r for r in RESULTS if not r[4]]
observed = [r for r in RESULTS if r[4]]
total = len(core)
passed = sum(1 for r in core if r[2])
failed = total - passed
print(f"核心断言 {total} 条：PASS={passed}  FAIL={failed}")
if observed:
    obs_ok = sum(1 for r in observed if r[2])
    print(f"观测项 {len(observed)} 条（不计入判定）：满足={obs_ok}  不满足={len(observed) - obs_ok}")
if failed:
    print("\n失败明细：")
    for item, name, ok, evidence, _obs in core:
        if not ok:
            print(f"  - {item} | {name}")
            if evidence:
                print(f"      证据: {truncate(evidence, 500)}")
if observed:
    print("\n观测项明细（契约未强制，仅供 lead 决定是否优化数据层）：")
    for item, name, ok, evidence, _obs in observed:
        print(f"  - [{'满足' if ok else '不满足'}] {item} | {name}")
        if evidence:
            print(f"      {truncate(evidence, 500)}")
print("\n按条目汇总：")


def item_summary(item: str) -> None:
    rows = [r for r in core if r[0] == item]
    if not rows:
        return
    ok = sum(1 for r in rows if r[2])
    print(f"  {item}: {ok}/{len(rows)} {'PASS' if ok == len(rows) else 'FAIL'}")


for item in dict.fromkeys(r[0] for r in core):
    item_summary(item)

print("\nRESULT: " + ("ALL PASS" if failed == 0 else f"{failed} FAILED"))
sys.exit(0 if failed == 0 else 1)
