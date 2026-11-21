// nl2prompt 前端 i18n（阶段 3）。
//
// 契约：
//   * `t(key, params)`：取当前语言文案；缺失回落 zh；仍缺失时 console.warn 一次并返回 key
//   * `detectComfyLocale()`：用户覆盖 → Comfy.Locale → navigator.language
//   * `setLocale(mode)`：auto|zh|en，写 localStorage 缓存，通知订阅者（即时生效）
//   * `initI18n()`：启动时先用 localStorage 乐观值，再拉 GET /nl2prompt/settings_ui 为真源
//   * `applyLocaleToDocument()`：给 <html> 打 lang
//   * `assertLocalesComplete()`：zh/en 键集合必须完全一致
//   * 键命名 `模块.语义` 两级；params 用 `{name}` 占位
//   * 后端错误码走 `err.<code>`，并优先消费后端 detail_zh / detail_en
//
// 本文件不 import common.js（common.js 会 import 本文件，避免循环依赖）。

import { api } from "../../scripts/api.js";

export const LOCALES = ["auto", "zh", "en"];

const STORAGE_KEY = "nl2prompt.ui_locale";

/* ------------------------------------------------------------------ zh 字典 */

const ZH = {
    // ---------- common（对话框基类）
    "common.list_separator": "，",
    // 节点栏（左侧「节点」面板）显示名 —— zh 可被后端原名覆盖，en 用这里的英文名
    "node.title.composer": "自然语言提示词",
    "node.title.composer_legacy": "（旧）自然语言提示词",
    "node.title.llm_gguf": "加载LLM（GGUF）",
    "node.title.llm_safetensors": "加载LLM（safetensors）",
    "node.title.llm_cloud": "加载云端LLM",
    "node.title.llm_legacy": "加载LLM（旧·本地/云端合一）",
    "node.title.llm_legacy_v2": "（旧）加载LLM",
    "node.title.lora_stack": "LoRA 堆栈加载",
    "node.title.lora_stack_legacy": "（旧）LoRA 堆栈加载",
    "node.desc.composer": "把自然语言 + 角色/风格 LoRA 激活词 + 文字替换组装成提示词；可选调用 LLM 重写，失败自动回退模板。clip 连接时直接输出 CONDITIONING。",
    "node.desc.composer_legacy": "把自然语言 + 角色/风格 LoRA 激活词 + 文字替换组装成提示词；可选调用 LLM 重写，失败自动回退模板。clip 连接时直接输出 CONDITIONING。",
    "node.desc.llm_gguf": "本地 GGUF 模型：由插件自动拉起 llama-server 并绑定指定 GPU，输出 LLM_MODEL（绝不含 api_key 明文）。",
    "node.desc.llm_safetensors": "本地 HuggingFace safetensors 模型（需目录内含 config.json）：transformers 直接加载到指定 GPU，显存不足时自动降级并报 local_model_oom。",
    "node.desc.llm_cloud": "云端 OpenAI 兼容端点：只产出 LLM_MODEL（base_url / model / 采样参数），api_key 由 providers 在调用时解析，绝不明文进入工作流。",
    "node.desc.llm_legacy": "配置本地 llama-server 或云端 OpenAI 兼容端点，输出 LLM_MODEL（绝不含 api_key 明文）。",
    "node.desc.llm_legacy_v2": "配置本地 llama-server 或云端 OpenAI 兼容端点，输出 LLM_MODEL（绝不含 api_key 明文）。",
    "node.desc.lora_stack": "按 NL2PromptComposer 输出的 lora_stack 依次应用 LoRA；单个失败打印告警后跳过。",
    "node.desc.lora_stack_legacy": "按 NL2PromptComposer 输出的 lora_stack 依次应用 LoRA；单个失败打印告警后跳过。",

    "common.save": "保存",
    "common.cancel": "取消",
    "common.close": "关闭",
    "common.saving": "保存中…",
    "common.saved": "已保存",
    "common.save_failed": "保存失败",
    "common.save_failed_detail": "保存失败：{detail}",
    "common.tab_fallback": "标签页 {index}",
    "common.tab_render_failed": "面板渲染失败：{detail}",
    "common.panel_render_failed": "面板渲染失败：{detail}",

    // ---------- composer 正面
    "composer.head": "⚪ 自然语言处理",
    "composer.use_llm.on": "LLM 重写",
    "composer.use_llm.off": "仅模板",
    "composer.widget.natural_language_tooltip": "自然语言描述，例如：银发少女站在雨后的霓虹街道上",
    "composer.head_tooltip": "展开 / 收起角色行与风格行",
    "composer.placeholder.character_name": "自然语言-角色名字",
    "composer.placeholder.character_trigger": "lora激活词",
    "composer.placeholder.character_outfit": "英文标签（可留空）",
    "composer.placeholder.style_name": "自然语言-画师/风格",
    "composer.placeholder.style_trigger": "lora激活词",
    "composer.outfit_tooltip": "纯英文装扮标签，会紧跟角色 LoRA 触发词注入正向提示词；也可填预设服装名（如「原皮」「假日皮肤」）",
    "composer.add_character": "【添加指定的角色lora激活词】",
    "composer.add_style": "【添加指定的画师画风/风格激活词】",
    "composer.add_character_tooltip": "追加一个角色 LoRA 激活词行",
    "composer.add_style_tooltip": "追加一个画师/风格 LoRA 激活词行",
    "composer.rewrite": "【重写提示词】",
    "composer.rewrite_tooltip": "调用 /nl2prompt/rewrite 立即重写提示词（不触发出图）",
    "composer.reuse_last": "【沿用上一轮】",
    "composer.reuse_last_tooltip": "把对话历史最后一轮的正/负向提示词设为上轮重写结果",
    "composer.settings": "⚙ 设置",
    "composer.settings_tooltip": "通用 / 提示词替换 / 系统提示词 / 基础词 / 提示词技能 / 对话历史",
    "composer.seed_line": "上轮种子: {seed} · 历史 {count} 轮",
    "composer.rewrite_status.notes_suffix": "（{notes}）",
    "composer.rewrite_status.empty": "尚未重写",
    "composer.rewrite_status.done": "质量 {quality} · 上轮重写: {analysis}",
    "composer.rewrite_status.done_no_quality": "上轮重写: {analysis}",
    "composer.rewrite_status.no_analysis": "（无说明）",
    "composer.rewrite_status.fallback_note": "回退：{code}",
    "composer.rewrite_status.issues_note": "校验问题 {count} 项",
    "composer.remove_character_row": "删除该角色行",
    "composer.remove_style_row": "删除该风格行",

    // ---------- composer 重写 / 沿用
    "composer.rewrite.running": "正在重写…",
    "composer.rewrite.empty_input": "重写失败：自然语言输入框为空",
    "composer.rewrite.failed": "重写失败：{error}",
    "composer.rewrite.failed_detail": "重写失败：{error}（{detail}）",
    "composer.rewrite.issues_suffix": "（校验问题：{issues}）",
    "composer.rewrite.no_positive": "重写失败：后端未返回 positive",
    "composer.reuse.no_history": "沿用失败：暂无对话历史",
    "composer.reuse.no_positive": "沿用失败：上一轮没有可复用的正向提示词",

    // ---------- composer 窗口 / Tab
    "composer.window.title": "提示词设置",
    "composer.tab.general": "通用",
    "composer.tab.replace": "提示词替换",
    "composer.tab.system_prompt": "系统提示词",
    "composer.tab.positive_base": "正面基础词",
    "composer.tab.negative_base": "负面基础词",
    "composer.tab.skill": "提示词技能",
    "composer.tab.history": "对话历史",

    // ---------- composer 通用 Tab
    "composer.general.hint": "通用设置保存在后端 user 设置里（不写入 workflow）。切换语言会立即生效。",
    "composer.general.locale_label": "界面语言",
    "composer.locale.auto": "跟随 ComfyUI",
    "composer.locale.zh": "中文",
    "composer.locale.en": "English",
    "composer.general.locale_hint": "切换后立即生效，并保存到后端设置。",
    "composer.general.positive_default_label": "正面基础词默认值 default_positive_base",
    "composer.general.positive_default_placeholder": "例如：masterpiece, best quality, very aesthetic",
    "composer.general.positive_default_hint": "这里只是「填充推荐值」的兜底来源；每个 workflow 自己的「正面基础词」优先级更高。",
    "composer.general.save": "保存通用设置",
    "composer.general.reload": "重新读取",
    "composer.general.saving": "正在保存通用设置…",
    "composer.general.saved": "已保存通用设置",
    "composer.general.save_failed": "保存失败：{error}",
    "composer.general.reloaded": "已重新读取通用设置",
    "composer.general.reload_failed": "读取失败：{error}",
    "composer.general.unavailable": "通用设置接口不可用（{error}）：保存会失败，显示值为默认",
    "composer.prompt_opt.title": "提示词优化",
    "composer.prompt_opt.hint": "这些开关只影响 LLM 的 token 消耗与缓存命中，不影响出图质量；默认全部开启。",
    "composer.prompt_opt.unavailable": "后端未返回开关设置（或读取失败），当前按默认值显示：全部开启",
    "composer.prompt_opt.dedupe_skill": "消除重复提示词",
    "composer.prompt_opt.dedupe_skill_hint": "关闭会恢复「skill 注入两遍」，多消耗约 45% 输入 token，仅用于对照排查。",
    "composer.prompt_opt.dedupe_skill_warn": "将显著增加 token 消耗",
    "composer.prompt_opt.static_selected": "固定系统提示前缀",
    "composer.prompt_opt.static_selected_hint": "关闭后换角色/服装时缓存前缀失效，每次都要重新计算。",
    "composer.prompt_opt.retry_multiturn": "重试使用多轮对话",
    "composer.prompt_opt.retry_multiturn_hint": "关闭后退回旧的重试方式（重建整个上下文）。",
    "composer.general.locale_saved": "语言已切换为{locale}，立即生效",

    // ---------- composer 提示词替换 Tab
    "composer.replace.hint": "自然语言输入框中出现左侧词语/句子时，会把右侧 Prompt 加入指定的正向（positive）或负向（negative）提示词。匹配不区分大小写。",
    "composer.replace.find_placeholder": "自然语言中的词语/句子",
    "composer.replace.prompt_placeholder": "转换为的 Prompt",
    "composer.replace.target_positive": "正向 positive",
    "composer.replace.target_negative": "负向 negative",
    "composer.replace.remove_rule": "删除该替换规则",
    "composer.replace.row_hint": "自然语言包含左侧词语时，右侧 Prompt 会加入选中目标",
    "composer.replace.empty": "暂无替换规则",
    "composer.replace.add": "＋ 添加替换规则",

    // ---------- composer 系统提示词 Tab
    "composer.system.hint": "用户的附加系统提示词：非空时排在 skill 条款前面（可改风格，skill 约束仍生效）。留空则只使用 skill 内置提示词。可用占位符：{{character_presets}}、{{style_presets}}、{{selected}}。",
    "composer.system.placeholder": "留空 = 只使用 skill 内置系统提示词",
    "composer.system.restore": "恢复默认",
    "composer.system.confirm_restore": "确定恢复默认系统提示词？当前内容将丢失。",
    "composer.system.reading_default": "正在读取默认系统提示词…",
    "composer.system.fallback_used": "接口不可用，已填入内置默认提示词（尚未保存）",
    "composer.system.loaded": "已载入默认系统提示词（尚未保存）",
    "composer.system.cleared": "已清空：将只使用 skill 内置系统提示词（尚未保存）",

    // ---------- composer 正面基础词 Tab
    "composer.positive_base.hint": "最终正向提示词以它开头；角色/风格激活词与自然语言结果追加在后面。留空表示不注入。",
    "composer.positive_base.placeholder": "例如：masterpiece, best quality, very aesthetic",
    "composer.positive_base.fill": "填充推荐值",
    "composer.positive_base.reading": "正在读取推荐正面词…",
    "composer.positive_base.filled_skill": "已填入 skill 推荐正面词（尚未保存）",
    "composer.positive_base.filled_builtin": "已填入内置推荐正面词（尚未保存）",

    // ---------- composer 负面基础词 Tab
    "composer.negative_base.hint": "最终负向提示词以此为基础；角色/风格的抑制词与负向替换规则会追加到它后面。留空表示仅使用 skill 提供的负面词。",
    "composer.negative_base.placeholder": "例如：lowres, bad anatomy, text, watermark",
    "composer.negative_base.fill": "填充推荐值",
    "composer.negative_base.reading": "正在读取推荐负面词…",
    "composer.negative_base.filled_skill": "已填入 skill 推荐负面词（尚未保存）",
    "composer.negative_base.filled_builtin": "接口不可用，已填入内置推荐负面词（尚未保存）",

    // ---------- composer 技能 Tab
    "composer.skill.hint": "提示词技能（Skill）由后端 plugin/skills/*.json 提供，决定标签顺序、质量词与校验规则。改动会先写入本 workflow，点「保存到服务器」才写回后端文件。",
    "composer.skill.label": "技能 skill",
    "composer.skill.target_model": "目标模型：{model}",
    "composer.skill.target_model_unknown": "目标模型：—",
    "composer.skill.editor_placeholder": "该 skill 的 system_prompt（可编辑）",
    "composer.skill.unavailable_option": "（提示词技能接口不可用）",
    "composer.skill.unavailable_placeholder": "接口不可用时使用内置默认提示词（无法编辑保存）",
    "composer.skill.unavailable_status": "提示词技能接口不可用（{error}），已降级为内置默认",
    "composer.skill.restore_default": "恢复默认",
    "composer.skill.confirm_restore": "确定重新拉取该 skill 的服务器内容？当前编辑将丢失。",
    "composer.skill.reading": "正在重新拉取…",
    "composer.skill.rewritten": "已重新拉取服务器内容（尚未保存）",
    "composer.skill.save_server": "保存到服务器",
    "composer.skill.saving": "正在保存到服务器…",
    "composer.skill.saved": "已保存到服务器",
    "composer.skill.save_failed": "保存失败：{error}",
    "composer.skill.no_skill": "保存失败：没有可用的 skill",
    "composer.skill.no_content": "保存失败：未取到该 skill 的服务器内容（接口不可用）",
    "composer.skill.select_hint": "已选择 skill：{id}（保存后写入 workflow）",
    "composer.skill.new": "＋ 新建 skill",
    "composer.skill.new_tooltip": "在后端 plugin/skills/ 下新建一个 skill 文件",
    "composer.skill.delete": "删除 skill",
    "composer.skill.delete_tooltip": "删除当前 skill 文件（默认 skill 受保护）",
    "composer.skill.save_as": "另存为…",
    "composer.skill.save_as_tooltip": "把当前编辑内容另存为新的 skill",
    "composer.skill.prompt.new_id": "新 skill 的 id（小写字母/数字/连字符）：",
    "composer.skill.prompt.display_name": "显示名（例如 Anima 柔和风）：",
    "composer.skill.prompt.save_as_id": "另存为新的 skill id（小写字母/数字/连字符）：",
    "composer.skill.confirm_copy": "是否从当前 skill（{id}）复制内容？\n确定 = 复制内容；取消 = 创建空白 skill。",
    "composer.skill.bad_id": "id 只允许小写字母、数字、连字符（例如 anima-soft）",
    "composer.skill.creating": "正在新建 skill…",
    "composer.skill.new_failed": "新建失败：{error}",
    "composer.skill.created": "已新建 skill：{id}",
    "composer.skill.created_unrefreshed": "已新建 skill：{id}（列表未刷新到，请点「恢复默认」重拉）",
    "composer.skill.deleting": "正在删除 skill…",
    "composer.skill.delete_no_selected": "删除失败：没有选中的 skill",
    "composer.skill.confirm_delete": "确定删除 skill「{id}」？该操作不可撤销。",
    "composer.skill.protected": "默认 skill 不能删除",
    "composer.skill.delete_failed": "删除失败：{error}",
    "composer.skill.deleted": "已删除该 skill",
    "composer.skill.save_as_exists": "另存为失败：{id} 已存在",
    "composer.skill.save_as_no_content": "另存为失败：未取到当前 skill 的服务端内容",
    "composer.skill.save_as_copy_suffix": "{name} 副本",
    "composer.skill.saving_as": "正在另存为…",
    "composer.skill.save_as_failed": "另存为失败：{error}",
    "composer.skill.saved_as": "已另存为 {id}",
    "composer.skill.saved_as_unrefreshed": "已另存为 {id}（列表未刷新到，请点「恢复默认」重拉）",

    // ---------- composer 对话历史 Tab
    "composer.history.summary": "共 {count} 轮（上限 {max}，超出丢弃最旧）",
    "composer.history.empty": "暂无历史",
    "composer.history.clear": "清空历史",
    "composer.history.confirm_clear": "确定清空全部对话历史？该操作保存后不可撤销。",
    "composer.history.cleared": "已清空对话历史（保存后生效）",
    "composer.history.entry_head": "#{index} {time} · {text}",
    "composer.history.no_text": "（无自然语言）",
    "composer.history.positive_line": "Positive: {text}",
    "composer.history.negative_line": "Negative: {text}",
    "composer.history.empty_value": "（空）",
    "composer.history.reuse": "复用该轮提示词",
    "composer.history.reuse_failed": "该轮没有可复用的正向提示词",
    "composer.history.reused": "已选择第 {index} 轮的提示词（保存后生效）",

    // ---------- LLM 节点正面
    "llm.widget.model_type": "模型类型",
    "llm.widget.gpu": "GPU",
    "llm.widget.model_path": "模型路径[本地]",
    "llm.widget.model_file": "选择模型[本地]",
    "llm.widget.provider": "供应商[云端]",
    "llm.widget.base_url": "供应商 base_url[云端]",
    "llm.widget.cloud_model": "供应商模型[云端]",
    "llm.widget.model_type_tooltip": "本地模型走 llama-server；云端模型走 OpenAI 兼容 /v1 接口",
    "llm.widget.gpu_tooltip": "选择显卡（本地模式生效）",
    "llm.widget.model_path_tooltip": "GGUF 模型根目录（绝对路径）；留空时自动探测 LM Studio / Ollama 默认路径",
    "llm.widget.model_file_tooltip": "由「刷新模型列表」按模型路径扫描得到",
    "llm.model_file.placeholder": "(请先选择路径后刷新)",
    "llm.widget.provider_tooltip": "云端供应商；切换后自动带出 base_url",
    "llm.widget.base_url_tooltip": "留空则按供应商注册表默认值填充",
    "llm.widget.cloud_model_tooltip": "云端模型代号；可在「LLM 设置 - 云端模型」里拉取或手填",
    "llm.widget.model_dir_tooltip": "HF 模型根目录（含 config.json 的目录）；留空自动探测",
    "llm.widget.dtype_tooltip": "权重精度：fp16 最快；显存不足可试 bf16 或 auto",
    "llm.widget.unload_tooltip": "本次生成后立即卸载模型并释放显存",
    "llm.refresh_models": "刷新模型列表",
    "llm.refresh_models_tooltip": "递归扫描模型目录下的 {ext}（含子目录；路径留空时自动探测默认目录）",
    "llm.settings": "⚙ 设置",
    "llm.settings_tooltip": "云端模型 / 采样参数 / 运行设置与密钥",
    "llm.model_hint": "当前云端模型：{model}（在设置中修改）",
    "llm.model_hint.unset": "未设置",
    "llm.model_hint_tooltip": "在「LLM 设置 - 云端模型」中选择或填写",
    "llm.provider_hint": "当前：{label} · {base_url}{suffix}",
    "llm.provider_hint.unverified": "（未校准）",
    "llm.provider_hint.custom": "自定义",
    "llm.provider_hint.unselected": "（未选择）",
    "llm.provider_note_fallback": "离线降级数据（/nl2prompt/providers 不可用时使用）",
    "llm.provider_note_custom": "base_url 与模型名均由用户填写",

    // ---------- LLM 扫描
    "llm.scan.need_path": "请先填写模型路径",
    "llm.scan.running": "扫描中…",
    "llm.scan.unavailable": "扫描接口不可用",
    "llm.scan.failed": "扫描失败：{error}",
    "llm.scan.empty": "未找到 {ext} 文件",
    "llm.scan.found": "已找到 {count} 个模型",

    // ---------- LLM 窗口 / Tab
    "llm.window.title": "LLM 设置",
    "llm.tab.sampling": "采样参数",
    "llm.tab.cloud": "云端模型",
    "llm.tab.runtime": "运行设置",
    "llm.tab.local": "本地设置",

    // ---------- LLM 云端模型 Tab
    "llm.cloud.hint": "云端模型走 OpenAI 兼容 /v1 接口：先选供应商（自动带出 base_url），再点「拉取模型」读取 {base_url}/models。拉取需要 api_key（本页下方「供应商 api_key」保存，立即生效）。本页修改在点底部「保存」后写回节点。",
    "llm.cloud.provider_label": "供应商 provider",
    "llm.cloud.provider_unknown": "{id}（未知供应商）",
    "llm.cloud.base_url_label": "base_url",
    "llm.cloud.base_url_placeholder": "留空 = 按供应商默认（例如 https://api.deepseek.com/v1）",
    "llm.cloud.base_url_hint_custom": "使用自定义 base_url",
    "llm.cloud.base_url_hint_fallback": "留空 → 将使用 {base_url}",
    "llm.cloud.base_url_hint_required": "请填写 base_url",
    "llm.cloud.fetch_tooltip": "请求 {base_url}/models 拉取该供应商可用模型（需要已保存的 api_key）",
    "llm.cloud.fetch": "拉取模型",
    "llm.cloud.no_models": "尚未拉取模型列表。",
    "llm.cloud.cache_mismatch": "已缓存的 {count} 个模型属于 {base_url}，与当前 base_url 不同，请重新拉取。",
    "llm.cloud.cache_empty_base": "（空）",
    "llm.cloud.cache_expired": "缓存已过期（{count} 个模型），可重新拉取。",
    "llm.cloud.item_tooltip": "点击设置为 {id}",
    "llm.cloud.item_selected": "已设置为 {id}",
    "llm.cloud.model_label": "云端模型 cloud_model",
    "llm.cloud.model_placeholder": "手填模型代号，例如 deepseek-chat / gpt-4o-mini",
    "llm.cloud.model_hint": "手填内容在点底部「保存」后写入节点；点上方模型列表会立即写入。",
    "llm.cloud.provider_switched": "已切换供应商：{label}",
    "llm.cloud.need_base_url": "请先选择供应商或填写 base_url",
    "llm.cloud.fetching": "正在拉取模型…",
    "llm.cloud.fetch_failed": "拉取失败：{error}",
    "llm.cloud.fetch_failed_detail": "拉取失败：{error}（{detail}）",
    "llm.cloud.fetch_empty": "未返回任何模型",
    "llm.cloud.fetch_ok": "已获取 {count} 个模型",

    // ---------- LLM 采样参数 Tab
    "llm.sampling.hint": "这些参数直接决定 LLM 采样行为，属于模型推理参数，保存在 _llm_json holder 中。",
    "llm.sampling.top_k": "Top K（0–100）",
    "llm.sampling.top_p": "Top P（0–1）",
    "llm.sampling.temperature": "温度 temperature（0–2）",
    "llm.sampling.max_tokens": "最大 token 数 max_tokens",
    "llm.sampling.seed": "种子 seed",
    "llm.sampling.seed_random": "每次随机（seed = -1）",
    "llm.sampling.seed_hint_random": "当前：每次执行使用随机种子（seed = -1）",
    "llm.sampling.seed_hint_fixed": "当前：固定种子 {seed}（每次执行结果可复现）",
    "llm.sampling.last_seed": "上轮种子",
    "llm.sampling.top_k_cloud_hint": "云端 OpenAI 兼容接口通常不支持 top_k，已置灰（仅本地模型生效）",

    // ---------- LLM 运行设置 Tab
    "llm.runtime.hint": "本地模型：ctx 会作为 llama-server 的 -c 上下文长度，timeout 作为启动/请求超时。",
    "llm.runtime.ctx": "上下文长度 ctx（512–32768）",
    "llm.runtime.timeout": "超时 timeout（10–600 秒）",
    "llm.runtime.api_key_label": "供应商 api_key",
    "llm.runtime.api_key_placeholder": "留空则不修改已保存的密钥",
    "llm.runtime.api_key_hint": "密钥只会通过 POST /nl2prompt/secret 写入服务端（环境变量或 data/secrets.json），不会进入 workflow。",
    "llm.runtime.save_key": "保存密钥",
    "llm.runtime.secret_loading": "密钥状态：读取中…",
    "llm.runtime.secret_unavailable": "无法读取密钥状态（接口不可用）",
    "llm.runtime.secret_status": "密钥状态：{text}",
    "llm.runtime.secret_configured": "已配置（来源: {source}）",
    "llm.runtime.secret_unconfigured": "未配置",
    "llm.runtime.secret_source_env": "环境变量",
    "llm.runtime.secret_source_file": "密钥文件",
    "llm.runtime.secret_source_unknown": "未知来源",
    "llm.runtime.key_empty": "请输入 api_key，或用空字符串提交以清除已保存密钥",
    "llm.runtime.key_saving": "正在保存密钥…",
    "llm.runtime.key_saved": "密钥已保存",
    "llm.runtime.key_cleared": "密钥已清除",
    "llm.runtime.key_failed": "密钥保存失败：{error}",
    "llm.runtime.key_failed_detail": "密钥保存失败：{error}（{detail}）",
    "llm.runtime.test": "测试连接",
    "llm.runtime.test_tooltip": "会调用 POST /nl2prompt/runtime/start（本地启动/复用 llama-server，云端校验端点）。",
    "llm.runtime.testing": "正在连接…",
    "llm.runtime.test_ok": "连接成功：{model} @ {base_url}",
    "llm.runtime.test_ok_no_url": "连接成功：{model}",
    "llm.runtime.test_failed": "连接失败：{error}",
    "llm.runtime.test_failed_detail": "连接失败：{error}（{detail}）",
    "llm.runtime.test_no_model": "(未返回模型名)",

    // ---------- 错误码（后端 error 字段）
    "err.network": "网络不可用",
    "err.timeout": "请求超时",
    "err.dns": "域名解析失败",
    "err.no_key": "未配置 api_key",
    "err.bad_url": "base_url 无效",
    "err.bad_json": "响应不是合法 JSON",
    "err.bad_request": "请求参数不合法",
    "err.http_401": "鉴权失败（401）",
    "err.http_403": "无权限（403）",
    "err.http_404": "接口不存在（404）",
    "err.http_429": "请求过于频繁（429）",
    "err.http_500": "服务端错误（500）",
    "err.http_502": "网关错误（502）",
    "err.http_503": "服务不可用（503）",
    "err.llm_not_connected": "LLM 未连接",
    "err.llm_key_missing": "缺少 api_key",
    "err.llm_timeout": "LLM 超时",
    "err.llm_bad_json": "LLM 返回无法解析",
    "err.llm_empty_positive": "LLM 返回的正向提示词为空",
    "err.llm_http_401": "LLM 鉴权失败（401）",
    "err.llm_http_403": "LLM 无权限（403）",
    "err.llm_http_429": "LLM 限流（429）",
    "err.llm_unexpected_error": "LLM 调用异常",
    "err.local_model_missing": "本地模型文件不存在",
    "err.runtime_start_failed": "运行时启动失败",
    "err.runtime_unavailable": "运行时不可用",
    "err.path_not_found": "路径不存在",
    "err.store_unavailable": "数据层不可用",
    "err.store_corrupt": "数据文件损坏",
    "err.skill_unavailable": "提示词技能接口不可用",
    "err.skill_load_failed": "skills 读取失败",
    "err.skill_invalid": "skill 内容校验未通过",
    "err.skill_exists": "skill 已存在",
    "err.skill_not_found": "skill 不存在",
    "err.skill_protected": "默认 skill 受保护",
    "err.skill_save_failed": "skill 写入失败",
    "err.bad_skill_id": "skill id 不合法",
    "err.scan_failed": "扫描失败",
    "err.empty": "无数据",
};

/* ------------------------------------------------------------------ en 字典 */

const EN = {
    "common.list_separator": ", ",
    // Node library (left "Nodes" panel) display names
    "node.title.composer": "Natural Language Prompt",
    "node.title.composer_legacy": "(Legacy) Natural Language Prompt",
    "node.title.llm_gguf": "Load LLM (GGUF)",
    "node.title.llm_safetensors": "Load LLM (safetensors)",
    "node.title.llm_cloud": "Load LLM (Cloud)",
    "node.title.llm_legacy": "Load LLM (Legacy · local/cloud)",
    "node.title.llm_legacy_v2": "(Legacy) Load LLM",
    "node.title.lora_stack": "LoRA Stack Loader",
    "node.title.lora_stack_legacy": "(Legacy) LoRA Stack Loader",
    "node.desc.composer": "Assemble a prompt from natural language + character/style LoRA trigger words + text replacements; optionally rewrite with an LLM, falling back to the template on failure. Emits CONDITIONING directly when clip is connected.",
    "node.desc.composer_legacy": "Assemble a prompt from natural language + character/style LoRA trigger words + text replacements; optionally rewrite with an LLM, falling back to the template on failure. Emits CONDITIONING directly when clip is connected.",
    "node.desc.llm_gguf": "Local GGUF model: the plugin starts llama-server on the selected GPU and outputs LLM_MODEL (never contains the api_key).",
    "node.desc.llm_safetensors": "Local HuggingFace safetensors model (directory must contain config.json): loaded by transformers onto the selected GPU; falls back and reports local_model_oom when VRAM is short.",
    "node.desc.llm_cloud": "Cloud OpenAI-compatible endpoint: only produces LLM_MODEL (base_url / model / sampling parameters); the api_key is resolved by providers at call time and never enters the workflow.",
    "node.desc.llm_legacy": "Configure a local llama-server or a cloud OpenAI-compatible endpoint; outputs LLM_MODEL (never contains the api_key).",
    "node.desc.llm_legacy_v2": "Configure a local llama-server or a cloud OpenAI-compatible endpoint; outputs LLM_MODEL (never contains the api_key).",
    "node.desc.lora_stack": "Apply the lora_stack produced by NL2PromptComposer in order; a failed LoRA logs a warning and is skipped.",
    "node.desc.lora_stack_legacy": "Apply the lora_stack produced by NL2PromptComposer in order; a failed LoRA logs a warning and is skipped.",

    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.close": "Close",
    "common.saving": "Saving…",
    "common.saved": "Saved",
    "common.save_failed": "Save failed",
    "common.save_failed_detail": "Save failed: {detail}",
    "common.tab_fallback": "Tab {index}",
    "common.tab_render_failed": "Panel failed to render: {detail}",
    "common.panel_render_failed": "Panel failed to render: {detail}",

    "composer.head": "⚪ Natural language",
    "composer.use_llm.on": "LLM rewrite",
    "composer.use_llm.off": "Template only",
    "composer.widget.natural_language_tooltip": "Natural-language description, e.g. a silver-haired girl on a rainy neon street",
    "composer.head_tooltip": "Expand / collapse character and style rows",
    "composer.placeholder.character_name": "Character name",
    "composer.placeholder.character_trigger": "LoRA trigger words",
    "composer.placeholder.character_outfit": "English tags (optional)",
    "composer.placeholder.style_name": "Artist / style",
    "composer.placeholder.style_trigger": "LoRA trigger words",
    "composer.outfit_tooltip": "Plain English outfit tags, injected right after the character LoRA trigger words. A preset outfit name (e.g. default skin) also works.",
    "composer.add_character": "[Add character LoRA triggers]",
    "composer.add_style": "[Add artist / style triggers]",
    "composer.add_character_tooltip": "Append a character LoRA trigger row",
    "composer.add_style_tooltip": "Append an artist / style LoRA trigger row",
    "composer.rewrite": "[Rewrite prompt]",
    "composer.rewrite_tooltip": "Call /nl2prompt/rewrite to rewrite now (does not queue a generation)",
    "composer.reuse_last": "[Reuse last round]",
    "composer.reuse_last_tooltip": "Use the positive/negative of the last dialog round as the rewrite result",
    "composer.settings": "⚙ Settings",
    "composer.settings_tooltip": "General / replacements / system prompt / base words / skills / history",
    "composer.seed_line": "Last seed: {seed} · History {count}",
    "composer.rewrite_status.notes_suffix": " ({notes})",
    "composer.rewrite_status.empty": "Not rewritten yet",
    "composer.rewrite_status.done": "Score {quality} · Last rewrite: {analysis}",
    "composer.rewrite_status.done_no_quality": "Last rewrite: {analysis}",
    "composer.rewrite_status.no_analysis": "(no description)",
    "composer.rewrite_status.fallback_note": "fallback: {code}",
    "composer.rewrite_status.issues_note": "{count} check issue(s)",
    "composer.remove_character_row": "Delete this character row",
    "composer.remove_style_row": "Delete this style row",

    "composer.rewrite.running": "Rewriting…",
    "composer.rewrite.empty_input": "Rewrite failed: natural language input is empty",
    "composer.rewrite.failed": "Rewrite failed: {error}",
    "composer.rewrite.failed_detail": "Rewrite failed: {error} ({detail})",
    "composer.rewrite.issues_suffix": " (check issues: {issues})",
    "composer.rewrite.no_positive": "Rewrite failed: backend returned no positive prompt",
    "composer.reuse.no_history": "Reuse failed: no dialog history",
    "composer.reuse.no_positive": "Reuse failed: last round has no reusable positive prompt",

    "composer.window.title": "Prompt settings",
    "composer.tab.general": "General",
    "composer.tab.replace": "Replacements",
    "composer.tab.system_prompt": "System prompt",
    "composer.tab.positive_base": "Positive base",
    "composer.tab.negative_base": "Negative base",
    "composer.tab.skill": "Skills",
    "composer.tab.history": "History",

    "composer.general.hint": "General settings live in the backend user settings (not in the workflow). Switching language takes effect immediately.",
    "composer.general.locale_label": "Interface language",
    "composer.locale.auto": "Follow ComfyUI",
    "composer.locale.zh": "中文",
    "composer.locale.en": "English",
    "composer.general.locale_hint": "Takes effect immediately and is saved to the backend settings.",
    "composer.general.positive_default_label": "Default positive base (default_positive_base)",
    "composer.general.positive_default_placeholder": "e.g. masterpiece, best quality, very aesthetic",
    "composer.general.positive_default_hint": "Only a fallback source for the Fill recommended button; each workflow's own Positive base has priority.",
    "composer.general.save": "Save general settings",
    "composer.general.reload": "Reload",
    "composer.general.saving": "Saving general settings…",
    "composer.general.saved": "General settings saved",
    "composer.general.save_failed": "Save failed: {error}",
    "composer.general.reloaded": "General settings reloaded",
    "composer.general.reload_failed": "Reload failed: {error}",
    "composer.general.unavailable": "General settings API unavailable ({error}): saving will fail, showing defaults",
    "composer.prompt_opt.title": "Prompt optimization",
    "composer.prompt_opt.hint": "These switches only affect LLM token usage and cache hits, never image quality. All on by default.",
    "composer.prompt_opt.unavailable": "Backend did not return the switches (or reading failed); showing defaults: all on",
    "composer.prompt_opt.dedupe_skill": "Dedupe system prompt",
    "composer.prompt_opt.dedupe_skill_hint": "Turning this off injects the skill twice and costs about 45% more input tokens. For troubleshooting only.",
    "composer.prompt_opt.dedupe_skill_warn": "Will significantly increase token usage",
    "composer.prompt_opt.static_selected": "Static system prefix",
    "composer.prompt_opt.static_selected_hint": "Turning this off invalidates the cached prefix whenever the character or outfit changes.",
    "composer.prompt_opt.retry_multiturn": "Multi-turn retry",
    "composer.prompt_opt.retry_multiturn_hint": "Turning this off falls back to rebuilding the whole context on retry.",
    "composer.general.locale_saved": "Language switched to {locale}; applied immediately",

    "composer.replace.hint": "When the natural language input contains the left text, the right prompt is added to the selected positive or negative target. Matching is case-insensitive.",
    "composer.replace.find_placeholder": "Word / sentence in natural language",
    "composer.replace.prompt_placeholder": "Prompt to inject",
    "composer.replace.target_positive": "Positive",
    "composer.replace.target_negative": "Negative",
    "composer.replace.remove_rule": "Delete this rule",
    "composer.replace.row_hint": "If the natural language contains the left text, the right prompt is added to the selected target",
    "composer.replace.empty": "No replacement rules yet",
    "composer.replace.add": "+ Add replacement rule",

    "composer.system.hint": "Extra user system prompt: when non-empty it is placed before the skill clauses (you can change style, skill constraints still apply). Leave empty to use the skill prompt only. Placeholders: {{character_presets}}, {{style_presets}}, {{selected}}.",
    "composer.system.placeholder": "Empty = use the skill system prompt only",
    "composer.system.restore": "Restore default",
    "composer.system.confirm_restore": "Restore the default system prompt? Current content will be lost.",
    "composer.system.reading_default": "Loading default system prompt…",
    "composer.system.fallback_used": "API unavailable; built-in default prompt inserted (not saved)",
    "composer.system.loaded": "Default system prompt loaded (not saved)",
    "composer.system.cleared": "Cleared: only the skill system prompt will be used (not saved)",

    "composer.positive_base.hint": "The final positive prompt starts with this; character/style triggers and the natural-language result follow. Leave empty to inject nothing.",
    "composer.positive_base.placeholder": "e.g. masterpiece, best quality, very aesthetic",
    "composer.positive_base.fill": "Fill recommended",
    "composer.positive_base.reading": "Loading recommended positive words…",
    "composer.positive_base.filled_skill": "Filled positive words from the skill (not saved)",
    "composer.positive_base.filled_builtin": "Filled built-in positive words (not saved)",

    "composer.negative_base.hint": "The final negative prompt starts with this; character/style suppressions and negative replacement rules follow. Leave empty to use the skill negative words only.",
    "composer.negative_base.placeholder": "e.g. lowres, bad anatomy, text, watermark",
    "composer.negative_base.fill": "Fill recommended",
    "composer.negative_base.reading": "Loading recommended negative words…",
    "composer.negative_base.filled_skill": "Filled negative words from the skill (not saved)",
    "composer.negative_base.filled_builtin": "API unavailable; built-in negative words filled (not saved)",

    "composer.skill.hint": "Prompt skills come from the backend plugin/skills/*.json files and define tag order, quality words and validation. Edits first go to this workflow; click Save to server to write the file back.",
    "composer.skill.label": "Skill",
    "composer.skill.target_model": "Target model: {model}",
    "composer.skill.target_model_unknown": "Target model: —",
    "composer.skill.editor_placeholder": "This skill's system_prompt (editable)",
    "composer.skill.unavailable_option": "(skills API unavailable)",
    "composer.skill.unavailable_placeholder": "API unavailable: built-in default prompt is used (cannot be edited or saved)",
    "composer.skill.unavailable_status": "Skills API unavailable ({error}); fell back to the built-in default",
    "composer.skill.restore_default": "Restore default",
    "composer.skill.confirm_restore": "Reload this skill from the server? Current edits will be lost.",
    "composer.skill.reading": "Reloading…",
    "composer.skill.rewritten": "Reloaded from the server (not saved)",
    "composer.skill.save_server": "Save to server",
    "composer.skill.saving": "Saving to server…",
    "composer.skill.saved": "Saved to server",
    "composer.skill.save_failed": "Save failed: {error}",
    "composer.skill.no_skill": "Save failed: no skill selected",
    "composer.skill.no_content": "Save failed: no server content for this skill (API unavailable)",
    "composer.skill.select_hint": "Skill selected: {id} (written to the workflow on save)",
    "composer.skill.new": "+ New skill",
    "composer.skill.new_tooltip": "Create a new skill file under backend plugin/skills/",
    "composer.skill.delete": "Delete skill",
    "composer.skill.delete_tooltip": "Delete the current skill file (the default skill is protected)",
    "composer.skill.save_as": "Save as…",
    "composer.skill.save_as_tooltip": "Save the current edits as a new skill",
    "composer.skill.prompt.new_id": "New skill id (lowercase letters/digits/hyphens):",
    "composer.skill.prompt.display_name": "Display name (e.g. Anima Soft):",
    "composer.skill.prompt.save_as_id": "Save as new skill id (lowercase letters/digits/hyphens):",
    "composer.skill.confirm_copy": "Copy content from the current skill ({id})?\nOK = copy; Cancel = create an empty skill.",
    "composer.skill.bad_id": "id may only contain lowercase letters, digits and hyphens (e.g. anima-soft)",
    "composer.skill.creating": "Creating skill…",
    "composer.skill.new_failed": "Create failed: {error}",
    "composer.skill.created": "Skill created: {id}",
    "composer.skill.created_unrefreshed": "Skill created: {id} (list not refreshed, click Restore default to reload)",
    "composer.skill.deleting": "Deleting skill…",
    "composer.skill.delete_no_selected": "Delete failed: no skill selected",
    "composer.skill.confirm_delete": "Delete skill {id}? This cannot be undone.",
    "composer.skill.protected": "The default skill cannot be deleted",
    "composer.skill.delete_failed": "Delete failed: {error}",
    "composer.skill.deleted": "Skill deleted",
    "composer.skill.save_as_exists": "Save as failed: {id} already exists",
    "composer.skill.save_as_no_content": "Save as failed: no server content for the current skill",
    "composer.skill.save_as_copy_suffix": "{name} copy",
    "composer.skill.saving_as": "Saving as…",
    "composer.skill.save_as_failed": "Save as failed: {error}",
    "composer.skill.saved_as": "Saved as {id}",
    "composer.skill.saved_as_unrefreshed": "Saved as {id} (list not refreshed, click Restore default to reload)",

    "composer.history.summary": "{count} round(s) (max {max}, oldest dropped)",
    "composer.history.empty": "No history",
    "composer.history.clear": "Clear history",
    "composer.history.confirm_clear": "Clear all dialog history? This cannot be undone once saved.",
    "composer.history.cleared": "Dialog history cleared (applies on save)",
    "composer.history.entry_head": "#{index} {time} · {text}",
    "composer.history.no_text": "(no natural language)",
    "composer.history.positive_line": "Positive: {text}",
    "composer.history.negative_line": "Negative: {text}",
    "composer.history.empty_value": "(empty)",
    "composer.history.reuse": "Reuse this round",
    "composer.history.reuse_failed": "This round has no reusable positive prompt",
    "composer.history.reused": "Round {index} selected (applies on save)",

    "llm.widget.model_type": "Model type",
    "llm.widget.gpu": "GPU",
    "llm.widget.model_path": "Model path [local]",
    "llm.widget.model_file": "Model file [local]",
    "llm.widget.provider": "Provider [cloud]",
    "llm.widget.base_url": "Provider base_url [cloud]",
    "llm.widget.cloud_model": "Cloud model [cloud]",
    "llm.widget.model_type_tooltip": "Local model runs on llama-server; cloud model uses an OpenAI-compatible /v1 endpoint",
    "llm.widget.gpu_tooltip": "GPU to use (local mode)",
    "llm.widget.model_path_tooltip": "GGUF model root (absolute path); auto-detects LM Studio / Ollama defaults when empty",
    "llm.widget.model_file_tooltip": "Filled by Refresh model list, scanned from the model path",
    "llm.model_file.placeholder": "(select a folder, then refresh)",
    "llm.widget.provider_tooltip": "Cloud provider; switching auto-fills base_url",
    "llm.widget.base_url_tooltip": "Empty = use the provider registry default",
    "llm.widget.cloud_model_tooltip": "Cloud model id; fetch or type it in LLM Settings - Cloud model",
    "llm.widget.model_dir_tooltip": "HF model root directory (must contain config.json); empty = auto-detect",
    "llm.widget.dtype_tooltip": "Weight precision: fp16 is fastest; try bf16 or auto when VRAM is tight",
    "llm.widget.unload_tooltip": "Unload the model right after this run to free VRAM",
    "llm.refresh_models": "Refresh model list",
    "llm.refresh_models_tooltip": "Recursively scan the model directory for {ext} (subfolders included; auto-detects defaults when the path is empty)",
    "llm.settings": "⚙ Settings",
    "llm.settings_tooltip": "Cloud model / sampling / runtime and API key",
    "llm.model_hint": "Current cloud model: {model} (edit in Settings)",
    "llm.model_hint.unset": "not set",
    "llm.model_hint_tooltip": "Select or type it in LLM Settings - Cloud model",
    "llm.provider_hint": "Current: {label} · {base_url}{suffix}",
    "llm.provider_hint.unverified": " (unverified)",
    "llm.provider_hint.custom": "custom",
    "llm.provider_hint.unselected": "(not selected)",
    "llm.provider_note_fallback": "Offline fallback data (/nl2prompt/providers unavailable)",
    "llm.provider_note_custom": "base_url and model name are both entered by the user",

    "llm.scan.need_path": "Please fill in the model path first",
    "llm.scan.running": "Scanning…",
    "llm.scan.unavailable": "Scan API unavailable",
    "llm.scan.failed": "Scan failed: {error}",
    "llm.scan.empty": "No {ext} files found",
    "llm.scan.found": "Found {count} model(s)",

    "llm.window.title": "LLM settings",
    "llm.tab.sampling": "Sampling",
    "llm.tab.cloud": "Cloud model",
    "llm.tab.runtime": "Runtime",
    "llm.tab.local": "Local settings",

    "llm.cloud.hint": "Cloud models use an OpenAI-compatible /v1 endpoint: pick a provider (base_url is filled in), then Fetch models to read {base_url}/models. Fetching needs an api_key (save it in the \"Provider api_key\" section below; it applies immediately). Changes on this page are written back after clicking Save.",
    "llm.cloud.provider_label": "Provider",
    "llm.cloud.provider_unknown": "{id} (unknown provider)",
    "llm.cloud.base_url_label": "base_url",
    "llm.cloud.base_url_placeholder": "Empty = provider default (e.g. https://api.deepseek.com/v1)",
    "llm.cloud.base_url_hint_custom": "Using a custom base_url",
    "llm.cloud.base_url_hint_fallback": "Empty → will use {base_url}",
    "llm.cloud.base_url_hint_required": "Please fill in base_url",
    "llm.cloud.fetch_tooltip": "GET {base_url}/models for this provider (requires a saved api_key)",
    "llm.cloud.fetch": "Fetch models",
    "llm.cloud.no_models": "No model list yet.",
    "llm.cloud.cache_mismatch": "{count} cached model(s) belong to {base_url}, which differs from the current base_url; fetch again.",
    "llm.cloud.cache_empty_base": "(empty)",
    "llm.cloud.cache_expired": "Cache expired ({count} model(s)); fetch again.",
    "llm.cloud.item_tooltip": "Click to set {id}",
    "llm.cloud.item_selected": "Set to {id}",
    "llm.cloud.model_label": "Cloud model (cloud_model)",
    "llm.cloud.model_placeholder": "Model id, e.g. deepseek-chat / gpt-4o-mini",
    "llm.cloud.model_hint": "Typed values are written to the node after clicking Save; clicking a model above writes immediately.",
    "llm.cloud.provider_switched": "Provider switched: {label}",
    "llm.cloud.need_base_url": "Please pick a provider or fill in base_url",
    "llm.cloud.fetching": "Fetching models…",
    "llm.cloud.fetch_failed": "Fetch failed: {error}",
    "llm.cloud.fetch_failed_detail": "Fetch failed: {error} ({detail})",
    "llm.cloud.fetch_empty": "No models returned",
    "llm.cloud.fetch_ok": "Fetched {count} model(s)",

    "llm.sampling.hint": "These parameters drive LLM sampling and are stored in the _llm_json holder.",
    "llm.sampling.top_k": "Top K (0-100)",
    "llm.sampling.top_p": "Top P (0-1)",
    "llm.sampling.temperature": "Temperature (0-2)",
    "llm.sampling.max_tokens": "Max tokens",
    "llm.sampling.seed": "Seed",
    "llm.sampling.seed_random": "Random every run (seed = -1)",
    "llm.sampling.seed_hint_random": "Current: a random seed every run (seed = -1)",
    "llm.sampling.seed_hint_fixed": "Current: fixed seed {seed} (reproducible)",
    "llm.sampling.last_seed": "Last seed",
    "llm.sampling.top_k_cloud_hint": "Cloud OpenAI-compatible APIs usually do not support top_k; disabled (local models only)",

    "llm.runtime.hint": "Local model: ctx becomes llama-server -c context length, timeout covers startup and requests.",
    "llm.runtime.ctx": "Context length ctx (512-32768)",
    "llm.runtime.timeout": "Timeout (10-600 s)",
    "llm.runtime.api_key_label": "Provider api_key",
    "llm.runtime.api_key_placeholder": "Empty = keep the saved key",
    "llm.runtime.api_key_hint": "The key is only sent via POST /nl2prompt/secret (env var or data/secrets.json); it never enters the workflow.",
    "llm.runtime.save_key": "Save key",
    "llm.runtime.secret_loading": "Key status: loading…",
    "llm.runtime.secret_unavailable": "Cannot read key status (API unavailable)",
    "llm.runtime.secret_status": "Key status: {text}",
    "llm.runtime.secret_configured": "configured (source: {source})",
    "llm.runtime.secret_unconfigured": "not configured",
    "llm.runtime.secret_source_env": "env var",
    "llm.runtime.secret_source_file": "secret file",
    "llm.runtime.secret_source_unknown": "unknown source",
    "llm.runtime.key_empty": "Enter an api_key, or submit an empty string to clear the saved key",
    "llm.runtime.key_saving": "Saving key…",
    "llm.runtime.key_saved": "Key saved",
    "llm.runtime.key_cleared": "Key cleared",
    "llm.runtime.key_failed": "Key save failed: {error}",
    "llm.runtime.key_failed_detail": "Key save failed: {error} ({detail})",
    "llm.runtime.test": "Test connection",
    "llm.runtime.test_tooltip": "Calls POST /nl2prompt/runtime/start (starts/reuses llama-server locally, validates the cloud endpoint).",
    "llm.runtime.testing": "Connecting…",
    "llm.runtime.test_ok": "Connected: {model} @ {base_url}",
    "llm.runtime.test_ok_no_url": "Connected: {model}",
    "llm.runtime.test_failed": "Connection failed: {error}",
    "llm.runtime.test_failed_detail": "Connection failed: {error} ({detail})",
    "llm.runtime.test_no_model": "(no model name returned)",

    "err.network": "network unavailable",
    "err.timeout": "request timed out",
    "err.dns": "DNS lookup failed",
    "err.no_key": "api_key not configured",
    "err.bad_url": "invalid base_url",
    "err.bad_json": "response is not valid JSON",
    "err.bad_request": "invalid request parameters",
    "err.http_401": "unauthorized (401)",
    "err.http_403": "forbidden (403)",
    "err.http_404": "not found (404)",
    "err.http_429": "too many requests (429)",
    "err.http_500": "server error (500)",
    "err.http_502": "bad gateway (502)",
    "err.http_503": "service unavailable (503)",
    "err.llm_not_connected": "LLM not connected",
    "err.llm_key_missing": "missing api_key",
    "err.llm_timeout": "LLM timed out",
    "err.llm_bad_json": "LLM response could not be parsed",
    "err.llm_empty_positive": "LLM returned an empty positive prompt",
    "err.llm_http_401": "LLM unauthorized (401)",
    "err.llm_http_403": "LLM forbidden (403)",
    "err.llm_http_429": "LLM rate limited (429)",
    "err.llm_unexpected_error": "unexpected LLM error",
    "err.local_model_missing": "local model file not found",
    "err.runtime_start_failed": "runtime failed to start",
    "err.runtime_unavailable": "runtime unavailable",
    "err.path_not_found": "path not found",
    "err.store_unavailable": "data layer unavailable",
    "err.store_corrupt": "data files are corrupt",
    "err.skill_unavailable": "skills API unavailable",
    "err.skill_load_failed": "failed to load skills",
    "err.skill_invalid": "skill content validation failed",
    "err.skill_exists": "skill already exists",
    "err.skill_not_found": "skill not found",
    "err.skill_protected": "the default skill is protected",
    "err.skill_save_failed": "failed to write skill",
    "err.bad_skill_id": "invalid skill id",
    "err.scan_failed": "scan failed",
    "err.empty": "no data",
};

const DICTS = { zh: ZH, en: EN };

/* ------------------------------------------------------------------ 状态 */

let userMode = "auto";
let resolvedLocale = "zh";
const listeners = new Set();
const warnedKeys = new Set();

export function currentMode() {
    return userMode;
}

export function currentLocale() {
    return resolvedLocale;
}

/** 订阅语言变化（返回取消函数）。 */
export function subscribeLocale(handler) {
    listeners.add(handler);
    return () => listeners.delete(handler);
}

function notify() {
    for (const handler of Array.from(listeners)) {
        try {
            handler(resolvedLocale, userMode);
        } catch (error) {
            console.error("[nl2prompt][i18n] locale listener 执行失败", error);
        }
    }
}

/* ------------------------------------------------------------------ t() */

export function t(key, params) {
    const dict = DICTS[resolvedLocale] || ZH;
    let text = dict[key];
    if (text === undefined) {
        text = ZH[key];
        if (text === undefined) {
            if (!warnedKeys.has(key)) {
                warnedKeys.add(key);
                console.warn(`[nl2prompt][i18n] 缺少文案 key：${key}`);
            }
            return key;
        }
    }
    if (params && typeof params === "object") {
        text = text.replace(/\{(\w+)\}/g, (match, name) => {
            const value = params[name];
            return value === undefined || value === null ? match : String(value);
        });
    }
    return text;
}

/* ------------------------------------------------------------------ 语言检测 / 切换 */

export function detectComfyLocale() {
    try {
        const comfy = globalThis.app?.ui?.settings?.getSettingValue?.("Comfy.Locale");
        if (typeof comfy === "string" && comfy) return /^zh/i.test(comfy) ? "zh" : "en";
    } catch (_error) {
        // 设置未注册时 getSettingValue 可能抛错，忽略并继续兜底
    }
    let nav = "";
    try {
        nav = String(globalThis.navigator?.language || "");
    } catch (_error) {
        nav = "";
    }
    return /^zh/i.test(nav) ? "zh" : "en";
}

function normalizeMode(mode) {
    return LOCALES.includes(mode) ? mode : "auto";
}

function resolveLocale(mode) {
    if (mode === "zh" || mode === "en") return mode;
    return detectComfyLocale();
}

/** 把 `<html lang>` 对齐到 `t()` 正在使用的那个 locale（唯一真源 = resolvedLocale）。 */
function syncDocumentLang() {
    try {
        const root = globalThis.document?.documentElement;
        if (!root) return;
        if (root.lang !== resolvedLocale) root.lang = resolvedLocale;
    } catch (_error) {
        // 无 DOM 环境（Node 自测）时忽略
    }
}

let langGuardInstalled = false;

/**
 * ComfyUI 自己的 i18n 也会写 `<html lang>`（用它的 Comfy.Locale），启动/切换时机可能与
 * 我们交错，导致「lang 是 en、文案是 zh」。这里挂一个 MutationObserver：任何人改 lang，
 * 都把它拉回 resolvedLocale。写同值不会再次触发，因此不会自我循环。
 */
function installLangGuard() {
    if (langGuardInstalled) return;
    const doc = globalThis.document;
    const Observer = globalThis.MutationObserver;
    if (!doc || !doc.documentElement || typeof Observer !== "function") return;
    langGuardInstalled = true;
    try {
        new Observer(() => syncDocumentLang()).observe(doc.documentElement, {
            attributes: true,
            attributeFilter: ["lang"],
        });
    } catch (_error) {
        langGuardInstalled = false;
    }
}

/** 打 `<html lang>`：始终等于 `t()` 用的 locale，并保证被外部改写后能自愈。 */
export function applyLocaleToDocument() {
    syncDocumentLang();
    installLangGuard();
}

/** 切换语言（auto|zh|en）：更新状态 + localStorage + <html lang>，并通知订阅者。 */
export function setLocale(mode) {
    userMode = normalizeMode(mode);
    resolvedLocale = resolveLocale(userMode);
    try {
        globalThis.localStorage?.setItem(STORAGE_KEY, userMode);
    } catch (_error) {
        // localStorage 不可用（隐私模式 / Node）时忽略
    }
    applyLocaleToDocument();
    notify();
    if (userMode === "auto") scheduleAutoRecheck();
    return resolvedLocale;
}

/** zh/en 键集合必须完全一致；不一致时 console.error 并让调用方回落 zh。 */
export function assertLocalesComplete() {
    const zhKeys = Object.keys(ZH);
    const enKeys = Object.keys(EN);
    const missingInEn = zhKeys.filter((key) => !(key in EN));
    const missingInZh = enKeys.filter((key) => !(key in ZH));
    const ok = missingInEn.length === 0 && missingInZh.length === 0;
    if (!ok) {
        console.error("[nl2prompt][i18n] zh/en 字典键不一致", { missingInEn, missingInZh });
    }
    return { ok, total: zhKeys.length, enTotal: enKeys.length, missingInEn, missingInZh };
}

export function dictStats() {
    return {
        zh: Object.keys(ZH).length,
        en: Object.keys(EN).length,
        resolved: resolvedLocale,
        mode: userMode,
    };
}

// 调试钩子：探针 / 控制台可直接切语言，与设置面板下拉走完全相同的链路
// （setLocale → notify → subscribeLocale 订阅者 → 节点栏与节点正面刷新）
if (typeof globalThis !== "undefined") {
    globalThis.__nl2promptSetLocale = (mode) => setLocale(mode);
    globalThis.__nl2promptDictStats = () => dictStats();
}

/**
 * `auto` 模式的校准：模块顶层执行时 `Comfy.Locale` 可能还没注册，
 * `detectComfyLocale()` 会回落到 navigator.language。这里在稍后两个时点重新解析一次，
 * 保证 `<html lang>` 与最终文案用的是同一个结果。
 */
function scheduleAutoRecheck() {
    if (userMode !== "auto") return;
    const recheck = () => {
        if (userMode !== "auto") return;
        if (resolveLocale("auto") === resolvedLocale) return;
        setLocale("auto");
    };
    try {
        globalThis.setTimeout(recheck, 0);
        globalThis.setTimeout(recheck, 400);
    } catch (_error) {
        // 无定时器环境（极少见）时忽略
    }
}

let initPromise = null;

/** 启动初始化（幂等）：localStorage 乐观值 → 后端 settings_ui（真源）→ 自检。 */
export function initI18n() {
    if (initPromise) return initPromise;
    initPromise = (async () => {
        let optimistic = "auto";
        try {
            const cached = globalThis.localStorage?.getItem(STORAGE_KEY);
            if (LOCALES.includes(cached)) optimistic = cached;
        } catch (_error) {
            // localStorage 不可用时用 auto
        }
        setLocale(optimistic);

        try {
            const response = await api.fetchApi("/nl2prompt/settings_ui", { method: "GET" });
            if (response && response.ok) {
                const data = await response.json();
                const mode = data && typeof data === "object" ? data.ui_locale : null;
                if (typeof mode === "string" && LOCALES.includes(mode) && mode !== optimistic) {
                    setLocale(mode);
                }
            }
        } catch (_error) {
            // 后端不可用：保持 optimistic / auto
        }

        const report = assertLocalesComplete();
        if (!report.ok) {
            // 缺键时显式回落 zh，避免半英半中的界面
            setLocale("zh");
        }
        scheduleAutoRecheck();
        return report;
    })();
    return initPromise;
}

/* ------------------------------------------------------------------ 错误码 → 文案 */

/**
 * 后端错误对象 → 本地化文本。
 * 优先 `detail_zh` / `detail_en`（后端 v3 B4 已提供），其次 `err.<code>` 字典，最后原始 code。
 * @param {{error?:string, detail?:string, data?:object}|null} response
 * @param {string} [fallback] 全部缺失时的兜底文案
 */
const CJK_RE = /[一-鿿]/;

export function describeError(response, fallback) {
    const res = response || {};
    const data = res.data && typeof res.data === "object" ? res.data : {};
    const code = String(res.error || data.error || "").trim();
    const localized = resolvedLocale === "en" ? data.detail_en : data.detail_zh;
    const usable = typeof localized === "string" && localized.trim() !== "";
    // 英文界面下后端 detail_en 可能仍是中文（后端未提供英文详情）：此时宁可显示本地化的错误码文案。
    if (usable && !(resolvedLocale === "en" && CJK_RE.test(localized))) return localized;
    if (code) {
        const key = `err.${code}`;
        const known = EN[key] !== undefined || ZH[key] !== undefined;
        if (known) return t(key);
    }
    if (usable) return localized;
    if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
    if (typeof res.detail === "string" && res.detail.trim()) return res.detail;
    if (code) return code;
    return fallback === undefined ? t("err.empty") : fallback;
}

/** 仅已知错误码时的本地化文本（无后端详情）。 */
export function errorText(code, fallback) {
    const value = String(code || "").trim();
    if (!value) return fallback === undefined ? t("err.empty") : fallback;
    const key = `err.${value}`;
    const known = EN[key] !== undefined || ZH[key] !== undefined;
    return known ? t(key) : value;
}
