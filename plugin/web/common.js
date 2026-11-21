// ComfyUI 自然语言提示词 —— 前端公共库。
//
// 契约：docs/接口契约.md §9（前端契约）
// 目标环境：ComfyUI 0.45.3 / 前端 1.58.5。
// 只使用已验证的公开 API：
//   * app.registerExtension / beforeRegisterNodeDef
//   * node.addDOMWidget(name, type, element, options)
//   * node.widgets 数组操作、node.setDirtyCanvas、node.setSize
//   * api.fetchApi
//
// 本文件只导出工具函数，不注册扩展；即使被 ComfyUI 当作扩展脚本加载也无副作用。

import { api } from "../../scripts/api.js";
import { currentLocale, subscribeLocale, t } from "./i18n.js";

export const NODE_MIN_WIDTH = 400;

const STYLE_ID = "anima-nl-styles";

const STYLES = `
.anima-nl-root {
    --anima-nl-text: var(--input-text, var(--fg-color, #e5e7eb));
    --anima-nl-muted: color-mix(in srgb, var(--anima-nl-text) 55%, transparent);
    --anima-nl-line: color-mix(in srgb, var(--anima-nl-text) 16%, transparent);
    --anima-nl-field: var(--comfy-input-bg, rgba(127, 127, 127, 0.12));
    --anima-nl-accent: var(--primary-background, var(--p-primary-color, #31b9f4));
    --anima-nl-hover: color-mix(in srgb, var(--anima-nl-text) 8%, transparent);
    --anima-nl-danger: #f87171;
    box-sizing: border-box;
    color: var(--anima-nl-text);
    font-family: inherit;
    font-size: 12px;
    line-height: 1.4;
    pointer-events: auto;
}
.anima-nl-root *, .anima-nl-root *::before, .anima-nl-root *::after {
    box-sizing: border-box;
}

/* ---------- 折叠头 ---------- */
.anima-nl-head {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    height: 100%;
    padding: 0 8px;
    border: 1px solid var(--anima-nl-line);
    border-radius: 6px;
    background: color-mix(in srgb, var(--anima-nl-text) 4%, transparent);
    color: var(--anima-nl-text);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    user-select: none;
}
.anima-nl-head:hover {
    border-color: color-mix(in srgb, var(--anima-nl-accent) 60%, transparent);
}
.anima-nl-head-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.anima-nl-caret { margin-left: auto; color: var(--anima-nl-muted); font-size: 10px; }

/* ---------- 行 ---------- */
.anima-nl-block { display: flex; flex-direction: column; gap: 4px; width: 100%; overflow: hidden; }
.anima-nl-row { display: grid; gap: 4px; align-items: center; height: 26px; }
.anima-nl-row.is-character { grid-template-columns: 1fr 1fr 1fr 24px; }
.anima-nl-row.is-style { grid-template-columns: 1fr 1fr 24px; }
.anima-nl-row.is-textmap { grid-template-columns: 1fr 1.4fr 110px 24px; height: 26px; }
.anima-nl-list { display: flex; flex-direction: column; gap: 10px; }
.anima-nl-rule { display: flex; flex-direction: column; gap: 3px; }

.anima-nl-input, .anima-nl-select, .anima-nl-textarea {
    width: 100%;
    border: 1px solid var(--anima-nl-line);
    border-radius: 5px;
    background: var(--anima-nl-field);
    color: var(--anima-nl-text);
    font-family: inherit;
    font-size: 12px;
    outline: none;
}
.anima-nl-input { height: 24px; padding: 0 6px; }
.anima-nl-input::placeholder { color: var(--anima-nl-muted); }
.anima-nl-input:focus, .anima-nl-textarea:focus, .anima-nl-select:focus {
    border-color: color-mix(in srgb, var(--anima-nl-accent) 70%, transparent);
}
.anima-nl-input:disabled { opacity: 0.55; }
.anima-nl-select { height: 24px; padding: 0 4px; }
.anima-nl-textarea {
    padding: 6px 8px;
    min-height: 160px;
    resize: vertical;
    line-height: 1.45;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    white-space: pre-wrap;
}
.anima-nl-number { max-width: 160px; }
.anima-nl-hint { color: var(--anima-nl-muted); font-size: 11px; line-height: 1.4; }
.anima-nl-icon {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 1px solid transparent;
    border-radius: 5px;
    background: transparent;
    color: var(--anima-nl-muted);
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
}
.anima-nl-icon:hover {
    border-color: color-mix(in srgb, var(--anima-nl-danger) 60%, transparent);
    background: color-mix(in srgb, var(--anima-nl-danger) 12%, transparent);
    color: var(--anima-nl-danger);
}

/* ---------- 按钮 ---------- */
.anima-nl-buttons { display: flex; gap: 4px; width: 100%; height: 100%; }
.anima-nl-btn {
    flex: 1 1 0;
    min-width: 0;
    height: 28px;
    padding: 0 8px;
    border: 1px dashed color-mix(in srgb, var(--anima-nl-text) 26%, transparent);
    border-radius: 6px;
    background: color-mix(in srgb, var(--anima-nl-text) 3%, transparent);
    color: var(--anima-nl-muted);
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
}
.anima-nl-btn:hover {
    border-color: color-mix(in srgb, var(--anima-nl-accent) 70%, transparent);
    background: color-mix(in srgb, var(--anima-nl-accent) 10%, transparent);
    color: var(--anima-nl-text);
}
.anima-nl-btn.is-solid {
    border-style: solid;
    border-color: var(--anima-nl-line);
    background: color-mix(in srgb, var(--anima-nl-text) 6%, transparent);
    color: var(--anima-nl-text);
}
.anima-nl-btn.is-error { color: var(--anima-nl-danger); border-color: color-mix(in srgb, var(--anima-nl-danger) 60%, transparent); }
.anima-nl-btn.is-ok { color: #4ade80; border-color: rgba(74, 222, 128, 0.6); }

.anima-nl-seed { display: flex; align-items: center; width: 100%; height: 100%; color: var(--anima-nl-muted); font-size: 11px; }

/* ---------- 设置窗口 ---------- */
.anima-nl-overlay {
    position: fixed;
    inset: 0;
    z-index: 10000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: rgba(0, 0, 0, 0.52);
}
.anima-nl-dialog {
    display: flex;
    flex-direction: column;
    width: min(780px, 94vw);
    max-height: min(820px, 88vh);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    background: var(--comfy-menu-bg, #202020);
    color: var(--fg-color, #ddd);
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.55);
    overflow: hidden;
    outline: none;
}
.anima-nl-dialog-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.anima-nl-dialog-title { flex: 1; min-width: 0; font-size: 13px; font-weight: 700; }
.anima-nl-dialog-close {
    width: 26px;
    height: 26px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 6px;
    background: transparent;
    color: inherit;
    font-family: inherit;
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
}
.anima-nl-dialog-close:hover { background: rgba(248, 113, 113, 0.18); border-color: rgba(248, 113, 113, 0.6); }
.anima-nl-tabs { display: flex; gap: 4px; padding: 8px 14px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
.anima-nl-tab {
    height: 28px;
    padding: 0 12px;
    border: 1px solid transparent;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    background: transparent;
    color: inherit;
    opacity: 0.7;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
}
.anima-nl-tab:hover { opacity: 1; background: rgba(255, 255, 255, 0.05); }
.anima-nl-tab.is-active {
    opacity: 1;
    background: rgba(255, 255, 255, 0.09);
    border-color: rgba(255, 255, 255, 0.12);
    font-weight: 600;
}
.anima-nl-tabpanels { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 14px; }
.anima-nl-tabpanel { display: none; flex-direction: column; gap: 10px; }
.anima-nl-tabpanel.is-active { display: flex; }
.anima-nl-field { display: flex; flex-direction: column; gap: 5px; }
.anima-nl-field-label { font-size: 12px; font-weight: 600; }
.anima-nl-field-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.anima-nl-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.anima-nl-checkbox { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; cursor: pointer; }
.anima-nl-readonly {
    padding: 2px 8px;
    border: 1px solid var(--anima-nl-line);
    border-radius: 5px;
    color: var(--anima-nl-text);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 12px;
}
.anima-nl-dialog-foot {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.anima-nl-status { flex: 1; min-width: 0; font-size: 11px; color: #9aa0a6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.anima-nl-status.is-error { color: #f87171; }
.anima-nl-status.is-ok { color: #4ade80; }
.anima-nl-action {
    height: 28px;
    padding: 0 14px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.06);
    color: inherit;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
}
.anima-nl-action:hover { background: rgba(255, 255, 255, 0.12); }
.anima-nl-action.is-primary {
    border-color: var(--primary-background, #31b9f4);
    background: color-mix(in srgb, var(--primary-background, #31b9f4) 28%, transparent);
}
.anima-nl-action:disabled { opacity: 0.55; cursor: default; }
.anima-nl-divider { height: 1px; background: rgba(255, 255, 255, 0.08); margin: 2px 0; }

/* v5：设置面板里的开关行（提示词优化） */
.anima-nl-switch-row { display: flex; align-items: center; gap: 8px; }
.anima-nl-switch-row input[type="checkbox"] {
    width: 15px;
    height: 15px;
    margin: 0;
    flex: 0 0 auto;
    cursor: pointer;
    accent-color: var(--anima-nl-accent);
}
.anima-nl-switch-row label { cursor: pointer; font-size: 12px; font-weight: 600; }
.anima-nl-switch-warn { color: #f59e0b; font-size: 11px; line-height: 1.4; }
.anima-nl-hint.is-error { color: #f87171; }
.anima-nl-hint.is-ok { color: #4ade80; }
.anima-nl-fetch { display: flex; flex-direction: column; gap: 4px; width: 100%; overflow: hidden; }
.anima-nl-dialog-entry {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px;
    border: 1px solid var(--anima-nl-line);
    border-radius: 6px;
    background: color-mix(in srgb, var(--anima-nl-text) 3%, transparent);
}
.anima-nl-dialog-entry-head { font-size: 12px; font-weight: 600; }
.anima-nl-dialog-entry-body {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 11px;
    color: var(--anima-nl-muted);
    word-break: break-word;
}
.anima-nl-model-grid { display: flex; flex-wrap: wrap; gap: 4px; }
.anima-nl-model-item {
    max-width: 100%;
    padding: 3px 8px;
    border: 1px solid var(--anima-nl-line);
    border-radius: 5px;
    background: color-mix(in srgb, var(--anima-nl-text) 4%, transparent);
    color: var(--anima-nl-text);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 11px;
    cursor: pointer;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.anima-nl-model-item:hover {
    border-color: color-mix(in srgb, var(--anima-nl-accent) 70%, transparent);
    background: color-mix(in srgb, var(--anima-nl-accent) 12%, transparent);
}
.anima-nl-model-item.is-active {
    border-color: var(--anima-nl-accent);
    background: color-mix(in srgb, var(--anima-nl-accent) 24%, transparent);
    font-weight: 600;
}
`;

/** 注入全局样式（只一次）。 */
export function injectStyles() {
    if (typeof document === "undefined") return;
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = STYLES;
    document.head.appendChild(style);
}

/* ------------------------------------------------------------------ DOM 工具 */

/** 创建元素：createElement("input", "anima-nl-input", "文本")。 */
export function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
}

/** 阻止画布拖拽，但保留输入框聚焦/文本选择（不 preventDefault）。 */
export function stopPointerEvent(event) {
    if (!event) return;
    event.stopPropagation();
}

/** 阻止画布拖拽，并阻止默认行为（用于按钮，避免抢焦点）。 */
export function blockPointerEvent(event) {
    if (!event) return;
    event.preventDefault();
    event.stopPropagation();
}

/** 按名称查找 widget（不依赖数组下标）。 */
export function findWidget(node, name) {
    return node?.widgets?.find((widget) => widget.name === name) ?? null;
}

/** 隐藏/显示 widget（1.58.5 的 widget.hidden 会进入布局过滤）。 */
export function setWidgetHidden(widget, hidden) {
    if (!widget) return;
    widget.hidden = hidden === true;
}

/**
 * 把一组自有 widget 依次插到锚点 widget 之后，其余 widget 相对顺序不变。
 * 用于「按契约顺序」重排 node.widgets。
 */
export function arrangeWidgets(node, anchor, ordered) {
    if (!node?.widgets) return;
    const list = node.widgets;
    const anchorWidget = typeof anchor === "string" ? findWidget(node, anchor) : (anchor ?? null);
    const own = (ordered ?? []).filter((widget) => widget && list.includes(widget));
    if (!own.length) return;

    const anchorIndex = anchorWidget ? list.indexOf(anchorWidget) : -1;
    if (anchorIndex < 0) {
        const others = list.filter((widget) => !own.includes(widget));
        const next = [...others, ...own];
        if (next.some((widget, index) => list[index] !== widget)) list.splice(0, list.length, ...next);
        return;
    }

    const before = list.slice(0, anchorIndex + 1);
    const after = list.slice(anchorIndex + 1).filter((widget) => !own.includes(widget));
    const next = [...before, ...own, ...after];
    if (next.some((widget, index) => list[index] !== widget)) list.splice(0, list.length, ...next);
}

/** 把 widget 移动到锚点（名称或 widget）之后。 */
export function moveWidgetAfter(node, widget, anchor) {
    if (!node?.widgets || !widget) return;
    const list = node.widgets;
    const target = typeof anchor === "string" ? findWidget(node, anchor) : (anchor ?? null);
    if (!target || target === widget) return;
    const from = list.indexOf(widget);
    if (from < 0) return;
    list.splice(from, 1);
    const at = list.indexOf(target);
    list.splice(at + 1, 0, widget);
}

/** 让节点高度贴合内容；只在需要时调用 setSize。 */
export function fitNodeHeight(node, minWidth = NODE_MIN_WIDTH) {
    if (!node?.computeSize) return;
    let size = null;
    try {
        size = node.computeSize();
    } catch (error) {
        // 节点尚未挂到 graph 时 computeSize 会抛错，忽略即可。
        return;
    }
    if (!Array.isArray(size) || !Number.isFinite(size[1])) return;
    const width = Math.max(node.size?.[0] ?? 0, minWidth);
    const height = Math.max(size[1], 1);
    if (Math.abs((node.size?.[1] ?? 0) - height) > 1 || Math.abs((node.size?.[0] ?? 0) - width) > 1) {
        node.setSize?.([width, height]);
    }
    node.setDirtyCanvas?.(true, true);
}

/* ------------------------------------------------------------------ 数据工具 */

/** 解析 holder 值（兼容字符串与对象）；失败返回 null。 */
export function readJson(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === "object") return value;
    const text = String(value).trim();
    if (!text) return null;
    try {
        const parsed = JSON.parse(text);
        return parsed && typeof parsed === "object" ? parsed : null;
    } catch (_error) {
        return null;
    }
}

/** 纯 JSON 数据的深拷贝。 */
export function cloneJson(value) {
    try {
        return JSON.parse(JSON.stringify(value === undefined ? null : value));
    } catch (_error) {
        return null;
    }
}

/** 数字容错。 */
export function toNumber(value, fallback) {
    const parsed = typeof value === "number" ? value : Number.parseFloat(String(value ?? "").trim());
    return Number.isFinite(parsed) ? parsed : fallback;
}

let idCounter = 0;

/** 生成行 id。 */
export function uid(prefix = "id") {
    idCounter += 1;
    return `${prefix}${Date.now().toString(36)}${idCounter.toString(36)}`;
}

/** widget 的字符串值（widget 不存在返回 fallback）。 */
export function widgetText(node, name, fallback = "") {
    const widget = findWidget(node, name);
    const value = widget === null || widget === undefined ? fallback : widget.value;
    return typeof value === "string" ? value : String(value ?? "");
}

/** 取 widget 的数值。 */
export function widgetNumber(node, name, fallback) {
    return toNumber(widgetText(node, name, String(fallback ?? "")), fallback);
}

/** 安全更新 COMBO widget 的选项列表；保持当前值不动。返回是否发生了改动。 */
export function setComboValues(widget, values) {
    if (!widget) return false;
    const list = Array.isArray(values) ? values.filter((value) => typeof value === "string" && value) : [];
    if (!list.length) return false;
    if (!widget.options || typeof widget.options !== "object") widget.options = {};
    const current = widget.options.values;
    const same = Array.isArray(current) && current.length === list.length && current.every((entry, index) => entry === list[index]);
    if (same) return false;
    widget.options.values = list;
    return true;
}

/** 截断文本（节点小字 / 历史列表用）。 */
export function truncateText(text, max = 40) {
    const value = String(text === null || text === undefined ? "" : text).replace(/\s+/g, " ").trim();
    if (!value) return "";
    if (!Number.isFinite(max) || max <= 0) return value;
    return value.length > max ? `${value.slice(0, max)}…` : value;
}

/** 时间戳归一化成毫秒（兼容秒 / 毫秒 / ISO 字符串）。 */
export function toTimestampMs(value) {
    if (value === null || value === undefined || value === "") return 0;
    if (typeof value === "string") {
        const trimmed = value.trim();
        if (!trimmed) return 0;
        const parsed = Date.parse(trimmed);
        if (Number.isFinite(parsed)) return parsed;
        const numeric = Number(trimmed);
        return Number.isFinite(numeric) ? toTimestampMs(numeric) : 0;
    }
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return 0;
    return numeric < 1e12 ? Math.round(numeric * 1000) : Math.round(numeric);
}

/** 时间戳 → `MM-DD HH:mm`；无效返回「—」。 */
export function formatTimestamp(value) {
    const ms = toTimestampMs(value);
    if (!ms) return "—";
    const date = new Date(ms);
    if (Number.isNaN(date.getTime())) return "—";
    const pad = (number) => String(number).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 时间戳是否在 TTL 内。 */
export function isFreshTimestamp(value, ttlMs) {
    const ms = toTimestampMs(value);
    if (!ms) return false;
    const ttl = Number.isFinite(ttlMs) ? ttlMs : 0;
    return Date.now() - ms <= ttl;
}

/** 从 `"0: NVIDIA GeForce RTX 3060 (12288 MiB)"` 解析出 index 与名字。 */
export function parseGpuText(text) {
    const raw = String(text === null || text === undefined ? "" : text).trim();
    const index = toNumber(raw.split(":")[0], 0);
    const name = raw
        .replace(/^\s*\d+\s*:\s*/, "")
        .replace(/\s*\([^)]*MiB\)\s*$/i, "")
        .trim();
    return {
        gpu_index: Number.isFinite(index) ? Math.trunc(index) : 0,
        gpu_name: name || raw,
    };
}

/** 用「模型根目录 + 模型文件」拼路径（后端 `Path(root) / file` 同语义）。 */
export function joinModelPath(rootPath, fileName) {
    const root = String(rootPath === null || rootPath === undefined ? "" : rootPath).trim().replace(/[\\/]+$/, "");
    const file = String(fileName === null || fileName === undefined ? "" : fileName).trim().replace(/^[\\/]+/, "");
    if (!root) return file;
    if (!file) return root;
    return `${root}/${file}`;
}

/** 图上所有节点（兼容 `_nodes` 数组 / `nodes` 字典 / `_nodes_by_id`）。 */
export function graphNodes(graph) {
    if (!graph) return [];
    if (Array.isArray(graph._nodes)) return graph._nodes.filter(Boolean);
    if (graph.nodes && typeof graph.nodes === "object") return Object.values(graph.nodes).filter(Boolean);
    if (graph._nodes_by_id && typeof graph._nodes_by_id === "object") return Object.values(graph._nodes_by_id).filter(Boolean);
    return [];
}

/**
 * 原生 widget（后端 INPUT_TYPES 声明的那些）的显示文案本地化。
 *
 * 实测 ComfyUI 1.58（前端 1.58.5）：
 *   * BOOLEAN 的文案存在 `widget.options.on` / `widget.options.off`（后端 label_on/label_off 会被框架
 *     改写成 "true"/"false"，不再可用），所以两边都写，兼容不同渲染路径；
 *   * 后端 `tooltip` 不会传到 widget 上（实测恒为空串），因此这里直接用字典文案补一条提示，
 *     中文界面用中文、英文界面用英文。
 *
 * **绝不碰 `widget.value`**：COMBO / BOOLEAN 的取值属于后端契约。
 *
 * @param {object} widget LiteGraph widget
 * @param {{tooltipKey?:string, labelOnKey?:string, labelOffKey?:string}} spec
 */
export function localizeNativeWidget(widget, spec) {
    if (!widget || !spec) return;
    if (spec.tooltipKey) {
        const text = t(spec.tooltipKey);
        widget.tooltip = text;
        if (widget.options) widget.options.tooltip = text;
    }
    if ((spec.labelOnKey || spec.labelOffKey) && widget.options) {
        const options = widget.options;
        if (widget.__nl2promptOrigOn === undefined) widget.__nl2promptOrigOn = options.on ?? options.label_on;
        if (widget.__nl2promptOrigOff === undefined) widget.__nl2promptOrigOff = options.off ?? options.label_off;
        const onText = spec.labelOnKey ? t(spec.labelOnKey) : widget.__nl2promptOrigOn;
        const offText = spec.labelOffKey ? t(spec.labelOffKey) : widget.__nl2promptOrigOff;
        if (spec.labelOnKey) {
            options.on = onText;
            options.label_on = onText;
        }
        if (spec.labelOffKey) {
            options.off = offText;
            options.label_off = offText;
        }
    }
}

/**
 * 左侧「节点」面板（节点库）里的显示名。
 *
 * 探测结论（ComfyUI 1.58 + frontend 1.58.5）：
 *   * 注册表 `LiteGraph.registered_node_types[type].title` / `.nodeData.display_name` 背书名；
 *   * 节点库列表真正渲染的是 pinia `nodeDef` store 的 `nodeDefsByName[type].display_name`
 *     （改注册表 title 不会刷新已渲染的列表，必须同时改 store）。
 *
 * 中文用节点注册时读到的后端原名（首次调用时记录），英文用 `node.title.*` 字典。
 * 只改显示名，不重新注册节点类型，不碰 NODE_DISPLAY_NAME_MAPPINGS。
 */
export const NODE_TITLE_KEYS = {
    NL2PromptComposer: "node.title.composer",
    AnimaNLComposer: "node.title.composer_legacy",
    NL2PromptLLMGGUF: "node.title.llm_gguf",
    NL2PromptLLMSafetensors: "node.title.llm_safetensors",
    NL2PromptLLMCloud: "node.title.llm_cloud",
    NL2PromptLoadLLM: "node.title.llm_legacy",
    AnimaLoadLLM: "node.title.llm_legacy_v2",
    NL2PromptLoRAStack: "node.title.lora_stack",
    AnimaLoRAStackLoader: "node.title.lora_stack_legacy",
};

/**
 * 节点描述（鼠标悬停节点标题时出现的 tooltip）。
 *
 * 后端 `DESCRIPTION` 是中文硬编码，前端不能翻译，因此这里按语言覆盖：
 * 与 `display_name` 同理，`ComfyNodeDefImpl.description` 是派生自
 * `backendDescription` 的 getter，注册表侧则要改 `nodeData.description`。
 */
export const NODE_DESC_KEYS = {
    NL2PromptComposer: "node.desc.composer",
    AnimaNLComposer: "node.desc.composer_legacy",
    NL2PromptLLMGGUF: "node.desc.llm_gguf",
    NL2PromptLLMSafetensors: "node.desc.llm_safetensors",
    NL2PromptLLMCloud: "node.desc.llm_cloud",
    NL2PromptLoadLLM: "node.desc.llm_legacy",
    AnimaLoadLLM: "node.desc.llm_legacy_v2",
    NL2PromptLoRAStack: "node.desc.lora_stack",
    AnimaLoRAStackLoader: "node.desc.lora_stack_legacy",
};

/** 取节点库 store 的 `nodeDefsByName`（拿不到就返回 null，后续只改注册表）。 */
function nodeDefsByName() {
    const pinia = globalThis.app?.extensionManager?._p;
    try {
        const store = pinia?._s?.get?.("nodeDef");
        if (store && store.nodeDefsByName) return store.nodeDefsByName;
    } catch (_error) {
        // 内部结构变化时静默回落
    }
    try {
        const defs = pinia?.state?.value?.nodeDef?.nodeDefsByName;
        if (defs) return defs;
    } catch (_error) {
        // 同上
    }
    return null;
}

// 调试钩子：探针脚本/控制台可直接调用，验证节点栏显示名来源
globalThis.__nl2promptNodeTitles = {
    apply: () => applyNodeDisplayNames(),
    defs: () => nodeDefsByName(),
    rerender: () => forceNodeLibraryRerender(),
};

/** 记录后端的原始显示名（中文），供切回中文时还原。用 WeakMap，避免污染响应式对象。 */
const ORIG_TITLES = new WeakMap();

/**
 * 强制节点库（左侧「节点」面板）重新渲染。
 *
 * 为什么必须有这一步：改完 `def.backendDisplayName` 后，`def.display_name` getter
 * 会立即返回新值（数据层已对），但**已挂载的 Vue 列表不会重渲染**——模板里读的是
 * 响应式依赖，而 `backendDisplayName` 不在 ComfyNodeDefImpl 的响应式字段里，
 * 所以界面要刷新页面才变。这里显式踢一次，三级手段任一生效即可：
 *   ① pinia store 浅拷贝赋值（`nodeDefsByName` 若是普通 state）
 *   ② `store.$patch`（同上，pinia 标准写法）
 *   ③ 组件树 `$forceUpdate`（兜底，直接让列表重跑一遍渲染函数）
 */
function forceNodeLibraryRerender() {
    try {
        const pinia = globalThis.app?.extensionManager?._p;
        const store = pinia?._s?.get?.("nodeDef");
        const current = store?.nodeDefsByName;
        if (current) {
            const copy = { ...current };
            let assigned = false;
            try {
                store.nodeDefsByName = copy;
                assigned = store.nodeDefsByName === copy;
            } catch (_error) {
                assigned = false;
            }
            if (!assigned && typeof store.$patch === "function") {
                try {
                    store.$patch({ nodeDefsByName: copy });
                } catch (_error) {
                    /* computed / 只读时忽略，交给 ③ */
                }
            }
        }
    } catch (_error) {
        /* 内部结构变化时静默回落 */
    }

    try {
        const roots = document.querySelectorAll('[class*="node-library"], [class*="sidebar"]');
        for (const el of roots) {
            let comp = el.__vueParentComponent;
            let depth = 0;
            while (comp && depth < 12) {
                try {
                    comp.proxy?.$forceUpdate?.();
                } catch (_error) {
                    /* 单个组件失败不影响其它 */
                }
                comp = comp.parent;
                depth += 1;
            }
        }
    } catch (_error) {
        /* 拿不到组件树时静默 */
    }
}

/** 把节点栏显示名与节点描述切到当前语言（幂等；中英来回切都安全）。 */
export function applyNodeDisplayNames() {
    const registry = globalThis.LiteGraph?.registered_node_types ?? null;
    const defs = nodeDefsByName();
    const useEnglish = currentLocale() === "en";
    let touchedStore = false;
    for (const [typeName, key] of Object.entries(NODE_TITLE_KEYS)) {
        const reg = registry ? registry[typeName] : null;
        const def = defs ? defs[typeName] : null;
        if (!reg && !def) continue;

        let orig = ORIG_TITLES.get(reg ?? def);
        if (!orig) {
            orig = {
                title: reg ? reg.title : "",
                nodeDisplay: reg && reg.nodeData ? reg.nodeData.display_name : "",
                backendDisplay: def ? (def.backendDisplayName ?? "") : "",
                description: reg && reg.nodeData ? (reg.nodeData.description ?? "") : "",
            };
            ORIG_TITLES.set(reg ?? def, orig);
        }
        const zhText = orig.title || orig.nodeDisplay || orig.backendDisplay || t(key);
        const text = useEnglish ? t(key) : zhText;

        // ① 注册表：节点创建时用的是 title / nodeData.display_name
        if (reg) {
            try {
                reg.title = text;
                if (reg.nodeData) reg.nodeData.display_name = text;
            } catch (error) {
                console.error("[nl2prompt] 注册表节点名改写失败", error);
            }
        }
        // ② 节点库 store：ComfyNodeDefImpl.display_name 是只读 getter
        //    （getter = i18n('display_name', name, backendDisplayName)），只能改 backendDisplayName。
        if (def) {
            try {
                if (def.backendDisplayName !== text) {
                    def.backendDisplayName = text;
                    touchedStore = true;
                }
            } catch (error) {
                try {
                    if (def.display_name !== text) {
                        def.display_name = text;
                        touchedStore = true;
                    }
                } catch (inner) {
                    console.error("[nl2prompt] 节点库显示名改写失败", inner || error);
                }
            }
        }

        // ③ 节点描述（悬停 tooltip）：后端 DESCRIPTION 是中文硬编码，按语言覆盖
        const descKey = NODE_DESC_KEYS[typeName];
        if (!descKey) continue;
        const descText = useEnglish ? t(descKey) : (orig.description || t(descKey));
        if (reg) {
            try {
                if (reg.description !== descText) reg.description = descText;
            } catch (error) {
                console.error("[nl2prompt] 注册表节点描述改写失败", error);
            }
            // `nodeData` 本身就是 ComfyNodeDefImpl，其 `description` 是原型上的
            // 只读 getter（无 setter），直接赋值会静默失败，只能 defineProperty 覆盖。
            const nodeData = reg.nodeData;
            if (nodeData) {
                try {
                    if (nodeData.description !== descText) {
                        Object.defineProperty(nodeData, "description", {
                            value: descText,
                            writable: true,
                            configurable: true,
                        });
                    }
                } catch (error) {
                    console.error("[nl2prompt] nodeData 描述改写失败", error);
                }
            }
        }
        if (def) {
            try {
                if (def.backendDescription !== descText) {
                    def.backendDescription = descText;
                    touchedStore = true;
                }
            } catch (error) {
                console.error("[nl2prompt] 节点库描述改写失败", error);
            }
        }
    }
    // ④ 数据改了但 Vue 未必感知，踢一次重渲染（否则节点栏要刷新页面才变）
    if (touchedStore) forceNodeLibraryRerender();
}

/**
 * 语言切换后刷新图上所有节点正面的文案：调用节点控制器挂上的
 * `__nl2promptRefreshLabels()`，再重绘画布（不重建节点、不丢状态）。
 */
export function refreshAllNodeFaces(app) {
    for (const node of graphNodes(app ? app.graph : null)) {
        try {
            if (typeof node.__nl2promptRefreshLabels === "function") node.__nl2promptRefreshLabels();
        } catch (error) {
            console.error("[nl2prompt] 节点正面文案刷新失败", error);
        }
        try {
            if (typeof node.setDirtyCanvas === "function") node.setDirtyCanvas(true, true);
        } catch (error) {
            /* 画布重绘失败只影响观感 */
        }
    }
}

/**
 * `model_type` 的三个选项，中英各一份。
 * 必须与后端 `nodes.py` 的 COMBO 选项对应——后端按特征判断
 * （含 `safetensors` → safetensors 分支；以「云」或 `cloud` 开头 → cloud），
 * 因此中英两种写法都认，值随语言切换不会破坏工作流。
 */
const MODEL_TYPE_ZH = ["本地模型", "本地模型（safetensors）", "云端模型"];
const MODEL_TYPE_EN = ["Local model", "Local model (safetensors)", "Cloud"];

/** `model_type` 是否表示「云端」（中英选项都认，与后端判断一致）。 */
export function isCloudModelType(value) {
    const text = String(value ?? "").trim();
    return text.startsWith("云") || text.toLowerCase().startsWith("cloud");
}

/** `model_type` 是否表示「safetensors 本地模型」。 */
export function isSafetensorsModelType(value) {
    return /safetensors/i.test(String(value ?? ""));
}

/** `model_type` 对应的形态：gguf / safetensors / cloud。 */
export function modelTypeFlavor(value) {
    if (isSafetensorsModelType(value)) return "safetensors";
    if (isCloudModelType(value)) return "cloud";
    return "gguf";
}

/**
 * 把 `model_type` 下拉换成本语言。
 *
 * 与 `model_file` 占位项同理：ComfyUI 的 COMBO 没有 label/value 分离，只能让「值」
 * 随语言变，再由后端用特征判断同时接受中英两种写法。
 */
export function localizeModelType(node) {
    const widget = findWidget(node, "model_type");
    if (!widget) return;
    const table = currentLocale() === "en" ? MODEL_TYPE_EN : MODEL_TYPE_ZH;
    const flavor = modelTypeFlavor(widget.value);
    const target = flavor === "safetensors" ? table[1] : (flavor === "cloud" ? table[2] : table[0]);

    const values = widget.options?.values;
    if (Array.isArray(values) && values.length === table.length) {
        for (let i = 0; i < table.length; i += 1) values[i] = table[i];
    }
    if (widget.value !== target) widget.value = target;
}

/**
 * 从任意 `NL2PromptLoadLLM` 节点组装契约 §2.2 的 LLM_MODEL dict。
 * holderState 传该节点 `_llm_json` 的解析结果（sampling / runtime）；缺省时用默认值。
 */
export function buildLlmModelPayload(node, holderState) {
    const state = holderState && typeof holderState === "object" ? holderState : {};
    const local = !isCloudModelType(widgetText(node, "model_type", "本地模型"));
    const gpu = parseGpuText(widgetText(node, "gpu", "0"));
    const modelPath = widgetText(node, "model_path");
    const modelFile = widgetText(node, "model_file");
    const sampling = state.sampling && typeof state.sampling === "object" ? state.sampling : {};
    const runtime = state.runtime && typeof state.runtime === "object" ? state.runtime : {};

    return {
        mode: local ? "local" : "cloud",
        provider: widgetText(node, "provider", "deepseek"),
        base_url: widgetText(node, "base_url"),
        model: local ? modelFile : widgetText(node, "cloud_model"),
        gpu_index: gpu.gpu_index,
        gpu_name: gpu.gpu_name,
        local_path: local ? joinModelPath(modelPath, modelFile) : "",
        sampling: {
            top_k: toNumber(sampling.top_k, 40),
            top_p: toNumber(sampling.top_p, 0.9),
            temperature: toNumber(sampling.temperature, 0.7),
            max_tokens: toNumber(sampling.max_tokens, 1024),
            seed: toNumber(sampling.seed, -1),
        },
        runtime: {
            ctx: toNumber(runtime.ctx, 8192),
            timeout: toNumber(runtime.timeout, 120),
        },
    };
}

/* ------------------------------------------------------------------ API 封装 */

async function readResponse(response) {
    // 不在这里写中文 detail：交给 i18n 的 err.network 本地化（en 界面也不会漏出中文）
    if (!response) return { ok: false, error: "network" };
    let data = null;
    try {
        data = await response.json();
    } catch (_error) {
        data = null;
    }
    if (!response.ok) {
        return {
            ok: false,
            status: response.status,
            error: (data && data.error) || `http_${response.status}`,
            detail: (data && data.detail) || response.statusText || "",
            // 失败响应体也可能带 debug（如 /rewrite 的 skill_check），保留给调用方提示
            data: data && typeof data === "object" ? data : null,
        };
    }
    if (data && typeof data === "object" && data.ok === false) {
        return {
            ok: false,
            status: response.status,
            error: data.error || "unknown_error",
            detail: data.detail || "",
            data,
        };
    }
    return { ok: true, status: response.status, data: data ?? {} };
}

/** GET 请求；网络异常返回 {ok:false,error:"network"}，绝不抛出。 */
export async function apiGet(path) {
    try {
        const response = await api.fetchApi(path, { method: "GET" });
        return await readResponse(response);
    } catch (error) {
        return { ok: false, error: "network", detail: String(error?.message ?? error) };
    }
}

/** POST JSON 请求；网络异常返回 {ok:false,error:"network"}，绝不抛出。 */
export async function apiPost(path, body) {
    try {
        const response = await api.fetchApi(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body === undefined ? {} : body),
        });
        return await readResponse(response);
    } catch (error) {
        return { ok: false, error: "network", detail: String(error?.message ?? error) };
    }
}

/* ------------------------------------------------------------------ 设置窗口基类 */

/**
 * 打开一个模态设置窗口。
 *
 * options:
 *   title      窗口标题
 *   tabs       [{ label, render(ctx, panel) }]，render 返回节点则自动插入 panel
 *   onSave     async (ctx) => boolean|void；返回 false 表示保存失败、窗口保持打开
 *   onCancel   (ctx) => void；✕ / 遮罩 / Esc / 取消 时调用
 *   saveLabel / cancelLabel / showSave / showCancel
 *   onClose    (ctx) => void；任何关闭方式都会调用（清理资源）
 *
 * 返回 { close, save, status }。
 * 关闭方式：右上角 ✕、点遮罩、Esc —— 三种都走 onCancel（丢弃未保存修改）。
 */
export function openDialog(options = {}) {
    injectStyles();

    const tabs = Array.isArray(options.tabs) ? options.tabs.filter(Boolean) : [];
    const currentTitle = () => String(typeof options.title === "function" ? (options.title() ?? "") : (options.title ?? ""));
    const currentSaveLabel = () => String(
        typeof options.saveLabel === "function" ? (options.saveLabel() ?? "") : (options.saveLabel || t("common.save")),
    );
    const currentCancelLabel = () => String(
        typeof options.cancelLabel === "function" ? (options.cancelLabel() ?? "") : (options.cancelLabel || t("common.cancel")),
    );
    const showSave = options.showSave !== false;
    const showCancel = options.showCancel !== false;
    const onSave = typeof options.onSave === "function" ? options.onSave : null;
    const onCancel = typeof options.onCancel === "function" ? options.onCancel : null;
    const onClose = typeof options.onClose === "function" ? options.onClose : null;

    const root = createElement("div", "anima-nl-root anima-nl-overlay");
    root.lang = currentLocale();
    const dialog = createElement("div", "anima-nl-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", currentTitle());
    dialog.tabIndex = -1;

    const head = createElement("div", "anima-nl-dialog-head");
    const titleEl = createElement("div", "anima-nl-dialog-title", currentTitle());
    head.appendChild(titleEl);
    const closeButton = createElement("button", "anima-nl-dialog-close", "✕");
    closeButton.type = "button";
    closeButton.title = t("common.close");
    head.appendChild(closeButton);

    const tabBar = createElement("div", "anima-nl-tabs");
    const panels = createElement("div", "anima-nl-tabpanels");
    const foot = createElement("div", "anima-nl-dialog-foot");
    const statusEl = createElement("div", "anima-nl-status");
    const cancelButton = createElement("button", "anima-nl-action", currentCancelLabel());
    cancelButton.type = "button";
    const saveButton = createElement("button", "anima-nl-action is-primary", currentSaveLabel());
    saveButton.type = "button";
    foot.append(statusEl);
    if (showCancel) foot.appendChild(cancelButton);
    if (showSave) foot.appendChild(saveButton);

    if (tabs.length) {
        dialog.append(head, tabBar, panels, foot);
    } else {
        dialog.append(head, panels, foot);
    }
    root.appendChild(dialog);

    const ctx = {
        root,
        dialog,
        panels,
        statusEl,
        close: null,
        save: null,
        setStatus: null,
        onClose: null,
    };

    let closed = false;
    const closeHooks = [];
    ctx.onClose = (hook) => {
        if (typeof hook === "function") closeHooks.push(hook);
    };

    function setStatus(text, kind = "") {
        statusEl.textContent = text ? String(text) : "";
        statusEl.classList.toggle("is-error", kind === "error");
        statusEl.classList.toggle("is-ok", kind === "ok");
    }
    ctx.setStatus = setStatus;

    function close(reason = "cancel") {
        if (closed) return;
        closed = true;
        unsubscribeLocale();
        document.removeEventListener("keydown", onKeyDown, true);
        root.remove();
        for (const hook of closeHooks) {
            try {
                hook(ctx, reason);
            } catch (error) {
                console.error("[nl2prompt] 窗口清理失败", error);
            }
        }
        try {
            onClose?.(ctx, reason);
        } catch (error) {
            console.error("[nl2prompt] onClose 失败", error);
        }
        if (reason === "cancel") {
            try {
                onCancel?.(ctx);
            } catch (error) {
                console.error("[nl2prompt] onCancel 失败", error);
            }
        }
    }

    async function requestSave() {
        if (closed) return false;
        if (!onSave) {
            close("save");
            return true;
        }
        saveButton.disabled = true;
        cancelButton.disabled = true;
        setStatus(t("common.saving"), "");
        let keepOpen = false;
        try {
            const result = await onSave(ctx);
            keepOpen = result === false;
        } catch (error) {
            keepOpen = true;
            setStatus(t("common.save_failed_detail", { detail: String(error?.message ?? error) }), "error");
            console.error("[nl2prompt] 保存失败", error);
        }
        saveButton.disabled = false;
        cancelButton.disabled = false;
        if (!keepOpen) {
            setStatus(t("common.saved"), "ok");
            close("save");
            return true;
        }
        if (!statusEl.classList.contains("is-error")) setStatus(t("common.save_failed"), "error");
        return false;
    }

    ctx.close = close;
    ctx.save = () => {
        void requestSave();
        return true;
    };

    // ---- Tab 切换（首次激活时才渲染，避免一次性构建全部面板）
    const renderedTabs = new Set();
    const tabButtons = [];
    let activeIndex = -1;

    function tabLabel(tab, index) {
        const label = typeof tab.label === "function" ? tab.label() : tab.label;
        return label || t("common.tab_fallback", { index: index + 1 });
    }

    function activate(index) {
        const tab = tabs[index];
        if (!tab) return;
        activeIndex = index;
        tabButtons.forEach((button, position) => button.classList.toggle("is-active", position === index));
        Array.from(panels.children).forEach((panel, position) => panel.classList.toggle("is-active", position === index));
        if (renderedTabs.has(index)) return;
        renderedTabs.add(index);
        const panel = panels.children[index];
        if (!panel) return;
        try {
            const content = tab.render?.(ctx, panel);
            if (content instanceof Node && content !== panel) panel.appendChild(content);
        } catch (error) {
            console.error("[nl2prompt] Tab 渲染失败", error);
            panel.appendChild(createElement("div", "anima-nl-hint", t("common.tab_render_failed", {
                detail: String(error?.message ?? error),
            })));
        }
    }

    if (tabs.length) {
        tabs.forEach((tab, index) => {
            const button = createElement("button", "anima-nl-tab", tabLabel(tab, index));
            button.type = "button";
            button.addEventListener("pointerdown", blockPointerEvent);
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                activate(index);
            });
            tabButtons.push(button);
            tabBar.appendChild(button);
            panels.appendChild(createElement("div", "anima-nl-tabpanel"));
        });
    }

    // ---- 语言切换：重渲染标题 / Tab 名 / 底部按钮 / 当前 Tab 内容
    function rerenderForLocale() {
        if (closed) return;
        root.lang = currentLocale();
        titleEl.textContent = currentTitle();
        dialog.setAttribute("aria-label", currentTitle());
        closeButton.title = t("common.close");
        cancelButton.textContent = currentCancelLabel();
        saveButton.textContent = currentSaveLabel();
        if (!tabs.length) {
            panels.replaceChildren();
            if (typeof options.render === "function") {
                try {
                    const content = options.render(ctx, panels);
                    if (content instanceof Node && content !== panels) panels.appendChild(content);
                } catch (error) {
                    console.error("[nl2prompt] 面板渲染失败", error);
                }
            }
            return;
        }
        tabButtons.forEach((button, index) => {
            button.textContent = tabLabel(tabs[index], index);
        });
        const index = activeIndex >= 0 ? activeIndex : 0;
        const panel = panels.children[index];
        if (!panel) return;
        panel.replaceChildren();
        renderedTabs.delete(index);
        activate(index);
    }

    const unsubscribeLocale = subscribeLocale(() => {
        try {
            rerenderForLocale();
        } catch (error) {
            console.error("[nl2prompt] 语言切换重渲染失败", error);
        }
    });

    // ---- 事件绑定
    function onKeyDown(event) {
        if (event.key !== "Escape") return;
        event.stopPropagation();
        close("cancel");
    }

    closeButton.addEventListener("pointerdown", blockPointerEvent);
    closeButton.addEventListener("click", (event) => {
        event.stopPropagation();
        close("cancel");
    });
    cancelButton.addEventListener("pointerdown", blockPointerEvent);
    cancelButton.addEventListener("click", (event) => {
        event.stopPropagation();
        close("cancel");
    });
    saveButton.addEventListener("pointerdown", blockPointerEvent);
    saveButton.addEventListener("click", (event) => {
        event.stopPropagation();
        void requestSave();
    });

    // 遮罩点击关闭；面板内的 pointerdown 一律阻断，避免触发画布拖拽。
    root.addEventListener("mousedown", (event) => {
        event.stopPropagation();
        if (event.target === root) close("cancel");
    });
    root.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
    });
    dialog.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
    });
    dialog.addEventListener("mousedown", (event) => {
        event.stopPropagation();
    });
    document.addEventListener("keydown", onKeyDown, true);

    document.body.appendChild(root);
    if (tabs.length) activate(0);
    else if (typeof options.render === "function") {
        try {
            const content = options.render(ctx, panels);
            if (content instanceof Node) panels.appendChild(content);
        } catch (error) {
            console.error("[nl2prompt] 面板渲染失败", error);
        }
    }
    dialog.focus();

    return { close, save: ctx.save, status: setStatus, root, dialog, ctx };
}
