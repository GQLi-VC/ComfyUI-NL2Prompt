"""UI 探针 v4：验证「提示词替换」行的正向/负向下拉可点击并生效。

原缺陷：targetSelect 监听 pointerdown 且调用 blockPointerEvent()（内部 preventDefault），
导致 <select> 的原生下拉无法展开 → 用户点了没反应。
本探针直接检测 pointerdown 的 defaultPrevented，并验证切换后能写回 holder。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

SCRIPT = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
    if (!n) return {node: false};
    const sw = n.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!gear) return {node: true, gear: false};
    gear.click();
    await sleep(600);
    let ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return {node: true, gear: true, overlay: false};

    const add = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').includes('添加替换规则'));
    if (add) { add.click(); await sleep(500); }
    ov = document.querySelector('.anima-nl-overlay');
    const sel = ov ? ov.querySelector('select') : null;
    if (!sel) return {node: true, gear: true, overlay: true, select: false, add_found: !!add};

    // 1) 根因检测：pointerdown 是否被 preventDefault（修复后必须为 false）
    const ev = new PointerEvent('pointerdown', {bubbles: true, cancelable: true});
    sel.dispatchEvent(ev);
    const prevented = ev.defaultPrevented;

    const cs = getComputedStyle(sel);
    const rect = sel.getBoundingClientRect();
    const out = {
        node: true, gear: true, overlay: true, select: true,
        pointerdown_prevented: prevented,
        pointer_events: cs.pointerEvents,
        display: cs.display,
        visibility: cs.visibility,
        disabled: !!sel.disabled,
        rect: {w: Math.round(rect.width), h: Math.round(rect.height)},
        options: [...sel.options].map(o => o.value),
        value_before: sel.value
    };

    // 2) 切换到 negative 并触发 change
    sel.value = 'negative';
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    await sleep(300);
    out.value_after_change = sel.value;

    // 3) 保存 → 读 holder 里持久化的 target
    const saveBtn = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '保存');
    if (saveBtn) { saveBtn.click(); await sleep(700); }
    let holder = null;
    try { holder = JSON.parse(n.widgets.find(w => w.name === '_ui_json').value); } catch (e) {}
    out.overlay_after_save = !!document.querySelector('.anima-nl-overlay');
    out.saved_text_map = holder && holder.text_map ? holder.text_map : null;
    out.saved_target = holder && holder.text_map && holder.text_map[0] ? holder.text_map[0].target : null;

    // 4) 再打开一次，确认下拉回显为 negative（往返一致）
    gear.click();
    await sleep(600);
    const ov2 = document.querySelector('.anima-nl-overlay');
    const sel2 = ov2 ? ov2.querySelector('select') : null;
    out.reopen_select_value = sel2 ? sel2.value : null;
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await sleep(300);
    return out;
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9336)
    ap.add_argument("--shot", default="")
    args = ap.parse_args()
    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe4_profile")
    report = {}
    proc = None
    ws = None
    try:
        proc, base = start_chrome(args.port, profile)
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 1200, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")
        cdp.eval(
            "(() => { const n = LiteGraph.createNode('NL2PromptComposer'); n.pos=[80,80]; app.graph.add(n);"
            " app.graph.setDirtyCanvas(true,true); return n.id; })()"
        )
        time.sleep(1.5)
        report["select_test"] = cdp.eval(SCRIPT, await_promise=True)
        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(args.shot, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot"] = args.shot
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
        if proc and proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
