# 供应商地址探活报告

> 本文件由 `tools/verify_providers.py` 自动生成，请勿手工编辑表格。
> 数据真源：`plugin/providers.py` 的 `PROVIDERS`（契约 §14.1）。

| 项 | 值 |
|---|---|
| 生成时间 | `2026-11-17T23:05:44+0800` |
| 探测目标 | `{base_url}/models`（**不带 api_key**） |
| 超时 | `8.0s` / 请求 |
| 供应商条数 | `34` |
| Python | `3.13.11` |
| 轮次 | direct（直连）, proxy（http://127.0.0.1:7890） |
| 结论判定基准 | 各轮取信息量最大的探测（拿到 HTTP 响应优先于 unreachable / base_url 空） |

## 判定口径

| 响应 | verified | 含义 |
|---|---|---|
| `401 / 403` | `True` | 地址有效，仅缺鉴权 |
| `2xx` | `True` | 地址与路径均有效 |
| `404` | `False` | 路径疑似错误 |
| 其它 HTTP 状态 | `False` | 如实上报，不猜测 |
| DNS/连接/超时 | `False` | **网络不可达**，不代表地址错 |
| `base_url` 为空 | `False` | 需用户填写 |

## 汇总（各轮对比）

| 轮次 | 网络模式 | 条目 | verified=True | 401/403 | 200 | 404 | 网络不可达 | base_url 空 | 其它 |
|---|---|---|---|---|---|---|---|---|---|
| direct（直连） | 直连（已清空代理环境变量） | 34 | **23** | 19 | 4 | 0 | 8 | 2 | 1 |
| proxy（http://127.0.0.1:7890） | 显式代理 http://127.0.0.1:7890（no_proxy=127.0.0.1,localhost,::1） | 34 | **0** | 0 | 0 | 0 | 32 | 2 | 0 |

> ⚠️ **有轮次的网络链路整体不可用**：
> - `proxy（http://127.0.0.1:7890）` 轮 **0 家**拿到 HTTP 响应（全部 `unreachable`）。该轮结论**不能**用于判定地址对错——最可能的原因是代理端口未监听/未启动。
> - 因此 `verified` 结论取自**能拿到 HTTP 响应的那一轮**（见上文判定基准）。

## 明细：direct（直连） 轮

- 网络模式：`直连（已清空代理环境变量）`
- 链路可用性：可用（至少一家拿到 HTTP 响应）

| id | label | base_url | openai_compat | 探测结果 | verified | 备注 |
|---|---|---|---|---|---|---|
| `openai` | OpenAI | `https://api.openai.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `anthropic` | Anthropic Claude | `https://api.anthropic.com/v1` | ❌ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403）<br>📝 原生 API 为 /v1/messages，非 OpenAI 兼容；需兼容层才能走 /chat/completions |
| `google` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 使用 Gemini 官方 OpenAI 兼容层（/v1beta/openai）；本机直连探活不可达（疑网络限制） |
| `xai` | xAI Grok | `https://api.x.ai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 本机直连探活不可达（疑网络/代理限制，非地址错误） |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 本机直连探活不可达（疑网络/代理限制，非地址错误） |
| `cohere` | Cohere | `https://api.cohere.ai/compatibility/v1` | ✅ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403）<br>📝 OpenAI 兼容层路径为 /compatibility/v1 |
| `groq` | Groq | `https://api.groq.com/openai/v1` | ✅ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403）<br>📝 OpenAI 兼容路径为 /openai/v1 |
| `together` | Together AI | `https://api.together.xyz/v1` | ✅ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403）<br>📝 模型名以官方 /v1/models 列表为准 |
| `fireworks` | Fireworks AI | `https://api.fireworks.ai/inference/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 模型名为 accounts/<acct>/models/<model> 形式 |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | ✅ | 200 | ✅ True | 地址有效（HTTP 200） |
| `deepinfra` | DeepInfra | `https://api.deepinfra.com/v1/openai` | ✅ | 200 | ✅ True | 地址有效（HTTP 200）<br>📝 OpenAI 兼容路径为 /v1/openai |
| `perplexity` | Perplexity | `https://api.perplexity.ai` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 base_url 无 /v1 后缀，直接拼 /chat/completions；本机直连探活不可达（疑网络限制） |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | ✅ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403） |
| `github` | GitHub Models | `https://models.github.ai/inference` | ✅ | 200 | ✅ True | 地址有效（HTTP 200）<br>📝 旧端点为 https://models.inference.ai.azure.com（两者字段一致） |
| `azure` | Azure OpenAI | *(空)* | ✅ | - | ❌ False | base_url 为空，需用户填写<br>📝 base_url 必须由用户填写，形如 https://<resource>.openai.azure.com/openai/deployments/<dep> |
| `deepseek` | DeepSeek 深度求索 | `https://api.deepseek.com/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `moonshot` | Moonshot Kimi 月之暗面 | `https://api.moonshot.cn/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `dashscope` | 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 OpenAI 兼容模式路径为 /compatible-mode/v1 |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `minimax` | MiniMax 稀宇科技 | `https://api.minimax.chat/v1` | ❌ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 原生对话路径为 /v1/text/chatcompletion_v2，与标准 /chat/completions 不同 |
| `volcengine` | 字节豆包（火山方舟） | `https://ark.cn-beijing.volces.com/api/v3` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 模型名是推理接入点 endpoint id（ep-xxxx），需用户在控制台创建 |
| `siliconflow` | 硅基流动 SiliconFlow | `https://api.siliconflow.cn/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 模型名以官方 /v1/models 列表为准 |
| `qianfan` | 百度文心千帆 | `https://qianfan.baidubce.com/v2` | ✅ | 403 | ✅ True | 地址有效（缺鉴权，HTTP 403）<br>📝 v2 为 OpenAI 兼容接口 |
| `hunyuan` | 腾讯混元 | `https://api.hunyuan.cloud.tencent.com/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `spark` | 讯飞星火 | `https://spark-api-open.xf-yun.com/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 HTTP 鉴权使用 APIPassword；模型名形如 generalv3.5 |
| `stepfun` | 阶跃星辰 StepFun | `https://api.stepfun.com/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `lingyiwanwu` | 零一万物 Yi | `https://api.lingyiwanwu.com/v1` | ✅ | 410 | ❌ False | 未预期状态 HTTP 410（不判定为地址错误，需人工复核）<br>📝 实测 `{base}/models` 返回 410 Gone、`{base}/chat/completions` 返回 405（路径存在）：地址本身有效，但 /models 列表端点已下线 |
| `baichuan` | 百川智能 | `https://api.baichuan-ai.com/v1` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401） |
| `sensenova` | 商汤日日新 SenseNova | `https://api.sensenova.cn/v1/llm` | ✅ | 401 | ✅ True | 地址有效（缺鉴权，HTTP 401）<br>📝 base_url 由实测校正：`{base}/models` 返回 401（地址有效）；此前的 /compatible-mode/v1 实测 404。标准 /chat/completions 子路径未通过实测，调用前请以商汤官方文档核对对话路径 |
| `modelscope` | 魔搭 ModelScope | `https://api-inference.modelscope.cn/v1` | ✅ | 200 | ✅ True | 地址有效（HTTP 200）<br>📝 需绑定阿里云账号的 ModelScope Token；模型名为 <org>/<model> 形式 |
| `ollama` | Ollama（本地） | `http://127.0.0.1:11434/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需先在本机启动 ollama serve |
| `lmstudio` | LM Studio（本地） | `http://127.0.0.1:1234/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需在 LM Studio 中开启 Local Server |
| `vllm` | vLLM（本地） | `http://127.0.0.1:8000/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需先以 --api-key 或免鉴权方式启动 vLLM |
| `custom` | 自定义（OpenAI 兼容） | *(空)* | ✅ | - | ❌ False | base_url 为空，需用户填写<br>📝 base_url 与模型名均由用户填写 |

## 明细：proxy（http://127.0.0.1:7890） 轮

- 网络模式：`显式代理 http://127.0.0.1:7890（no_proxy=127.0.0.1,localhost,::1）`
- 链路可用性：**整体不可用**（0 家拿到 HTTP 响应，仅能证明该链路不通）

| id | label | base_url | openai_compat | 探测结果 | verified | 备注 |
|---|---|---|---|---|---|---|
| `openai` | OpenAI | `https://api.openai.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `anthropic` | Anthropic Claude | `https://api.anthropic.com/v1` | ❌ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 原生 API 为 /v1/messages，非 OpenAI 兼容；需兼容层才能走 /chat/completions |
| `google` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 使用 Gemini 官方 OpenAI 兼容层（/v1beta/openai）；本机直连探活不可达（疑网络限制） |
| `xai` | xAI Grok | `https://api.x.ai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 本机直连探活不可达（疑网络/代理限制，非地址错误） |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 本机直连探活不可达（疑网络/代理限制，非地址错误） |
| `cohere` | Cohere | `https://api.cohere.ai/compatibility/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 OpenAI 兼容层路径为 /compatibility/v1 |
| `groq` | Groq | `https://api.groq.com/openai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 OpenAI 兼容路径为 /openai/v1 |
| `together` | Together AI | `https://api.together.xyz/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 模型名以官方 /v1/models 列表为准 |
| `fireworks` | Fireworks AI | `https://api.fireworks.ai/inference/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 模型名为 accounts/<acct>/models/<model> 形式 |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `deepinfra` | DeepInfra | `https://api.deepinfra.com/v1/openai` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 OpenAI 兼容路径为 /v1/openai |
| `perplexity` | Perplexity | `https://api.perplexity.ai` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 base_url 无 /v1 后缀，直接拼 /chat/completions；本机直连探活不可达（疑网络限制） |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `github` | GitHub Models | `https://models.github.ai/inference` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 旧端点为 https://models.inference.ai.azure.com（两者字段一致） |
| `azure` | Azure OpenAI | *(空)* | ✅ | - | ❌ False | base_url 为空，需用户填写<br>📝 base_url 必须由用户填写，形如 https://<resource>.openai.azure.com/openai/deployments/<dep> |
| `deepseek` | DeepSeek 深度求索 | `https://api.deepseek.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `moonshot` | Moonshot Kimi 月之暗面 | `https://api.moonshot.cn/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `dashscope` | 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 OpenAI 兼容模式路径为 /compatible-mode/v1 |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `minimax` | MiniMax 稀宇科技 | `https://api.minimax.chat/v1` | ❌ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 原生对话路径为 /v1/text/chatcompletion_v2，与标准 /chat/completions 不同 |
| `volcengine` | 字节豆包（火山方舟） | `https://ark.cn-beijing.volces.com/api/v3` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 模型名是推理接入点 endpoint id（ep-xxxx），需用户在控制台创建 |
| `siliconflow` | 硅基流动 SiliconFlow | `https://api.siliconflow.cn/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 模型名以官方 /v1/models 列表为准 |
| `qianfan` | 百度文心千帆 | `https://qianfan.baidubce.com/v2` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 v2 为 OpenAI 兼容接口 |
| `hunyuan` | 腾讯混元 | `https://api.hunyuan.cloud.tencent.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `spark` | 讯飞星火 | `https://spark-api-open.xf-yun.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 HTTP 鉴权使用 APIPassword；模型名形如 generalv3.5 |
| `stepfun` | 阶跃星辰 StepFun | `https://api.stepfun.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `lingyiwanwu` | 零一万物 Yi | `https://api.lingyiwanwu.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 实测 `{base}/models` 返回 410 Gone、`{base}/chat/completions` 返回 405（路径存在）：地址本身有效，但 /models 列表端点已下线 |
| `baichuan` | 百川智能 | `https://api.baichuan-ai.com/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错） |
| `sensenova` | 商汤日日新 SenseNova | `https://api.sensenova.cn/v1/llm` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 base_url 由实测校正：`{base}/models` 返回 401（地址有效）；此前的 /compatible-mode/v1 实测 404。标准 /chat/completions 子路径未通过实测，调用前请以商汤官方文档核对对话路径 |
| `modelscope` | 魔搭 ModelScope | `https://api-inference.modelscope.cn/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需绑定阿里云账号的 ModelScope Token；模型名为 <org>/<model> 形式 |
| `ollama` | Ollama（本地） | `http://127.0.0.1:11434/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需先在本机启动 ollama serve |
| `lmstudio` | LM Studio（本地） | `http://127.0.0.1:1234/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需在 LM Studio 中开启 Local Server |
| `vllm` | vLLM（本地） | `http://127.0.0.1:8000/v1` | ✅ | unreachable | ❌ False | **网络不可达**（可能是本机网络/代理限制，不代表地址错）<br>📝 需先以 --api-key 或免鉴权方式启动 vLLM |
| `custom` | 自定义（OpenAI 兼容） | *(空)* | ✅ | - | ❌ False | base_url 为空，需用户填写<br>📝 base_url 与模型名均由用户填写 |

## 直连 vs 代理 差异

以 `direct（直连）` 轮为基准，列出与后续轮次结论不一致的条目：

> ⚠️ 存在链路整体不可用的轮次（proxy（http://127.0.0.1:7890）），因此下表的差异**只反映网络链路差异，不反映地址对错**。

| id | label | direct（直连） 轮 | proxy（http://127.0.0.1:7890） 轮 | 说明 |
|---|---|---|---|---|
| `anthropic` | Anthropic Claude | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `cohere` | Cohere | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `groq` | Groq | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `together` | Together AI | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `fireworks` | Fireworks AI | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `openrouter` | OpenRouter | 200 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `deepinfra` | DeepInfra | 200 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `cerebras` | Cerebras | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `github` | GitHub Models | 200 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `deepseek` | DeepSeek 深度求索 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `moonshot` | Moonshot Kimi 月之暗面 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `dashscope` | 阿里通义千问 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `zhipu` | 智谱 GLM | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `minimax` | MiniMax 稀宇科技 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `volcengine` | 字节豆包（火山方舟） | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `siliconflow` | 硅基流动 SiliconFlow | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `qianfan` | 百度文心千帆 | 403 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `hunyuan` | 腾讯混元 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `spark` | 讯飞星火 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `stepfun` | 阶跃星辰 StepFun | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `lingyiwanwu` | 零一万物 Yi | 410 / ❌ False | unreachable / ❌ False | 两轮均未通过（不一致原因见各轮备注） |
| `baichuan` | 百川智能 | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `sensenova` | 商汤日日新 SenseNova | 401 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |
| `modelscope` | 魔搭 ModelScope | 200 / ✅ True | unreachable / ❌ False | 仅某一轮拿到 HTTP 响应；**另一轮链路整体不可用**（不能据此判地址错） |

## 结论（合并各轮最大信息量）

| 分类 | 家数 | verified |
|---|---|---|
| 已核实地址有效（2xx + 401/403） | 23 | `True` |
| 网络不可达（不能证伪） | 8 | `False` |
| 路径疑似错误（404） | 0 | `False` |
| 其它 HTTP 状态（需人工复核） | 1 | `False` |
| `base_url` 为空 | 2 | `False` |

## `verified: False` 清单

**严格区分「本机网络/代理限制」与「地址或路径错误」**，两者不可混为一谈：

### A. 网络不可达（8 家）—— 不代表地址错

这些条目在**所有轮次**里都是 DNS/连接/超时失败——即本机找不到任何可用链路到达该地址。
结论只能是「未核实」，**不能**判为地址错误。要判定真伪，需要在能出网的机器上重跑，
或先启动代理后用 `--proxy` 重跑。

- `openai`（OpenAI）：https://api.openai.com/v1
- `google`（Google Gemini）：https://generativelanguage.googleapis.com/v1beta/openai
- `xai`（xAI Grok）：https://api.x.ai/v1
- `mistral`（Mistral AI）：https://api.mistral.ai/v1
- `perplexity`（Perplexity）：https://api.perplexity.ai
- `ollama`（Ollama（本地））：http://127.0.0.1:11434/v1
- `lmstudio`（LM Studio（本地））：http://127.0.0.1:1234/v1
- `vllm`（vLLM（本地））：http://127.0.0.1:8000/v1

### B. 404 —— 路径疑似错误（0 家）

*无*

### C. 其它 HTTP 状态 —— 需人工复核（1 家）

这些状态码既不是 401/403（可判有效），也不是 404（可判路径错），因此**不猜测**，如实记录：

- `lingyiwanwu`（零一万物 Yi）：`410` — 未预期状态 HTTP 410（不判定为地址错误，需人工复核）
  - 📝 注册表备注：实测 `{base}/models` 返回 410 Gone、`{base}/chat/completions` 返回 405（路径存在）：地址本身有效，但 /models 列表端点已下线

### D. base_url 为空（2 家）—— 由用户填写

- `azure`（Azure OpenAI）：base_url 为空，需用户填写
- `custom`（自定义（OpenAI 兼容））：base_url 为空，需用户填写

## 已核实地址有效（23 家）

- `anthropic`（Anthropic Claude）：403 — 地址有效（缺鉴权，HTTP 403）
- `cohere`（Cohere）：403 — 地址有效（缺鉴权，HTTP 403）
- `groq`（Groq）：403 — 地址有效（缺鉴权，HTTP 403）
- `together`（Together AI）：403 — 地址有效（缺鉴权，HTTP 403）
- `fireworks`（Fireworks AI）：401 — 地址有效（缺鉴权，HTTP 401）
- `openrouter`（OpenRouter）：200 — 地址有效（HTTP 200）
- `deepinfra`（DeepInfra）：200 — 地址有效（HTTP 200）
- `cerebras`（Cerebras）：403 — 地址有效（缺鉴权，HTTP 403）
- `github`（GitHub Models）：200 — 地址有效（HTTP 200）
- `deepseek`（DeepSeek 深度求索）：401 — 地址有效（缺鉴权，HTTP 401）
- `moonshot`（Moonshot Kimi 月之暗面）：401 — 地址有效（缺鉴权，HTTP 401）
- `dashscope`（阿里通义千问）：401 — 地址有效（缺鉴权，HTTP 401）
- `zhipu`（智谱 GLM）：401 — 地址有效（缺鉴权，HTTP 401）
- `minimax`（MiniMax 稀宇科技）：401 — 地址有效（缺鉴权，HTTP 401）
- `volcengine`（字节豆包（火山方舟））：401 — 地址有效（缺鉴权，HTTP 401）
- `siliconflow`（硅基流动 SiliconFlow）：401 — 地址有效（缺鉴权，HTTP 401）
- `qianfan`（百度文心千帆）：403 — 地址有效（缺鉴权，HTTP 403）
- `hunyuan`（腾讯混元）：401 — 地址有效（缺鉴权，HTTP 401）
- `spark`（讯飞星火）：401 — 地址有效（缺鉴权，HTTP 401）
- `stepfun`（阶跃星辰 StepFun）：401 — 地址有效（缺鉴权，HTTP 401）
- `baichuan`（百川智能）：401 — 地址有效（缺鉴权，HTTP 401）
- `sensenova`（商汤日日新 SenseNova）：401 — 地址有效（缺鉴权，HTTP 401）
- `modelscope`（魔搭 ModelScope）：200 — 地址有效（HTTP 200）

## 与注册表 `verified` 字段的偏差

只统计**已拿到明确 HTTP 响应**的条目（`unreachable` / `base_url` 空不参与，因为不能证伪地址）。

*无偏差（注册表与实测一致，或剩余偏差均属于网络不可达、无法下结论）*

## 方法学说明

- 探测只发 `GET {base_url}/models`，**不带** `Authorization`，因此 `401/403` 是「地址正确」的强信号。
- `403` 也可能由 CDN/WAF/地区封锁返回，其可信度低于 `401`；若某家长期只返回 `403`，请人工复核。
- 本地供应商（`ollama` / `lmstudio` / `vllm`）在未启动服务时必然 `unreachable`，这是**预期结果**。
- 走代理时脚本自动把 `127.0.0.1 / localhost` 加入 `no_proxy`，避免本地服务被代理劫持造成假阴性。
- 若 `--proxy` 指向的端口未监听，该轮所有条目都会是 `ConnectionRefusedError` → `unreachable`；
  这不代表任何地址有问题，报告会把该轮标记为「链路整体不可用」。
- 报告不记录任何 api_key；代理 URL 中的凭据已脱敏为 `***@`。

