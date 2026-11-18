"""检查 5 个 LLM 节点（含旧类名）是否都装出了 UI 与「设置」按钮。

用法：python -X utf8 tools/check_llm_ui.py [--shot 目录]
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NODE_TYPES = [
    "NL2PromptComposer",       # v3 主节点
    "AnimaNLComposer",         # v2 旧主节点（旧工作流）
    "NL2PromptLoadLLM",        # v3 合并版
    "AnimaLoadLLM",            # v2 旧类名（旧工作流）
    "NL2PromptLLMGGUF",        # v3 拆分
    "NL2PromptLLMSafetensors",
    "NL2PromptLLMCloud",
    "NL2PromptLoRAStack",      # 无自定义 UI，预期无设置按钮
]

SCRIPT = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const res = {};
    for (const t of %s) {
        let n = null;
        try { n = LiteGraph.createNode(t); } catch (e) { res[t] = { create_error: String(e) }; continue; }
        if (!n) { res[t] = { create_error: "createNode returned null" }; continue; }
        n.pos = [80, 80];
        app.graph.add(n);
        await sleep(900);
        const names = n.widgets.map(w => w.name);
        const settingsW = n.widgets.find(w => w.name === "anima_llm_settings" || w.name === "anima_nl_settings");
        const btn = settingsW && settingsW.element ? settingsW.element.querySelector("button") : null;
        let opened = false;
        let tabs = [];
        let buttons = [];
        if (btn) {
            btn.click();
            await sleep(600);
            const ov = document.querySelector(".anima-nl-overlay");
            opened = !!ov;
            if (ov) {
                // Tab 标题（设置窗口顶部那排按钮），用于核对 v3.2 的 Tab 结构
                tabs = [...ov.querySelectorAll(".anima-nl-tab")].map(b => (b.textContent || "").trim());
                buttons = [...ov.querySelectorAll("button")].map(b => (b.textContent || "").trim()).filter(Boolean);
            }
        }
        const ov2 = document.querySelector(".anima-nl-overlay");
        if (ov2) ov2.remove();
        res[t] = {
            widget_count: n.widgets.length,
            has_settings_widget: !!settingsW,
            settings_button: btn ? (btn.textContent || "").trim() : null,
            window_opened: opened,
            tabs: tabs,
            buttons: buttons,
            widgets: names,
        };
        try { app.graph.remove(n); } catch (e) {}
        await sleep(250);
    }
    return res;
})()
""" % json.dumps(NODE_TYPES)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8199")
    parser.add_argument("--port", type=int, default=9351)
    parser.add_argument("--shot", default="")
    args = parser.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_check_llm_ui")
    report = {}
    proc = ws = None
    try:
        proc, base = start_chrome(args.port, profile)
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            print(json.dumps({"error": "app not ready"}, ensure_ascii=True))
            return 2
        masked = "document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1"
        cdp.eval(masked)

        report["nodes"] = cdp.eval(SCRIPT, await_promise=True)
        time.sleep(0.5)

        print("=== 摘要 ===")
        for name, info in (report.get("nodes") or {}).items():
            print(
                "%-30s widgets=%-2s settings=%-5s btn=%-10r opened=%-5s tabs=%s"
                % (
                    name,
                    info.get("widget_count"),
                    info.get("has_settings_widget"),
                    info.get("settings_button"),
                    info.get("window_opened"),
                    " / ".join(info.get("tabs") or []) or "-",
                )
            )
        print()

        if args.shot:
            os.makedirs(os.path.join(ROOT, args.shot), exist_ok=True)
            data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            path = os.path.join(ROOT, args.shot, "llm_nodes.png")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(data["data"]))
            report["shot"] = args.shot

        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"fatal": "%s: %s" % (type(exc).__name__, exc)}, ensure_ascii=False))
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
