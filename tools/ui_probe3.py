"""UI 探针 v3：A5（onExecuted 种子渲染）与 workflow 文件可加载性。

验证两件 v1/v2 未覆盖的事：
  1. 主节点 onExecuted 收到 ui.anima_nl[0].seed 后，节点正面是否真的渲染出「上轮种子: N」
  2. workflows/*.workflow.json 能否被 ComfyUI 前端真正加载（节点数 / 连线数）

用法：python -X utf8 tools/ui_probe3.py --wf workflows/NL2Prompt_MVP.workflow.json [--shot ...]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9335)
    ap.add_argument("--wf", default="workflows/NL2Prompt_MVP.workflow.json")
    ap.add_argument("--wf2", default="workflows/NL2Prompt_WithLoRA.workflow.json")
    ap.add_argument("--shot", default="")
    ap.add_argument("--shot2", default="")
    args = ap.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wf_path = os.path.join(project_root, args.wf)
    with open(wf_path, encoding="utf-8") as fh:
        wf = json.load(fh)

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe3_profile")
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
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1600, "height": 1200, "deviceScaleFactor": 1, "mobile": False},
        )
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")

        # --- 1) 加载 UI 格式 workflow
        payload = json.dumps(wf, ensure_ascii=True)
        report["wf_file"] = args.wf
        report["wf_load"] = cdp.eval(
            "(async () => { window.__wf = " + payload + ";"
            " try { await app.loadGraphData(window.__wf); }"
            " catch (e) { return {ok: false, err: String(e).slice(0, 300)}; }"
            " await new Promise(r => setTimeout(r, 1500));"
            " const nodes = app.graph._nodes || [];"
            " return {ok: true, node_count: nodes.length,"
            "   types: nodes.map(n => n.type),"
            "   link_count: Object.keys(app.graph.links || {}).length,"
            "   has_composer: nodes.some(n => n.type === 'NL2PromptComposer'),"
            "   has_llm: nodes.some(n => n.type === 'NL2PromptLoadLLM'),"
            "   has_lora_loader: nodes.some(n => n.type === 'NL2PromptLoRAStack')};"
            " })()",
            await_promise=True,
        )

        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(os.path.join(project_root, args.shot), "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot"] = args.shot

        # --- 2) A5：onExecuted 种子渲染
        report["a5_render"] = cdp.eval(
            """(async () => {
                const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
                if (!n) return {node: false};
                const seedW = n.widgets.find(w => w.name === 'anima_nl_seed');
                const read = () => seedW && seedW.element ? (seedW.element.innerText || seedW.element.textContent || '').trim() : null;
                const before = read();
                if (typeof n.onExecuted !== 'function') return {node: true, on_executed: false, before};
                n.onExecuted({anima_nl: [{seed: 777888999, used_llm: false, fallback: ''}]});
                await new Promise(r => setTimeout(r, 500));
                const after = read();
                n.onExecuted({anima_nl: [{seed: 42, used_llm: true, fallback: 'llm_timeout'}]});
                await new Promise(r => setTimeout(r, 500));
                const after2 = read();
                return {
                    node: true, on_executed: true,
                    has_seed_widget: !!seedW,
                    before: before,
                    after_seed: after,
                    after_fallback: after2,
                    renders_seed: !!after && String(after).includes('777888999')
                };
            })()""",
            await_promise=True,
        )

        # --- 3) WithLoRA 工作流也加载一遍（切换）
        wf2_path = os.path.join(project_root, args.wf2)
        if os.path.isfile(wf2_path):
            with open(wf2_path, encoding="utf-8") as fh:
                wf2 = json.load(fh)
            report["wf2_file"] = args.wf2
            report["wf2_load"] = cdp.eval(
                "(async () => { window.__wf2 = " + json.dumps(wf2, ensure_ascii=True) + ";"
                " try { await app.loadGraphData(window.__wf2); }"
                " catch (e) { return {ok: false, err: String(e).slice(0, 300)}; }"
                " await new Promise(r => setTimeout(r, 1200));"
                " const nodes = app.graph._nodes || [];"
                " return {ok: true, node_count: nodes.length,"
                "   has_composer: nodes.some(n => n.type === 'NL2PromptComposer'),"
                "   has_lora_loader: nodes.some(n => n.type === 'NL2PromptLoRAStack'),"
                "   lora_widgets: (nodes.find(n => n.type === 'NL2PromptLoRAStack') || {widgets: []}).widgets.map(w => w.name)};"
                " })()",
                await_promise=True,
            )
            if args.shot2:
                shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
                with open(os.path.join(project_root, args.shot2), "wb") as fh:
                    fh.write(base64.b64decode(shot["data"]))
                report["shot2"] = args.shot2

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
