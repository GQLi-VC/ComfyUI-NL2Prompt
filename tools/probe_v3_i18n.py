"""v3 i18n 探针：强制 zh / en 各 3 张截图 + en 下实测「重写提示词」仍可用。

用法：
    python -X utf8 tools/probe_v3_i18n.py
    python -X utf8 tools/probe_v3_i18n.py --out D:/tmp/x --port 8199 --chrome-port 9341

前置：测试环境已启动（默认 http://127.0.0.1:8199）且插件已加载。
产出：<项目>/log/v3_ui/i18n_zh_*.png（3 张）、i18n_en_1/2/3/4/5_*.png（5 张）+ 一行 JSON 报告（stdout）。

流程要点
    1. 后端 settings_ui 的 ui_locale 设成 zh → reload，确认节点正面中文。
    2. 截「主节点正面 / 通用 Tab / 云端模型 Tab」三张 zh 图。
    3. 在通用 Tab 里把语言下拉改成 en（真实交互，不 reload）→ 验证「立即生效」。
    4. 截三张 en 图，并确认 COMBO 值（本地模型/云端模型）仍是数据原样。
    5. en 下关闭窗口、点「重写提示词」→ 功能不崩、状态文案为英文。
    6. 结束时把 ui_locale 复位为 auto 并 reload，避免污染测试环境。
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
BASE_URL = "http://127.0.0.1:8199"

# 建两个节点（清空画布，保证截图只含目标节点）
CREATE_NODES = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const c = LiteGraph.createNode('NL2PromptComposer'); c.pos = [40, 40]; app.graph.add(c);
    const l = LiteGraph.createNode('NL2PromptLoadLLM'); l.pos = [760, 40]; app.graph.add(l);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1400);
    return { composer: !!c, llm: !!l };
})()
"""

# 读节点正面的可见文案（判断语言是否真的换了）
READ_FACES = """
(() => {
    const grab = (type) => {
        const n = app.graph._nodes.find(x => x.type === type);
        if (!n) return null;
        const out = {};
        for (const w of n.widgets) {
            const el = w.element;
            if (!el || w.hidden) continue;
            out[w.name] = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
            const btn = el.querySelector('button');
            if (btn) out[w.name + ':btn'] = (btn.textContent || '').trim();
        }
        return out;
    };
    const c = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
    const l = app.graph._nodes.find(x => x.type === 'NL2PromptLoadLLM');
    return {
        composer: grab('NL2PromptComposer'),
        llm: grab('NL2PromptLoadLLM'),
        composerModelType: c ? String(c.widgets.find(w => w.name === 'use_llm')?.value) : null,
        llmModelType: l ? String(l.widgets.find(w => w.name === 'model_type')?.value) : null,
        llmModelTypeOptions: l ? (l.widgets.find(w => w.name === 'model_type')?.options?.values || []) : [],
    };
})()
"""

OPEN_COMPOSER_GENERAL = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(200);
    const c = app.graph._nodes.find(n => n.type === 'NL2PromptComposer');
    const sw = c && c.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) gear.click();
    await sleep(900);
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { opened: false };
    const tab = [...ov.querySelectorAll('.anima-nl-tab')].find(b => (b.textContent || '').trim().length > 0);
    if (tab) tab.click();
    await sleep(500);
    const sel = [...ov.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === 'auto'));
    return {
        opened: true,
        title: (ov.querySelector('.anima-nl-dialog-title') || {}).textContent || '',
        tabLabels: [...ov.querySelectorAll('.anima-nl-tab')].map(b => (b.textContent || '').trim()),
        hasLocaleSelect: !!sel,
        localeValue: sel ? sel.value : null,
        localeOptionLabels: sel ? [...sel.options].map(o => (o.textContent || '').trim()) : [],
        status: (ov.querySelector('.anima-nl-status') || {}).textContent || '',
    };
})()
"""

SWITCH_LOCALE = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const sel = [...ov.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === 'auto'));
    if (!sel) return { ok: false, reason: 'no locale select' };
    sel.value = %s;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(1200);
    const ov2 = document.querySelector('.anima-nl-overlay');
    return {
        ok: true,
        status: (ov2.querySelector('.anima-nl-status') || {}).textContent || '',
        title: (ov2.querySelector('.anima-nl-dialog-title') || {}).textContent || '',
        tabLabels: [...ov2.querySelectorAll('.anima-nl-tab')].map(b => (b.textContent || '').trim()),
        localeOptionLabels: [...sel.options].map(o => (o.textContent || '').trim()),
    };
})()
"""

OPEN_LLM_CLOUD = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(300);
    const l = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
    const mt = l.widgets.find(w => w.name === 'model_type');
    if (mt) { mt.value = '云端模型'; if (mt.callback) { try { mt.callback(mt.value); } catch (e) {} } }
    await sleep(300);
    const sw = l.widgets.find(w => w.name === 'anima_llm_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (gear) gear.click();
    await sleep(900);
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { opened: false };
    const tabs = [...ov.querySelectorAll('.anima-nl-tab')];
    const target = tabs[1] || null;
    if (target) target.click();
    await sleep(700);
    const panel = ov.querySelector('.anima-nl-tabpanel.is-active');
    const btns = panel ? [...panel.querySelectorAll('button')].map(b => (b.textContent || '').trim()) : [];
    const inputs = panel ? [...panel.querySelectorAll('input')].map(i => i.placeholder || '') : [];
    return {
        opened: true,
        title: (ov.querySelector('.anima-nl-dialog-title') || {}).textContent || '',
        tabLabels: tabs.map(b => (b.textContent || '').trim()),
        buttons: btns,
        inputPlaceholders: inputs,
        panelText: panel ? (panel.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 400) : '',
        nodeCloudModel: String(l.widgets.find(w => w.name === 'cloud_model').value || ''),
    };
})()
"""

CLICK_REWRITE = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(300);
    const c = app.graph._nodes.find(n => n.type === 'NL2PromptComposer');
    const nl = c.widgets.find(w => w.name === 'natural_language');
    if (nl && !String(nl.value || '').trim()) {
        nl.value = 'rainy neon street, purple hair girl';
        if (nl.callback) { try { nl.callback(nl.value); } catch (e) {} }
    }
    const aw = c.widgets.find(w => w.name === 'anima_nl_rewrite_actions');
    const buttons = aw && aw.element ? [...aw.element.querySelectorAll('button')] : [];
    const rewrite = buttons.find(b => /重写|Rewrite/.test(b.textContent || ''));
    if (!rewrite) return { ok: false, reason: 'no rewrite button', buttons: buttons.map(b => (b.textContent || '').trim()) };
    rewrite.click();
    await sleep(6000);
    const st = c.widgets.find(w => w.name === 'anima_nl_rewrite_status');
    return {
        ok: true,
        buttonLabel: (rewrite.textContent || '').trim(),
        status: st && st.element ? (st.element.textContent || '').trim() : '',
        isError: st && st.element ? st.element.classList.contains('is-error') : null,
        isOk: st && st.element ? st.element.classList.contains('is-ok') : null,
    };
})()
"""

AUTO_WITH_ENGLISH_COMFY = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const settings = app.ui.settings;
    if (settings && !settings.__nl2promptOrigGet) {
        settings.__nl2promptOrigGet = settings.getSettingValue.bind(settings);
        settings.getSettingValue = (key) => (key === 'Comfy.Locale' ? 'en' : settings.__nl2promptOrigGet(key));
    }
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(250);
    const c = app.graph._nodes.find(n => n.type === 'NL2PromptComposer');
    const sw = c && c.widgets.find(w => w.name === 'anima_nl_settings');
    const gear = sw && sw.element ? sw.element.querySelector('button') : null;
    if (!gear) return { ok: false, reason: 'no settings button' };
    gear.click();
    await sleep(800);
    const ov = document.querySelector('.anima-nl-overlay');
    if (!ov) return { ok: false, reason: 'no overlay' };
    const tabs = [...ov.querySelectorAll('.anima-nl-tab')];
    if (tabs[0]) tabs[0].click();
    await sleep(500);
    const sel = [...ov.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === 'auto'));
    if (!sel) return { ok: false, reason: 'no locale select', tabs: tabs.map(b => (b.textContent || '').trim()) };
    sel.value = 'auto';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(1600);
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(300);
    return { ok: true };
})()
"""

READ_LOCALE_STATE = """
(async () => {
    const headText = (() => {
        const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
        const w = n && n.widgets.find(x => x.name === 'anima_nl_head');
        return w && w.element ? (w.element.textContent || '').trim() : '';
    })();
    let backend = null;
    try { backend = await (await fetch('/nl2prompt/settings_ui')).json(); } catch (e) {}
    let comfyLocale = null;
    try { comfyLocale = app?.ui?.settings?.getSettingValue?.('Comfy.Locale') ?? null; } catch (e) {}
    const isChinese = /[\u4e00-\u9fff]/.test(headText);
    return {
        htmlLang: document.documentElement.lang,
        localStorageMode: (() => { try { return localStorage.getItem('nl2prompt.ui_locale'); } catch (e) { return null; } })(),
        backendUiLocale: backend ? backend.ui_locale : null,
        comfyLocale: comfyLocale === undefined ? null : comfyLocale,
        navigatorLanguage: navigator.language,
        headText,
        textLocale: isChinese ? 'zh' : 'en',
        consistent: document.documentElement.lang === (isChinese ? 'zh' : 'en'),
    };
})()
"""

READ_NATIVE_WIDGETS = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(200);
    const pick = (w) => ({
        tooltip: String((w.options && w.options.tooltip) || w.tooltip || ''),
        label_on: w.options ? w.options.label_on ?? null : null,
        label_off: w.options ? w.options.label_off ?? null : null,
        value: typeof w.value === 'string' ? w.value.slice(0, 40) : w.value,
    });
    const composer = app.graph._nodes.find(n => n.type === 'NL2PromptComposer');
    const llm = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
    // 三个拆分节点：临时创建，读完就删（只验证原生 widget 文案本地化）
    const split = {};
    for (const type of ['NL2PromptLLMGGUF', 'NL2PromptLLMSafetensors', 'NL2PromptLLMCloud']) {
        let n = null;
        try {
            n = LiteGraph.createNode(type);
            if (n) { n.pos = [-4000, -4000]; app.graph.add(n); }
        } catch (e) { n = null; }
        if (!n) { split[type] = null; continue; }
        const data = {};
        for (const w of n.widgets) data[w.name] = pick(w);
        split[type] = data;
        try { app.graph.remove(n); } catch (e) {}
    }
    const collect = (node) => {
        if (!node) return null;
        const out = {};
        for (const w of node.widgets) out[w.name] = pick(w);
        return out;
    };
    return {
        composer: collect(composer),
        llm: collect(llm),
        splitNodes: split,
        useLlmValue: composer ? composer.widgets.find(w => w.name === 'use_llm')?.value : null,
        comboModelType: llm ? llm.widgets.find(w => w.name === 'model_type')?.value : null,
    };
})()
"""

FOCUS_LLM = """
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove());
    await sleep(250);
    const l = app.graph._nodes.find(n => n.type === 'NL2PromptLoadLLM');
    if (!l) return { ok: false };
    const mt = l.widgets.find(w => w.name === 'model_type');
    if (mt && String(mt.value) !== '本地模型') {
        mt.value = '本地模型';
        if (mt.callback) { try { mt.callback('本地模型'); } catch (e) {} }
    }
    try { app.canvas.centerOnNode(l); } catch (e) {}
    app.canvas.setDirty(true, true);
    await sleep(800);
    const useLlm = (app.graph._nodes.find(n => n.type === 'NL2PromptComposer') || { widgets: [] })
        .widgets.find(w => w.name === 'use_llm');
    return {
        ok: true,
        llmWidgets: l.widgets.filter(w => !w.hidden).map(w => w.name),
        useLlmLabelOn: useLlm && useLlm.options ? useLlm.options.label_on : null,
        useLlmLabelOff: useLlm && useLlm.options ? useLlm.options.label_off : null,
    };
})()
"""

CLOSE_ALL = "(document.querySelectorAll('.anima-nl-overlay').forEach(e => e.remove()), 1)"


def set_locale_backend(cdp, mode):
    """直接改后端 settings_ui（保留其它字段），返回是否成功。"""
    js = """
    (async () => {
        const g = await (await fetch('/nl2prompt/settings_ui')).json();
        const body = {
            ui_locale: %s,
            default_positive_base: g.default_positive_base || '',
        };
        const r = await fetch('/nl2prompt/settings_ui', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        return { ok: r.ok, status: r.status, body: await r.json().catch(() => null) };
    })()
    """ % json.dumps(mode)
    return cdp.eval(js, await_promise=True)


def shot(cdp, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(data["data"]))
    return path


def reload_app(cdp, port):
    cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d" % port})
    return wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app")


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
        proc, base = start_chrome(chrome_port, os.path.join(os.environ.get("TEMP", "."), "v3_i18n_profile"))
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1680, "height": 1050, "deviceScaleFactor": 1, "mobile": False})

        if not reload_app(cdp, port):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role=\"dialog\"]').forEach(e => e.remove()), 1")

        # ---------- 1) 强制 zh
        report["set_zh"] = set_locale_backend(cdp, "zh")
        if not reload_app(cdp, port):
            report["error"] = "app not ready after zh reload"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        report["nodes"] = cdp.eval(CREATE_NODES, await_promise=True)
        time.sleep(0.5)
        report["faces_zh"] = cdp.eval(READ_FACES)
        report["shots"].append(shot(cdp, "i18n_zh_1_node_face.png"))

        report["locale_state_zh"] = cdp.eval(READ_LOCALE_STATE, await_promise=True)
        report["general_zh"] = cdp.eval(OPEN_COMPOSER_GENERAL, await_promise=True)
        time.sleep(0.5)
        report["shots"].append(shot(cdp, "i18n_zh_2_general_tab.png"))

        report["cloud_zh"] = cdp.eval(OPEN_LLM_CLOUD, await_promise=True)
        time.sleep(0.5)
        report["shots"].append(shot(cdp, "i18n_zh_3_cloud_tab.png"))

        # ---------- 2) 界面里切到 en（不 reload，验证立即生效）
        cdp.eval(OPEN_COMPOSER_GENERAL, await_promise=True)
        time.sleep(0.4)
        report["switch_en"] = cdp.eval(SWITCH_LOCALE % json.dumps("en"), await_promise=True)
        time.sleep(0.5)
        report["faces_en"] = cdp.eval(READ_FACES)
        report["shots"].append(shot(cdp, "i18n_en_2_general_tab.png"))

        report["cloud_en"] = cdp.eval(OPEN_LLM_CLOUD, await_promise=True)
        time.sleep(0.5)
        report["shots"].append(shot(cdp, "i18n_en_3_cloud_tab.png"))

        cdp.eval(CLOSE_ALL)
        time.sleep(0.5)
        report["native_en"] = cdp.eval(READ_NATIVE_WIDGETS, await_promise=True)
        report["locale_state_en"] = cdp.eval(READ_LOCALE_STATE, await_promise=True)
        report["shots"].append(shot(cdp, "i18n_en_1_node_face.png"))

        # LLM 节点正面单独一张（居中放大视野，看 provider/模型小字与「仅模板」标签）
        report["focus_llm"] = cdp.eval(FOCUS_LLM, await_promise=True)
        time.sleep(0.4)
        report["shots"].append(shot(cdp, "i18n_en_5_llm_face.png"))

        # ---------- 3) en 下点「重写提示词」
        report["rewrite_en"] = cdp.eval(CLICK_REWRITE, await_promise=True)
        time.sleep(0.4)
        report["shots"].append(shot(cdp, "i18n_en_4_rewrite.png"))

        # ---------- 4) auto + ComfyUI 英文：lang 与文案必须同为 en
        report["auto_english_sim"] = cdp.eval(AUTO_WITH_ENGLISH_COMFY, await_promise=True)
        time.sleep(0.4)
        report["locale_state_auto_en"] = cdp.eval(READ_LOCALE_STATE, await_promise=True)
        report["shots"].append(shot(cdp, "i18n_en_6_auto_with_english_comfy.png"))

        # ---------- 5) 复位（后端 auto + reload）
        cdp.eval(CLOSE_ALL)
        report["reset"] = set_locale_backend(cdp, "auto")
        reload_app(cdp, port)
        cdp.eval(CREATE_NODES, await_promise=True)
        time.sleep(0.5)
        report["native_zh"] = cdp.eval(READ_NATIVE_WIDGETS, await_promise=True)
        report["locale_state_auto"] = cdp.eval(READ_LOCALE_STATE, await_promise=True)

        # ---------- 5) 派生结论（供报告引用）
        faces_zh = report.get("faces_zh") or {}
        faces_en = report.get("faces_en") or {}
        zh_head = str((faces_zh.get("composer") or {}).get("anima_nl_head", ""))
        en_head = str((faces_en.get("composer") or {}).get("anima_nl_head", ""))
        report["verdict"] = {
            "zh_head": zh_head,
            "en_head": en_head,
            "immediate_switch": bool(zh_head and en_head and zh_head != en_head),
            "combo_untouched": report.get("faces_en", {}).get("llmModelType") == "云端模型"
                               and "本地模型" in (report.get("faces_en", {}).get("llmModelTypeOptions") or []),
            "rewrite_works_in_en": bool((report.get("rewrite_en") or {}).get("ok")),
            "rewrite_status": (report.get("rewrite_en") or {}).get("status", ""),
            "rewrite_status_is_english": not any(
                "\u4e00" <= ch <= "\u9fff" for ch in str((report.get("rewrite_en") or {}).get("status", ""))
            ),
            "html_lang_en": (report.get("locale_state_en") or {}).get("htmlLang"),
            "html_lang_auto": (report.get("locale_state_auto") or {}).get("htmlLang"),
            "html_lang_auto_en": (report.get("locale_state_auto_en") or {}).get("htmlLang"),
            "lang_matches_text_auto_en": (report.get("locale_state_auto_en") or {}).get("consistent"),
            "lang_matches_text_en": (report.get("locale_state_en") or {}).get("consistent"),
            "lang_matches_text_auto": (report.get("locale_state_auto") or {}).get("consistent"),
            "use_llm_label_en": ((report.get("native_en") or {}).get("composer") or {}).get("use_llm", {}),
            "use_llm_label_zh": ((report.get("native_zh") or {}).get("composer") or {}).get("use_llm", {}),
            "split_nodes_tooltips_en": {
                node: {name: (info or {}).get("tooltip", "") for name, info in (widgets or {}).items()}
                for node, widgets in ((report.get("native_en") or {}).get("splitNodes") or {}).items()
                if widgets
            },
        }
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
