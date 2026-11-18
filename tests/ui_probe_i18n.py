# -*- coding: utf-8 -*-
"""C6 双语 CDP 探针：验证 zh / en / auto 三种语言下节点文案真的切换。

契约依据（plugin/web/i18n.js）：
    * localStorage 键是 ``nl2prompt.ui_locale``（小写）；
    * ``initI18n()`` 在**页面加载时**先读 localStorage、再拉 ``GET /nl2prompt/settings_ui``；
    * ``setLocale()`` 会给 ``<html lang>`` 打标记（``applyLocaleToDocument``）。
因此「改 locale → 必须刷新页面」才能观测到真实生效结果，本探针按此流程执行。

采集内容（每 locale 一份）：
    * ``lang``：``document.documentElement.lang``（最直接的生效标记）
    * ``i18n_sample``：动态 import i18n 模块后直接调 ``t()`` 取到的 zh/en 文案样本
    * ``composer_texts``：节点正面 DOM 上的真实文案（widget label / innerText / 按钮）
    * ``widget_names`` / ``widget_count`` / ``node_title``

产物：``tests/_ui_probe_i18n.json``，供
``tests/test_v3.py --only C6 --ui-probe-report ...`` 断言。

用法：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\ui_probe_i18n.py
    ... --url http://127.0.0.1:8199 --port 9343
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

LOCALES = ("zh", "en", "auto")

# 采集脚本：假定页面已经是目标 locale（由外层先 set + reload）
COLLECT = r"""
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const out = {};

    // 1) i18n 模块直采：从已加载的资源 URL 反推 i18n.js（避免硬编码扩展目录名）
    out.i18n_sample = null;
    try {
        const res = performance.getEntriesByType('resource').map(e => e.name);
        const composerUrl = res.find(n => n.indexOf('nl2prompt_composer.js') >= 0);
        if (composerUrl) {
            const i18nUrl = composerUrl.replace('nl2prompt_composer.js', 'i18n.js');
            const m = await import(i18nUrl);
            out.i18n_sample = {
                i18n_url: i18nUrl,
                current_locale: (m.currentLocale && m.currentLocale()) || '',
                mode: (m.currentMode && m.currentMode()) || '',
                t_head_tooltip: (m.t && m.t('composer.head_tooltip')) || '',
                t_window_title: (m.t && m.t('composer.window.title')) || '',
                complete: (m.assertLocalesComplete && m.assertLocalesComplete()) || null,
            };
        }
    } catch (e) { out.i18n_sample = {error: String(e)}; }

    // 2) 建一个 Composer 节点，采真实文案
    try { app.graph.clear(); } catch (e) {}
    await sleep(300);
    const c = LiteGraph.createNode('NL2PromptComposer');
    c.pos = [140, 140];
    app.graph.add(c);
    app.graph.setDirtyCanvas(true, true);
    await sleep(1500);

    const texts = [];
    for (const w of c.widgets || []) {
        if (w.name === '_ui_json') continue;
        const label = (w.label || '').trim();
        if (label) texts.push(label);
        if (w.element && w.element.innerText) {
            const t = w.element.innerText.replace(/\s+/g, ' ').trim();
            if (t) texts.push(t.slice(0, 160));
        }
    }
    try {
        const btns = [...document.querySelectorAll('.anima-nl-root button')]
            .map(b => (b.textContent || '').trim()).filter(Boolean);
        texts.push(...btns.slice(0, 40));
    } catch (e) {}

    out.lang = (document.documentElement && document.documentElement.lang) || '';
    out.composer_texts = [...new Set(texts)];
    out.widget_names = (c.widgets || []).map(w => w.name);
    out.widget_count = (c.widgets || []).length;
    out.node_title = c.title || '';
    return out;
})()
"""


def set_locale(cdp: "CDP", locale: str) -> dict:
    """把 locale 写进 localStorage 与后端 settings，返回通道结果。"""
    js = (
        "(() => {"
        " const out = {};"
        f" try {{ localStorage.setItem('nl2prompt.ui_locale', '{locale}'); out.local_storage = true; }}"
        " catch (e) { out.local_storage = String(e); }"
        " fetch('/nl2prompt/settings_ui', {method: 'POST',"
        "   headers: {'Content-Type': 'application/json'},"
        f"  body: JSON.stringify({{ui_locale: '{locale}'}})}})"
        "   .then(r => { out.settings_ui = r.status; })"
        "   .catch(e => { out.settings_ui = String(e); });"
        " return out;"
        "})()"
    )
    return cdp.eval(js, await_promise=False) or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8199")
    parser.add_argument("--port", type=int, default=9343)
    parser.add_argument("--out", default=str(HERE / "_ui_probe_i18n.json"))
    args = parser.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe_i18n_profile")
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
        if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True,
                       120, "app"):
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2
        cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, "
                 "[role=\"dialog\"]').forEach(e => e.remove()), 1")

        ui: dict = {"channels_used": {}, "locale_text_counts": {}}
        for locale in LOCALES:
            ui["channels_used"][locale] = set_locale(cdp, locale)
            # 关键：i18n 只在页面加载时初始化 → 必须 reload 才能生效
            cdp.call("Page.reload", {"ignoreCache": False})
            if not wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)",
                           True, 120, f"app@{locale}"):
                ui[locale] = {"error": "app not ready after reload"}
                continue
            time.sleep(0.6)
            cdp.eval("document.querySelectorAll('.p-dialog-mask, .comfy-modal, "
                     "[role=\"dialog\"]').forEach(e => e.remove()), 1")
            data = cdp.eval(COLLECT, await_promise=True) or {}
            ui[locale] = data
            ui["locale_text_counts"][locale] = len(data.get("composer_texts") or [])

        report["ui"] = ui
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
            Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
            print(f"报告已写入: {args.out}")
        except OSError as exc:
            print(f"报告写入失败: {exc}")


if __name__ == "__main__":
    sys.exit(main())
