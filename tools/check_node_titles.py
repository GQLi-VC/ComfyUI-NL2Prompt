"""检查左侧节点栏（节点库）的显示名是否跟随语言切换（zh / en 各截一张）。

用法：
    python -X utf8 tools/check_node_titles.py
    python -X utf8 tools/check_node_titles.py --out log/v3_ui

流程：后端语言设 zh → reload → 打开节点库、搜 "NL2Prompt"、读条目 + 截图
      → 走真实 UI（通用 Tab）切 en → 再读条目 + 截图 → 复位成初始 ui_locale
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "log", "v3_ui")
OUT_DIR = DEFAULT_OUT

OPEN_LIBRARY_AND_SEARCH = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    const tab = document.querySelector('[data-testid="node-library-tab-button"]');
    const alreadyOpen = !![...document.querySelectorAll('input')].find(i => /搜索|search/i.test(i.placeholder || ''));
    if (!alreadyOpen && tab) { tab.click(); await sleep(1200); }
    else if (alreadyOpen) { await sleep(300); }
    else return { ok: false, reason: 'no node-library tab button' };
    const input = [...document.querySelectorAll('input')].find(i => /搜索|search/i.test(i.placeholder || ''));
    if (!input) return { ok: false, reason: 'no search input' };
    input.focus();
    input.value = %s;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(1400);
    const texts = [];
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
        const txt = (n.textContent || '').trim();
        if (!txt || txt.length > 60) continue;
        if (!/提示词|LLM|Natural|Load LLM|LoRA|Stack|Prompt/i.test(txt)) continue;
        if (seen.has(txt)) continue;
        seen.add(txt);
        texts.push(txt);
    }
    const reg = {};
    for (const t of Object.keys(LiteGraph.registered_node_types)) {
        if (!/NL2Prompt|Anima/.test(t)) continue;
        reg[t] = LiteGraph.registered_node_types[t].title;
    }
    const store = (() => {
        try {
            const pinia = app.extensionManager._p;
            const defs = pinia._s.get('nodeDef').nodeDefsByName;
            const out = {};
            for (const t of Object.keys(defs)) if (/NL2Prompt|Anima/.test(t)) out[t] = defs[t].display_name;
            return out;
        } catch (e) { return { error: String(e) }; }
    })();
    return { ok: true, query: %s, libraryTexts: texts, registryTitles: reg, storeDisplayNames: store, htmlLang: document.documentElement.lang };
})()
"""

SWITCH_LOCALE_UI = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(250);
    const c = app.graph._nodes.find(n => /Composer/.test(n.type));
    if (!c) { try { const n = LiteGraph.createNode('NL2PromptComposer'); app.graph.add(n); } catch (e) {} }
    const node = app.graph._nodes.find(n => /Composer/.test(n.type));
    if (!node) return { ok: false, reason: 'no composer node' };
    const sw = node.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!gear) return { ok: false, reason: 'no settings button' };
    gear.click();
    await sleep(900);
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const tabs = [...ov.querySelectorAll('.anima-nl-tab')];
    if (tabs[0]) tabs[0].click();
    await sleep(500);
    const sel = [...ov.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === 'auto'));
    if (!sel) return { ok: false, reason: 'no locale select' };
    sel.value = %s;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(1700);
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(300);
    return { ok: true, htmlLang: document.documentElement.lang };
})()
"""


def shot(cdp, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(data["data"]))
    return path


def set_backend_locale(cdp, mode):
    js = """
    (async () => {
        const g = await (await fetch('/nl2prompt/settings_ui')).json();
        const r = await fetch('/nl2prompt/settings_ui', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ui_locale: %s, default_positive_base: g.default_positive_base || '' }),
        });
        return { ok: r.ok, body: await r.json().catch(() => null) };
    })()
    """ % json.dumps(mode)
    return cdp.eval(js, await_promise=True)


def main(argv=None) -> int:
    global OUT_DIR
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8199
    chrome_port = 9385
    while argv:
        flag = argv.pop(0)
        if flag == "--out" and argv:
            OUT_DIR = os.path.abspath(argv.pop(0))
        elif flag == "--port" and argv:
            port = int(argv.pop(0))
        elif flag == "--chrome-port" and argv:
            chrome_port = int(argv.pop(0))
    os.makedirs(OUT_DIR, exist_ok=True)

    report = {"shots": [], "out_dir": OUT_DIR}
    proc = ws = None
    try:
        proc, base = start_chrome(chrome_port, os.path.join(os.environ.get("TEMP", "."), "check_node_titles"))
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1680, "height": 1050, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d" % port})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 150, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")

        original = cdp.eval("(async () => (await (await fetch('/nl2prompt/settings_ui')).json()).ui_locale)()",
                            await_promise=True)
        report["original_ui_locale"] = original

        # ---------- zh
        report["set_zh"] = set_backend_locale(cdp, "zh")
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d" % port})
        if not wait_js(cdp, "!!(window.app && window.app.graph)", True, 150, "app"):
            report["error"] = "app not ready (zh)"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        time.sleep(0.8)
        report["zh"] = cdp.eval(OPEN_LIBRARY_AND_SEARCH % (json.dumps("NL2Prompt"), json.dumps("NL2Prompt")),
                                await_promise=True)
        time.sleep(0.4)
        report["shots"].append(shot(cdp, "node_panel_zh.png"))

        # ---------- 走真实 UI 切 en
        report["switch_en"] = cdp.eval(SWITCH_LOCALE_UI % json.dumps("en"), await_promise=True)
        time.sleep(0.5)
        report["en"] = cdp.eval(OPEN_LIBRARY_AND_SEARCH % (json.dumps("NL2Prompt"), json.dumps("NL2Prompt")),
                                await_promise=True)
        time.sleep(0.4)
        report["shots"].append(shot(cdp, "node_panel_en.png"))

        # ---------- 复位
        report["reset"] = set_backend_locale(cdp, original if isinstance(original, str) else "auto")
        report["verdict"] = {
            "zh_titles": (report.get("zh") or {}).get("registryTitles"),
            "en_titles": (report.get("en") or {}).get("registryTitles"),
            "zh_store": (report.get("zh") or {}).get("storeDisplayNames"),
            "en_store": (report.get("en") or {}).get("storeDisplayNames"),
            "en_library_texts": (report.get("en") or {}).get("libraryTexts"),
            "zh_library_texts": (report.get("zh") or {}).get("libraryTexts"),
        }
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        report["fatal"] = "%s: %s" % (type(exc).__name__, exc)
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if proc and proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
