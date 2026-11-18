"""逐个 Tab 截图，用于给用户展示 v2 的设置窗口与 LLM 云端面板。

用法：python -X utf8 tools/shot_settings.py
产出：log/v2_tab_*.png（5 张）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OPEN = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const c = LiteGraph.createNode('NL2PromptComposer'); c.pos = [40, 40]; app.graph.add(c);
    const l = LiteGraph.createNode('NL2PromptLoadLLM'); l.pos = [900, 40]; app.graph.add(l);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1400);

    const sw = c.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) { gear.click(); }
    await sleep(900);

    const mt = l.widgets.find(w => w.name === 'model_type');
    if (mt) { mt.value = '云端模型'; if (mt.callback) { try { mt.callback(mt.value); } catch (e) {} } }
    await sleep(700);

    return { opened: !!document.querySelector('.anima-nl-overlay'),
             tabs: document.querySelector('.anima-nl-overlay')
                   ? [...document.querySelector('.anima-nl-overlay').querySelectorAll('button')]
                       .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 8) : [] };
})()
"""

CLICK = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const want = %s;
    const b = [...ov.querySelectorAll('button')].find(x => (x.textContent || '').trim() === want);
    if (!b) return { ok: false, reason: 'tab not found: ' + want };
    b.click();
    await sleep(700);
    return { ok: true };
})()
"""

TABS = ["提示词替换", "提示词技能", "对话历史", "负面基础词"]


def shot(cdp, name):
    data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    path = os.path.join(ROOT, "log", name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(data["data"]))
    return name


def main() -> int:
    report = {}
    proc = ws = None
    try:
        proc, base = start_chrome(9339, os.path.join(os.environ.get("TEMP", "."), "anima_shot_profile"))
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:8199"})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")

        report["open"] = cdp.eval(OPEN, await_promise=True)
        time.sleep(0.6)
        report["shots"] = [shot(cdp, "v2_tab_0_settings.png")]

        for idx, tab in enumerate(TABS, start=1):
            res = cdp.eval(CLICK % json.dumps(tab), await_promise=True)
            time.sleep(0.45)
            name = "v2_tab_%d_%s.png" % (idx, tab)
            report["shots"].append(shot(cdp, name))
            report.setdefault("tab_results", {})[tab] = res

        # LLM 节点云端面板（关掉设置窗口单独截）
        cdp.eval("(document.querySelector('.anima-nl-overlay') || {remove(){}}).remove(), 1")
        time.sleep(0.5)
        cdp.eval("""
        (() => {
            const l = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
            if (l) { app.canvas.selectNode(l); app.canvas.centerOnNode(l); }
            app.graph.setDirtyCanvas(true, true);
            return 1;
        })()
        """)
        time.sleep(1.0)
        report["shots"].append(shot(cdp, "v2_tab_5_llm_cloud.png"))
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as exc:
        report["fatal"] = "%s: %s" % (type(exc).__name__, exc)
        print(json.dumps(report, ensure_ascii=True))
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
