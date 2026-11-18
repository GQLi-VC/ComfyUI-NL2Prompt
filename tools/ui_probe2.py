"""UI 探针 v2：聚焦 A4（设置窗口/恢复默认二次确认）与 A8（密钥不落盘）。

v1（tools/ui_probe.py）已验证 A1 结构、A2 动态行、A7 显隐切换。
v2 修正 v1 的按钮定位问题（v1 用全文档 textContent 搜索，命中了 ComfyUI 自带的"设置"按钮），
改为从节点的 DOM widget 元素内部定位，并补齐 A4 的三个关闭方式与 confirm 两分支、A8 的落盘检查。

用法：python -X utf8 tools/ui_probe2.py [--url ...] [--shot ...]
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


def js_sleep(ms: int) -> str:
    return f"await new Promise(r => setTimeout(r, {ms}));"


MAIN_WINDOW = """
(async () => {
    const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
    if (!n) return {node: false};
    const holder = n.widgets.find(w => w.name === '_ui_json');
    let st = {};
    try { st = JSON.parse(holder.value) || {}; } catch (e) {}
    st.system_prompt = 'CUSTOM_PROMPT_MARKER_XYZ';
    holder.value = JSON.stringify(st);
    if (holder.callback) { try { holder.callback(holder.value); } catch (e) {} }
    await new Promise(r => setTimeout(r, 300));

    const sw = n.widgets.find(w => w.name === 'anima_nl_settings');
    const btn = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!btn) return {node: true, settings_widget: !!sw, element: !!(sw && sw.element), btn: false};

    btn.click();
    await new Promise(r => setTimeout(r, 600));
    let overlay = document.querySelector('.anima-nl-overlay');
    if (!overlay) return {node: true, btn: true, overlay: false};

    const out = {node: true, btn: true, overlay: true};
    out.tabs = [...overlay.querySelectorAll('button')].map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 12);
    out.text_head = (overlay.innerText || '').replace(/\\s+/g, ' ').slice(0, 200);

    // --- 关闭方式 1：Esc
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await new Promise(r => setTimeout(r, 400));
    out.close_by_esc = !document.querySelector('.anima-nl-overlay');

    // --- 关闭方式 2：点遮罩
    btn.click();
    await new Promise(r => setTimeout(r, 400));
    overlay = document.querySelector('.anima-nl-overlay');
    if (overlay) {
        overlay.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
        await new Promise(r => setTimeout(r, 400));
    }
    out.close_by_mask = !document.querySelector('.anima-nl-overlay');

    // --- 关闭方式 3：点 ✕
    btn.click();
    await new Promise(r => setTimeout(r, 400));
    overlay = document.querySelector('.anima-nl-overlay');
    if (overlay) {
        const closeBtn = [...overlay.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '\u2715' || (b.textContent || '').includes('\u2715'));
        if (closeBtn) { closeBtn.click(); await new Promise(r => setTimeout(r, 400)); }
        out.close_btn_found = !!closeBtn;
    }
    out.close_by_x = !document.querySelector('.anima-nl-overlay');

    // --- 恢复默认：confirm 返回 false（应不生效）→ 返回 true（应生效）
    btn.click();
    await new Promise(r => setTimeout(r, 500));
    overlay = document.querySelector('.anima-nl-overlay');
    if (!overlay) return out;
    const sysTab = [...overlay.querySelectorAll('button')].find(b => (b.textContent || '').includes('系统提示词'));
    if (sysTab) { sysTab.click(); await new Promise(r => setTimeout(r, 400)); }
    const restore = [...overlay.querySelectorAll('button')].find(b => (b.textContent || '').includes('恢复默认'));
    out.restore_btn_found = !!restore;
    const textareas = () => [...document.querySelectorAll('.anima-nl-overlay textarea')];
    out.ta_count_in_window = textareas().length;
    out.ta_value_before = textareas().map(t => t.value.slice(0, 60));
    let calls = 0;
    const origConfirm = window.confirm;
    if (restore) {
        window.confirm = () => { calls++; return false; };
        restore.click();
        await new Promise(r => setTimeout(r, 400));
        out.confirm_calls_cancel = calls;
        out.ta_after_cancel = textareas().map(t => t.value.slice(0, 60));
        window.confirm = () => { calls++; return true; };
        restore.click();
        await new Promise(r => setTimeout(r, 800));
        out.confirm_calls_total = calls;
        out.ta_after_confirm = textareas().map(t => t.value.slice(0, 60));
    }
    window.confirm = origConfirm;
    return out;
})()
"""

LLM_WINDOW = """
(async () => {
    const n = app.graph._nodes.find(x => x.type === 'NL2PromptLoadLLM');
    if (!n) return {node: false};
    const sw = n.widgets.find(w => w.name === 'anima_llm_settings');
    const btn = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!btn) return {node: true, btn: false};
    btn.click();
    await new Promise(r => setTimeout(r, 600));
    let overlay = document.querySelector('.anima-nl-overlay');
    if (!overlay) return {node: true, btn: true, overlay: false};

    const text1 = overlay.innerText || '';
    const out = {
        node: true, btn: true, overlay: true,
        sampling_tab: {
            has_top_k: /Top\\s*K/i.test(text1),
            has_top_p: /Top\\s*P/i.test(text1),
            has_temp: /温度|temperature/i.test(text1),
            has_max_tokens: /max_tokens/i.test(text1),
            has_seed: /种子|seed/i.test(text1),
            has_last_seed: /上轮种子/i.test(text1),
            topk_grey_hint: /置灰|不支持/.test(text1)
        }
    };

    const rtab = [...overlay.querySelectorAll('button')].find(b => (b.textContent || '').includes('运行设置'));
    if (rtab) { rtab.click(); await new Promise(r => setTimeout(r, 500)); }
    const text2 = overlay.innerText || '';
    const pw = [...overlay.querySelectorAll('input[type=password]')];
    out.runtime_tab = {
        has_ctx: /ctx|上下文/i.test(text2),
        has_timeout: /timeout|超时/i.test(text2),
        password_inputs: pw.length,
        has_secret_status: /已配置|未配置|密钥/.test(text2),
        text_head: text2.replace(/\\s+/g, ' ').slice(0, 220)
    };

    // A8：填入假密钥并保存，随后序列化检查明文绝不出现
    const DUMMY = 'sk-DUMMY-9876543210';
    let saveClicked = false;
    if (pw.length) {
        pw[0].value = DUMMY;
        pw[0].dispatchEvent(new Event('input', {bubbles: true}));
        const saveBtn = [...overlay.querySelectorAll('button')].find(b => (b.textContent || '').includes('保存密钥'));
        if (saveBtn) { saveBtn.click(); saveClicked = true; await new Promise(r => setTimeout(r, 1500)); }
    }
    out.dummy_saved = saveClicked;
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await new Promise(r => setTimeout(r, 400));
    out.overlay_after = !!document.querySelector('.anima-nl-overlay');

    const holder = n.widgets.find(w => w.name === '_llm_json');
    const holderVal = holder ? String(holder.value) : '';
    const ser = JSON.stringify(app.graph.serialize());
    out.a8 = {
        holder_contains_dummy: holderVal.includes(DUMMY),
        serialized_contains_dummy: ser.includes(DUMMY),
        holder_has_configured_flag: /api_key_configured/.test(holderVal),
        holder_value_head: holderVal.slice(0, 180)
    };
    return out;
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9334)
    ap.add_argument("--shot", default="")
    ap.add_argument("--shot2", default="")
    args = ap.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe2_profile")
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

        cdp.eval(
            "document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1"
        )

        # 建两个节点
        report["setup"] = cdp.eval(
            """(() => {
                const a = LiteGraph.createNode('NL2PromptComposer'); a.pos = [60, 60]; app.graph.add(a);
                const b = LiteGraph.createNode('NL2PromptLoadLLM'); b.pos = [520, 60]; app.graph.add(b);
                app.graph.setDirtyCanvas(true, true);
                return {composer: a.id, llm: b.id};
            })()"""
        )
        time.sleep(1.5)

        report["main_window"] = cdp.eval(MAIN_WINDOW, await_promise=True)
        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(args.shot, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot"] = args.shot
        cdp.eval("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})), 1")
        time.sleep(0.4)

        report["llm_window"] = cdp.eval(LLM_WINDOW, await_promise=True)
        if args.shot2:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(args.shot2, "wb") as fh:
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
