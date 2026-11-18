# -*- coding: utf-8 -*-
"""截取 v3 直连工作流的节点连线示例图（中 / 英）。

流程：启动 headless Chrome → 打开 ComfyUI → 设 locale → 加载工作流 → 自动适配视图 → 截图。

用法：
    python tools/shot_workflow_examples.py [--port 8199] [--out docs/images]
"""
import base64
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_probe import WS, CDP, start_chrome, page_target, wait_js  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "workflows" / "NL2Prompt_v3_Direct.workflow.json"
DEFAULT_OUT = ROOT / "docs" / "images"

VIEW_W, VIEW_H = 1920, 1000

CLOSE_DIALOGS = """(() => {
    let n = 0;
    document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role="dialog"]').forEach(e => { e.remove(); n++; });
    return n;
})()"""

# 隐藏画布之外的 UI 外壳（顶部标签栏、左侧工具栏、小地图、缩放条、队列按钮、浮动预览）
HIDE_UI = """(() => {
    const old = document.getElementById('__nl2shot_hide');
    if (old) old.remove();
    const st = document.createElement('style');
    st.id = '__nl2shot_hide';
    st.textContent = `
        .workflow-tabs-container,
        .side-toolbar-container,
        [data-testid="side-toolbar"],
        .minimap-main-container,
        [class*="minimap"],
        .queue-button-group,
        [class*="preview-panel"],
        [class*="graph-preview"],
        .graph-canvas-menu,
        [class*="bottom-panel"] { display: none !important; visibility: hidden !important; }
        [class*="inset-x-0"][class*="bottom-0"] { display: none !important; }
    `;
    document.head.appendChild(st);
    return document.querySelectorAll('#__nl2shot_hide').length;
})()"""

# 截图时裁掉顶部标签栏（38px），避免浏览器层面的浮层残留
TOP_INSET = 38

# 清理画布上可能残留的默认节点，然后加载工作流并适配视图
LOAD_WORKFLOW = """(async () => {
    const wf = JSON.parse(%s);
    if (app.graph) { app.graph.clear(); }
    await app.loadGraphData(wf);
    await new Promise(r => setTimeout(r, 1500));

    const nodes = (app.graph && app.graph._nodes) || [];
    let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    for (const n of nodes) {
        const w = (n.size && n.size[0]) || 200;
        const h = (n.size && n.size[1]) || 100;
        minX = Math.min(minX, n.pos[0]);
        minY = Math.min(minY, n.pos[1]);
        maxX = Math.max(maxX, n.pos[0] + w);
        maxY = Math.max(maxY, n.pos[1] + h);
    }
    const c = app.canvas;
    const dpr = window.devicePixelRatio || 1;
    const cw = c.canvas.width / dpr;
    const ch = c.canvas.height / dpr;
    const pad = 70;
    const sx = (cw - pad * 2) / Math.max(1, maxX - minX);
    const sy = (ch - pad * 2) / Math.max(1, maxY - minY);
    const s = Math.min(sx, sy, 1.5);
    c.ds.scale = s;
    c.ds.offset = [-(minX * s) + (cw - (maxX - minX) * s) / 2, -(minY * s) + (ch - (maxY - minY) * s) / 2];
    c.setDirty(true, true);
    await new Promise(r => setTimeout(r, 900));
    return { nodes: nodes.length, scale: Math.round(s * 1000) / 1000, viewport: [Math.round(cw), Math.round(ch)] };
})()"""


def set_locale(cdp, mode):
    """走后端 settings_ui 设置插件语言（auto / zh / en）。"""
    js = """
    (async () => {
        const g = await (await fetch('/nl2prompt/settings_ui')).json();
        const body = { ui_locale: %s, default_positive_base: g.default_positive_base || '' };
        const r = await fetch('/nl2prompt/settings_ui', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        return { ok: r.ok, status: r.status };
    })()
    """ % json.dumps(mode)
    return cdp.eval(js, await_promise=True)


def set_comfy_locale(cdp, mode):
    """把 ComfyUI 前端语言也切过去，让原生节点控件（模型/正向条件…）跟随。"""
    js = """(() => {
        try {
            app.ui.settings.setSettingValue('Comfy.Locale', %s);
            return { ok: true, current: app.ui.settings.getSettingValue('Comfy.Locale') };
        } catch (e) { return { ok: false, err: String(e) }; }
    })()""" % json.dumps(mode)
    return cdp.eval(js)


TITLES = {
    "zh": {
        "UNETLoader": "UNET加载（Anima 主模型）",
        "CLIPLoader": "CLIP加载（Qwen3-06B）",
        "EmptyLatentImage": "空 Latent 1024x1024",
        "NL2PromptComposer": "自然语言提示词",
        "NL2PromptLoRAStack": "LoRA 堆栈加载",
        "KSampler": "KSampler",
        "VAELoader": "VAE加载（qwen_image_vae）",
        "VAEDecode": "VAE 解码",
        "SaveImage": "保存图像",
    },
    "en": {
        "UNETLoader": "Load UNET (Anima base)",
        "CLIPLoader": "Load CLIP (Qwen3-06B)",
        "EmptyLatentImage": "Empty Latent 1024x1024",
        "NL2PromptComposer": "Natural Language Prompt",
        "NL2PromptLoRAStack": "LoRA Stack Loader",
        "KSampler": "KSampler",
        "VAELoader": "Load VAE (qwen_image_vae)",
        "VAEDecode": "VAE Decode",
        "SaveImage": "Save Image",
    },
}


def set_node_titles(cdp, locale):
    """工作流 JSON 里的中文 title 会覆盖插件的语言切换，这里按目标语言重设。"""
    js = """(() => {
        const T = %s;
        let n = 0;
        for (const node of (app.graph && app.graph._nodes) || []) {
            const t = T[node.type];
            if (t) { node.title = t; n++; }
            if (node.setDirtyCanvas) node.setDirtyCanvas(true, true);
        }
        app.canvas.setDirty(true, true);
        return n;
    })()""" % json.dumps(TITLES[locale], ensure_ascii=False)
    return cdp.eval(js)


def shot(cdp, path, clip=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    params = {"format": "png", "captureBeyondViewport": False}
    if clip:
        params["clip"] = {
            "x": 0, "y": TOP_INSET,
            "width": VIEW_W, "height": VIEW_H - TOP_INSET,
            "scale": 1,
        }
    data = cdp.call("Page.captureScreenshot", params)
    path.write_bytes(base64.b64decode(data["data"]))
    return path


def shoot_locale(port, chrome_port, out_dir, locale, fname, wf_json, report):
    """用一个独立的 Chrome 实例截一个语言的图（避免同连接内 reload 导致 CDP 超时）。"""
    url = f"http://127.0.0.1:{port}"
    proc = ws = None
    try:
        profile = os.path.join(os.environ.get("TEMP", "."), f"nl2prompt_shot_{locale}_{chrome_port}")
        proc, base = start_chrome(chrome_port, profile)
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": VIEW_W, "height": VIEW_H, "deviceScaleFactor": 1, "mobile": False})

        cdp.call("Page.navigate", {"url": url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["errors"].append(f"app not ready ({locale})")
            return False
        time.sleep(1.5)
        cdp.eval(CLOSE_DIALOGS)

        loc = set_locale(cdp, locale)
        comfy_loc = set_comfy_locale(cdp, locale)
        cdp.call("Page.navigate", {"url": url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app2"):
            report["errors"].append(f"app not ready after locale={locale}")
            return False
        time.sleep(2.0)
        cdp.eval(CLOSE_DIALOGS)

        loaded = cdp.eval(LOAD_WORKFLOW % json.dumps(wf_json), await_promise=True)
        titled = set_node_titles(cdp, locale)
        cdp.eval(CLOSE_DIALOGS)
        hidden = cdp.eval(HIDE_UI)
        time.sleep(0.6)
        # 隐藏外壳后强制画布重新适配
        cdp.eval("""(() => {
            const c = app.canvas;
            if (c && c.resize) { try { c.resize(); } catch (e) {} }
            const nodes = (app.graph && app.graph._nodes) || [];
            if (!nodes.length) return 0;
            let minX=1e9,minY=1e9,maxX=-1e9,maxY=-1e9;
            for (const n of nodes) {
                const w=(n.size&&n.size[0])||200, h=(n.size&&n.size[1])||100;
                minX=Math.min(minX,n.pos[0]); minY=Math.min(minY,n.pos[1]);
                maxX=Math.max(maxX,n.pos[0]+w); maxY=Math.max(maxY,n.pos[1]+h);
            }
            const dpr=window.devicePixelRatio||1;
            const cw=c.canvas.width/dpr, ch=c.canvas.height/dpr, pad=70;
            const s=Math.min((cw-pad*2)/Math.max(1,maxX-minX), (ch-pad*2)/Math.max(1,maxY-minY), 1.5);
            c.ds.scale=s; c.ds.offset=[-(minX*s)+(cw-(maxX-minX)*s)/2, -(minY*s)+(ch-(maxY-minY)*s)/2];
            c.setDirty(true,true);
            return Math.round(s*1000)/1000;
        })()""")
        time.sleep(0.7)

        p = shot(cdp, out_dir / fname, clip=True)
        report["shots"].append({
            "locale": locale, "file": fname, "bytes": p.stat().st_size,
            "set_locale": loc, "comfy_locale": comfy_loc, "loaded": loaded,
            "titled": titled, "hidden_ui_elements": hidden,
        })
        print(f"[shot] {fname}  locale={locale}  {p.stat().st_size // 1024} KB  loaded={loaded}")
        return True
    except Exception as exc:
        report["errors"].append(f"{locale}: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if proc:
            proc.kill()


def main() -> int:
    port = 8199
    chrome_port = 9351
    out_dir = DEFAULT_OUT
    only = None
    argv = sys.argv[1:]
    while argv:
        f = argv.pop(0)
        if f == "--port" and argv:
            port = int(argv.pop(0))
        elif f == "--chrome-port" and argv:
            chrome_port = int(argv.pop(0))
        elif f == "--out" and argv:
            out_dir = pathlib.Path(argv.pop(0))
        elif f == "--locale" and argv:
            only = argv.pop(0)

    if not WORKFLOW.is_file():
        print(f"workflow not found: {WORKFLOW}", file=sys.stderr)
        return 2

    wf_json = WORKFLOW.read_text(encoding="utf-8")
    report = {"url": f"http://127.0.0.1:{port}", "workflow": WORKFLOW.name, "shots": [], "errors": []}

    targets = [("zh", "workflow-example-zh.png"), ("en", "workflow-example-en.png")]
    if only:
        targets = [t for t in targets if t[0] == only]

    ok = True
    for i, (locale, fname) in enumerate(targets):
        ok = shoot_locale(port, chrome_port + i, out_dir, locale, fname, wf_json, report) and ok

    # 复位语言为 auto
    try:
        proc2, base2 = start_chrome(chrome_port + 20, os.path.join(os.environ.get("TEMP", "."), "nl2prompt_shot_reset"))
        tgt2 = page_target(base2)
        ws2 = WS(tgt2["webSocketDebuggerUrl"])
        cdp2 = CDP(ws2)
        cdp2.call("Page.enable")
        cdp2.call("Runtime.enable")
        cdp2.call("Page.navigate", {"url": f"http://127.0.0.1:{port}"})
        wait_js(cdp2, "!!(window.app && window.app.graph)", True, 120, "reset")
        set_locale(cdp2, "auto")
        ws2.close()
        proc2.kill()
        report["locale_reset"] = True
    except Exception as exc:
        report["locale_reset"] = f"failed: {exc}"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1



if __name__ == "__main__":
    sys.exit(main())
