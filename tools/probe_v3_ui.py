"""v3 前端探针：逐 Tab 截图 + 云端模型拉取实测（可复现脚本）。

用法：
    python -X utf8 tools/probe_v3_ui.py                 # 默认写到 log/v3_ui/
    python -X utf8 tools/probe_v3_ui.py --out D:/tmp/x   # 自定义输出目录
    python -X utf8 tools/probe_v3_ui.py --port 8199 --chrome-port 9341

前置：测试环境已启动（默认 http://127.0.0.1:8199）且插件已加载。
产出：<项目>/log/v3_ui/v3_*.png（10 张）+ 一行 JSON 报告（stdout，ensure_ascii）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

DEFAULT_OUT_DIR = os.path.join(ROOT, "log", "v3_ui")
OUT_DIR = DEFAULT_OUT_DIR

CREATE_NODES = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const c = LiteGraph.createNode('NL2PromptComposer'); c.pos = [60, 60]; app.graph.add(c);
    const l = LiteGraph.createNode('NL2PromptLoadLLM'); l.pos = [820, 60]; app.graph.add(l);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1500);
    return { composer: !!c, llm: !!l, widgets: c.widgets.map(w => w.name) };
})()
"""

OPEN_COMPOSER = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const c = app.graph._nodes.find(n => n.type === 'NL2PromptComposer');
    const sw = c && c.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) gear.click();
    await sleep(1100);
    const ov = document.querySelector('.anima-nl-overlay');
    return { opened: !!ov, tabLabels: ov ? [...ov.querySelectorAll('.anima-nl-tab')].map(b => (b.textContent || '').trim()) : [] };
})()
"""

CLICK_TAB = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const b = [...ov.querySelectorAll('.anima-nl-tab')].find(x => (x.textContent || '').trim() === %s);
    if (!b) return { ok: false, reason: 'tab not found' };
    b.click();
    await sleep(800);
    const panel = ov.querySelector('.anima-nl-tabpanel.is-active');
    const tabs = [...ov.querySelectorAll('.anima-nl-tab')];
    return {
        ok: true,
        index: tabs.indexOf(b),
        textareas: panel ? panel.querySelectorAll('textarea').length : -1,
        inputs: panel ? panel.querySelectorAll('input').length : -1,
        selects: panel ? panel.querySelectorAll('select').length : -1,
        buttons: panel ? [...panel.querySelectorAll('button')].map(x => (x.textContent || '').trim()).slice(0, 8) : [],
        textLen: panel ? (panel.textContent || '').trim().length : 0,
        status: (ov.querySelector('.anima-nl-status') || {}).textContent || '',
    };
})()
"""

OPEN_LLM_CLOUD_TAB = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    (document.querySelector('.anima-nl-overlay') || {remove(){}}).remove();
    await sleep(400);
    const l = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
    const mt = l.widgets.find(w => w.name === 'model_type');
    if (mt) { mt.value = '云端模型'; if (mt.callback) { try { mt.callback(mt.value); } catch (e) {} } }
    const sw = l.widgets.find(w => w.name === 'anima_llm_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) gear.click();
    await sleep(1100);
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { opened: false };
    const tab = [...ov.querySelectorAll('.anima-nl-tab')].find(b => (b.textContent || '').trim() === '云端模型');
    if (tab) tab.click();
    await sleep(900);
    const province = [...ov.querySelectorAll('select')].find(s => (s.getAttribute('title') || '').length === 0);
    return {
        opened: true,
        tabLabels: [...ov.querySelectorAll('.anima-nl-tab')].map(b => (b.textContent || '').trim()),
        hasBaseInput: !![...ov.querySelectorAll('input')].find(i => (i.placeholder || '').includes('留空 = 按供应商默认')),
        hasFetchButton: !![...ov.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '拉取模型'),
        providerOptions: [...ov.querySelectorAll('select')].map(s => [...s.options].map(o => o.value)),
    };
})()
"""

FETCH_WITH_EMPTY_BASE = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const base = [...ov.querySelectorAll('input')].find(i => (i.placeholder || '').includes('留空 = 按供应商默认'));
    if (!base) return { ok: false, reason: 'no base input' };
    base.value = '';
    base.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(200);
    const sel = [...ov.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === 'deepseek'));
    if (sel) { sel.value = 'deepseek'; sel.dispatchEvent(new Event('change', { bubbles: true })); }
    await sleep(200);
    const base2 = [...ov.querySelectorAll('input')].find(i => (i.placeholder || '').includes('留空 = 按供应商默认'));
    base2.value = '';
    base2.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(200);
    const btn = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '拉取模型');
    if (!btn) return { ok: false, reason: 'no fetch button' };
    btn.click();
    await sleep(4000);
    const status = (ov.querySelector('.anima-nl-status') || {}).textContent || '';
    const modelItems = [...ov.querySelectorAll('.anima-nl-model-item')].map(x => (x.textContent || '').trim());
    const node = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
    return {
        ok: true,
        status,
        modelItems,
        nodeBaseUrl: node.widgets.find(w => w.name === 'base_url').value,
        nodeProvider: node.widgets.find(w => w.name === 'provider').value,
        hasKeyError: /no_key|未配置|api_key|401|403|拉取失败/.test(status),
        // 回归点：绝不能出现「请先填写 base_url」这类前端拦截
        wrongGuard: status.includes('请先选择供应商或填写 base_url'),
    };
})()
"""

CLOSE_OVERLAY = "(document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove()), 1)"


def shot(cdp, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(data["data"]))
    return path


def main(argv=None) -> int:
    global OUT_DIR
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8199
    chrome_port = 9341
    while argv:
        flag = argv.pop(0)
        if flag == "--out" and argv:
            OUT_DIR = os.path.abspath(argv.pop(0))
        elif flag == "--port" and argv:
            port = int(argv.pop(0))
        elif flag == "--chrome-port" and argv:
            chrome_port = int(argv.pop(0))
    os.makedirs(OUT_DIR, exist_ok=True)

    report = {"shots": [], "out_dir": OUT_DIR, "url": "http://127.0.0.1:%d" % port}
    proc = ws = None
    try:
        proc, base = start_chrome(chrome_port, os.path.join(os.environ.get("TEMP", "."), "v3_probe_profile"))
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1680, "height": 1050, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d" % port})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")

        report["nodes"] = cdp.eval(CREATE_NODES, await_promise=True)
        report["open"] = cdp.eval(OPEN_COMPOSER, await_promise=True)
        time.sleep(0.6)
        report["shots"].append(shot(cdp, "v3_00_composer_window.png"))

        tabs = report["open"].get("tabLabels") or []
        report["tab_results"] = {}
        for idx, name in enumerate(tabs):
            res = cdp.eval(CLICK_TAB % json.dumps(name), await_promise=True)
            time.sleep(0.5)
            report["shots"].append(shot(cdp, "v3_%02d_%s.png" % (idx + 1, name)))
            report["tab_results"][name] = res

        report["llm"] = cdp.eval(OPEN_LLM_CLOUD_TAB, await_promise=True)
        time.sleep(0.6)
        report["shots"].append(shot(cdp, "v3_90_llm_cloud_tab.png"))

        report["fetch"] = cdp.eval(FETCH_WITH_EMPTY_BASE, await_promise=True)
        time.sleep(0.4)
        report["shots"].append(shot(cdp, "v3_91_llm_fetch_result.png"))

        cdp.eval(CLOSE_OVERLAY)
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as exc:  # noqa: BLE001
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
