"""UI 探针 v5：验证 v2 新增的前端能力。

覆盖：
  1. 主节点正面新增的「重写提示词 / 沿用上一轮」控件与状态小字
  2. 设置窗口 5 个 Tab（提示词替换 / 系统提示词 / 负面基础词 / 提示词技能 / 对话历史）
  3. 提示词技能 Tab：skill 下拉 + system_prompt 文本（应来自后端 skill 文件）
  4. 负面基础词 Tab：「填充推荐值」按钮
  5. LLM 节点：provider 34 选项 + 「当前：label · base_url」小字 + 云端「拉取模型」控件

用法：python -X utf8 tools/ui_probe5.py [--shot log/v2_ui.png]
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
    const out = {};
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);

    const c = LiteGraph.createNode('NL2PromptComposer'); c.pos = [60, 60]; app.graph.add(c);
    const l = LiteGraph.createNode('NL2PromptLoadLLM'); l.pos = [760, 60]; app.graph.add(l);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1400);

    // ---- 主节点正面
    out.composer_widgets = c.widgets.map(w => w.name);
    out.composer_widget_count = c.widgets.length;
    out.has_rewrite_actions = c.widgets.some(w => w.name === 'anima_nl_rewrite_actions');
    out.has_rewrite_status = c.widgets.some(w => w.name === 'anima_nl_rewrite_status');
    const st = c.widgets.find(w => w.name === 'anima_nl_rewrite_status');
    out.rewrite_status_text = st && st.element ? (st.element.innerText || '').trim().slice(0, 80) : null;
    const seedW = c.widgets.find(w => w.name === 'anima_nl_seed');
    out.seed_text = seedW && seedW.element ? (seedW.element.innerText || '').trim() : null;

    // 主节点按钮文案
    const btns = [...document.querySelectorAll('button')].map(b => (b.textContent || '').trim()).filter(Boolean);
    out.has_rewrite_button = btns.some(t => t.includes('重写提示词'));
    out.has_reuse_button = btns.some(t => t.includes('沿用上一轮'));

    // ---- 设置窗口 5 Tab
    const sw = c.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) { gear.click(); await sleep(700); }
    let ov = document.querySelector('.anima-nl-overlay');
    out.window_opened = !!ov;
    if (ov) {
        out.tab_labels = [...ov.querySelectorAll('button')].map(b => (b.textContent || '').trim()).filter(Boolean);
        // 提示词技能 Tab
        const skillTab = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').includes('提示词技能'));
        out.has_skill_tab = !!skillTab;
        if (skillTab) {
            skillTab.click(); await sleep(900);
            ov = document.querySelector('.anima-nl-overlay');
            const sel = ov ? ov.querySelector('select') : null;
            const ta = ov ? [...ov.querySelectorAll('textarea')] : [];
            out.skill_select_options = sel ? [...sel.options].map(o => ({v: o.value, t: o.textContent.trim()})) : null;
            out.skill_textarea_count = ta.length;
            out.skill_prompt_head = ta.length ? (ta[0].value || '').slice(0, 120) : null;
            out.skill_prompt_len = ta.length ? (ta[0].value || '').length : 0;
            const skillBtns = [...ov.querySelectorAll('button')].map(b => (b.textContent || '').trim());
            out.skill_action_buttons = skillBtns.filter(t => /恢复默认|保存到服务器|保存/.test(t));
        }
        // 对话历史 Tab
        const histTab = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').includes('对话历史'));
        out.has_history_tab = !!histTab;
        if (histTab) {
            histTab.click(); await sleep(700);
            ov = document.querySelector('.anima-nl-overlay');
            out.history_text = ov ? (ov.innerText || '').replace(/\\s+/g, ' ').slice(0, 160) : null;
        }
        // 负面基础词 Tab
        const negTab = [...ov.querySelectorAll('button')].find(b => (b.textContent || '').includes('负面基础词'));
        out.has_negative_tab = !!negTab;
        if (negTab) {
            negTab.click(); await sleep(600);
            ov = document.querySelector('.anima-nl-overlay');
            const negBtns = ov ? [...ov.querySelectorAll('button')].map(b => (b.textContent || '').trim()) : [];
            out.negative_has_fill_button = negBtns.some(t => t.includes('填充推荐值'));
        }
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
        await sleep(300);
    }
    out.window_closed = !document.querySelector('.anima-nl-overlay');

    // ---- 点「重写提示词」（无 LLM 节点连接 → 应显示错误，不崩）
    const rwBtn = [...document.querySelectorAll('button')].find(b => (b.textContent || '').includes('重写提示词'));
    if (rwBtn) {
        rwBtn.click();
        await sleep(2500);
        const st2 = c.widgets.find(w => w.name === 'anima_nl_rewrite_status');
        out.rewrite_after_click = st2 && st2.element ? (st2.element.innerText || '').trim().slice(0, 120) : null;
    }

    // ---- LLM 节点
    out.llm_widgets = l.widgets.map(w => w.name);
    const pw = l.widgets.find(w => w.name === 'provider');
    out.provider_option_count = pw && pw.options && pw.options.values ? pw.options.values.length : null;
    out.provider_value = pw ? pw.value : null;
    const hint = l.widgets.find(w => w.name === 'anima_llm_provider_hint');
    out.provider_hint_text = hint && hint.element ? (hint.element.innerText || '').trim().slice(0, 100) : null;
    out.has_fetch_widget = l.widgets.some(w => w.name === 'anima_llm_fetch');
    const mt = l.widgets.find(w => w.name === 'model_type');
    if (mt) {
        mt.value = '云端模型';
        if (mt.callback) { try { mt.callback(mt.value); } catch (e) {} }
        await sleep(900);
    }
    out.llm_visible_cloud = l.widgets.filter(w => !w.hidden && w.serialize !== false).map(w => w.name);
    const fetchW = l.widgets.find(w => w.name === 'anima_llm_fetch');
    out.fetch_visible_in_cloud = fetchW ? !fetchW.hidden : null;
    const fetchBtns = fetchW && fetchW.element ? [...fetchW.element.querySelectorAll('button')].map(b => (b.textContent || '').trim()) : [];
    out.fetch_buttons = fetchBtns;
    return out;
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9338)
    ap.add_argument("--shot", default="")
    args = ap.parse_args()
    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe5_profile")
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
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1700, "height": 1200, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")
        report["ui"] = cdp.eval(SCRIPT, await_promise=True)
        time.sleep(0.8)
        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, args.shot), "wb") as fh:
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
