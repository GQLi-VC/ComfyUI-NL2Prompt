# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""供应商注册表与 api_key 解析（llm-runtime 负责）。

契约参考：docs/接口契约.md **§14.1（供应商注册表）/ §13.2（secrets 单一真源）/
§13.3（api_key 优先级）**，以及 §2.2 / §6 / §8。

设计原则
--------
1. **绝不抛异常**：所有对外函数在失败时返回 ``None`` / ``False`` 并打印告警，
   保证 ComfyUI 节点执行链路不被密钥问题打断（节点层负责回退模板）。
2. **绝不泄漏明文 key**：本模块任何返回值/日志/异常信息都不包含明文 key，
   需要展示时只能走 :func:`mask`，它恒返回 ``"***"``。
3. 只用标准库（原 ComfyUI Python 环境不允许装包）。
4. **单一真源**：v2 起本文件是供应商数据的唯一持有者。
   ``nodes.py`` / ``web/nl2prompt_load_llm.js`` 的 ``PROVIDER_BASE_URLS`` 必须改为
   消费本模块（过渡期可用 :data:`PROVIDER_BASE_URLS` 兼容别名）。

供应商注册表结构（§14.1）
--------------------------
``PROVIDERS`` 由「``id -> base_url`` 字符串」升级为「``id -> dict``」::

    PROVIDERS = {
        "openai": {
            "name": "OpenAI",              # 英文品牌名
            "label": "OpenAI",             # 前端显示名（中文优先）
            "base_url": "https://api.openai.com/v1",
            "openai_compat": True,         # 是否 OpenAI 兼容 /chat/completions
            "default_model": "gpt-4o-mini",
            "needs_key": True,
            "verified": True,              # 是否已确认可连通
            "note": "",
        },
        ...
    }

**向后兼容**：为兼容 v1 其它模块（``resolve_base_url`` 直接按下标取字符串），
本模块保留 :func:`_base_url_for` 内部取值函数与 :data:`PROVIDER_BASE_URLS`
扁平别名；:func:`resolve_base_url` / :func:`resolve_api_key` / :func:`api_key` /
:func:`save_api_key` / :func:`secret_status` / :func:`mask` / :data:`KNOWN_PROVIDERS` /
:data:`DATA_DIR` / :data:`SECRETS_PATH` / :data:`ENV_GLOBAL` / :data:`ENV_PREFIX`
的签名与行为均**不变**。

密钥优先级（§13.3，高 → 低）
----------------------------
1. 环境变量 ``ANIMA_LLM_API_KEY_<PROVIDER大写>``（分供应商）
2. 环境变量 ``ANIMA_LLM_API_KEY``（全局兜底）
3. ``plugin/data/secrets.json`` → ``{"<provider>": "<key>", "_default": "<key>"}``
   （``custom`` 或空 provider 读 ``_default``）
4. 都没有 → ``(None, "none")``

注意：契约 §6 的 ``/secret_status`` 只认 ``"env" | "file" | "none"`` 三种 source，
所以本模块的 ``source`` 也只用这三值（具体是哪个环境变量不对外暴露）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 供应商注册表（§14.1，单一真源）
# ---------------------------------------------------------------------------
# 字段说明见模块 docstring。default_model 只填**确定存在**的模型代号，
# 不确定的一律留空串 —— 绝不编造（用户可在云端模型 widget 手填）。
# verified 由 tools/verify_providers.py 实测回填，见 docs/history/providers-verify.md。

PROVIDERS: dict = {
    # ---------------- 国际 ----------------
    "openai": {
        "name": "OpenAI",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "openai_compat": True,
        "default_model": "gpt-4o-mini",
        "needs_key": True,
        "verified": False,
        "note": "",
    },
    "anthropic": {
        "name": "Anthropic",
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "openai_compat": False,
        "default_model": "claude-3-5-sonnet-20241022",
        "needs_key": True,
        "verified": True,
        "note": "原生 API 为 /v1/messages，非 OpenAI 兼容；需兼容层才能走 /chat/completions",
    },
    "google": {
        "name": "Google",
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "openai_compat": True,
        "default_model": "gemini-2.0-flash",
        "needs_key": True,
        "verified": False,
        "note": (
            "使用 Gemini 官方 OpenAI 兼容层（/v1beta/openai）"
        ),
    },
    "xai": {
        "name": "xAI",
        "label": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "openai_compat": True,
        "default_model": "",
        "needs_key": True,
        "verified": False,
        "note": "",
    },
    "mistral": {
        "name": "Mistral",
        "label": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "openai_compat": True,
        "default_model": "mistral-large-latest",
        "needs_key": True,
        "verified": False,
        "note": "",
    },
    "cohere": {
        "name": "Cohere",
        "label": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "openai_compat": True,
        "default_model": "command-r-plus-08-2024",
        "needs_key": True,
        "verified": True,
        "note": "OpenAI 兼容层路径为 /compatibility/v1",
    },
    "groq": {
        "name": "Groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "openai_compat": True,
        "default_model": "llama-3.3-70b-versatile",
        "needs_key": True,
        "verified": True,
        "note": "OpenAI 兼容路径为 /openai/v1",
    },
    "together": {
        "name": "Together AI",
        "label": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "openai_compat": True,
        "default_model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "needs_key": True,
        "verified": True,
        "note": "模型名以官方 /v1/models 列表为准",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "label": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "openai_compat": True,
        "default_model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "needs_key": True,
        "verified": True,
        "note": "模型名为 accounts/<acct>/models/<model> 形式",
    },
    "openrouter": {
        "name": "OpenRouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "openai_compat": True,
        "default_model": "openai/gpt-4o-mini",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "deepinfra": {
        "name": "DeepInfra",
        "label": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "openai_compat": True,
        "default_model": "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "needs_key": True,
        "verified": True,
        "note": "OpenAI 兼容路径为 /v1/openai",
    },
    "perplexity": {
        "name": "Perplexity",
        "label": "Perplexity",
        "base_url": "https://api.perplexity.ai",
        "openai_compat": True,
        "default_model": "sonar",
        "needs_key": True,
        "verified": False,
        "note": (
            "base_url 无 /v1 后缀，直接拼接 /chat/completions"
        ),
    },
    "cerebras": {
        "name": "Cerebras",
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "openai_compat": True,
        "default_model": "",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "github": {
        "name": "GitHub Models",
        "label": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "openai_compat": True,
        "default_model": "gpt-4o-mini",
        "needs_key": True,
        "verified": True,
        "note": "旧端点为 https://models.inference.ai.azure.com（两者字段一致）",
    },
    "azure": {
        "name": "Azure OpenAI",
        "label": "Azure OpenAI",
        "base_url": "",
        "openai_compat": True,
        "default_model": "",
        "needs_key": True,
        "verified": False,
        "note": "base_url 必须由用户填写，形如 https://<resource>.openai.azure.com/openai/deployments/<dep>",
    },
    # ---------------- 国内 ----------------
    "deepseek": {
        "name": "DeepSeek",
        "label": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "openai_compat": True,
        "default_model": "deepseek-chat",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "moonshot": {
        "name": "Moonshot",
        "label": "Moonshot Kimi 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "openai_compat": True,
        "default_model": "moonshot-v1-8k",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "dashscope": {
        "name": "Alibaba DashScope",
        "label": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "openai_compat": True,
        "default_model": "qwen-plus",
        "needs_key": True,
        "verified": True,
        "note": "OpenAI 兼容模式路径为 /compatible-mode/v1",
    },
    "zhipu": {
        "name": "Zhipu AI",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "openai_compat": True,
        "default_model": "glm-4-flash",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "minimax": {
        "name": "MiniMax",
        "label": "MiniMax 稀宇科技",
        "base_url": "https://api.minimax.chat/v1",
        "openai_compat": False,
        "default_model": "",
        "needs_key": True,
        "verified": True,
        "note": "原生对话路径为 /v1/text/chatcompletion_v2，与标准 /chat/completions 不同",
    },
    "volcengine": {
        "name": "Volcengine Ark",
        "label": "字节豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "openai_compat": True,
        "default_model": "",
        "needs_key": True,
        "verified": True,
        "note": "模型名是推理接入点 endpoint id（ep-xxxx），需用户在控制台创建",
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "openai_compat": True,
        "default_model": "deepseek-ai/DeepSeek-V3",
        "needs_key": True,
        "verified": True,
        "note": "模型名以官方 /v1/models 列表为准",
    },
    "qianfan": {
        "name": "Baidu Qianfan",
        "label": "百度文心千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "openai_compat": True,
        "default_model": "ernie-4.0-8k",
        "needs_key": True,
        "verified": True,
        "note": "v2 为 OpenAI 兼容接口",
    },
    "hunyuan": {
        "name": "Tencent Hunyuan",
        "label": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "openai_compat": True,
        "default_model": "hunyuan-turbo",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "spark": {
        "name": "iFlytek Spark",
        "label": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "openai_compat": True,
        "default_model": "generalv3.5",
        "needs_key": True,
        "verified": True,
        "note": "HTTP 鉴权使用 APIPassword；模型名形如 generalv3.5",
    },
    "stepfun": {
        "name": "StepFun",
        "label": "阶跃星辰 StepFun",
        "base_url": "https://api.stepfun.com/v1",
        "openai_compat": True,
        "default_model": "step-1-8k",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "lingyiwanwu": {
        "name": "01.AI",
        "label": "零一万物 Yi",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "openai_compat": True,
        "default_model": "yi-large",
        "needs_key": True,
        "verified": False,
        "note": (
            "实测 `{base}/models` 返回 410 Gone、`{base}/chat/completions` 返回 405（路径存在）：地址本身有效，但 /models 列表端点已下线"
        ),
    },
    "baichuan": {
        "name": "Baichuan",
        "label": "百川智能",
        "base_url": "https://api.baichuan-ai.com/v1",
        "openai_compat": True,
        "default_model": "Baichuan4",
        "needs_key": True,
        "verified": True,
        "note": "",
    },
    "sensenova": {
        "name": "SenseNova",
        "label": "商汤日日新 SenseNova",
        "base_url": "https://api.sensenova.cn/v1/llm",
        "openai_compat": True,
        "default_model": "SenseChat-5",
        "needs_key": True,
        "verified": True,
        "note": (
            "base_url 由实测校正：`{base}/models` 返回 401（地址有效）；"
            "此前的 /compatible-mode/v1 实测 404。标准 /chat/completions 子路径未通过实测，"
            "调用前请以商汤官方文档核对对话路径"
        ),
    },
    "modelscope": {
        "name": "ModelScope",
        "label": "魔搭 ModelScope",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "openai_compat": True,
        "default_model": "Qwen/Qwen2.5-72B-Instruct",
        "needs_key": True,
        "verified": True,
        "note": "需绑定阿里云账号的 ModelScope Token；模型名为 <org>/<model> 形式",
    },
    # ---------------- 本地 ----------------
    "ollama": {
        "name": "Ollama",
        "label": "Ollama（本地）",
        "base_url": "http://127.0.0.1:11434/v1",
        "openai_compat": True,
        "default_model": "llama3.1",
        "needs_key": False,
        "verified": False,
        "note": "需先在本机启动 ollama serve",
    },
    "lmstudio": {
        "name": "LM Studio",
        "label": "LM Studio（本地）",
        "base_url": "http://127.0.0.1:1234/v1",
        "openai_compat": True,
        "default_model": "",
        "needs_key": False,
        "verified": False,
        "note": "需在 LM Studio 中开启 Local Server",
    },
    "vllm": {
        "name": "vLLM",
        "label": "vLLM（本地）",
        "base_url": "http://127.0.0.1:8000/v1",
        "openai_compat": True,
        "default_model": "",
        "needs_key": False,
        "verified": False,
        "note": "需先以 --api-key 或免鉴权方式启动 vLLM",
    },
    # ---------------- 兜底 ----------------
    "custom": {
        "name": "Custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "openai_compat": True,
        "default_model": "",
        "needs_key": True,
        "verified": False,
        "note": "base_url 与模型名均由用户填写",
    },
}

#: 兼容别名（§14.1）：``{id: base_url}``，供 nodes.py / 前端过渡期使用。
PROVIDER_BASE_URLS: dict = {
    pid: str(meta.get("base_url") or "") for pid, meta in PROVIDERS.items()
}

#: ``provider_options()`` 的展示顺序：国内常用在前 → 国际 → 本地 → 兜底 custom。
#: 注：与 ``list(PROVIDERS.keys())`` 的顺序**不同**（后者按注册表书写顺序）。
_OPTION_ORDER: tuple = (
    # 国内常用（按本机使用频率）
    "deepseek",
    "dashscope",
    "zhipu",
    "moonshot",
    "volcengine",
    "siliconflow",
    "minimax",
    "qianfan",
    "hunyuan",
    "spark",
    "stepfun",
    "lingyiwanwu",
    "baichuan",
    "sensenova",
    "modelscope",
    # 国际
    "openai",
    "anthropic",
    "google",
    "xai",
    "mistral",
    "cohere",
    "groq",
    "together",
    "fireworks",
    "openrouter",
    "deepinfra",
    "perplexity",
    "cerebras",
    "github",
    "azure",
    # 本地
    "ollama",
    "lmstudio",
    "vllm",
    # 兜底
    "custom",
)

DATA_DIR: Path = Path(__file__).resolve().parent / "data"
SECRETS_PATH: Path = DATA_DIR / "secrets.json"

ENV_GLOBAL = "ANIMA_LLM_API_KEY"
ENV_PREFIX = "ANIMA_LLM_API_KEY_"

#: 已知供应商 id 元组（保持 v1 语义：id 的 tuple）。
#: 已知供应商之外的 provider 名（例如用户在 custom 里填 "moonshot"）也允许
#: 通过 ``ANIMA_LLM_API_KEY_<NAME>`` 注入。
KNOWN_PROVIDERS = tuple(PROVIDERS.keys())

#: ``probe()`` 的默认超时（秒）
PROBE_TIMEOUT = 8.0

#: ``probe()`` 的 UA —— 少数网关对空 UA 直接 403，会污染判定
_PROBE_UA = "anima-nl-providers-probe/1.0 (+comfyui-plugin)"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _warn(msg: str) -> None:
    """打印告警到 stderr（不抛异常）。"""
    try:
        print(f"[nl2prompt][providers] {msg}", file=sys.stderr)
    except Exception:
        pass


def _norm_provider(provider) -> str:
    """归一化 provider 名：去空白、转小写；非法输入返回 ``""``。"""
    if not isinstance(provider, str):
        return ""
    return provider.strip().lower()


def _meta(provider) -> dict:
    """取某供应商的注册表条目；未知返回空 dict（不抛）。"""
    prov = _norm_provider(provider)
    entry = PROVIDERS.get(prov)
    return entry if isinstance(entry, dict) else {}


def _base_url_for(provider) -> str:
    """取注册表内 base_url（原始值，可能为空串）。"""
    val = _meta(provider).get("base_url")
    return val if isinstance(val, str) else ""


def _env_name_for(provider: str) -> str:
    """构造分供应商环境变量名。"""
    return ENV_PREFIX + provider.upper().replace("-", "_").replace(" ", "_")


def _read_secrets() -> dict:
    """读 secrets.json 并归一化成 ``{"<slot>": key}`` 视图。

    兼容两种落盘结构（同一文件会被 ``llm_client.py`` 与本模块分别写入）：
      * 本模块：``{"deepseek": "...", "_default": "..."}``
      * llm_client.py：``{"api_key": "...", "updated_at": "..."}``
    其中 ``api_key`` 系列键一律映射到 ``_default`` 槽位。
    任何异常都返回空 dict。
    """
    try:
        if not SECRETS_PATH.is_file():
            return {}
        with open(SECRETS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        view = {}
        for alias in ("api_key", "apiKey", "key", "token"):
            val = data.get(alias)
            if isinstance(val, str) and val.strip():
                view["_default"] = val.strip()
                break
        for name, val in data.items():
            if name in ("api_key", "apiKey", "key", "token", "updated_at", "schema"):
                continue
            if isinstance(val, str) and val.strip():
                view[name] = val.strip()
        return view
    except Exception as exc:  # noqa: BLE001 - 明确不抛
        _warn(f"读取 secrets.json 失败（按空处理）：{type(exc).__name__}")
        return {}


def _write_secrets(view: dict) -> bool:
    """原子写 secrets.json，失败返回 False。

    ``view`` 是归一化视图；落盘时**同时**写 ``api_key``（供 ``llm_client.py`` 读取）
    与各 provider 槽位（供本模块读取），避免两处互相覆盖后谁都读不到。

    权限说明（Windows）：本函数刻意**不做** ``icacls`` 之类的 ACL 收紧动作——
    在 ComfyUI 宿主进程里调用会刷大量输出、且需要额外权限，副作用大于收益。
    实际防护靠三点：
      1. 文件落在插件私有目录 ``plugin/data/``，并已被 ``.gitignore`` 覆盖（不入库）；
      2. 明文永不进入 workflow / 节点返回值 / HTTP 响应（见 §2.2、§6、§8）；
      3. 相对其它路径也不被 LLM 提示词拼接引用。
    因此本文件的机密性等级 = 「本机同用户可读」，**不防同机其它用户/管理员**。
    用户若需要更强保护，应改用环境变量 ``ANIMA_LLM_API_KEY*`` 或系统级密钥库。
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(view)
        if view.get("_default"):
            payload["api_key"] = view["_default"]
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # 同目录临时文件 + os.replace，避免写一半被读到
        fd, tmp = tempfile.mkstemp(
            prefix=".secrets_", suffix=".tmp", dir=str(DATA_DIR)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, SECRETS_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        _warn(f"写入 secrets.json 失败：{type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# 对外接口：base_url / 注册表查询（§14.1）
# ---------------------------------------------------------------------------


def resolve_base_url(provider, user_base_url=None):
    """解析 OpenAI 兼容 base_url。

    规则：
      * ``custom`` → 只用用户填的值（这也是用户唯一能自定的方式）；
      * 其它 provider → 表内默认值，但**允许用户覆盖**（非空则用用户的）；
      * 未知 provider → 用用户值，无用户值则返回 ``None``。

    :return: 规范化后的 base_url（去掉尾部 ``/``），或 ``None``
    """
    try:
        prov = _norm_provider(provider)
        user = user_base_url.strip() if isinstance(user_base_url, str) else ""

        if user:
            return user.rstrip("/")

        if prov in PROVIDERS:
            default = _base_url_for(prov)
            return default.rstrip("/") if default else None

        _warn(f"未知 provider {provider!r} 且未填 base_url")
        return None
    except Exception as exc:  # noqa: BLE001
        _warn(f"resolve_base_url 异常：{type(exc).__name__}")
        return None


def list_providers() -> list:
    """返回全表（含 id），供 ``/nl2prompt/providers``。

    :return: ``[{"id","name","label","base_url","openai_compat",
                 "default_model","needs_key","verified","note"}, ...]``
             顺序同 :func:`provider_options`（国内常用在前，custom 最后）
    """
    out = []
    try:
        for pid in provider_options():
            meta = _meta(pid)
            out.append(
                {
                    "id": pid,
                    "name": str(meta.get("name") or ""),
                    "label": str(meta.get("label") or meta.get("name") or pid),
                    "base_url": str(meta.get("base_url") or ""),
                    "openai_compat": bool(meta.get("openai_compat", True)),
                    "default_model": str(meta.get("default_model") or ""),
                    "needs_key": bool(meta.get("needs_key", True)),
                    "verified": bool(meta.get("verified", False)),
                    "note": str(meta.get("note") or ""),
                }
            )
    except Exception as exc:  # noqa: BLE001
        _warn(f"list_providers 异常：{type(exc).__name__}")
        return []
    return out


def provider_options() -> list:
    """仅 id 列表，供 INPUT_TYPES 的 COMBO。

    顺序：国内常用在前 → 国际 → 本地 → ``custom`` 最后。
    注册表新增但未列入顺序表的 id 会被追加到 ``custom`` 之前，**绝不丢失**。
    """
    try:
        ordered = [pid for pid in _OPTION_ORDER if pid in PROVIDERS]
        extra = [pid for pid in PROVIDERS if pid not in ordered]
        if "custom" in ordered:
            ordered.remove("custom")
            return ordered + extra + ["custom"]
        return ordered + extra
    except Exception as exc:  # noqa: BLE001
        _warn(f"provider_options 异常：{type(exc).__name__}")
        return list(PROVIDERS.keys())


def default_model(provider) -> str:
    """该供应商默认模型代号；未知或无默认值返回 ``""``。"""
    try:
        val = _meta(provider).get("default_model")
        return val if isinstance(val, str) else ""
    except Exception as exc:  # noqa: BLE001
        _warn(f"default_model 异常：{type(exc).__name__}")
        return ""


def probe(provider, timeout=8) -> dict:
    """对 ``{base_url}/models`` 发一次 **不带 api_key** 的 GET，判定地址可达性。

    判定（§14.1）：
      * ``2xx``            → ``ok=True``（地址与路径均有效）
      * ``401 / 403``      → ``ok=True``（地址正确，仅缺鉴权）
      * ``404``            → ``ok=False, error="http_404_path"``（路径错）
      * ``其它 HTTP 状态`` → ``ok=False, error="http_<status>"``（如实上报，不猜）
      * 连接 / DNS / 超时  → ``ok=False, error="unreachable"``
      * ``base_url`` 为空  → ``ok=False, error="no_base_url"``

    **绝不抛异常，绝不发送 api_key。**

    :param provider: 供应商 id
    :param timeout: 单次请求超时（秒）
    :return: ``{"ok": bool, "status": int | None, "error": str, "elapsed_ms": int}``
    """
    started = time.perf_counter()

    def _done(ok, status=None, error=""):
        return {
            "ok": bool(ok),
            "status": status if isinstance(status, int) else None,
            "error": str(error),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    try:
        base = resolve_base_url(provider)
        if not base:
            return _done(False, None, "no_base_url")

        url = base.rstrip("/") + "/models"
        try:
            tmo = float(timeout)
        except Exception:  # noqa: BLE001
            tmo = PROBE_TIMEOUT
        if not (tmo > 0):
            tmo = PROBE_TIMEOUT

        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _PROBE_UA)
        # 刻意不加 Authorization：只验证地址，不验证鉴权

        try:
            with urllib.request.urlopen(req, timeout=tmo) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                return _done(True, status, "")
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            if status in (401, 403):
                return _done(True, status, "")
            if status == 404:
                return _done(False, status, "http_404_path")
            return _done(False, status, f"http_{status}" if status else "http_error")
        except urllib.error.URLError as exc:
            # DNS / 拒绝连接 / TLS / 代理失败
            reason = getattr(exc, "reason", exc)
            name = type(reason).__name__ if not isinstance(reason, str) else reason
            _warn(f"probe {provider} 网络不可达：{name}")
            return _done(False, None, "unreachable")
        except (TimeoutError, OSError) as exc:
            _warn(f"probe {provider} 超时/IO 错误：{type(exc).__name__}")
            return _done(False, None, "unreachable")
    except Exception as exc:  # noqa: BLE001 - 兜底绝不抛
        _warn(f"probe {provider} 异常：{type(exc).__name__}")
        return _done(False, None, "unreachable")


# ---------------------------------------------------------------------------
# 对外接口：api_key（§13.2 / §13.3，行为与 v1 完全一致）
# ---------------------------------------------------------------------------


def resolve_api_key(provider) -> tuple:
    """按优先级解析 api_key。

    :return: ``(key | None, source)``，``source ∈ {"env", "file", "none"}``
    """
    try:
        prov = _norm_provider(provider)
        # custom / 空 provider → 落 _default 槽位；同时也允许 ANIMA_LLM_API_KEY
        slot = prov if prov and prov not in ("custom",) else "_default"

        # 1) 分供应商环境变量
        if prov and prov != "custom":
            val = os.environ.get(_env_name_for(prov), "").strip()
            if val:
                return val, "env"

        # 2) 全局环境变量
        val = os.environ.get(ENV_GLOBAL, "").strip()
        if val:
            return val, "env"

        # 3) secrets.json
        secrets = _read_secrets()
        for key_slot in (slot, "_default"):
            val = secrets.get(key_slot)
            if isinstance(val, str) and val.strip():
                return val.strip(), "file"

        return None, "none"
    except Exception as exc:  # noqa: BLE001
        _warn(f"resolve_api_key 异常：{type(exc).__name__}")
        return None, "none"


def save_api_key(provider, key) -> bool:
    """把 api_key 写入 ``plugin/data/secrets.json``。

    * ``provider`` 为 ``custom`` 或空 → 写 ``_default``；否则写 ``<provider>``；
    * ``key`` 为空串/None → 删除该条（并清理空文件结构）；
    * 环境变量存在时**仍会写文件**，但 :func:`resolve_api_key` 优先读环境变量，
      此时文件值是"备用值"（会在告警里提示）。

    :return: 是否写入成功
    """
    try:
        prov = _norm_provider(provider)
        slot = prov if prov and prov != "custom" else "_default"

        secrets = _read_secrets()
        removing = key is None or (isinstance(key, str) and not key.strip())
        existed = slot in secrets

        if removing:
            if existed:
                removed = secrets.pop(slot, None)
                # 镜像清理：_default 与刚删除的是同一个 key 时一并删除，
                # 否则"清除"后仍会经 _default 兜底读到旧值。
                if slot != "_default" and removed is not None and secrets.get("_default") == removed:
                    secrets.pop("_default", None)
                _warn(f"已清除 secrets.json 中的 {slot}")
            else:
                _warn(f"secrets.json 中无 {slot}，清除为空操作")
        else:
            secrets[slot] = key.strip()
            # llm_client.py 只认顶层 ``api_key``：同时镜像到 _default，
            # 保证两条读取链路（providers / llm_client）都能拿到最新设置的 key。
            secrets["_default"] = key.strip()

        ok = _write_secrets(secrets)
        if ok and not removing:
            # 只报告"配置项变化"，绝不报告值
            _warn(f"secrets.json 已更新槽位 {slot}（值不回显）")
        return ok
    except Exception as exc:  # noqa: BLE001
        _warn(f"save_api_key 异常：{type(exc).__name__}")
        return False


def mask(key) -> str:
    """日志用的脱敏显示。**恒返回 ``"***"``**，不保留任何前缀/长度信息。"""
    return "***"


def api_key(provider=None):
    """只返回 key 字符串的便捷入口（``None`` = 未配置）。

    为什么存在：``nodes.py`` 的 ``_resolve_api_key`` 会按
    ``resolve_api_key → get_api_key → load_api_key → api_key`` 顺序探测，
    且要求返回 **str**；本函数名正好命中第 4 个候选，因此即使
    :func:`resolve_api_key` 返回 ``(key, source)`` 元组，主链也能拿到完整优先级
    （含分供应商环境变量）的值。
    """
    try:
        key, _source = resolve_api_key(provider)
        return key
    except Exception:  # noqa: BLE001
        return None


def secret_status(provider) -> dict:
    """便捷封装：``{"configured": bool, "source": "env|file|none"}``。

    契约 §6 ``/secret_status`` 直接消费本函数结构；**不含任何值**。
    """
    try:
        key, source = resolve_api_key(provider)
        return {"configured": bool(key), "source": source if key else "none"}
    except Exception:  # noqa: BLE001
        return {"configured": False, "source": "none"}


def provider_names() -> list:
    """返回 provider id 列表（v1 兼容入口；顺序同 :func:`provider_options`）。"""
    return provider_options()


__all__ = [
    "PROVIDERS",
    "PROVIDER_BASE_URLS",
    "KNOWN_PROVIDERS",
    "DATA_DIR",
    "SECRETS_PATH",
    "ENV_GLOBAL",
    "ENV_PREFIX",
    "resolve_base_url",
    "resolve_api_key",
    "api_key",
    "save_api_key",
    "mask",
    "secret_status",
    "provider_names",
    "list_providers",
    "provider_options",
    "default_model",
    "probe",
]
