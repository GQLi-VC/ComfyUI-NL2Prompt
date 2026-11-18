# v3 改进对照表（用户 9 项需求 → 实现 → 验收）

> 状态：实现进行中，本表随完成度更新。最后更新 2026-11-18 00:30。

| # | 用户原话 | 实现 | 状态 |
|---|---|---|---|
| 1 | 加载LLM拆分为加载LLM（safetensors）、加载LLM（GGUF）、加载云端LLM | 新增 `NL2PromptLLMGGUF` / `NL2PromptLLMSafetensors` / `NL2PromptLLMCloud` 三个独立节点（`plugin/nodes_llm.py` 892 行），旧的合一版保留为「加载LLM（旧·本地/云端合一）」 | ✅ 已注册 6 节点 |
| 2 | 将节点"Anima自然语言提示词"的名称更改，去掉"Anima"这样的专门指代的词 | 显示名改为 **自然语言提示词**，类名 `NL2PromptComposer`，CATEGORY `NL2Prompt`；全仓 35 文件 370 处替换，品牌残留校验 **0 处** | ✅ |
| 3 | positive 和 negative 应为可直接连接采样器的，而不是仅输出文本 | `RETURN_TYPES` 尾部追加 `CONDITIONING, CONDITIONING, MODEL`（索引 0–4 不变）；`clip` 输入已连接时用 `clip.tokenize` + `encode_from_tokens_scheduled` 生成条件；未连接输出空条件不报错 | ✅ 后端完成，待端到端出图验收 |
| 4 | 加载LLM的拉取模型无法正常生效，把它集成到设置中，可通过设置更改 cloud_model | ① 定位并修复真 bug：`base_url` 默认空串时 `fetchCloudModels()` 直接 return，没回退供应商注册表；② 拉取整块从节点正面移入 LLM 设置窗口的「云端模型」Tab，点列表即写 `cloud_model` | ✅ 前端完成，真实环境实测通过 |
| 5 | 将插件名字从"Anima NL"改成其他的名字 | → **NL2Prompt**。旧类名仍注册（旧工作流能开）、旧路由前缀 `/anima_nl/*` 保留为别名 | ✅ 旧类名出图实测通过 |
| 6 | 额外在提示词设置中增加正面基础词 | 设置窗口新增「正面基础词」Tab（与负面完全对称）；holder 键 `positive_base`，拼在 positive 最前面；来源优先级 ui → settings → 空 | ✅ 后端 41/41、前端 Tab 已渲染 |
| 7 | 提示词技能那里需要添加一个或几个类似于"添加skill"、"管理skill"的按键 | 技能 Tab 从 2 按钮变 5 按钮：恢复默认 / 保存到服务器 / **＋ 新建 skill** / **删除 skill** / **另存为…**；配套 4 条新路由（`skills/{id}` GET、`skills/new`、`skills/delete`、`settings_ui`） | ✅ 后端 49/49、前端完成 |
| 8 | 加入"重写完毕后自动从GPU卸载本地模型"的开关 | `unload_after_use` 放在两个本地 LLM 节点（GGUF / safetensors）；Composer 在 `compose_with_llm` 的 `finally` 里执行，**成功和失败都卸载**；写 `debug.unloaded` | ✅ 后端 24/24 |
| 9 | 增加中英双语适配，根据 ComfyUI 语言自动切换，且可手动调整 | 新增 `plugin/web/i18n.js`；检测链 `ui_locale` → `Comfy.Locale`（已确认该键存在于前端 1.58.5）→ `navigator.language`；设置窗口「通用」Tab 可切「跟随 ComfyUI / 中文 / English」，即时生效 | 🔄 进行中 |

## 配套的兼容与质量措施

| 措施 | 说明 |
|---|---|
| 旧工作流兼容 | 旧类名注册到同一类对象 + 路由双前缀别名 + 输出索引只追加不插入 |
| GBK 红线 | Python 端新增文案逐行扫描，0 行不可编码 |
| 品牌残留校验 | `tools/check_rename.py`，品牌残留必须为 0（语义正确的 anima 保留在白名单） |
| 迁移可复现 | `tools/migrate_to_nl2prompt.py`（幂等，支持 dry-run / `--show`） |
| UI 可复现 | `tools/probe_v3_ui.py`（7 Tab + 3 Tab 截图）、`tools/v3_dom_stub.mjs`（129 项 DOM 断言） |

## safetensors 本地 LLM 的真实限制（必须让用户知道）

实测数据：
- GPU1（RTX 2060，12 GB）实际可用 **10.95 GiB**
- 6B 模型 fp16 权重 11.18 GiB，加 KV/激活冗余 **≈13.97 GiB → 必 OOM**
- 同一台机器跑 GGUF 量化（Qwen3.5-9B-Q4_K_M + ctx8192）只占 **5.47 GiB**

**结论：12 GB 显卡上跑 6B safetensors 不现实，请用 GGUF。** 节点会在 OOM 时返回 `local_model_oom` 并直接提示"建议改用 GGUF"。

另外两个已修的平台坑：
1. **RTX 2060 是 Turing（SM 7.5），不支持 bf16 推理** —— 加载能成功但 `generate` 会 `cutlassF: no kernel found`；已自动降 fp16
2. **HF 缓存是符号链接**（`snapshots/*/model.safetensors` → `blobs/*`）—— 不跟随符号链接会误判"目录里没有权重"；已修

## 保留的 "anima"（不是改名遗漏）

| 保留项 | 原因 |
|---|---|
| `base_model` / `target_model` = `"anima"` | 指 Anima 模型 |
| skill id `anima-tag-master`、显示名「Anima 标签大师」 | skill 是给 Anima 写的 |
| `check_anima()`、`"checker": "anima_v1"` | 校验 Anima 提示词的函数与版本 |
| widget 名 `anima_nl_*`、`anima_llm` | 序列化内部标识，改动风险大于收益 |
| CSS 类 `anima-nl-*` | 纯内部样式 |
| 环境变量 `ANIMA_LLM_*` | 用户可能已配置 |
| 真实文件名 `anima-preview3-base.safetensors` 等 | 磁盘上的文件名 |
