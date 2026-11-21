# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 GQLI-VC
"""LLM 调用层（backend-core）。

契约：``docs/接口契约.md`` §7（LLM 调用契约）/ §8（错误码）

设计约束
--------
* **只用标准库**（``urllib.request`` / ``json``），不引入任何新依赖。
* 只尝试 **1 次**，绝不重试（契约 §7）。
* 任何失败都以 :class:`LLMError` 抛出，``code`` 使用契约 §8 的错误码，
  ``detail`` 截断到 ≤200 字，由调用方（``nodes.py``）写进 ``debug``。
* 云端**不发送** ``top_k``（OpenAI 兼容协议非标准字段，易 400）；
  本地 llama-server 模式才发送。

本模块不导入 ``store`` / ``runtime`` / ``providers`` 等同级模块，可独立加载。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- 常量

PLUGIN_DIR = Path(__file__).resolve().parent

#: 全局 api_key 环境变量；分供应商变量为 ``ANIMA_LLM_API_KEY_<PROVIDER大写>``
API_KEY_ENV = "ANIMA_LLM_API_KEY"

#: 密钥读写**唯一真源** = ``providers.py``（契约 §13.2）。
#: 本模块**不**自行读写 ``plugin/data/secrets.json``，只在 providers 不可用时
#: 降级为「只读环境变量」。

#: system prompt 中的占位符（契约 §7）
PH_CHARACTERS = "{{character_presets}}"
PH_STYLES = "{{style_presets}}"
PH_SELECTED = "{{selected}}"
_ALL_PLACEHOLDERS = (PH_CHARACTERS, PH_STYLES, PH_SELECTED)

#: 错误 detail 最大长度（契约 §8：``llm_http_<code>`` 的 detail ≤200 字）
MAX_DETAIL_CHARS = 200

#: 契约 §7 默认超时（秒）；``LLM_MODEL.runtime.timeout`` 可覆盖
DEFAULT_TIMEOUT = 30.0


# --------------------------------------------------------------------------- 工具


def _safe_text(text):
    """把文本降级为当前 stdout/stderr 都能编码的字符串（P0 级防护）。

    中文 Windows 下 ComfyUI 的 stderr 是 gbk；把无法 gbk 编码的字符（emoji、
    U+203A 角引号等）写进异常 message 或日志，会抛 ``UnicodeEncodeError``
    进而 ``lost sys.stderr``，打死 ``prompt_worker`` 线程、出图队列永久卡死。
    HTTP 响应片段、异常文本、用户输入都可能携带这类字符，故统一降级。
    """
    raw = text if isinstance(text, str) else str(text)
    for stream in (sys.stderr, sys.stdout):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            raw = raw.encode(encoding, "replace").decode(encoding, "replace")
        except Exception:  # noqa: BLE001 - 未知编码名等，跳过该流继续
            pass
    return raw


def _text(value, limit=MAX_DETAIL_CHARS):
    """把任意值转成去空白、编码安全、并截断的字符串。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return _safe_text(text)


def _as_str(value):
    """宽松取字符串（None → ""）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _clamp(value, low, high, default):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return max(low, min(high, num))


# --------------------------------------------------------------------------- 异常


class LLMError(Exception):
    """LLM 调用失败。

    ``code`` 取契约 §8 的错误码之一:
    ``llm_not_connected`` / ``llm_key_missing`` / ``llm_timeout`` /
    ``llm_bad_json`` / ``llm_http_<code>`` / ``llm_empty_positive`` /
    ``local_model_missing`` / ``runtime_start_failed``。
    """

    def __init__(self, code, detail=""):
        self.code = str(code) if code else "llm_error"
        # detail 会被写进 ComfyUI 的 stderr traceback → 必须先做编码降级
        self.detail = _text(detail)
        message = self.code if not self.detail else f"{self.code}: {self.detail}"
        super().__init__(_safe_text(message))


# --------------------------------------------------------------------------- JSON 解析


def _first_balanced_object(text):
    """从左往右扫描，返回第一个括号平衡的 ``{...}`` 片段（字符串内括号不计）。"""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_json(text):
    """把模型输出解析成 dict。

    1. 先试 ``json.loads``（含对 ```json 代码围栏的剥离）；
    2. 失败则用括号平衡扫描取首个 ``{...}`` 再解析；
    3. 仍失败返回 ``None``（调用方转成 ``llm_bad_json``）。

    契约 §7「解析规则」。
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    if not stripped:
        return None

    # 1) 直接解析
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass

    # 2) 剥掉 markdown 代码围栏后再试
    if stripped.startswith("```"):
        body = stripped
        if body.startswith("```"):
            body = body[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass

    # 3) 括号平衡扫描
    block = _first_balanced_object(stripped)
    if block:
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            return None
    return None


# --------------------------------------------------------------------------- 响应体取内容


def _content_to_text(content):
    """OpenAI ``message.content`` 可能是 str 或 [{type:text,text:...}]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    pieces.append(text["value"])
        return "\n".join(piece for piece in pieces if piece.strip())
    return ""


def extract_content(payload):
    """从 HTTP 响应体里取出模型输出的文本。

    依次兼容：OpenAI ``choices[0].message.content``、
    Anthropic 原生 ``content[]``、以及响应体本身就是目标 JSON 的情况。
    """
    if not isinstance(payload, dict):
        return ""

    # 响应体本身就是 {"positive": ...}（部分本地端点直接透传）
    if isinstance(payload.get("positive"), str):
        return json.dumps(payload, ensure_ascii=False)

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                text = _content_to_text(message.get("content"))
                if text.strip():
                    return text
                # 少数端点把结构化结果放在 message.parsed
                parsed = message.get("parsed")
                if isinstance(parsed, dict):
                    return json.dumps(parsed, ensure_ascii=False)
            if isinstance(first.get("text"), str) and first["text"].strip():
                return first["text"]
            # tool_calls 里塞 JSON 的情况
            tool_calls = first.get("message", {})
            if isinstance(tool_calls, dict):
                args = _first_tool_arguments(tool_calls)
                if args:
                    return args

    # Anthropic 原生：{"content":[{"type":"text","text":"..."}]}
    text = _content_to_text(payload.get("content"))
    if text.strip():
        return text

    # 有些端点直接把结果放 result / output
    for key in ("result", "output", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
    return ""


def _first_tool_arguments(message):
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return ""
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            args = function.get("arguments")
            if isinstance(args, str) and args.strip():
                return args
            if isinstance(args, dict):
                return json.dumps(args, ensure_ascii=False)
    return ""


# --------------------------------------------------------------------------- 请求体


def build_payload(cfg, messages):
    """按契约 §7 组装请求体。

    云端**不发送** ``top_k``；本地（``mode == "local"``）才发送。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    sampling = cfg.get("sampling") if isinstance(cfg.get("sampling"), dict) else {}
    mode = _as_str(cfg.get("mode")).lower() or "local"

    payload = {
        "model": _as_str(cfg.get("model")),
        "messages": messages,
        "stream": False,
    }

    # 刻意**不发送** ``response_format``。
    #
    # OpenAI 标准允许 ``{"type": "json_object"}``，但不少 OpenAI 兼容实现并不认：
    # 新版 LM Studio 只接受 ``text`` / ``json_schema``，发 ``json_object`` 直接回
    #   400 {"error":"'response_format.type' must be 'json_schema' or 'text'"}
    # llama.cpp 的 server 与部分第三方网关也有类似限制。
    #
    # 而 system prompt 里本来就写死了「只输出 JSON：{...}」，``extract_content`` 到
    # ``parse_json_loose`` 那一层也能剥 ```json 代码围栏。所以发这个字段只增加
    # 不兼容面，不增加可靠性。

    temperature = _clamp(sampling.get("temperature", 0.7), 0.0, 2.0, 0.7)
    top_p = _clamp(sampling.get("top_p", 0.9), 0.0, 1.0, 0.9)
    payload["temperature"] = temperature
    payload["top_p"] = top_p

    max_tokens = sampling.get("max_tokens", 1024)
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = 1024
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens

    seed = sampling.get("seed", -1)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    if mode == "local":
        # 本地 llama-server 接受 top_k / seed
        top_k = sampling.get("top_k", 40)
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 40
        if top_k > 0:
            payload["top_k"] = top_k
        if seed >= 0:
            payload["seed"] = seed
    # 云端：top_k / seed 一律不发（契约 §7）

    return payload


def _build_headers(cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    api_key = _as_str(cfg.get("api_key"))
    provider = _as_str(cfg.get("provider")).lower()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if provider == "anthropic":
            # Anthropic 原生要求 x-api-key；同时保留 Authorization 无害
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
    return headers


def chat_url(base_url):
    base = _as_str(base_url).rstrip("/")
    if not base:
        return ""
    return f"{base}/chat/completions"


# --------------------------------------------------------------------------- 核心调用


def chat_json(cfg, messages, timeout=DEFAULT_TIMEOUT):
    """调用 OpenAI 兼容 ``POST {base_url}/chat/completions``，返回解析后的 dict。

    :param cfg: 至少含 ``base_url`` / ``model``；可选 ``provider`` /
        ``api_key`` / ``mode`` / ``sampling``。
    :param messages: ``[{"role": ..., "content": ...}, ...]``
    :param timeout: 秒。
    :returns: ``{"positive": str, "negative": str|None, "debug": dict|None, ...}``
    :raises LLMError: 错误码见契约 §8。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    url = chat_url(cfg.get("base_url"))
    model = _as_str(cfg.get("model"))

    if not url:
        # 本地配置校验错误，不能归到 llm_http_*（那不是 HTTP 故障）
        raise LLMError("cloud_base_url_empty", "云端 base_url 为空，请在「加载LLM」节点填写供应商或 base_url")
    if not model:
        raise LLMError("cloud_model_empty", "云端模型代号为空，请填写 cloud_model")
    if not isinstance(messages, list) or not messages:
        raise LLMError("llm_bad_json", "messages 为空")

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT
    if timeout_value <= 0:
        timeout_value = DEFAULT_TIMEOUT

    payload = build_payload(cfg, messages)
    headers = _build_headers(cfg)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LLMError("llm_bad_json", f"请求体序列化失败：{exc}") from exc

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    raw = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout_value) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 非 200 → llm_http_<code>，detail 带响应片段（≤200 字）
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读取错误体失败不应掩盖原错误
            detail = ""
        if not detail:
            detail = _as_str(getattr(exc, "reason", "")) or f"HTTP {exc.code}"
        raise LLMError(f"llm_http_{exc.code}", detail) from exc
    except TimeoutError as exc:
        raise LLMError("llm_timeout", f"请求超时（{timeout_value:g}s）") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise LLMError("llm_timeout", f"请求超时（{timeout_value:g}s）") from exc
        raise LLMError("llm_http_0", f"无法连接 {url}：{reason}") from exc
    except OSError as exc:
        raise LLMError("llm_http_0", f"网络错误：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 任何未知异常都必须转成契约错误码
        raise LLMError("llm_http_0", f"{type(exc).__name__}: {exc}") from exc

    try:
        envelope = json.loads(raw)
    except (ValueError, TypeError):
        raise LLMError("llm_bad_json", raw) from None

    content = extract_content(envelope)
    if not content.strip():
        raise LLMError("llm_bad_json", raw)

    parsed = parse_json(content)
    if not isinstance(parsed, dict):
        raise LLMError("llm_bad_json", content)

    positive = parsed.get("positive")
    if not isinstance(positive, str) or not positive.strip():
        raise LLMError("llm_empty_positive", content)

    return parsed


# --------------------------------------------------------------------------- 消息组装


def _first_str(source, keys):
    if not isinstance(source, dict):
        return ""
    for key in keys:
        text = _as_str(source.get(key))
        if text:
            return text
    return ""


def _catalog_characters(catalog):
    characters = catalog.get("characters") if isinstance(catalog, dict) else None
    if not isinstance(characters, list) or not characters:
        return "（无）"
    lines = []
    for character in characters:
        if not isinstance(character, dict):
            continue
        name = _first_str(character, ("display_name", "name", "id")) or "(未命名)"
        aliases = [t for t in (_as_str(a) for a in _as_list(character.get("aliases"))) if t]
        outfits = []
        for outfit in _as_list(character.get("outfits")):
            if not isinstance(outfit, dict):
                continue
            outfit_name = _first_str(outfit, ("display_name", "name", "id"))
            if outfit_name:
                outfits.append(outfit_name)
        segments = [f"- {name}"]
        if aliases:
            segments.append("别名：" + "、".join(aliases))
        if outfits:
            segments.append("服装：" + "、".join(outfits))
        lines.append("｜".join(segments))
    return "\n".join(lines) if lines else "（无）"


def _catalog_styles(catalog):
    styles = catalog.get("styles") if isinstance(catalog, dict) else None
    if not isinstance(styles, list) or not styles:
        return "（无）"
    lines = []
    for style in styles:
        if not isinstance(style, dict):
            continue
        name = _first_str(style, ("display_name", "name", "id")) or "(未命名)"
        triggers = [t for t in (_as_str(a) for a in _as_list(style.get("activation_words"))) if t]
        segments = [f"- {name}"]
        if triggers:
            segments.append("触发词：" + "、".join(triggers))
        lines.append("｜".join(segments))
    return "\n".join(lines) if lines else "（无）"


def _selected_text(selected):
    if not isinstance(selected, dict):
        return ""
    lines = []
    for row in _as_list(selected.get("character_rows")):
        if not isinstance(row, dict):
            continue
        name = _as_str(row.get("name"))
        trigger = _as_str(row.get("trigger"))
        hint = _as_str(row.get("outfit_hint"))
        if not (name or trigger or hint):
            continue
        segments = ["角色：" + (name or "(未命名)")]
        if hint:
            segments.append("服装：" + hint)
        if trigger:
            segments.append("激活词：" + trigger)
        lines.append("｜".join(segments))
    for row in _as_list(selected.get("style_rows")):
        if not isinstance(row, dict):
            continue
        name = _as_str(row.get("name"))
        trigger = _as_str(row.get("trigger"))
        if not (name or trigger):
            continue
        segments = ["风格：" + (name or "(未命名)")]
        if trigger:
            segments.append("激活词：" + trigger)
        lines.append("｜".join(segments))
    return "\n".join(lines)


def _render_system_prompt(system_prompt, catalog, selected):
    """填充 ``{{...}}`` 占位符；无占位符时**原文使用**（契约 §7）。"""
    template = system_prompt if isinstance(system_prompt, str) else ""
    if not any(placeholder in template for placeholder in _ALL_PLACEHOLDERS):
        return template
    catalog = catalog if isinstance(catalog, dict) else {}
    selected_text = _selected_text(selected) or "（未选择角色/风格）"
    return (
        template.replace(PH_CHARACTERS, _catalog_characters(catalog))
        .replace(PH_STYLES, _catalog_styles(catalog))
        .replace(PH_SELECTED, selected_text)
    )


def _tokens_of(values):
    """把 ``forbidden_tokens`` 一类的可迭代值归一成字符串列表（保序去重、去空白）。"""
    out = []
    seen = set()
    if values is None or isinstance(values, (str, bytes)):
        if isinstance(values, str):
            values = [values]
        else:
            return out
    try:
        iterator = list(values)
    except TypeError:
        return out
    for item in iterator:
        text = item.strip() if isinstance(item, str) else str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _build_user_content(
    natural_language, draft_positive, draft_negative, selected, forbidden_tokens=()
):
    language = natural_language.strip() if isinstance(natural_language, str) else ""
    blocks = [
        "【自然语言】\n" + (language or "（空）"),
        "【模板草稿 - 正向】\n" + (draft_positive or "（空）"),
        "【模板草稿 - 负向】\n" + (draft_negative or "（空）"),
    ]
    selected_text = _selected_text(selected)
    if selected_text:
        blocks.append("【已选约束】\n" + selected_text)

    # 契约 §14.2（lead 裁决）：把「待压制词清单」显式写进 prompt。
    # 只在非空时追加 —— forbidden_tokens 为空时输出与 v1 **逐字节一致**。
    suppressed = _tokens_of(forbidden_tokens)
    if suppressed:
        blocks.append(
            "【必须出现在 negative 的压制词】\n"
            + ", ".join(suppressed)
            + "\n（这些是同一角色其它服装的区分词，必须全部出现在 negative 里，一个都不能漏）"
        )

    blocks.append(
        "请只输出一个 JSON 对象（不要解释文字、不要 markdown 代码块）："
        '{"positive":"...","negative":"...",'
        '"debug":{"characters":[],"outfits":[],"styles":[],"injected_triggers":[]}}'
    )
    return "\n\n".join(blocks)


def _render_examples(examples, limit=2):
    """把 skill 的 ``examples`` 渲染成 few-shot 文本（最多 ``limit`` 条）。

    契约 §14.2：``skill`` 存在时在 system prompt 末尾追加 ``\\n\\n示例：`` + few-shot。
    """
    if not isinstance(examples, list) or not examples:
        return ""
    blocks = []
    for item in examples:
        if len(blocks) >= limit:
            break
        if not isinstance(item, dict):
            continue
        source = _as_str(item.get("input"))
        positive = _as_str(item.get("positive"))
        negative = _as_str(item.get("negative"))
        if not (source or positive or negative):
            continue
        parts = []
        if source:
            parts.append("输入：" + source)
        if positive:
            parts.append("正向：" + positive)
        if negative:
            parts.append("负向：" + negative)
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _render_skill_prompt(skill, catalog, selected):
    """渲染 ``skill.system_prompt``（占位符生效）并追加 few-shot 示例。"""
    if not isinstance(skill, dict):
        return ""
    template = skill.get("system_prompt")
    if not isinstance(template, str) or not template.strip():
        return ""
    rendered = _render_system_prompt(template, catalog, selected)
    examples = _render_examples(skill.get("examples"))
    if examples:
        rendered = rendered.rstrip() + "\n\n示例：\n" + examples
    return rendered


def build_messages(
    system_prompt,
    natural_language,
    draft_positive,
    draft_negative,
    catalog,
    selected,
    skill=None,
    forbidden_tokens=(),
):
    """组装 ``messages``（契约 §7 / §14.2）。

    * ``{{character_presets}}`` / ``{{style_presets}}`` / ``{{selected}}`` 被填充；
    * 自定义 system prompt 里的同名占位符同样生效；
    * 没有占位符时原文使用；
    * ``skill`` 为 dict 时：追加 skill 的 ``system_prompt``（渲染后）+ 最多 2 条
      few-shot 示例；**用户文本在前，skill 条款在后**（契约 §14.2）；
    * ``forbidden_tokens`` 非空时：在 user content 里显式列出「必须出现在 negative
      的压制词」——LLM 不知道要压什么就会偷懒漏词（实测 R2 覆盖率 0/11）；
    * ``skill=None`` 且 ``forbidden_tokens`` 为空时，输出与 v1 **逐字节一致**。
    """
    user_block = _render_system_prompt(system_prompt, catalog, selected)
    skill_block = _render_skill_prompt(skill, catalog, selected)

    if skill_block:
        # 契约 §14.2：用户可改风格（用户文本在前），skill 约束仍生效（条款在后）
        system_content = "\n\n".join(
            part for part in (user_block, skill_block) if part
        )
    else:
        # 与 v1 完全一致的路径（不做任何额外拼接）
        system_content = user_block

    user_content = _build_user_content(
        natural_language, draft_positive, draft_negative, selected, forbidden_tokens
    )
    messages = []
    if system_content.strip():
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


# --------------------------------------------------------------------------- 模型列表（契约 §14.3）


def _models_url(base_url):
    base = _as_str(base_url).rstrip("/")
    if not base:
        return ""
    return base + "/models"


def _provider_base_url_via_registry(provider):
    """取 provider 的默认 base_url（以 ``providers`` 注册表为单一真源，容错）。

    providers.py 由 providers-curator 并行维护：缺失或函数签名变化时返回 ``""``，
    由调用方报 ``bad_url``，绝不抛异常。
    """
    module = providers_module()
    if module is None:
        return ""

    function = getattr(module, "resolve_base_url", None)
    if callable(function):
        for arguments in ((provider, None), (provider,)):
            try:
                value = function(*arguments)
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                break
            if isinstance(value, str) and value.strip():
                return value.strip()

    table = getattr(module, "PROVIDER_BASE_URLS", None)
    if isinstance(table, dict):
        value = table.get(_as_str(provider).lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def list_models(base_url, provider=None, timeout=10.0):
    """列出 OpenAI 兼容端点的模型（契约 §14.3）。

    ``GET {base_url}/models``，带 ``Authorization: Bearer <key>``；**只请求 1 次**。
    ``base_url`` 为空时按 ``provider`` 从注册表解析。

    :returns: ``{"ok":bool,"models":[{"id":str}],"count":int,"elapsed_ms":float,
        "error":None|str,"detail":str}``
        其中 ``error`` ∈ ``bad_url / no_key / http_<code> / timeout / dns / bad_json``。
        **返回值与 detail 绝不含 api_key。**
    """
    started = time.perf_counter()
    result = {
        "ok": False,
        "models": [],
        "count": 0,
        "elapsed_ms": 0.0,
        "error": None,
        "detail": "",
    }

    def finish(error=None, detail=""):
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        result["count"] = len(result["models"])
        result["error"] = error
        # detail 只可能来自 HTTP 响应体 / 网络原因，绝不含 key
        result["detail"] = _text(detail) if detail else ""
        return result

    url_base = _as_str(base_url).rstrip("/")
    if not url_base:
        url_base = _provider_base_url_via_registry(provider).rstrip("/")
    if not url_base:
        return finish("bad_url", "base_url 为空（请填写或选择支持的 provider）")
    if not url_base.startswith(("http://", "https://")):
        return finish("bad_url", "base_url 必须以 http:// 或 https:// 开头")

    api_key, _source = resolve_api_key(provider)
    if not api_key:
        return finish(
            "no_key",
            "未配置 api_key（请在 LLM 设置中填写，或设置环境变量 ANIMA_LLM_API_KEY）",
        )

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = 10.0
    if timeout_value <= 0:
        timeout_value = 10.0

    request = urllib.request.Request(
        _models_url(url_base),
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="GET",
    )

    raw = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout_value) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读错误体失败不影响错误分类
            detail = ""
        return finish(f"http_{exc.code}", detail or f"HTTP {exc.code}")
    except TimeoutError:
        return finish("timeout", f"请求超时（{timeout_value:g}s）")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return finish("timeout", f"请求超时（{timeout_value:g}s）")
        if isinstance(reason, socket.gaierror):
            return finish("dns", f"域名解析失败：{reason}")
        return finish("dns", f"无法连接 {url_base}：{reason}")
    except socket.gaierror as exc:
        return finish("dns", f"域名解析失败：{exc}")
    except OSError as exc:
        return finish("dns", f"网络错误：{exc}")
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须转成契约错误码
        return finish("dns", f"{type(exc).__name__}: {exc}")

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return finish("bad_json", raw)

    items = None
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        return finish("bad_json", raw)

    models = []
    for item in items:
        if isinstance(item, str):
            identifier = item.strip()
        elif isinstance(item, dict):
            identifier = (
                _as_str(item.get("id"))
                or _as_str(item.get("name"))
                or _as_str(item.get("model"))
            )
        else:
            identifier = ""
        if identifier:
            models.append({"id": identifier})

    result["models"] = models
    result["ok"] = True
    return finish(None, "")


# --------------------------------------------------------------------------- 密钥（转发 providers，契约 §13.2 / §13.3）

_PROVIDERS_MODULE = None
_PROVIDERS_TRIED = False


def providers_module():
    """懒加载同级 ``providers.py``（容错：缺失/损坏返回 ``None``）。"""
    global _PROVIDERS_MODULE, _PROVIDERS_TRIED
    if _PROVIDERS_TRIED:
        return _PROVIDERS_MODULE
    _PROVIDERS_TRIED = True

    package = __package__ or ""
    if package:
        try:
            _PROVIDERS_MODULE = importlib.import_module(".providers", package)
            return _PROVIDERS_MODULE
        except Exception:  # noqa: BLE001 - 缺失即降级
            pass
    try:
        path = PLUGIN_DIR / "providers.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(
                "_anima_nl_providers_for_llm_client", path
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _PROVIDERS_MODULE = module
    except Exception:  # noqa: BLE001 - 损坏的 providers 不得阻止 LLM 调用层加载
        _PROVIDERS_MODULE = None
    return _PROVIDERS_MODULE


def _env_only_api_key(provider=None):
    """降级路径：只读环境变量，返回 ``(key|None, source)``。

    优先级：``ANIMA_LLM_API_KEY_<PROVIDER大写>`` > ``ANIMA_LLM_API_KEY``。
    契约 §13.2 明确 providers 不可用时**不要**自行写文件、不自行读 secrets.json。
    """
    prov = provider.strip().lower() if isinstance(provider, str) else ""
    if prov and prov != "custom":
        name = API_KEY_ENV + "_" + prov.upper().replace("-", "_").replace(" ", "_")
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, "env"
    value = (os.environ.get(API_KEY_ENV) or "").strip()
    if value:
        return value, "env"
    return None, "none"


def resolve_api_key(provider=None):
    """解析 api_key，返回 ``(key | None, source)``，``source ∈ {"env","file","none"}``。

    直接消费 ``providers.resolve_api_key(provider)``（契约 §13.3 的优先级由此保证）：
    ``ANIMA_LLM_API_KEY_<PROVIDER大写>`` > ``ANIMA_LLM_API_KEY`` >
    ``secrets[provider]`` > ``secrets["_default"]``。
    """
    module = providers_module()
    if module is not None:
        function = getattr(module, "resolve_api_key", None)
        if callable(function):
            try:
                result = function(provider)
                if isinstance(result, tuple) and len(result) >= 2:
                    key = result[0]
                    source = result[1]
                    key = key.strip() if isinstance(key, str) else ""
                    if source not in ("env", "file", "none"):
                        source = "file" if key else "none"
                    return (key or None), source
                if isinstance(result, str):
                    key = result.strip()
                    return (key or None), ("file" if key else "none")
            except Exception:  # noqa: BLE001
                pass
    return _env_only_api_key(provider)


def load_api_key(provider=None):
    """便捷入口：只返回 key 字符串（空串 = 未配置）。"""
    key, _source = resolve_api_key(provider)
    return key or ""


def secret_status(provider=None):
    """返回 ``(configured, source)``，``source`` ∈ ``{"env","file","none"}``（不含值）。"""
    module = providers_module()
    if module is not None:
        function = getattr(module, "secret_status", None)
        if callable(function):
            try:
                result = function(provider if provider is not None else "")
                if isinstance(result, dict):
                    source = result.get("source")
                    if source not in ("env", "file", "none"):
                        source = "none"
                    configured = bool(result.get("configured"))
                    return configured, (source if configured else "none")
            except Exception:  # noqa: BLE001
                pass
    key, source = _env_only_api_key(provider)
    if key:
        return True, "env"
    return False, "none"


def save_api_key(provider, key=None):
    """写入 / 清除密钥，**只转发 providers.save_api_key**（契约 §13.2）。

    :param provider: 供应商槽位；空串或 ``custom`` → ``_default`` 槽位。
    :param key: 明文；空串 = 清除该槽位。
    :return: 是否成功。
    :raises LLMError: ``secret_write_failed``（providers 不可用或写入失败）。
    """
    # 向后兼容单参数调用：save_api_key(key)
    if key is None and isinstance(provider, str):
        key = provider
        provider = ""

    module = providers_module()
    if module is None:
        raise LLMError(
            "secret_write_failed",
            "providers.py 不可用；按契约 §13.2 本插件不自行写 secrets.json",
        )

    function = getattr(module, "save_api_key", None)
    if not callable(function):
        raise LLMError("secret_write_failed", "providers.save_api_key 缺失")

    try:
        ok = bool(function(provider, key))
    except Exception as exc:  # noqa: BLE001
        raise LLMError(
            "secret_write_failed", f"{type(exc).__name__}: {exc}"
        ) from exc

    if not ok:
        raise LLMError(
            "secret_write_failed", "providers.save_api_key 返回失败（详见服务端日志）"
        )
    return True

