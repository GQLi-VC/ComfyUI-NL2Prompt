"""启动一个**可见** Chrome 窗口并让 ComfyUI 显示修复后的真实效果。

与 ui_probe*.py 的区别：不做 headless，浏览器窗口会留在桌面上给用户看；
同时截一张图存到 log/ 便于在回复里展示。

用法：python -X utf8 tools/show_demo.py [--shot log/demo.png] [--close]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_probe import CDP, WS, page_target, wait_js  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

DEMO = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const n = LiteGraph.createNode('NL2PromptComposer');
    n.pos = [140, 120];
    app.graph.add(n);
    await sleep(900);

    n.widgets.find(w => w.name === 'natural_language').value = '银发少女站在雨后的霓虹街道上';

    const holder = n.widgets.find(w => w.name === '_ui_json');
    const st = {
        schema: 1,
        character_rows: [
            {id: 'r1', name: '遐蝶', trigger: 'castorice, purple hair', outfit_hint: '假日皮肤'},
            {id: 'r2', name: '', trigger: '', outfit_hint: ''}
        ],
        style_rows: [{id: 's1', name: '@nixeu', trigger: '@nixeu, soft light'}],
        text_map: [{id: 't1', find: '霓虹街道', prompt: 'neon-lit street, rain', target: 'negative'}],
        system_prompt: '',
        negative_base: 'lowres, bad anatomy, text, watermark',
        sampling: {top_k: 40, top_p: 0.9, seed: -1},
        ui: {collapsed_main: false}
    };
    holder.value = JSON.stringify(st);
    if (holder.callback) { try { holder.callback(holder.value); } catch (e) {} }
    await sleep(900);

    app.graph.setDirtyCanvas(true, true);
    try { app.canvas.centerOnNode ? app.canvas.centerOnNode(n) : app.canvas.center(); } catch (e) {}
    try { app.canvas.setZoom(1.0); } catch (e) {}
    await sleep(700);

    return {node_id: n.id, char_rows: 2};
})()
"""

OPEN_WINDOW = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
    if (!n) return {node: false};
    const sw = n.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!gear) return {node: true, gear: false};
    gear.click();
    await sleep(800);
    const ov = document.querySelector('.anima-nl-overlay');
    const sel = ov ? ov.querySelector('select') : null;
    if (sel) {
        // 真实交互路径：像用户那样把下拉切到「负向 negative」
        sel.value = 'negative';
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        await sleep(300);
    }
    return {overlay: !!ov, select_value: sel ? sel.value : null};
})()
"""


def launch_visible(port: int, profile: str):
    args = [
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--mute-audio",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--window-size=1700,1150",
        "--window-position=40,20",
        "about:blank",
    ]
    return subprocess.Popen([CHROME] + args)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9337)
    ap.add_argument("--shot", default="")
    ap.add_argument("--shot2", default="")
    args = ap.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_show_demo_profile")
    proc = launch_visible(args.port, profile)
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(160):
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as r:
                json.load(r)
            break
        except Exception:
            time.sleep(0.25)
    else:
        print(json.dumps({"fatal": "devtools endpoint not up"}, ensure_ascii=True))
        return 1

    report = {}
    ws = None
    try:
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1700, "height": 1150, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")
        report["demo"] = cdp.eval(DEMO, await_promise=True)
        time.sleep(1.0)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png"})
            with open(os.path.join(root, args.shot), "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot"] = args.shot
        report["window"] = cdp.eval(OPEN_WINDOW, await_promise=True)
        time.sleep(1.0)
        if args.shot2:
            shot = cdp.call("Page.captureScreenshot", {"format": "png"})
            with open(os.path.join(root, args.shot2), "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot2"] = args.shot2
        report["browser_left_open"] = True
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, ensure_ascii=True))
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        # 故意不杀浏览器：用户要看到窗口


if __name__ == "__main__":
    sys.exit(main())
