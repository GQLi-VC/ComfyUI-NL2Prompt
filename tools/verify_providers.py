#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供应商地址探活工具（契约 §14.1）。

做什么
------
遍历 :data:`plugin.providers.PROVIDERS`，对每家的 ``{base_url}/models`` 发一次
**不带 api_key** 的 GET，判定"地址是否有效"，并把结论写成 Markdown 报告
（默认 ``docs/history/providers-verify.md``）＋ JSON（stdout）。

判定规则（严格照契约 §14.1，不粉饰）
------------------------------------
============================  ================================================
响应                         结论
============================  ================================================
``401 / 403``                 ``verified = True``，备注"地址有效（缺鉴权）"
``2xx``                       ``verified = True``，备注"地址有效（HTTP 200）"
``404``                       ``verified = False``，备注"路径疑似错误"
``其它 HTTP 状态``            ``verified = False``，备注 ``http_<status>``（不猜）
DNS / 连接失败 / 超时         ``verified = False``，备注"网络不可达
                              （可能是本机网络/代理限制，不代表地址错）"
``base_url`` 为空             ``verified = False``，备注"base_url 为空，需用户填写"
============================  ================================================

**网络限制 ≠ 地址错误** —— 报告里这两类结论分开统计，不做合并。

用法
----
::

    # 只跑直连（尊重环境变量 HTTPS_PROXY，若存在）
    python tools/verify_providers.py

    # 直连 + 代理两轮对比（推荐；海外厂商往往必须走代理）
    python tools/verify_providers.py --proxy http://127.0.0.1:7890

    # 只探指定的几家
    python tools/verify_providers.py --only deepseek,openai,ollama

    # 把实测结论回填 provider.py 的 verified 字段（只回填拿到明确 HTTP 响应的条目）
    python tools/verify_providers.py --proxy http://127.0.0.1:7890 --apply-verified

    # 只跑代理轮，改超时，改报告路径
    python tools/verify_providers.py --proxy http://127.0.0.1:7890 \
        --rounds proxy --timeout 15 --out docs/history/providers-verify.md

只用标准库，不装任何包。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugin"
_PROVIDERS_PATH = _PLUGIN_DIR / "providers.py"

if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import providers  # noqa: E402  （必须先插 sys.path）

#: 本地地址——走代理时必须绕过，否则必然假阴性
_LOOPBACK = "127.0.0.1,localhost,::1"

#: 各结论的展示顺序（报告与摘要统一）
_CATEGORIES = (
    "http_200",
    "auth_401_403",
    "http_404_path",
    "unreachable",
    "no_base_url",
    "other_http",
)


# ---------------------------------------------------------------------------
# 网络环境切换
# ---------------------------------------------------------------------------


def _sanitize_url(url: str) -> str:
    """隐去代理 URL 里的凭据（``user:pass@`` → ``***@``）。"""
    try:
        text = str(url or "")
        if "@" not in text:
            return text
        head, _, tail = text.rpartition("@")
        scheme = ""
        rest = head
        if "//" in head:
            scheme, _, rest = head.partition("//")
            scheme += "//"
        return f"{scheme}***@{tail}"
    except Exception:  # noqa: BLE001
        return "***"


def _env_proxy_snapshot() -> dict:
    """当前进程可见的代理环境变量快照（值脱敏）。"""
    keys = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
    out = {}
    for key in keys:
        val = os.environ.get(key) or os.environ.get(key.upper()) or ""
        out[key] = _sanitize_url(val) if val else ""
    return out


def _install_network(proxy: str) -> str:
    """为当前轮次安装 urllib 的 opener，返回"生效模式"描述。

    * ``proxy`` 为空 → 直连语义：**显式清空代理**（``ProxyHandler({})``），
      避免被环境变量里的历史代理悄悄劫持，保证"直连"这一轮真的是直连。
    * ``proxy`` 非空 → 全部 http/https 走该代理，同时把回环地址加入
      ``no_proxy``，否则 ollama / lmstudio / vllm 必然探活失败。

    为什么必须重装 opener：``urllib.request.urlopen`` 使用模块级缓存的
    ``_opener``，一旦构建就不会再读环境变量；不重装则第二轮代理不生效。
    """
    if proxy:
        os.environ["no_proxy"] = _LOOPBACK
        os.environ["NO_PROXY"] = _LOOPBACK
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        urllib.request.install_opener(urllib.request.build_opener(handler))
        return f"显式代理 {_sanitize_url(proxy)}（no_proxy={_LOOPBACK}）"

    for key in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    return "直连（已清空代理环境变量）"


# ---------------------------------------------------------------------------
# 结论归类（诚实分类，见模块 docstring 表格）
# ---------------------------------------------------------------------------


def classify(result: dict) -> dict:
    """把 :func:`providers.probe` 的原始结果翻译成 ``verified`` + 分类 + 备注。"""
    status = result.get("status")
    error = str(result.get("error") or "")
    ok = bool(result.get("ok"))

    if error == "no_base_url":
        return {
            "verified": False,
            "category": "no_base_url",
            "detail": "-",
            "remark": "base_url 为空，需用户填写",
        }

    if status in (401, 403):
        return {
            "verified": True,
            "category": "auth_401_403",
            "detail": f"{status}",
            "remark": f"地址有效（缺鉴权，HTTP {status}）",
        }

    if ok and isinstance(status, int) and 200 <= status < 300:
        return {
            "verified": True,
            "category": "http_200",
            "detail": f"{status}",
            "remark": f"地址有效（HTTP {status}）",
        }

    if error == "http_404_path" or status == 404:
        return {
            "verified": False,
            "category": "http_404_path",
            "detail": "404" if status is None else f"{status}",
            "remark": "路径疑似错误",
        }

    if status is None:
        # 无状态码 = 连接层失败：DNS / 拒绝连接 / TLS / 超时 / 代理不可用
        return {
            "verified": False,
            "category": "unreachable",
            "detail": "unreachable",
            "remark": "**网络不可达**（可能是本机网络/代理限制，不代表地址错）",
        }

    return {
        "verified": False,
        "category": "other_http",
        "detail": f"{status}",
        "remark": f"未预期状态 HTTP {status}（不判定为地址错误，需人工复核）",
    }


# ---------------------------------------------------------------------------
# 单轮探测
# ---------------------------------------------------------------------------


def run_round(name: str, proxy: str, ids: list, timeout: float) -> dict:
    """跑一轮探测并返回结构化结果。"""
    mode = _install_network(proxy)
    results = []
    for pid in ids:
        raw = providers.probe(pid, timeout=timeout)
        verdict = classify(raw)
        meta = providers.PROVIDERS.get(pid) or {}
        results.append(
            {
                "id": pid,
                "label": str(meta.get("label") or pid),
                "base_url": str(meta.get("base_url") or ""),
                "openai_compat": bool(meta.get("openai_compat", True)),
                "needs_key": bool(meta.get("needs_key", True)),
                "registry_verified": bool(meta.get("verified", False)),
                "note": str(meta.get("note") or ""),
                "raw": raw,
                "verified": verdict["verified"],
                "category": verdict["category"],
                "detail": verdict["detail"],
                "remark": verdict["remark"],
            }
        )

    summary = {key: 0 for key in _CATEGORIES}
    for item in results:
        summary[item["category"]] = summary.get(item["category"], 0) + 1
    summary["total"] = len(results)
    summary["verified_true"] = sum(1 for item in results if item["verified"])
    summary["verified_false"] = sum(1 for item in results if not item["verified"])

    return {
        "name": name,
        "mode": mode,
        "proxy": _sanitize_url(proxy) if proxy else "",
        "env_proxy": _env_proxy_snapshot(),
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 合并各轮结论
# ---------------------------------------------------------------------------

#: 这两类结果"没有信息量"：它们不能证伪地址，也证明不了地址
_UNINFORMATIVE = ("unreachable", "no_base_url")


def merge_rounds(rounds: list) -> list:
    """把多轮结果合并成"每个 id 一条最有信息量的结论"。

    规则：**拿到明确 HTTP 响应**的轮次优先于 ``unreachable`` / ``no_base_url``。
    理由：本机直连被墙（unreachable）而代理可用时，唯一有效的判定来自代理轮；
    反之代理未监听（全网段 refused）而直连可用时，唯一有效的判定来自直连轮。
    按"最后一轮为准"会把有效结论全部丢掉，这是必须避免的失真。
    """
    by_id: dict = {}
    order: list = []
    for round_ in rounds:
        for item in round_["results"]:
            pid = item["id"]
            if pid not in by_id:
                by_id[pid] = dict(item)
                order.append(pid)
                continue
            prev = by_id[pid]
            if prev["category"] in _UNINFORMATIVE and item["category"] not in _UNINFORMATIVE:
                by_id[pid] = dict(item)
    return [by_id[pid] for pid in order]


def _round_usable(round_: dict) -> bool:
    """该轮是否产出了任何有信息量的结论。"""
    return any(item["category"] not in _UNINFORMATIVE for item in round_["results"])


# ---------------------------------------------------------------------------
# 回填注册表（可选，--apply-verified）
# ---------------------------------------------------------------------------

#: 只有**拿到明确 HTTP 响应**的分类才允许回填 ``verified``。
#: ``unreachable`` / ``no_base_url`` 不能证伪地址，回填会把网络限制误记成地址错误。
_APPLYABLE = ("http_200", "auth_401_403", "http_404_path", "other_http")


def apply_verified(results: list) -> list:
    """把实测结论回填到 ``plugin/providers.py`` 的 ``verified`` 字段。

    仅改写形如 ``        "verified": True,`` 的单行，不触碰其它任何内容。
    :param results: 应为 :func:`merge_rounds` 的合并结论（不是单轮）
    :return: ``[{"id","from","to"}, ...]``（实际发生的变更；无变更返回 ``[]``）
    """
    actionable = [item for item in results if item["category"] in _APPLYABLE]
    changes = [
        (item["id"], item["registry_verified"], item["verified"])
        for item in actionable
        if bool(item["registry_verified"]) != bool(item["verified"])
    ]
    if not changes:
        return []

    try:
        text = _PROVIDERS_PATH.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] 读取 providers.py 失败，跳过回填：{type(exc).__name__}", file=sys.stderr)
        return []

    wanted = {pid: val for pid, _old, val in changes}
    lines = text.splitlines(keepends=True)
    current = None
    hits = 0
    for idx, line in enumerate(lines):
        head = re.match(r'^    "([A-Za-z0-9_]+)": \{\s*$', line)
        if head:
            current = head.group(1)
            continue
        if (
            current in wanted
            and re.match(r'^        "verified": (True|False),\s*$', line)
        ):
            new_line = '        "verified": %s,\n' % ("True" if wanted[current] else "False")
            if line != new_line:
                lines[idx] = new_line
                hits += 1
            current = None

    if hits:
        try:
            _PROVIDERS_PATH.write_text("".join(lines), encoding="utf-8")
            print(f"[verify] 已回填 providers.py 的 {hits} 条 verified", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] 写入 providers.py 失败：{type(exc).__name__}", file=sys.stderr)
            return []

    # 内存与报告同步：回填后不再把"回填前的值"当作偏差展示
    for pid, _old, val in changes:
        meta = providers.PROVIDERS.get(pid)
        if isinstance(meta, dict):
            meta["verified"] = bool(val)
    for item in actionable:
        item["registry_verified"] = bool(item["verified"])

    return [{"id": pid, "from": bool(old), "to": bool(new)} for pid, old, new in changes]


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _md_escape(text) -> str:
    """表格单元格转义：竖线与换行会破坏 Markdown 表格。"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _bool_mark(flag: bool) -> str:
    return "✅ True" if flag else "❌ False"


def render_markdown(payload: dict) -> str:
    rounds = payload["rounds"]
    lines = []
    add = lines.append

    add("# 供应商地址探活报告")
    add("")
    add("> 本文件由 `tools/verify_providers.py` 自动生成，请勿手工编辑表格。")
    add("> 数据真源：`plugin/providers.py` 的 `PROVIDERS`（契约 §14.1）。")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 生成时间 | `{payload['generated_at']}` |")
    add(f"| 探测目标 | `{{base_url}}/models`（**不带 api_key**） |")
    add(f"| 超时 | `{payload['timeout_s']}s` / 请求 |")
    add(f"| 供应商条数 | `{payload['count']}` |")
    add(f"| Python | `{payload['python']}` |")
    add(f"| 轮次 | {', '.join(round_['name'] for round_ in rounds)} |")
    add(f"| 结论判定基准 | {_md_escape(payload.get('merge_basis', '各轮合并'))} |")
    add("")

    # ---- 判定口径 ----
    add("## 判定口径")
    add("")
    add("| 响应 | verified | 含义 |")
    add("|---|---|---|")
    add("| `401 / 403` | `True` | 地址有效，仅缺鉴权 |")
    add("| `2xx` | `True` | 地址与路径均有效 |")
    add("| `404` | `False` | 路径疑似错误 |")
    add("| 其它 HTTP 状态 | `False` | 如实上报，不猜测 |")
    add("| DNS/连接/超时 | `False` | **网络不可达**，不代表地址错 |")
    add("| `base_url` 为空 | `False` | 需用户填写 |")
    add("")

    # ---- 汇总 ----
    add("## 汇总（各轮对比）")
    add("")
    header = "| 轮次 | 网络模式 | 条目 | verified=True | 401/403 | 200 | 404 | 网络不可达 | base_url 空 | 其它 |"
    add(header)
    add("|---|---|---|---|---|---|---|---|---|---|")
    for round_ in rounds:
        s = round_["summary"]
        add(
            "| {name} | {mode} | {total} | **{vt}** | {auth} | {ok200} | {p404} | {unreach} | {nobase} | {other} |".format(
                name=round_["name"],
                mode=_md_escape(round_["mode"]),
                total=s["total"],
                vt=s["verified_true"],
                auth=s["auth_401_403"],
                ok200=s["http_200"],
                p404=s["http_404_path"],
                unreach=s["unreachable"],
                nobase=s["no_base_url"],
                other=s["other_http"],
            )
        )
    add("")

    # ---- 轮次可用性（诚实提示：某一轮整体不可用则其结论不可用于判定地址） ----
    dead_rounds = [round_ for round_ in rounds if not _round_usable(round_)]
    if dead_rounds:
        add("> ⚠️ **有轮次的网络链路整体不可用**：")
        for round_ in dead_rounds:
            add(
                f"> - `{round_['name']}` 轮 **0 家**拿到 HTTP 响应（全部 `unreachable`）。"
                "该轮结论**不能**用于判定地址对错——最可能的原因是代理端口未监听/未启动。"
            )
        add("> - 因此 `verified` 结论取自**能拿到 HTTP 响应的那一轮**（见上文判定基准）。")
        add("")

    # ---- 明细 ----
    for round_ in rounds:
        add(f"## 明细：{round_['name']} 轮")
        add("")
        add(f"- 网络模式：`{_md_escape(round_['mode'])}`")
        add(
            "- 链路可用性："
            + ("可用（至少一家拿到 HTTP 响应）" if _round_usable(round_)
               else "**整体不可用**（0 家拿到 HTTP 响应，仅能证明该链路不通）")
        )
        if round_["env_proxy"].get("http_proxy") or round_["env_proxy"].get("https_proxy"):
            add(
                f"- 环境代理：`http_proxy={round_['env_proxy'].get('http_proxy') or '-'}`, "
                f"`https_proxy={round_['env_proxy'].get('https_proxy') or '-'}`"
            )
        add("")
        add("| id | label | base_url | openai_compat | 探测结果 | verified | 备注 |")
        add("|---|---|---|---|---|---|---|")
        for item in round_["results"]:
            remark = _md_escape(item["remark"])
            note = _md_escape(item.get("note") or "")
            if note:
                remark = f"{remark}<br>📝 {note}"
            add(
                "| `{id}` | {label} | {base} | {compat} | {detail} | {ver} | {remark} |".format(
                    id=item["id"],
                    label=_md_escape(item["label"]),
                    base=f"`{_md_escape(item['base_url'])}`" if item["base_url"] else "*(空)*",
                    compat="✅" if item["openai_compat"] else "❌",
                    detail=_md_escape(item["detail"]),
                    ver=_bool_mark(item["verified"]),
                    remark=remark,
                )
            )
        add("")

    # ---- 两轮差异 ----
    if len(rounds) >= 2:
        base_round = rounds[0]
        add("## 直连 vs 代理 差异")
        add("")
        add(f"以 `{base_round['name']}` 轮为基准，列出与后续轮次结论不一致的条目：")
        add("")
        if dead_rounds:
            add(
                f"> ⚠️ 存在链路整体不可用的轮次（{', '.join(r_['name'] for r_ in dead_rounds)}），"
                "因此下表的差异**只反映网络链路差异，不反映地址对错**。"
            )
            add("")
        add("| id | label | " + " | ".join(f"{r_['name']} 轮" for r_ in rounds) + " | 说明 |")
        add("|---|---|" + "---|" * len(rounds) + "---|")
        diff_count = 0
        for idx, item in enumerate(base_round["results"]):
            row = [item]
            for other in rounds[1:]:
                if idx < len(other["results"]):
                    row.append(other["results"][idx])
            marks = [f"{r_['detail']} / {_bool_mark(r_['verified'])}" for r_ in row]
            verdicts = {r_["verified"] for r_ in row}
            cats = {r_["category"] for r_ in row}
            if len(verdicts) > 1 or len(cats) > 1:
                diff_count += 1
                if verdicts == {True}:
                    explain = "两轮均验证通过"
                elif True in verdicts:
                    if dead_rounds:
                        explain = "仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错）"
                    else:
                        explain = "**仅某一轮通过 → 网络链路差异**（非地址错误）"
                else:
                    explain = "两轮均未通过（不一致原因见各轮备注）"
                add(
                    f"| `{item['id']}` | {_md_escape(item['label'])} | "
                    + " | ".join(_md_escape(mark) for mark in marks)
                    + f" | {explain} |"
                )
        if diff_count == 0:
            add("| *(无差异)* | | " + " | ".join("—" for _ in rounds) + " | 两轮结论完全一致 |")
        add("")

    # ---- 未通过清单（诚实分类，基于合并结论） ----
    verdict = payload.get("verdict") or rounds[-1]["results"]
    failing = [item for item in verdict if not item["verified"]]
    network_fail = [item for item in failing if item["category"] == "unreachable"]
    notfound_fail = [item for item in failing if item["category"] == "http_404_path"]
    other_fail = [item for item in failing if item["category"] == "other_http"]
    nobase_fail = [item for item in failing if item["category"] == "no_base_url"]

    add("## 结论（合并各轮最大信息量）")
    add("")
    add("| 分类 | 家数 | verified |")
    add("|---|---|---|")
    add(f"| 已核实地址有效（2xx + 401/403） | {len([i for i in verdict if i['verified']])} | `True` |")
    add(f"| 网络不可达（不能证伪） | {len(network_fail)} | `False` |")
    add(f"| 路径疑似错误（404） | {len(notfound_fail)} | `False` |")
    add(f"| 其它 HTTP 状态（需人工复核） | {len(other_fail)} | `False` |")
    add(f"| `base_url` 为空 | {len(nobase_fail)} | `False` |")
    add("")

    add("## `verified: False` 清单")
    add("")
    add("**严格区分「本机网络/代理限制」与「地址或路径错误」**，两者不可混为一谈：")
    add("")

    add(f"### A. 网络不可达（{len(network_fail)} 家）—— 不代表地址错")
    add("")
    if network_fail:
        add("这些条目在**所有轮次**里都是 DNS/连接/超时失败——即本机找不到任何可用链路到达该地址。")
        add("结论只能是「未核实」，**不能**判为地址错误。要判定真伪，需要在能出网的机器上重跑，")
        add("或先启动代理后用 `--proxy` 重跑。")
        add("")
        for item in network_fail:
            add(f"- `{item['id']}`（{_md_escape(item['label'])}）：{_md_escape(item['base_url']) or '*(空)*'}")
    else:
        add("*无*")
    add("")

    add(f"### B. 404 —— 路径疑似错误（{len(notfound_fail)} 家）")
    add("")
    if notfound_fail:
        add("`{base_url}/models` 返回 404，说明域名可达但该路径下没有 Models 接口。")
        add("")
        for item in notfound_fail:
            add(f"- `{item['id']}`（{_md_escape(item['label'])}）：`{_md_escape(item['base_url'])}`")
    else:
        add("*无*")
    add("")

    add(f"### C. 其它 HTTP 状态 —— 需人工复核（{len(other_fail)} 家）")
    add("")
    if other_fail:
        add("这些状态码既不是 401/403（可判有效），也不是 404（可判路径错），因此**不猜测**，如实记录：")
        add("")
        for item in other_fail:
            note = _md_escape(item.get("note") or "")
            add(
                f"- `{item['id']}`（{_md_escape(item['label'])}）：`{_md_escape(item['detail'])}`"
                f" — {_md_escape(item['remark'])}"
                + (f"\n  - 📝 注册表备注：{note}" if note else "")
            )
    else:
        add("*无*")
    add("")

    add(f"### D. base_url 为空（{len(nobase_fail)} 家）—— 由用户填写")
    add("")
    if nobase_fail:
        for item in nobase_fail:
            add(f"- `{item['id']}`（{_md_escape(item['label'])}）：{_md_escape(item['remark'])}")
    else:
        add("*无*")
    add("")

    # ---- 通过清单 ----
    passing = [item for item in verdict if item["verified"]]
    add(f"## 已核实地址有效（{len(passing)} 家）")
    add("")
    if passing:
        for item in passing:
            add(f"- `{item['id']}`（{_md_escape(item['label'])}）：{_md_escape(item['detail'])} — {_md_escape(item['remark'])}")
    else:
        add("*无*")
    add("")

    # ---- 注册表回填记录 ----
    applied = payload.get("applied_verified") or []
    if applied:
        add(f"## 注册表回填记录（`--apply-verified`，{len(applied)} 条）")
        add("")
        add("已把下列实测结论写入 `plugin/providers.py` 的 `verified` 字段：")
        add("")
        add("| id | 回填前 | 回填后 |")
        add("|---|---|---|")
        for row in applied:
            add(f"| `{row['id']}` | {_bool_mark(row['from'])} | {_bool_mark(row['to'])} |")
        add("")
        add("> `unreachable` 与 `base_url` 为空的条目**不会**被回填——它们不能证伪地址。")
        add("")

    # ---- 与注册表的一致性 ----
    drift = [
        item
        for item in verdict
        if item["category"] not in _UNINFORMATIVE and item["verified"] != item["registry_verified"]
    ]
    add("## 与注册表 `verified` 字段的偏差")
    add("")
    add("只统计**已拿到明确 HTTP 响应**的条目（`unreachable` / `base_url` 空不参与，因为不能证伪地址）。")
    add("")
    if drift:
        add("| id | 注册表 verified | 实测 verified | 建议动作 |")
        add("|---|---|---|---|")
        for item in drift:
            add(
                f"| `{item['id']}` | {_bool_mark(item['registry_verified'])} | {_bool_mark(item['verified'])} | "
                f"把 `PROVIDERS['{item['id']}']['verified']` 改为 `{item['verified']}` |"
            )
    else:
        add("*无偏差（注册表与实测一致，或剩余偏差均属于网络不可达、无法下结论）*")
    add("")

    add("## 方法学说明")
    add("")
    add("- 探测只发 `GET {base_url}/models`，**不带** `Authorization`，因此 `401/403` 是「地址正确」的强信号。")
    add("- `403` 也可能由 CDN/WAF/地区封锁返回，其可信度低于 `401`；若某家长期只返回 `403`，请人工复核。")
    add("- 本地供应商（`ollama` / `lmstudio` / `vllm`）在未启动服务时必然 `unreachable`，这是**预期结果**。")
    add("- 走代理时脚本自动把 `127.0.0.1 / localhost` 加入 `no_proxy`，避免本地服务被代理劫持造成假阴性。")
    add("- 若 `--proxy` 指向的端口未监听，该轮所有条目都会是 `ConnectionRefusedError` → `unreachable`；")
    add("  这不代表任何地址有问题，报告会把该轮标记为「链路整体不可用」。")
    add("- 报告不记录任何 api_key；代理 URL 中的凭据已脱敏为 `***@`。")
    add("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _parse_ids(raw: str, all_ids: list) -> list:
    """``--only a,b,c`` → 校验后的 id 列表（保持注册表顺序）。"""
    wanted = [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]
    unknown = [pid for pid in wanted if pid not in all_ids]
    if unknown:
        print(
            f"[verify] 警告：忽略未知 id {unknown}；可用 id 见 --list",
            file=sys.stderr,
        )
    picked = [pid for pid in all_ids if pid in wanted]
    if not picked:
        print("[verify] 错误：--only 未匹配到任何已知 id", file=sys.stderr)
        return []
    return picked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ComfyUI NL2Prompt 供应商地址探活（契约 §14.1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--only", default="", help="只探这些 id，逗号分隔，如 deepseek,openai")
    parser.add_argument(
        "--proxy",
        default="",
        help="代理地址（如 http://127.0.0.1:7890）；给出时在直连轮之外**追加**一轮代理轮",
    )
    parser.add_argument(
        "--rounds",
        default="auto",
        help="auto（默认：有 --proxy 则 direct,proxy；否则只跑 direct）| direct | proxy | 逗号分隔",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="单请求超时秒数，默认 8")
    parser.add_argument(
        "--out",
        default=str(_REPO_ROOT / "docs" / "providers-verify.md"),
        help="Markdown 报告输出路径",
    )
    parser.add_argument("--no-report", action="store_true", help="只打印 JSON，不写 Markdown")
    parser.add_argument(
        "--from-json",
        default="",
        help="不联网，直接用之前保存的 JSON 重新渲染报告（调试/补报告用）",
    )
    parser.add_argument(
        "--apply-verified",
        action="store_true",
        help=(
            "把实测结论回填到 plugin/providers.py 的 verified 字段"
            "（只回填拿到明确 HTTP 响应的条目；unreachable / base_url 空 一律不回填）"
        ),
    )
    parser.add_argument("--list", action="store_true", help="只列出注册表 id 后退出")
    parser.add_argument("--quiet", action="store_true", help="不打印进度到 stderr")
    args = parser.parse_args(argv)

    all_ids = list(providers.PROVIDERS.keys())

    if args.list:
        for pid in providers.provider_options():
            meta = providers.PROVIDERS.get(pid) or {}
            print(f"{pid}\t{meta.get('label', '')}\t{meta.get('base_url', '')}")
        return 0

    ids = all_ids
    if args.only:
        ids = _parse_ids(args.only, all_ids)
        if not ids:
            return 2

    # ---- 离线重渲染：不联网，直接用旧 JSON 出报告 ----
    if args.from_json:
        try:
            payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] 读取 {args.from_json} 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if "verdict" not in payload:
            _rounds = payload.get("rounds") or []
            payload["verdict"] = _rounds[-1].get("results", []) if _rounds else []
        payload.setdefault("applied_verified", [])
        payload.setdefault("merge_basis", "各轮合并")
        if not args.no_report:
            out_path = Path(args.out)
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(render_markdown(payload), encoding="utf-8")
                payload["report_path"] = str(out_path)
                print(f"[verify] 报告已重新写入 {out_path}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                payload["report_path"] = ""
                payload["report_error"] = f"{type(exc).__name__}: {exc}"
                print(f"[verify] 写报告失败：{payload['report_error']}", file=sys.stderr)
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # 轮次解析
    raw_rounds = str(args.rounds or "auto").strip().lower()
    if raw_rounds == "auto":
        plan = ["direct"] + (["proxy"] if args.proxy else [])
    else:
        plan = [part.strip() for part in raw_rounds.split(",") if part.strip()]
    plan = [name for name in plan if name in ("direct", "proxy")]
    if not plan:
        plan = ["direct"]
    if "proxy" in plan and not args.proxy:
        print("[verify] 错误：--rounds 含 proxy 但未提供 --proxy", file=sys.stderr)
        return 2
    # 去重保序
    seen = set()
    plan = [name for name in plan if not (name in seen or seen.add(name))]

    if not args.quiet:
        print(
            f"[verify] 共 {len(ids)} 家 / {len(plan)} 轮：{', '.join(plan)}",
            file=sys.stderr,
        )

    rounds = []
    label = {"direct": "direct（直连）", "proxy": f"proxy（{_sanitize_url(args.proxy)}）"}
    for name in plan:
        proxy = args.proxy if name == "proxy" else ""
        if not args.quiet:
            print(f"[verify] 开始第 {len(rounds) + 1} 轮：{label.get(name, name)}", file=sys.stderr)
        round_data = run_round(
            label.get(name, name), proxy, ids, float(args.timeout or 8.0)
        )
        rounds.append(round_data)
        if not args.quiet:
            s = round_data["summary"]
            print(
                "[verify]   完成：verified=True {vt} / unreachable {un} / 404 {p4} / "
                "no_base_url {nb} / 其它 {ot}".format(
                    vt=s["verified_true"],
                    un=s["unreachable"],
                    p4=s["http_404_path"],
                    nb=s["no_base_url"],
                    ot=s["other_http"],
                ),
                file=sys.stderr,
            )

    merged = merge_rounds(rounds)

    payload = {
        "generated_at": _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "probe_target": "{base_url}/models",
        "timeout_s": float(args.timeout or 8.0),
        "count": len(ids),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "ids": ids,
        "rounds": rounds,
        "verdict": merged,
        "merge_basis": "各轮取信息量最大的探测（拿到 HTTP 响应优先于 unreachable / base_url 空）",
    }

    # 回填基于**合并结论**：多轮里取到过 HTTP 响应的那次才算数
    if args.apply_verified and merged:
        payload["applied_verified"] = apply_verified(merged)
    else:
        payload["applied_verified"] = []

    # 先写报告再打 JSON：写盘失败也要能让调用方拿到 console 结果
    report_error = ""
    if not args.no_report:
        try:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_markdown(payload), encoding="utf-8")
            if not args.quiet:
                print(f"[verify] 报告已写入 {out_path}", file=sys.stderr)
            payload["report_path"] = str(out_path)
        except Exception as exc:  # noqa: BLE001
            report_error = f"{type(exc).__name__}: {exc}"
            payload["report_path"] = ""
            payload["report_error"] = report_error
            print(f"[verify] 写报告失败：{report_error}", file=sys.stderr)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台兜底
    except Exception:  # noqa: BLE001
        pass

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not report_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
