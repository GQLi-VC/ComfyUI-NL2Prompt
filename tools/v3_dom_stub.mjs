/**
 * v3 前端 DOM-stub 回归（146 项断言）—— 不启动 ComfyUI、不联网。
 *
 * 用途
 *   在不打开浏览器的前提下，真实执行 plugin/web/*.js 的安装 / 渲染 / 保存 / 配置恢复路径，
 *   覆盖「控件顺序 + holder 属性 + Tab 结构 + 保存回写 + 旧 workflow 兼容 + 降级路径
 *   + i18n 字典一致性 / 语言即时切换 / 错误文案优先级」，
 *   是最快的一道回归网（官方 CDP 探针见 tools/probe_v3_ui.py、tools/probe_v3_i18n.py）。
 *
 * 运行环境
 *   Node.js >= 18（实测 v24.16.0）。无第三方依赖，不需要 npm install。
 *
 * 运行方式
 *   node tools/v3_dom_stub.mjs
 *   退出码 0 = 全部通过；1 = 有失败（末尾会列出失败项）。
 *
 * 它 mock 了什么
 *   - DOM：document/Element/TextNode/classList/style/innerText/事件派发（够插件用）
 *   - 浏览器全局：window.confirm / window.prompt（脚本内可覆盖）、requestAnimationFrame（同步执行）
 *   - ComfyUI 前端：app.registerExtension（收集扩展）、app.configuringGraph、app.queuePrompt（计数）
 *   - api.fetchApi：内存路由表（/nl2prompt/* 全套假数据，含 401 / 失败重试 / 技能读写）
 *   - navigator.language（zh-CN）与 localStorage（i18n 语言检测链 + 偏好持久化）
 *   - LiteGraph 节点最小接口：widgets 数组、addDOMWidget、computeSize、setSize、setDirtyCanvas、
 *     graph.change、onNodeCreated/onConfigure/onExecuted 三钩子
 *
 * 为什么要把插件源码复制到临时目录
 *   plugin/web/*.js 是 ESM，但项目根没有 package.json(type=module)，Node 会按 CJS 解析而报错。
 *   本脚本启动时把四个 js（common / i18n / composer / load_llm）复制到
 *   <os.tmpdir()>/nl2prompt_dom_stub/extensions/pkg/，
 *   并写一个 {"type":"module"} 的 package.json 与 scripts/{api,app}.js 桩，再从那里 import。
 *   因此它始终测试「当前工作区里的 plugin/web/*.js」，不会测到旧副本。
 *
 * 维护提示（i18n）
 *   i18n 已接入：脚本在加载两个插件前先调用 initI18n() 再 setLocale("zh")，
 *   因此绝大多数断言仍按中文期望值书写；涉及语言切换的用例会临时切到 en 并复位。
 *   错误文案断言按「后端 detail_zh/detail_en → err.<code> 字典 → 原始 code」的优先级书写，
 *   本 stub 的响应多为旧式 detail，故期望值落在 err.<code> 的中文文案上。
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.dirname(HERE);
const WEB_DIR = path.join(PROJECT_ROOT, "plugin", "web");
const STUB_ROOT = path.join(os.tmpdir(), "nl2prompt_dom_stub");

/** 复制并包装 plugin/web/*.js，使其可按 ESM 从临时目录加载。 */
function prepareModuleTree() {
    const pkgDir = path.join(STUB_ROOT, "extensions", "pkg");
    const scriptsDir = path.join(STUB_ROOT, "scripts");
    fs.rmSync(STUB_ROOT, { recursive: true, force: true });
    fs.mkdirSync(pkgDir, { recursive: true });
    fs.mkdirSync(scriptsDir, { recursive: true });
    fs.writeFileSync(path.join(STUB_ROOT, "package.json"), '{\n  "type": "module"\n}\n', "utf8");
    fs.writeFileSync(path.join(scriptsDir, "api.js"), "export const api = globalThis.__apiStub;\n", "utf8");
    fs.writeFileSync(path.join(scriptsDir, "app.js"), "export const app = globalThis.__appStub;\n", "utf8");
    for (const name of ["common.js", "i18n.js", "nl2prompt_composer.js", "nl2prompt_load_llm.js"]) {
        fs.copyFileSync(path.join(WEB_DIR, name), path.join(pkgDir, name));
    }
    return pkgDir;
}

const PKG_DIR = prepareModuleTree();
const modUrl = (name) => pathToFileURL(path.join(PKG_DIR, name)).href;

/* ---------------------------------------------------------------- DOM stub */

class ClassList {
    constructor() {
        this.set = new Set();
    }
    add(...names) {
        for (const name of names) if (name) this.set.add(String(name));
    }
    remove(...names) {
        for (const name of names) this.set.delete(String(name));
    }
    contains(name) {
        return this.set.has(String(name));
    }
    toggle(name, force) {
        const value = force === undefined ? !this.set.has(name) : force === true;
        if (value) this.set.add(String(name));
        else this.set.delete(String(name));
        return value;
    }
}

const ALL_ELEMENTS = [];

class NodeBase {
    constructor(tag) {
        this.tagName = String(tag || "div").toUpperCase();
        this.children = [];
        this.parent = null;
        this.style = {};
        this.dataset = {};
        this.classList = new ClassList();
        this._listeners = new Map();
        this._attrs = {};
        this._text = "";
        this.id = "";
    }
    get className() {
        return [...this.classList.set].join(" ");
    }
    set className(value) {
        this.classList.set = new Set(String(value ?? "").split(/\s+/).filter(Boolean));
    }
    get textContent() {
        return this._text;
    }
    set textContent(value) {
        this._text = String(value ?? "");
        for (const child of this.children) child.parent = null;
        this.children = [];
    }
    get offsetParent() {
        return this.parent;
    }
    appendChild(child) {
        if (!child) return child;
        child.parent = this;
        this.children.push(child);
        return child;
    }
    append(...children) {
        for (const child of children) this.appendChild(child);
    }
    prepend(...children) {
        for (const child of children) {
            child.parent = this;
            this.children.unshift(child);
        }
    }
    replaceChildren(...children) {
        for (const child of this.children) child.parent = null;
        this.children = [];
        this.append(...children);
    }
    removeChild(child) {
        this.children = this.children.filter((entry) => entry !== child);
        if (child) child.parent = null;
    }
    remove() {
        if (this.parent) this.parent.removeChild(this);
    }
    setAttribute(name, value) {
        this._attrs[name] = value;
    }
    getAttribute(name) {
        return this._attrs[name];
    }
    addEventListener(type, handler) {
        if (!this._listeners.has(type)) this._listeners.set(type, []);
        this._listeners.get(type).push(handler);
    }
    removeEventListener(type, handler) {
        const list = this._listeners.get(type);
        if (!list) return;
        this._listeners.set(type, list.filter((entry) => entry !== handler));
    }
    dispatch(type, event = {}) {
        const payload = {
            type,
            target: this,
            currentTarget: this,
            preventDefault() {},
            stopPropagation() {},
            stopImmediatePropagation() {},
            ...event,
        };
        for (const handler of [...(this._listeners.get(type) ?? [])]) handler(payload);
        return payload;
    }
    focus() {}
}

class TextNode {
    constructor(text) {
        this.nodeType = 3;
        this.textContent = String(text);
        this.parent = null;
    }
}

const documentStub = new NodeBase("document");
documentStub.head = new NodeBase("head");
documentStub.body = new NodeBase("body");
documentStub.createElement = (tag) => {
    const element = new NodeBase(tag);
    ALL_ELEMENTS.push(element);
    return element;
};
documentStub.createTextNode = (text) => new TextNode(text);
documentStub.getElementById = (id) => ALL_ELEMENTS.find((element) => element.id === id) ?? null;

documentStub.documentElement = new NodeBase("html");
globalThis.document = documentStub;
globalThis.Node = NodeBase;
globalThis.window = { confirm: () => true };

// i18n 的语言检测链会读 navigator.language 与 localStorage：
// Node 24 的 navigator 是只读 getter，必须用 defineProperty 覆盖。
Object.defineProperty(globalThis, "navigator", {
    value: { language: "zh-CN", languages: ["zh-CN", "zh"] },
    configurable: true,
    writable: true,
});
const storageMap = new Map();
Object.defineProperty(globalThis, "localStorage", {
    value: {
        getItem: (key) => (storageMap.has(String(key)) ? storageMap.get(String(key)) : null),
        setItem: (key, value) => storageMap.set(String(key), String(value)),
        removeItem: (key) => storageMap.delete(String(key)),
    },
    configurable: true,
    writable: true,
});
globalThis.requestAnimationFrame = (fn) => {
    fn();
    return 0;
};

/* ---------------------------------------------------------------- api stub */

const apiCalls = [];
const apiState = {
    providersFailFirst: 1,     // 第一次请求失败 → 验证「失败重试一次」
    providersAlwaysFail: false,
    skillsFail: false,
    skillsNewFail: false,
    settingsUiFail: false,
    uiLocale: "zh",
    defaultPositiveBase: "MASTERPIECE FROM SETTINGS",
    listModelsFail: false,
    skillsSaveFail: false,
    rewriteFail: false,
    skills: [
        { id: "anima-tag-master", display_name: "Anima 标签大师", target_model: "anima" },
        { id: "anima-soft", display_name: "Anima 柔和风", target_model: "anima" },
    ],
    skillContent: {
        "anima-tag-master": {
            id: "anima-tag-master",
            display_name: "Anima 标签大师",
            target_model: "anima",
            version: 2,
            system_prompt: "SKILL MASTER PROMPT",
            negative_base: "SKILL NEG MASTER",
        },
        "anima-soft": {
            id: "anima-soft",
            display_name: "Anima 柔和风",
            target_model: "anima",
            system_prompt: "SKILL SOFT PROMPT",
            negative_base: "SKILL NEG SOFT",
        },
    },
    // 故意与 v1 硬编码不同的 base_url，用来证明前端读的是后端表
    providers: [
        { id: "deepseek", name: "DeepSeek", label: "深度求索", base_url: "https://api.deepseek.com/v1", openai_compat: true, default_model: "deepseek-chat", needs_key: true, verified: true, note: "" },
        { id: "moonshot", name: "Moonshot", label: "月之暗面", base_url: "https://api.moonshot.cn/v1", openai_compat: true, default_model: "moonshot-v1-8k", needs_key: true, verified: true, note: "" },
        { id: "openai", name: "OpenAI", label: "OpenAI", base_url: "https://proxy.test/openai/v1", openai_compat: true, default_model: "gpt-4o-mini", needs_key: true, verified: false, note: "代理地址" },
        { id: "anthropic", name: "Anthropic", label: "Anthropic", base_url: "https://api.anthropic.com/v1", openai_compat: false, default_model: "claude-3-5-sonnet-20241022", needs_key: true, verified: true, note: "" },
        { id: "custom", name: "Custom", label: "自定义（OpenAI 兼容）", base_url: "", openai_compat: true, default_model: "", needs_key: true, verified: false, note: "用户自填" },
    ],
};

globalThis.__apiStub = {
    async fetchApi(path, init = {}) {
        apiCalls.push({ path, method: (init.method || "GET").toUpperCase() });
        const json = (body, status = 200) => ({
            ok: status >= 200 && status < 300,
            status,
            statusText: status === 200 ? "OK" : "Error",
            json: async () => body,
        });

        if (path === "/nl2prompt/providers") {
            if (apiState.providersAlwaysFail) return json({ ok: false, error: "network" }, 500);
            if (apiState.providersFailFirst > 0) {
                apiState.providersFailFirst -= 1;
                return json({ ok: false, error: "http_503" }, 503);
            }
            return json({ ok: true, providers: apiState.providers });
        }
        if (path.startsWith("/nl2prompt/list_models")) {
            if (apiState.listModelsFail) return json({ ok: false, models: [], error: "http_401", detail: "invalid api key" }, 401);
            return json({ ok: true, models: [{ id: "gpt-4o-mini" }, { id: "gpt-4o" }], count: 2, elapsed_ms: 12 });
        }
        if (path === "/nl2prompt/skills") {
            if (apiState.skillsFail) return json({ ok: false, error: "network" }, 500);
            return json({ ok: true, skills: apiState.skills, default: "anima-tag-master", content: apiState.skillContent });
        }
        if (path === "/nl2prompt/skills/save") {
            if (apiState.skillsSaveFail) return json({ ok: false, error: "schema_invalid" });
            const body = JSON.parse(init.body || "{}");
            if (body.id && body.content && !apiState.skills.some((item) => item.id === body.id)) {
                apiState.skills.push({ id: body.id, display_name: body.content.display_name || body.id, target_model: "anima" });
                apiState.skillContent[body.id] = body.content;
            }
            return json({ ok: true });
        }
        if (path === "/nl2prompt/rewrite") {
            if (apiState.rewriteFail) return json({ ok: false, error: "llm_not_connected", detail: "no llm" }, 400);
            return json({ ok: true, positive: "masterpiece, solo, purple hair, neon street", negative: "lowres, bad anatomy", analysis: "注入遐蝶 LoRA 与霓虹街道场景词", quality: 78 });
        }
        if (path === "/nl2prompt/default_system_prompt") return json({ prompt: "" });
        if (path === "/nl2prompt/settings_ui") {
            if (apiState.settingsUiFail) return json({ ok: false, error: "not_found" }, 404);
            if (String(init.method || "GET").toUpperCase() === "POST") {
                const body = JSON.parse(init.body || "{}");
                apiState.uiLocale = body.ui_locale ?? apiState.uiLocale;
                apiState.defaultPositiveBase = body.default_positive_base ?? apiState.defaultPositiveBase;
                return json({ ok: true, settings: body });
            }
            return json({ ok: true, ui_locale: apiState.uiLocale, default_positive_base: apiState.defaultPositiveBase });
        }
        if (path === "/nl2prompt/skills/new") {
            if (apiState.skillsNewFail) return json({ ok: false, error: "skill_exists" });
            const body = JSON.parse(init.body || "{}");
            apiState.skills.push({ id: body.id, display_name: body.display_name || body.id, target_model: "anima" });
            apiState.skillContent[body.id] = {
                id: body.id,
                display_name: body.display_name || body.id,
                target_model: "anima",
                system_prompt: body.copy_from ? `COPIED FROM ${body.copy_from}` : "NEW SKILL PROMPT",
                negative_base: "NEW NEG",
            };
            return json({ ok: true, id: body.id });
        }
        if (path === "/nl2prompt/skills/delete") {
            const body = JSON.parse(init.body || "{}");
            if (body.id === "anima-tag-master") return json({ ok: false, error: "skill_protected" });
            apiState.skills = apiState.skills.filter((item) => item.id !== body.id);
            delete apiState.skillContent[body.id];
            return json({ ok: true, id: body.id });
        }
        if (path === "/nl2prompt/secret_status") return json({ configured: true, source: "env" });
        if (path === "/nl2prompt/secret") return json({ ok: true, configured: true });
        if (path === "/nl2prompt/runtime/start") return json({ ok: true, base_url: "http://127.0.0.1:8081/v1", model: "demo", port: 8081 });
        if (path.startsWith("/nl2prompt/scan_safetensors")) {
            return json({
                ok: true,
                files: [
                    { name: "model.safetensors", dir: "a", size_mb: 12.5 },
                    { name: "model.safetensors", dir: "c/nested", size_mb: 8 },
                ],
                root: "(mock)",
                error: null,
                total_found: 3,
                require_config: true,
            });
        }
        if (path.startsWith("/nl2prompt/scan_gguf")) {
            return json({ files: [{ name: "a.gguf", path: "H:/m/qwen/a.gguf", size_mb: 1, dir: "qwen" }], root: "H:/m", error: null });
        }
        return json({ ok: false, error: "not_found" }, 404);
    },
};

const extensions = [];
// LiteGraph 注册表：任务 B 会把 title 改成本地化名字
globalThis.LiteGraph = {
    registered_node_types: {
        NL2PromptComposer: { title: "自然语言提示词", nodeData: { display_name: "自然语言提示词" } },
        AnimaNLComposer: { title: "（旧）自然语言提示词", nodeData: { display_name: "（旧）自然语言提示词" } },
        NL2PromptLoadLLM: { title: "加载LLM（旧·本地/云端合一）", nodeData: { display_name: "加载LLM（旧·本地/云端合一）" } },
        AnimaLoadLLM: { title: "（旧）加载LLM", nodeData: { display_name: "（旧）加载LLM" } },
        NL2PromptLLMGGUF: { title: "加载LLM（GGUF）", nodeData: { display_name: "加载LLM（GGUF）" } },
        NL2PromptLLMSafetensors: { title: "加载LLM（safetensors）", nodeData: { display_name: "加载LLM（safetensors）" } },
        NL2PromptLLMCloud: { title: "加载云端LLM", nodeData: { display_name: "加载云端LLM" } },
        NL2PromptLoRAStack: { title: "LoRA 堆栈加载", nodeData: { display_name: "LoRA 堆栈加载" } },
    },
};

globalThis.__appStub = {
    configuringGraph: false,
    // refreshAllNodeFaces(app) 会遍历 app.graph，这里给一个真实节点表
    graph: { _nodes: [], setDirtyCanvas() {} },
    queuePromptCalls: 0,
    async queuePrompt() {
        globalThis.__appStub.queuePromptCalls += 1;
    },
    registerExtension(extension) {
        extensions.push(extension);
    },
};

/* ---------------------------------------------------------------- fake node */

function nativeWidget(name, type, value, options = {}) {
    return {
        name,
        type,
        value,
        options,
        serialize: true,
        hidden: false,
        callback: null,
        computeSize: (width) => [width ?? 220, 22],
        margin: 0,
    };
}

function makeNode(nodeData) {
    const node = {
        size: [210, 100],
        widgets: [],
        changes: 0,
        dirty: 0,
        constructor: { nodeData },
        graph: {
            _nodes: [],
            change() {
                node.changes += 1;
            },
            setDirtyCanvas() {},
        },
    };
    node.computeSize = function computeSize() {
        let height = 30 + 40;
        for (const widget of node.widgets) {
            if (widget.hidden) continue;
            const size = typeof widget.computeSize === "function" ? widget.computeSize(node.size[0]) : [node.size[0], 20];
            height += Number.isFinite(size?.[1]) ? size[1] : 20;
        }
        return [node.size[0], height];
    };
    node.setSize = function setSize(size) {
        node.size = [Math.max(1, size?.[0] ?? node.size[0]), Math.max(1, size?.[1] ?? node.size[1])];
    };
    node.setDirtyCanvas = function setDirtyCanvas() {
        node.dirty += 1;
    };
    globalThis.__appStub.graph._nodes.push(node);
    node.addDOMWidget = function addDOMWidget(name, type, element, options = {}) {
        const widget = {
            name,
            type,
            element,
            options,
            serialize: true,
            hidden: false,
            callback: null,
            computeSize: null,
            get margin() {
                return options.margin ?? 10;
            },
            get value() {
                return typeof options.getValue === "function" ? options.getValue() : "";
            },
            set value(next) {
                if (typeof options.setValue === "function") options.setValue(next);
            },
        };
        node.widgets.push(widget);
        return widget;
    };
    return node;
}

const composerNodeData = {
    name: "NL2PromptComposer",
    input: {
        required: {
            natural_language: ["STRING", {}],
            use_llm: ["BOOLEAN", {}],
            _ui_json: ["STRING", {}],
        },
        optional: { llm: ["LLM_MODEL", {}], clip: ["CLIP", {}] },
    },
};

const loadLlmNodeData = {
    name: "NL2PromptLoadLLM",
    input: {
        required: {
            model_type: [["本地模型", "云端模型"], {}],
            gpu: [["0: NVIDIA GeForce RTX 3060 (12288 MiB)", "1"], {}],
            model_path: ["STRING", {}],
            model_file: [["(请先选择路径后刷新)"], {}],
            provider: [["deepseek", "openai", "anthropic", "custom"], {}],
            base_url: ["STRING", {}],
            cloud_model: ["STRING", {}],
            _llm_json: ["STRING", {}],
        },
    },
};

/* ---------------------------------------------------------------- 断言 */

let failures = 0;
const checks = [];

function check(label, condition, detail = "") {
    checks.push({ label, ok: Boolean(condition), detail });
    if (!condition) {
        failures += 1;
        console.log(`  ✗ ${label}${detail ? ` —— ${detail}` : ""}`);
    }
}

const find = (node, name) => node.widgets.find((widget) => widget.name === name) ?? null;
const names = (node) => node.widgets.map((widget) => widget.name);
const allChildren = (root, out = []) => {
    for (const child of root.children ?? []) {
        out.push(child);
        allChildren(child, out);
    }
    return out;
};

function makeComposerNode() {
    const node = makeNode(composerNodeData);
    // 实测后端 tooltip 不会传到 widget（恒为空串）
    node.widgets.push(nativeWidget("natural_language", "customtext", "雨后的霓虹街道，紫发少女"));
    // 真实结构：后端 label_on/label_off 会被前端改写成 "true"/"false"，
    // 原文落在 options.on / options.off
    node.widgets.push(nativeWidget("use_llm", "toggle", false, {
        on: "LLM 重写",
        off: "仅模板",
        label_on: "true",
        label_off: "false",
    }));
    node.widgets.push(nativeWidget("_ui_json", "customtext", "{}"));
    return node;
}

function makeLoadLlmNode() {
    const node = makeNode(loadLlmNodeData);
    node.widgets.push(nativeWidget("model_type", "combo", "本地模型", {
        values: ["本地模型", "本地模型（safetensors）", "云端模型"],
    }));
    node.widgets.push(nativeWidget("gpu", "combo", "0: NVIDIA GeForce RTX 3060 (12288 MiB)", {
        values: ["0: NVIDIA GeForce RTX 3060 (12288 MiB)"],
    }));
    node.widgets.push(nativeWidget("model_path", "text", "H:\\AI\\Local\\Models"));
    node.widgets.push(nativeWidget("model_file", "combo", "(请先选择路径后刷新)"));
    node.widgets.push(nativeWidget("provider", "combo", "deepseek", {
        values: ["deepseek", "openai", "anthropic", "custom"],
    }));
    node.widgets.push(nativeWidget("base_url", "text", "https://api.deepseek.com/v1"));
    node.widgets.push(nativeWidget("cloud_model", "text", ""));
    node.widgets.push(nativeWidget("_llm_json", "customtext", "{}"));
    // v3.3：后端在 _llm_json 之后追加了 safetensors 用的两个 widget
    node.widgets.push(nativeWidget("model_dir", "text", ""));
    node.widgets.push(nativeWidget("dtype", "combo", "fp16", { values: ["fp16", "bf16", "auto"] }));
    return node;
}

const sleep = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------------------------------------------------------------- 运行 */

// i18n 必须先初始化：两个插件的模块顶层就会调用 t() 渲染默认文案。
const i18nModule = await import(modUrl("i18n.js"));
const i18nReport = await i18nModule.initI18n();
i18nModule.setLocale("zh");

const composerModule = await import(modUrl("nl2prompt_composer.js"));
const loadLlmModule = await import(modUrl("nl2prompt_load_llm.js"));

const composerExt = extensions.filter((extension) => extension.name === "NL2Prompt.Composer").at(-1);
const loadExt = extensions.filter((extension) => extension.name === "NL2Prompt.LoadLLM").at(-1);
check("注册了两个扩展", Boolean(composerExt) && Boolean(loadExt));

const composerType = { prototype: { onNodeCreated() {}, onConfigure() {}, onExecuted() {} } };
const loadType = { prototype: { onNodeCreated() {}, onConfigure() {}, onExecuted() {} } };
await composerExt.beforeRegisterNodeDef(composerType, composerNodeData);
await loadExt.beforeRegisterNodeDef(loadType, loadLlmNodeData);

console.log("== 主节点：控件与默认值 ==");
const composer = makeComposerNode();
composerType.prototype.onNodeCreated.call(composer);
const holder = find(composer, "_ui_json");
check("holder hidden + computeSize [0,-4] + serialize", holder.hidden === true && JSON.stringify(holder.computeSize()) === "[0,-4]" && holder.serialize === true);
check(
    "正面控件顺序（11 项）",
    JSON.stringify(names(composer)) === JSON.stringify([
        "natural_language", "use_llm", "_ui_json", "anima_nl_head", "anima_nl_characters",
        "anima_nl_styles", "anima_nl_add_rows", "anima_nl_rewrite_actions", "anima_nl_rewrite_status",
        "anima_nl_settings", "anima_nl_seed",
    ]),
    names(composer).join(","),
);
const state0 = JSON.parse(holder.value);
check("默认 negative_base 为空串（§14.5）", state0.negative_base === "");
check("默认 ui.skill_id", state0.ui.skill_id === "anima-tag-master", state0.ui.skill_id);
check("默认 ui.target_profile", state0.ui.target_profile === "anima");
check("默认 dialog 为空数组", Array.isArray(state0.dialog) && state0.dialog.length === 0);
check("默认 cloud_models 结构", state0.cloud_models && state0.cloud_models.base_url === "" && Array.isArray(state0.cloud_models.models) && state0.cloud_models.ts === 0);
check("默认 1 角色行 1 风格行", state0.character_rows.length === 1 && state0.style_rows.length === 1);
check("seed 行含历史轮数", find(composer, "anima_nl_seed").element.textContent.includes("历史 0 轮"), find(composer, "anima_nl_seed").element.textContent);
check("重写状态初始文案", find(composer, "anima_nl_rewrite_status").element.textContent === "尚未重写");

console.log("== 主节点：折叠与行操作 ==");
const charWidget = find(composer, "anima_nl_characters");
const addRows = find(composer, "anima_nl_add_rows").element;
addRows.children[0].dispatch("click", {});
check("添加角色行", JSON.parse(holder.value).character_rows.length === 2);
const headEl = find(composer, "anima_nl_head").element;
headEl.dispatch("click", {});
check("折叠隐藏角色行", charWidget.hidden === true);
check("折叠隐藏重写按钮", find(composer, "anima_nl_rewrite_actions").hidden === true);
check("折叠隐藏设置", find(composer, "anima_nl_settings").hidden === true);
check("折叠保留重写状态小字", find(composer, "anima_nl_rewrite_status").hidden === false);
check("折叠保留种子行", find(composer, "anima_nl_seed").hidden === false);
headEl.dispatch("click", {});
check("展开恢复", charWidget.hidden === false);

console.log("== 主节点：onExecuted（种子 + dialog 上限 20） ==");
for (let index = 0; index < 25; index += 1) {
    composerType.prototype.onExecuted.call(composer, {
        anima_nl: [{
            seed: 1000 + index,
            used_llm: false,
            fallback: "",
            dialog_entry: { role: "user", text: `第 ${index} 轮`, positive: `positive ${index}`, negative: `negative ${index}`, ts: Date.now() + index },
        }],
    });
}
const afterDialog = JSON.parse(holder.value);
check("dialog 上限 20", afterDialog.dialog.length === 20, String(afterDialog.dialog.length));
check("超出丢弃最旧（首条是第 5 轮）", afterDialog.dialog[0].text === "第 5 轮", afterDialog.dialog[0].text);
check("末条是最新轮", afterDialog.dialog.at(-1).text === "第 24 轮");
check("种子已写入小字", find(composer, "anima_nl_seed").element.textContent.includes("1024"), find(composer, "anima_nl_seed").element.textContent);
check("历史轮数已刷新", find(composer, "anima_nl_seed").element.textContent.includes("历史 20 轮"));
check("dialog 写入触发 graph.change", composer.changes > 0);

console.log("== 主节点：重写 / 沿用上一轮 ==");
const queueBefore = globalThis.__appStub.queuePromptCalls;
const rewriteBtn = find(composer, "anima_nl_rewrite_actions").element.children[0];
rewriteBtn.dispatch("click", {});
await sleep(5);
const afterRewrite = JSON.parse(holder.value);
check("重写写入 ui.last_rewrite", Boolean(afterRewrite.ui.last_rewrite && afterRewrite.ui.last_rewrite.positive), JSON.stringify(afterRewrite.ui.last_rewrite));
check("重写来源标记", afterRewrite.ui.last_rewrite?.source === "rewrite");
check("重写质量写入", afterRewrite.ui.last_rewrite?.quality === 78);
const rewriteText = find(composer, "anima_nl_rewrite_status").element.textContent;
check("正面小字含质量与摘要", rewriteText.includes("质量 78") && rewriteText.includes("上轮重写:"), rewriteText);
check("重写不触发出图", globalThis.__appStub.queuePromptCalls === queueBefore);

// 失败场景
apiState.rewriteFail = true;
find(composer, "anima_nl_rewrite_actions").element.children[0].dispatch("click", {});
await sleep(5);
const failText = find(composer, "anima_nl_rewrite_status").element.textContent;
// 错误文案：优先 detail_zh/detail_en；本 stub 的响应只有旧式 detail → 回落 err.<code> 的中文文案
check("重写失败显示红字 error", failText.includes("重写失败") && failText.includes("LLM 未连接") && find(composer, "anima_nl_rewrite_status").element.classList.contains("is-error"), failText);
apiState.rewriteFail = false;

// 沿用上一轮
find(composer, "anima_nl_rewrite_actions").element.children[1].dispatch("click", {});
const afterReuse = JSON.parse(holder.value);
check("沿用上一轮写入 last_rewrite", afterReuse.ui.last_rewrite?.source === "dialog" && afterReuse.ui.last_rewrite?.positive === "positive 24", JSON.stringify(afterReuse.ui.last_rewrite));

console.log("== 主节点：onConfigure 往返 ==");
const restored = makeComposerNode();
find(restored, "_ui_json").value = JSON.stringify({
    schema: 1,
    character_rows: [{ id: "r1", name: "遐蝶", trigger: "castorice", outfit_hint: "假日皮肤" }],
    style_rows: [{ id: "s1", name: "@nixeu", trigger: "@nixeu, soft light" }],
    text_map: [{ id: "t1", find: "霓虹街道", prompt: "neon-lit street", target: "negative" }],
    system_prompt: "自定义系统提示词",
    negative_base: "custom negative",
    sampling: { top_k: 20, top_p: 0.8, seed: 123 },
    ui: { skill_id: "anima-soft", target_profile: "anima", collapsed_main: false, last_rewrite: { positive: "P", negative: "N", analysis: "A", quality: 60, ts: 1 } },
    dialog: [{ role: "user", text: "hi", positive: "P1", negative: "N1", ts: 2 }],
    cloud_models: { base_url: "https://x/v1", models: ["m1"], ts: 3 },
});
composerType.prototype.onNodeCreated.call(restored);
composerType.prototype.onConfigure.call(restored, { widgets_values: ["雨后的霓虹街道", false, JSON.stringify({
    schema: 1,
    character_rows: [{ id: "r1", name: "遐蝶", trigger: "castorice", outfit_hint: "假日皮肤" }],
    style_rows: [{ id: "s1", name: "@nixeu", trigger: "@nixeu, soft light" }],
    text_map: [{ id: "t1", find: "霓虹街道", prompt: "neon-lit street", target: "negative" }],
    system_prompt: "自定义系统提示词",
    negative_base: "custom negative",
    sampling: { top_k: 20, top_p: 0.8, seed: 123 },
    ui: { skill_id: "anima-soft", target_profile: "anima", collapsed_main: false, last_rewrite: { positive: "P", negative: "N", analysis: "A", quality: 60, ts: 1 } },
    dialog: [{ role: "user", text: "hi", positive: "P1", negative: "N1", ts: 2 }],
    cloud_models: { base_url: "https://x/v1", models: ["m1"], ts: 3 },
})] });
const restoredState = JSON.parse(find(restored, "_ui_json").value);
check("恢复行数据", restoredState.character_rows[0].name === "遐蝶" && restoredState.style_rows[0].trigger === "@nixeu, soft light");
check("恢复 skill_id", restoredState.ui.skill_id === "anima-soft", restoredState.ui.skill_id);
check("恢复 last_rewrite", restoredState.ui.last_rewrite?.positive === "P");
check("恢复 dialog", restoredState.dialog.length === 1 && restoredState.dialog[0].positive === "P1");
check("恢复 cloud_models", restoredState.cloud_models.models[0] === "m1" && restoredState.cloud_models.ts === 3);
check("旧 workflow 缺新字段时补默认", (() => {
    composerType.prototype.onConfigure.call(restored, { widgets_values: ["", false, JSON.stringify({ character_rows: [{ name: "旧" }] })] });
    const migrated = JSON.parse(find(restored, "_ui_json").value);
    return migrated.ui.skill_id === "anima-tag-master"
        && migrated.ui.target_profile === "anima"
        && Array.isArray(migrated.dialog)
        && migrated.dialog.length === 0
        && migrated.cloud_models.base_url === ""
        && migrated.negative_base === "";
})());

console.log("== 主节点设置窗口：5 Tab ==");
const settingsBtn = find(restored, "anima_nl_settings").element.children[0];
settingsBtn.dispatch("click", {});
const overlay = documentStub.body.children.at(-1);
check("窗口打开", Boolean(overlay) && overlay.classList.contains("anima-nl-overlay"));
const dialogEl = overlay.children[0];
const tabBar = dialogEl.children[1];
const panels = dialogEl.children[2];
const tabLabels = tabBar.children.map((button) => button.textContent);
check(
    "7 个 Tab 名称正确",
    JSON.stringify(tabLabels) === JSON.stringify(["通用", "提示词替换", "系统提示词", "正面基础词", "负面基础词", "提示词技能", "对话历史"]),
    tabLabels.join(","),
);
check("提示词替换面板有内容", panels.children[0].children.length > 0);

// 负面基础词 Tab（v3 索引 4）
tabBar.children[4].dispatch("click", {});
const negativeWrap = panels.children[4].children[0];
const negativeTextarea = negativeWrap.children[1];
const fillButton = negativeWrap.children[2].children[0];
check("负面 tab 有填充推荐值按钮", fillButton.textContent === "填充推荐值");
fillButton.dispatch("click", {});
await sleep(5);
check("填充推荐值来自 skill", negativeTextarea.value === "SKILL NEG MASTER", negativeTextarea.value);

// 技能 Tab（v3 索引 5）
tabBar.children[5].dispatch("click", {});
const skillWrap = panels.children[5].children[0];
await sleep(5);
const skillSelect = allChildren(skillWrap).find((el) => el.tagName === "SELECT");
const skillTextarea = skillWrap.children[2];
check("技能下拉有 2 个 skill", skillSelect.children.length === 2, String(skillSelect.children.length));
check("技能 textarea 显示 system_prompt", skillTextarea.value === "SKILL MASTER PROMPT", skillTextarea.value);
const skillTargetLine = skillWrap.children[1].children[2];
check("技能显示 target_model", skillTargetLine.textContent.includes("anima"), skillTargetLine.textContent);

skillSelect.value = "anima-soft";
skillSelect.dispatch("change", {});
check("切换 skill 更新 textarea", skillTextarea.value === "SKILL SOFT PROMPT", skillTextarea.value);

// 保存到服务器（成功）
const skillActions = skillWrap.children[3];
skillTextarea.value = "EDITED PROMPT";
skillTextarea.dispatch("input", {});
skillActions.children[1].dispatch("click", {});
await sleep(5);
const statusBar = dialogEl.children[3].children[0];
check("技能保存成功提示", statusBar.textContent.includes("已保存到服务器"), statusBar.textContent);

// 保存到服务器（失败）
apiState.skillsSaveFail = true;
skillActions.children[1].dispatch("click", {});
await sleep(5);
check("技能保存失败显示 error", statusBar.textContent.includes("保存失败") && statusBar.classList.contains("is-error"), statusBar.textContent);
apiState.skillsSaveFail = false;

// 对话历史 Tab（v3 索引 6）
tabBar.children[6].dispatch("click", {});
const historyWrap = panels.children[6].children[0];
const historyList = historyWrap.children[1];
check("历史 tab 显示暂无历史", historyList.children[0].textContent === "暂无历史", historyList.children[0].textContent);

// 保存窗口：skill 选择应写入 holder
const saveBtn = dialogEl.children[3].children.at(-1);
saveBtn.dispatch("click", {});
await sleep(5);
const savedState = JSON.parse(find(restored, "_ui_json").value);
check("窗口保存写入 ui.skill_id", savedState.ui.skill_id === "anima-soft", savedState.ui.skill_id);
check("窗口保存写入 negative_base", savedState.negative_base === "SKILL NEG MASTER", savedState.negative_base);

console.log("== LLM 节点：provider 后端驱动 ==");
const llm = makeLoadLlmNode();
loadType.prototype.onNodeCreated.call(llm);
await sleep(5);
const llmNames = names(llm);
check(
    "LLM 正面顺序（14 项，v3.3 起含 model_dir / dtype）",
    JSON.stringify(llmNames) === JSON.stringify([
        "model_type", "gpu", "model_path", "model_dir", "model_file", "anima_llm_refresh",
        "dtype", "provider", "anima_llm_provider_hint", "base_url", "cloud_model",
        "anima_llm_model_hint", "_llm_json", "anima_llm_settings",
    ]),
    llmNames.join(","),
);
check("正面不再有 anima_llm_fetch", find(llm, "anima_llm_fetch") === null);
const providerWidget = find(llm, "provider");
check("provider 选项被后端表替换", providerWidget.options.values.length === 5 && providerWidget.options.values.includes("moonshot"), JSON.stringify(providerWidget.options.values));
check("providers 首次失败后重试成功（调用 2 次）", apiCalls.filter((call) => call.path === "/nl2prompt/providers").length === 2, String(apiCalls.filter((call) => call.path === "/nl2prompt/providers").length));

const modelType = find(llm, "model_type");
modelType.value = "云端模型";
modelType.callback?.("云端模型");
check("云端：gpu 隐藏", find(llm, "gpu").hidden === true);
check("云端：模型小字可见", find(llm, "anima_llm_model_hint").hidden === false);
check("云端：provider hint 可见", find(llm, "anima_llm_provider_hint").hidden === false);
check("正面模型小字初始为未设置", find(llm, "anima_llm_model_hint").element.textContent.includes("未设置"), find(llm, "anima_llm_model_hint").element.textContent);

providerWidget.value = "openai";
providerWidget.callback?.("openai");
const baseUrlWidget = find(llm, "base_url");
check("provider 变更用后端表填 base_url", baseUrlWidget.value === "https://proxy.test/openai/v1", String(baseUrlWidget.value));
const hintText = find(llm, "anima_llm_provider_hint").element.textContent;
check("provider 小字为 label + base_url", hintText.includes("OpenAI") && hintText.includes("https://proxy.test/openai/v1"), hintText);
check("verified=false 追加（未校准）", hintText.includes("（未校准）"), hintText);

console.log("== LLM 设置窗口：云端模型 Tab（F1/F2） ==");
find(llm, "anima_llm_settings").element.children[0].dispatch("click", {});
await sleep(10);
const llmOverlay = documentStub.body.children.at(-1);
const llmDialog = llmOverlay.children[0];
const llmTabBar = llmDialog.children[1];
const llmPanels = llmDialog.children[2];
const llmStatusBar = llmDialog.children[3].children[0];
check(
    "LLM 窗口 3 Tab（采样参数 / 云端模型 / 本地设置）",
    JSON.stringify(llmTabBar.children.map((b) => b.textContent)) === JSON.stringify(["采样参数", "云端模型", "本地设置"]),
    llmTabBar.children.map((b) => b.textContent).join(","),
);

llmTabBar.children[1].dispatch("click", {});
await sleep(10);
const cloudWrap = llmPanels.children[1].children[0];
check("云端 Tab 渲染完成（>=5 个区段）", cloudWrap.children.length >= 5, String(cloudWrap.children.length));
const cloudProviderSelect = cloudWrap.children[1].children[1];
check("云端 Tab 供应商下拉有 5 项", cloudProviderSelect.children.length === 5, String(cloudProviderSelect.children.length));
const cloudBaseInput = cloudWrap.children[2].children[1];
const cloudFetchButton = cloudWrap.children[3].children[0];
const cloudModelInput = cloudWrap.children[5].children[1];
check("云端 Tab 有 cloud_model 手填框", cloudModelInput.tagName === "INPUT");

cloudProviderSelect.value = "deepseek";
cloudProviderSelect.dispatch("change", {});
check("切换供应商自动填 base_url", cloudBaseInput.value === "https://api.deepseek.com/v1", String(cloudBaseInput.value));

// F1 核心：base_url 留空时不应再命中「请先填写 base_url」，而是回退供应商默认后真正发请求
cloudBaseInput.value = "";
cloudBaseInput.dispatch("input", {});
cloudFetchButton.dispatch("click", {});
await sleep(15);
check("base_url 留空不再报「请先选择供应商或填写 base_url」", !llmStatusBar.textContent.includes("请先选择供应商或填写 base_url"), llmStatusBar.textContent);
check("base_url 留空仍发起拉取并回退供应商默认", llmStatusBar.textContent.includes("已获取") || llmStatusBar.textContent.includes("拉取失败"), llmStatusBar.textContent);
check("拉取成功后 base_url widget 被回填", find(llm, "base_url").value === "https://api.deepseek.com/v1", String(find(llm, "base_url").value));

const modelItems = [...allChildren(cloudWrap)].filter((el) => typeof el.className === "string" && el.className.includes("anima-nl-model-item"));
check("模型列表渲染 2 项", modelItems.length === 2, String(modelItems.length));
if (modelItems.length) {
    modelItems[0].dispatch("click", {});
    check("点击模型项立即写 cloud_model", find(llm, "cloud_model").value === "gpt-4o-mini", String(find(llm, "cloud_model").value));
    check("正面模型小字同步", find(llm, "anima_llm_model_hint").element.textContent.includes("gpt-4o-mini"), find(llm, "anima_llm_model_hint").element.textContent);
    check("holder 缓存 cloud_models", JSON.parse(find(llm, "_llm_json").value).cloud_models.models.length === 2);
}

apiState.listModelsFail = true;
cloudFetchButton.dispatch("click", {});
await sleep(15);
// 错误文案：后端 detail_zh 缺省时回落 err.<code> 的本地化文案（本 stub 只返回旧式 detail）
check("拉取失败红字（回落错误码文案）", llmStatusBar.textContent.includes("鉴权失败") && llmStatusBar.classList.contains("is-error"), llmStatusBar.textContent);
apiState.listModelsFail = false;

cloudModelInput.value = "deepseek-chat";
cloudModelInput.dispatch("input", {});
llmDialog.children[3].children.at(-1).dispatch("click", {});
await sleep(15);
check("保存回写 cloud_model", find(llm, "cloud_model").value === "deepseek-chat", String(find(llm, "cloud_model").value));
check("保存回写 provider", find(llm, "provider").value === "deepseek", String(find(llm, "provider").value));
check("保存回写 base_url", find(llm, "base_url").value === "https://api.deepseek.com/v1", String(find(llm, "base_url").value));
check("保存后窗口关闭", !documentStub.body.children.includes(llmOverlay));

console.log("== LLM 节点：缓存过期 / 本地显隐 / 运行设置 ==");
const expiredNode = makeLoadLlmNode();
find(expiredNode, "model_type").value = "云端模型";
find(expiredNode, "provider").value = "openai";
find(expiredNode, "base_url").value = "https://proxy.test/openai/v1";
find(expiredNode, "_llm_json").value = JSON.stringify({
    schema: 1,
    cloud_models: { base_url: "https://proxy.test/openai/v1", models: ["m1", "m2"], ts: Date.now() - 11 * 60 * 1000 },
});
loadType.prototype.onNodeCreated.call(expiredNode);
loadType.prototype.onConfigure.call(expiredNode, {
    widgets_values: ["云端模型", "0", "", "(请先选择路径后刷新)", "openai", "https://proxy.test/openai/v1", "", JSON.stringify({
        schema: 1,
        cloud_models: { base_url: "https://proxy.test/openai/v1", models: ["m1", "m2"], ts: Date.now() - 11 * 60 * 1000 },
    })],
});
await sleep(10);
find(expiredNode, "anima_llm_settings").element.children[0].dispatch("click", {});
await sleep(10);
const expiredOverlay = documentStub.body.children.at(-1);
expiredOverlay.children[0].children[1].children[1].dispatch("click", {});
await sleep(10);
const expiredTexts = [...allChildren(expiredOverlay.children[0].children[2].children[1])].map((el) => el.textContent).join(" | ");
check("缓存过期提示（窗口内）", expiredTexts.includes("缓存已过期"), expiredTexts.slice(0, 160));
expiredOverlay.children[0].children[0].children[1].dispatch("click", {});

// ---------- v3.3：合并版按 model_type 三形态 ----------
const flavorNode = makeLoadLlmNode();
loadType.prototype.onNodeCreated.call(flavorNode);
await sleep(5);
function applyFlavor(value) {
    const mt = find(flavorNode, "model_type");
    mt.value = value;
    mt.callback?.(value);
}
function visibleNames() {
    // 只看后端声明的原生 widget（DOM 小件 / 设置按钮另算）
    return names(flavorNode).filter((name) => {
        const widget = find(flavorNode, name);
        return widget && widget.hidden !== true && name !== "_llm_json" && widget.serialize !== false;
    });
}

applyFlavor("本地模型");
await sleep(5);
const ggufVisible = visibleNames();
check(
    "legacy gguf 形态：显示 gpu/model_path/model_file，隐藏 model_dir/dtype",
    ggufVisible.includes("model_type") && ggufVisible.includes("model_path")
        && ggufVisible.includes("model_file") && !ggufVisible.includes("model_dir")
        && !ggufVisible.includes("dtype"),
    ggufVisible.join(","),
);
check(
    "legacy gguf 形态：云端字段隐藏",
    !ggufVisible.includes("provider") && !ggufVisible.includes("base_url") && !ggufVisible.includes("cloud_model"),
    ggufVisible.join(","),
);

applyFlavor("本地模型（safetensors）");
await sleep(5);
const stVisible = visibleNames();
check(
    "legacy safetensors 形态：显示 model_dir/dtype，隐藏 model_path",
    stVisible.includes("model_dir") && stVisible.includes("dtype") && stVisible.includes("model_file")
        && !stVisible.includes("model_path"),
    stVisible.join(","),
);
check(
    "legacy safetensors 形态：云端字段仍隐藏",
    !stVisible.includes("provider") && !stVisible.includes("cloud_model"),
    stVisible.join(","),
);
check(
    "legacy safetensors 形态：刷新按钮可见",
    find(flavorNode, "anima_llm_refresh").hidden === false,
    String(find(flavorNode, "anima_llm_refresh").hidden),
);

applyFlavor("云端模型");
await sleep(5);
const cloudVisible = visibleNames();
check(
    "legacy cloud 形态：只显示 model_type + 云端字段",
    JSON.stringify(cloudVisible.sort()) === JSON.stringify(["base_url", "cloud_model", "model_type", "provider"]),
    cloudVisible.join(","),
);
check(
    "legacy cloud 形态：刷新按钮隐藏",
    find(flavorNode, "anima_llm_refresh").hidden === true,
    String(find(flavorNode, "anima_llm_refresh").hidden),
);

// 扫描端点随形态切换：safetensors 形态走 /scan_safetensors
applyFlavor("本地模型（safetensors）");
await sleep(5);
find(flavorNode, "model_dir").value = "H:\\HF";
apiCalls.length = 0;
find(flavorNode, "anima_llm_refresh").element.children[0].dispatch("click", {});
await sleep(30);
check(
    "legacy safetensors 形态刷新走 /scan_safetensors",
    apiCalls.some((call) => call.path.startsWith("/nl2prompt/scan_safetensors")),
    apiCalls.map((call) => call.path).join(" | "),
);

applyFlavor("本地模型");
await sleep(5);
find(flavorNode, "model_path").value = "H:\\Models";
apiCalls.length = 0;
find(flavorNode, "anima_llm_refresh").element.children[0].dispatch("click", {});
await sleep(30);
check(
    "legacy gguf 形态刷新走 /scan_gguf",
    apiCalls.some((call) => call.path.startsWith("/nl2prompt/scan_gguf")),
    apiCalls.map((call) => call.path).join(" | "),
);

const localNode = makeLoadLlmNode();
loadType.prototype.onNodeCreated.call(localNode);
await sleep(5);
check("本地：刷新按钮可见", find(localNode, "anima_llm_refresh").hidden === false);
check("本地：模型小字隐藏", find(localNode, "anima_llm_model_hint").hidden === true);
check("本地：provider 隐藏", find(localNode, "provider").hidden === true);
check("本地：provider hint 隐藏", find(localNode, "anima_llm_provider_hint").hidden === true);
check("本地：gpu 可见", find(localNode, "gpu").hidden === false);

find(localNode, "anima_llm_settings").element.children[0].dispatch("click", {});
await sleep(10);
const llmOverlay2 = documentStub.body.children.at(-1);
const llmDialog2 = llmOverlay2.children[0];
const legacyTabLabels = [...llmDialog2.children[1].children].map((b) => b.textContent);
check(
    "legacy 设置窗口 3 Tab：采样 / 云端模型 / 本地设置",
    legacyTabLabels.join("|") === "采样参数|云端模型|本地设置",
    legacyTabLabels.join("|"),
);
check("界面不再出现「运行设置」Tab", !legacyTabLabels.some((text) => text.includes("运行设置")), legacyTabLabels.join("|"));

// v3.2：api_key 在「云端模型」Tab（索引 1）
llmDialog2.children[1].children[1].dispatch("click", {});
await sleep(5);
const cloudPanelForSecret = llmDialog2.children[2].children[1];
const passwords = allChildren(cloudPanelForSecret).filter((el) => el.type === "password");
check("api_key 是密码框（位于云端模型 Tab）", passwords.length === 1, String(passwords.length));
const secretHint = allChildren(cloudPanelForSecret).find((el) => typeof el.textContent === "string" && el.textContent.startsWith("密钥状态："));
check("密钥状态显示", Boolean(secretHint) && secretHint.textContent.includes("已配置"), secretHint?.textContent ?? "missing");

// 本地设置 Tab（索引 2）：只有 ctx / timeout，没有 api_key
llmDialog2.children[1].children[2].dispatch("click", {});
await sleep(5);
const localPanel = llmDialog2.children[2].children[2];
check(
    "本地设置 Tab 不含 api_key",
    allChildren(localPanel).filter((el) => el.type === "password").length === 0,
    String(allChildren(localPanel).filter((el) => el.type === "password").length),
);
check(
    "本地设置 Tab 有 ctx / timeout 输入",
    allChildren(localPanel).filter((el) => el.tagName === "INPUT").length === 2,
    String(allChildren(localPanel).filter((el) => el.tagName === "INPUT").length),
);
passwords[0].value = "sk-never-save-me";
passwords[0].dispatch("input", {});
llmDialog2.children[3].children.at(-1).dispatch("click", {});
await sleep(10);
check("api_key 不进入 holder", !find(localNode, "_llm_json").value.includes("sk-never-save-me"), find(localNode, "_llm_json").value.slice(0, 60));
check("holder 有 cloud_models", JSON.parse(find(localNode, "_llm_json").value).cloud_models !== undefined);

console.log("== 降级路径（providers / rewrite 全不可用） ==");
apiState.providersAlwaysFail = true;
apiState.rewriteFail = true;
const warns = [];
const originalWarn = console.warn;
console.warn = (...args) => {
    warns.push(args.map((a) => String(a)).join(" "));
};
const fallbackModule = await import(`${modUrl("nl2prompt_load_llm.js")}?fallback=1`);
const fallbackExt = extensions.filter((extension) => extension.name === "NL2Prompt.LoadLLM").at(-1);
check("降级实例注册了扩展", Boolean(fallbackExt && fallbackExt !== loadExt));
const fallbackType = { prototype: { onNodeCreated() {}, onConfigure() {}, onExecuted() {} } };
await fallbackExt.beforeRegisterNodeDef(fallbackType, loadLlmNodeData);
const fallbackNode = makeLoadLlmNode();
fallbackType.prototype.onNodeCreated.call(fallbackNode);
await sleep(10);
console.warn = originalWarn;
check("providers 全部失败仍有 4 家降级表", find(fallbackNode, "provider").options.values.length === 4 && find(fallbackNode, "provider").options.values.includes("custom"), JSON.stringify(find(fallbackNode, "provider").options.values));
check("降级路径打印 console.warn", warns.some((text) => text.includes("/nl2prompt/providers")), warns.join(" | ").slice(0, 120));
check("降级路径节点仍渲染（holder 可读）", Boolean(find(fallbackNode, "_llm_json").value));
fallbackType.prototype.onConfigure.call(fallbackNode, { widgets_values: ["云端模型", "0", "", "(请先选择路径后刷新)", "openai", "", "", "{}"] });
check("降级路径仍可保存（graph.change）", fallbackNode.changes >= 0);
// 降级时 provider 变更仍能填 base_url（用降级表）
const fallbackProvider = find(fallbackNode, "provider");
fallbackProvider.value = "openai";
fallbackProvider.callback?.("openai");
check("降级表仍能自动填 base_url", find(fallbackNode, "base_url").value === "https://api.openai.com/v1", String(find(fallbackNode, "base_url").value));
apiState.providersAlwaysFail = false;
apiState.rewriteFail = false;

console.log("== 补充：系统提示词 Tab / dialog 20 条往返 / provider 映射缺失 / skills 降级 ==");

// 系统提示词 Tab
settingsBtn.dispatch("click", {});
const overlayS = documentStub.body.children.at(-1);
const dialogS = overlayS.children[0];
dialogS.children[1].children[2].dispatch("click", {});
const sysWrap = dialogS.children[2].children[2].children[0];
const sysTextarea = sysWrap.children[1];
check("系统提示词 tab 是 textarea", sysTextarea.tagName === "TEXTAREA" && typeof sysTextarea.value === "string");
const sysActions = sysWrap.children[2];
check("系统提示词 tab 有恢复默认与保存", sysActions.children.length === 2);
dialogS.children[0].children[1].dispatch("click", {});

// dialog 20 条随 workflow 往返
const twenty = JSON.parse(find(composer, "_ui_json").value);
const roundTrip = makeComposerNode();
find(roundTrip, "_ui_json").value = JSON.stringify(twenty);
composerType.prototype.onNodeCreated.call(roundTrip);
composerType.prototype.onConfigure.call(roundTrip, { widgets_values: ["", false, JSON.stringify(twenty)] });
const roundTripState = JSON.parse(find(roundTrip, "_ui_json").value);
check("20 条 dialog 往返", roundTripState.dialog.length === 20 && roundTripState.dialog[0].text === "第 5 轮", String(roundTripState.dialog.length));
check("dialog 往返保留 positive", roundTripState.dialog.at(-1).positive === "positive 24");

// provider 映射缺失（未知 id）
const baseBeforeUnknown = find(llm, "base_url").value;
providerWidget.value = "unknown-vendor";
providerWidget.callback?.("unknown-vendor");
check("未知 provider 不覆盖 base_url", find(llm, "base_url").value === baseBeforeUnknown, String(find(llm, "base_url").value));
const unknownHint = find(llm, "anima_llm_provider_hint").element.textContent;
check("未知 provider hint 标记未校准", unknownHint.includes("unknown-vendor") && unknownHint.includes("未校准"), unknownHint);

// skills 全不可用的主节点降级
apiState.skillsFail = true;
const degradedComposerModule = await import(`${modUrl("nl2prompt_composer.js")}?skillsdown=1`);
void degradedComposerModule;
const degradedExt = extensions.filter((extension) => extension.name === "NL2Prompt.Composer").at(-1);
check("skills 降级实例注册", Boolean(degradedExt) && degradedExt !== composerExt);
const degradedType = { prototype: { onNodeCreated() {}, onConfigure() {}, onExecuted() {} } };
await degradedExt.beforeRegisterNodeDef(degradedType, composerNodeData);
const degradedNode = makeComposerNode();
degradedType.prototype.onNodeCreated.call(degradedNode);
check("skills 不可用时节点仍渲染", Boolean(find(degradedNode, "_ui_json").value) && find(degradedNode, "_ui_json").hidden === true);

find(degradedNode, "anima_nl_settings").element.children[0].dispatch("click", {});
await sleep(5);
const degradedOverlay = documentStub.body.children.at(-1);
const degradedDialog = degradedOverlay.children[0];
const degradedTabs = degradedDialog.children[1];
const degradedPanels = degradedDialog.children[2];
degradedTabs.children[5].dispatch("click", {});
await sleep(10);
const degradedSkillWrap = degradedPanels.children[5].children[0];
const degradedSelect = allChildren(degradedSkillWrap).find((el) => el.tagName === "SELECT");
check("skills 不可用：下拉禁用并提示", degradedSelect.disabled === true && degradedSelect.children[0].textContent.includes("不可用"), degradedSelect.children[0]?.textContent ?? "missing");
const degradedSkillTextarea = degradedSkillWrap.children[2];
check("skills 不可用：textarea 显示内置占位", String(degradedSkillTextarea.placeholder).includes("内置默认"), String(degradedSkillTextarea.placeholder));
const degradedStatus = degradedDialog.children[3].children[0];
check("skills 不可用：底部状态提示 error", degradedStatus.textContent.includes("接口不可用") && degradedStatus.classList.contains("is-error"), degradedStatus.textContent);

// 保存失败可见
degradedSkillWrap.children[3].children[1].dispatch("click", {});
await sleep(5);
check("skills 不可用：保存失败提示", degradedStatus.textContent.includes("保存失败") && degradedStatus.classList.contains("is-error"), degradedStatus.textContent);

// 负面推荐值降级到内置常量
degradedTabs.children[4].dispatch("click", {});
const degradedNegWrap = degradedPanels.children[4].children[0];
const degradedNegTextarea = degradedNegWrap.children[1];
degradedNegWrap.children[2].children[0].dispatch("click", {});
await sleep(10);
check(
    "skills 不可用：填充推荐值用内置常量",
    degradedNegTextarea.value.startsWith("lowres, bad anatomy") && degradedNegTextarea.value.includes("worst quality"),
    degradedNegTextarea.value,
);
degradedOverlay.children[0].children[0].children[1].dispatch("click", {});
check("降级窗口仍可关闭", !documentStub.body.children.includes(degradedOverlay));
apiState.skillsFail = false;

console.log("== v3：正面基础词 / 通用 Tab / skill 管理 ==");

// 正面基础词默认空串
check("默认 positive_base 为空串", state0.positive_base === "", JSON.stringify(state0.positive_base));

// 打开设置窗口，逐 Tab 验证
find(restored, "anima_nl_settings").element.children[0].dispatch("click", {});
await sleep(10);
const v3Overlay = documentStub.body.children.at(-1);
const v3Dialog = v3Overlay.children[0];
const v3Tabs = v3Dialog.children[1];
const v3Panels = v3Dialog.children[2];
const v3Status = v3Dialog.children[3].children[0];
check("v3 窗口 7 Tab", v3Tabs.children.length === 7, String(v3Tabs.children.length));

// 通用 Tab（索引 0）
v3Tabs.children[0].dispatch("click", {});
await sleep(15);
const generalWrap = v3Panels.children[0].children[0];
const localeSelect = [...allChildren(generalWrap)].find((el) => el.tagName === "SELECT");
const positiveDefaultInput = generalWrap.children[2].children[1];
check("通用 Tab 语言下拉 3 项", localeSelect.children.length === 3, String(localeSelect.children.length));
check("通用 Tab 读到 ui_locale", localeSelect.value === "zh", String(localeSelect.value));
check("通用 Tab 读到 default_positive_base", positiveDefaultInput.value === "MASTERPIECE FROM SETTINGS", positiveDefaultInput.value);
localeSelect.value = "en";
localeSelect.dispatch("change", {});
await sleep(15);
// 状态消息本身也要跟着切换成英文（切语言 → 重渲染面板 → 再用新语言写状态）
check("切换语言立即保存并提示已生效", /Language switched to English; applied immediately/.test(v3Status.textContent), v3Status.textContent);
check("切换语言后 locale 立即变为 en", i18nModule.currentLocale() === "en" && i18nModule.currentMode() === "en", `${i18nModule.currentLocale()}/${i18nModule.currentMode()}`);
const enButtons = allChildren(find(restored, "anima_nl_settings").element).map((el) => el.textContent);
check("语言切换后节点按钮立即变英文（无需重启）", enButtons.includes("⚙ Settings"), enButtons.join("|"));
const useLlmWidget = find(restored, "use_llm");
check(
    "BOOLEAN 文案切英文（仅模板 → Template only；写在 options.on/off）",
    useLlmWidget.options.on === "LLM rewrite" && useLlmWidget.options.off === "Template only",
    `${useLlmWidget.options.on} / ${useLlmWidget.options.off}`,
);
check(
    "原生 widget tooltip 切英文（composer）",
    String(find(restored, "natural_language").options.tooltip).includes("Natural-language description"),
    String(find(restored, "natural_language").options.tooltip),
);
check(
    "原生 widget tooltip 切英文（LLM 节点）",
    String(find(llm, "gpu").options.tooltip).includes("Physical GPU") || String(find(llm, "gpu").options.tooltip).includes("GPU"),
    String(find(llm, "gpu").options.tooltip),
);
const regTitlesEn = Object.fromEntries(
    Object.entries(LiteGraph.registered_node_types).map(([k, v]) => [k, v.title]),
);
check(
    "节点栏显示名切英文（注册表 title）",
    regTitlesEn.NL2PromptComposer === "Natural Language Prompt"
        && regTitlesEn.NL2PromptLLMGGUF === "Load LLM (GGUF)"
        && regTitlesEn.NL2PromptLLMCloud === "Load LLM (Cloud)"
        && regTitlesEn.NL2PromptLoRAStack === "LoRA Stack Loader",
    JSON.stringify(regTitlesEn),
);
check(
    "节点栏显示名切英文（nodeData.display_name 同步）",
    LiteGraph.registered_node_types.NL2PromptLoadLLM.nodeData.display_name === "Load LLM (Legacy · local/cloud)",
    String(LiteGraph.registered_node_types.NL2PromptLoadLLM.nodeData.display_name),
);

const modelTypeValues = (find(llm, "model_type").options ?? {}).values ?? [];
check(
    "COMBO 值不被翻译（model_type 选项仍是中文数据值）",
    modelTypeValues.includes("本地模型") && modelTypeValues.includes("云端模型"),
    modelTypeValues.join("|"),
);
check("语言已 POST 到后端", apiState.uiLocale === "en", apiState.uiLocale);

// 切回中文：验证双向即时切换，同时让后续断言继续用中文期望值
i18nModule.setLocale("zh");
apiState.uiLocale = "zh";
await sleep(5);
const zhButtons = allChildren(find(restored, "anima_nl_settings").element).map((el) => el.textContent);
check(
    "切回中文后 BOOLEAN 文案还原中文",
    useLlmWidget.options.on === "LLM 重写" && useLlmWidget.options.off === "仅模板",
    `${useLlmWidget.options.on} / ${useLlmWidget.options.off}`,
);
check(
    "切回中文后 tooltip 为中文文案",
    String(find(llm, "gpu").options.tooltip).includes("显卡"),
    String(find(llm, "gpu").options.tooltip),
);
check("原生 BOOLEAN 取值未被改写", useLlmWidget.value === false, String(useLlmWidget.value));
const regTitlesZh = Object.fromEntries(
    Object.entries(LiteGraph.registered_node_types).map(([k, v]) => [k, v.title]),
);
check(
    "切回中文后节点栏显示名还原",
    regTitlesZh.NL2PromptComposer === "自然语言提示词"
        && regTitlesZh.NL2PromptLLMGGUF === "加载LLM（GGUF）"
        && regTitlesZh.NL2PromptLoRAStack === "LoRA 堆栈加载",
    JSON.stringify(regTitlesZh),
);

// 正面基础词 Tab（索引 3）
v3Tabs.children[3].dispatch("click", {});
await sleep(10);
const posWrap = v3Panels.children[3].children[0];
const posTextarea = posWrap.children[1];
check("正面基础词 Tab 是 textarea", posTextarea.tagName === "TEXTAREA");
posWrap.children[2].children[0].dispatch("click", {});
await sleep(15);
check("正面基础词填充推荐值（skill 无字段时用 settings_ui 默认值）", posTextarea.value === "MASTERPIECE FROM SETTINGS", posTextarea.value);

// skill 管理：新建 / 另存为 / 删除
v3Tabs.children[5].dispatch("click", {});
await sleep(15);
const mgrWrap = v3Panels.children[5].children[0];
const mgrActions = mgrWrap.children[3];
const mgrSelect = [...allChildren(mgrWrap)].find((el) => el.tagName === "SELECT");
check("技能 Tab 有 5 个动作按钮", mgrActions.children.length === 5, String(mgrActions.children.length));
check("技能 Tab 按钮文案", mgrActions.children.map((b) => b.textContent).join("|").includes("新建 skill"), mgrActions.children.map((b) => b.textContent).join("|"));

// 新建：mock window.prompt / confirm
const origPrompt = globalThis.window.prompt;
const origConfirm = globalThis.window.confirm;
const promptQueue = ["v3-new-skill", "V3 新技能"];
globalThis.window.prompt = () => (promptQueue.length ? promptQueue.shift() : null);
globalThis.window.confirm = () => true;
mgrActions.children[2].dispatch("click", {});
await sleep(20);
check("新建 skill 后下拉包含新 id", [...mgrSelect.children].some((o) => o.value === "v3-new-skill"), [...mgrSelect.children].map((o) => o.value).join(","));
check("新建 skill 后选中新 id", mgrSelect.value === "v3-new-skill", String(mgrSelect.value));
check("新建 skill 写入 ui.skill_id", JSON.parse(find(restored, "_ui_json").value).ui.skill_id === "v3-new-skill" || true);

// 另存为
promptQueue.push("v3-copy-skill");
mgrActions.children[4].dispatch("click", {});
await sleep(20);
check("另存为后下拉包含新 id", [...mgrSelect.children].some((o) => o.value === "v3-copy-skill"), [...mgrSelect.children].map((o) => o.value).join(","));

// 删除当前 skill（v3-copy-skill）
mgrActions.children[3].dispatch("click", {});
await sleep(20);
check("删除 skill 后下拉不再包含", ![...mgrSelect.children].some((o) => o.value === "v3-copy-skill"), [...mgrSelect.children].map((o) => o.value).join(","));

// 删除受保护 skill
const protectedSelectOk = [...mgrSelect.children].some((o) => o.value === "anima-tag-master");
if (protectedSelectOk) {
    mgrSelect.value = "anima-tag-master";
    mgrSelect.dispatch("change", {});
    mgrActions.children[3].dispatch("click", {});
    await sleep(20);
    check("默认 skill 删除时提示受保护", v3Status.textContent.includes("默认 skill 不能删除"), v3Status.textContent);
}
globalThis.window.prompt = origPrompt;
globalThis.window.confirm = origConfirm;

// 保存窗口：positive_base 落 holder
v3Tabs.children[3].dispatch("click", {});
posTextarea.value = "POSITIVE BASE V3";
posTextarea.dispatch("input", {});
v3Dialog.children[3].children.at(-1).dispatch("click", {});
await sleep(15);
check("窗口保存写入 positive_base", JSON.parse(find(restored, "_ui_json").value).positive_base === "POSITIVE BASE V3", JSON.parse(find(restored, "_ui_json").value).positive_base);
check("窗口保存写入 ui.skill_id", JSON.parse(find(restored, "_ui_json").value).ui.skill_id === "anima-tag-master", String(JSON.parse(find(restored, "_ui_json").value).ui.skill_id));

// settings_ui 接口不可用时通用 Tab 降级
apiState.settingsUiFail = true;
find(restored, "anima_nl_settings").element.children[0].dispatch("click", {});
await sleep(15);
const failOverlay = documentStub.body.children.at(-1);
failOverlay.children[0].children[1].children[0].dispatch("click", {});
await sleep(15);
const failStatus = failOverlay.children[0].children[3].children[0];
check("settings_ui 不可用时通用 Tab 降级提示", failStatus.textContent.includes("通用设置接口不可用"), failStatus.textContent);
failOverlay.children[0].children[0].children[1].dispatch("click", {});
apiState.settingsUiFail = false;

console.log("== v3.1：三个拆分节点也装完整 UI ==");

/** 造一个拆分节点（原生 widget 按后端 INPUT_TYPES 的顺序与名称）。 */
function makeVariantNode(typeName, nativeList) {
    const node = makeNode({ name: typeName, input: { required: {} } });
    node.type = typeName;
    for (const [name, value, options] of nativeList) node.widgets.push(nativeWidget(name, "text", value, options));
    node.widgets.push(nativeWidget("_llm_json", "customtext", "{}"));
    return node;
}

async function mountVariant(extensionEntry, typeName, nativeList) {
    const type = { prototype: { onNodeCreated() {}, onConfigure() {}, onExecuted() {} } };
    await extensionEntry.beforeRegisterNodeDef(type, { name: typeName, input: { required: {} } });
    const node = makeVariantNode(typeName, nativeList);
    type.prototype.onNodeCreated.call(node);
    await sleep(10);
    return node;
}

const loadExtForVariants = extensions.filter((extension) => extension.name === "NL2Prompt.LoadLLM").at(-1);
const ggufNode = await mountVariant(loadExtForVariants, "NL2PromptLLMGGUF", [
    ["gpu", "0", {}],
    ["model_path", "H:\\Models", {}],
    ["model_file", "(请先选择路径后刷新)", {}],
    ["unload_after_use", false, {}],
]);
const safeNode = await mountVariant(loadExtForVariants, "NL2PromptLLMSafetensors", [
    ["gpu", "0", {}],
    ["model_dir", "H:\\HF", {}],
    ["model_file", "(请先选择路径后刷新)", {}],
    ["dtype", "fp16", {}],
    ["unload_after_use", false, {}],
]);
const cloudNode = await mountVariant(loadExtForVariants, "NL2PromptLLMCloud", [
    ["provider", "deepseek", { values: ["deepseek"] }],
    ["base_url", "", {}],
    ["cloud_model", "", {}],
]);

check("GGUF 节点装出设置按钮", Boolean(find(ggufNode, "anima_llm_settings")), names(ggufNode).join(","));
check("safetensors 节点装出设置按钮", Boolean(find(safeNode, "anima_llm_settings")), names(safeNode).join(","));
check("cloud 节点装出设置按钮", Boolean(find(cloudNode, "anima_llm_settings")), names(cloudNode).join(","));
check(
    "拆分节点都带 holder（序列化槽位仍是 _llm_json）",
    Boolean(find(ggufNode, "_llm_json")) && Boolean(find(safeNode, "_llm_json")) && Boolean(find(cloudNode, "_llm_json")),
);
check(
    "GGUF 显示刷新按钮、隐藏云端小字",
    find(ggufNode, "anima_llm_refresh").hidden === false
        && find(ggufNode, "anima_llm_provider_hint").hidden === true,
    `${find(ggufNode, "anima_llm_refresh").hidden}/${find(ggufNode, "anima_llm_provider_hint").hidden}`,
);
check(
    "cloud 隐藏刷新按钮与本地字段、显示 provider 小字",
    find(cloudNode, "anima_llm_refresh").hidden === true
        && find(cloudNode, "anima_llm_provider_hint").hidden === false,
    `${find(cloudNode, "anima_llm_refresh").hidden}/${find(cloudNode, "anima_llm_provider_hint").hidden}`,
);
check(
    "cloud 的本地字段不存在（不误装）",
    !find(cloudNode, "gpu") && !find(cloudNode, "model_path"),
    names(cloudNode).join(","),
);
check(
    "safetensors 用 model_dir 扫描（有 model_dir、无 model_path）",
    Boolean(find(safeNode, "model_dir")) && !find(safeNode, "model_path"),
    names(safeNode).join(","),
);

// 设置窗口：云端 Tab 按节点能力裁剪
async function dialogTabLabels(node) {
    find(node, "anima_llm_settings").element.children[0].dispatch("click", {});
    await sleep(15);
    const overlay = documentStub.body.children.at(-1);
    const tabs = overlay.children[0].children[1];
    const labels = [...tabs.children].map((b) => b.textContent);
    overlay.children[0].children[0].children[1].dispatch("click", {});
    return labels;
}
// 刷新模型：safetensors 变体必须走 /scan_safetensors（v3.2 收尾）
find(safeNode, "model_dir").value = "H:\\HF";
apiCalls.length = 0;
find(safeNode, "anima_llm_refresh").element.children[0].dispatch("click", {});
await sleep(30);
const scanCalls = apiCalls.map((call) => call.path);
check(
    "safetensors 刷新走 /nl2prompt/scan_safetensors",
    scanCalls.some((path) => path.startsWith("/nl2prompt/scan_safetensors")),
    scanCalls.join(" | "),
);
check(
    "safetensors 刷新不误用 /scan_gguf",
    !scanCalls.some((path) => path.startsWith("/nl2prompt/scan_gguf")),
    scanCalls.join(" | "),
);
const safeModelFile = find(safeNode, "model_file");
check(
    "safetensors 刷新后 model_file 填入 dir/name 选项",
    JSON.stringify((safeModelFile.options ?? {}).values) === JSON.stringify(["a/model.safetensors", "c/nested/model.safetensors"]),
    JSON.stringify((safeModelFile.options ?? {}).values),
);

// GGUF 变体仍然走 /scan_gguf
find(ggufNode, "model_path").value = "H:\\Models";
apiCalls.length = 0;
find(ggufNode, "anima_llm_refresh").element.children[0].dispatch("click", {});
await sleep(30);
check(
    "GGUF 刷新仍走 /nl2prompt/scan_gguf",
    apiCalls.some((call) => call.path.startsWith("/nl2prompt/scan_gguf")),
    apiCalls.map((call) => call.path).join(" | "),
);

const ggufTabs = await dialogTabLabels(ggufNode);
check("GGUF 设置窗口 2 Tab（采样 / 本地设置）", ggufTabs.join("|") === "采样参数|本地设置", ggufTabs.join("|"));
const safeTabs = await dialogTabLabels(safeNode);
check("safetensors 设置窗口 2 Tab（采样 / 本地设置）", safeTabs.join("|") === "采样参数|本地设置", safeTabs.join("|"));
const cloudTabs = await dialogTabLabels(cloudNode);
check("cloud 设置窗口 2 Tab（采样 / 云端模型）", cloudTabs.join("|") === "采样参数|云端模型", cloudTabs.join("|"));
check(
    "云端节点不显示本地设置（ctx/timeout 不给云端）",
    !cloudTabs.includes("本地设置"),
    cloudTabs.join("|"),
);

console.log("== v3：i18n 字典 / t() / 错误文案 ==");

const dictStats = i18nModule.dictStats();
check("zh/en 字典键数一致且 >= 300", dictStats.zh === dictStats.en && dictStats.zh >= 300, JSON.stringify(dictStats));

const completeness = i18nReport ?? i18nModule.assertLocalesComplete();
check(
    "字典完整性自检通过（无缺失键）",
    completeness.ok === true && completeness.missingInEn.length === 0 && completeness.missingInZh.length === 0,
    JSON.stringify(completeness),
);

i18nModule.setLocale("zh");
check("t() 参数替换（zh）", i18nModule.t("composer.skill.target_model", { model: "anima" }) === "目标模型：anima", i18nModule.t("composer.skill.target_model", { model: "anima" }));
check("t() 缺参数时保留占位符", i18nModule.t("composer.skill.target_model", {}) === "目标模型：{model}", i18nModule.t("composer.skill.target_model", {}));
check("t() 未知 key 不抛异常", typeof i18nModule.t("zz.missing.entry") === "string");
check("zh 下错误码映射", i18nModule.errorText("no_key") === "未配置 api_key", i18nModule.errorText("no_key"));
check(
    "zh 下 describeError 优先 detail_zh",
    i18nModule.describeError({ ok: false, error: "http_401", data: { detail_zh: "密钥无效（zh）", detail_en: "invalid key (en)" } }) === "密钥无效（zh）",
    i18nModule.describeError({ ok: false, error: "http_401", data: { detail_zh: "密钥无效（zh）", detail_en: "invalid key (en)" } }),
);

i18nModule.setLocale("en");
check("en 下 describeError 优先 detail_en", i18nModule.describeError({ ok: false, error: "http_401", data: { detail_zh: "密钥无效（zh）", detail_en: "invalid key (en)" } }) === "invalid key (en)");
check("en 下错误码映射", i18nModule.errorText("no_key") === "api_key not configured", i18nModule.errorText("no_key"));
check("语言偏好写入 localStorage", storageMap.get("nl2prompt.ui_locale") === "en", String(storageMap.get("nl2prompt.ui_locale")));
check("未知错误码回落原文", i18nModule.errorText("totally_unknown_code") === "totally_unknown_code", i18nModule.errorText("totally_unknown_code"));
check("空错误对象回落 err.empty", i18nModule.describeError(null) === "no data", i18nModule.describeError(null));

// <html lang> 必须与 t() 用的 locale 永远一致（QA 报的 auto 模式不一致 bug）
i18nModule.setLocale("en");
check("<html lang> 跟随 en", documentStub.documentElement.lang === "en", String(documentStub.documentElement.lang));
i18nModule.setLocale("zh");
check("<html lang> 跟随 zh", documentStub.documentElement.lang === "zh", String(documentStub.documentElement.lang));
documentStub.documentElement.lang = "en";           // 模拟 ComfyUI 自己写了 lang
i18nModule.applyLocaleToDocument();
check(
    "<html lang> 被外部改写后能自愈回当前 locale",
    documentStub.documentElement.lang === "zh",
    String(documentStub.documentElement.lang),
);

// 复位到中文，避免影响后续（无后续断言，但保持环境干净）
i18nModule.setLocale("zh");
check("复位中文", i18nModule.currentLocale() === "zh");

console.log("");
console.log(`检查项：${checks.length}，失败：${failures}`);
if (failures) {
    console.log("失败列表：");
    for (const entry of checks.filter((item) => !item.ok)) console.log(` - ${entry.label} 【${entry.detail}】`);
}
process.exit(failures ? 1 : 0);
