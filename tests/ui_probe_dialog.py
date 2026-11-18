# -*- coding: utf-8 -*-
"""C4 前端探针：验证 `onExecuted` 落 holder 后 `dialog` 上限 20（丢弃最旧）。

复用 `tools/ui_probe.py` 的 CDP 基础设施（**只读引用，不修改**）。
产物：`tests/_ui_probe_dialog.json`，供 `tests/test_v2.py` 读取断言。

用法：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\ui_probe_dialog.py
    ... --url http://127.0.0.1:8199 --port 9341
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
TOOLS = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

SCRIPT = r"""
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const out = {};
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);

    const c = LiteGraph.createNode('NL2PromptComposer');
    c.pos = [120, 120];
    app.graph.add(c);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1600);

    out.node_created = !!c;
    out.has_on_executed = typeof c.onExecuted === 'function';
    const holderWidget = c.widgets.find(w => w.name === '_ui_json');
    out.has_holder_widget = !!holderWidget;

    // 读取初始 dialog 长度（默认应为 0）
    let initial = null;
    try { initial = JSON.parse(holderWidget.value); } catch (e) { out.initial_parse_err = String(e); }
    out.initial_dialog_len = initial && Array.isArray(initial.dialog) ? initial.dialog.length : -1;

    // 连续 25 次 onExecuted（模拟 25 轮执行回填）
    out.call_errors = [];
    for (let i = 0; i < 25; i++) {
        try {
            c.onExecuted({anima_nl: [{
                seed: 1000 + i,
                used_llm: false,
                fallback: "",
                dialog_entry: {
                    role: "user",
                    text: "t" + i,
                    positive: "p" + i,
                    negative: "n" + i,
                    ts: i,
                },
            }]});
        } catch (e) {
            out.call_errors.push(String(e));
        }
    }
    await sleep(400);

    const w = c.widgets.find(x => x.name === '_ui_json');
    out.holder_len = w ? (w.value || '').length : 0;
    let parsed = null;
    try { parsed = JSON.parse(w.value); } catch (e) { out.parse_err = String(e); }
    out.holder_parsed = !!parsed;
    if (parsed && Array.isArray(parsed.dialog)) {
        out.dialog_len = parsed.dialog.length;
        out.first_text = parsed.dialog.length ? parsed.dialog[0].text : null;
        out.last_text = parsed.dialog.length ? parsed.dialog[parsed.dialog.length - 1].text : null;
        out.texts = parsed.dialog.map(d => d.text);
        out.last_role = parsed.dialog.length ? parsed.dialog[parsed.dialog.length - 1].role : null;
    } else {
        out.dialog_len = -1;
    }

    // 顺带确认种子小字被回填（最后一轮的 seed）
    const seedW = c.widgets.find(x => x.name === 'anima_nl_seed');
    out.seed_text = seedW && seedW.element ? (seedW.element.innerText || '').trim() : null;
    return out;
})()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8199")
    parser.add_argument("--port", type=int, default=9341)
    parser.add_argument("--out", default=str(HERE / "_ui_probe_dialog.json"))
    parser.add_argument("--shot", default="")
    args = parser.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe_dialog_profile")
    report = {"url": args.url, "ts": time.time()}
    proc = None
    ws = None
    try:
        proc, base = start_chrome(args.port, profile)
        target = page_target(base)
        ws = WS(target["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1700, "height": 1200, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")
        report["ui"] = cdp.eval(SCRIPT, await_promise=True)
        time.sleep(0.5)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:  # noqa: BLE001
            pass
        if proc and proc.poll() is None:
            proc.kill()
        try:
            Path(args.out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"报告已写入: {args.out}")
        except OSError as exc:
            print(f"报告写入失败: {exc}")


if __name__ == "__main__":
    sys.exit(main())
