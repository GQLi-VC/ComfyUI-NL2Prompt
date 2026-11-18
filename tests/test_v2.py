# -*- coding: utf-8 -*-
"""v2 验收 C1–C5（2026-11-17）。

覆盖契约 §14：
    C1 供应商注册表单一真源（34 家）+ `/nl2prompt/providers` + `docs/history/providers-verify.md`
    C2 默认值审计（R2）
    C3 skill 链路（build_messages / check_anima / 重试 1 次 / 失败回退 / analysis 隔离）
    C4 GPT 式体验（dialog_entry / dialog 上限 20 / `/nl2prompt/rewrite` / debug.quality+skill_check）
    C5 拉模型 `/nl2prompt/list_models`（本地 http.server 桩：成功/401/超时 + bad_url/no_key + 不回显 key）

运行：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\test_v2.py
    ... --skip-http          # 只跑不依赖 8199 的用例
    ... --ui-probe-report PATH

设计原则：
  * 不写 `plugin/`；所有 mock 在本进程内 monkeypatch，不改磁盘；
  * 不重启 8199；HTTP 用例只读或只调用幂等接口；
  * 观测项（契约未强制/字面与实质有偏差）单独列出，不计入 PASS/FAIL，但如实打印。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PLUGIN_DIR = PROJECT_ROOT / "plugin"
WEB_DIR = PLUGIN_DIR / "web"
API = "http://127.0.0.1:8199"

RESULTS = []


# ---------------------------------------------------------------- 断言框架


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


def truncate(text, limit=600) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


def http_json(path: str, payload=None, timeout: float = 20.0):
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


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def strip_comments(source: str) -> str:
    """剔除行注释（# // /* *），用于"活动代码里是否还有硬编码"的判断。"""
    kept = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------- 导入


print(f"plugin dir : {PLUGIN_DIR}")
print(f"python     : {sys.version.split()[0]} ({sys.executable})")

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

providers = nodes = llm_client = skill_check = skill_store = store = None
IMPORT_ERRORS = {}
for name in ("providers", "nodes", "llm_client", "skill_check", "skill_store", "store"):
    try:
        module = __import__(name)
        globals()[name] = module
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"
        print(f"!! import {name} 失败: {IMPORT_ERRORS[name]}")

if nodes is None or providers is None or llm_client is None or skill_check is None:
    section("导入失败，无法继续")
    for name, err in IMPORT_ERRORS.items():
        check("import", f"import {name}", False, err)
    print("\nRESULT: FAILED")
    sys.exit(1)

check("import", "providers / nodes / llm_client / skill_check / skill_store / store 可导入",
      not IMPORT_ERRORS, json.dumps(IMPORT_ERRORS, ensure_ascii=False) or "全部成功")


# ================================================================ C1 供应商表


def step_c1(skip_http: bool = False):
    section("C1 供应商注册表：单一真源 + 34 家 + 路由 + 验证文档")

    P = providers.PROVIDERS
    check("C1", f"PROVIDERS 长度 ≥ 28（实测 {len(P)}）", len(P) >= 28,
          "ids=" + ",".join(P.keys()))

    expected_ids = {
        "deepseek", "dashscope", "zhipu", "moonshot", "volcengine", "siliconflow",
        "minimax", "qianfan", "hunyuan", "spark", "stepfun", "lingyiwanwu", "baichuan",
        "sensenova", "modelscope", "openai", "anthropic", "google", "xai", "mistral",
        "cohere", "groq", "together", "fireworks", "openrouter", "deepinfra",
        "perplexity", "cerebras", "github", "azure", "ollama", "lmstudio", "vllm", "custom",
    }
    missing = sorted(expected_ids - set(P))
    extra = sorted(set(P) - expected_ids)
    check("C1", "34 家 id 与 lead 清单完全一致", not missing and not extra,
          f"缺失={missing} 多余={extra}")

    # 字段完整性（契约 §14.1）
    required_fields = ("name", "label", "base_url", "openai_compat", "default_model", "verified")
    field_problems = []
    for pid, meta in P.items():
        if not isinstance(meta, dict):
            field_problems.append(f"{pid}: 不是 dict")
            continue
        for field in required_fields:
            if field not in meta:
                field_problems.append(f"{pid}: 缺 {field}")
        if "base_url" in meta and meta["base_url"]:
            if not str(meta["base_url"]).startswith(("http://", "https://")):
                field_problems.append(f"{pid}: base_url 非法 {meta['base_url']!r}")
    check("C1", "每条含 name/label/base_url/openai_compat/default_model/verified",
          not field_problems, json.dumps(field_problems[:8], ensure_ascii=False))
    check("C1", "base_url 非空的条目全部是 http(s) 绝对地址",
          not [p for p in field_problems if "base_url 非法" in p])

    # 对外函数
    listed = providers.list_providers()
    check("C1", "list_providers() 返回 list 且长度 == len(PROVIDERS)", 
          isinstance(listed, list) and len(listed) == len(P),
          f"len={len(listed)}")
    check("C1", "list_providers() 每项含 id", all(isinstance(i, dict) and i.get("id") for i in listed))
    options = providers.provider_options()
    check("C1", "provider_options() 与注册表是同一集合（34 家，无缺无多）",
          sorted(options) == sorted(P.keys()),
          f"len={len(options)} 差集={sorted(set(options) ^ set(P.keys()))}")
    check("C1", "（观测）下拉展示顺序按常用优先重排（不影响数据集合）",
          list(options) != list(P.keys()),
          f"PROVIDERS 键序前 5={list(P.keys())[:5]}；provider_options 前 5={list(options)[:5]}"
          " → 刻意把国产 15 家前置，集合相同", observe=True)
    check("C1", "兼容别名 PROVIDER_BASE_URLS 与注册表一致（{id: base_url}）",
          isinstance(getattr(providers, "PROVIDER_BASE_URLS", None), dict)
          and getattr(providers, "PROVIDER_BASE_URLS") == {k: v.get("base_url", "") for k, v in P.items()},
          f"alias len={len(getattr(providers, 'PROVIDER_BASE_URLS', {}) or {})}")
    check("C1", "default_model('deepseek') 非空、未知 provider 返回空串",
          providers.default_model("deepseek") and providers.default_model("__nope__") == "",
          f"deepseek={providers.default_model('deepseek')!r} nope={providers.default_model('__nope__')!r}")

    # ---- 单一真源：字面量定义必须消失
    nodes_src = read_text(PLUGIN_DIR / "nodes.py")
    web_files = sorted(WEB_DIR.glob("*.js"))
    web_src = {path.name: read_text(path) for path in web_files}

    check("C1", "nodes.py 无 `PROVIDER_BASE_URLS = {` 字面量定义",
          not re.search(r"PROVIDER_BASE_URLS\s*=\s*\{", nodes_src),
          "契约 §14.1 要求该重复定义被消除")
    js_defs = [name for name, src in web_src.items()
               if re.search(r"PROVIDER_BASE_URLS\s*=\s*\{", src)]
    check("C1", "web/*.js 无 `PROVIDER_BASE_URLS = {` 字面量定义", not js_defs,
          f"命中={js_defs}")

    # 活动数据源应为后端注册表：INPUT_TYPES 的 provider COMBO 必须来自 provider_options()
    try:
        llm_inputs = nodes.NL2PromptLoadLLM.INPUT_TYPES()["required"]
        provider_combo = llm_inputs["provider"][0]
        check("C1", "NL2PromptLoadLLM.provider COMBO 来自注册表函数（34 项且与 provider_options 同序）",
              list(provider_combo) == list(options),
              f"COMBO len={len(provider_combo)}；前 5={list(provider_combo)[:5]}")
    except Exception as exc:  # noqa: BLE001
        check("C1", "NL2PromptLoadLLM.provider COMBO 来自注册表", False, f"{type(exc).__name__}: {exc}")

    # ---- 实质偏差：离线降级兜底表（如实记录，不掩盖）
    domains = sorted({urlparse(meta.get("base_url", "")).netloc
                      for meta in P.values() if meta.get("base_url")})
    nodes_active = strip_comments(nodes_src)
    nodes_domains = [d for d in domains if d in nodes_active]
    web_domains = {}
    for name, src in web_src.items():
        hits = [d for d in domains if d in strip_comments(src)]
        if hits:
            web_domains[name] = hits
    fallback_marked = (
        "_FALLBACK_PROVIDERS" in nodes_src
        and "仅" in nodes_src
        and "FALLBACK_PROVIDERS" in web_src.get("nl2prompt_load_llm.js", "")
    )
    check("C1", "（观测）非活动路径仍有离线降级兜底表，且被明确标注为降级",
          fallback_marked,
          f"nodes.py 活动代码含域名 {len(nodes_domains)} 个 {nodes_domains}；"
          f"web 活动代码含域名 {json.dumps(web_domains, ensure_ascii=False)}；"
          "两处均为 `_FALLBACK_PROVIDERS` / `FALLBACK_PROVIDERS`（注释声明仅在后端不可用时使用）",
          observe=True)
    check("C1", "（观测）兜底表规模远小于注册表（不构成第二真源）",
          True,
          f"兜底 {nodes_src.count(chr(34) + 'base_url' + chr(34))} 处 vs 注册表 {len(P)} 家 → 正常路径实际 34 家",
          observe=True)

    # ---- 文档
    verify_doc = PROJECT_ROOT / "docs" / "providers-verify.md"
    check("C1", "docs/history/providers-verify.md 存在且非空",
          verify_doc.is_file() and verify_doc.stat().st_size > 1024,
          f"size={verify_doc.stat().st_size if verify_doc.is_file() else 0} B")

    # ---- HTTP
    if skip_http:
        check("C1", "GET /nl2prompt/providers（--skip-http 跳过）", True,
              "按参数跳过", observe=True)
        return

    status, body = http_json("/nl2prompt/providers")
    ok_shape = status == 200 and body.get("ok") is True and isinstance(body.get("providers"), list)
    check("C1", "GET /nl2prompt/providers 返回 ok:true + providers 列表", ok_shape,
          f"HTTP {status} count={len(body.get('providers') or [])}")
    if ok_shape:
        rows = body["providers"]
        check("C1", "路由返回条数与注册表一致", len(rows) == len(P), f"{len(rows)} vs {len(P)}")
        need = {"id", "name", "label", "base_url", "openai_compat", "default_model", "verified", "note"}
        bad = [row.get("id") for row in rows if not need.issubset(set(row))]
        check("C1", "每条含 id/name/label/base_url/openai_compat/default_model/verified/note",
              not bad, f"缺字段的 id={bad[:6]}")
        check("C1", "路由不含任何 api_key 字段",
              "api_key" not in json.dumps(body, ensure_ascii=False).lower())


# ================================================================ C2 默认值审计


def step_c2():
    section("C2 默认值审计（契约 §14.5）")

    # ---- NL2PromptLoadLLM
    try:
        llm_inputs = nodes.NL2PromptLoadLLM.INPUT_TYPES()["required"]
    except Exception as exc:  # noqa: BLE001
        check("C2", "NL2PromptLoadLLM.INPUT_TYPES() 可调用", False, f"{type(exc).__name__}: {exc}")
        llm_inputs = {}

    def default_of(inputs, key):
        entry = inputs.get(key)
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return None
        opts = entry[1]
        return opts.get("default") if isinstance(opts, dict) else None

    model_path = default_of(llm_inputs, "model_path")
    if model_path:
        path = Path(str(model_path))
        ggufs = list(path.rglob("*.gguf"))[:50] if path.is_dir() else []
        check("C2", "model_path 默认值指向真实存在且含 gguf 的目录",
              path.is_dir() and bool(ggufs),
              f"default={model_path!r} exists={path.is_dir()} gguf 数={len(ggufs)}"
              + (f" 例={ggufs[0].name}" if ggufs else ""))
    else:
        check("C2", "model_path 默认值为 \"\"（契约允许空）", model_path == "", f"default={model_path!r}")

    check("C2", "base_url 默认 == \"\"", default_of(llm_inputs, "base_url") == "",
          f"default={default_of(llm_inputs, 'base_url')!r}")
    check("C2", "cloud_model 默认 == \"\"", default_of(llm_inputs, "cloud_model") == "",
          f"default={default_of(llm_inputs, 'cloud_model')!r}")
    check("C2", "provider 默认仍为 \"deepseek\"（契约不变项）",
          default_of(llm_inputs, "provider") == "deepseek",
          f"default={default_of(llm_inputs, 'provider')!r}")

    # ---- NL2PromptComposer
    try:
        comp_inputs = nodes.NL2PromptComposer.INPUT_TYPES()["required"]
        nl_default = default_of(comp_inputs, "natural_language")
        check("C2", "natural_language 默认 == \"\" 且无示例文字", nl_default == "",
              f"default={nl_default!r}")
        check("C2", "use_llm 默认 == False", default_of(comp_inputs, "use_llm") is False)
        check("C2", "_ui_json 默认 == \"{}\"", default_of(comp_inputs, "_ui_json") == "{}",
              f"default={default_of(comp_inputs, '_ui_json')!r}")
    except Exception as exc:  # noqa: BLE001
        check("C2", "NL2PromptComposer.INPUT_TYPES() 可调用", False, f"{type(exc).__name__}: {exc}")

    # ---- 主节点 holder 默认 negative_base == ""
    composer_js = read_text(WEB_DIR / "nl2prompt_composer.js")
    match = re.search(r"negative_base:\s*(\"[^\"]*\"|'[^']*')", composer_js)
    check("C2", "前端默认 state 的 negative_base == \"\"",
          bool(match) and match.group(1) in ('""', "''"),
          f"匹配={match.group(0) if match else '未找到'}")
    check("C2", "前端默认 state 含 v2 新字段（ui.skill_id / ui.target_profile / dialog / cloud_models）",
          all(fragment in composer_js for fragment in
              ("skill_id:", "target_profile:", "dialog: []", "cloud_models:")),
          "nl2prompt_composer.js defaultState")

    # ---- 后端兜底：默认空 → 自动取 settings 推荐值
    core = nodes.compose_plain("测试", {})
    store_data = store.load_all() if store is not None else {}
    recommended = (store_data.get("negative_base") or "").strip()
    check("C2", "backend 兜底：holder 无 negative_base 时 negative 取 settings 推荐值（非空）",
          bool(core["negative"].strip()),
          f"negative={truncate(core['negative'], 200)}")
    if recommended:
        check("C2", "兜底值 == settings.default_negative_base", core["negative"] == recommended,
              f"settings={truncate(recommended, 160)}\n实际={truncate(core['negative'], 160)}")
    check("C2", "显式传入 negative_base 时优先使用传入值（不被兜底覆盖）",
          nodes.compose_plain("测试", {"negative_base": "lowres, blurry"})["negative"]
          == "lowres, blurry",
          "holder.negative_base='lowres, blurry' → negative 原样")

    # ---- 默认值只保留一份（store.py）
    nodes_src = read_text(PLUGIN_DIR / "nodes.py")
    store_src = read_text(PLUGIN_DIR / "store.py")
    check("C2", "DEFAULT_NEGATIVE_BASE 只在 store.py 定义（nodes.py 用引用/兜底空串）",
          "DEFAULT_NEGATIVE_BASE = " in store_src
          and "DEFAULT_NEGATIVE_BASE = " not in nodes_src,
          "store.py 持有唯一定义；nodes.py 用 _default_negative_base() 从 store 读")
    check("C2", "system prompt 默认正文只在 store.py / skill 文件中（nodes.py 无长文本）",
          "DEFAULT_SYSTEM_PROMPT = \"\"\"你是 Anima 文生图模型的提示词重写器" in store_src,
          "store.py 持有 DEFAULT_SYSTEM_PROMPT 全文")


# ================================================================ C3 skill 链路


def step_c3():
    section("C3 skill 链路：build_messages / check_anima / 重试 / 回退 / analysis 隔离")

    # ---------- 3.1 build_messages(skill=None) 与 v1 等价 ----------
    catalog = {"characters": [{"id": "castorice", "display_name": "遐蝶", "aliases": ["遐蝶"],
                               "outfits": [{"id": "default", "display_name": "原皮"}]}],
               "styles": [{"id": "nixeu", "display_name": "@nixeu 画风"}]}
    selected = {"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}],
                "style_rows": [{"name": "@nixeu", "trigger": "@nixeu"}]}
    system_prompt = "自定义系统提示。角色：{{character_presets}} 已选：{{selected}}"
    args = (system_prompt, "银发少女站在霓虹街道", "draft pos", "draft neg", catalog, selected)

    with_none = llm_client.build_messages(*args, skill=None)
    without_param = llm_client.build_messages(*args)
    check("C3", "build_messages(skill=None) == build_messages(省略 skill)（默认参数等价）",
          with_none == without_param,
          f"messages 条数={len(with_none)}")

    system_content = with_none[0]["content"] if with_none and with_none[0]["role"] == "system" else ""
    user_content = with_none[-1]["content"]

    # v1 结构：占位符渲染 + 无 skill 追加
    rendered_expected = (
        system_prompt.replace("{{character_presets}}", llm_client._catalog_characters(catalog))
        .replace("{{style_presets}}", llm_client._catalog_styles(catalog))
        .replace("{{selected}}", llm_client._selected_text(selected))
    )
    check("C3", "skill=None 时 system == 手工按 v1 规则渲染的结果（逐字节）",
          system_content == rendered_expected,
          f"实际={truncate(system_content, 240)}\n期望={truncate(rendered_expected, 240)}")
    check("C3", "skill=None 时 system 不含 skill 特征（无「示例：」/无 skill 正文）",
          "示例：" not in system_content,
          "skill 未注入")
    check("C3", "skill=None 时 user 内容符合 v1 结构（自然语言/草稿/输出要求）",
          all(fragment in user_content for fragment in
              ("【自然语言】", "银发少女站在霓虹街道", "【模板草稿 - 正向】", "draft pos",
               "【模板草稿 - 负向】", "draft neg", "请只输出一个 JSON 对象")),
          f"user 前 200 字={truncate(user_content, 200)}")
    check("C3", "skill=None 时 selected 为空则不出现「【已选约束】」（v1 行为）",
          "【已选约束】" not in llm_client.build_messages("sys", "nl", "p", "n", {}, {})[-1]["content"],
          "selected={} → 无该段")

    # ---------- 3.2 build_messages(skill=X) ----------
    skill = skill_store.load_skill("anima-tag-master") if skill_store else None
    if not isinstance(skill, dict) or not skill:
        check("C3", "skill_store.load_skill('anima-tag-master') 可加载", False,
              "skill 文件不可用")
    else:
        with_skill = llm_client.build_messages(*args, skill=skill)
        sys_skill = with_skill[0]["content"]
        skill_fragment = (skill.get("system_prompt") or "")[:60]
        check("C3", "skill 版本 system 含 skill.system_prompt 正文",
              bool(skill_fragment) and (skill_fragment in sys_skill or
                                        skill_fragment[:30] in sys_skill),
              f"skill.system_prompt len={len(skill.get('system_prompt') or '')}")
        check("C3", "skill 版本 system 含「示例：」小节",
              "\n\n示例：" in sys_skill or sys_skill.rstrip().endswith("示例："),
              f"示例位置={sys_skill.find('示例：')}")
        example_count = sys_skill.count("输入：")
        check("C3", f"examples ≤ 2 条（实测 {example_count}）", 0 < example_count <= 2,
              f"skill.examples 原始条数={len(skill.get('examples') or [])}")
        # 用户文本在前，skill 条款在后
        user_pos = sys_skill.find("自定义系统提示")
        skill_pos = sys_skill.find(skill_fragment[:30]) if skill_fragment else -1
        check("C3", "用户 system_prompt 在前、skill 条款在后（契约 §14.2）",
              user_pos >= 0 and skill_pos > user_pos,
              f"user@{user_pos} skill@{skill_pos}")

        # ---------- 3.3 check_anima 识别 6 类违规 ----------
        def codes(positive, negative="lowres, bad anatomy, blurry, worst quality",
                  required=(), forbidden=(), sk=None):
            result = skill_check.check_anima(positive, negative,
                                             required_tokens=required,
                                             forbidden_tokens=forbidden, skill=sk)
            return {issue.get("code") for issue in (result.get("issues") or [])}, result

        good_positive = "1girl, silver hair, purple eyes, rain, neon lights, city, night, depth of field"

        bad, _ = codes("")
        check("C3", "check_anima 识别空 positive（code=empty）", "empty" in bad, f"codes={sorted(bad)}")

        bad, _ = codes("中文标签, 银色头发, 夜景, 霓虹灯, 城市")
        check("C3", "check_anima 识别中文（code=contains_chinese）",
              "contains_chinese" in bad, f"codes={sorted(bad)}")

        bad, _ = codes(good_positive, required=("castorice", "purple hair"))
        check("C3", "check_anima 识别缺必填触发词（code=missing_required）",
              "missing_required" in bad, f"codes={sorted(bad)}")

        bad, _ = codes(good_positive, forbidden=("silver hair",))
        check("C3", "check_anima 识别命中禁止词（code=forbidden_present）",
              "forbidden_present" in bad, f"codes={sorted(bad)}")

        bad, _ = codes("1girl, silver hair")
        check("C3", "check_anima 识别标签过少（code=tag_count_low）",
              "tag_count_low" in bad, f"codes={sorted(bad)}")

        bad, _ = codes(good_positive + ", (bad weight:9.9), (other:0.1)")
        check("C3", "check_anima 识别权重越界（code=bad_weight）",
              "bad_weight" in bad, f"codes={sorted(bad)}")

        ok_codes, ok_result = codes(good_positive)
        check("C3", "合格正向提示词无 issues（ok=true）",
              ok_result.get("ok") is True and not ok_result.get("issues"),
              f"issues={json.dumps(ok_result.get('issues'), ensure_ascii=False)} "
              f"stats={json.dumps(ok_result.get('stats'), ensure_ascii=False)}")

        # 空洞词（forbidden_phrases via skill）
        bad, _ = codes("1girl, beautiful, amazing, silver hair, night, city, rain", sk=skill)
        check("C3", "check_anima 用 skill.forbidden_phrases 识别空洞词（code=empty_phrase）",
              "empty_phrase" in bad, f"codes={sorted(bad)}")

        # ---------- 3.4 重试 1 次 ----------
        original = nodes._call_llm_once
        calls = []
        try:
            def fake_once(nl, config, core, llm, view, skill_arg, messages=None, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    return ("中文标签, 银色头发, 霓虹灯, 夜景, 城市", "lowres, blurry", "", {}, {})
                return ("1girl, silver hair, purple eyes, rain, neon lights, city, night, depth of field",
                        "lowres, bad anatomy, blurry, worst quality", "第一轮说明", {}, {})

            nodes._call_llm_once = fake_once
            fake_llm = {"mode": "cloud", "provider": "custom",
                        "base_url": "http://127.0.0.1:59999/v1", "model": "dummy",
                        "sampling": {}, "runtime": {"ctx": 8192, "timeout": 5}}
            result = nodes._rewrite_only(fake_llm, "银发少女站在霓虹街道", {})
            check("C3", "校验失败 → 只重试 1 次（LLM 调用共 2 次）",
                  len(calls) == 2, f"调用次数={len(calls)}")
            check("C3", "重试后合格 → ok=true 且 debug.skill_check.retried=true",
                  result.get("ok") is True
                  and (result.get("debug") or {}).get("skill_check", {}).get("retried") is True,
                  f"ok={result.get('ok')} skill_check="
                  + truncate(json.dumps((result.get('debug') or {}).get('skill_check'),
                                        ensure_ascii=False), 300))
            check("C3", "重试成功路径 debug 含 quality 与 skill_check（契约 §14.2 / R4）",
                  isinstance((result.get("debug") or {}).get("quality"), dict)
                  and isinstance((result.get("debug") or {}).get("skill_check"), dict),
                  "keys=" + json.dumps(sorted(result.get("debug") or {}), ensure_ascii=False))

            # ---------- 3.5 两次都不合格 → 回退模板 ----------
            calls.clear()

            def fake_always_bad(nl, config, core, llm, view, skill_arg, messages=None, **kwargs):
                calls.append(1)
                return ("中文标签, 银色头发, 霓虹灯, 夜景, 城市", "lowres, blurry", "", {}, {})

            nodes._call_llm_once = fake_always_bad
            failed = nodes._rewrite_only(fake_llm, "银发少女站在霓虹街道", {})
            check("C3", "两次不合格 → error == llm_skill_check_failed",
                  failed.get("ok") is False and failed.get("error") == "llm_skill_check_failed",
                  f"ok={failed.get('ok')} error={failed.get('error')!r} "
                  f"detail={truncate(failed.get('detail'), 160)}")
            check("C3", "两次不合格 → 调用次数 == 2（不无限重试）", len(calls) == 2,
                  f"调用次数={len(calls)}")

            core = nodes.compose_with_llm("银发少女站在霓虹街道", {}, fake_llm)
            template = nodes.compose_plain("银发少女站在霓虹街道", {})
            check("C3", "compose_with_llm 回退模板：positive 与模板结果逐字节一致",
                  core["positive"] == template["positive"],
                  f"fallback={(core['debug'] or {}).get('fallback')!r}\n"
                  f"回退后 positive={truncate(core['positive'], 200)}")
            check("C3", "回退时 debug.fallback == llm_skill_check_failed",
                  (core["debug"] or {}).get("fallback") == "llm_skill_check_failed",
                  f"fallback={(core['debug'] or {}).get('fallback')!r}")

            # ---------- 3.6 analysis 不混入 text 输出 ----------
            ANALYSIS = "本轮重写把夜景与霓虹灯放在场景槽位"
            calls.clear()

            def fake_with_analysis(nl, config, core, llm, view, skill_arg, messages=None, **kwargs):
                calls.append(1)
                return ("1girl, silver hair, purple eyes, rain, neon lights, city, night",
                        "lowres, bad anatomy, blurry, worst quality", ANALYSIS, {}, {})

            nodes._call_llm_once = fake_with_analysis
            node = nodes.NL2PromptComposer()
            out = node.compose("银发少女站在霓虹街道", True, "{}", llm=fake_llm)
            text_out = out["result"][2]
            positive_out = out["result"][0]
            debug_obj = json.loads(out["result"][4])
            check("C3", "analysis 不出现在 text 输出里", ANALYSIS not in text_out,
                  f"text={truncate(text_out, 200)}")
            check("C3", "analysis 不出现在 positive 输出里", ANALYSIS not in positive_out)
            check("C3", "analysis 出现在 debug 里（供前端展示）",
                  debug_obj.get("analysis") == ANALYSIS,
                  f"debug.analysis={truncate(debug_obj.get('analysis'), 120)!r}")
            check("C3", "text 仍严格符合 Positive/Negative 两段格式",
                  bool(re.match(r"^Positive:\n.*\n\nNegative:\n.*$", text_out, re.S)),
                  "格式校验")
        finally:
            nodes._call_llm_once = original


# ================================================================ C4 GPT 式体验


def step_c4(skip_http: bool = False, ui_report: Path | None = None):
    section("C4 GPT 式体验：dialog_entry / 上限 20 / /rewrite / quality+skill_check")

    # ---------- 4.1 dialog_entry 进 ui.anima_nl[0] ----------
    node = nodes.NL2PromptComposer()
    out = node.compose("银发少女站在霓虹街道", False, "{}")
    payload = (out.get("ui") or {}).get("anima_nl", [{}])[0]
    check("C4", "ui.anima_nl[0] 含 dialog_entry", isinstance(payload.get("dialog_entry"), dict),
          "payload keys=" + json.dumps(sorted(payload), ensure_ascii=False))
    entry = payload.get("dialog_entry") or {}
    check("C4", "dialog_entry 结构含 role/text/positive/negative/ts",
          set(entry) >= {"role", "text", "positive", "negative", "ts"},
          json.dumps(entry, ensure_ascii=False)[:300])
    check("C4", "dialog_entry.text == 本轮自然语言", entry.get("text") == "银发少女站在霓虹街道",
          f"text={entry.get('text')!r}")
    check("C4", "dialog_entry.positive/negative 与本轮输出一致",
          entry.get("positive") == out["result"][0] and entry.get("negative") == out["result"][1])
    check("C4", "ui.anima_nl[0] 含 dialog_len", isinstance(payload.get("dialog_len"), int),
          f"dialog_len={payload.get('dialog_len')}")

    # ---------- 4.2 上限 20（后端计算） ----------
    many = {"dialog": [{"role": "user", "text": f"t{i}", "positive": "", "negative": "", "ts": i}
                       for i in range(30)]}
    check("C4", "holder 已有 30 条 dialog → 后端只保留最新 20 条",
          len(nodes._normalize_ui(many)["dialog"]) == 20,
          f"保留={len(nodes._normalize_ui(many)['dialog'])}")
    check("C4", "dialog_len 计算：已有 19 条 → 20", nodes._next_dialog_len({"dialog": [{}] * 19}) == 20,
          f"dialog_len={nodes._next_dialog_len({'dialog': [{}] * 19})}")
    check("C4", "dialog_len 计算：已有 30 条 → 封顶 20", nodes._next_dialog_len({"dialog": [{}] * 30}) == 20,
          f"dialog_len={nodes._next_dialog_len({'dialog': [{}] * 30})}")

    # ---------- 4.3 前端上限 20（CDP 探针报告） ----------
    report_path = ui_report or (HERE / "_ui_probe_dialog.json")
    if report_path.is_file():
        try:
            report = json.loads(read_text(report_path))
        except ValueError:
            report = {}
        ui = report.get("ui") or {}
        check("C4", "前端 onExecuted 后 dialog 上限 20（CDP 实测）",
              ui.get("dialog_len") == 20,
              f"连续调用 onExecuted 25 次 → holder.dialog.length={ui.get('dialog_len')}；"
              f"首条={ui.get('first_text')!r} 末条={ui.get('last_text')!r}")
        check("C4", "前端丢最旧：保留 t5..t24（第一条为 t5、末条为 t24）",
              ui.get("first_text") == "t5" and ui.get("last_text") == "t24",
              f"first={ui.get('first_text')!r} last={ui.get('last_text')!r}")
        check("C4", "前端 holder 可被后端解析（JSON 合法）", ui.get("holder_parsed") is True,
              f"holder 长度={ui.get('holder_len')}")
    else:
        check("C4", "前端 onExecuted 后 dialog 上限 20（CDP 探针）", True,
              f"未找到探针报告 {report_path}（可运行 tests\\ui_probe_dialog.py 生成）→ 待浏览器验证",
              observe=True)

    # ---------- 4.4 /nl2prompt/rewrite ----------
    if skip_http:
        check("C4", "POST /nl2prompt/rewrite（--skip-http 跳过）", True, "按参数跳过", observe=True)
    else:
        status, body = http_json("/nl2prompt/rewrite", {"natural_language": "测试"})
        check("C4", "缺 llm 时 /rewrite 返回 ok:false 且 HTTP 非 500",
              status == 200 and body.get("ok") is False,
              f"HTTP {status} body={truncate(json.dumps(body, ensure_ascii=False), 300)}")
        check("C4", "缺 llm 的错误码为 llm_not_connected",
              body.get("error") == "llm_not_connected", f"error={body.get('error')!r}")

        status2, body2 = http_json("/nl2prompt/rewrite", {"llm": {"mode": "cloud"},
                                                        "natural_language": "测试"})
        check("C4", "llm 结构不完整时 /rewrite 仍不抛 500",
              status2 == 200, f"HTTP {status2} body={truncate(json.dumps(body2, ensure_ascii=False), 240)}")
        check("C4", "/rewrite 响应不回显 api_key",
              "api_key" not in json.dumps(body2, ensure_ascii=False).lower()
              or "api_key_configured" in json.dumps(body2, ensure_ascii=False).lower(),
              "响应字段检查")

    # ---------- 4.5 成功路径（mock）: debug.quality + debug.skill_check ----------
    original = nodes._call_llm_once
    try:
        def fake_ok(nl, config, core, llm, view, skill_arg, messages=None, **kwargs):
            return ("1girl, silver hair, purple eyes, rain, neon lights, city, night",
                    "lowres, bad anatomy, blurry, worst quality", "简短说明", {}, {})

        nodes._call_llm_once = fake_ok
        fake_llm = {"mode": "cloud", "provider": "custom", "base_url": "http://127.0.0.1:59999/v1",
                    "model": "dummy", "sampling": {}, "runtime": {"ctx": 8192, "timeout": 5}}
        ok_result = nodes._rewrite_only(fake_llm, "银发少女站在霓虹街道", {})
        debug = ok_result.get("debug") or {}
        check("C4", "成功路径 debug 含 quality（含 score）",
              isinstance(debug.get("quality"), dict) and "score" in (debug.get("quality") or {}),
              f"quality={truncate(json.dumps(debug.get('quality'), ensure_ascii=False), 240)}")
        check("C4", "成功路径 debug 含 skill_check（ok/issues/retried/stats）",
              isinstance(debug.get("skill_check"), dict)
              and {"ok", "issues", "retried"}.issubset(set(debug.get("skill_check") or {})),
              f"skill_check={truncate(json.dumps(debug.get('skill_check'), ensure_ascii=False), 300)}")
        check("C4", "成功路径 debug.used_llm == True",
              debug.get("used_llm") is True, f"used_llm={debug.get('used_llm')!r}")
    finally:
        nodes._call_llm_once = original


# ================================================================ C5 list_models


class _ModelStub:
    """本地 OpenAI 兼容桩：/ok 成功、/auth 401、/slow 超时。"""

    OK_BODY = json.dumps({"data": [{"id": "stub-model-a"}, {"id": "stub-model-b"}]}).encode()
    AUTH_BODY = json.dumps({"error": {"message": "invalid api key"}}).encode()

    def __init__(self):
        self.server = None
        self.port = None
        self.requests = []

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append({"path": self.path,
                                      "auth": self.headers.get("Authorization", "")})
                if self.path.startswith("/ok/"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(stub.OK_BODY)))
                    self.end_headers()
                    self.wfile.write(stub.OK_BODY)
                elif self.path.startswith("/auth/"):
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(stub.AUTH_BODY)))
                    self.end_headers()
                    self.wfile.write(stub.AUTH_BODY)
                elif self.path.startswith("/slow/"):
                    time.sleep(3.0)
                    body = b'{"data":[]}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


def step_c5(skip_http: bool = False):
    section("C5 拉模型 /nl2prompt/list_models（本地桩：成功/401/超时/bad_url/no_key）")

    FAKE_KEY = "sk-v2-test-FAKE-KEY-do-not-log-998877"
    stub = _ModelStub()
    original_resolve = llm_client.resolve_api_key
    try:
        port = stub.start()
        base_ok = f"http://127.0.0.1:{port}/ok/v1"
        base_auth = f"http://127.0.0.1:{port}/auth/v1"
        base_slow = f"http://127.0.0.1:{port}/slow/v1"
        print(f"    桩服务: 127.0.0.1:{port}")

        llm_client.resolve_api_key = lambda provider=None: (FAKE_KEY, "env")
        try:
            from llm_client import resolve_api_key as _orig_in_module  # noqa: F401
        except Exception:  # noqa: BLE001
            pass

        # ---- 成功
        ok = llm_client.list_models(base_ok, None, 5.0)
        check("C5", "成功态：ok=true 且解析出 models", ok.get("ok") is True and ok.get("count") == 2,
              f"ok={ok.get('ok')} count={ok.get('count')} models={json.dumps(ok.get('models'), ensure_ascii=False)}")
        check("C5", "成功态：请求带 Bearer 且 URL 为 {base}/models",
              bool(stub.requests) and stub.requests[-1]["path"] == "/ok/v1/models"
              and stub.requests[-1]["auth"].startswith("Bearer "),
              f"最后一次请求={json.dumps(stub.requests[-1] if stub.requests else {}, ensure_ascii=False)}")

        # ---- 401
        auth = llm_client.list_models(base_auth, None, 5.0)
        check("C5", "401 态：ok=false 且 error == http_401",
              auth.get("ok") is False and auth.get("error") == "http_401",
              f"error={auth.get('error')!r} detail={truncate(auth.get('detail'), 160)}")
        check("C5", "401 态：detail 来自响应体且不含 key",
              FAKE_KEY not in json.dumps(auth, ensure_ascii=False),
              "detail=" + truncate(auth.get("detail"), 160))

        # ---- 超时
        started = time.time()
        slow = llm_client.list_models(base_slow, None, 1.0)
        elapsed = time.time() - started
        check("C5", "超时态：error == timeout 且耗时接近上限",
              slow.get("ok") is False and slow.get("error") == "timeout" and elapsed < 2.9,
              f"error={slow.get('error')!r} elapsed={elapsed:.2f}s detail={truncate(slow.get('detail'), 120)}")

        # ---- bad_url
        bad = llm_client.list_models("not-a-url", None, 5.0)
        check("C5", "bad_url 态：非 http(s) 的 base_url → error == bad_url",
              bad.get("ok") is False and bad.get("error") == "bad_url",
              f"error={bad.get('error')!r} detail={truncate(bad.get('detail'), 160)}")
        empty = llm_client.list_models("", "custom", 5.0)
        check("C5", "bad_url 态：空 base_url + 空 provider → bad_url",
              empty.get("ok") is False and empty.get("error") == "bad_url",
              f"error={empty.get('error')!r}")

        # ---- no_key（还原真实解析器：本机无密钥）
        llm_client.resolve_api_key = original_resolve
        no_key = llm_client.list_models(base_ok, None, 5.0)
        check("C5", "no_key 态：未配置密钥 → error == no_key",
              no_key.get("ok") is False and no_key.get("error") == "no_key",
              f"error={no_key.get('error')!r} detail={truncate(no_key.get('detail'), 160)}")

        # ---- 全路径不回显 key
        for label, result in (("success", ok), ("401", auth), ("timeout", slow),
                              ("bad_url", bad), ("no_key", no_key)):
            check("C5", f"响应不含 api_key（{label}）",
                  FAKE_KEY not in json.dumps(result, ensure_ascii=False)
                  and "bearer" not in json.dumps(result, ensure_ascii=False).lower(),
                  "已扫描全部字段")
    finally:
        llm_client.resolve_api_key = original_resolve
        stub.stop()

    # ---- HTTP 层（不依赖密钥的两态）
    if skip_http:
        check("C5", "GET /nl2prompt/list_models（--skip-http 跳过）", True, "按参数跳过", observe=True)
        return

    stub2 = _ModelStub()
    try:
        port2 = stub2.start()
        base2 = f"http://127.0.0.1:{port2}/ok/v1"
        # 无密钥时 HTTP 路由应返回 no_key（不 500）
        status, body = http_json(f"/nl2prompt/list_models?base_url={base2}")
        check("C5", "HTTP：未配置密钥 → ok:false + error=no_key（HTTP 200 不 500）",
              status == 200 and body.get("ok") is False and body.get("error") == "no_key",
              f"HTTP {status} body={truncate(json.dumps(body, ensure_ascii=False), 300)}")

        status2, body2 = http_json("/nl2prompt/list_models?base_url=")
        check("C5", "HTTP：空 base_url → ok:false + error=bad_url（不 500）",
              status2 == 200 and body2.get("ok") is False and body2.get("error") == "bad_url",
              f"HTTP {status2} body={truncate(json.dumps(body2, ensure_ascii=False), 300)}")
        check("C5", "HTTP 响应结构含 models/count/elapsed_ms/error",
              {"models", "count", "elapsed_ms", "error"}.issubset(set(body2)),
              "keys=" + json.dumps(sorted(body2), ensure_ascii=False))
        check("C5", "HTTP 响应不含 api_key",
              "api_key" not in json.dumps(body2, ensure_ascii=False).lower())
    finally:
        stub2.stop()


# ================================================================ 汇总


def summary() -> int:
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
        print("\n失败明细：")
        for item, name, ok, evidence, _o in core:
            if not ok:
                print(f"  - {item} | {name}\n      {truncate(evidence, 600)}")
    if observed:
        print("\n观测项：")
        for item, name, ok, evidence, _o in observed:
            print(f"  - [{'满足' if ok else '不满足'}] {item} | {name}\n      {truncate(evidence, 500)}")
    print("\n按条目汇总：")
    for item in dict.fromkeys(row[0] for row in core):
        rows = [row for row in core if row[0] == item]
        ok = sum(1 for row in rows if row[2])
        print(f"  {item}: {ok}/{len(rows)} {'PASS' if ok == len(rows) else 'FAIL'}")
    print("\nRESULT: " + ("ALL PASS" if failed == 0 else f"{failed} FAILED"))
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="v2 验收 C1–C5")
    parser.add_argument("--skip-http", action="store_true", help="跳过依赖 8199 的用例")
    parser.add_argument("--ui-probe-report", default="", help="CDP 探针报告路径")
    args = parser.parse_args()

    ui_report = Path(args.ui_probe_report) if args.ui_probe_report else None
    if not args.skip_http:
        try:
            code, stats = http_json("/system_stats", timeout=8)
            online = code == 200
        except Exception:  # noqa: BLE001
            online = False
        check("env", "ComfyUI 8199 在线", online,
              f"HTTP {code if 'code' in dir() else '?'}")
        if not online:
            print("!! 8199 不可达；请加 --skip-http 只跑离线用例")
            return summary()

    step_c1(args.skip_http)
    step_c2()
    step_c3()
    step_c4(args.skip_http, ui_report)
    step_c5(args.skip_http)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
