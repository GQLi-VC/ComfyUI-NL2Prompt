# v3 验收对照报告（开源前基线）

- 日期：2026-11-18
- 对照基准：`实施计划.md`（2026-11-15 批准版）+ 任务书 §10 + v3 六项优化（用户批准）
- 实测数据来源：本轮全量回归（见文末）

---

## 一、原计划验收项（A1–A9 / B1–B8）

| # | 原计划要求 | 实测 | 证据 |
|---|---|---|---|
| A1 | 主节点三项区齐全：NL 输入框、角色行（3 列）、风格行（2 列）、两个添加按钮 | ✅ PASS | `tools/ui_probe.py`：11 个 widget、角色 3 列、风格 2 列、两个添加按钮实测渲染 |
| A2 | 动态行增删 + 保存重开后不丢失 | ✅ PASS | `ui_probe.py`（char_rows 1→3、style_rows 0→2、节点高 500→590）+ `ui_probe4`（重开 select 值保持） |
| A3 | 文字指定提示词：NL 含「词语」→ 输出含对应 Prompt | ✅ PASS | `test_composer.py` 7/7（含反向用例与 `target=negative`） |
| A4 | **设置窗口**可开可关；系统提示词可改，恢复默认需二次确认 | ✅ PASS | `ui_probe2.py`：5→7 Tab、Esc/遮罩/✕ 三种关闭全 true、「恢复默认」confirm 两次（取消保留、确认恢复） |
| A5 | TopK/TopP/种子（随机/指定）在 LLM 窗口；每次执行种子写回并显示 | ✅ PASS | `test_composer.py` 3/3 + `ui_probe3.py`（`上轮种子: -` → `777888999`，回退 → `42`） |
| A6 | 文本输出端格式 `Positive:\n...\nNegative:\n...` | ✅ PASS | `test_composer.py` 4/4（纯函数 + 节点 `text` 双路径） |
| A7 | 加载LLM：本地/云端切换、GPU 名、路径扫描（含子目录）、api_key 不明文 | ✅ PASS | `ui_probe.py`（双卡枚举、显隐切换）+ `check_v32_scan`（9 场景扫描）+ `secret_status` 只回 configured/source |
| A8 | api_key 不出现在保存的 workflow JSON 中 | ✅ PASS | `run_acceptance.py` A8 7/7（holder 无 api_key 字段、密码框 `serialize:false`、落盘扫描 0 命中） |
| A9 | **测试环境独立**：原 `<你的ComfyUI根目录>` 零改动 | ✅ PASS | 全目录最近 mtime 停在用户自己使用时的 22:41，此后本项目所有操作零写入 |
| B1 | 基础出图 | ✅ PASS | `run_acceptance --only mvp`：1024×1024、success |
| B2 | 角色 LoRA 注入 | ✅ PASS | 纯函数 4/4 + `--only lora` 端到端 10/10（日志 `已加载 LoRA「遐蝶(ANI).safetensors」`） |
| B3 | 多服装隔离（双向 + 零交叉） | ✅ PASS | 原皮 5/5、假日 5/5、镜像 4/4、lora 2/2；两方向交叉 token 均为 ∅ |
| B4 | 风格注入 | ✅ PASS | 3/3（`@nixeu` + `soft light` + `debug.styles`） |
| B5 | LLM 可关闭 | ✅ PASS | 5/5（`used_llm=False`、`mode=template`、`fallback=""`） |
| B6 | LLM 失败回退 | ✅ PASS | `run_acceptance` B6 9/9 + 补充 6/6（`llm_not_connected` / `llm_key_missing` / `llm_http_0` 均回退不中断） |
| B7 | 双卡不 OOM | ✅ PASS | MVP 与 WithLoRA 各 6/6；GPU1 恒 0 MiB（未接 LLM 时）、无 OOM |
| B8 | 输出兼容（STRING → CLIPTextEncode） | ✅ PASS | 5/5 底模校验 + `render_conditioning` 直连采样器出图 |

**A1–A9 / B1–B8：17 项全部 PASS。**

---

## 二、原计划的结构性要求

| 要求 | 状态 | 说明 |
|---|---|---|
| 「不使用节点内折叠区，两个节点各带 ⚙ 设置按钮，弹独立窗口」 | ✅ 达成 | `document.body` + `position:fixed` 遮罩 + 居中面板；Esc / 遮罩 / ✕ 三种关闭 |
| 窗口开关状态不写入 workflow | ✅ 达成 | 只存 DOM 状态，`_ui_json`/`_llm_json` 不含开窗标志 |
| 数据仍存 hidden holder JSON | ✅ 达成 | `_ui_json` / `_llm_json` 是唯一序列化真源，窗口只是编辑器 |
| 设置项按匹配度重新分配 | ⚠️ **部分偏离（用户后续主动修正）** | 见下节「偏离项」 |
| TopK 云端模式灰化并提示 | ✅ 达成 | LLM 窗口「采样参数」内 top_k 在云端置灰 + hint |
| 完全隔离的测试环境 | ✅ 达成 | `test_env/`（端口 8199），模型经 `extra_model_paths.yaml` 只读引用 |
| 产出示例工作流 | ✅ 达成 | `workflows/NL2Prompt_MVP*.json` + `NL2Prompt_WithLoRA*.json`（各含 API 格式与 workflow 格式） |

---

## 三、v3 六项优化（用户批准的第二批需求）

| # | 要求 | 状态 | 证据 |
|---|---|---|---|
| R1 | 云端供应商注册表 ≥28 家，单一真源 + 探活校准 | ✅ **34 家** | `providers.py` 唯一真源；探活报告 23 家直连有效；前端下拉 34 项 |
| R2 | 默认值审计（该留空的留空） | ✅ 达成，且**比原计划更彻底** | `natural_language` / `cloud_model` / `base_url` / `system_prompt` / `negative_base` / `positive_base` / **`model_path` / `model_dir` / `model_file`** 全部默认空 |
| R3 | Anima 提示词 skill（规范 + 校验器 + 自动注入） | ✅ 达成 | `anima-tag-master.json` 4525 字符 / 11 节 / 9 条自检；`skill_check.py` 10 个错误码；失败自动重试 1 次 |
| R4 | GPT 相似体验（多轮历史 / 一键重写 / 质量评分） | ✅ 达成 | 历史 20 轮、重写按钮、`quality.score`、沿用上一轮、`analysis` 说明 |
| R5 | base_url 自动拉取模型列表 | ✅ 达成 | `/list_models` + 「云端模型」Tab 内拉取 + 模型列表点击写入 |
| R6 | 通用化仅写架构预留 | ✅ 达成（未实现） | `ui.target_profile` 架构位 |

---

## 四、后续追加需求（v3 目标 + 三轮反馈）

| 批次 | 要求 | 状态 |
|---|---|---|
| v3 目标 | 改名 NL2Prompt + 旧工作流兼容 | ✅ 旧类名仍注册、旧路由前缀保留别名、旧图可出图（D1 9/9） |
| v3 目标 | LLM 节点拆分为 GGUF / safetensors / 云端 | ✅ 三个独立节点 + 旧合并版保留 |
| v3 目标 | Composer 输出 CONDITIONING 与 MODEL 直通 | ✅ `positive_cond`/`negative_cond`/`model`，删掉 CLIPTextEncode 实测出图 |
| v3 目标 | 正面基础词 + skill 增删管理 | ✅ 新 Tab + 5 按钮 + 4 条路由 |
| v3 目标 | 本地模型卸载开关 | ✅ `unload_after_use`，成功/失败都卸，`debug.unloaded` 以「是否仍驻留」为准 |
| v3 目标 | 中英双语 | ✅ zh/en 各 320 键，四态 lang 与文案一致 |
| v3.1 反馈 | 设置按钮消失 / 旧节点 UI 被破坏 | ✅ 根因：前端按单类名匹配；已改数组匹配 + 三个拆分节点补 UI |
| v3.1 反馈 | 左侧节点栏名称跟随语言 | ✅ 改 `backendDisplayName` + `registered_node_types[].title`，即时生效 |
| v3.2 反馈 | api_key 挪到「云端模型」；本地/云端设置分开 | ✅ Tab 按变体裁剪（legacy/cloud 3 Tab，gguf/safetensors 2 Tab） |
| v3.2 反馈 | 本地 LLM 的 model_path / model 置空 | ✅ 三节点默认空 + 运行时回退自动探测 |
| v3.2 反馈 | safetensors 刷新显示 gguf 文案 | ✅ 文案参数化 + **后端新增 `/scan_safetensors`**（原来调的是扫 gguf 的路由，属真缺口） |
| v3.3 反馈 | legacy 合并版同时支持 GGUF 和 safetensors | ✅ `model_type` 三选项（首项保留旧值）+ `model_dir`/`dtype` 追加在 holder 之后 + 整块委托拆分节点 |

---

## 五、偏离原计划的地方（全部为用户主动要求，非实现偏差）

| 原计划 | 现实现 | 原因 |
|---|---|---|
| 「模型类型 / GPU / 路径 / 扫描 / 供应商 / api_key / 模型代号」放在**节点正面**（因需频繁切换） | 扫描与 api_key 移入**设置窗口**，节点正面保留只读小字 | 用户在 v3 阶段明确要求「拉取模型集成到设置中」、v3.2 要求「api_key 挪到云端模型」 |
| `model_path` 默认预填自动探测结果 | 默认空串，运行时回退探测 | 用户要求「置空」（该留空的留空） |
| 单一 `加载LLM` 节点 | 拆三个 + 保留合并版 | 用户要求拆分；同时在 v3.3 要求合并版保留双格式能力 |
| 插件名 / 节点名带 Anima | NL2Prompt / 自然语言提示词 | 用户要求去掉 Anima 专有词 |

> 这些偏离都是**需求演进**，不是实现与计划不符。每一处都有明确的用户指令对应。

---

## 六、保留的 "anima"（不是遗漏）

`base_model`/`target_model: "anima"`、skill id `anima-tag-master`、`check_anima()`、`"checker":"anima_v1"`、widget 名 `anima_nl_*`/`anima_llm`、CSS 类 `anima-nl-*`、环境变量 `ANIMA_LLM_*`、真实模型文件名。

判定工具 `tools/check_rename.py`：品牌残留 **0 处**。

---

## 七、本轮实测数据（开源前基线）

```
语法（全部 py + 4 个 js）           FAIL=0
v3_dom_stub.mjs                     186/186  失败 0
check_llm_ui                        7/8 有设置按钮（LoRA 节点本无 UI）
check_v32_scan                      9 场景全对
probe_v3_ui                         wrongGuard=false，7 Tab 全 ok
probe_v3_i18n                       langMatch=true（四态一致）
check_node_titles                   9 类名中英对应
test_composer.py                    63/63   ALL PASS
test_v2.py                          96/96   ALL PASS
test_v3.py                          85/85   ALL PASS（2 条待显存不足环境）
run_acceptance --only mvp           44/44   ALL PASS
run_acceptance --only lora          10/10   ALL PASS
backend_selfcheck（6 脚本）          189/189 PASS
render_conditioning（CONDITIONING）  success，1024×1024
check_rename                        品牌残留 0 处
plugin/data/secrets.json            干净
GPU0 766 MiB / GPU1 0 MiB
```

---

## 八、结论

**与原计划一致，且超出原计划范围。**

- 原计划的 17 项验收（A1–A9 / B1–B8）**全部 PASS**
- 原计划的结构性要求（窗口化、holder 数据真源、隔离环境、示例工作流）**全部达成**
- 4 处偏离均为用户主动提出的需求变更，已全部实现
- v3 六项优化 + 三轮反馈（v3.1/v3.2/v3.3）共 12 项追加需求**全部完成**

**可以进入开源准备阶段**（本轮不做，下一轮执行）。

---

## 九、开源前待办清单（下一轮）

### 必须做

1. **清理开发痕迹**
   - `plugin/data/settings.json` 里的本机路径（`<你的模型目录>\llama.cpp\...`、`<你的模型目录>\Models`）→ 改为空或跨平台默认
   - `plugin/data/characters.json` / `styles.json` 里的本机 LoRA 文件名 → 考虑是否作为示例保留（含个人偏好，建议保留但标注为示例）
   - `tests/_*.log` / `_*.json` 证据文件 → 移出仓库或加 `.gitignore`
   - `log/`（26 篇开发日志）→ 保留（体现开发过程）或移到 `docs/devlog/`
2. **补全开源必需文件**
   - `LICENSE`（需你决定协议，常见 MIT / Apache-2.0）
   - `CONTRIBUTING.md`（可选）
   - `CHANGELOG.md`（把 v1→v3 整理成发布说明）
   - `requirements.txt`（`transformers`/`accelerate` 是可选依赖，要标注清楚）
3. **`README.md` 重构**
   - 现在 365 行偏"开发记录"风格，开源版应重写为「这是什么 → 截图 → 安装 → 快速开始 → 配置 → FAQ」
   - 明确 **safetensors 不适合 12GB 卡跑 6B** 这个硬限制
   - 加截图（`log/v3_ui/` 有 19 张可用）
4. **`.gitignore` 复核**
   - 现排除 `plugin/data/secrets.json` / `logs/` / `test_env/` / `__pycache__`
   - 需补：`plugin/data/settings.json`（含本机路径）、`tests/_*`、`*.gguf`、`*.safetensors`
5. **敏感信息最终扫描**
   - 全仓搜 `sk-`、`<模型目录前缀>`、`<用户目录前缀>`、IP、内网路径
6. **降低首次使用门槛**
   - `plugin/data/characters.json` 目前是特定角色（星穹铁道等），开源应考虑提供空模板 + 示例两套，或明确说明「这是示例数据，请替换」

### 建议做

7. **safetensors 节点标注清晰**：README 明确「12GB 显卡跑 6B 会 OOM，建议用 GGUF」
8. **`providers.py` 的 34 家供应商**：确认无内网地址、无个人 token
9. **节点文档**：给每个节点写一段「输入输出 + 典型接法」
10. **i18n 覆盖度**：确认 en 界面的 provider label（「深度求索」等）是否可接受（当前保留中文）

### 已知限制（开源前应写进 README 的「Known limitations」）

- safetensors 加载 6B 在 12GB 卡上必 OOM（`local_model_oom`）
- ComfyUI 重建 nodeDefs 时节点栏显示名可能暂回中文（下次切语言/刷新自愈）
- `(robin (honkai: star rail):9.9)` 这类**嵌套权重**越界不会报错（单层正则所致）
- 云端 provider label 在英文界面仍显示中文（数据层单一真源）
- safetensors OOM 分支未在真实显存不足环境验证过
