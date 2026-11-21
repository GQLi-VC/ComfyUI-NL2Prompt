// 自然语言提示词（NL2PromptComposer）—— 主节点 UI（v2）。
//
// 契约：docs/接口契约.md §1（节点 IO）、§3 / §14.4（holder schema）、§7（system prompt）、
//       §9.1 / §9.2 / §9.3（前端）、§14.2（Skill 体系）、§14.3（新路由）、§14.5（默认值）。
//
// 数据真源只有 hidden holder（_ui_json）；所有动态行 / 按钮 / 状态小字都是 serialize=false 的
// DOM widget，只负责显示与编辑，不参与序列化。
//
// 所有网络请求都走 common.apiGet/apiPost（失败返回 {ok:false,error}，绝不抛异常）：
// 后端路由未就绪时节点仍能正常渲染、编辑、保存。

import { app } from "../../scripts/app.js";
import {
    apiGet,
    apiPost,
    applyNodeDisplayNames,
    arrangeWidgets,
    blockPointerEvent,
    buildLlmModelPayload,
    cloneJson,
    createElement,
    findWidget,
    fitNodeHeight,
    formatTimestamp,
    graphNodes,
    injectStyles,
    localizeNativeWidget,
    openDialog,
    readJson,
    refreshAllNodeFaces,
    setWidgetHidden,
    stopPointerEvent,
    toNumber,
    toTimestampMs,
    truncateText,
    uid,
    widgetText,
} from "./common.js";
import { currentMode, describeError, initI18n, setLocale, subscribeLocale, t } from "./i18n.js";

/**
 * 需要安装完整 UI 的节点类名。
 * - `NL2PromptComposer`：v3 当前类名
 * - `AnimaNLComposer`：v2 旧类名 —— 旧工作流反序列化得到的就是它，
 *   不在这里会导致「旧节点 UI 消失」
 */
const NODE_CLASSES = ["NL2PromptComposer", "AnimaNLComposer"];
const NODE_CLASS = "NL2PromptComposer";
const HOLDER_NAME = "_ui_json";
// Python 侧声明的 BOOLEAN widget：新建节点默认开启「LLM 重写」。
const USE_LLM_WIDGET = "use_llm";

const MIN_WIDTH = 420;
const WIDGET_MARGIN = 4;
const HEAD_HEIGHT = 28;
const ROW_HEIGHT = 26;
const ROW_GAP = 4;
const BUTTONS_HEIGHT = 32;
const SETTINGS_HEIGHT = 32;
const REWRITE_HEIGHT = 32;
const STATUS_HEIGHT = 18;
const SEED_HEIGHT = 18;

/** 契约 §14.4：dialog 上限 20 条（超出丢最旧） */
const MAX_DIALOG = 20;

const DEFAULT_SKILL_ID = "anima-tag-master";
const DEFAULT_TARGET_PROFILE = "anima";

/** 契约 §14.5：`negative_base` 默认空串；推荐值由设置窗口的「填充推荐值」按钮写入。 */
const FALLBACK_NEGATIVE_BASE = "lowres, bad anatomy, bad hands, text, watermark, extra digits, worst quality, low quality";

/** v3 F3：正面基础词兜底值（skill 里取不到时使用）。 */
const FALLBACK_POSITIVE_BASE = "masterpiece, best quality, very aesthetic";
// 契约 §7 的默认系统提示词：接口不可用时的兜底文本（包含 {{...}} 占位符）。
const FALLBACK_SYSTEM_PROMPT = `你是 Anima 文生图模型的提示词重写器。把用户的自然语言描述重写为 Anima 更容易执行的提示词。

已知可用角色与服装：
{{character_presets}}

已知可用画师/风格：
{{style_presets}}

当前用户选择：
{{selected}}

规则：
1. 只注入当前选中服装的触发词，不要注入同角色其他服装的触发词。
2. 保留角色 LoRA 激活词、服装触发词、风格触发词。
3. 用具体视觉事实替代空洞形容词。
4. 输出 JSON：{"positive":"...","negative":"...","debug":{"characters":[],"outfits":[],"styles":[],"injected_triggers":[]}}
5. 不要输出解释性文字，只输出 JSON。
`;

/* ------------------------------------------------------------------ Skill 缓存（后端驱动） */

const SKILLS_CACHE = {
    loaded: false,
    loading: null,
    ok: false,
    error: "",
    skills: [],
    content: {},
    default: DEFAULT_SKILL_ID,
};

function normalizeSkillSummary(item) {
    if (!item || typeof item !== "object") return null;
    const id = typeof item.id === "string" ? item.id.trim() : "";
    if (!id) return null;
    return {
        id,
        display_name: typeof item.display_name === "string" && item.display_name ? item.display_name : id,
        target_model: typeof item.target_model === "string" ? item.target_model : "",
    };
}

/** 拉取 `/nl2prompt/skills`；失败返回 ok:false（调用方降级到内置常量）。 */
async function ensureSkills(force = false) {
    if (!force && SKILLS_CACHE.loaded) return SKILLS_CACHE;
    if (SKILLS_CACHE.loading) return SKILLS_CACHE.loading;

    SKILLS_CACHE.loading = (async () => {
        const response = await apiGet("/nl2prompt/skills");
        if (!response.ok) {
            SKILLS_CACHE.ok = false;
            SKILLS_CACHE.error = String(response.error || "network");
            SKILLS_CACHE.skills = [];
            SKILLS_CACHE.content = {};
            SKILLS_CACHE.default = DEFAULT_SKILL_ID;
        } else {
            const data = response.data ?? {};
            const skills = (Array.isArray(data.skills) ? data.skills : []).map(normalizeSkillSummary).filter(Boolean);
            SKILLS_CACHE.ok = skills.length > 0;
            SKILLS_CACHE.error = SKILLS_CACHE.ok ? "" : "empty";
            SKILLS_CACHE.skills = skills;
            SKILLS_CACHE.content = data.content && typeof data.content === "object" ? data.content : {};
            SKILLS_CACHE.default = typeof data.default === "string" && data.default ? data.default : (skills[0]?.id ?? DEFAULT_SKILL_ID);
        }
        SKILLS_CACHE.loaded = true;
        SKILLS_CACHE.loading = null;
        return SKILLS_CACHE;
    })();

    return SKILLS_CACHE.loading;
}

function skillContent(skillId) {
    const id = String(skillId ?? "");
    const entry = SKILLS_CACHE.content ? SKILLS_CACHE.content[id] : null;
    return entry && typeof entry === "object" ? entry : null;
}

/* ------------------------------------------------------------------ 数据模型 */

function makeCharacterRow(row) {
    return {
        id: typeof row?.id === "string" && row.id ? row.id : uid("r"),
        name: typeof row?.name === "string" ? row.name : "",
        trigger: typeof row?.trigger === "string" ? row.trigger : "",
        outfit_hint: typeof row?.outfit_hint === "string" ? row.outfit_hint : "",
    };
}

function makeStyleRow(row) {
    return {
        id: typeof row?.id === "string" && row.id ? row.id : uid("s"),
        name: typeof row?.name === "string" ? row.name : "",
        trigger: typeof row?.trigger === "string" ? row.trigger : "",
    };
}

function makeTextMapRow(row) {
    return {
        id: typeof row?.id === "string" && row.id ? row.id : uid("t"),
        find: typeof row?.find === "string" ? row.find : "",
        prompt: typeof row?.prompt === "string" ? row.prompt : "",
        target: row?.target === "negative" ? "negative" : "positive",
    };
}

function normalizeRewrite(raw) {
    if (!raw || typeof raw !== "object") return null;
    const positive = typeof raw.positive === "string" ? raw.positive : "";
    const negative = typeof raw.negative === "string" ? raw.negative : "";
    if (!positive && !negative) return null;
    const quality = toNumber(raw.quality, NaN);
    return {
        positive,
        negative,
        analysis: typeof raw.analysis === "string" ? raw.analysis : "",
        quality: Number.isFinite(quality) ? quality : null,
        ts: toTimestampMs(raw.ts) || Date.now(),
        source: typeof raw.source === "string" ? raw.source : "",
        fallback: typeof raw.fallback === "string" ? raw.fallback : "",
        issues: Array.isArray(raw.issues) ? raw.issues.filter((item) => typeof item === "string").slice(0, 5) : [],
    };
}

/** 把 `/rewrite` 的 `debug.skill_check.issues` 压成简短代码列表。 */
function readSkillIssues(container) {
    const debug = container && typeof container === "object" ? container.debug : null;
    const check = debug && typeof debug === "object" ? debug.skill_check : null;
    const list = check && typeof check === "object" && Array.isArray(check.issues) ? check.issues : [];
    return list
        .map((item) => (item && typeof item === "object" ? String(item.code || item.detail || "") : String(item ?? "")))
        .map((item) => item.trim())
        .filter(Boolean)
        .slice(0, 5);
}

function normalizeDialogEntry(raw) {
    if (!raw || typeof raw !== "object") return null;
    const positive = typeof raw.positive === "string" ? raw.positive : "";
    const negative = typeof raw.negative === "string" ? raw.negative : "";
    const text = typeof raw.text === "string" ? raw.text : (typeof raw.natural_language === "string" ? raw.natural_language : "");
    if (!positive && !negative && !text) return null;
    const quality = toNumber(raw.quality, NaN);
    return {
        role: typeof raw.role === "string" && raw.role ? raw.role : "user",
        text,
        positive,
        negative,
        analysis: typeof raw.analysis === "string" ? raw.analysis : "",
        quality: Number.isFinite(quality) ? quality : null,
        ts: toTimestampMs(raw.ts) || Date.now(),
    };
}

function normalizeCloudModels(raw) {
    const input = raw && typeof raw === "object" ? raw : {};
    return {
        base_url: typeof input.base_url === "string" ? input.base_url : "",
        models: Array.isArray(input.models)
            ? input.models.map((item) => (typeof item === "string" ? item : String(item?.id ?? ""))).filter(Boolean)
            : [],
        ts: toNumber(input.ts, 0),
    };
}

function defaultState() {
    return {
        schema: 1,
        character_rows: [],
        style_rows: [],
        text_map: [],
        system_prompt: "",
        positive_base: "",
        negative_base: "",
        sampling: { top_k: 40, top_p: 0.9, seed: -1 },
        ui: {
            skill_id: DEFAULT_SKILL_ID,
            target_profile: DEFAULT_TARGET_PROFILE,
            collapsed_main: false,
            open_main_window: false,
            last_rewrite: null,
        },
        dialog: [],
        cloud_models: { base_url: "", models: [], ts: 0 },
    };
}

// 缺字段时补默认值（旧 workflow 不能打挂前端）。
function normalizeState(raw) {
    const input = raw && typeof raw === "object" ? raw : {};
    const state = defaultState();

    state.schema = toNumber(input.schema, 1);
    state.character_rows = Array.isArray(input.character_rows)
        ? input.character_rows.map(makeCharacterRow)
        : [makeCharacterRow()];
    state.style_rows = Array.isArray(input.style_rows)
        ? input.style_rows.map(makeStyleRow)
        : [makeStyleRow()];
    state.text_map = Array.isArray(input.text_map) ? input.text_map.map(makeTextMapRow) : [];
    state.system_prompt = typeof input.system_prompt === "string" ? input.system_prompt : "";
    // v3 F3：正面基础词（缺字段则空串，旧 workflow 兼容）
    state.positive_base = typeof input.positive_base === "string" ? input.positive_base : "";
    // 契约 §14.5：v2 默认空串（推荐值走设置窗口按钮）
    state.negative_base = typeof input.negative_base === "string" ? input.negative_base : "";

    const sampling = input.sampling && typeof input.sampling === "object" ? input.sampling : {};
    state.sampling = {
        top_k: toNumber(sampling.top_k, 40),
        top_p: toNumber(sampling.top_p, 0.9),
        seed: toNumber(sampling.seed, -1),
    };

    const ui = input.ui && typeof input.ui === "object" ? input.ui : {};
    state.ui = {
        skill_id: typeof ui.skill_id === "string" && ui.skill_id ? ui.skill_id : DEFAULT_SKILL_ID,
        target_profile: typeof ui.target_profile === "string" && ui.target_profile ? ui.target_profile : DEFAULT_TARGET_PROFILE,
        collapsed_main: ui.collapsed_main === true,
        open_main_window: ui.open_main_window === true,
        last_rewrite: normalizeRewrite(ui.last_rewrite),
    };

    const dialog = Array.isArray(input.dialog) ? input.dialog.map(normalizeDialogEntry).filter(Boolean) : [];
    state.dialog = dialog.slice(-MAX_DIALOG);
    state.cloud_models = normalizeCloudModels(input.cloud_models);

    return state;
}

function rowsHeight(count) {
    return count > 0 ? count * ROW_HEIGHT + (count - 1) * ROW_GAP : 0;
}

/** 从图谱里找 NL2PromptLoadLLM 节点。 */
function findLlmNode(node) {
    const nodes = graphNodes(node?.graph);
    return nodes.find((entry) => entry && (entry.type === "NL2PromptLoadLLM" || entry.comfyClass === "NL2PromptLoadLLM")) ?? null;
}

/** 读某个节点 holder（`_llm_json` / `_ui_json`）的解析结果。 */
function readNodeHolder(node, holderName) {
    const holder = findWidget(node, holderName);
    const value = holder === null || holder === undefined ? null : holder.value;
    const parsed = readJson(value);
    return parsed && typeof parsed === "object" ? parsed : null;
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
        // 宽度始终取「节点当前宽度」与传入宽度的较大值：
        // 上游有时会传 0 / 旧宽度（node.computeSize() 从 [0,0] 起算），
        // 若据此返回 MIN_WIDTH，DOM 容器就会被按窄宽度布局，表现为内容只占节点一半。
        const nodeWidth = Math.max(
            Number.isFinite(width) && width > 0 ? width : 0,
            toNumber(node.size?.[0], MIN_WIDTH),
            MIN_WIDTH,
        );
        const height = Math.max(0, toNumber(heightFn?.(), 0));
        return [nodeWidth, height + 2 * (widget.margin ?? WIDGET_MARGIN)];
    };
    return widget;
}

function installComposer(node) {
    injectStyles();

    // ---- 节点级 computeSize 兜底：ComfyUI 的默认实现只累加 widget 的高度，
    // 宽度会退化成 NODE_WIDTH * 1.5（140 * 1.5 = 210）。任何用该宽度布局 DOM 的路径
    // 都会把内容压成「节点一半宽」。这里保证宽度不小于节点当前宽度。
    if (!node.__animaNlComputeSizePatched) {
        const originalNodeComputeSize = node.computeSize;
        node.computeSize = function (out) {
            const result = originalNodeComputeSize?.call(this, out);
            const width = Math.max(
                toNumber(result?.[0], 0),
                toNumber(this.size?.[0], 0),
                MIN_WIDTH,
            );
            return [width, Math.max(1, toNumber(result?.[1], 1))];
        };
        node.__animaNlComputeSizePatched = true;
    }

    // ---- 用同名 DOM holder 接管 Python 声明的 _ui_json（保持 widgets_values 的名称与顺序不变）
    let state = normalizeState(null);
    const nativeHolder = findWidget(node, HOLDER_NAME);
    if (nativeHolder) {
        const index = node.widgets.indexOf(nativeHolder);
        if (index >= 0) node.widgets.splice(index, 1);
        if (!app.configuringGraph) state = normalizeState(readJson(nativeHolder.value));
    }

    const holderEl = createElement("div", "anima-nl-root");
    const holder = node.addDOMWidget(HOLDER_NAME, "anima_nl_holder", holderEl, {
        hideOnZoom: false,
        serialize: true,
        getValue: () => JSON.stringify(state),
        setValue: (value) => {
            const next = normalizeState(readJson(value));
            if (JSON.stringify(next) === JSON.stringify(state)) return;
            state = next;
            render();
        },
    });
    holder.serialize = true;
    holder.hidden = true;
    holder.computeSize = () => [0, -4];

    // ---- 折叠头
    const headEl = createElement("div", "anima-nl-head");
    headEl.setAttribute("role", "button");
    headEl.tabIndex = 0;
    headEl.title = t("composer.head_tooltip");
    const headLabel = createElement("span", "anima-nl-head-label", t("composer.head"));
    const headCaret = createElement("span", "anima-nl-caret", "▾");
    headEl.append(headLabel, headCaret);
    headEl.style.height = `${HEAD_HEIGHT}px`;
    const headWidget = addBlockWidget(node, "anima_nl_head", headEl, () => HEAD_HEIGHT);

    // ---- 角色行容器
    const characterEl = createElement("div", "anima-nl-block");
    const characterWidget = addBlockWidget(node, "anima_nl_characters", characterEl, () => rowsHeight(state.character_rows.length));

    // ---- 风格行容器
    const styleEl = createElement("div", "anima-nl-block");
    const styleWidget = addBlockWidget(node, "anima_nl_styles", styleEl, () => rowsHeight(state.style_rows.length));

    // ---- 两个添加按钮
    const addRow = createElement("div", "anima-nl-buttons");
    const addCharacterButton = createElement("button", "anima-nl-btn", t("composer.add_character"));
    addCharacterButton.type = "button";
    addCharacterButton.title = t("composer.add_character_tooltip");
    const addStyleButton = createElement("button", "anima-nl-btn", t("composer.add_style"));
    addStyleButton.type = "button";
    addStyleButton.title = t("composer.add_style_tooltip");
    addRow.append(addCharacterButton, addStyleButton);
    const addRowWidget = addBlockWidget(node, "anima_nl_add_rows", addRow, () => BUTTONS_HEIGHT);

    // ---- 重写 / 沿用上一轮
    const rewriteRow = createElement("div", "anima-nl-buttons");
    const rewriteButton = createElement("button", "anima-nl-btn is-solid", t("composer.rewrite"));
    rewriteButton.type = "button";
    rewriteButton.title = t("composer.rewrite_tooltip");
    const reuseButton = createElement("button", "anima-nl-btn", t("composer.reuse_last"));
    reuseButton.type = "button";
    reuseButton.title = t("composer.reuse_last_tooltip");
    rewriteRow.append(rewriteButton, reuseButton);
    const rewriteWidget = addBlockWidget(node, "anima_nl_rewrite_actions", rewriteRow, () => REWRITE_HEIGHT);

    // ---- 重写状态小字
    const rewriteStatusEl = createElement("div", "anima-nl-hint", t("composer.rewrite_status.empty"));
    rewriteStatusEl.style.height = `${STATUS_HEIGHT}px`;
    rewriteStatusEl.style.display = "flex";
    rewriteStatusEl.style.alignItems = "center";
    rewriteStatusEl.style.overflow = "hidden";
    rewriteStatusEl.style.whiteSpace = "nowrap";
    rewriteStatusEl.style.textOverflow = "ellipsis";
    const rewriteStatusWidget = addBlockWidget(node, "anima_nl_rewrite_status", rewriteStatusEl, () => STATUS_HEIGHT);

    // ---- 设置按钮
    const settingsRow = createElement("div", "anima-nl-buttons");
    const settingsButton = createElement("button", "anima-nl-btn is-solid", t("composer.settings"));
    settingsButton.type = "button";
    settingsButton.title = t("composer.settings_tooltip");
    settingsRow.appendChild(settingsButton);
    const settingsWidget = addBlockWidget(node, "anima_nl_settings", settingsRow, () => SETTINGS_HEIGHT);

    // ---- 种子 / 历史小字
    const seedEl = createElement("div", "anima-nl-seed", t("composer.seed_line", { seed: "-", count: 0 }));
    seedEl.style.height = `${SEED_HEIGHT}px`;
    const seedWidget = addBlockWidget(node, "anima_nl_seed", seedEl, () => SEED_HEIGHT);

    /* ---------------- 通用变更通知 ---------------- */

    function changed() {
        node.setDirtyCanvas?.(true, true);
        node.graph?.change?.();
    }

    function fit() {
        fitNodeHeight(node, MIN_WIDTH);
        scheduleWidthHeal();
    }

    // 兜底自愈：极少数重排路径会把 DOM 容器的 inline 宽度留成旧值/最小值，
    // 表现为「自然语言处理以下的所有内容只有节点一半宽」。检测到偏差就用一次
    // 尺寸抖动（+1px 再回退）逼框架重跑 DOM 布局，视觉上不可察觉。
    //
    // 防误抖 / 防抖动循环（正常尺寸下绝不能反复触发）：
    //   1) 判定要同时满足「小于节点宽度的 85%」与「至少窄 8px」，避开亚像素与主题 padding 误差；
    //   2) 必须连续两次采样（各隔一帧）都判窄才动手，瞬时测量误差不会触发；
    //   3) 抖动进行中与抖动结束后 1.5s 冷却期内一律不再检测，且恢复尺寸后不再复查；
    //   4) 折叠状态下跳过。
    const HEAL_MIN_GAP_PX = 8;
    const HEAL_COOLDOWN_MS = 1500;
    let widthHealScheduled = false;
    let widthHealInFlight = false;
    let widthHealCooldownUntil = 0;
    let widthHealStrikes = 0;

    function isNarrowEnoughToHeal() {
        const scale = toNumber(node.graph?.canvas?.ds?.scale, 1) || 1;
        const width = Math.max(MIN_WIDTH, toNumber(node.size?.[0], MIN_WIDTH));
        // 允许的最大宽度偏差：比例与绝对值取更严的那个
        const threshold = Math.min(width * scale * 0.85, width * scale - HEAL_MIN_GAP_PX);
        for (const widget of node.widgets) {
            if (!widget?.element || widget.hidden) continue;
            const rect = widget.element.getBoundingClientRect?.();
            if (!rect || !(rect.width > 4)) continue;
            if (rect.width < threshold) return true;
        }
        return false;
    }

    function scheduleWidthHeal() {
        if (widthHealScheduled || widthHealInFlight) return;
        if (Date.now() < widthHealCooldownUntil) return;
        const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : null;
        if (!raf) return;
        widthHealScheduled = true;
        raf(() => {
            widthHealScheduled = false;
            if (widthHealInFlight || Date.now() < widthHealCooldownUntil) return;
            if (!node.graph || node.flags?.collapsed) {
                widthHealStrikes = 0;
                return;
            }
            if (!isNarrowEnoughToHeal()) {
                widthHealStrikes = 0;
                return;
            }
            widthHealStrikes += 1;
            if (widthHealStrikes < 2) {
                // 下一帧再确认一次，避免瞬时测量误差触发抖动
                scheduleWidthHeal();
                return;
            }
            widthHealStrikes = 0;
            widthHealInFlight = true;
            const width = Math.max(MIN_WIDTH, toNumber(node.size?.[0], MIN_WIDTH));
            const height = toNumber(node.size?.[1], 1);
            node.setSize?.([width + 1, height]);
            node.setDirtyCanvas?.(true, true);
            raf(() => {
                if (node.graph) {
                    node.setSize?.([width, toNumber(node.size?.[1], height)]);
                    node.setDirtyCanvas?.(true, true);
                }
                // 抖动结束后冷却，且不做「再检查一次再抖」——故障确实还在时，
                // 由后续用户操作或下一轮 fit 在冷却期之后重试，绝不逐帧抖动。
                widthHealCooldownUntil = Date.now() + HEAL_COOLDOWN_MS;
                widthHealInFlight = false;
            });
        });
    }

    function arrange() {
        arrangeWidgets(node, holder, [
            headWidget,
            characterWidget,
            styleWidget,
            addRowWidget,
            rewriteWidget,
            rewriteStatusWidget,
            settingsWidget,
            seedWidget,
        ]);
    }

    /* ---------------- 行渲染 ---------------- */

    function characterRowEl(row) {
        const wrap = createElement("div", "anima-nl-row is-character");
        const fields = [
            ["name", t("composer.placeholder.character_name")],
            ["trigger", t("composer.placeholder.character_trigger")],
            ["outfit_hint", t("composer.placeholder.character_outfit")],
        ];
        for (const [field, placeholder] of fields) {
            const input = createElement("input", "anima-nl-input");
            input.type = "text";
            input.placeholder = placeholder;
            if (field === "outfit_hint") {
                // 第三列语义：纯英文装扮标签，紧跟角色 LoRA 触发词注入正向提示词
                input.title = t("composer.outfit_tooltip");
            }
            input.value = row[field] ?? "";
            input.addEventListener("pointerdown", stopPointerEvent);
            input.addEventListener("input", () => {
                row[field] = input.value;
                changed();
            });
            wrap.appendChild(input);
        }
        wrap.appendChild(removeButton(t("composer.remove_character_row"), () => {
            state.character_rows = state.character_rows.filter((entry) => entry !== row);
            render();
            changed();
        }));
        return wrap;
    }

    function styleRowEl(row) {
        const wrap = createElement("div", "anima-nl-row is-style");
        const fields = [
            ["name", t("composer.placeholder.style_name")],
            ["trigger", t("composer.placeholder.style_trigger")],
        ];
        for (const [field, placeholder] of fields) {
            const input = createElement("input", "anima-nl-input");
            input.type = "text";
            input.placeholder = placeholder;
            input.value = row[field] ?? "";
            input.addEventListener("pointerdown", stopPointerEvent);
            input.addEventListener("input", () => {
                row[field] = input.value;
                changed();
            });
            wrap.appendChild(input);
        }
        wrap.appendChild(removeButton(t("composer.remove_style_row"), () => {
            state.style_rows = state.style_rows.filter((entry) => entry !== row);
            render();
            changed();
        }));
        return wrap;
    }

    function removeButton(title, onRemove) {
        const button = createElement("button", "anima-nl-icon", "✕");
        button.type = "button";
        button.title = title;
        button.addEventListener("pointerdown", blockPointerEvent);
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            onRemove();
        });
        return button;
    }

    /* ---------------- 状态小字 ---------------- */

    let lastSeed = null;
    // 临时提示（运行中 / 失败 / 成功）走 i18n key，语言切换后可重渲染
    let rewriteNotice = null;

    function renderSeedLine() {
        const seedText = Number.isFinite(lastSeed) && lastSeed >= 0 ? String(lastSeed) : "-";
        const historyCount = Array.isArray(state.dialog) ? state.dialog.length : 0;
        seedEl.textContent = t("composer.seed_line", { seed: seedText, count: historyCount });
    }

    function renderRewriteStatus() {
        rewriteStatusEl.classList.remove("is-error", "is-ok");
        if (rewriteNotice) {
            rewriteStatusEl.textContent = t(rewriteNotice.key, rewriteNotice.params);
            if (rewriteNotice.kind === "error") rewriteStatusEl.classList.add("is-error");
            if (rewriteNotice.kind === "ok") rewriteStatusEl.classList.add("is-ok");
            return;
        }
        const last = state.ui?.last_rewrite ?? null;
        if (!last) {
            rewriteStatusEl.textContent = t("composer.rewrite_status.empty");
            return;
        }
        const quality = toNumber(last.quality, NaN);
        const analysis = truncateText(last.analysis, 40) || t("composer.rewrite_status.no_analysis");
        const notes = [];
        if (last.fallback) notes.push(t("composer.rewrite_status.fallback_note", { code: last.fallback }));
        if (Array.isArray(last.issues) && last.issues.length) {
            notes.push(t("composer.rewrite_status.issues_note", { count: last.issues.length }));
        }
        const suffix = notes.length
            ? t("composer.rewrite_status.notes_suffix", { notes: notes.join(t("common.list_separator")) })
            : "";
        rewriteStatusEl.textContent = Number.isFinite(quality)
            ? t("composer.rewrite_status.done", { quality: Math.round(quality), analysis }) + suffix
            : t("composer.rewrite_status.done_no_quality", { analysis }) + suffix;
        if (last.fallback) rewriteStatusEl.classList.add("is-error");
    }

    function setRewriteNotice(key, params, kind = "") {
        rewriteNotice = key ? { key, params: params || {}, kind } : null;
        renderRewriteStatus();
    }

    /** 后端声明字段的显示文案（只改 tooltip / BOOLEAN label，不动取值）。 */
    const NATIVE_WIDGET_SPECS = {
        natural_language: { tooltipKey: "composer.widget.natural_language_tooltip" },
        use_llm: { labelOnKey: "composer.use_llm.on", labelOffKey: "composer.use_llm.off" },
    };

    function refreshNativeWidgetText() {
        for (const [name, spec] of Object.entries(NATIVE_WIDGET_SPECS)) {
            localizeNativeWidget(findWidget(node, name), spec);
        }
    }

    /** 语言切换 / 首次渲染时刷新所有静态文案（行内 placeholder 由 render 重建负责）。 */
    function refreshLabels() {
        refreshNativeWidgetText();
        headEl.title = t("composer.head_tooltip");
        headLabel.textContent = t("composer.head");
        addCharacterButton.textContent = t("composer.add_character");
        addCharacterButton.title = t("composer.add_character_tooltip");
        addStyleButton.textContent = t("composer.add_style");
        addStyleButton.title = t("composer.add_style_tooltip");
        rewriteButton.textContent = t("composer.rewrite");
        rewriteButton.title = t("composer.rewrite_tooltip");
        reuseButton.textContent = t("composer.reuse_last");
        reuseButton.title = t("composer.reuse_last_tooltip");
        settingsButton.textContent = t("composer.settings");
        settingsButton.title = t("composer.settings_tooltip");
    }

    /* ---------------- 整体渲染 ---------------- */

    function render() {
        const collapsed = state.ui?.collapsed_main === true;

        refreshLabels();
        headCaret.textContent = collapsed ? "▸" : "▾";
        headEl.classList.toggle("is-collapsed", collapsed);

        characterEl.replaceChildren(...state.character_rows.map(characterRowEl));
        characterEl.style.height = `${rowsHeight(state.character_rows.length)}px`;

        styleEl.replaceChildren(...state.style_rows.map(styleRowEl));
        styleEl.style.height = `${rowsHeight(state.style_rows.length)}px`;

        setWidgetHidden(characterWidget, collapsed);
        setWidgetHidden(styleWidget, collapsed);
        setWidgetHidden(addRowWidget, collapsed);
        setWidgetHidden(rewriteWidget, collapsed);
        setWidgetHidden(settingsWidget, collapsed);

        renderSeedLine();
        renderRewriteStatus();

        arrange();
        fit();
    }

    // 语言切换：重建行（placeholder）+ 刷新静态文案 + 状态小字
    const unsubscribeLocale = subscribeLocale(() => {
        try {
            render();
            refreshAllNodeFaces(app);
        } catch (error) {
            console.error("[nl2prompt] 语言切换重绘失败", error);
        }
    });

    /* ---------------- 事件 ---------------- */

    headEl.addEventListener("pointerdown", stopPointerEvent);
    headEl.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        headEl.click();
    });
    headEl.addEventListener("click", (event) => {
        event.stopPropagation();
        state.ui = { ...(state.ui ?? {}), collapsed_main: !(state.ui?.collapsed_main === true) };
        render();
        changed();
    });

    addCharacterButton.addEventListener("pointerdown", blockPointerEvent);
    addCharacterButton.addEventListener("click", (event) => {
        event.stopPropagation();
        state.character_rows.push(makeCharacterRow());
        state.ui = { ...(state.ui ?? {}), collapsed_main: false };
        render();
        changed();
    });

    addStyleButton.addEventListener("pointerdown", blockPointerEvent);
    addStyleButton.addEventListener("click", (event) => {
        event.stopPropagation();
        state.style_rows.push(makeStyleRow());
        state.ui = { ...(state.ui ?? {}), collapsed_main: false };
        render();
        changed();
    });

    settingsButton.addEventListener("pointerdown", blockPointerEvent);
    settingsButton.addEventListener("click", (event) => {
        event.stopPropagation();
        openPromptSettings();
    });

    /* ---------------- 重写（R4） ---------------- */

    async function rewritePrompt() {
        const naturalLanguage = String(findWidget(node, "natural_language")?.value ?? "");
        if (!naturalLanguage.trim()) {
            setRewriteNotice("composer.rewrite.empty_input", null, "error");
            return;
        }

        const llmNode = findLlmNode(node);
        const llm = llmNode ? buildLlmModelPayload(llmNode, readNodeHolder(llmNode, "_llm_json")) : {};

        rewriteButton.disabled = true;
        setRewriteNotice("composer.rewrite.running");
        const response = await apiPost("/nl2prompt/rewrite", {
            llm,
            natural_language: naturalLanguage,
            ui: cloneJson(state) ?? {},
        });
        rewriteButton.disabled = false;

        if (!response.ok) {
            const issues = readSkillIssues(response.data);
            const issueText = issues.length
                ? t("composer.rewrite.issues_suffix", { issues: issues.slice(0, 2).join(" / ") })
                : "";
            setRewriteNotice("composer.rewrite.failed", { error: describeError(response) + issueText }, "error");
            return;
        }

        const data = response.data ?? {};
        const result = data.result && typeof data.result === "object" ? data.result : {};
        const debug = data.debug && typeof data.debug === "object" ? data.debug : {};
        const positive = pickText(data.positive, result.positive);
        const negative = pickText(data.negative, result.negative);
        if (!positive) {
            setRewriteNotice("composer.rewrite.no_positive", null, "error");
            return;
        }
        const analysis = pickText(data.analysis, result.analysis, debug.analysis);
        const quality = toNumber(data.quality ?? data.score ?? debug.quality ?? debug.score, NaN);
        const fallback = pickText(debug.fallback);
        const issues = readSkillIssues(data);

        state.ui = {
            ...(state.ui ?? {}),
            last_rewrite: {
                positive,
                negative,
                analysis,
                quality: Number.isFinite(quality) ? quality : null,
                ts: Date.now(),
                source: "rewrite",
                fallback,
                issues,
            },
        };
        rewriteNotice = null;
        renderRewriteStatus();
        changed();
    }

    function reuseLastDialog() {
        const entries = Array.isArray(state.dialog) ? state.dialog : [];
        const last = entries[entries.length - 1];
        if (!last) {
            setRewriteNotice("composer.reuse.no_history", null, "error");
            return;
        }
        if (!last.positive) {
            setRewriteNotice("composer.reuse.no_positive", null, "error");
            return;
        }
        state.ui = {
            ...(state.ui ?? {}),
            last_rewrite: {
                positive: last.positive,
                negative: last.negative ?? "",
                analysis: last.analysis ?? "",
                quality: last.quality ?? null,
                ts: Date.now(),
                source: "dialog",
            },
        };
        renderRewriteStatus();
        changed();
    }

    function pickText(...values) {
        for (const value of values) {
            if (typeof value === "string" && value.trim()) return value;
        }
        return "";
    }

    rewriteButton.addEventListener("pointerdown", blockPointerEvent);
    rewriteButton.addEventListener("click", (event) => {
        event.stopPropagation();
        void rewritePrompt();
    });

    reuseButton.addEventListener("pointerdown", blockPointerEvent);
    reuseButton.addEventListener("click", (event) => {
        event.stopPropagation();
        reuseLastDialog();
    });

    /* ---------------- 执行后回填 ---------------- */

    function setSeed(seed) {
        const value = toNumber(seed, NaN);
        lastSeed = Number.isFinite(value) && value >= 0 ? Math.trunc(value) : null;
        renderSeedLine();
    }

    /** 契约 §14.4：后端 ui 回填的 dialog_entry 追加进 holder.dialog（上限 20）。 */
    function appendDialog(raw) {
        const entry = normalizeDialogEntry(raw);
        if (!entry) return;
        const next = [...(Array.isArray(state.dialog) ? state.dialog : []), entry];
        while (next.length > MAX_DIALOG) next.shift();
        state = { ...state, dialog: next };
        renderSeedLine();
        changed();
    }

    /* ---------------- 提示词设置窗口（v3：7 Tab） ---------------- */

    function openPromptSettings() {
        const draft = normalizeState(cloneJson(state));

        openDialog({
            title: () => t("composer.window.title"),
            tabs: [
                { label: () => t("composer.tab.general"), render: (ctx) => buildGeneralTab(ctx, draft) },
                { label: () => t("composer.tab.replace"), render: (ctx) => buildTextMapTab(ctx, draft) },
                { label: () => t("composer.tab.system_prompt"), render: (ctx) => buildSystemPromptTab(ctx, draft) },
                { label: () => t("composer.tab.positive_base"), render: (ctx) => buildPositiveBaseTab(ctx, draft) },
                { label: () => t("composer.tab.negative_base"), render: (ctx) => buildNegativeBaseTab(ctx, draft) },
                { label: () => t("composer.tab.skill"), render: (ctx) => buildSkillTab(ctx, draft) },
                { label: () => t("composer.tab.history"), render: (ctx) => buildDialogTab(ctx, draft) },
            ],
            onSave: () => {
                state = normalizeState(draft);
                render();
                changed();
                return true;
            },
        });
    }

    /* ---------------- 对外控制对象 ---------------- */

    node.__nl2promptRefreshLabels = () => render();

    node.__animaNl = {
        getState: () => state,
        applyState(raw) {
            state = normalizeState(readJson(raw));
            render();
        },
        render,
        fit,
        changed,
        setSeed,
        appendDialog,
        rewritePrompt,
        reuseLastDialog,
        openSettings: openPromptSettings,
        holder,
    };

    render();
    node.setSize([Math.max(MIN_WIDTH, node.size?.[0] ?? MIN_WIDTH), Math.max(160, node.size?.[1] ?? 160)]);
    requestAnimationFrame(() => {
        if (node.graph) fit();
    });
}

/* ------------------------------------------------------------------ 设置窗口内容 */

function buildTextMapTab(ctx, draft) {
    void ctx;
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.replace.hint"),
    ));

    const list = createElement("div", "anima-nl-list");

    function ruleEl(entry) {
        const box = createElement("div", "anima-nl-rule");
        const row = createElement("div", "anima-nl-row is-textmap");

        const findInput = createElement("input", "anima-nl-input");
        findInput.type = "text";
        findInput.placeholder = t("composer.replace.find_placeholder");
        findInput.value = entry.find ?? "";
        findInput.addEventListener("pointerdown", stopPointerEvent);
        findInput.addEventListener("input", () => {
            entry.find = findInput.value;
        });

        const promptInput = createElement("input", "anima-nl-input");
        promptInput.type = "text";
        promptInput.placeholder = t("composer.replace.prompt_placeholder");
        promptInput.value = entry.prompt ?? "";
        promptInput.addEventListener("pointerdown", stopPointerEvent);
        promptInput.addEventListener("input", () => {
            entry.prompt = promptInput.value;
        });

        const targetSelect = createElement("select", "anima-nl-select");
        for (const [value, text] of [["positive", t("composer.replace.target_positive")], ["negative", t("composer.replace.target_negative")]]) {
            const option = createElement("option", null, text);
            option.value = value;
            targetSelect.appendChild(option);
        }
        targetSelect.value = entry.target === "negative" ? "negative" : "positive";
        targetSelect.addEventListener("pointerdown", stopPointerEvent);
        targetSelect.addEventListener("change", () => {
            entry.target = targetSelect.value === "negative" ? "negative" : "positive";
        });

        const removeRuleButton = createElement("button", "anima-nl-icon", "✕");
        removeRuleButton.type = "button";
        removeRuleButton.title = t("composer.replace.remove_rule");
        removeRuleButton.addEventListener("pointerdown", blockPointerEvent);
        removeRuleButton.addEventListener("click", (event) => {
            event.stopPropagation();
            draft.text_map = draft.text_map.filter((item) => item !== entry);
            renderList();
        });

        row.append(findInput, promptInput, targetSelect, removeRuleButton);
        box.append(row, createElement(
            "div",
            "anima-nl-hint",
            t("composer.replace.row_hint"),
        ));
        return box;
    }

    function renderList() {
        if (!draft.text_map.length) {
            list.replaceChildren(createElement("div", "anima-nl-hint", t("composer.replace.empty")));
            return;
        }
        list.replaceChildren(...draft.text_map.map(ruleEl));
    }

    const addButton = createElement("button", "anima-nl-action", t("composer.replace.add"));
    addButton.type = "button";
    addButton.addEventListener("pointerdown", blockPointerEvent);
    addButton.addEventListener("click", (event) => {
        event.stopPropagation();
        draft.text_map.push(makeTextMapRow(null));
        renderList();
    });

    renderList();
    wrap.append(list, addButton);
    return wrap;
}

function buildSystemPromptTab(ctx, draft) {
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.system.hint"),
    ));

    const textarea = createElement("textarea", "anima-nl-textarea");
    textarea.rows = 14;
    textarea.spellcheck = false;
    textarea.placeholder = t("composer.system.placeholder");
    textarea.value = draft.system_prompt ?? "";
    textarea.addEventListener("pointerdown", stopPointerEvent);
    textarea.addEventListener("input", () => {
        draft.system_prompt = textarea.value;
    });

    const actions = createElement("div", "anima-nl-actions");

    const resetButton = createElement("button", "anima-nl-action", t("composer.system.restore"));
    resetButton.type = "button";
    resetButton.addEventListener("pointerdown", blockPointerEvent);
    resetButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!window.confirm(t("composer.system.confirm_restore"))) return;
        ctx.setStatus(t("composer.system.reading_default"), "");
        const response = await apiGet("/nl2prompt/default_system_prompt");
        if (!response.ok) {
            textarea.value = FALLBACK_SYSTEM_PROMPT;
            draft.system_prompt = FALLBACK_SYSTEM_PROMPT;
            ctx.setStatus(t("composer.system.fallback_used"), "error");
            return;
        }
        const prompt = typeof response.data?.prompt === "string" ? response.data.prompt : "";
        textarea.value = prompt;
        draft.system_prompt = prompt;
        ctx.setStatus(
            prompt.trim() ? t("composer.system.loaded") : t("composer.system.cleared"),
            "ok",
        );
    });

    const saveButton = createElement("button", "anima-nl-action is-primary", t("common.save"));
    saveButton.type = "button";
    saveButton.addEventListener("pointerdown", blockPointerEvent);
    saveButton.addEventListener("click", (event) => {
        event.stopPropagation();
        draft.system_prompt = textarea.value;
        ctx.save();
    });

    actions.append(resetButton, saveButton);
    wrap.append(textarea, actions);
    return wrap;
}

/* ------------------------------------------------------------------ 通用设置（v3 F5） */

const UI_SETTINGS_CACHE = {
    loaded: false,
    loading: null,
    ok: false,
    error: "",
    ui_locale: "auto",
    default_positive_base: "",
    // v5：提示词优化开关（缺键 = 显示开启）
    prompt_opt: { dedupe_skill: true, static_selected: true, retry_multiturn: true },
    prompt_opt_available: false,
    // v5.1：模型思考模式（off / auto，缺键 = off，与后端默认一致）
    thinking_mode: "off",
    thinking_mode_available: false,
};

/** `thinking_mode` 合法取值（与后端 store.THINKING_MODES 一致）。 */
const THINKING_MODES = ["off", "auto"];

/** 归一化：只认 off / auto，其余（含缺键、类型异常）一律 off。 */
function normalizeThinkingMode(value) {
    const text = String(value === null || value === undefined ? "" : value).trim().toLowerCase();
    return THINKING_MODES.includes(text) ? text : "off";
}

/**
 * `prompt_opt` 的键（与后端 store.DEFAULT_PROMPT_OPT 一致，共 4 个）。
 * 注意 `tagdb_agent` 不是"省 token"类开关，界面上单独放在「标签库」组，
 * 因此渲染时只用 PROMPT_OPT_INLINE_KEYS，读写仍覆盖全部 4 个。
 */
const PROMPT_OPT_KEYS = ["dedupe_skill", "static_selected", "retry_multiturn", "tagdb_agent"];
const PROMPT_OPT_INLINE_KEYS = ["dedupe_skill", "static_selected", "retry_multiturn"];

/**
 * 归一化 prompt_opt：只有显式的假值才算关闭，缺键 / 类型异常一律视为开启。
 * 这条规则保证「用户从没设置过」与「settings.json 缺这个键」都显示为开启。
 */
function normalizePromptOpt(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const out = {};
    for (const key of PROMPT_OPT_KEYS) {
        const value = source[key];
        out[key] = value === false || value === 0 || (typeof value === "string" && value.trim().toLowerCase() === "false")
            ? false
            : true;
    }
    return out;
}

function normalizeLocale(value) {
    const text = String(value === null || value === undefined ? "" : value).trim().toLowerCase();
    return ["auto", "zh", "en"].includes(text) ? text : "auto";
}

function localeLabel(value) {
    switch (normalizeLocale(value)) {
        case "zh":
            return t("composer.locale.zh");
        case "en":
            return t("composer.locale.en");
        default:
            return t("composer.locale.auto");
    }
}

/** 拉取 `/nl2prompt/settings_ui`（失败返回缓存默认值，绝不抛异常）。 */
async function ensureUiSettings(force = false) {
    if (!force && UI_SETTINGS_CACHE.loaded) return UI_SETTINGS_CACHE;
    if (UI_SETTINGS_CACHE.loading) return UI_SETTINGS_CACHE.loading;

    UI_SETTINGS_CACHE.loading = (async () => {
        const response = await apiGet("/nl2prompt/settings_ui");
        if (!response.ok) {
            UI_SETTINGS_CACHE.ok = false;
            UI_SETTINGS_CACHE.error = String(response.error || "network");
        } else {
            const data = response.data && typeof response.data === "object" ? response.data : {};
            const source = data.settings && typeof data.settings === "object" ? data.settings : data;
            UI_SETTINGS_CACHE.ok = true;
            UI_SETTINGS_CACHE.error = "";
            UI_SETTINGS_CACHE.ui_locale = normalizeLocale(source.ui_locale);
            UI_SETTINGS_CACHE.default_positive_base =
                typeof source.default_positive_base === "string" ? source.default_positive_base : "";
            // v5：后端支持后会回传 prompt_opt；没有这个键时按默认全开显示
            UI_SETTINGS_CACHE.prompt_opt_available = Object.prototype.hasOwnProperty.call(source, "prompt_opt");
            UI_SETTINGS_CACHE.prompt_opt = normalizePromptOpt(source.prompt_opt);
            // v5.1：后端未返回 thinking_mode 时按 off 显示（与 store 默认值一致）
            UI_SETTINGS_CACHE.thinking_mode_available = Object.prototype.hasOwnProperty.call(source, "thinking_mode");
            UI_SETTINGS_CACHE.thinking_mode = normalizeThinkingMode(source.thinking_mode);
        }
        UI_SETTINGS_CACHE.loaded = true;
        UI_SETTINGS_CACHE.loading = null;
        return UI_SETTINGS_CACHE;
    })();

    return UI_SETTINGS_CACHE.loading;
}

/* ---------------- 通用 Tab（语言 + 正面基础词默认值） ---------------- */

function buildGeneralTab(ctx, draft) {
    void draft;
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.general.hint"),
    ));

    // ---- 语言
    const localeField = createElement("div", "anima-nl-field");
    localeField.appendChild(createElement("label", "anima-nl-field-label", t("composer.general.locale_label")));
    const localeSelect = createElement("select", "anima-nl-select");
    localeSelect.addEventListener("pointerdown", stopPointerEvent);
    for (const [value, text] of [["auto", t("composer.locale.auto")], ["zh", t("composer.locale.zh")], ["en", t("composer.locale.en")]]) {
        const option = createElement("option", null, text);
        option.value = value;
        localeSelect.appendChild(option);
    }
    localeSelect.value = UI_SETTINGS_CACHE.ui_locale;
    localeField.appendChild(localeSelect);
    localeField.appendChild(createElement("div", "anima-nl-hint", t("composer.general.locale_hint")));
    wrap.appendChild(localeField);

    // ---- 正面基础词默认值
    const positiveField = createElement("div", "anima-nl-field");
    positiveField.appendChild(createElement("label", "anima-nl-field-label", t("composer.general.positive_default_label")));
    const positiveInput = createElement("input", "anima-nl-input");
    positiveInput.type = "text";
    positiveInput.placeholder = t("composer.general.positive_default_placeholder");
    positiveInput.value = UI_SETTINGS_CACHE.default_positive_base;
    positiveInput.addEventListener("pointerdown", stopPointerEvent);
    positiveField.appendChild(positiveInput);
    positiveField.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.general.positive_default_hint"),
    ));
    wrap.appendChild(positiveField);

    // ---- 模型思考（v5.1：off / auto，直接影响重写能不能跑通，所以放在优化开关之前）
    wrap.appendChild(createElement("div", "anima-nl-divider"));
    wrap.appendChild(createElement("div", "anima-nl-field-label", t("composer.thinking.title")));
    wrap.appendChild(createElement("div", "anima-nl-hint", t("composer.thinking.hint")));

    const thinkingField = createElement("div", "anima-nl-field");
    thinkingField.appendChild(createElement("label", "anima-nl-field-label", t("composer.thinking.label")));
    const thinkingSelect = createElement("select", "anima-nl-select");
    thinkingSelect.addEventListener("pointerdown", stopPointerEvent);
    for (const [value, text] of [
        ["off", t("composer.thinking.option_off")],
        ["auto", t("composer.thinking.option_auto")],
    ]) {
        const option = createElement("option", null, text);
        option.value = value;
        thinkingSelect.appendChild(option);
    }
    thinkingSelect.dataset.role = "thinking-mode";
    thinkingSelect.value = normalizeThinkingMode(UI_SETTINGS_CACHE.thinking_mode);
    thinkingField.appendChild(thinkingSelect);
    thinkingField.appendChild(createElement("div", "anima-nl-hint", t("composer.thinking.local_only")));
    wrap.appendChild(thinkingField);

    const thinkingUnavailable = createElement("div", "anima-nl-hint", t("composer.thinking.unavailable"));
    thinkingUnavailable.dataset.role = "thinking-unavailable";
    thinkingUnavailable.style.display = UI_SETTINGS_CACHE.thinking_mode_available ? "none" : "";
    wrap.appendChild(thinkingUnavailable);

    /** 读取思考模式下拉的当前值。 */
    function readThinkingMode() {
        return normalizeThinkingMode(thinkingSelect.value);
    }

    /** 用后端值回填思考模式（缺键 → off）。 */
    function applyThinkingMode(value) {
        thinkingSelect.value = normalizeThinkingMode(value);
    }

    // ---- 提示词优化（v5：省 token 的三个开关）
    wrap.appendChild(createElement("div", "anima-nl-divider"));
    wrap.appendChild(createElement("div", "anima-nl-field-label", t("composer.prompt_opt.title")));
    wrap.appendChild(createElement("div", "anima-nl-hint", t("composer.prompt_opt.hint")));

    const promptOptInputs = {};
    const promptOptWarns = {};

    /** 派发 change：真实 DOM 用 dispatchEvent，自测 stub 用 dispatch。 */
    function emitChange(element) {
        try {
            if (typeof element.dispatchEvent === "function") {
                element.dispatchEvent(new Event("change", { bubbles: true }));
            } else if (typeof element.dispatch === "function") {
                element.dispatch("change", {});
            }
        } catch (error) {
            console.error("[nl2prompt] 派发 change 失败", error);
        }
    }

    for (const key of PROMPT_OPT_INLINE_KEYS) {
        const field = createElement("div", "anima-nl-field");
        const row = createElement("div", "anima-nl-switch-row");
        const checkbox = createElement("input", null);
        checkbox.type = "checkbox";
        checkbox.id = `anima-nl-prompt-opt-${key}`;
        checkbox.checked = UI_SETTINGS_CACHE.prompt_opt[key] !== false;
        checkbox.dataset.optKey = key;
        checkbox.addEventListener("pointerdown", stopPointerEvent);
        const label = createElement("label", null, t(`composer.prompt_opt.${key}`));
        label.htmlFor = checkbox.id;
        // 点标题也能切换（原生 label 行为在画布 DOM widget 里不稳定，显式接线）
        label.addEventListener("click", (event) => {
            event.preventDefault();
            checkbox.checked = !checkbox.checked;
            emitChange(checkbox);
        });
        row.append(checkbox, label);
        field.appendChild(row);
        field.appendChild(createElement("div", "anima-nl-hint", t(`composer.prompt_opt.${key}_hint`)));

        // dedupe_skill 关闭时的橙色提醒（正常人不会关）
        if (key === "dedupe_skill") {
            const warn = createElement("div", "anima-nl-switch-warn", t("composer.prompt_opt.dedupe_skill_warn"));
            warn.style.display = checkbox.checked ? "none" : "";
            field.appendChild(warn);
            promptOptWarns[key] = warn;
        }

        promptOptInputs[key] = checkbox;
        wrap.appendChild(field);
    }

    const promptOptUnavailable = createElement("div", "anima-nl-hint", t("composer.prompt_opt.unavailable"));
    promptOptUnavailable.dataset.role = "prompt-opt-unavailable";
    promptOptUnavailable.style.display = UI_SETTINGS_CACHE.prompt_opt_available ? "none" : "";
    wrap.appendChild(promptOptUnavailable);

    /** 读取三个开关的当前值。 */
    function readPromptOpt() {
        const out = {};
        for (const key of PROMPT_OPT_KEYS) {
            const checkbox = promptOptInputs[key];
            out[key] = checkbox ? checkbox.checked !== false : true;
        }
        return out;
    }

    /** 用后端值回填开关（缺键 → 开启）。 */
    function applyPromptOpt(raw) {
        const opt = normalizePromptOpt(raw);
        for (const key of PROMPT_OPT_KEYS) {
            const checkbox = promptOptInputs[key];
            if (checkbox) checkbox.checked = opt[key] !== false;
            const warn = promptOptWarns[key];
            if (warn) warn.style.display = opt[key] === false ? "" : "none";
        }
    }

    for (const key of PROMPT_OPT_INLINE_KEYS) {
        promptOptInputs[key].addEventListener("change", () => {
            const warn = promptOptWarns[key];
            if (warn) warn.style.display = promptOptInputs[key].checked ? "none" : "";
        });
    }

    // ---- 标签库（v7：LLM 写出库外标签时，把库内最接近的合法候选反馈给它再修一轮）
    wrap.appendChild(createElement("div", "anima-nl-divider"));
    wrap.appendChild(createElement("div", "anima-nl-field-label", t("composer.tagdb.title")));

    const tagdbField = createElement("div", "anima-nl-field");
    const tagdbRow = createElement("div", "anima-nl-switch-row");
    const tagdbCheckbox = createElement("input", null);
    tagdbCheckbox.type = "checkbox";
    tagdbCheckbox.id = "anima-nl-tagdb-agent";
    tagdbCheckbox.dataset.optKey = "tagdb_agent";
    tagdbCheckbox.checked = UI_SETTINGS_CACHE.prompt_opt.tagdb_agent !== false;
    tagdbCheckbox.addEventListener("pointerdown", stopPointerEvent);
    const tagdbLabel = createElement("label", null, t("composer.tagdb.agent"));
    tagdbLabel.htmlFor = tagdbCheckbox.id;
    tagdbLabel.addEventListener("click", (event) => {
        event.preventDefault();
        tagdbCheckbox.checked = !tagdbCheckbox.checked;
        emitChange(tagdbCheckbox);
    });
    tagdbRow.append(tagdbCheckbox, tagdbLabel);
    tagdbField.appendChild(tagdbRow);
    tagdbField.appendChild(createElement("div", "anima-nl-hint", t("composer.tagdb.agent_hint")));
    tagdbField.appendChild(createElement("div", "anima-nl-hint", t("composer.tagdb.agent_cost")));
    wrap.appendChild(tagdbField);
    promptOptInputs.tagdb_agent = tagdbCheckbox;

    // ---- 保存
    const actions = createElement("div", "anima-nl-actions");
    const saveButton = createElement("button", "anima-nl-action is-primary", t("composer.general.save"));
    saveButton.type = "button";
    saveButton.addEventListener("pointerdown", blockPointerEvent);
    const reloadButton = createElement("button", "anima-nl-action", t("composer.general.reload"));
    reloadButton.type = "button";
    reloadButton.addEventListener("pointerdown", blockPointerEvent);
    actions.append(saveButton, reloadButton);
    wrap.appendChild(actions);

    async function save(kind) {
        const payload = {
            ui_locale: normalizeLocale(localeSelect.value),
            default_positive_base: positiveInput.value,
            // v5：跟着同一条 settings_ui 保存链路走，不另开路由
            prompt_opt: readPromptOpt(),
            // v5.1：模型思考 off / auto
            thinking_mode: readThinkingMode(),
        };
        const previousLocale = normalizeLocale(UI_SETTINGS_CACHE.ui_locale);
        saveButton.disabled = true;
        ctx.setStatus(t("composer.general.saving"), "");
        const response = await apiPost("/nl2prompt/settings_ui", payload);
        saveButton.disabled = false;
        if (!response.ok) {
            ctx.setStatus(t("composer.general.save_failed", { error: describeError(response) }), "error");
            return;
        }
        UI_SETTINGS_CACHE.loaded = true;
        UI_SETTINGS_CACHE.ok = true;
        UI_SETTINGS_CACHE.error = "";
        UI_SETTINGS_CACHE.ui_locale = payload.ui_locale;
        UI_SETTINGS_CACHE.default_positive_base = payload.default_positive_base;
        UI_SETTINGS_CACHE.prompt_opt = payload.prompt_opt;
        UI_SETTINGS_CACHE.thinking_mode = payload.thinking_mode;
        if (kind === "locale") {
            // 先切语言再写状态：这条消息本身也要用新语言渲染
            if (previousLocale !== payload.ui_locale) applyLocale(payload.ui_locale);
            ctx.setStatus(t("composer.general.locale_saved", { locale: localeLabel(payload.ui_locale) }), "ok");
        } else {
            ctx.setStatus(t("composer.general.saved"), "ok");
        }
    }

    localeSelect.addEventListener("change", (event) => {
        event.stopPropagation();
        void save("locale");
    });

    saveButton.addEventListener("click", (event) => {
        event.stopPropagation();
        void save("all");
    });

    reloadButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        const cache = await ensureUiSettings(true);
        localeSelect.value = cache.ui_locale;
        positiveInput.value = cache.default_positive_base;
        applyPromptOpt(cache.prompt_opt);
        promptOptUnavailable.style.display = cache.prompt_opt_available ? "none" : "";
        applyThinkingMode(cache.thinking_mode);
        thinkingUnavailable.style.display = cache.thinking_mode_available ? "none" : "";
        if (cache.ok && normalizeLocale(cache.ui_locale) !== currentMode()) applyLocale(cache.ui_locale);
        ctx.setStatus(
            cache.ok
                ? t("composer.general.reloaded")
                : t("composer.general.reload_failed", { error: cache.error || "network" }),
            cache.ok ? "ok" : "error",
        );
    });

    function applyLocale(value) {
        try {
            setLocale(normalizeLocale(value));
        } catch (error) {
            console.error("[nl2prompt] 应用界面语言失败", error);
        }
    }

    // 每次打开该 Tab 都重新读一次后端设置（用户可能在别处改过）
    void ensureUiSettings(true).then((cache) => {
        localeSelect.value = cache.ui_locale;
        positiveInput.value = cache.default_positive_base;
        applyPromptOpt(cache.prompt_opt);
        promptOptUnavailable.style.display = cache.prompt_opt_available ? "none" : "";
        applyThinkingMode(cache.thinking_mode);
        thinkingUnavailable.style.display = cache.thinking_mode_available ? "none" : "";
        if (!cache.ok) {
            ctx.setStatus(t("composer.general.unavailable", { error: cache.error || "network" }), "error");
        }
    });

    return wrap;
}

/* ---------------- 正面基础词 Tab（v3 F3） ---------------- */

function buildPositiveBaseTab(ctx, draft) {
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.positive_base.hint"),
    ));

    const textarea = createElement("textarea", "anima-nl-textarea");
    textarea.rows = 8;
    textarea.spellcheck = false;
    textarea.placeholder = t("composer.positive_base.placeholder");
    textarea.value = draft.positive_base ?? "";
    textarea.addEventListener("pointerdown", stopPointerEvent);
    textarea.addEventListener("input", () => {
        draft.positive_base = textarea.value;
    });

    const actions = createElement("div", "anima-nl-actions");
    const fillButton = createElement("button", "anima-nl-action", t("composer.positive_base.fill"));
    fillButton.type = "button";
    fillButton.addEventListener("pointerdown", blockPointerEvent);
    fillButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        fillButton.disabled = true;
        ctx.setStatus(t("composer.positive_base.reading"), "");
        let value = FALLBACK_POSITIVE_BASE;
        let fromSkill = false;

        const cache = await ensureSkills();
        if (cache.ok) {
            const skillId = draft.ui?.skill_id || cache.default;
            const content = skillContent(skillId) ?? skillContent(cache.default);
            if (content) {
                if (typeof content.positive_base === "string" && content.positive_base.trim()) {
                    value = content.positive_base;
                    fromSkill = true;
                } else if (Array.isArray(content.quality_tags) && content.quality_tags.length) {
                    value = content.quality_tags.join(", ");
                    fromSkill = true;
                }
            }
        }
        if (!fromSkill) {
            const uiSettings = await ensureUiSettings();
            if (uiSettings.ok && uiSettings.default_positive_base.trim()) {
                value = uiSettings.default_positive_base;
            }
        }

        fillButton.disabled = false;
        textarea.value = value;
        draft.positive_base = value;
        ctx.setStatus(
            fromSkill ? t("composer.positive_base.filled_skill") : t("composer.positive_base.filled_builtin"),
            fromSkill ? "ok" : (cache.ok ? "ok" : "error"),
        );
    });

    actions.append(fillButton);
    wrap.append(textarea, actions);
    return wrap;
}

function buildNegativeBaseTab(ctx, draft) {
    const wrap = createElement("div", "anima-nl-field");
    wrap.appendChild(createElement(
        "div",
        "anima-nl-hint",
        t("composer.negative_base.hint"),
    ));

    const textarea = createElement("textarea", "anima-nl-textarea");
    textarea.rows = 8;
    textarea.spellcheck = false;
    textarea.placeholder = t("composer.negative_base.placeholder");
    textarea.value = draft.negative_base ?? "";
    textarea.addEventListener("pointerdown", stopPointerEvent);
    textarea.addEventListener("input", () => {
        draft.negative_base = textarea.value;
    });

    const actions = createElement("div", "anima-nl-actions");
    const fillButton = createElement("button", "anima-nl-action", t("composer.negative_base.fill"));
    fillButton.type = "button";
    fillButton.addEventListener("pointerdown", blockPointerEvent);
    fillButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        fillButton.disabled = true;
        ctx.setStatus(t("composer.negative_base.reading"), "");
        let value = FALLBACK_NEGATIVE_BASE;
        let fromSkill = false;
        const cache = await ensureSkills();
        if (cache.ok) {
            const skillId = draft.ui?.skill_id || cache.default;
            const content = skillContent(skillId) ?? skillContent(cache.default);
            if (content && typeof content.negative_base === "string" && content.negative_base.trim()) {
                value = content.negative_base;
                fromSkill = true;
            }
        }
        fillButton.disabled = false;
        textarea.value = value;
        draft.negative_base = value;
        ctx.setStatus(
            fromSkill ? t("composer.negative_base.filled_skill") : t("composer.negative_base.filled_builtin"),
            fromSkill ? "ok" : "error",
        );
    });

    actions.append(fillButton);
    wrap.append(textarea, actions);
    return wrap;
}

/* ---------------- 提示词技能 Tab ---------------- */

function buildSkillTab(ctx, draft) {
    const wrap = createElement("div", "anima-nl-field");
    const intro = createElement(
        "div",
        "anima-nl-hint",
        t("composer.skill.hint"),
    );
    wrap.appendChild(intro);

    const selectField = createElement("div", "anima-nl-field");
    selectField.appendChild(createElement("label", "anima-nl-field-label", t("composer.skill.label")));
    const select = createElement("select", "anima-nl-select");
    select.addEventListener("pointerdown", stopPointerEvent);
    selectField.appendChild(select);
    const targetLine = createElement("div", "anima-nl-hint", t("composer.skill.target_model_unknown"));
    selectField.appendChild(targetLine);
    wrap.appendChild(selectField);

    const textarea = createElement("textarea", "anima-nl-textarea");
    textarea.rows = 14;
    textarea.spellcheck = false;
    textarea.placeholder = t("composer.skill.editor_placeholder");
    textarea.addEventListener("pointerdown", stopPointerEvent);
    wrap.appendChild(textarea);

    const actions = createElement("div", "anima-nl-actions");
    const resetButton = createElement("button", "anima-nl-action", t("composer.skill.restore_default"));
    resetButton.type = "button";
    resetButton.addEventListener("pointerdown", blockPointerEvent);
    const saveButton = createElement("button", "anima-nl-action is-primary", t("composer.skill.save_server"));
    saveButton.type = "button";
    saveButton.addEventListener("pointerdown", blockPointerEvent);
    const newButton = createElement("button", "anima-nl-action", t("composer.skill.new"));
    newButton.type = "button";
    newButton.title = t("composer.skill.new_tooltip");
    newButton.addEventListener("pointerdown", blockPointerEvent);
    const deleteButton = createElement("button", "anima-nl-action", t("composer.skill.delete"));
    deleteButton.type = "button";
    deleteButton.title = t("composer.skill.delete_tooltip");
    deleteButton.addEventListener("pointerdown", blockPointerEvent);
    const saveAsButton = createElement("button", "anima-nl-action", t("composer.skill.save_as"));
    saveAsButton.type = "button";
    saveAsButton.title = t("composer.skill.save_as_tooltip");
    saveAsButton.addEventListener("pointerdown", blockPointerEvent);
    actions.append(resetButton, saveButton, newButton, deleteButton, saveAsButton);
    wrap.appendChild(actions);

    let skillId = draft.ui?.skill_id || DEFAULT_SKILL_ID;
    let summaries = [];

    function applySkill(id) {
        skillId = id;
        const content = skillContent(id);
        const summary = summaries.find((item) => item.id === id);
        const targetModel = (content && typeof content.target_model === "string" && content.target_model)
            || (summary && summary.target_model)
            || "—";
        targetLine.textContent = t("composer.skill.target_model", { model: targetModel });
        textarea.value = content && typeof content.system_prompt === "string" ? content.system_prompt : "";
        if (content && typeof content.display_name === "string") {
            targetLine.title = content.display_name;
        }
    }

    function renderOptions() {
        select.replaceChildren();
        if (!summaries.length) {
            const option = createElement("option", null, t("composer.skill.unavailable_option"));
            option.value = "";
            select.appendChild(option);
            select.disabled = true;
            return;
        }
        select.disabled = false;
        for (const item of summaries) {
            const option = createElement("option", null, `${item.display_name}（${item.id}）`);
            option.value = item.id;
            select.appendChild(option);
        }
        select.value = skillId;
    }

    async function load(force = false) {
        const cache = await ensureSkills(force);
        summaries = cache.skills;
        if (!summaries.length) {
            renderOptions();
            textarea.value = "";
            textarea.placeholder = t("composer.skill.unavailable_placeholder");
            targetLine.textContent = t("composer.skill.target_model", { model: DEFAULT_TARGET_PROFILE });
            ctx.setStatus(t("composer.skill.unavailable_status", { error: cache.error || "network" }), "error");
            return;
        }
        if (!summaries.some((item) => item.id === skillId)) {
            skillId = cache.default || summaries[0].id;
        }
        renderOptions();
        applySkill(skillId);
    }

    select.addEventListener("change", (event) => {
        event.stopPropagation();
        if (!select.value) return;
        skillId = select.value;
        draft.ui = { ...(draft.ui ?? {}), skill_id: skillId };
        applySkill(skillId);
        ctx.setStatus(t("composer.skill.select_hint", { id: skillId }), "ok");
    });

    resetButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!window.confirm(t("composer.skill.confirm_restore"))) return;
        ctx.setStatus(t("composer.skill.reading"), "");
        await load(true);
        ctx.setStatus(t("composer.skill.rewritten"), "ok");
    });

    saveButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!skillId) {
            ctx.setStatus(t("composer.skill.no_skill"), "error");
            return;
        }
        const base = skillContent(skillId);
        if (!base) {
            ctx.setStatus(t("composer.skill.no_content"), "error");
            return;
        }
        const content = { ...base, id: skillId, system_prompt: textarea.value };
        saveButton.disabled = true;
        ctx.setStatus(t("composer.skill.saving"), "");
        const response = await apiPost("/nl2prompt/skills/save", { id: skillId, content });
        saveButton.disabled = false;
        if (!response.ok || response.data?.ok === false) {
            ctx.setStatus(t("composer.skill.save_failed", { error: describeError(response) }), "error");
            return;
        }
        SKILLS_CACHE.content = { ...SKILLS_CACHE.content, [skillId]: { ...content } };
        ctx.setStatus(t("composer.skill.saved"), "ok");
    });

    const SKILL_ID_PATTERN = /^[a-z0-9-]+$/;

    function promptSkillId(message, fallback) {
        const raw = window.prompt(message, fallback || "");
        if (raw === null) return null;
        const value = String(raw).trim().toLowerCase();
        if (!SKILL_ID_PATTERN.test(value)) {
            ctx.setStatus(t("composer.skill.bad_id"), "error");
            return null;
        }
        return value;
    }

    /** 重启 / 新建 / 删除后重新拉取列表并选中指定 skill。 */
    async function reloadAndSelect(nextId) {
        await load(true);
        if (nextId && summaries.some((item) => item.id === nextId)) {
            skillId = nextId;
            draft.ui = { ...(draft.ui ?? {}), skill_id: nextId };
            renderOptions();
            try {
                select.value = nextId;
            } catch (error) {
                console.error("[nl2prompt] skill 下拉选中失败", error);
            }
            applySkill(nextId);
            return true;
        }
        return false;
    }

    newButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        const newId = promptSkillId(t("composer.skill.prompt.new_id"), "");
        if (!newId) return;
        const display = window.prompt(t("composer.skill.prompt.display_name"), newId);
        if (display === null) return;
        const copy = window.confirm(t("composer.skill.confirm_copy", { id: skillId }));
        const copyFrom = copy ? skillId : "";
        const base = skillContent(skillId);

        newButton.disabled = true;
        ctx.setStatus(t("composer.skill.creating"), "");
        const response = await apiPost("/nl2prompt/skills/new", {
            id: newId,
            display_name: String(display || newId).trim() || newId,
            copy_from: copyFrom,
            target_model: (base && typeof base.target_model === "string" && base.target_model) || DEFAULT_TARGET_PROFILE,
        });
        newButton.disabled = false;

        if (!response.ok || response.data?.ok === false) {
            ctx.setStatus(t("composer.skill.new_failed", { error: describeError(response) }), "error");
            return;
        }
        const selected = await reloadAndSelect(newId);
        ctx.setStatus(selected ? t("composer.skill.created", { id: newId }) : t("composer.skill.created_unrefreshed", { id: newId }), selected ? "ok" : "error");
    });

    deleteButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!skillId) {
            ctx.setStatus(t("composer.skill.delete_no_selected"), "error");
            return;
        }
        if (!window.confirm(t("composer.skill.confirm_delete", { id: skillId }))) return;

        deleteButton.disabled = true;
        ctx.setStatus(t("composer.skill.deleting"), "");
        const response = await apiPost("/nl2prompt/skills/delete", { id: skillId });
        deleteButton.disabled = false;

        if (!response.ok || response.data?.ok === false) {
            const code = response.ok ? (response.data?.error || "unknown") : response.error;
            ctx.setStatus(
                code === "skill_protected"
                    ? t("composer.skill.protected")
                    : t("composer.skill.delete_failed", { error: describeError(response) }),
                "error",
            );
            return;
        }
        const fallbackId = SKILLS_CACHE.default || "";
        const selected = await reloadAndSelect(fallbackId);
        if (!selected && summaries.length) {
            await reloadAndSelect(summaries[0].id);
        }
        ctx.setStatus(t("composer.skill.deleted"), "ok");
    });

    saveAsButton.addEventListener("click", async (event) => {
        event.stopPropagation();
        const newId = promptSkillId(t("composer.skill.prompt.save_as_id"), "");
        if (!newId) return;
        if (summaries.some((item) => item.id === newId)) {
            ctx.setStatus(t("composer.skill.save_as_exists", { id: newId }), "error");
            return;
        }
        const base = skillContent(skillId);
        if (!base) {
            ctx.setStatus(t("composer.skill.save_as_no_content"), "error");
            return;
        }
        const content = {
            ...base,
            id: newId,
            display_name: t("composer.skill.save_as_copy_suffix", { name: base.display_name || base.id }),
            system_prompt: textarea.value,
        };

        saveAsButton.disabled = true;
        ctx.setStatus(t("composer.skill.saving_as"), "");
        const response = await apiPost("/nl2prompt/skills/save", { id: newId, content });
        saveAsButton.disabled = false;

        if (!response.ok || response.data?.ok === false) {
            ctx.setStatus(t("composer.skill.save_as_failed", { error: describeError(response) }), "error");
            return;
        }
        SKILLS_CACHE.content = { ...SKILLS_CACHE.content, [newId]: { ...content } };
        const selected = await reloadAndSelect(newId);
        ctx.setStatus(selected ? t("composer.skill.saved_as", { id: newId }) : t("composer.skill.saved_as_unrefreshed", { id: newId }), selected ? "ok" : "error");
    });

    draft.ui = { ...(draft.ui ?? {}), skill_id: skillId };
    void load(false);
    return wrap;
}

/* ---------------- 对话历史 Tab ---------------- */

function buildDialogTab(ctx, draft) {
    const wrap = createElement("div", "anima-nl-field");
    const summaryLine = createElement("div", "anima-nl-hint", "");
    wrap.appendChild(summaryLine);

    const list = createElement("div", "anima-nl-list");
    wrap.appendChild(list);

    const actions = createElement("div", "anima-nl-actions");
    const clearButton = createElement("button", "anima-nl-action", t("composer.history.clear"));
    clearButton.type = "button";
    clearButton.addEventListener("pointerdown", blockPointerEvent);
    clearButton.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!window.confirm(t("composer.history.confirm_clear"))) return;
        draft.dialog = [];
        renderList();
        ctx.setStatus(t("composer.history.cleared"), "ok");
    });
    actions.append(clearButton);
    wrap.appendChild(actions);

    function entryEl(entry, index) {
        const box = createElement("div", "anima-nl-dialog-entry");
        const head = createElement(
            "div",
            "anima-nl-dialog-entry-head",
            t("composer.history.entry_head", {
                index: index + 1,
                time: formatTimestamp(entry.ts),
                text: truncateText(entry.text, 30) || t("composer.history.no_text"),
            }),
        );
        const positiveLine = createElement(
            "div",
            "anima-nl-dialog-entry-body",
            t("composer.history.positive_line", {
                text: truncateText(entry.positive, 60) || t("composer.history.empty_value"),
            }),
        );
        const negativeLine = createElement(
            "div",
            "anima-nl-dialog-entry-body",
            t("composer.history.negative_line", {
                text: truncateText(entry.negative, 40) || t("composer.history.empty_value"),
            }),
        );
        const reuse = createElement("button", "anima-nl-action", t("composer.history.reuse"));
        reuse.type = "button";
        reuse.addEventListener("pointerdown", blockPointerEvent);
        reuse.addEventListener("click", (event) => {
            event.stopPropagation();
            if (!entry.positive) {
                ctx.setStatus(t("composer.history.reuse_failed"), "error");
                return;
            }
            draft.ui = {
                ...(draft.ui ?? {}),
                last_rewrite: {
                    positive: entry.positive,
                    negative: entry.negative ?? "",
                    analysis: entry.analysis ?? "",
                    quality: entry.quality ?? null,
                    ts: Date.now(),
                    source: "dialog",
                },
            };
            ctx.setStatus(t("composer.history.reused", { index: index + 1 }), "ok");
        });

        const reuseRow = createElement("div", "anima-nl-actions");
        reuseRow.append(reuse);
        box.append(head, positiveLine, negativeLine, reuseRow);
        return box;
    }

    function renderList() {
        const entries = Array.isArray(draft.dialog) ? draft.dialog : [];
        summaryLine.textContent = t("composer.history.summary", { count: entries.length, max: MAX_DIALOG });
        if (!entries.length) {
            list.replaceChildren(createElement("div", "anima-nl-hint", t("composer.history.empty")));
            return;
        }
        // 最新一轮排在最上面
        const reversed = [...entries].reverse();
        list.replaceChildren(...reversed.map((entry, position) => entryEl(entry, entries.length - 1 - position)));
    }

    renderList();
    return wrap;
}

/* ------------------------------------------------------------------ 扩展注册 */

// 启动语言：localStorage 乐观值 → 后端 settings_ui 真源 → 自检（幂等，重复调用无副作用）。
// <html lang> 与 t() 的文案都由 initI18n 解析出的同一个 locale 决定。
void initI18n().then(() => {
    refreshNodeDisplayNames();
});

/**
 * 节点栏显示名刷新：注册表可以立刻改，但节点库 store（nodeDefsByName）要等后端
 * node_defs 拉完才存在，因此再补几个延迟时点（幂等，重复调用只改显示名）。
 */
function refreshNodeDisplayNames() {
    const run = () => {
        try {
            applyNodeDisplayNames();
        } catch (error) {
            console.error("[nl2prompt] 节点栏显示名本地化失败", error);
        }
    };
    run();
    for (const delay of [300, 1000, 2500]) {
        try {
            setTimeout(run, delay);
        } catch (error) {
            break;
        }
    }
}

// 语言切换时同步左侧节点栏显示名（注册表 + 节点库 store，可即时生效）
subscribeLocale(() => {
    refreshNodeDisplayNames();
});

app.registerExtension({
    name: "NL2Prompt.Composer",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_CLASSES.includes(nodeData?.name)) return;

        const originalOnCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnCreated?.apply(this, arguments);
            try {
                installComposer(this);
            } catch (error) {
                console.error("[nl2prompt] 主节点 UI 安装失败（后端仍可用）", error);
            }
            try {
                // 新建节点默认开启「LLM 重写」；加载/粘贴旧工作流时（app.configuringGraph）
                // 不介入，仍由 widgets_values 里的历史值决定，旧工作流保持原来的关闭状态。
                if (!app.configuringGraph) {
                    const useLlmWidget = findWidget(this, USE_LLM_WIDGET);
                    if (useLlmWidget && useLlmWidget.value === false) {
                        useLlmWidget.value = true;
                        this.setDirtyCanvas?.(true, true);
                    }
                }
            } catch (error) {
                console.error("[nl2prompt] use_llm 默认值设置失败", error);
            }
            return result;
        };

        const originalOnConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const result = originalOnConfigure?.apply(this, arguments);
            const controller = this.__animaNl;
            if (!controller) return result;
            try {
                // 按 widget 名称取 holder（不依赖 widgets_values 的数组下标）。
                let raw = null;
                const named = info && typeof info === "object" ? info.widgets_values_named : null;
                if (named && typeof named === "object" && Object.prototype.hasOwnProperty.call(named, HOLDER_NAME)) {
                    raw = named[HOLDER_NAME];
                }
                if (raw === null || raw === undefined) raw = readHolderFromValuesArray(this, info?.widgets_values);
                if (raw === null || raw === undefined) {
                    const holderValue = findWidget(this, HOLDER_NAME)?.value;
                    if (typeof holderValue === "string" && holderValue.trim()) raw = holderValue;
                }
                if (raw !== null && raw !== undefined) controller.applyState(raw);
                else controller.render();
            } catch (error) {
                console.error("[nl2prompt] 读取 holder 失败，使用默认值", error);
            }
            return result;
        };

        const originalOnExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const result = originalOnExecuted?.apply(this, arguments);
            const controller = this.__animaNl;
            if (!controller) return result;
            try {
                const payload = Array.isArray(message?.anima_nl) ? message.anima_nl[0] : message?.anima_nl;
                const seed = typeof payload === "number" ? payload : payload?.seed;
                controller.setSeed(seed);
                // 契约 §14.4：后端 ui 回填 dialog_entry → 写入 holder.dialog（上限 20）
                const dialogEntry = payload && typeof payload === "object"
                    ? (payload.dialog_entry ?? payload.dialogEntry ?? null)
                    : null;
                if (dialogEntry) controller.appendDialog(dialogEntry);
            } catch (error) {
                console.error("[nl2prompt] 读取执行结果失败", error);
            }
            return result;
        };
    },
});

/**
 * 兜底迁移：旧 workflow 只有 widgets_values 数组时，按「序列化 widget 名称」定位 _ui_json。
 * 与 ComfyUI 1.58.5 的 getWidgetRestorationSlot 一致：按 widgets 顺序、跳过 serialize=false、
 * 用递增索引对应数组项，因此不会因为数组里有非序列化控件而错位。
 */
function readHolderFromValuesArray(node, values) {
    if (!Array.isArray(values)) return null;
    const serialized = (node?.widgets ?? []).filter((widget) => widget.serialize !== false);
    const index = serialized.findIndex((widget) => widget.name === HOLDER_NAME);
    if (index < 0 || index >= values.length) return null;
    const candidate = values[index];
    return readJson(candidate) ? candidate : null;
}
