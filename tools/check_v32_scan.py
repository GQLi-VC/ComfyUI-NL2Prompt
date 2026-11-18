"""v3.2/v3.3 专项验证：刷新模型端点与文案按变体、legacy 三形态显隐、Tab 结构。

逐场景调用（每个场景一次 CDP eval，避免单次调用过长触发 websocket 超时）。

用法：
    python -X utf8 tools/check_v32_scan.py
    python -X utf8 tools/check_v32_scan.py --port 8199 --hf <dir> --fixture <dir>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from ui_probe import CDP, WS, page_target, start_chrome, wait_js  # noqa: E402

DEFINE = """
window.__v32Probe = async (args) => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const n = LiteGraph.createNode(args.type);
    if (!n) return { error: 'create failed' };
    n.pos = [80, 80];
    app.graph.add(n);
    await sleep(900);

    if (args.modelType) {
        const mt = n.widgets.find(w => w.name === 'model_type');
        if (mt) {
            mt.value = args.modelType;
            try { mt.callback?.(args.modelType); } catch (e) {}
            await sleep(400);
        }
    }
    if (args.field) {
        const fw = n.widgets.find(w => w.name === args.field);
        if (fw) {
            fw.value = args.value;
            try { fw.callback?.(args.value); } catch (e) {}
            await sleep(200);
        }
    }

    const refresh = n.widgets.find(w => w.name === 'anima_llm_refresh');
    const btn = refresh && refresh.element ? refresh.element.querySelector('button') : null;
    const tooltip = btn ? (btn.title || '') : '';
    const mf = n.widgets.find(w => w.name === 'model_file');
    const modelFileBefore = mf ? mf.value : null;
    if (btn && refresh && !refresh.hidden) btn.click();
    await sleep(2200);
    const result = {
        type: args.type,
        model_type: (n.widgets.find(w => w.name === 'model_type')?.value) ?? null,
        refresh_hidden: refresh ? !!refresh.hidden : null,
        refresh_after: btn ? (btn.textContent || '').trim() : null,
        tooltip: tooltip,
        model_file_before: modelFileBefore,
        model_file_after: mf ? mf.value : null,
        option_values: mf && mf.options && Array.isArray(mf.options.values) ? mf.options.values.slice(0, 6) : null,
        visibility: Object.fromEntries(
            n.widgets.filter(w => w.serialize !== false && w.name !== '_llm_json')
                .map(w => [w.name, !w.hidden]),
        ),
        tab_labels: null,
        cloud_has_api_key: null,
    };

    const sw = n.widgets.find(w => w.name === 'anima_llm_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) {
        gear.click();
        await sleep(800);
        const ov = document.querySelector('.anima-nl-overlay');
        if (ov) {
            result.tab_labels = [...ov.querySelectorAll('.anima-nl-tab')].map(b => (b.textContent || '').trim());
            const cloudTab = [...ov.querySelectorAll('.anima-nl-tab')]
                .find(b => /云端模型|Cloud model/.test(b.textContent || ''));
            if (cloudTab) {
                cloudTab.click();
                await sleep(600);
                const panel = ov.querySelector('.anima-nl-tabpanel.is-active');
                result.cloud_has_api_key = panel ? !!panel.querySelector('input[type=password]') : null;
            }
            ov.remove();
        }
    }
    try { app.graph.remove(n); } catch (e) {}
    await sleep(150);
    return result;
};
1
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8199
    chrome_port = 9395
    hf_dir = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    fixture_dir = os.path.join(tempfile.gettempdir(), "nl2prompt_hf_fixture")
    while argv:
        flag = argv.pop(0)
        if flag == "--port" and argv:
            port = int(argv.pop(0))
        elif flag == "--chrome-port" and argv:
            chrome_port = int(argv.pop(0))
        elif flag == "--hf" and argv:
            hf_dir = argv.pop(0)
        elif flag == "--fixture" and argv:
            fixture_dir = argv.pop(0)

    empty_dir = os.path.join(tempfile.gettempdir(), "nl2prompt_empty_scan")
    os.makedirs(empty_dir, exist_ok=True)
    for sub_dir, has_config in (("a", True), (os.path.join("c", "nested"), True), ("b", False)):
        target = os.path.join(fixture_dir, sub_dir)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "model.safetensors"), "wb") as fh:
            fh.write(b"\x00" * 16)
        if has_config:
            with open(os.path.join(target, "config.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")

    scenarios = [
        ("safetensors_empty", {"type": "NL2PromptLLMSafetensors", "field": "model_dir", "value": empty_dir}),
        ("safetensors_hf", {"type": "NL2PromptLLMSafetensors", "field": "model_dir", "value": hf_dir}),
        ("safetensors_fixture", {"type": "NL2PromptLLMSafetensors", "field": "model_dir", "value": fixture_dir}),
        ("gguf_empty", {"type": "NL2PromptLLMGGUF", "field": "model_path", "value": empty_dir}),
        ("gguf_hf", {"type": "NL2PromptLLMGGUF", "field": "model_path", "value": hf_dir}),
        ("cloud", {"type": "NL2PromptLLMCloud", "field": "base_url", "value": ""}),
        ("legacy_gguf", {"type": "NL2PromptLoadLLM", "modelType": "本地模型",
                         "field": "model_path", "value": empty_dir}),
        ("legacy_safetensors", {"type": "NL2PromptLoadLLM", "modelType": "本地模型（safetensors）",
                                "field": "model_dir", "value": hf_dir}),
        ("legacy_cloud", {"type": "NL2PromptLoadLLM", "modelType": "云端模型",
                          "field": "base_url", "value": ""}),
    ]

    report = {"empty_dir": empty_dir, "hf_dir": hf_dir, "fixture_dir": fixture_dir, "result": {}}
    proc = ws = None
    try:
        proc, base = start_chrome(chrome_port, os.path.join(os.environ.get("TEMP", "."), "check_v32_scan"))
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d" % port})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 150, "app"):
            print(json.dumps({"error": "app not ready"}, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")
        cdp.eval(DEFINE)
        for name, args in scenarios:
            report["result"][name] = cdp.eval(
                "window.__v32Probe(%s)" % json.dumps(args, ensure_ascii=False),
                await_promise=True,
            )
            time.sleep(0.2)
        print(json.dumps(report, ensure_ascii=False, indent=1))
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
