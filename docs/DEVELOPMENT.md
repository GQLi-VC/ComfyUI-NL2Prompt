# 自然语言提示词（NL2Prompt · ComfyUI 插件）

把「自然语言 → 结构化 LoRA 注入 → 出图」做成一个节点：输入一句人话，选择角色/服装/画师 LoRA，输出**可直接接 K 采样器**的 `positive_cond` / `negative_cond`，也可以只取文本再自己接 `CLIPTextEncode`。

- 目标模型：**Anima**（`anima-preview3-base.safetensors` + `Qwen3-06B-Base.safetensors` + `qwen_image_vae.safetensors`）
- 实测环境：ComfyUI **0.45.3** / 前端 1.58.5 / Python 3.13.11 / torch 2.11.0+cu130
- 实测显卡：RTX 3060 12G（出图）+ RTX 2060 12G（本地 LLM）

> 本项目所有开发验证都在**隔离测试环境** `test_env\` 完成，用户原有 ComfyUI 环境全程零改动。

**v3 已上线**（v1/v2 能力全部保留，旧工作流仍可加载）：

- **LLM 节点拆成三个**：`加载LLM（GGUF）` / `加载LLM（safetensors）` / `加载云端LLM`，各管一种来源（旧的合一版仍保留为「加载LLM（旧·本地/云端合一）」）
- **提示词直连采样器**：新增 `positive_cond` / `negative_cond`（CONDITIONING）+ `model` 直通，**不用再绕 `CLIPTextEncode`**
- **正面基础词**：与负面基础词对称，拼在最前面
- **skill 管理**：新建 / 删除 / 另存为，不再只能改不能加
- **拉取模型搬进设置**：LLM 设置窗口新增「云端模型」Tab，点列表即写入 `cloud_model`；并修复了「选了供应商仍说请先填 base_url」的老 bug
- **自动卸载本地模型**：`unload_after_use` 开关，重写完成后释放显存
- **中英双语**：跟随 ComfyUI 语言自动切换，也可手动固定
- **插件改名**：Anima NL → **NL2Prompt**（旧类名仍注册，旧图能直接打开）

**v2 能力**：34 家云端供应商（单一真源 + 探活校准）、Anima 专属提示词技能（4525 字符规范 + 校验器 + 自动重试）、GPT 式体验（多轮历史 20 轮 / 一键重写 / 质量评分 / 沿用上一轮）、云端自动拉模型、该留空的留空、通用化架构预留。

---

## 1. 六个节点

| 节点 | 类名 | 作用 |
|---|---|---|
| 自然语言提示词 | `NL2PromptComposer` | 主节点：组装提示词 + 可选 LLM 重写 + 输出 CONDITIONING |
| 加载LLM（GGUF） | `NL2PromptLLMGGUF` | 本地 GGUF 模型（llama-server），推荐 |
| 加载LLM（safetensors） | `NL2PromptLLMSafetensors` | 本地 HF 格式模型（transformers），**需较大显存** |
| 加载云端LLM | `NL2PromptLLMCloud` | 云端 OpenAI 兼容接口 |
| 加载LLM（旧·本地/云端合一） | `NL2PromptLoadLLM` | v2 合并版，保留用于兼容旧工作流 |
| LoRA 堆栈加载 | `NL2PromptLoRAStack` | 把 `lora_stack` 应用成 MODEL + CLIP |

### 1.1 `自然语言提示词`（`NL2PromptComposer`）

| 输出 | 类型 | 说明 |
|---|---|---|
| `positive` | STRING | 最终正向提示词 |
| `negative` | STRING | 最终负向提示词 |
| `text` | STRING | 文本输出端：`Positive:\n{pos}\n\nNegative:\n{neg}`，可选接线 |
| `lora_stack` | LORA_STACK | `[(name, model_strength, clip_strength), ...]`，接 `LoRA 堆栈加载` |
| `debug` | STRING | JSON：命中角色/服装/风格、注入词、`used_llm`、`fallback`、`seed`、`negative_suppress_source` |
| `positive_cond` | CONDITIONING | **v3 新增**：正向条件，直连 K 采样器 `positive` |
| `negative_cond` | CONDITIONING | **v3 新增**：负向条件，直连 K 采样器 `negative` |
| `model` | MODEL | **v3 新增**：MODEL 直通（可选，仅用于整理走线） |

**两种接法**

```
① 直连采样器（推荐，v3 新增）
【加载CLIP】/【加载LoRA】/【Checkpoint加载器】输出 CLIP
        ↓ 输入 CLIP
【自然语言提示词】输出 POSITIVE_COND / NEGATIVE_COND
        ↓
【K采样器】positive / negative

② 只要文本（兼容 v1/v2）
【自然语言提示词】输出 positive / negative → 【CLIPTextEncode】→ K采样器
【自然语言提示词】输出 text → 【预览文本】/【保存文本】
```

**MODEL 直通的可选链**（只是让画布走线好看，不是必需）：
```
【Checkpoint加载器】/【加载UNet】→ MODEL → 【加载LoRA】→ … → 【自然语言提示词】(model) → 【K采样器】
```

> `positive_cond` / `negative_cond` 需要 **CLIP 输入已连接**；没连接时输出空条件（不会报错，但接采样器会得到空提示词）。`model` 不连就输出 `None`，不接即可。

**正面 UI**
1. 自然语言输入框（多行）
2. `⚪ 自然语言处理` 折叠区
3. 角色行（3 列）：`自然语言-角色名字` ｜ `lora激活词` ｜ `英文标签（可留空）`，每行可删
   - 第三列填**纯英文装扮标签**（如 `red dress, bare shoulders`），会紧跟角色 LoRA 触发词注入正向提示词
   - 也可填预设服装名（如 `原皮`、`假日皮肤`），此时由数据层该套服装接管：注入它的 activation_words / prompt / LoRA，并把同角色其它服装的词压进 negative（预设名本身不会进入提示词）
4. 风格行（2 列）：`自然语言-画师/风格` ｜ `lora激活词`，每行可删
5. 【添加指定的角色lora激活词】【添加指定的画师画风/风格激活词】
6. 「重写提示词」「沿用上一轮」→ **只调 LLM 改写、不出图**；状态小字显示 `质量 N · 上轮重写: <analysis>`
7. `⚙ 设置` → 独立设置窗口
8. 执行后显示「上轮种子: N · 历史 M 轮」

**`⚙ 设置` 窗口**（v2：5 个标签页，独立弹窗，不占节点版面）
- **提示词替换**：`|自然语言|→|转换为的Prompt|` 规则行，可指定进 positive 还是 negative。自然语言包含左侧词语时，右侧 Prompt 会被注入
- **系统提示词**：临时覆盖用的系统提示词（非空时用户文本在前、skill 条款在后）；「恢复默认」有二次确认
- **负面基础词**：`negative_base`，留空 = 用推荐基线；「填充推荐值」可把基线固化进 workflow
- **提示词技能**：选择 skill（当前只有 `Anima 标签大师`）、查看/编辑它的 system_prompt、「恢复默认」、「保存到服务器」
- **对话历史**：最多 20 轮记录（时间 / 自然语言摘要 / positive 摘要），可「复用该轮提示词」或「清空历史」

### 1.2 `加载LLM`（`NL2PromptLoadLLM`）

| 输入 | 说明 |
|---|---|
| 模型类型 | 本地模型 / 云端模型（切换后自动显隐下方参数） |
| GPU | **物理显卡精确名字**（`0: ... RTX 3060 (12288 MiB)` / `1: ... RTX 2060 (12288 MiB)`），默认选 2060 |
| 模型路径[本地] | gguf 根目录，默认 `你的模型根目录` |
| 选择模型[本地] | 按路径递归扫描（含子目录）出的 gguf 列表，点「刷新模型列表」重新扫描 |
| 供应商选择[云端] | **34 家**（国内 15 / 国际 15 / 本地 3 / custom），详见 §4.2 |
| 拉取模型[云端] | 按钮 → 向 `{base_url}/models` 取列表，填进可编辑下拉；选中写 `cloud_model`，也可手打 |
| 供应商 base_url | 选供应商自动填，custom 留空手填 |
| 供应商 api | **不写入 workflow**：密码框 → `POST /nl2prompt/secret` 存本机；优先读环境变量 |
| 供应商模型选择 | 手填模型代号 |

输出 `llm`（自定义类型 `LLM_MODEL`），接主节点的 `llm` 输入。

**`⚙ 设置` 窗口**（2 个标签页）
- **采样参数**：`top_k` / `top_p` / `temperature` / `max_tokens` / `seed`（勾选「每次随机」= -1）/ `last_seed` 只读显示。云端模式下 `top_k` 灰化（OpenAI 兼容接口通常不支持）
- **运行设置**：`ctx` / `timeout` / api_key 密码框 + 「保存密钥」+ 密钥状态 + 「测试连接」

### 1.3 `LoRA 堆栈加载`（`NL2PromptLoRAStack`）

`MODEL + CLIP + lora_stack → MODEL + CLIP`，按顺序逐个加载，单个失败跳过并告警。`strength_scale` 可整体缩放强度。

---

## 2. 快速开始

### 2.1 在隔离测试环境里试用（推荐先这样）

```powershell
cd 你的项目路径
.\test_env\start_test.ps1            # 前台，Ctrl+C 停止
.\test_env\start_test.ps1 -Background # 后台
.\test_env\stop_test.ps1             # 停止
```
打开 <http://127.0.0.1:8199>，双击画布搜索 `自然语言提示词`。

> **必须用 `start_test.ps1` 启动**：脚本强制 UTF-8 模式（见 §5.1）。裸 `python main.py` 会退回 GBK 并踩 P0 编码坑。

### 2.2 部署到你自己的 ComfyUI

插件源码在 `plugin\`，以目录联接的方式挂进 ComfyUI：

```powershell
# 用管理员 PowerShell（符号联接需要）
New-Item -ItemType Junction `
  -Path '<ComfyUI>\custom_nodes\nl2prompt' `
  -Target '你的项目路径\plugin'
```
重启 ComfyUI 即可。插件**零 pip 依赖**（只用标准库 + ComfyUI 自带的 torch），不需要装任何包。

示例工作流在 `workflows\`：
- `NL2Prompt_MVP.json` / `NL2Prompt_MVP.workflow.json`（API 格式 / UI 格式）
- `NL2Prompt_WithLoRA.json` / `.workflow.json`

把 `.workflow.json` 拖进 ComfyUI 画布即可打开。

---

## 3. 数据配置

编辑 `plugin\data\` 下的 JSON（改完重启 ComfyUI，或调 `store.reload()`）：

### `characters.json`
```json
{
  "id": "castorice",
  "display_name": "遐蝶",
  "aliases": ["遐蝶", "Castorice", "卡斯特里斯"],
  "lora_name": "遐蝶(ANI).safetensors",
  "lora_strength": 0.8,
  "base_model": "anima",
  "activation_words": ["castorice", "purple hair", "twintails", "purple eyes"],
  "outfits": [
    { "id": "default", "display_name": "原皮",
      "activation_words": ["strapless dress", "detached sleeves", "white thighhighs"],
      "prompt": "紫色露肩礼裙，分离袖，白色大腿袜",
      "negative_suppress": ["purple dress", "clothing cutout"],
      "lora_name": null, "lora_strength": 0.0 },
    { "id": "dreamy", "display_name": "假日皮肤",
      "activation_words": ["layered dress", "white skirt", "bare legs"],
      "prompt": "层叠白裙，裸腿",
      "negative_suppress": ["layered dress", "white skirt"],
      "lora_name": "遐蝶皮肤(ANI).safetensors", "lora_strength": 0.8 }
  ]
}
```
**多服装隔离规则**（重要）：
- 每套服装的 `negative_suppress` 填**该服装自身的区分性标签**
- 选中服装 A 时，后端把所有**其它**服装的 `activation_words ∪ negative_suppress` 加入 negative，并**减去**「当前服装 + 角色级」词表（差集抑制），既彻底压制又不自伤、不压角色身份词
- 第一套服装 `id` 必须是 `default`（未指定服装时的默认项）

### `styles.json`
`lora_name` 为 `null` 表示纯画师标签风格（用 `artist_tags`）。

现有种子数据：**9 个角色（4 个多服装：遐蝶/流萤/昔涟/薇薇安）+ 8 个风格**（5 带 LoRA、3 纯画师标签）。遐蝶两套触发词取自真实笔记 `loras\遐蝶.txt` / `遐蝶皮肤.txt`；其余部分触发词是可编辑种子，建议出图后自行校准。

### 底模护栏
`base_model` 必须为 `anima`。`validate_lora_base_model()` 会拒绝文件名含 `(Illustrious)` / `(SDXL)` / `pony` 的 LoRA —— 混用会得到完全错误的画面。

---

## 4. LLM 配置

### 4.1 本地（推荐，绑 2060）
节点会自动拉起 `llama-server`（默认 `你的 llama-server 路径`），并：
- 用 `CUDA_VISIBLE_DEVICES=<物理index>` 精确绑卡，**默认 2060**
- 默认 `-ngl 99 -c 8192`，显存实测 **5.34 GB**
- **默认附加 `--reasoning off`**（关键，见 §5.2）
- 端口自动挑（8081 起，避开 8080/8188/8199），同模型同卡复用
- 实测：启动 4.63s、首 token 0.119s、§7 重写任务 2.5–3.4s 返回合法 JSON

可用模型（`你的模型根目录`）：`Qwen3.5-9B-Q4_K_M`（默认）、`Qwen3.5-4B`、`Qwen3-4B-Thinking`、`gemma-4-12B-it`、`DeepSeek-R1-0528-Qwen3-8B`。

### 4.2 云端（v2：34 家供应商开箱可用）

`provider` 下拉直接列全，数据真源是 `plugin\providers.py` 的 `PROVIDERS`（前端经 `GET /nl2prompt/providers` 拉取，**不再硬编码**）。

**国内（15）**：DeepSeek 深度求索 / 阿里通义千问（DashScope）/ 智谱 GLM / Moonshot Kimi / 字节豆包（火山方舟）/ 硅基流动 / MiniMax / 百度文心（千帆）/ 腾讯混元 / 讯飞星火 / 阶跃星辰 / 零一万物 / 百川 / 商汤日日新 / 魔搭 ModelScope

**国际（15）**：OpenAI / Anthropic Claude / Google Gemini / xAI Grok / Mistral / Cohere / Groq / Together / Fireworks / OpenRouter / DeepInfra / Perplexity / Cerebras / GitHub Models / Azure OpenAI

**本地（3）**：Ollama `127.0.0.1:11434` / LM Studio `127.0.0.1:1234` / vLLM `127.0.0.1:8000`　**兜底**：`custom`（base_url 自己填）

选中供应商后 `base_url` 自动填好，节点正面会显示一行「当前：<中文名> · <base_url>」。

**「拉取模型」按钮**（云端模式）：向 `{base_url}/models` 请求模型列表，填进可编辑下拉；选中即写入 `cloud_model`，也可以直接手打。结果缓存 10 分钟（存 workflow 里）。

**地址可信度**：`tools\verify_providers.py` 会逐家探活（`GET {base_url}/models`，不带 key），结果见 [docs/history/providers-verify.md](docs/history/providers-verify.md)。实测直连 23 家地址有效（401/403 或 200），8 家网络不可达（本机无出口链路，**不代表地址错**）、1 家 `/models` 端点被官方下线（`lingyiwanwu` 返回 410，但 `/chat/completions` 路径存在）、2 家需自填地址。供应商下线或改地址不会自动感知，**建议定期重跑该脚本**。

> 未校准的供应商在节点小字里会标「（未校准）」，可直接用，只是地址未经验证。

### 4.3 密钥优先级
```
ANIMA_LLM_API_KEY_<供应商大写>  >  ANIMA_LLM_API_KEY  >  data\secrets.json[<provider>]  >  data\secrets.json["_default"]
```
密钥**永不写入 workflow**（前端只有 `api_key_configured: bool`），`data\secrets.json` 已在 `.gitignore` 里。

---

## 5. 已知问题与注意事项

### 5.1 【重要】GBK 编码会打死 ComfyUI 执行线程
中文 Windows 下 ComfyUI 进程 `sys.stdout.encoding = gbk`。若任何输出含 GBK 不可编码字符（如 `›` U+203A），会：
```
UnicodeEncodeError → lost sys.stderr → prompt_worker 线程死亡 → 出图队列永久卡死
```
现象极具误导性：**HTTP 路由仍然正常应答**，看起来进程还活着，但任务永远停在 `queue_running`，`/interrupt` 和 `queue clear` 都无效，只能重启。

本项目已上三层防线：
1. 源码层：全仓扫描 GBK 不可编码字符，0 命中
2. 输出层：`_safe_text()` 按 stderr/stdout 的 encoding 做 `replace` 降级；且 `compose()` 不向 ComfyUI 回抛异常
3. 进程层：`start_test.ps1` 强制 `PYTHONUTF8=1` + `-X utf8`

**你部署到自己的环境时，建议给 ComfyUI 启动脚本也加上 `-X utf8` 或 `set PYTHONUTF8=1`。**

复验命令：
```powershell
<ComfyUI>\python\python.exe -X utf8 -c "import pathlib,sys;bad=0
for p in sorted(pathlib.Path(r'<项目根>\plugin').rglob('*.py')):
    for i,l in enumerate(p.read_text(encoding='utf-8').splitlines(),1):
        try: l.encode('gbk')
        except UnicodeEncodeError as e: bad+=1;print(f'{p}:{i} U+{ord(l[e.start]):04X}')
print('bad_lines =',bad);sys.exit(1 if bad else 0)"
```

### 5.2 Qwen3.5 是 thinking 模型
不加 `--reasoning off` 时 `message.content` **恒为空**（思考吃掉全部 max_tokens，实测 44.8s 返回空内容）。`--reasoning-budget 0` 无效，从 `reasoning_content` 正则提取会拿到占位符草稿（假阳性，比失败更糟）。插件已默认处理。

### 5.3 主模型文件名与任务书不一致
任务书写的 `anima-base-v1.0.safetensors` 本机不存在。实际使用 `anima-preview3-base.safetensors`。备选：`unet\Anima-2.9B-preview-v1.safetensors`、`unet\waiANIMA_v10.safetensors`。改工作流里 `UNETLoader` 的 `unet_name` 即可切换。

### 5.4 LoRA 底模必须匹配
`(ANI)` 后缀兼容 Anima；`(Illustrious)` / `(SDXL)` / `pony` **不兼容**，混用会出完全错误的画面。插件有校验但不阻断（返回 `ok=False`）。

### 5.5 云端不支持 `top_k`
OpenAI 兼容接口通常不支持 `top_k`，插件在云端模式**不发送**该参数（避免 400），窗口内对应输入框灰化。

### 5.6 其余模块的日志降级
`store.py` / `providers.py` / `runtime.py` 的告警输出有 `try/except` 包裹，编码失败时**静默丢日志**但不会打死线程。属已知小瑕疵，不影响功能。

### 5.7 供应商 `verified` 是静态快照
`PROVIDERS` 里的 `verified` 字段由 `tools\verify_providers.py` 探活时写入，**不会自动更新**。平台改域名/下线端点后需要手动重跑：

```powershell
<ComfyUI>\python\python.exe -X utf8 tools\verify_providers.py --out docs/history/providers-verify.md
```

本机 7890 代理**当前未监听**（探活脚本会自动区分「网络不可达」与「地址错」，不会把网络限制误记成地址错误）。海外供应商在国内网络下大多需要代理，此时 `verified=false` 只代表"本机连不上"，不代表地址写错。

### 5.8 默认值的取舍
- `负面基础词` 在 workflow 里默认是**空**；留空时后端会自动使用 `data\settings.json` 的 `default_negative_base`（一份推荐基线）。想把它固化进 workflow，点设置窗口的「填充推荐值」。
- `模型路径[本地]` 默认空，自动探测顺序：`settings.json.llm_model_path` → `settings.json.model_roots[0]` → 环境变量 `ANIMA_LLM_MODELS` → LM Studio / Ollama 常见目录。**每个候选都要求真的含 >100MB 的 .gguf**，空的占位目录会被跳过。
- `base_url` 默认空，随 `provider` 自动填。

### 5.9 LLM 模式的负向抑制
模板模式下 negative 一定包含同角色其它服装的全部抑制词（差集抑制，确定性）。走 LLM 时抑制词由模型自由发挥，实测覆盖率不稳（3 次实跑 7/11、0/11、11/11），因此 v2 在 LLM 返回后会把模板的抑制词**强制并入** negative，保证语义不依赖模型自觉。若你在 debug 里看到 `skill_check.negative_missing` 非空，说明该次模型自己漏写了（已由合并兜底）。

### 5.10 校验器不会报「嵌套权重」的越界
`skill_check` 的权重判据是「只有 `(标签:数字)` 才算权重语法」（这条判据是为了不误杀 `robin (honkai: star rail)` 这类 danbooru 系列名，否则 9 个角色里 5 个会失败）。

副作用：**权重写在带括号的角色名外层时不被校验**，例如 `(robin (honkai: star rail):9.9)` —— 越界也不会报错。影响很小：①不会误判失败、不会触发回退；②Anima 的 CLIP 对超过 1.5 的权重本身就会饱和处理。日常使用建议把权重写在无括号的标签上（`(long hair:1.2)`），这类一定被正确校验。

已知小修项，计划在 v2.1 处理（改用括号深度扫描而非单层正则）。

---

## 6. 测试与验收

全部在隔离测试环境跑：

| 项目 | 结果 |
|---|---|
| 纯函数验收（`tests\test_composer.py`） | **60/60 PASS** |
| 端到端验收（`tests\run_acceptance.py --only all`） | **51/51 PASS**，97.2s |
| v2 验收（`tests\test_v2.py`，C1–C5） | **96/96 PASS** |
| 真实 LLM 重写实跑（`tests\test_rewrite_real.py`） | 3/3 成功，`skill_check` 3/3 通过 |
| P0 回归（`--only a8 --enable-llm-crash-path`） | **15/15 PASS** |
| 后端自检（成员内部） | 367/367 + GBK 专项 0 命中 |
| 数据层自检 | 61/61 |
| skill 自检 | 57/57（标签核对 66/74 命中，未命中全是 danbooru 不收录的质量词） |
| P0 回归（`--only a8 --enable-llm-crash-path`） | **15/15 PASS** |
| 后端自检（成员内部） | 242/242 + GBK 专项 11/11 |
| 数据层自检 | 61/61 |
| 前端 DOM 断言 | 102/102 |

**出图实测**
| 工作流 | 耗时 | 尺寸 | 峰值显存 |
|---|---|---|---|
| `NL2Prompt_MVP` | 48.27s | 1024×1024 | GPU0 7377 MiB / GPU1 0 |
| `NL2Prompt_WithLoRA` | 44.36s | 1024×1024 | GPU0 7895 MiB / GPU1 0 |

全程无 CUDA OOM。产物在 `test_env\output\NL2Prompt_MVP\`。

详细逐条记录见 `docs\测试记录.md`（A1–A9 / B1–B8 + 证据 + 命令）。

**UI 已自动化验证（8/8 PASS）**：用 Chrome headless + CDP 探针在真实前端里操作 LiteGraph 图与 DOM，覆盖 A1 节点结构、A2 动态行增删与序列化、A4 设置窗口三种关闭方式与「恢复默认」二次确认的两分支、A5 种子小字渲染、A7 本地/云端显隐与 base_url 联动、A8 密钥不落盘、LLM 采样参数 Tab、`.workflow.json` 可加载（11 节点/14 连线）。

工具在 `tools\ui_probe.py`（A1/A2/A7）、`tools\ui_probe2.py`（A4/A8）、`tools\ui_probe3.py`（A5 + workflow 加载），纯标准库、可直接重跑：

```powershell
<ComfyUI>\python\python.exe -X utf8 tools\ui_probe2.py --shot log\ui_a4.png
```

截图证据在 `log\`：`ui_a4.png`（提示词设置窗口）、`ui_wf_mvp.png`（完整工作流）、`ui_probe_1.png`（动态行）。逐条证据见 [docs/history/测试记录.md](docs/history/测试记录.md) §8。

---

## 7. 目录结构

```
ComfyUI_NatureTextToPrompt\
├─ plugin\                       ← 插件源码（联接到 custom_nodes）
│   ├─ __init__.py               节点注册（容错，缺模块也能启动）
│   ├─ nodes.py                  3 个节点 + compose_plain 纯函数
│   ├─ llm_client.py             LLM 调用与回退
│   ├─ routes.py                 9 条 HTTP 路由
│   ├─ runtime.py                本地 llama-server 生命周期
│   ├─ providers.py              供应商映射与密钥
│   ├─ store.py                  数据加载/解析/校验
│   ├─ data\                     角色/风格配置（+ 运行时 secrets.json、logs\）
│   └─ web\                      前端 UI（3 个 js）
├─ test_env\                     ← 隔离测试环境（ComfyUI 0.45.3，可整目录删除）
├─ workflows\                    示例工作流（API + UI 格式）
├─ tests\                        验收脚本
├─ docs\                         实施计划、接口契约、测试记录
└─ log\                          逐轮进度日志（INDEX.md 为索引）
```

## 8. 开发约定

- 接口契约是唯一真源：`docs\接口契约.md`（节点 IO、holder JSON schema、组装算法、路由、错误码、前端窗口、实测裁决）
- 实施计划：`docs\实施计划.md`
- 每轮工作日志：`log\`，索引在 `log\INDEX.md`

---

## 许可证 / License

本项目采用 **双许可（dual licensing）** 模式：

| 使用场景 | 许可 | 费用 |
|---|---|---|
| 个人使用、学习、公司内部工作流、开源项目 | **GNU AGPL-3.0** | 免费 |
| 闭源产品集成、付费 SaaS、需要不公开源码的商业场景 | **商业授权** | 需购买 |

- 完整协议文本：[LICENSE](LICENSE)（AGPL-3.0-only）
- 双许可说明与 FAQ：[LICENSING.md](LICENSING.md)
- 商业授权申请模板：[LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md)

> **AGPL-3.0 要点**：可以自由使用、修改、分发；但如果你把修改后的版本**分发**出去，或作为**网络服务**提供给别人用，则必须同样以 AGPL-3.0 公开完整源码。不想开源请购买商业授权。
>
> 修改本项目需声明：保留版权声明、标注改动的文件、说明改动内容（AGPL-3.0 §5）。

**Copyright (C) 2026 GQLI-VC**
