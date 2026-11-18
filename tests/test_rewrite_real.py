# -*- coding: utf-8 -*-
"""v2 真实本地 LLM `/nl2prompt/rewrite` 闭环 + P0 修复复验。

两阶段：
    A. 原 3 次实跑（遐蝶/假日皮肤）—— 核心指标：**negative 抑制词覆盖 11/11**
    B. 5 个 honkai 角色（流萤/昔涟/知更鸟/三月七/花火）—— P0（`bad_weight` 误判）
       修复前这些角色必定回退模板，修复后应 **used_llm=true / fallback==""**

设计说明（为什么不自己起 llama-server）：
    契约 §11 / `nodes._resolve_llm_target` 在 `mode == "local"` 时**必定**调用
    `runtime.ensure_runtime(llm)`。若测试进程先起一个、再交给 ComfyUI，
    ComfyUI 进程的 runtime 注册表是空的，会**再起第二个**（双份 5.6GB）。
    因此直接 POST local 模式 dict，让插件自己在 ComfyUI 进程内拉起（绑 GPU1、
    自动带 `--reasoning off`），停止走 `POST /nl2prompt/runtime/stop`。

用法：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\test_rewrite_real.py
    ... --phase a|b|all        # 默认 all
    ... --timeout 900

产物：`tests/_rewrite_real.json`（完整原文，供 docs/history/测试记录.md 引用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
API = "http://127.0.0.1:8199"
REPORT_PATH = HERE / "_rewrite_real.json"

GGUF = os.environ.get("NL2PROMPT_GGUF", r"D:\models\your-model.gguf")
GPU_INDEX = 1
SKILL_ID = "anima-tag-master"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 遐蝶「原皮」的 11 个区分性词（characters.json：activation_words 9 + negative_suppress 额外 2）
CAST_ORICE_DEFAULT_SUPPRESS = [
    "strapless dress", "purple dress", "frilled dress", "detached collar",
    "detached sleeves", "purple sleeves", "clothing cutout", "elbow gloves",
    "mismatched gloves", "white thighhighs", "gradient legwear",
]

# ---------------- A 阶段：原 3 次（lead 首轮指定）
A_CASES = [
    {
        "id": "A1", "label": "雨夜礼裙",
        "nl": "银发少女穿紫色礼裙站在雨后的霓虹街道上",
        "ui": {
            "character_rows": [{"id": "r1", "name": "遐蝶",
                                "trigger": "castorice, purple hair", "outfit_hint": "假日皮肤"}],
            "style_rows": [{"id": "s1", "name": "@nixeu", "trigger": "@nixeu, soft light"}],
            "expected_suppress": CAST_ORICE_DEFAULT_SUPPRESS,
        },
    },
    {
        "id": "A2", "label": "教室校服",
        "nl": "银发少女坐在教室窗边，穿校服，午后阳光",
        "ui": {
            "character_rows": [{"id": "r1", "name": "遐蝶",
                                "trigger": "castorice, purple hair", "outfit_hint": "假日皮肤"}],
            "style_rows": [{"id": "s1", "name": "@nixeu", "trigger": "@nixeu, soft light"}],
            "expected_suppress": CAST_ORICE_DEFAULT_SUPPRESS,
        },
    },
    {
        "id": "A3", "label": "海边泳装",
        "nl": "银发少女站在海边，穿泳装，夏日阳光",
        "ui": {
            "character_rows": [{"id": "r1", "name": "遐蝶",
                                "trigger": "castorice, purple hair", "outfit_hint": "假日皮肤"}],
            "style_rows": [{"id": "s1", "name": "@nixeu", "trigger": "@nixeu, soft light"}],
            "expected_suppress": CAST_ORICE_DEFAULT_SUPPRESS,
        },
    },
]

# ---------------- B 阶段：5 个 honkai 角色（P0 修复直接证据）
HONKAI_CASES = [
    {"id": "B1", "label": "流萤", "nl": "流萤站在夜晚的城市天台上",
     "trigger": "firefly (honkai: star rail)"},
    {"id": "B2", "label": "昔涟", "nl": "昔涟站在清晨的庭院里",
     "trigger": "cyrene (honkai: star rail)"},
    {"id": "B3", "label": "知更鸟", "nl": "知更鸟站在舞台聚光灯下",
     "trigger": "robin (honkai: star rail)"},
    {"id": "B4", "label": "三月七", "nl": "三月七站在雪地里微笑",
     "trigger": "march 7th (honkai: star rail)"},
    {"id": "B5", "label": "花火", "nl": "花火站在剧场中央",
     "trigger": "sparkle (honkai: star rail)"},
]

RESULTS = []

#: `analysis` 长度硬上限（Lead 裁决 2026-11-17：由 60 放宽到 100）
ANALYSIS_MAX_CHARS = 100


def check(item: str, name: str, ok: bool, evidence: str = "", observe: bool = False) -> bool:
    RESULTS.append((item, name, bool(ok), evidence, observe))
    flag = ("观测满足" if ok else "观测不满足") if observe else ("PASS" if ok else "FAIL")
    print(f"[{flag}] {item} | {name}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"       {line}")
    return bool(ok)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def truncate(text, limit=700) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


def http_json(path: str, payload=None, timeout: float = 60.0):
    url = API + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if payload is not None else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw}
        return exc.code, body


def gpu_snapshot():
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total", "--format=csv"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return []
    if completed.returncode != 0:
        return []
    rows = []
    for line in (completed.stdout or "").strip().splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            used = int(re.sub(r"[^0-9]", "", parts[2]))
            total = int(re.sub(r"[^0-9]", "", parts[3]))
        except ValueError:
            continue
        rows.append({"index": parts[0], "name": parts[1], "used": used, "total": total})
    return rows


def gpu_by_index(snapshot, index):
    for row in snapshot:
        if row["index"] == str(index):
            return row
    return None


def llama_server_processes():
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV"],
            capture_output=True, timeout=15,
        )
        text = (completed.stdout or b"").decode("gbk", errors="replace")
        return [line for line in text.splitlines() if "llama-server" in line.lower()]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- 请求体


def build_llm():
    return {
        "mode": "local", "provider": "custom", "base_url": "", "model": "",
        "cloud_model": "", "gpu_index": GPU_INDEX,
        "gpu_name": "NVIDIA GeForce RTX 2060", "local_path": GGUF,
        "sampling": {"top_k": 40, "top_p": 0.9, "temperature": 0.7,
                     "max_tokens": 1024, "seed": -1},
        "runtime": {"ctx": 8192, "timeout": 180},
    }


def build_ui(case_ui: dict):
    """holder（契约 §3/§14.4）；skill_id 在 ui 子对象内。"""
    return {
        "schema": 1,
        "character_rows": case_ui.get("character_rows") or [],
        "style_rows": case_ui.get("style_rows") or [],
        "text_map": [],
        "system_prompt": "",
        "negative_base": "",  # 走后端 settings 兜底
        "ui": {"skill_id": SKILL_ID, "target_profile": "anima"},
        "skill_id": SKILL_ID,  # 兼容平铺写法
        "dialog": [],
        "cloud_models": {"base_url": "", "models": [], "ts": 0},
    }


def expected_suppress(character_rows) -> list:
    """从插件模板逻辑算出「当前选择下应被压制的词」（= forbidden_tokens 真源）。"""
    sys.path.insert(0, str(PROJECT_ROOT / "plugin"))
    try:
        import nodes  # noqa: PLC0415
        core = nodes.compose_plain("x", {"character_rows": character_rows})
        source = (core.get("debug") or {}).get("negative_suppress_source") or {}
        return list(source.get("outfit") or [])
    except Exception as exc:  # noqa: BLE001
        print(f"    !! expected_suppress 计算失败：{type(exc).__name__}: {exc}")
        return []


def template_baseline(natural_language: str, ui: dict):
    sys.path.insert(0, str(PROJECT_ROOT / "plugin"))
    try:
        import nodes  # noqa: PLC0415
        core = nodes.compose_plain(natural_language, ui)
        return {"positive": core["positive"], "negative": core["negative"]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 传输层


DIRECT_STATE = {"ready": False, "base_url": "", "model": "", "runtime": None}


def direct_call(natural_language: str, ui: dict, timeout: float):
    """进程外直调 `nodes._rewrite_only`（复用本进程自起的 llama-server）。

    为什么需要：`/nl2prompt/rewrite` 路由的 debug 白名单只透传
    ``used_llm/skill_check/quality/fallback``，**拿不到 `negative_missing`**；
    直调可以拿到完整 debug。同时它加载的是**磁盘最新代码**，不受 8199 进程
    是否已重启的影响（可用于提前验证修复）。

    :returns: ``(http_like_status, response_dict)``，结构对齐路由返回。
    """
    sys.path.insert(0, str(PROJECT_ROOT / "plugin"))
    import nodes  # noqa: PLC0415
    import runtime  # noqa: PLC0415

    if not DIRECT_STATE["ready"]:
        local_llm = build_llm()
        info = runtime.ensure_runtime(local_llm)
        DIRECT_STATE["base_url"] = (info or {}).get("base_url") or ""
        DIRECT_STATE["model"] = (info or {}).get("model") or Path(GGUF).stem
        DIRECT_STATE["runtime"] = runtime
        DIRECT_STATE["ready"] = True
        print(f"    [direct] 运行时就绪 base_url={DIRECT_STATE['base_url']} "
              f"model={DIRECT_STATE['model']}")

    # 用 cloud 模式打同一个本地端点：跳过 ensure_runtime（避免二次加载），
    # 本地 llama-server 不校验 key，用 dummy 满足 cloud 分支的前置检查。
    os.environ.setdefault("ANIMA_LLM_API_KEY", "dummy-local-server-no-key-needed")
    cloud_llm = {
        "mode": "cloud", "provider": "custom",
        "base_url": DIRECT_STATE["base_url"],
        "model": DIRECT_STATE["model"], "cloud_model": DIRECT_STATE["model"],
        "sampling": {"top_k": 40, "top_p": 0.9, "temperature": 0.7,
                     "max_tokens": 1024, "seed": -1},
        "runtime": {"ctx": 8192, "timeout": 180},
    }
    result = nodes._rewrite_only(cloud_llm, natural_language, ui)
    if result.get("ok"):
        return 200, {
            "ok": True,
            "positive": result.get("positive", ""),
            "negative": result.get("negative", ""),
            "analysis": result.get("analysis", ""),
            "debug": result.get("debug") or {},  # 完整 debug（含 negative_missing）
        }
    return 200, {
        "ok": False,
        "error": result.get("error"),
        "detail": result.get("detail"),
        "debug": result.get("debug") or {},
    }


def direct_cleanup():
    if DIRECT_STATE.get("runtime") is not None:
        try:
            stopped = DIRECT_STATE["runtime"].stop_all()
        except Exception as exc:  # noqa: BLE001
            stopped = f"{type(exc).__name__}: {exc}"
        DIRECT_STATE["ready"] = False
        return stopped
    return []


# ---------------------------------------------------------------- 单例执行


def run_one(case: dict, phase: str, report: dict, timeout: float, transport: str = "http"):
    item = case["id"]
    section(f"{item}（{phase}）实跑：{case['nl']}")

    rows = case["ui"]["character_rows"]
    expected = case["ui"].get("expected_suppress")
    if expected is None:
        expected = expected_suppress(rows)
    ui = build_ui(case["ui"])
    llm = build_llm()

    gpu_before = gpu_snapshot()
    started = time.time()
    status, response = 0, {}
    try:
        if transport == "direct":
            status, response = direct_call(case["nl"], ui, timeout)
        else:
            status, response = http_json(
                "/nl2prompt/rewrite",
                {"llm": llm, "natural_language": case["nl"], "ui": ui},
                timeout=timeout,
            )
    except Exception as exc:  # noqa: BLE001
        response = {"exception": f"{type(exc).__name__}: {exc}"}
    elapsed = time.time() - started
    gpu_after = gpu_snapshot()
    peak = {"0": 0, "1": 0}
    for snapshot in (gpu_before, gpu_after):
        for row in snapshot:
            peak[row["index"]] = max(peak.get(row["index"], 0), row["used"])

    record = {
        "id": item, "phase": phase, "label": case.get("label"),
        "natural_language": case["nl"],
        "character_rows": rows,
        "expected_suppress": expected,
        "http_status": status, "elapsed_s": round(elapsed, 2), "gpu_peak": peak,
        "response": response,
    }
    report["cases"].append(record)

    verify_payload(item, expected, response, status, elapsed, peak, record)
    return record


def verify_payload(item, expected, response, status, elapsed, peak, record=None):
    """对一次实测结果做全部断言。

    **`run_one` 与 `--rejudge` 共用本函数**，保证「重跑」与「按新口径重判已有输出」
    使用完全相同的判据，不会出现两套口径。

    :param record: 既有结果记录（原地更新）；``None`` 时新建。
    """
    record = record if isinstance(record, dict) else {"id": item}
    ok = status == 200 and response.get("ok") is True
    check(item, "HTTP 200 且 ok:true", ok,
          f"HTTP {status} elapsed={elapsed:.2f}s "
          f"body={truncate(json.dumps(response, ensure_ascii=False), 450)}")
    if not ok:
        check(item, "（失败原文）error / detail / skill_check.issues", False,
              f"error={response.get('error')!r} detail={truncate(response.get('detail'), 400)}"
              f" debug={truncate(json.dumps(response.get('debug'), ensure_ascii=False), 700)}")
        return record

    positive = response.get("positive") or ""
    negative = response.get("negative") or ""
    analysis = response.get("analysis") or ""
    debug = response.get("debug") or {}
    skill = debug.get("skill_check") or {}
    quality = debug.get("quality") or {}
    negative_missing = debug.get("negative_missing")

    record.update({
        "positive": positive, "negative": negative, "analysis": analysis,
        "skill_check": skill, "quality": quality,
        "retried": debug.get("retried"),
        "negative_missing": negative_missing,
        "fallback": debug.get("fallback"),
    })

    # ---- P0 修复核心断言
    check(item, "used_llm:true（未回退模板）", debug.get("used_llm") is True,
          f"used_llm={debug.get('used_llm')!r}")
    check(item, "debug.fallback == \"\"", debug.get("fallback") in ("", None),
          f"fallback={debug.get('fallback')!r}")
    check(item, "debug.skill_check.ok == true", skill.get("ok") is True,
          f"skill_check={truncate(json.dumps(skill, ensure_ascii=False), 420)}")
    check(item, "skill_check.issues 为空",
          not (skill.get("issues") or []),
          f"issues={json.dumps(skill.get('issues'), ensure_ascii=False)}")

    # ---- 负向抑制覆盖（本轮核心）
    expected = list(expected or [])
    missing_now = [token for token in expected if token.lower() not in negative.lower()]
    check(item, f"negative 覆盖全部抑制词（{len(expected) - len(missing_now)}/{len(expected)}）",
          not missing_now,
          f"仍缺={json.dumps(missing_now, ensure_ascii=False)}；"
          f"合并前 LLM 漏词 negative_missing="
          f"{json.dumps(negative_missing, ensure_ascii=False)}")
    record["negative_covered"] = f"{len(expected) - len(missing_now)}/{len(expected)}"
    record["negative_missing_final"] = missing_now

    # ---- 其它通用断言
    cjk_ratio = len(CJK.findall(analysis)) / max(len(analysis.strip()), 1)
    check(item, f"analysis 非空且中文 ≤{ANALYSIS_MAX_CHARS} 字",
          bool(analysis.strip()) and len(analysis.strip()) <= ANALYSIS_MAX_CHARS
          and cjk_ratio >= 0.3,
          f"analysis={analysis!r} 长度={len(analysis.strip())}"
          f"（上限 {ANALYSIS_MAX_CHARS}，Lead 裁决 2026-11-17 由 60 放宽）")
    check(item, "debug.quality.score 为数值",
          isinstance(quality.get("score"), (int, float)),
          f"quality={truncate(json.dumps(quality, ensure_ascii=False), 260)}")
    check(item, "retried 字段存在（bool）",
          isinstance(debug.get("retried"), bool),
          f"retried={debug.get('retried')!r}（skill_check.retried={skill.get('retried')!r}）")

    # negative_missing 为「合并前」漏词，允许非空（已由后端补齐）
    check(item, "（观测）negative_missing 记录「合并前」漏词", True,
          f"negative_missing={json.dumps(negative_missing, ensure_ascii=False)}"
          + ("（LLM 原始输出漏了这些，已由 _merge_suppress_into_negative 补齐）"
             if negative_missing else "（LLM 原始输出已齐全）"),
          observe=True)

    print("")
    print(f"    >>> positive = {positive}")
    print(f"    >>> negative = {negative}")
    print(f"    >>> analysis = {analysis}（{len(analysis.strip())} 字）")
    print(f"    >>> skill_check = {json.dumps(skill, ensure_ascii=False)}")
    print(f"    >>> quality = {json.dumps(quality, ensure_ascii=False)}；"
          f"retried={debug.get('retried')}；negative_missing={json.dumps(negative_missing, ensure_ascii=False)}")
    print(f"    >>> 耗时 = {elapsed:.2f}s；GPU 峰值 = {json.dumps(peak)}")
    return record


def summary():
    """核心断言汇总；返回退出码（run 与 rejudge 共用）。"""
    section("汇总")
    core = [row for row in RESULTS if not row[4]]
    observed = [row for row in RESULTS if row[4]]
    passed = sum(1 for row in core if row[2])
    failed = len(core) - passed
    print(f"核心断言 {len(core)} 条：PASS={passed}  FAIL={failed}")
    if observed:
        obs_ok = sum(1 for row in observed if row[2])
        print(f"观测项 {len(observed)} 条（不计入判定）：满足={obs_ok} 不满足={len(observed) - obs_ok}")
    if failed:
        print()
        print("失败明细：")
        for item, name, ok, evidence, _o in core:
            if not ok:
                print(f"  - {item} | {name}")
                print(f"      {truncate(evidence, 700)}")
    print()
    print("按条目汇总：")
    for item in dict.fromkeys(row[0] for row in core):
        rows = [row for row in core if row[0] == item]
        ok_count = sum(1 for row in rows if row[2])
        print(f"  {item}: {ok_count}/{len(rows)} {'PASS' if ok_count == len(rows) else 'FAIL'}")
    print()
    print("RESULT: " + ("ALL PASS" if failed == 0 else f"{failed} FAILED"))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- 汇总与重判


def summarize_phases(cases):
    """A/B 两阶段的分组汇总断言（run 与 rejudge 共用）。"""
    section("实跑汇总")
    a_cases = [c for c in cases if str(c.get("phase", "")).startswith("A")]
    b_cases = [c for c in cases if str(c.get("phase", "")).startswith("B")]
    if a_cases:
        a_ok = [c for c in a_cases
                if c.get("negative_covered", "0/0").split("/")[0]
                == c.get("negative_covered", "0/0").split("/")[1]
                and c.get("negative_covered", "0/0").split("/")[1] != "0"]
        check("R-汇总A", f"A 组 negative 抑制覆盖全部到位（{len(a_ok)}/{len(a_cases)} 次）",
              len(a_ok) == len(a_cases) and bool(a_cases),
              "覆盖=" + json.dumps({c["id"]: c.get("negative_covered") for c in a_cases},
                                   ensure_ascii=False))
    if b_cases:
        b_llm = [c for c in b_cases
                 if ((c.get("response") or {}).get("debug") or {}).get("used_llm") is True]
        check("R-汇总B", f"B 组 honkai 角色全部 used_llm=true（{len(b_llm)}/{len(b_cases)}）",
              len(b_llm) == len(b_cases) and len(b_cases) >= 2, "详见各 B 用例")
        b_cov = {c["id"]: c.get("negative_covered") for c in b_cases}
        check("R-汇总B", "B 组 negative 抑制覆盖全部到位",
              all((c.get("negative_covered", "0/0").split("/")[0]
                   == c.get("negative_covered", "0/0").split("/")[1])
                  for c in b_cases),
              "覆盖=" + json.dumps(b_cov, ensure_ascii=False))


def rejudge(path):
    """按**当前判据**重判既有实测输出（不重新推理、不占显存）。

    判据与实跑完全同源（逐 case 复用 :func:`verify_payload`），另外补齐实跑时
    才有的四组断言（R0 环境 / B 组触发词 / 模板对照 / 清理），因此断言总数与
    一次完整实跑一致（97 条），可直接比对。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    env = data.get("env") or {}
    print(f"重判来源 : {path}")
    print(f"用例数   : {len(cases)}")
    print(f"当前判据 : ANALYSIS_MAX_CHARS={ANALYSIS_MAX_CHARS}"
          f"（Lead 裁决 2026-11-17：由 60 放宽到 100）")

    # ---- R0 环境前置（重判时复测 + 报告数据）
    section("0 前置检查（重判）")
    code, _stats = 0, {}
    try:
        code, _stats = http_json("/system_stats", timeout=10)
        online = code == 200
    except Exception:  # noqa: BLE001
        online = False
    check("R0", "ComfyUI 8199 在线", online, f"HTTP {code}")
    check("R0", "gguf 文件存在", Path(GGUF).is_file(), GGUF)
    gpu_now = gpu_snapshot()
    card_now = gpu_by_index(gpu_now, GPU_INDEX)
    check("R0", f"GPU{GPU_INDEX} 空闲", bool(card_now) and card_now["used"] < 600,
          f"GPU{GPU_INDEX} used={card_now['used'] if card_now else '?'} MiB")
    check("R0", "无 llama-server 进程", not llama_server_processes())
    _, rt_status = http_json("/nl2prompt/runtime/status", timeout=10)
    check("R0", "/runtime/status 无运行实例", rt_status.get("running") == [],
          f"running={rt_status.get('running')}")

    # ---- 逐 case 重判（与实跑同一函数）
    for case in cases:
        section(f"{case.get('id')} 重判（复用已有实测输出，不重新推理）")
        verify_payload(
            case.get("id") or "?",
            case.get("expected_suppress") or [],
            case.get("response") or {},
            case.get("http_status") or 0,
            case.get("elapsed_s") or 0.0,
            case.get("gpu_peak") or {},
            case,
        )
        # B 组触发词（实跑时在 main 里断言，重判需一并补齐以对齐总数）
        if str(case.get("phase", "")).startswith("B"):
            rows = case.get("character_rows") or []
            trigger = (rows[0].get("trigger") if rows else "") or ""
            head = trigger.split(" (")[0].lower()
            positive = case.get("positive") or ""
            if head:
                check(case.get("id"), f"positive 保留角色触发词根 {head!r}",
                      head in positive.lower(), f"positive={truncate(positive, 320)}")

    # ---- 模板对照（用报告里已记录的 baseline + llm_positive 重算）
    section("对照：同一输入的模板结果（重判）")
    for row in data.get("baseline") or []:
        template = (row.get("template") or {}).get("positive") or ""
        llm_positive = row.get("llm_positive") or ""
        differs = bool(llm_positive) and llm_positive != template
        check(f"{row.get('id')}-对照", "LLM/skill 输出与模板结果不同（skill 生效）", differs,
              f"模板={truncate(template, 240)}" + chr(10) + f"LLM ={truncate(llm_positive, 240)}")

    # ---- 清理（报告数据 + 当前复测）
    section("清理：停止 llama-server 并确认 GPU 回落（重判）")
    stop = data.get("stop") or {}
    check("R-cleanup", "POST /nl2prompt/runtime/stop 成功（记录值）",
          stop.get("http_status") == 200,
          f"body={truncate(json.dumps(stop.get('body'), ensure_ascii=False), 200)}")
    gpu_after = env.get("gpu_after") or gpu_now
    card_after = gpu_by_index(gpu_after, GPU_INDEX)
    check("R-cleanup", f"GPU{GPU_INDEX} 已回落（<300 MiB）",
          bool(card_after) and card_after["used"] < 300,
          f"GPU{GPU_INDEX}={card_after['used'] if card_after else '?'} MiB")
    check("R-cleanup", "llama-server 进程已消失（复测）", not llama_server_processes())
    _, rt_after = http_json("/nl2prompt/runtime/status", timeout=10)
    check("R-cleanup", "/runtime/status 恢复为空（复测）", rt_after.get("running") == [],
          f"running={rt_after.get('running')}")

    summarize_phases(cases)
    return summary()


# ---------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--phase", choices=["a", "b", "all"], default="all")
    parser.add_argument("--transport", choices=["http", "direct"], default="http",
                        help="http=POST /nl2prompt/rewrite（需 8199 已重启到最新代码）；"
                             "direct=进程外直调 nodes._rewrite_only（加载磁盘最新代码，含完整 debug）")
    parser.add_argument("--rejudge", default="",
                        help="按当前判据重判既有报告（不重新推理）；传 _rewrite_real.json 路径")
    args = parser.parse_args()

    if args.rejudge:
        path = Path(args.rejudge)
        if not path.is_file():
            print(f"报告不存在: {path}")
            return 2
        return rejudge(path)

    print(f"API      : {API}")
    print(f"gguf     : {GGUF}")
    print(f"GPU 目标 : {GPU_INDEX}    phase={args.phase}  transport={args.transport}")
    report = {"cases": [], "env": {}, "phase": args.phase}

    section("0 前置检查")
    code, _stats = 0, {}
    try:
        code, _stats = http_json("/system_stats", timeout=10)
        online = code == 200
    except Exception:  # noqa: BLE001
        online = False
    check("R0", "ComfyUI 8199 在线", online, f"HTTP {code}")
    if not online and args.transport == "http":
        print("\nRESULT: FAILED（环境不可用；direct 模式可离线运行）")
        return 1
    if not online:
        print("    （direct 模式：8199 离线不影响本模式，跳过路由相关检查）")
    check("R0", "gguf 文件存在", Path(GGUF).is_file(), GGUF)

    before = gpu_snapshot()
    report["env"]["gpu_before"] = before
    card1 = gpu_by_index(before, GPU_INDEX)
    check("R0", f"GPU{GPU_INDEX} 起跑前空闲", bool(card1) and card1["used"] < 600,
          f"GPU{GPU_INDEX} used={card1['used'] if card1 else '?'} MiB")
    check("R0", "起跑前无 llama-server 进程", not llama_server_processes())
    if args.transport == "http":
        _, rt_status = http_json("/nl2prompt/runtime/status", timeout=10)
        check("R0", "/runtime/status 无运行实例", rt_status.get("running") == [],
              f"running={rt_status.get('running')}")

    # ---- A 阶段
    if args.phase in ("a", "all"):
        for case in A_CASES:
            run_one(case, "A:遐蝶/假日皮肤", report, args.timeout, args.transport)

    # ---- B 阶段（P0 复验）
    if args.phase in ("b", "all"):
        section("B 阶段：honkai 角色（P0 bad_weight 误判修复复验）")
        for case in HONKAI_CASES:
            rows = [{"id": "r1", "name": case["label"],
                     "trigger": case["trigger"], "outfit_hint": ""}]
            entry = {
                "id": case["id"], "label": case["label"], "nl": case["nl"],
                "ui": {"character_rows": rows, "style_rows": [], "expected_suppress": None},
            }
            record = run_one(entry, "B:honkai", report, args.timeout, args.transport)
            if record and record.get("positive"):
                trigger_head = case["trigger"].split(" (")[0].lower()
                check(case["id"], f"positive 保留角色触发词根 {trigger_head!r}",
                      trigger_head in record["positive"].lower(),
                      f"positive={truncate(record['positive'], 320)}")

    # ---- 汇总
    summarize_phases(report["cases"])

    # ---- 对照（模板 vs LLM/skill）
    section("对照：同一输入的模板结果（不走 LLM/skill）")
    compare_cases = []
    if args.phase in ("a", "all"):
        compare_cases.extend(A_CASES)
    if args.phase in ("b", "all"):
        compare_cases.extend([{
            "id": c["id"], "nl": c["nl"],
            "ui": {"character_rows": [{"id": "r1", "name": c["label"],
                                       "trigger": c["trigger"], "outfit_hint": ""}],
                   "style_rows": []},
        } for c in HONKAI_CASES])
    for case in compare_cases:
        baseline = template_baseline(case["nl"], build_ui(case["ui"]))
        record = next((c for c in report["cases"] if c["id"] == case["id"]), {})
        llm_positive = record.get("positive") or ""
        differs = bool(llm_positive) and llm_positive != baseline.get("positive")
        report.setdefault("baseline", []).append({
            "id": case["id"], "natural_language": case["nl"],
            "template": baseline, "llm_positive": llm_positive, "differs": differs,
        })
        check(f"{case['id']}-对照", "LLM/skill 输出与模板结果不同（skill 生效）", differs,
              f"模板={truncate(baseline.get('positive'), 240)}\n"
              f"LLM ={truncate(llm_positive, 240)}")

    # ---- 清理
    section("清理：停止 llama-server 并确认 GPU 回落")
    stop_status, stop_body = 0, {}
    if args.transport == "direct":
        stopped = direct_cleanup()
        stop_status, stop_body = 200, {"ok": True, "stopped": stopped, "via": "runtime.stop_all()"}
        report["stop"] = {"http_status": stop_status, "body": stop_body}
        check("R-cleanup", "runtime.stop_all() 成功（进程外）", True,
              f"stopped={json.dumps(stopped, ensure_ascii=False)}")
    else:
        try:
            stop_status, stop_body = http_json("/nl2prompt/runtime/stop", {}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            stop_body = {"exception": f"{type(exc).__name__}: {exc}"}
        report["stop"] = {"http_status": stop_status, "body": stop_body}
        check("R-cleanup", "POST /nl2prompt/runtime/stop 成功", stop_status == 200,
              f"HTTP {stop_status} body={truncate(json.dumps(stop_body, ensure_ascii=False), 260)}")

    settled = None
    for attempt in range(30):
        time.sleep(1)
        snapshot = gpu_snapshot()
        card = gpu_by_index(snapshot, GPU_INDEX)
        if card is not None and card["used"] < 300:
            settled = (attempt + 1, card, snapshot)
            break
    if settled:
        check("R-cleanup", f"GPU{GPU_INDEX} 已回落（<300 MiB）", True,
              f"等待 {settled[0]}s 后 GPU{GPU_INDEX}={settled[1]['used']} MiB；"
              f"全卡={json.dumps(settled[2], ensure_ascii=False)}")
        report["env"]["gpu_after"] = settled[2]
    else:
        final = gpu_snapshot()
        report["env"]["gpu_after"] = final
        check("R-cleanup", f"GPU{GPU_INDEX} 已回落（<300 MiB）", False,
              f"30s 后仍未回落：{json.dumps(final, ensure_ascii=False)}")

    procs_after = llama_server_processes()
    report["env"]["llama_procs_after"] = procs_after
    check("R-cleanup", "llama-server 进程已消失", not procs_after, f"残留={procs_after[:3]}")
    if args.transport == "http":
        _, rt_after = http_json("/nl2prompt/runtime/status", timeout=10)
        check("R-cleanup", "/runtime/status 恢复为空", rt_after.get("running") == [],
              f"running={rt_after.get('running')}")

    # ---- 汇总
    code = summary()

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整报告: {REPORT_PATH}")
    return code


if __name__ == "__main__":
    sys.exit(main())
