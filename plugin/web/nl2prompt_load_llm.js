// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 GQLI-VC
// 加载LLM（NL2PromptLoadLLM）—— 节点正面 UI 与 LLM 设置窗口（v2）。
//
// 契约：docs/接口契约.md §2（节点 IO）、§4 / §14.4（holder schema）、§6 / §14.3（路由）、
//       §9.3 / §9.4（前端）、§14.1（供应商注册表单一真源 = providers.py）、§14.5（默认值）。
//
// 数据真源只有 hidden holder（_llm_json）；刷新按钮 / 拉取模型 / 设置按钮都是 serialize=false 的 DOM widget。
// api_key 只在窗口内存里存在：只在点击「保存密钥」时 POST /nl2prompt/secret，
// 绝不写入 holder、也不进入 workflow。
//
// 所有网络请求都走 common.apiGet/apiPost（失败返回 {ok:false,error}，绝不抛异常）：
// 后端路由未就绪时节点仍能正常渲染、编辑、保存。

import { app } from "../../scripts/app.js";
import {
    apiGet,
    apiPost,
    blockPointerEvent,
    buildLlmModelPayload,
    cloneJson,
    createElement,
    findWidget,
    fitNodeHeight,
    injectStyles,
    isFreshTimestamp,
    localizeModelType,
    localizeNativeWidget,
    modelTypeFlavor,
    moveWidgetAfter,
    openDialog,
    readJson,
    refreshAllNodeFaces,
    setComboValues,
    setWidgetHidden,
    stopPointerEvent,
    toNumber,
    truncateText,
    widgetText,
} from "./common.js";
import { currentLocale, describeError, subscribeLocale, t } from "./i18n.js";

/**
 * 需要安装**完整 UI**（折叠头 / provider 提示 / 刷新 / 设置按钮 / 设置窗口）的节点类名。
 * - `NL2PromptLoadLLM`：v3 的合并版
 * - `AnimaLoadLLM`：v2 旧类名 —— 旧工作流反序列化得到的就是这个名字，
 *   不在这里会导致「旧节点 UI 消失、设置按钮不见」
 */
const NODE_CLASSES = [
    "NL2PromptLoadLLM",        // v3 合并版
    "AnimaLoadLLM",            // v2 旧类名（旧工作流反序列化得到）
    "NL2PromptLLMGGUF",        // v3 拆分：本地 GGUF
    "NL2PromptLLMSafetensors", // v3 拆分：本地 safetensors
    "NL2PromptLLMCloud",       // v3 拆分：云端
];
const HOLDER_NAME = "_llm_json";

const MIN_WIDTH = 400;
const WIDGET_MARGIN = 4;
const ACTION_HEIGHT = 30;
const HINT_HEIGHT = 18;

const MODEL_TYPE_LOCAL = "本地模型";

const LOCAL_WIDGETS = ["gpu", "model_path", "model_file"];
const CLOUD_WIDGETS = ["provider", "base_url", "cloud_model"];

/**
 * 拆分节点的本地字段（按变体）。合并版沿用 LOCAL_WIDGETS + 运行时切换。
 * 只影响显隐，不动 widget 名 / 取值。
 */
const VARIANT_LOCAL_WIDGETS = {
    gguf: ["gpu", "model_path", "model_file", "unload_after_use"],
    safetensors: ["gpu", "model_dir", "model_file", "dtype", "unload_after_use"],
};

/** 云端模型列表缓存有效期（契约：10 分钟内） */
const CLOUD_CACHE_TTL_MS = 10 * 60 * 1000;

/* ------------------------------------------------------------------ 供应商表（后端驱动） */

/**
 * 供应商表缓存。数据真源是后端 `providers.py`（经 `/nl2prompt/providers`）；
 * 下面 FALLBACK_PROVIDERS 只在接口不可用时兜底，避免节点无法使用。
 */
const PROVIDER_CACHE = {
    loaded: false,
    seeded: false,
    failed: false,
    items: [],
    byId: new Map(),
    loading: null,
};

const FALLBACK_PROVIDERS = [
    {
        id: "deepseek",
        name: "DeepSeek",
        label: "深度求索",
        base_url: "https://api.deepseek.com/v1",
        openai_compat: true,
        default_model: "deepseek-chat",
        needs_key: true,
        verified: true,
        note_key: "llm.provider_note_fallback",
    },
    {
        id: "openai",
        name: "OpenAI",
        label: "OpenAI",
        base_url: "https://api.openai.com/v1",
        openai_compat: true,
        default_model: "gpt-4o-mini",
        needs_key: true,
        verified: true,
        note_key: "llm.provider_note_fallback",
    },
    {
        id: "anthropic",
        name: "Anthropic",
        label: "Anthropic",
        base_url: "https://api.anthropic.com/v1",
        openai_compat: false,
        default_model: "claude-3-5-sonnet-20241022",
        needs_key: true,
        verified: true,
        note_key: "llm.provider_note_fallback",
    },
    {
        id: "custom",
        name: "Custom",
        label: "自定义（OpenAI 兼容）",
        base_url: "",
        openai_compat: true,
        default_model: "",
        needs_key: true,
        verified: false,
        note_key: "llm.provider_note_custom",
    },
];

/**
 * 供应商显示名。
 *
 * 后端 `providers.py` 的 `label` 中文优先（34 家里 19 家含中文，如「DeepSeek 深度求索」），
 * 英文界面下会露出中文，所以英文用品牌名 `name` 兜底。`label_zh` 保留原始值，
 * 切回中文时无需重新请求后端。
 */
function providerLabel(name, rawLabel, locale = currentLocale()) {
    if (locale === "en" && name) return name;
    return rawLabel || name;
}

function normalizeProviderItem(item, locale = currentLocale()) {
    if (!item || typeof item !== "object") return null;
    const id = typeof item.id === "string" ? item.id.trim() : "";
    if (!id) return null;
    const name = typeof item.name === "string" ? item.name : "";
    const rawLabel = typeof item.label === "string" && item.label.trim() ? item.label.trim() : (name || id);
    return {
        id,
        name,
        label: providerLabel(name, rawLabel, locale),
        label_zh: rawLabel,
        base_url: typeof item.base_url === "string" ? item.base_url : "",
        openai_compat: item.openai_compat !== false,
        default_model: typeof item.default_model === "string" ? item.default_model : "",
        needs_key: item.needs_key !== false,
        verified: item.verified === true,
        note: typeof item.note === "string" ? item.note : "",
    };
}

/** 语言切换后重算所有供应商显示名（不重新请求后端）。 */
function relocalizeProviderLabels() {
    const locale = currentLocale();
    for (const item of PROVIDER_CACHE.items) {
        const next = providerLabel(item.name, item.label_zh || item.label, locale);
        if (item.label !== next) item.label = next;
    }
}

function setProviderTable(items, failed) {
    const list = (Array.isArray(items) ? items : []).map(normalizeProviderItem).filter(Boolean);
    if (!list.length) return false;
    PROVIDER_CACHE.items = list;
    PROVIDER_CACHE.byId = new Map(list.map((item) => [item.id, item]));
    PROVIDER_CACHE.failed = failed === true;
    PROVIDER_CACHE.seeded = true;
    return true;
}

/** 先用降级表填一次，保证界面立即有内容（标记为未 loaded，随后仍会请求后端覆盖）。 */
function seedFallbackProviders() {
    if (PROVIDER_CACHE.loaded || PROVIDER_CACHE.seeded) return;
    setProviderTable(FALLBACK_PROVIDERS, true);
}

/**
 * 拉取 `/nl2prompt/providers`（失败重试一次）。
 * 全部失败 → 使用 FALLBACK_PROVIDERS 并 console.warn，绝不抛异常。
 */
async function ensureProviderTable() {
    if (PROVIDER_CACHE.loaded) return PROVIDER_CACHE;
    if (PROVIDER_CACHE.loading) return PROVIDER_CACHE.loading;

    PROVIDER_CACHE.loading = (async () => {
        let response = await apiGet("/nl2prompt/providers");
        if (!response.ok) response = await apiGet("/nl2prompt/providers");

        const raw = response.ok && response.data && Array.isArray(response.data.providers)
            ? response.data.providers
            : [];
        if (!setProviderTable(raw, false)) {
            setProviderTable(FALLBACK_PROVIDERS, true);
            console.warn(
                "[nl2prompt] /nl2prompt/providers 不可用，已使用内置降级供应商表（deepseek/openai/anthropic/custom）",
                response.ok ? "empty" : (response.error || response.status),
            );
        }
        PROVIDER_CACHE.loaded = true;
        PROVIDER_CACHE.loading = null;
        return PROVIDER_CACHE;
    })();

    return PROVIDER_CACHE.loading;
}

function providerMeta(providerId) {
    const id = String(providerId ?? "").trim();
    if (!id) return null;
    return PROVIDER_CACHE.byId.get(id) ?? null;
}

/* ------------------------------------------------------------------ 数据模型 */

function normalizeCloudModels(raw) {
    const input = raw && typeof raw === "object" ? raw : {};
    const models = Array.isArray(input.models)
        ? input.models.map((item) => (typeof item === "string" ? item : String(item?.id ?? ""))).filter(Boolean)
        : [];
    return {
        base_url: typeof input.base_url === "string" ? input.base_url : "",
        models,
        ts: toNumber(input.ts, 0),
    };
}

function defaultState() {
    return {
        schema: 1,
        api_key_configured: false,
        api_key_env: "ANIMA_LLM_API_KEY",
        sampling: { top_k: 40, top_p: 0.9, temperature: 0.7, max_tokens: 1024, seed: -1 },
        runtime: { ctx: 8192, timeout: 120, port: 0 },
        last_seed: -1,
        cloud_models: { base_url: "", models: [], ts: 0 },
    };
}

// 缺字段补默认（旧 workflow 没有 cloud_models 也能正常用）
function normalizeState(raw) {
    const input = raw && typeof raw === "object" ? raw : {};
    const state = defaultState();

    state.schema = toNumber(input.schema, 1);
    state.api_key_configured = input.api_key_configured === true;
    state.api_key_env = typeof input.api_key_env === "string" && input.api_key_env
        ? input.api_key_env
        : "ANIMA_LLM_API_KEY";

    const sampling = input.sampling && typeof input.sampling === "object" ? input.sampling : {};
    state.sampling = {
        top_k: toNumber(sampling.top_k, 40),
        top_p: toNumber(sampling.top_p, 0.9),
        temperature: toNumber(sampling.temperature, 0.7),
        max_tokens: toNumber(sampling.max_tokens, 1024),
        seed: toNumber(sampling.seed, -1),
    };

    const runtime = input.runtime && typeof input.runtime === "object" ? input.runtime : {};
    state.runtime = {
        ctx: toNumber(runtime.ctx, 8192),
        timeout: toNumber(runtime.timeout, 120),
        port: toNumber(runtime.port, 0),
    };

    state.last_seed = toNumber(input.last_seed, -1);
    state.cloud_models = normalizeCloudModels(input.cloud_models);
    return state;
}

/* ------------------------------------------------------------------ 节点安装 */

function addBlockWidget(node, name, element, heightFn) {
    element.classList.add("anima-nl-root");
    const widget = node.addDOMWidget(name, "anima_nl_block", element, {
        hideOnZoom: false,
        serialize: false,
        margin: WIDGET_MARGIN,
        getValue: () => "",
        setValue: () => {},
    });
    widget.serialize = false;
    widget.computeSize = (width) => {
        const nodeWidth = Number.isFinite(width) ? width : (node.size?.[0] ?? MIN_WIDTH);
        const height = Math.max(0, toNumber(heightFn?.(), 0));
        return [Math.max(nodeWidth, MIN_WIDTH), height + 2 * (widget.margin ?? WIDGET_MARGIN)];
    };
    return widget;
}

/** 节点变体：三个拆分节点字段固定；合并版（含 AnimaLoadLLM）按 model_type 运行时切换。 */
function nodeVariant(node) {
    const type = String(node?.type ?? node?.constructor?.nodeData?.name ?? "");
    if (type === "NL2PromptLLMGGUF") return "gguf";
    if (type === "NL2PromptLLMSafetensors") return "safetensors";
    if (type === "NL2PromptLLMCloud") return "cloud";
    return "legacy";
}

/**
 * 合并版（legacy）按 model_type 再细分三形态：
 *   "本地模型"（= GGUF，走 llama-server）/ "本地模型（safetensors）" / "云端模型"
 * 旧工作流里 model_type 只可能是前两种旧值，无需迁移。
 */
function legacyFlavor(node) {
    const value = String(widgetText(node, "model_type", MODEL_TYPE_LOCAL) ?? "");
    return modelTypeFlavor(value);
}

/** 统一形态：拆分节点直接用变体名，合并版按 model_type 细分。 */
function effectiveFlavor(node) {
    const variant = nodeVariant(node);
    return variant === "legacy" ? legacyFlavor(node) : variant;
}

function isLocalMode(node) {
    return effectiveFlavor(node) !== "cloud";
}

/** 扫描模型列表要读的根目录字段：safetensors 形态是 model_dir，其余是 model_path。 */
function modelRootWidgetName(node) {
    return effectiveFlavor(node) === "safetensors" ? "model_dir" : "model_path";
}

/** 刷新模型时按变体显示的文件类型（.gguf / .safetensors），用于提示与错误文案。 */
function scanExtension(node) {
    return effectiveFlavor(node) === "safetensors" ? ".safetensors" : ".gguf";
}

/** 刷新按钮的 tooltip（按变体带扩展名）。 */
function refreshModelsTooltip(node) {
    return t("llm.refresh_models_tooltip", { ext: scanExtension(node) });
}

/**
 * 扫描端点：safetensors 走 /scan_safetensors（只返回同目录含 config.json 的权重），
 * 其余（gguf / legacy）走 /scan_gguf。cloud 变体没有刷新按钮。
 */
function scanEndpoint(node) {
    return effectiveFlavor(node) === "safetensors"
        ? "/nl2prompt/scan_safetensors"
        : "/nl2prompt/scan_gguf";
}

/**
 * 「尚未选择模型」的占位项，中英各一份。
 * 必须与后端 `MODEL_FILE_PLACEHOLDERS`（nodes.py / nodes_llm.py）保持一致——
 * 占位值会随 widgets_values 存进工作流，后端靠它判断「没选模型」。
 */
const MODEL_PLACEHOLDER_SAMPLES = ["(请先选择路径后刷新)", "(select a folder, then refresh)"];

/** 是否是占位值（空串也算，后端同样把空串当作未选择）。 */
function isModelPlaceholder(value) {
    const text = String(value ?? "").trim();
    return !text || MODEL_PLACEHOLDER_SAMPLES.includes(text);
}

/**
 * 把 model_file 下拉里的占位项换成本语言。
 *
 * 注意改的是**值**而不只是显示：ComfyUI 的 COMBO 没有 label/value 分离，
 * 所以只能让值是当前语言，再由后端同时认中英两种写法。
 */
function localizeModelPlaceholder(node) {
    const widget = findWidget(node, "model_file");
    if (!widget) return;
    const text = t("llm.model_file.placeholder");

    const values = widget.options?.values;
    if (Array.isArray(values)) {
        const index = values.findIndex(isModelPlaceholder);
        if (index >= 0 && values[index] !== text) values[index] = text;
    }
    if (isModelPlaceholder(widget.value) && widget.value !== text) {
        widget.value = text;
    }
}

/** 把扫描结果（`/scan_gguf` 或 `/scan_safetensors`）写进 model_file 的 options.values。 */
function applyModelChoices(node, values) {
    const widget = findWidget(node, "model_file");
    if (!widget) return;
    if (!widget.options || typeof widget.options !== "object") widget.options = {};
    widget.options.values = values;
    if (!values.includes(widget.value)) widget.value = values[0];
    // 后端返回的是中文占位（INPUT_TYPES 默认值），这里按当前语言再刷一次
    localizeModelPlaceholder(node);
    node.setDirtyCanvas?.(true, true);
}

/* ---------------- 加载工作流后补齐 model_file 选项 ---------------- */

/**
 * 重启 ComfyUI 后补齐 `model_file` 的选项。
 *
 * 后端 `INPUT_TYPES` 刻意不预扫描（启动快、不碰磁盘），所以 COMBO 的 options
 * 只有占位项。而工作流里存着上次选好的模型文件名，ComfyUI 在提交时会做 COMBO
 * 校验，于是报：
 *
 *     Value not in list: model_file: 'xxx.gguf' not in ['(请先选择路径后刷新)']
 *     → invalid prompt: prompt_outputs_failed_validation
 *
 * 用户看到的就是「模型不存在，要重下」。这里在加载工作流后静默补扫一次。
 */
async function ensureModelChoices(node) {
    const widget = findWidget(node, "model_file");
    if (!widget) return;
    const current = String(widget.value ?? "").trim();
    if (isModelPlaceholder(current)) return;

    // 判断「是否还没扫过」：列表里除了占位项和当前值之外还有别的模型，说明已经扫过。
    // 不能只看 `values.includes(current)`——keepCurrentModelChoice 会先把当前值塞进去，
    // 那样这里会立刻 return，扫描永远不会发生。
    const values = widget.options?.values;
    if (Array.isArray(values)) {
        const others = values.filter((v) => !isModelPlaceholder(v) && String(v).trim() !== current);
        if (others.length > 0) return;
    }

    const rootPath = widgetText(node, modelRootWidgetName(node)).trim();
    if (!rootPath) return;
    try {
        const response = await apiGet(`${scanEndpoint(node)}?path=${encodeURIComponent(rootPath)}`);
        if (!response.ok) return;
        const data = response.data ?? {};
        if (data.error) return;
        const files = Array.isArray(data.files) ? data.files : [];
        const choices = [];
        for (const item of files) {
            const name = String(item?.name ?? "").trim();
            if (!name) continue;
            const dir = String(item?.dir ?? "").trim().replace(/^[\\/]+|[\\/]+$/g, "");
            choices.push(dir ? `${dir}/${name}` : name);
        }
        if (choices.length) applyModelChoices(node, choices);
    } catch (error) {
        console.error("[nl2prompt] 自动补齐模型列表失败", error);
    }
}

/** 先把当前值塞进 options，保证即使扫描还没回来、提交也不会被 COMBO 校验拦下。 */
function keepCurrentModelChoice(node) {
    const widget = findWidget(node, "model_file");
    if (!widget) return;
    const current = String(widget.value ?? "").trim();
    if (isModelPlaceholder(current)) return;
    if (!widget.options || typeof widget.options !== "object") widget.options = {};
    if (!Array.isArray(widget.options.values)) widget.options.values = [t("llm.model_file.placeholder")];
    if (!widget.options.values.includes(current)) widget.options.values.push(current);
}


/**
 * 后端 INPUT_TYPES 声明的原生 widget → i18n key（只用于显示文案：tooltip）。
 * 取值（COMBO / BOOLEAN 的 value）一律原样，不参与翻译。
 */
const NATIVE_WIDGET_SPECS = {
    model_type: { tooltipKey: "llm.widget.model_type_tooltip" },
    gpu: { tooltipKey: "llm.widget.gpu_tooltip" },
    model_path: { tooltipKey: "llm.widget.model_path_tooltip" },
    model_dir: { tooltipKey: "llm.widget.model_dir_tooltip" },
    model_file: { tooltipKey: "llm.widget.model_file_tooltip" },
    dtype: { tooltipKey: "llm.widget.dtype_tooltip" },
    unload_after_use: { tooltipKey: "llm.widget.unload_tooltip" },
    provider: { tooltipKey: "llm.widget.provider_tooltip" },
    base_url: { tooltipKey: "llm.widget.base_url_tooltip" },
    cloud_model: { tooltipKey: "llm.widget.cloud_model_tooltip" },
};

/** 本地化一个节点上所有后端声明 widget 的显示文案（幂等，中英可来回切）。 */
function localizeNodeNativeWidgets(node) {
    for (const [name, spec] of Object.entries(NATIVE_WIDGET_SPECS)) {
        localizeNativeWidget(findWidget(node, name), spec);
    }
}

function installLoadLlm(node) {
    injectStyles();

    // ---- 用同名 DOM holder 接管 Python 声明的 _llm_json
    let state = normalizeState(null);
    const nativeHolder = findWidget(node, HOLDER_NAME);
    if (nativeHolder) {
        const index = node.widgets.indexOf(nativeHolder);
        if (index >= 0) node.widgets.splice(index, 1);
        if (!app.configuringGraph) state = normalizeState(readJson(nativeHolder.value));
    }

    const holderEl = createElement("div", "anima-nl-root");
    const holder = node.addDOMWidget(HOLDER_NAME, "anima_llm_holder", holderEl, {
        hideOnZoom: false,
        serialize: true,
        getValue: () => JSON.stringify(state),
        setValue: (value) => {
            const next = normalizeState(readJson(value));
            if (JSON.stringify(next) === JSON.stringify(state)) return;
            state = next;
            refreshCloudModelHint();
        },
    });
    holder.serialize = true;
    holder.hidden = true;
    holder.computeSize = () => [0, -4];

    function changed() {
        node.setDirtyCanvas?.(true, true);
        node.graph?.change?.();
    }

    /* ---------------- 本地：刷新模型列表 ---------------- */

    const refreshRow = createElement("div", "anima-nl-buttons");
    const refreshButton = createElement("button", "anima-nl-btn is-solid", t("llm.refresh_models"));
    refreshButton.type = "button";
    refreshButton.title = refreshModelsTooltip(node);
    refreshRow.appendChild(refreshButton);
    const refreshWidget = addBlockWidget(node, "anima_llm_refresh", refreshRow, () => ACTION_HEIGHT);

    let flashTimer = null;

    function flashButton(text, kind = "") {
        if (flashTimer !== null) {
            clearTimeout(flashTimer);
            flashTimer = null;
        }
        refreshButton.classList.remove("is-error", "is-ok");
        if (kind === "error") refreshButton.classList.add("is-error");
        if (kind === "ok") refreshButton.classList.add("is-ok");
        if (text) refreshButton.textContent = text;
        if (!kind) return;
        flashTimer = setTimeout(() => {
            flashTimer = null;
            refreshButton.classList.remove("is-error", "is-ok");
            refreshButton.textContent = t("llm.refresh_models");
        }, 2600);
    }

    async function refreshModelList() {
        const rootPath = widgetText(node, modelRootWidgetName(node)).trim();
        if (!rootPath) {
            flashButton(t("llm.scan.need_path"), "error");
            return;
        }
        flashButton(t("llm.scan.running"));
        const response = await apiGet(`${scanEndpoint(node)}?path=${encodeURIComponent(rootPath)}`);
        if (!response.ok) {
            flashButton(t("llm.scan.unavailable"), "error");
            return;
        }
        const data = response.data ?? {};
        if (data.error) {
            flashButton(t("llm.scan.failed", { error: data.error }), "error");
            return;
        }
        const files = Array.isArray(data.files) ? data.files : [];
        const choices = [];
        for (const item of files) {
            const name = String(item?.name ?? "").trim();
            if (!name) continue;
            const dir = String(item?.dir ?? "").trim().replace(/^[\\/]+|[\\/]+$/g, "");
            choices.push(dir ? `${dir}/${name}` : name);
        }
        if (!choices.length) {
            flashButton(t("llm.scan.empty", { ext: scanExtension(node) }), "error");
            return;
        }
        applyModelChoices(node, choices);
        flashButton(t("llm.scan.found", { count: choices.length }), "ok");
        changed();
    }


    /* ---------------- 云端：只读模型小字（拉取模型已移入「LLM 设置」窗口） ---------------- */

    const cloudModelHintEl = createElement("div", "anima-nl-hint", t("llm.model_hint", { model: t("llm.model_hint.unset") }));
    cloudModelHintEl.style.height = `${HINT_HEIGHT}px`;
    cloudModelHintEl.style.display = "flex";
    cloudModelHintEl.style.alignItems = "center";
    cloudModelHintEl.style.overflow = "hidden";
    cloudModelHintEl.style.whiteSpace = "nowrap";
    cloudModelHintEl.style.textOverflow = "ellipsis";
    cloudModelHintEl.title = t("llm.model_hint_tooltip");
    const cloudModelHintWidget = addBlockWidget(node, "anima_llm_model_hint", cloudModelHintEl, () => HINT_HEIGHT);

    function refreshCloudModelHint() {
        const model = widgetText(node, "cloud_model").trim();
        cloudModelHintEl.textContent = t("llm.model_hint", { model: model || t("llm.model_hint.unset") });
    }

    function writeCloudModel(value) {
        const widget = findWidget(node, "cloud_model");
        if (!widget) return;
        const next = String(value ?? "");
        if (String(widget.value ?? "") === next) return;
        widget.value = next;
        refreshCloudModelHint();
        changed();
    }

    /** 写入云端模型缓存（holder.cloud_models），设置窗口据此渲染模型列表。 */
    function setCloudModelsCache(cache) {
        state = { ...state, cloud_models: normalizeCloudModels(cache) };
        changed();
    }

    /** 供应商兜底：显式 base_url 为空时回退到注册表默认值（F1 修复点）。 */
    function resolveBaseUrl(explicitBaseUrl, provider) {
        const explicit = String(explicitBaseUrl ?? "").trim();
        if (explicit) return explicit;
        const meta = providerMeta(provider);
        return meta && meta.base_url ? meta.base_url : "";
    }

    /* ---------------- 设置按钮 ---------------- */

    const settingsRow = createElement("div", "anima-nl-buttons");
    const settingsButton = createElement("button", "anima-nl-btn is-solid", t("llm.settings"));
    settingsButton.type = "button";
    settingsButton.title = t("llm.settings_tooltip");
    settingsRow.appendChild(settingsButton);
    const settingsWidget = addBlockWidget(node, "anima_llm_settings", settingsRow, () => ACTION_HEIGHT);

    /* ---------------- provider 说明小字 ---------------- */

    const providerHintEl = createElement("div", "anima-nl-hint", t("llm.provider_hint", { label: "—", base_url: "", suffix: "" }));
    providerHintEl.style.height = `${HINT_HEIGHT}px`;
    providerHintEl.style.display = "flex";
    providerHintEl.style.alignItems = "center";
    providerHintEl.style.overflow = "hidden";
    providerHintEl.style.whiteSpace = "nowrap";
    providerHintEl.style.textOverflow = "ellipsis";
    const providerHintWidget = addBlockWidget(node, "anima_llm_provider_hint", providerHintEl, () => HINT_HEIGHT);

    function refreshProviderHint() {
        const providerId = widgetText(node, "provider", "deepseek");
        const meta = providerMeta(providerId);
        const explicitBase = widgetText(node, "base_url").trim();
        const baseUrl = explicitBase || (meta ? meta.base_url : "");
        const label = meta ? meta.label : (providerId || t("llm.provider_hint.unselected"));
        const unverified = meta ? meta.verified === false : true;
        providerHintEl.textContent = t("llm.provider_hint", {
            label,
            base_url: baseUrl || t("llm.provider_hint.custom"),
            suffix: unverified ? t("llm.provider_hint.unverified") : "",
        });
        providerHintEl.title = providerNote(meta);
    }

    function applyProviderTable() {
        const providerWidget = findWidget(node, "provider");
        if (providerWidget) {
            const ids = PROVIDER_CACHE.items.map((item) => item.id);
            if (setComboValues(providerWidget, ids)) node.setDirtyCanvas?.(true, true);
        }
        refreshProviderHint();
    }

    /** 供应商说明：后端 providers 表自带 note 原文，离线降级项用 i18n key。 */
    function providerNote(meta) {
        if (!meta) return "";
        if (meta.note) return String(meta.note);
        if (meta.note_key) return t(meta.note_key);
        return "";
    }

    /** 语言切换 / 首次渲染时刷新节点正面的静态文案。 */
    function refreshLabels() {
        localizeNodeNativeWidgets(node);
        localizeModelType(node);
        localizeModelPlaceholder(node);
        refreshButton.textContent = t("llm.refresh_models");
        refreshButton.title = refreshModelsTooltip(node);
        settingsButton.textContent = t("llm.settings");
        settingsButton.title = t("llm.settings_tooltip");
        cloudModelHintEl.title = t("llm.model_hint_tooltip");
        relocalizeProviderLabels();
        refreshCloudModelHint();
        refreshProviderHint();
    }

    /* ---------------- 显隐规则（契约 §9.4） ---------------- */

    function applyMode() {
        const variant = nodeVariant(node);
        if (variant === "legacy") {
            // 合并版：按 model_type 细分 gguf / safetensors / cloud 三形态
            const flavor = legacyFlavor(node);
            const showLocal = flavor !== "cloud";
            const localNames = !showLocal
                ? []   // 云端形态：本地字段全部隐藏
                : (flavor === "safetensors"
                    ? ["gpu", "model_dir", "model_file", "dtype"]
                    : ["gpu", "model_path", "model_file"]);
            for (const name of ["gpu", "model_path", "model_dir", "model_file", "dtype"]) {
                const widget = findWidget(node, name);
                if (!widget) continue;
                setWidgetHidden(widget, !localNames.includes(name));
            }
            for (const name of CLOUD_WIDGETS) {
                const widget = findWidget(node, name);
                if (!widget) continue;
                setWidgetHidden(widget, showLocal);
            }
            setWidgetHidden(refreshWidget, !showLocal);
            setWidgetHidden(cloudModelHintWidget, showLocal);
            setWidgetHidden(providerHintWidget, showLocal);
            // model_type 恒可见：用户靠它切换本地/云端与 GGUF/safetensors
            setWidgetHidden(findWidget(node, "model_type"), false);
        } else {
            // 拆分节点：字段固定，只显示本变体自己的原生 widget（缺字段自动跳过）
            const keep = new Set(
                variant === "cloud" ? CLOUD_WIDGETS : (VARIANT_LOCAL_WIDGETS[variant] ?? []),
            );
            for (const widget of node.widgets ?? []) {
                if (!widget || widget.serialize === false) continue;
                if (widget.name === HOLDER_NAME) continue;
                setWidgetHidden(widget, !keep.has(widget.name));
            }
            const localVariant = variant !== "cloud";
            setWidgetHidden(refreshWidget, !localVariant);
            setWidgetHidden(cloudModelHintWidget, variant !== "cloud");
            setWidgetHidden(providerHintWidget, variant !== "cloud");
        }
        refreshProviderHint();
        fitNodeHeight(node, MIN_WIDTH);
        node.setDirtyCanvas?.(true, true);
    }

    /* ---------------- provider → base_url 自动填充 ---------------- */

    // 仅内存 flag，不写入 workflow。
    let baseUrlEditedByUser = false;
    let suppressBaseUrlEdit = false;

    function wrapCallback(widget, handler) {
        if (!widget) return;
        const original = widget.callback;
        widget.callback = function (...args) {
            const result = original?.apply(this, args);
            try {
                handler(...args);
            } catch (error) {
                console.error("[nl2prompt] widget 回调处理失败", error);
            }
            return result;
        };
    }

    const providerWidget = findWidget(node, "provider");
    const baseUrlWidget = findWidget(node, "base_url");

    if (baseUrlWidget) {
        wrapCallback(baseUrlWidget, () => {
            if (!suppressBaseUrlEdit) baseUrlEditedByUser = true;
            refreshProviderHint();
            refreshCloudModelHint();
        });
    }

    if (providerWidget) {
        wrapCallback(providerWidget, () => {
            refreshProviderHint();
            const meta = baseUrlEditedByUser ? null : providerMeta(String(providerWidget.value ?? ""));
            if (!meta || !baseUrlWidget) {
                refreshCloudModelHint();
                return;
            }
            const url = meta.base_url;
            if (String(baseUrlWidget.value ?? "") === url) {
                refreshCloudModelHint();
                return;
            }
            suppressBaseUrlEdit = true;
            baseUrlWidget.value = url;
            try {
                baseUrlWidget.callback?.(url);
            } catch (error) {
                console.error("[nl2prompt] base_url 同步失败", error);
            }
            suppressBaseUrlEdit = false;
            refreshProviderHint();
            refreshCloudModelHint();
            changed();
        });
    }

    wrapCallback(findWidget(node, "cloud_model"), () => {
        refreshCloudModelHint();
    });

    wrapCallback(findWidget(node, "model_type"), () => applyMode());

    /* ---------------- 事件 ---------------- */

    refreshButton.addEventListener("pointerdown", blockPointerEvent);
    refreshButton.addEventListener("click", (event) => {
        event.stopPropagation();
        void refreshModelList();
    });

    settingsButton.addEventListener("pointerdown", blockPointerEvent);
    settingsButton.addEventListener("click", (event) => {
        event.stopPropagation();
        openLlmSettings();
    });

    /* ---------------- 排列 ---------------- */

    moveWidgetAfter(node, findWidget(node, "model_dir"), "model_path");
    moveWidgetAfter(node, refreshWidget, "model_file");
    // dtype 只属于 safetensors 形态，排在刷新按钮之后，避免挤在 model_file 与按钮之间
    moveWidgetAfter(node, findWidget(node, "dtype"), refreshWidget);
    moveWidgetAfter(node, cloudModelHintWidget, "cloud_model");
    moveWidgetAfter(node, providerHintWidget, "provider");
    moveWidgetAfter(node, settingsWidget, holder);

    /* ---------------- 执行后回填 last_seed ---------------- */

    function setLastSeed(seed) {
        const value = toNumber(seed, NaN);
        if (!Number.isFinite(value) || value < 0) return;
        state = { ...state, last_seed: Math.trunc(value) };
        changed();
    }

    /* ---------------- LLM 设置窗口（契约 §9.3） ---------------- */

    function openLlmSettings() {
        injectStyles();
        const draft = normalizeState(cloneJson(state) ?? state);
        const cloudMode = !isLocalMode(node);

        // 密钥状态：只在内存中，且绝不回显值
        const secret = {
            loading: true,
            configured: draft.api_key_configured === true,
            source: "none",
            message: "",
        };
        const watchers = new Set();

        function notifySecret() {
            for (const watcher of Array.from(watchers)) {
                try {
                    watcher(secret);
                } catch (error) {
                    console.error("[nl2prompt] 密钥状态刷新失败", error);
                }
            }
        }

        async function refreshSecretStatus() {
            const response = await apiGet("/nl2prompt/secret_status");
            secret.loading = false;
            if (response.ok) {
                secret.configured = response.data?.configured === true;
                const source = String(response.data?.source ?? "none");
                secret.source = ["env", "env_provider", "env_global", "file", "none"].includes(source) ? source : "none";
            } else {
                secret.configured = false;
                secret.source = "none";
                secret.message = t("llm.runtime.secret_unavailable");
            }
            notifySecret();
        }

        // 云端模型：窗口内的编辑副本（保存时才写回 widget）
        const draftCloud = {
            provider: widgetText(node, "provider", "deepseek"),
            base_url: widgetText(node, "base_url"),
            cloud_model: widgetText(node, "cloud_model"),
        };

        /** 拉取模型成功后立即把解析出的 base_url 回填到节点 widget。 */
        function setBaseUrlWidget(value) {
            if (!baseUrlWidget) return;
            const next = String(value ?? "");
            if (String(baseUrlWidget.value ?? "") === next) return;
            suppressBaseUrlEdit = true;
            baseUrlWidget.value = next;
            suppressBaseUrlEdit = false;
            refreshProviderHint();
            changed();
        }

        /** 保存时把云端模型草稿写回节点 widget（provider 优先，base_url 之后覆盖）。 */
        function applyCloudDraft() {            if (providerWidget && String(providerWidget.value ?? "") !== draftCloud.provider) {
                providerWidget.value = draftCloud.provider;
                try {
                    providerWidget.callback?.(draftCloud.provider);
                } catch (error) {
                    console.error("[nl2prompt] provider 回写失败", error);
                }
            }
            if (baseUrlWidget) {
                suppressBaseUrlEdit = true;
                baseUrlWidget.value = draftCloud.base_url;
                suppressBaseUrlEdit = false;
            }
            const modelWidget = findWidget(node, "cloud_model");
            if (modelWidget && String(modelWidget.value ?? "") !== draftCloud.cloud_model) {
                modelWidget.value = draftCloud.cloud_model;
                try {
                    modelWidget.callback?.(draftCloud.cloud_model);
                } catch (error) {
                    console.error("[nl2prompt] cloud_model 回写失败", error);
                }
            }
            const meta = providerMeta(draftCloud.provider);
            const expected = meta ? meta.base_url : "";
            baseUrlEditedByUser = Boolean(String(draftCloud.base_url ?? "").trim())
                && String(draftCloud.base_url ?? "").trim() !== expected;
            refreshProviderHint();
            refreshCloudModelHint();
        }

        // Tab 结构按节点变体裁剪（v3.2）：
        //   legacy        → 采样 / 云端模型（含 api_key）/ 本地设置
        //   cloud         → 采样 / 云端模型（含 api_key）
        //   gguf+safetensors → 采样 / 本地设置
        const variant = nodeVariant(node);
        const hasCloudTab = (variant === "legacy" || variant === "cloud") && Boolean(findWidget(node, "provider"));
        const hasLocalTab = variant !== "cloud";
        const secretDeps = {
            node,
            secret,
            watchSecret: (watcher) => {
                watchers.add(watcher);
                try {
                    watcher(secret);
                } catch (error) {
                    console.error(error);
                }
                return () => watchers.delete(watcher);
            },
            refreshSecretStatus,
        };

        const tabs = [
            {
                label: () => t("llm.tab.sampling"),
                render: (ctx) => buildSamplingTab(ctx, draft, cloudMode),
            },
        ];
        if (hasCloudTab) {
            tabs.push({
                label: () => t("llm.tab.cloud"),
                render: (ctx) => buildCloudTab(ctx, draft, {
                    ...secretDeps,
                    draftCloud,
                    getCloudModels: () => normalizeCloudModels(state.cloud_models),
                    setCloudModels: setCloudModelsCache,
                    writeCloudModel,
                    setBaseUrlWidget,
                    applyProviderTable,
                }),
            });
        }
        if (hasLocalTab) {
            tabs.push({
                label: () => t("llm.tab.local"),
                render: (ctx) => buildLocalTab(ctx, draft, secretDeps),
            });
        }

        const dialog = openDialog({
            title: () => t("llm.window.title"),
            tabs,
            onSave: () => {
                applyCloudDraft();
                draft.api_key_configured = secret.configured === true;
                // api_key 字段根本不在 draft 里：holder 永远不会包含明文。
                state = normalizeState(draft);
                changed();
                return true;
            },
            onClose: () => {
                watchers.clear();
            },
        });

        void refreshSecretStatus();
        return dialog;
    }

    /* ---------------- 对外控制对象 ---------------- */

    node.__nl2promptRefreshLabels = () => refreshLabels();

    node.__animaLlm = {
        getState: () => state,
        applyState(raw) {
            state = normalizeState(readJson(raw));
            refreshCloudModelHint();
        },
        applyMode,
        refreshCloudModelHint,
        setCloudModelsCache,
        resolveBaseUrl,
        applyProviderTable,
        refreshProviderHint,
        refreshModelList,
        setLastSeed,
        openSettings: openLlmSettings,
        holder,
        markBaseUrlEdited(value) {
            baseUrlEditedByUser = value === true;
        },
    };

    seedFallbackProviders();
    refreshLabels();
    applyProviderTable();
    refreshCloudModelHint();
    applyMode();

    void ensureProviderTable().then(() => {
        applyProviderTable();
        refreshCloudModelHint();
    });

    requestAnimationFrame(() => {
        if (node.graph) applyMode();
    });
}

/* ------------------------------------------------------------------ 设置窗口内容 */

function numberField(labelText, options, onCommit) {
    const field = createElement("div", "anima-nl-field");
    field.appendChild(createElement("label", "anima-nl-field-label", labelText));

    const input = createElement("input", "anima-nl-input anima-nl-number");
    input.type = "number";
    if (options.min !== undefined) input.min = String(options.min);
    if (options.max !== undefined) input.max = String(options.max);
    if (options.step !== undefined) input.step = String(options.step);
    input.value = String(options.value ?? 0);
    input.disabled = options.disabled === true;
    input.addEventListener("pointerdown", stopPointerEvent);

    const commit = () => {
        let value = toNumber(input.value, options.value ?? 0);
        if (options.min !== undefined) value = Math.max(options.min, value);
        if (options.max !== undefined) value = Math.min(options.max, value);
        onCommit(value);
    };
    input.addEventListener("input", commit);
    input.addEventListener("change", commit);

    field.appendChild(input);
    if (options.hint) field.appendChild(createElement("div", "anima-nl-hint", options.hint));
    return field;
}

/* ---------------- 设置窗口：云端模型 Tab（v3 F2） ---------------- */

function buildCloudTab(ctx, draft, deps) {
    const { node, draftCloud, getCloudModels, setCloudModels, writeCloudModel, setBaseUrlWidget } = deps;
    const wrap = createElement("div", "anima-nl-field");

    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("llm.cloud.hint"),
    ));

    function resolveBase() {
        const explicit = String(draftCloud.base_url ?? "").trim();
        if (explicit) return explicit;
        const meta = providerMeta(draftCloud.provider);
        return meta && meta.base_url ? meta.base_url : "";
    }

    // ---- 供应商
    const providerField = createElement("div", "anima-nl-field");
    providerField.appendChild(createElement("label", "anima-nl-field-label", t("llm.cloud.provider_label")));
    const providerSelect = createElement("select", "anima-nl-select");
    providerSelect.addEventListener("pointerdown", stopPointerEvent);
    providerField.appendChild(providerSelect);
    const providerHint = createElement("div", "anima-nl-hint", "");
    providerField.appendChild(providerHint);
    wrap.appendChild(providerField);

    function renderProviderOptions() {
        const wanted = draftCloud.provider || providerSelect.value || "deepseek";
        providerSelect.replaceChildren();
        const items = PROVIDER_CACHE.items;
        for (const item of items) {
            const option = createElement("option", null, `${item.label}（${item.id}）`);
            option.value = item.id;
            providerSelect.appendChild(option);
        }
        if (!items.some((item) => item.id === wanted)) {
            const option = createElement("option", null, t("llm.cloud.provider_unknown", { id: wanted || t("llm.provider_hint.unselected") }));
            option.value = wanted || "";
            providerSelect.appendChild(option);
        }
        providerSelect.value = wanted;
    }

    // ---- base_url
    const baseField = createElement("div", "anima-nl-field");
    baseField.appendChild(createElement("label", "anima-nl-field-label", "base_url"));
    const baseInput = createElement("input", "anima-nl-input");
    baseInput.type = "text";
    baseInput.placeholder = t("llm.cloud.base_url_placeholder");
    baseInput.value = draftCloud.base_url ?? "";
    baseInput.addEventListener("pointerdown", stopPointerEvent);
    baseInput.addEventListener("input", () => {
        draftCloud.base_url = baseInput.value;
        renderStatus();
        renderModels();
    });
    baseField.appendChild(baseInput);
    const baseHint = createElement("div", "anima-nl-hint", "");
    baseField.appendChild(baseHint);
    wrap.appendChild(baseField);

    function renderStatus() {
        const meta = providerMeta(draftCloud.provider);
        const label = meta ? meta.label : (draftCloud.provider || t("llm.provider_hint.unselected"));
        const base = resolveBase();
        const unverified = meta ? meta.verified === false : true;
        providerHint.textContent = t("llm.provider_hint", {
            label,
            base_url: base || t("llm.provider_hint.custom"),
            suffix: unverified ? t("llm.provider_hint.unverified") : "",
        });
        providerHint.title = (meta && meta.note) ? String(meta.note) : (meta && meta.note_key ? t(meta.note_key) : "");
        baseHint.textContent = String(draftCloud.base_url ?? "").trim()
            ? t("llm.cloud.base_url_hint_custom")
            : (base ? t("llm.cloud.base_url_hint_fallback", { base_url: base }) : t("llm.cloud.base_url_hint_required"));
    }

    // ---- 拉取模型
    const fetchRow = createElement("div", "anima-nl-field-row");
    const fetchButton = createElement("button", "anima-nl-action is-primary", t("llm.cloud.fetch"));
    fetchButton.type = "button";
    fetchButton.title = t("llm.cloud.fetch_tooltip", { base_url: resolveBase() || "/v1" });
    fetchButton.addEventListener("pointerdown", blockPointerEvent);
    fetchRow.appendChild(fetchButton);
    const fetchStatus = createElement("span", "anima-nl-hint", "");
    fetchStatus.style.flex = "1 1 160px";
    fetchStatus.style.minWidth = "0";
    fetchStatus.style.overflow = "hidden";
    fetchStatus.style.textOverflow = "ellipsis";
    fetchRow.appendChild(fetchStatus);
    wrap.appendChild(fetchRow);

    // ---- 模型列表
    const listBox = createElement("div", "anima-nl-list");
    wrap.appendChild(listBox);

    // ---- cloud_model 手填
    const modelField = createElement("div", "anima-nl-field");
    modelField.appendChild(createElement("label", "anima-nl-field-label", t("llm.cloud.model_label")));
    const modelInput = createElement("input", "anima-nl-input");
    modelInput.type = "text";
    modelInput.placeholder = t("llm.cloud.model_placeholder");
    modelInput.value = draftCloud.cloud_model ?? "";
    modelInput.addEventListener("pointerdown", stopPointerEvent);
    modelInput.addEventListener("input", () => {
        draftCloud.cloud_model = modelInput.value;
        renderModels();
    });
    modelField.appendChild(modelInput);
    modelField.appendChild(createElement("div", "anima-nl-hint", t("llm.cloud.model_hint")));
    wrap.appendChild(modelField);

    function renderModels() {
        const cache = getCloudModels();
        const base = resolveBase();
        const models = cache.models;
        listBox.replaceChildren();

        if (!models.length) {
            listBox.appendChild(createElement("div", "anima-nl-hint", t("llm.cloud.no_models")));
            return;
        }
        if (!cache.base_url || cache.base_url !== base) {
            listBox.appendChild(createElement("div", "anima-nl-hint",
                t("llm.cloud.cache_mismatch", {
                count: models.length,
                base_url: cache.base_url || t("llm.cloud.cache_empty_base"),
            })));
            return;
        }
        if (!isFreshTimestamp(cache.ts, CLOUD_CACHE_TTL_MS)) {
            listBox.appendChild(createElement("div", "anima-nl-hint", t("llm.cloud.cache_expired", { count: models.length })));
        }
        const grid = createElement("div", "anima-nl-model-grid");
        for (const id of models) {
            const item = createElement(
                "button",
                `anima-nl-model-item${id === draftCloud.cloud_model ? " is-active" : ""}`,
                id,
            );
            item.type = "button";
            item.title = t("llm.cloud.item_tooltip", { id });
            item.addEventListener("pointerdown", blockPointerEvent);
            item.addEventListener("click", (event) => {
                event.stopPropagation();
                draftCloud.cloud_model = id;
                modelInput.value = id;
                writeCloudModel(id);
                renderModels();
                ctx.setStatus(t("llm.cloud.item_selected", { id }), "ok");
            });
            grid.appendChild(item);
        }
        listBox.appendChild(grid);
    }

    providerSelect.addEventListener("change", (event) => {
        event.stopPropagation();
        draftCloud.provider = providerSelect.value;
        const meta = providerMeta(draftCloud.provider);
        const url = meta ? meta.base_url : "";
        draftCloud.base_url = url;
        baseInput.value = url;
        renderStatus();
        renderModels();
        ctx.setStatus(t("llm.cloud.provider_switched", { label: meta ? meta.label : draftCloud.provider }), "ok");
    });

    fetchButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        const baseUrl = resolveBase();
        const provider = draftCloud.provider;
        if (!baseUrl) {
            ctx.setStatus(t("llm.cloud.need_base_url"), "error");
            return;
        }
        fetchButton.disabled = true;
        ctx.setStatus(t("llm.cloud.fetching"), "");
        const response = await apiGet(
            `/nl2prompt/list_models?base_url=${encodeURIComponent(baseUrl)}&provider=${encodeURIComponent(provider)}`,
        );
        fetchButton.disabled = false;

        if (!response.ok) {
            ctx.setStatus(t("llm.cloud.fetch_failed", { error: describeError(response) }), "error");
            return;
        }
        const data = response.data ?? {};
        const models = (Array.isArray(data.models) ? data.models : [])
            .map((item) => (typeof item === "string" ? item : item?.id))
            .map((item) => (typeof item === "string" ? item.trim() : ""))
            .filter(Boolean);
        if (!models.length) {
            ctx.setStatus(t("llm.cloud.fetch_empty"), "error");
            return;
        }

        // 解析出的 baseUrl 立即回填（避免下次又空）
        draftCloud.base_url = baseUrl;
        baseInput.value = baseUrl;
        if (typeof setBaseUrlWidget === "function") setBaseUrlWidget(baseUrl);
        setCloudModels({ base_url: baseUrl, models, ts: Date.now() });
        renderStatus();
        renderModels();
        ctx.setStatus(t("llm.cloud.fetch_ok", { count: models.length }), "ok");
    });

    renderProviderOptions();
    renderStatus();
    renderModels();

    // 供应商表可能还在异步加载（/providers），到齐后重建一次选项
    void ensureProviderTable().then(() => {
        renderProviderOptions();
        renderStatus();
    });

    // ---- api_key（v3.2：从「运行设置」搬到这里，与云端配置放在一起）
    wrap.appendChild(createElement("div", "anima-nl-divider"));
    appendSecretSection(wrap, ctx, deps);

    // ---- 测试连接（云端：校验端点与模型）
    wrap.appendChild(createElement("div", "anima-nl-divider"));
    wrap.appendChild(buildTestRow(ctx, node, draft));

    return wrap;
}

function buildSamplingTab(ctx, draft, cloudMode) {
    void ctx;
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("llm.sampling.hint"),
    ));

    const sampling = draft.sampling;

    wrap.appendChild(numberField("Top K（0–100）", {
        min: 0,
        max: 100,
        step: 1,
        value: sampling.top_k,
        disabled: cloudMode,
        hint: cloudMode ? t("llm.sampling.top_k_cloud_hint") : "",
    }, (value) => {
        sampling.top_k = Math.trunc(value);
    }));

    wrap.appendChild(numberField("Top P（0–1）", {
        min: 0,
        max: 1,
        step: 0.01,
        value: sampling.top_p,
    }, (value) => {
        sampling.top_p = value;
    }));

    wrap.appendChild(numberField(t("llm.sampling.temperature"), {
        min: 0,
        max: 2,
        step: 0.01,
        value: sampling.temperature,
    }, (value) => {
        sampling.temperature = value;
    }));

    wrap.appendChild(numberField(t("llm.sampling.max_tokens"), {
        min: 1,
        max: 32768,
        step: 1,
        value: sampling.max_tokens,
    }, (value) => {
        sampling.max_tokens = Math.trunc(value);
    }));

    // ---- 种子：每次随机 / 固定值
    const seedField = createElement("div", "anima-nl-field");
    seedField.appendChild(createElement("div", "anima-nl-field-label", t("llm.sampling.seed")));

    const seedRow = createElement("div", "anima-nl-field-row");
    const randomLabel = createElement("label", "anima-nl-checkbox");
    const randomCheckbox = createElement("input");
    randomCheckbox.type = "checkbox";
    randomCheckbox.checked = sampling.seed < 0;
    randomLabel.append(randomCheckbox, document.createTextNode(t("llm.sampling.seed_random")));

    const seedInput = createElement("input", "anima-nl-input anima-nl-number");
    seedInput.type = "number";
    seedInput.step = "1";
    seedInput.min = "0";
    seedInput.value = String(sampling.seed >= 0 ? Math.trunc(sampling.seed) : 0);
    seedInput.disabled = randomCheckbox.checked;
    seedInput.addEventListener("pointerdown", stopPointerEvent);

    const seedHint = createElement("div", "anima-nl-hint", "");
    const updateSeedHint = () => {
        seedHint.textContent = randomCheckbox.checked
            ? t("llm.sampling.seed_hint_random")
            : t("llm.sampling.seed_hint_fixed", { seed: Math.trunc(toNumber(sampling.seed, 0)) });
    };

    randomCheckbox.addEventListener("pointerdown", stopPointerEvent);
    randomCheckbox.addEventListener("change", () => {
        if (randomCheckbox.checked) {
            sampling.seed = -1;
            seedInput.disabled = true;
        } else {
            const value = Math.max(0, Math.trunc(toNumber(seedInput.value, 0)));
            sampling.seed = value;
            seedInput.value = String(value);
            seedInput.disabled = false;
        }
        updateSeedHint();
    });

    const commitSeed = () => {
        if (randomCheckbox.checked) return;
        const value = Math.max(0, Math.trunc(toNumber(seedInput.value, 0)));
        sampling.seed = value;
    };
    seedInput.addEventListener("input", commitSeed);
    seedInput.addEventListener("change", commitSeed);

    seedRow.append(randomLabel, seedInput);
    seedField.append(seedRow, seedHint);
    updateSeedHint();

    const lastSeedRow = createElement("div", "anima-nl-field-row");
    lastSeedRow.appendChild(createElement("span", "anima-nl-field-label", t("llm.sampling.last_seed")));
    lastSeedRow.appendChild(createElement(
        "span",
        "anima-nl-readonly",
        draft.last_seed >= 0 ? String(Math.trunc(draft.last_seed)) : "-",
    ));
    seedField.appendChild(lastSeedRow);

    wrap.appendChild(seedField);
    return wrap;
}

/**
 * 「测试连接」一行（本地启动 llama-server / 云端校验端点）。本地设置页与云端模型页共用。
 */
function buildTestRow(ctx, node, draft) {
    const testRow = createElement("div", "anima-nl-actions");
    const testButton = createElement("button", "anima-nl-action is-primary", t("llm.runtime.test"));
    testButton.type = "button";
    testButton.addEventListener("pointerdown", blockPointerEvent);
    testRow.appendChild(testButton);
    testRow.appendChild(createElement(
        "span",
        "anima-nl-hint",
        t("llm.runtime.test_tooltip"),
    ));

    testButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        testButton.disabled = true;
        ctx.setStatus(t("llm.runtime.testing"), "");
        const payload = buildLlmModelPayload(node, draft);
        const response = await apiPost("/nl2prompt/runtime/start", payload);
        testButton.disabled = false;
        if (!response.ok) {
            ctx.setStatus(t("llm.runtime.test_failed", { error: describeError(response) }), "error");
            return;
        }
        const data = response.data ?? {};
        const model = data.model ? String(data.model) : t("llm.runtime.test_no_model");
        const baseUrl = data.base_url ? String(data.base_url) : "";
        ctx.setStatus(
            baseUrl
                ? t("llm.runtime.test_ok", { model, base_url: baseUrl })
                : t("llm.runtime.test_ok_no_url", { model }),
            "ok",
        );
    });

    return testRow;
}

/**
 * api_key 区块（v3.2 从「运行设置」搬到「云端模型」）。
 * 密钥只走 POST /nl2prompt/secret，绝不写入 holder / workflow。
 */
function appendSecretSection(wrap, ctx, deps) {
    const { node, secret, watchSecret, refreshSecretStatus } = deps;

    const secretField = createElement("div", "anima-nl-field");
    secretField.appendChild(createElement("div", "anima-nl-field-label", t("llm.runtime.api_key_label")));

    const secretRow = createElement("div", "anima-nl-field-row");
    const secretInput = createElement("input", "anima-nl-input");
    secretInput.type = "password";
    secretInput.autocomplete = "off";
    secretInput.placeholder = t("llm.runtime.api_key_placeholder");
    secretInput.addEventListener("pointerdown", stopPointerEvent);
    secretInput.style.flex = "1 1 220px";
    secretRow.appendChild(secretInput);

    const saveSecretButton = createElement("button", "anima-nl-action", t("llm.runtime.save_key"));
    saveSecretButton.type = "button";
    saveSecretButton.addEventListener("pointerdown", blockPointerEvent);
    secretRow.appendChild(saveSecretButton);
    secretField.appendChild(secretRow);

    const secretStatusEl = createElement("div", "anima-nl-hint", t("llm.runtime.secret_loading"));
    secretField.appendChild(secretStatusEl);
    secretField.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("llm.runtime.api_key_hint"),
    ));

    const renderSecretStatus = (current) => {
        if (current.message) {
            secretStatusEl.textContent = t("llm.runtime.secret_status", { text: current.message });
            return;
        }
        if (current.loading) {
            secretStatusEl.textContent = t("llm.runtime.secret_loading");
            return;
        }
        if (current.configured) {
            const sourceText = String(current.source || "").startsWith("env")
                ? t("llm.runtime.secret_source_env")
                : (current.source === "file" ? t("llm.runtime.secret_source_file") : t("llm.runtime.secret_source_unknown"));
            secretStatusEl.textContent = t("llm.runtime.secret_status", { text: t("llm.runtime.secret_configured", { source: sourceText }) });
        } else {
            secretStatusEl.textContent = t("llm.runtime.secret_status", { text: t("llm.runtime.secret_unconfigured") });
        }
    };

    const unwatch = watchSecret(renderSecretStatus);
    ctx.onClose(unwatch);

    saveSecretButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        const raw = secretInput.value;
        if (!raw.trim()) {
            ctx.setStatus(t("llm.runtime.key_empty"), "error");
            return;
        }
        saveSecretButton.disabled = true;
        ctx.setStatus(t("llm.runtime.key_saving"), "");
        const response = await apiPost("/nl2prompt/secret", {
            api_key: raw,
            provider: widgetText(node, "provider", "deepseek"),
        });
        saveSecretButton.disabled = false;
        if (!response.ok) {
            ctx.setStatus(t("llm.runtime.key_failed", { error: describeError(response) }), "error");
            return;
        }
        secretInput.value = "";
        secret.configured = response.data?.configured === true;
        secret.source = secret.configured ? (secret.source === "none" ? "file" : secret.source) : "none";
        secret.loading = false;
        secret.message = "";
        void refreshSecretStatus();
        ctx.setStatus(secret.configured ? t("llm.runtime.key_saved") : t("llm.runtime.key_cleared"), "ok");
    });

    wrap.appendChild(secretField);
}

/**
 * 本地设置（v3.2 由「运行设置」改名）：只放 llama-server 相关参数。
 * 云端节点不显示这个 Tab（见 openLlmSettings 的 Tab 裁剪）。
 */
function buildLocalTab(ctx, draft, deps) {
    const { node } = deps;
    const wrap = createElement("div", "anima-nl-field");

    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("llm.runtime.hint"),
    ));

    const runtime = draft.runtime;

    wrap.appendChild(numberField(t("llm.runtime.ctx"), {
        min: 512,
        max: 32768,
        step: 256,
        value: runtime.ctx,
    }, (value) => {
        runtime.ctx = Math.trunc(value);
    }));

    wrap.appendChild(numberField(t("llm.runtime.timeout"), {
        min: 10,
        max: 600,
        step: 1,
        value: runtime.timeout,
    }, (value) => {
        runtime.timeout = Math.trunc(value);
    }));

    wrap.appendChild(createElement("div", "anima-nl-divider"));
    wrap.appendChild(buildTestRow(ctx, node, draft));

    return wrap;
}

/* ------------------------------------------------------------------ 扩展注册 */

// 语言切换：重绘所有节点正面（设置窗口由 openDialog 自身订阅）。
subscribeLocale(() => {
    try {
        refreshAllNodeFaces(app);
    } catch (error) {
        console.error("[nl2prompt] 语言切换后重绘节点失败", error);
    }
});

app.registerExtension({
    name: "NL2Prompt.LoadLLM",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_CLASSES.includes(nodeData?.name)) return;

        const originalOnCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnCreated?.apply(this, arguments);
            try {
                installLoadLlm(this);
            } catch (error) {
                console.error("[nl2prompt] 加载LLM UI 安装失败（后端仍可用）", error);
            }
            return result;
        };

        const originalOnConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const result = originalOnConfigure?.apply(this, arguments);
            const controller = this.__animaLlm;
            if (!controller) return result;
            try {
                const raw = readHolderValue(this, info);
                if (raw !== null && raw !== undefined) controller.applyState(raw);

                // base_url 是否被用户手工改过：与 provider 注册表默认值不同即视为手工设置。
                const provider = String(widgetText(this, "provider", "deepseek"));
                const baseUrl = String(widgetText(this, "base_url", "")).trim();
                const meta = providerMeta(provider);
                const expected = meta ? meta.base_url : "";
                controller.markBaseUrlEdited(Boolean(baseUrl) && baseUrl !== expected);

                controller.applyProviderTable();
                controller.refreshCloudModelHint();
                controller.applyMode();

                // 重启后 COMBO 的 options 只剩占位项，而工作流里存着上次选的模型名，
                // 提交时会被 ComfyUI 的 COMBO 校验拦下（表现为「模型不存在」）。
                // 先把当前值塞进 options 保证能提交，再异步补扫出完整列表。
                keepCurrentModelChoice(this);
                void ensureModelChoices(this);
            } catch (error) {
                console.error("[nl2prompt] 读取 _llm_json 失败，使用默认值", error);
            }
            return result;
        };

        const originalOnExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const result = originalOnExecuted?.apply(this, arguments);
            const controller = this.__animaLlm;
            if (!controller) return result;
            try {
                const payload = Array.isArray(message?.anima_llm) ? message.anima_llm[0] : message?.anima_llm;
                const seed = typeof payload === "number" ? payload : payload?.seed;
                if (seed !== undefined && seed !== null) controller.setLastSeed(seed);
            } catch (error) {
                console.error("[nl2prompt] 读取执行结果失败", error);
            }
            return result;
        };
    },
});

/** 按名称取 holder 值；回落到 widgets_values 数组时按「序列化 widget 顺序」定位（与框架恢复槽位一致）。 */
function readHolderValue(node, info) {
    const named = info && typeof info === "object" ? info.widgets_values_named : null;
    if (named && typeof named === "object" && Object.prototype.hasOwnProperty.call(named, HOLDER_NAME)) {
        return named[HOLDER_NAME];
    }
    const values = info?.widgets_values;
    if (Array.isArray(values)) {
        const serialized = (node?.widgets ?? []).filter((widget) => widget.serialize !== false);
        const index = serialized.findIndex((widget) => widget.name === HOLDER_NAME);
        if (index >= 0 && index < values.length && readJson(values[index])) return values[index];
    }
    const holderValue = findWidget(node, HOLDER_NAME)?.value;
    if (typeof holderValue === "string" && holderValue.trim()) return holderValue;
    return null;
}
