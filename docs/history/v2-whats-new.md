# v2 变更说明（六项优化）

> 版本：v2（2026-11-17）｜基础：v1（A1–A9 / B1–B8 已全绿）
> 契约：`docs/接口契约.md` §14｜验收：`测试记录.md` §11

## 一句话总结

v1 能"用自然语言出图并正确注入角色/服装/风格 LoRA"；v2 让它**接得上 34 家云端供应商、有专门的 Anima 提示词规范把关、具备 GPT 式的多轮改写体验**，并且把该留空的输入框都清空了。

---

## R1 云端供应商写全（34 家）

**之前**：只有 `deepseek / openai / anthropic / custom` 4 项，且这张表在 3 个文件里各写一份。
**现在**：34 家，**单一真源**在 `plugin/providers.py`：

| 分组 | 数量 | 清单 |
|---|---|---|
| 国内 | 15 | DeepSeek、通义千问(DashScope)、智谱 GLM、Moonshot Kimi、字节豆包(火山方舟)、硅基流动、MiniMax、百度文心(千帆)、腾讯混元、讯飞星火、阶跃星辰、零一万物、百川、商汤日日新、魔搭 |
| 国际 | 15 | OpenAI、Anthropic Claude、Google Gemini、xAI Grok、Mistral、Cohere、Groq、Together、Fireworks、OpenRouter、DeepInfra、Perplexity、Cerebras、GitHub Models、Azure OpenAI |
| 本地 | 3 | Ollama `11434`、LM Studio `1234`、vLLM `8000` |
| 兜底 | 1 | custom（自己填 base_url） |

- 前端不再硬编码，通过 `GET /nl2prompt/providers` 拉取；节点正面显示「当前：<中文名> · <base_url>」
- **地址校准**：`tools/verify_providers.py` 逐家探活，报告在 [providers-verify.md](providers-verify.md)。判定口径严格区分「401/403 = 地址有效」「404 = 路径错」「DNS/超时 = 网络不可达（不代表地址错）」
- 实测：直连 23 家地址有效；8 家本机网络不可达；`lingyiwanwu` 的 `/models` 被官方下线（410）但 `/chat/completions` 路径存在

## R2 该留空的留空

| 字段 | v1 | v2 |
|---|---|---|
| 自然语言输入框 | 空 | 空（不变） |
| 云端模型代号 | 空 | 空 |
| 系统提示词 | 空 | 空（默认文案由 skill 提供） |
| 负面基础词 | 预置长串 | **空**（留空=用推荐基线，可点「填充推荐值」固化） |
| 模型路径[本地] | 硬编码 `<你的模型目录>\Models` | **空 + 自动探测**（要求候选目录真的含 >100MB 的 .gguf，跳过空占位目录） |
| base_url | 预置 deepseek 地址 | **空**（随 provider 自动填） |

## R3 Anima 专属提示词技能

> 注意：这里不是模型微调，是**提示词工程规范 + 输出校验器**。

`plugin/skills/anima-tag-master.json`（4338 字符 system_prompt，11 节）：

1. 输出格式硬约束（严格 JSON，`analysis` 只作说明不混入提示词）
2. 标签顺序：`quality → subject → character → outfit → pose → scene → camera → lighting → style`
3. 全英文 danbooru 标签，禁中文/markdown/解释文字
4. **触发词逐字保护**（角色/服装/风格词一字不改，只可移位）
5. **服装隔离**（只注入当前服装，禁同角色其它服装词）
6. 具体化（禁 `beautiful`/`amazing`/`8k`/`octane render` 这类空泛或对动漫模型无效的词）
7. 权重语法 `(tag:1.2)`，0.5–1.5，最多 5 个
8. 负面词三段式（质量缺陷 + 冲突抑制 + 文字水印）
9. 补全而非反问
10. Anima 实用细节（景别/机位/光照标准词、画师 `@artist` 放末尾）
11. 输出前 8 条自检

**含互斥组合黑名单**（这是最实用的一条）：`close-up` 与 `full body`、`solo` 与 `2girls`、`day` 与 `night`、`from above` 与 `from below`、两种发色并存 —— LLM 极易顺手写矛盾标签，实测这是出图变糊的主因之一。

**每次 LLM 调用自动注入**；输出经 `skill_check.check_anima()` 校验（10 类问题码），不合格**带问题清单重试 1 次**，仍不合格回退模板并记 `debug.skill_check`。

**实测效果**（Qwen3.5-9B，3 次）：从"中文混杂的模板串"变成"合规英文标签 + 场景具体化"，`quality.score` 91/94/94，中文全清零。

## R4 GPT 式体验

| 功能 | 位置 | 说明 |
|---|---|---|
| 多轮历史 | 设置窗口「对话历史」+ 正面小字 | 最多 20 轮，随 workflow 保存，可「复用该轮提示词」 |
| 一键重写 | 正面「重写提示词」 | 只调 LLM 改写、**不出图**；走 `POST /nl2prompt/rewrite` |
| 内容质量评分 | 正面小字 `质量 N` | 基于 skill 规则本地打分（槽位覆盖/标签数/违规/具体度） |
| 中文说明 | `debug.analysis` | LLM 说明这轮改了什么（≤60 字） |
| 沿用上一轮 | 正面「沿用上一轮」 | 把上轮 positive 填入当前草稿 |

**未做**（预留接口）：图生提示词（VLM 反推）、批量变体、真正的聊天窗口。

## R5 base_url 自动拉模型

云端模式点「拉取模型」→ `GET /nl2prompt/list_models?base_url=&provider=` → 向 `{base_url}/models` 请求（带 Bearer key）→ 结果填进**可编辑下拉**。
- 选中即写 `cloud_model`，**也可以直接手打**（两者不冲突）
- 结果缓存 10 分钟（存 workflow）
- 失败显示 `http_401` / `timeout` / `dns` / `no_key` / `bad_url`

## R6 通用化（只做了架构预留）

**本期不实现**。已预留：
- holder 的 `ui.target_profile`（默认 `"anima"`）
- skill 文件的 `target_model` 字段
- 二期方案（写在契约 §14.6）：`model_profiles.py` 声明每个模型的标签体系/质量词/负面词/权重语法/CLIP 编码方式，覆盖 SDXL / SD1.5 / Flux / MOSS-Image / Qwen-Image-2.1

---

## 新增 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/nl2prompt/providers` | 34 家供应商表 |
| GET | `/nl2prompt/list_models?base_url=&provider=` | 拉上游模型列表 |
| GET | `/nl2prompt/skills` | skill 列表 + 全文 |
| POST | `/nl2prompt/skills/save` | 保存编辑后的 skill（带校验与回滚） |
| POST | `/nl2prompt/rewrite` | 只改写提示词、不出图 |

路由总数：9 → **14**。

## 数据结构变化（向后兼容，旧 workflow 能直接打开）

主节点 `_ui_json` 新增：
```json
{"ui": {"skill_id":"anima-tag-master","target_profile":"anima","collapsed_main":false},
 "dialog": [],
 "cloud_models": {"base_url":"","models":[],"ts":0}}
```
`ui.anima_nl[0]` 新增 `dialog_entry` 与 `dialog_len`。
`debug` 新增 `skill_check` / `quality` / `analysis` / `negative_missing`。
LLM 节点 `_llm_json` 新增 `cloud_models`。

## 已知限制

1. `providers.verified` 是**静态快照**，平台改地址不会自动感知，建议定期重跑 `tools/verify_providers.py`
2. 本机 7890 代理**当前未监听**；海外供应商在国内网络下多需代理，`verified=false` 只代表"本机连不上"
3. 云端不支持 `top_k`（窗口内已灰化）
4. LLM 模式的 negative 抑制词由模型自由发挥，实测覆盖率不稳（7/11、0/11、11/11），已用「强制并入模板抑制词」兜底
5. 供应商改地址/下线：`lingyiwanwu` 的 `/models` 端点已下线（410），但对话端点可用
