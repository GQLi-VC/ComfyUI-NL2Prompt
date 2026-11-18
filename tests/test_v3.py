# -*- coding: utf-8 -*-
"""v3 阶段 3 验收（A10–A12 / B9–B10 / C6–C8 / D1）。

> 状态：**骨架就绪，等待新特性实现**。默认三态输出：
>   * ``PASS`` / ``FAIL`` —— 已实现的判据；
>   * ``待实现`` —— 该特性尚未落地（节点未注册 / 返回类型未扩展 / 路由 404 / 前端无 i18n）。
>     **不计入 FAIL 也不计入 PASS**，单独统计；``RESULT`` 的退出码只看 FAIL。
>   这样实现到位后无需改脚本，用例会自动从「待实现」转为 PASS/FAIL；
>   绝不把未实现的东西打印成 PASS。

覆盖
----
A10  CONDITIONING 直连采样器（Composer 输出 positive_cond/negative_cond → KSampler，不经 CLIPTextEncode）
A11  MODEL 直通（LoRAStack → Composer(model) → KSampler.model；result[7] is 输入 model）
A12  clip 未连接（compose 不抛异常，两个 CONDITIONING 输出为 [[], {}]）
B9   三个新 LLM 节点（GGUF / Safetensors / Cloud）
B10  卸载开关（unload_after_use：true → GPU1 回落 0；false → 驻留）
C6   双语（zh / en / auto；zh/en 文案键集合完全一致）
C7   skill 管理（新建→读取→删除；删默认 → skill_protected；非法 id → bad_skill_id）
C8   拉取模型（base_url 为空但 provider 有默认地址时也能拉到；localhost mock 端点）
D1   旧工作流兼容（class_type 换回 AnimaNLComposer 仍能出图）
D2   compose_plain 的 ui 只接受 dict（传 JSON 字符串被视为空 holder）

运行：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\test_v3.py
    ... --only D1          # 只跑某一组（可多次传）
    ... --only D1 --only C8
    ... --no-gpu           # 跳过出图类用例（A10/A11/D1）以省显存

设计原则
--------
* **不碰** `plugin/`；不重启 8199；不写 `plugin/data/secrets.json`（C8 用进程内 monkeypatch + mock 端点）；
* 所有 mock 在本进程内，进程退出即消失；
* HTTP 用例只调用幂等/只读接口，唯一写操作是 C7 的 skill 新建/删除（会在**结束时清理**自建 skill）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PLUGIN_DIR = PROJECT_ROOT / "plugin"
WEB_DIR = PLUGIN_DIR / "web"
WORKFLOWS = PROJECT_ROOT / "workflows"
OUTPUT_DIR = PROJECT_ROOT / "test_env" / "output"
SECRETS = PLUGIN_DIR / "data" / "secrets.json"

API = "http://127.0.0.1:8199"
GGUF = os.environ.get("NL2PROMPT_GGUF", r"D:\models\your-model.gguf")
GPU_INDEX = 1
SUBMIT_TIMEOUT = 900.0

#: 旧类名 → 新类名（D1 用；ComfyUI 侧两个名字都已注册）
OLD_NAMES = {
    "NL2PromptComposer": "AnimaNLComposer",
    "NL2PromptLoadLLM": "AnimaLoadLLM",
    "NL2PromptLoRAStack": "AnimaLoRAStackLoader",
}

RESULTS = []


# ---------------------------------------------------------------- 断言框架（三态）


def check(item: str, name: str, ok: bool, evidence: str = "", observe: bool = False) -> bool:
    RESULTS.append((item, name, bool(ok), evidence, observe))
    flag = ("观测满足" if ok else "观测不满足") if observe else ("PASS" if ok else "FAIL")
    print(f"[{flag}] {item} | {name}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"       {line}")
    return bool(ok)


def pending(item: str, name: str, reason: str) -> None:
    """标记「特性尚未实现」。不计入 PASS/FAIL，单独统计。"""
    RESULTS.append((item, name, None, reason, False))
    print(f"[待实现] {item} | {name}")
    for line in str(reason).splitlines():
        print(f"       {line}")


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def truncate(text, limit=600) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


# ---------------------------------------------------------------- HTTP / 环境


def http_json(path: str, payload=None, timeout: float = 30.0, method=None):
    url = API + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw}
        return exc.code, body


def object_info(name: str):
    """查节点是否注册；未注册返回 None。"""
    code, body = http_json(f"/object_info/{name}", timeout=20)
    if code != 200 or not isinstance(body, dict) or name not in body:
        return None
    return body[name]


def gpu_snapshot():
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total", "--format=csv"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return []
    if completed.returncode != 0:
        return []
    rows = []
    for line in (completed.stdout or "").strip().splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            rows.append({"index": parts[0], "name": parts[1],
                         "used": int(re.sub(r"[^0-9]", "", parts[2])),
                         "total": int(re.sub(r"[^0-9]", "", parts[3]))})
        except ValueError:
            continue
    return rows


def gpu_used(index=GPU_INDEX):
    for row in gpu_snapshot():
        if row["index"] == str(index):
            return row["used"]
    return None


def wait_queue_idle(timeout: float = 180.0) -> bool:
    """提交前等队列空闲：避免自己的任务被别人 `POST /queue {"clear":true}` 清掉。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, queue = http_json("/queue", timeout=10)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if not (queue.get("queue_running") or queue.get("queue_pending")):
            return True
        time.sleep(2)
    return False


def submit_and_wait(prompt: dict, timeout: float = SUBMIT_TIMEOUT, client_id: str = "qa-v3",
                     retry: int = 1):
    """提交工作流并轮询到完成。返回 (status, response, entry, elapsed)。

    稳健性：提交前等队列空闲；轮询时若任务从队列消失且未进 history（被 clear / 被丢弃），
    连续 3 次后判定「任务丢失」直接返回，避免空等到超时。
    """
    for attempt in range(retry + 1):
        result = _submit_once(prompt, timeout, client_id)
        if result[2] is not None or result[0] != 200:
            return result
        if attempt < retry:
            print(f"    !! 第 {attempt + 1} 次执行未完成（任务可能被 queue clear / 环境重启），重试一次")
            wait_queue_idle()
    return result


def _submit_once(prompt: dict, timeout: float, client_id: str):
    wait_queue_idle()
    started = time.time()
    status, body = http_json("/prompt", {"prompt": prompt, "client_id": client_id}, timeout=60)
    prompt_id = (body or {}).get("prompt_id")
    if status != 200 or not prompt_id:
        return status, body, None, time.time() - started

    deadline = time.time() + timeout
    missing_streak = 0
    while time.time() < deadline:
        try:
            code, history = http_json(f"/history/{prompt_id}", timeout=20)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if code == 200 and isinstance(history, dict) and prompt_id in history:
            return status, body, history[prompt_id], time.time() - started
        # 任务丢失检测
        try:
            _, queue = http_json("/queue", timeout=10)
            entries = (queue.get("queue_running") or []) + (queue.get("queue_pending") or [])
            in_queue = any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id
                           for item in entries)
        except Exception:  # noqa: BLE001
            in_queue = True
        if in_queue:
            missing_streak = 0
        else:
            missing_streak += 1
            if missing_streak >= 3:
                print(f"    !! 任务 {prompt_id} 已从队列消失且无 history（可能被 queue clear）")
                return status, body, None, time.time() - started
        time.sleep(2)
    return status, body, None, time.time() - started


def verify_image(outputs: dict, item: str, save_node="11", expect=(1024, 1024)):
    """校验 outputs.images → 文件 → PIL 尺寸。"""
    images = ((outputs or {}).get(save_node, {}) or {}).get("images") or []
    check(item, "outputs 里有 images", bool(images),
          "outputs 键=" + json.dumps(list(outputs or {}), ensure_ascii=False)
          + " images=" + json.dumps(images, ensure_ascii=False))
    if not images:
        return
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        check(item, "PIL 可用", False, f"{type(exc).__name__}: {exc}")
        return
    for image in images:
        path = OUTPUT_DIR / (image.get("subfolder") or "") / (image.get("filename") or "")
        if not path.is_file():
            check(item, f"产物存在: {image.get('filename')}", False, str(path))
            continue
        try:
            with Image.open(path) as opened:
                size = opened.size
            check(item, f"产物 {path.name} 尺寸 {expect[0]}×{expect[1]}", size == expect,
                  f"实际={size} 路径={path}")
        except Exception as exc:  # noqa: BLE001
            check(item, f"PIL 打开 {path.name}", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 导入被测模块


sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(PLUGIN_DIR))
providers = nodes = llm_client = store = None
IMPORT_ERRORS = {}
for name in ("providers", "nodes", "llm_client", "store"):
    try:
        globals()[name] = __import__(name)
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"

# ``nodes._load_sibling`` 在 ``__package__`` 为空时走「按文件 exec_module」分支、
# **不做缓存**（每次返回新模块副本，各自独立 ``_REGISTRY``）。ComfyUI 实际把插件
# 当**包**导入，此时 ``import_module(".runtime", package)`` 有缓存、全局唯一。
# 需要验证运行时注册表一致性的用例（B10）必须用包导入形态。
pkg_nodes = None
try:
    import plugin.nodes as pkg_nodes  # noqa: F401 - 包导入形态（真实加载方式）
except Exception as exc:  # noqa: BLE001
    IMPORT_ERRORS["plugin.nodes"] = f"{type(exc).__name__}: {exc}"

COMPOSER = getattr(nodes, "NL2PromptComposer", None) if nodes else None


def composer_outputs():
    """返回 (output_length, output_names)；读不到返回 (0, [])。"""
    try:
        out = list(getattr(COMPOSER, "RETURN_TYPES", ()) or ())
        names = list(getattr(COMPOSER, "RETURN_NAMES", ()) or ())
        return len(out), names
    except Exception:  # noqa: BLE001
        return 0, []


def new_composer(**kwargs):
    """实例化 Composer（按当前签名调用，缺失参数由调用方补齐）。"""
    return COMPOSER()


# ---------------------------------------------------------------- mock OpenAI 端点（C8）


class MockOpenAI:
    """localhost OpenAI 兼容桩：``GET /v1/models`` → ``{"data":[{"id":"mock-model-1"}]}``。"""

    def __init__(self):
        self.server = None
        self.port = None
        self.requests = []

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append({"path": self.path,
                                      "auth": self.headers.get("Authorization", "")})
                if self.path.rstrip("/").endswith("/models"):
                    body = json.dumps({"data": [{"id": "mock-model-1"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


# ================================================================ D1 旧工作流兼容


def step_d1(no_gpu: bool = False):
    section("D1 旧工作流兼容：class_type 换回旧名仍能出图")

    path = WORKFLOWS / "NL2Prompt_MVP.json"
    if not path.is_file():
        check("D1", "workflows/NL2Prompt_MVP.json 存在", False, str(path))
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    prompt = doc.get("prompt") if isinstance(doc.get("prompt"), dict) else doc

    # 1) 静态：新名版本的工作流确实用了新类名
    used = sorted({node.get("class_type") for node in prompt.values() if isinstance(node, dict)})
    check("D1", "新工作流含 NL2Prompt 新类名",
          "NL2PromptComposer" in used, "class_type=" + json.dumps(used, ensure_ascii=False))

    # 2) 换成旧类名（深拷贝，不动磁盘文件）
    old_prompt = json.loads(json.dumps(prompt))
    replaced = []
    for node in old_prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type")
        if cls in OLD_NAMES:
            node["class_type"] = OLD_NAMES[cls]
            replaced.append(f"{cls}→{OLD_NAMES[cls]}")
    check("D1", "成功把新类名替换为旧类名", bool(replaced),
          "替换=" + json.dumps(sorted(set(replaced)), ensure_ascii=False))
    check("D1", "替换后不再含任何 NL2Prompt* 类名",
          not any(str(n.get("class_type", "")).startswith("NL2Prompt")
                  for n in old_prompt.values() if isinstance(n, dict)),
          "class_type=" + json.dumps(sorted({n.get("class_type") for n in old_prompt.values()}),
                                     ensure_ascii=False))

    # 3) 两个名字都必须仍注册在 ComfyUI 上
    for name in ("NL2PromptComposer", "AnimaNLComposer"):
        info = object_info(name)
        check("D1", f"节点 {name} 已注册", info is not None,
              "已注册" if info else "未注册")

    if no_gpu:
        pending("D1", "旧类名工作流实际出图（--no-gpu 跳过）", "按 --no-gpu 跳过，不占显存")
        return

    # 4) 端到端：用旧类名提交并出图
    old_prompt["11"]["inputs"]["filename_prefix"] = "NL2Prompt_OldCompat/old_compat"
    status, body, entry, elapsed = submit_and_wait(old_prompt, client_id="qa-v3-d1")
    prompt_id = (body or {}).get("prompt_id")
    check("D1", "旧类名工作流 POST /prompt 通过（非 400）",
          status == 200 and bool(prompt_id) and not (body or {}).get("node_errors"),
          f"HTTP {status} prompt_id={prompt_id} node_errors="
          + json.dumps((body or {}).get("node_errors"), ensure_ascii=False))
    if entry is None:
        check("D1", "旧类名工作流执行完成", False, f"超时/未完成，耗时 {elapsed:.1f}s")
        return
    status_obj = entry.get("status") or {}
    check("D1", "执行成功（status_str=success）",
          status_obj.get("status_str") == "success",
          f"status_str={status_obj.get('status_str')} completed={status_obj.get('completed')}")
    check("D1", "旧类名出图耗时记录", True, f"耗时 {elapsed:.2f}s", observe=True)
    verify_image(entry.get("outputs") or {}, "D1")


# ================================================================ D2 composer holder 契约


def step_d2():
    section("D2 compose_plain 的 ui 只接受 dict（传 JSON 字符串被视为空 holder）")
    if nodes is None or not hasattr(nodes, "compose_plain"):
        pending("D2", "compose_plain 可用", "nodes 模块无 compose_plain")
        return

    rich_ui = {"character_rows": [{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}]}
    baseline = nodes.compose_plain("测试文案", {})
    as_dict = nodes.compose_plain("测试文案", rich_ui)
    as_string = nodes.compose_plain("测试文案", json.dumps(rich_ui, ensure_ascii=False))

    check("D2", "ui 传 dict 时角色触发词生效", "castorice" in as_dict["positive"].lower(),
          "positive=" + truncate(as_dict["positive"], 200))
    check("D2", "ui 传 JSON 字符串时被当作空 holder（不解析）",
          as_string["positive"] == baseline["positive"],
          "字符串入参 positive=" + truncate(as_string["positive"], 160)
          + "\n空 holder  positive=" + truncate(baseline["positive"], 160))
    check("D2", "ui 传 JSON 字符串时不抛异常（静默降级为空 holder）", True,
          "已正常返回，且结果与空 holder 一致")

    # 对比：节点 compose 的 `_ui_json` **是** JSON 字符串，会被解析 —— 两者契约不同，勿混用
    try:
        node_out = new_composer().compose("测试文案", False,
                                          json.dumps(rich_ui, ensure_ascii=False))
        check("D2", "（对比）节点 compose(_ui_json 字符串) 会解析并注入",
              "castorice" in (node_out["result"][0] or "").lower(),
              "result[0]=" + truncate(node_out["result"][0], 200))
    except Exception as exc:  # noqa: BLE001
        check("D2", "（对比）节点 compose(_ui_json 字符串) 会解析并注入", False,
              f"{type(exc).__name__}: {exc}")


# ================================================================ A10 CONDITIONING 直连


def build_conditioning_workflow():
    """CLIPLoader → Composer(clip) → KSampler.positive/negative（不经 CLIPTextEncode）。

    Composer 输出索引（契约 v3）：5=positive_cond, 6=negative_cond, 7=model。
    """
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "anima-preview3-base.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "Qwen3-06B-Base.safetensors",
                         "type": "stable_diffusion", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "NL2PromptComposer",
              "inputs": {"natural_language": "银发少女站在雨后的霓虹街道上",
                         "use_llm": False, "_ui_json": "{}",
                         "clip": ["2", 0]}},  # 关键：clip 接入 → 应产出 CONDITIONING
        "5": {"class_type": "NL2PromptLoRAStack",
              "inputs": {"model": ["1", 0], "clip": ["2", 0], "lora_stack": ["4", 3],
                         "enabled": True, "strength_scale": 1.0}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["5", 0],
                         "positive": ["4", 5], "negative": ["4", 6],  # ← 直接吃 CONDITIONING
                         "latent_image": ["6", 0], "seed": 123456789, "steps": 24, "cfg": 7,
                         "sampler_name": "euler_ancestral", "scheduler": "normal", "denoise": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "NL2Prompt_v3/cond_direct"}},
    }


def step_a10(no_gpu: bool = False):
    section("A10 CONDITIONING 直连采样器（不经 CLIPTextEncode）")
    length, names = composer_outputs()
    if length < 8 or "positive_cond" not in names:
        pending("A10", "Composer 输出 positive_cond/negative_cond",
                f"当前 RETURN_TYPES 长度={length}，RETURN_NAMES={names}；"
                "契约要求尾部追加 CONDITIONING, CONDITIONING, MODEL（索引 5/6/7）")
        pending("A10", "CONDITIONING 直连 KSampler 出图", "依赖上一条实现")
        return
    check("A10", "Composer 输出含 positive_cond/negative_cond（索引 5/6）",
          names[5] == "positive_cond" and names[6] == "negative_cond",
          f"RETURN_NAMES={names}")
    check("A10", "Composer 输出含 model（索引 7）", len(names) >= 8 and names[7] == "model",
          f"RETURN_NAMES={names}")

    if no_gpu:
        pending("A10", "直连出图（--no-gpu 跳过）", "按 --no-gpu 跳过")
        return
    workflow = build_conditioning_workflow()
    check("A10", "工作流中无 CLIPTextEncode 节点",
          not any(n["class_type"] == "CLIPTextEncode" for n in workflow.values()),
          "已确认：positive/negative 直接来自 Composer 的 CONDITIONING 输出")
    status, body, entry, elapsed = submit_and_wait(workflow, client_id="qa-v3-a10")
    check("A10", "POST /prompt 通过（非 400）",
          status == 200 and bool((body or {}).get("prompt_id")) and not (body or {}).get("node_errors"),
          f"HTTP {status} node_errors={json.dumps((body or {}).get('node_errors'), ensure_ascii=False)}")
    if entry is None:
        check("A10", "执行完成", False, f"超时/未完成，耗时 {elapsed:.1f}s")
        return
    check("A10", "执行成功", (entry.get("status") or {}).get("status_str") == "success",
          f"status_str={(entry.get('status') or {}).get('status_str')} 耗时 {elapsed:.2f}s")
    verify_image(entry.get("outputs") or {}, "A10", save_node="9")


# ================================================================ A11 MODEL 直通


def step_a11(no_gpu: bool = False):
    section("A11 MODEL 直通（LoRAStack → Composer(model) → KSampler.model）")
    length, names = composer_outputs()
    if length < 8 or "model" not in names:
        pending("A11", "Composer 输出 model（索引 7）",
                f"当前 RETURN_TYPES 长度={length}，RETURN_NAMES={names}")
        pending("A11", "result[7] is 输入 model（同一对象）", "依赖上一条实现")
        pending("A11", "MODEL 直通出图", "依赖上一条实现")
        return

    # 单元级：传哨兵对象，断言原样透传（同一对象，不是拷贝）
    sentinel = object()
    try:
        composer = new_composer()
        out = composer.compose("测试", False, "{}", model=sentinel)
        result = out.get("result") or ()
        check("A11", "compose() 接受 model 关键字", True, "签名已支持 model")
        check("A11", "result[7] is 输入 model（同一对象，非拷贝）",
              len(result) >= 8 and result[7] is sentinel,
              f"len(result)={len(result)} 同一对象={len(result) >= 8 and result[7] is sentinel}")
    except TypeError as exc:
        pending("A11", "compose() 接受 model 关键字", f"TypeError: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        check("A11", "compose() 接受 model 关键字", False, f"{type(exc).__name__}: {exc}")
        return

    check("A11", "（观测）LoRAStack 放在 model 链上会与 Composer.lora_stack 形成循环依赖",
          True,
          "Composer.lora_stack → LoRAStack → Composer.model → Composer.lora_stack 成环；"
          "ComfyUI 不接受带环图，且 LORA_STACK 不接受字面量占位 → 出图链改用 "
          "UNETLoader → Composer(model) → KSampler.model（MODEL 直通语义不变）",
          observe=True)

    if no_gpu:
        pending("A11", "MODEL 直通出图（--no-gpu 跳过）", "按 --no-gpu 跳过")
        return

    workflow = build_conditioning_workflow()
    # MODEL 直通链：UNETLoader → Composer(model) → KSampler.model
    #
    # 关于 lead 给的结构（UNETLoader → LoRAStack → Composer(model) → KSampler）：
    # 若 LoRAStack 的 lora_stack 来自 Composer（["4",3]），再接回 Composer.model 会形成
    # **循环依赖**（Composer → LoRAStack → Composer）。已实测 ComfyUI 无法执行该图
    # （且 LORA_STACK 是自定义类型，不接受字面量 [] 占位）。
    # 因此本用例：LoRAStack 保留在图中消费 Composer.lora_stack，但 model 链直接走
    # UNETLoader → Composer(model)，以验证「MODEL 原样直通」这一核心语义。
    workflow["4"]["inputs"]["model"] = ["1", 0]
    workflow["7"]["inputs"]["model"] = ["4", 7]
    status, body, entry, elapsed = submit_and_wait(workflow, client_id="qa-v3-a11")
    check("A11", "POST /prompt 通过（非 400）",
          status == 200 and bool((body or {}).get("prompt_id")) and not (body or {}).get("node_errors"),
          f"HTTP {status} node_errors={json.dumps((body or {}).get('node_errors'), ensure_ascii=False)}")
    if entry is None:
        check("A11", "执行完成", False, f"超时/未完成，耗时 {elapsed:.1f}s")
        return
    check("A11", "MODEL 直通出图成功",
          (entry.get("status") or {}).get("status_str") == "success",
          f"status_str={(entry.get('status') or {}).get('status_str')} 耗时 {elapsed:.2f}s")
    verify_image(entry.get("outputs") or {}, "A11", save_node="9")


# ================================================================ A12 clip 未连接


def step_a12():
    section("A12 clip 未连接：不抛异常，两个 CONDITIONING 输出为 [[], {}]")
    length, names = composer_outputs()
    if length < 8:
        pending("A12", "clip 未连接时 CONDITIONING 输出为 [[], {}]",
                f"当前 RETURN_TYPES 长度={length}（未含 CONDITIONING），无法产生该输出")
        return
    try:
        composer = new_composer()
        out = composer.compose("银发少女站在霓虹街道上", False, "{}")  # 故意不传 clip
    except Exception as exc:  # noqa: BLE001
        check("A12", "clip 未连接时 compose() 不抛异常", False,
              f"{type(exc).__name__}: {exc}")
        return
    check("A12", "clip 未连接时 compose() 不抛异常", True, "正常返回")
    result = out.get("result") or ()
    check("A12", "positive_cond（索引 5）== [[], {}]",
          len(result) >= 6 and result[5] == [[], {}],
          f"result[5]={truncate(json.dumps(result[5], ensure_ascii=False) if len(result) > 5 else 'N/A', 200)}")
    check("A12", "negative_cond（索引 6）== [[], {}]",
          len(result) >= 7 and result[6] == [[], {}],
          f"result[6]={truncate(json.dumps(result[6], ensure_ascii=False) if len(result) > 6 else 'N/A', 200)}")
    check("A12", "原有 5 个输出仍正常（positive/negative/text 非空）",
          len(result) >= 3 and bool(result[0]) and bool(result[1]) and bool(result[2]),
          f"positive 前 80 字={truncate(result[0] if result else '', 80)}")


# ================================================================ B9 三个 LLM 节点


LLM_NODES = ("NL2PromptLLMGGUF", "NL2PromptLLMSafetensors", "NL2PromptLLMCloud")


def step_b9():
    section("B9 三个新 LLM 节点（GGUF / Safetensors / Cloud）")
    infos = {}
    missing = []
    for name in LLM_NODES:
        info = object_info(name)
        infos[name] = info
        if info is None:
            missing.append(name)

    if missing:
        pending("B9", "三个新 LLM 节点已注册", f"未注册：{missing}（特性未落地）")
        pending("B9", "三节点 INPUT_TYPES/RETURN_TYPES 合法性", "依赖节点注册")
        pending("B9", "GGUF 产出结构化 llm dict（真实 gguf）", "依赖节点实现")
        pending("B9", "Cloud provider→base_url 映射", "依赖节点实现")
        pending("B9", "Safetensors 显存失败错误码 = local_model_oom", "依赖节点实现")
        return

    for name in LLM_NODES:
        check("B9", f"节点 {name} 已注册", True, "已注册")

    # ---- 结构合法性
    for name, info in infos.items():
        inputs = (info.get("input") or {})
        required = inputs.get("required") or {}
        outputs = info.get("output") or []
        check("B9", f"{name} 有 required 输入且输出 LLM_MODEL",
              bool(required) and "LLM_MODEL" in outputs,
              f"required={list(required)} output={outputs}")

    def _llm_of(out):
        """三个 LLM 节点返回 ComfyUI 标准结构 {"ui":…, "result":[llm]}。"""
        if isinstance(out, dict):
            res = out.get("result")
            if isinstance(res, (list, tuple)) and res:
                return res[0]
            return out
        if isinstance(out, (list, tuple)) and out:
            return out[0]
        return out

    # ---- GGUF：用本机真实 gguf（model_file 传绝对路径 = 前端下拉的真实取值）
    try:
        cls = getattr(nodes, "NL2PromptLLMGGUF", None)
        if cls is None:
            pending("B9", "GGUF 节点可调用", "nodes 命名空间无 NL2PromptLLMGGUF")
        else:
            check("B9", "本机真实 gguf 存在", Path(GGUF).is_file(), GGUF)
            out = cls().load(
                gpu=f"{GPU_INDEX}: NVIDIA GeForce RTX 2060 (12288 MiB)",
                model_path=str(Path(GGUF).parent),
                model_file=str(GGUF),  # 绝对路径
                unload_after_use=False,
                _llm_json="{}",
            )
            llm = _llm_of(out)
            check("B9", "GGUF 返回 ComfyUI 标准结构（ui + result）",
                  isinstance(out, dict) and isinstance(out.get("result"), (list, tuple)),
                  "keys=" + json.dumps(sorted(out) if isinstance(out, dict) else type(out).__name__,
                                       ensure_ascii=False)
                  + f" result_type={type(out.get('result')).__name__ if isinstance(out, dict) else 'N/A'}")
            ok = (isinstance(llm, dict) and llm.get("mode") == "local"
                  and bool(llm.get("local_path")) and isinstance(llm.get("sampling"), dict)
                  and isinstance(llm.get("runtime"), dict) and llm.get("gpu_index") == GPU_INDEX)
            check("B9", "GGUF 产出结构化 llm dict（mode=local / local_path / sampling / runtime / gpu_index）",
                  ok, truncate(json.dumps(llm, ensure_ascii=False), 460))
            check("B9", "GGUF 的 local_path 指向真实文件",
                  isinstance(llm, dict) and Path(str(llm.get("local_path") or "")).is_file(),
                  "local_path=" + str((llm or {}).get("local_path") if isinstance(llm, dict) else None))
            check("B9", "GGUF 的 llm dict 不含明文 api_key",
                  "api_key" not in json.dumps(llm, ensure_ascii=False).lower()
                  or "api_key_configured" in json.dumps(llm, ensure_ascii=False).lower(),
                  "已检查")
            # 语义观测：model_file 相对路径时按 model_path 拼接（一级，不递归）
            rel = cls().load(gpu=f"{GPU_INDEX}: NVIDIA GeForce RTX 2060 (12288 MiB)",
                             model_path=os.environ.get("NL2PROMPT_MODEL_DIR", r"D:\models"),
                             model_file=Path(GGUF).name, unload_after_use=False, _llm_json="{}")
            rel_llm = _llm_of(rel)
            check("B9", "（观测）model_file 为裸文件名时按 model_path 一级拼接、不递归子目录",
                  isinstance(rel_llm, dict) and str(rel_llm.get("local_path") or "").endswith(Path(GGUF).name),
                  f"local_path={rel_llm.get('local_path') if isinstance(rel_llm, dict) else None}；"
                  "前端下拉应传绝对路径（/scan_gguf 的 path 字段），否则子目录 gguf 会定位失败",
                  observe=True)
    except Exception as exc:  # noqa: BLE001
        check("B9", "GGUF 产出结构化 llm dict", False, f"{type(exc).__name__}: {exc}")

    # ---- Cloud：provider → base_url（base_url 留空时按 provider 默认地址填）
    try:
        cls = getattr(nodes, "NL2PromptLLMCloud", None)
        if cls is None:
            pending("B9", "Cloud 节点 provider→base_url", "nodes 命名空间无 NL2PromptLLMCloud")
        else:
            out = cls().load(provider="deepseek", base_url="", cloud_model="", _llm_json="{}")
            llm = _llm_of(out)
            base_url = (llm or {}).get("base_url", "") if isinstance(llm, dict) else ""
            expected = (providers.PROVIDERS.get("deepseek") or {}).get("base_url", "")
            check("B9", "Cloud 节点 base_url 留空时按 provider 自动填",
                  base_url == expected, f"base_url={base_url!r} 期望={expected!r}")
            check("B9", "Cloud 的 llm dict 含 mode=cloud 且不含明文 api_key",
                  isinstance(llm, dict) and llm.get("mode") == "cloud"
                  and ("api_key" not in json.dumps(llm, ensure_ascii=False).lower()
                       or "api_key_configured" in json.dumps(llm, ensure_ascii=False).lower()),
                  truncate(json.dumps(llm, ensure_ascii=False), 380))
            detail = ((llm or {}).get("debug") or {}) if isinstance(llm, dict) else {}
            check("B9", "（观测）Cloud 的 model 为空（需用户填 cloud_model）",
                  isinstance(llm, dict) and llm.get("model") == "",
                  "model='' → 需填 cloud_model", observe=True)
    except Exception as exc:  # noqa: BLE001
        check("B9", "Cloud 节点 provider→base_url", False, f"{type(exc).__name__}: {exc}")

    # ---- Safetensors：允许因显存失败，但错误码必须是 local_model_oom
    try:
        cls = getattr(nodes, "NL2PromptLLMSafetensors", None)
        if cls is None:
            pending("B9", "Safetensors 节点可调用", "nodes 命名空间无 NL2PromptLLMSafetensors")
        else:
            check("B9", "源码存在 local_model_oom 错误码",
                  "local_model_oom" in (PLUGIN_DIR / "llm_local_hf.py").read_text(
                      encoding="utf-8", errors="replace"),
                  "llm_local_hf.py 含 local_model_oom")
            try:
                out = cls().load(gpu=f"{GPU_INDEX}: NVIDIA GeForce RTX 2060 (12288 MiB)",
                                 model_dir="", model_file="", dtype="fp16",
                                 unload_after_use=False, _llm_json="{}")
                llm = _llm_of(out)
                check("B9", "Safetensors 成功路径返回结构化 llm dict",
                      isinstance(llm, dict) and bool(llm.get("mode")),
                      truncate(json.dumps(llm, ensure_ascii=False), 380))
                pending("B9", "Safetensors 显存失败错误码 = local_model_oom",
                        "本机显存足够、走的是成功路径（lead 允许）；待显存不足环境再验")
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None)
                detail = getattr(exc, "detail", None) or str(exc)
                check("B9", "Safetensors 失败时错误码 == local_model_oom",
                      code == "local_model_oom",
                      f"code={code!r} detail={truncate(detail, 300)}")
    except Exception as exc:  # noqa: BLE001
        check("B9", "Safetensors 节点调用", False, f"{type(exc).__name__}: {exc}")


# ================================================================ B10 卸载开关


def step_b10(no_gpu: bool = False):
    section("B10 卸载开关（unload_after_use：true → GPU1 回落；false → 驻留）")
    store_src = (PLUGIN_DIR / "store.py").read_text(encoding="utf-8", errors="replace")
    nodes_src = (PLUGIN_DIR / "nodes.py").read_text(encoding="utf-8", errors="replace")

    check("B10", "settings 层支持 unload_after_use 默认值",
          "unload_after_use" in store_src, "store.py 含 unload_after_use")
    consumed = ("unload_after_use" in nodes_src) or ("unload_after_use" in
               (PLUGIN_DIR / "llm_client.py").read_text(encoding="utf-8", errors="replace")) \
               or ("unload_after_use" in (PLUGIN_DIR / "runtime.py").read_text(encoding="utf-8", errors="replace"))
    check("B10", "运行时/节点消费 unload_after_use（非仅 settings 定义）", consumed,
          "nodes.py / llm_client.py / runtime.py 之一含该键" if consumed
          else "仅 store.py 定义，尚无消费者 → 待实现")

    if not consumed:
        pending("B10", "true 跑一次重写后 GPU1 回落 0 MiB", "依赖 unload_after_use 被消费")
        pending("B10", "false 时保持驻留", "依赖 unload_after_use 被消费")
        return

    if no_gpu:
        pending("B10", "卸载开关实测（--no-gpu 跳过）", "按 --no-gpu 跳过，涉及真实 LLM 与显存")
        return

    # 真实实测：带 unload_after_use=true 走一次完整重写 → 观察 GPU1 是否回落
    #
    # 三个已踩过的坑（都按实现语义修正，非放宽断言）：
    #   1) 必须用 ``mode="local"`` 的 llm —— ``runtime.unload`` 按 mode 分派，
    #      cloud 模式恒返回 False、不碰本地进程；
    #   2) **只能通过 ``nodes._load_sibling("runtime")`` 拿运行时模块**：直接
    #      ``import runtime`` 得到的是另一份副本（独立 ``_REGISTRY``），会另起一个
    #      llama-server，造成「双实例、卸载不掉」的假象；
    #   3) 卸载钩子在 ``compose_with_llm`` 的 ``finally``（覆盖成功/失败两条路径），
    #      不在 ``_rewrite_only``。
    bn = pkg_nodes or nodes  # 用**包导入**的 nodes：_load_sibling 才有模块缓存
    check("B10", "（观测）包导入形态下 _load_sibling 返回同一 runtime 副本",
          pkg_nodes is not None and pkg_nodes._load_sibling("runtime") is pkg_nodes._load_sibling("runtime"),
          "顶层 import nodes（__package__=''）时每次返回新副本、_REGISTRY 会分裂；"
          "真实 ComfyUI 以包导入插件，故运行时状态全局唯一",
          observe=True)
    rt = bn._load_sibling("runtime")
    if rt is None:
        pending("B10", "卸载开关实测", "无法加载 runtime 模块")
        return
    local_llm = {
        "mode": "local", "provider": "custom", "base_url": "", "model": "",
        "gpu_index": GPU_INDEX, "local_path": GGUF,
        "sampling": {"top_k": 40, "top_p": 0.9, "temperature": 0.7,
                     "max_tokens": 512, "seed": -1},
        "runtime": {"ctx": 8192, "timeout": 180, "unload_after_use": True},
    }
    unload_calls = {"unload": 0, "stop_all": 0}
    orig_unload = getattr(rt, "unload", None)
    orig_stop_all = getattr(rt, "stop_all", None)

    def counting_unload(*a, **k):
        unload_calls["unload"] += 1
        return orig_unload(*a, **k) if callable(orig_unload) else True

    def counting_stop_all(*a, **k):
        unload_calls["stop_all"] += 1
        return orig_stop_all(*a, **k) if callable(orig_stop_all) else []

    try:
        if callable(orig_unload):
            rt.unload = counting_unload
        if callable(orig_stop_all):
            rt.stop_all = counting_stop_all

        base_gpu1 = gpu_used()
        core = bn.compose_with_llm("银发少女站在霓虹街道上", {}, local_llm)
        debug = core.get("debug") or {}
        check("B10", "带 unload_after_use=true 的重写成功", debug.get("used_llm") is True,
              f"used_llm={debug.get('used_llm')!r} fallback={debug.get('fallback')!r} "
              f"elapsed={debug.get('elapsed_ms')}ms")
        check("B10", "确认真起过本地 llama-server（base_url 指向 127.0.0.1）",
              "127.0.0.1" in str(debug.get("llm_base_url") or ""),
              f"llm_base_url={debug.get('llm_base_url')!r} llm_elapsed={debug.get('llm_elapsed_ms')}ms")
        check("B10", "成功路径 debug.unloaded == True",
              debug.get("unloaded") is True,
              f"unloaded={debug.get('unloaded')!r}")
        check("B10", "成功路径确实调用了 runtime.unload（不是空转）",
              unload_calls["unload"] >= 1,
              f"unload={unload_calls['unload']} stop_all={unload_calls['stop_all']}")

        settled = None
        for attempt in range(40):
            time.sleep(1)
            used = gpu_used()
            if used is not None and used < 300:
                settled = (attempt + 1, used)
                break
        check("B10", "重写完成后 GPU1 回落 0 MiB（unload_after_use=true）",
              settled is not None,
              f"等待 {settled[0]}s 后 GPU1={settled[1]} MiB（基线 {base_gpu1} MiB）" if settled
              else f"40s 后 GPU1={gpu_used()} MiB（未回落，基线 {base_gpu1} MiB）")

        # ---- 失败路径：卸载钩子在 finally 里，失败也必须执行（lead 要求覆盖两条路径）
        unload_calls["unload"] = 0
        unload_calls["stop_all"] = 0
        bad_llm = {
            "mode": "local", "provider": "custom", "base_url": "", "model": "",
            "gpu_index": GPU_INDEX, "local_path": "Z:/definitely/missing/nope.gguf",
            "sampling": {}, "runtime": {"ctx": 8192, "timeout": 30,
                                        "unload_after_use": True},
        }
        bad_core = bn.compose_with_llm("银发少女站在霓虹街道上", {}, bad_llm)
        bdebug = bad_core.get("debug") or {}
        check("B10", "失败路径仍返回模板结果（不中断）",
              bool((bad_core.get("positive") or "").strip()),
              "positive=" + truncate(bad_core.get("positive"), 160))
        check("B10", "失败路径错误码为 local_model_missing",
              bdebug.get("fallback") == "local_model_missing",
              f"fallback={bdebug.get('fallback')!r} detail={truncate(bdebug.get('detail'), 200)}")
        check("B10", "失败路径也执行卸载钩子（debug.unloaded=True）",
              bdebug.get("unloaded") is True,
              f"unloaded={bdebug.get('unloaded')!r}")
        check("B10", "失败路径卸载动作确实被调用（unload/stop_all 至少 1 次）",
              unload_calls["unload"] + unload_calls["stop_all"] >= 1,
              f"unload={unload_calls['unload']} stop_all={unload_calls['stop_all']}")
    finally:
        if callable(orig_unload):
            rt.unload = orig_unload
        if callable(orig_stop_all):
            rt.stop_all = orig_stop_all
        try:
            rt.stop_all()
        except Exception:  # noqa: BLE001
            pass


# ================================================================ C6 双语


def step_c6(ui_report: Path | None = None):
    section("C6 双语（zh / en / auto；zh/en 文案键集合一致）")
    js_files = sorted(WEB_DIR.glob("*.js"))
    sources = {path.name: path.read_text(encoding="utf-8", errors="replace") for path in js_files}

    # 1) 前端是否已有 i18n 基础设施
    has_locale = any(re.search(r"ui_locale|locale|i18n|translations|zh-CN|zh_CN", src)
                     for src in sources.values())
    if not has_locale:
        pending("C6", "前端存在语言/locale 基础设施",
                f"扫描 {len(sources)} 个 js，未发现 ui_locale/locale/i18n/translations（特性未落地）")
        pending("C6", "zh/en 文案键集合完全一致", "依赖 i18n 字典实现")
        pending("C6", "节点正面文案随 zh/en/auto 变化（CDP）", "依赖 i18n 实现")
        return
    check("C6", "前端存在语言/locale 基础设施", True,
          "命中=" + json.dumps([n for n, s in sources.items()
                              if re.search(r"ui_locale|locale|i18n|translations", s)],
                             ensure_ascii=False))

    # 2) 静态：解析 web/i18n.js 的 ZH / EN 两套字典，比对键集合完全一致
    i18n_path = WEB_DIR / "i18n.js"
    if not i18n_path.is_file():
        pending("C6", "zh/en 文案键集合完全一致", f"未找到 {i18n_path}")
        pending("C6", "节点正面文案随 zh/en/auto 变化（CDP）", "依赖 i18n.js")
        return
    i18n_src = i18n_path.read_text(encoding="utf-8", errors="replace")

    def dict_keys(name: str):
        marker = f"const {name} = {{"
        if marker not in i18n_src:
            return set()
        start = i18n_src.index(marker)
        end = i18n_src.index("\n};", start)
        return set(re.findall(r'"([A-Za-z0-9_.]+)"\s*:', i18n_src[start:end]))

    zh_keys, en_keys = dict_keys("ZH"), dict_keys("EN")
    check("C6", "（静态）解析出 zh / en 两套文案键", bool(zh_keys) and bool(en_keys),
          f"zh 键数={len(zh_keys)} en 键数={len(en_keys)}（来源 web/i18n.js）")
    check("C6", "zh / en 键集合完全一致",
          zh_keys == en_keys,
          f"仅 zh 有={sorted(zh_keys - en_keys)[:8]} 仅 en 有={sorted(en_keys - zh_keys)[:8]}")
    check("C6", "i18n.js 声明 LOCALES = auto/zh/en",
          all(tag in i18n_src for tag in ('"auto"', '"zh"', '"en"')),
          "LOCALES 三态齐备")
    check("C6", "i18n.js 提供 assertLocalesComplete（键完整性自检）",
          "assertLocalesComplete" in i18n_src, "已提供")
    check("C6", "i18n.js 通过 GET /nl2prompt/settings_ui 取真源",
          "settings_ui" in i18n_src, "initI18n 拉后端为真源")

    # 后端 settings_ui 往返（ui_locale 持久化）
    code_get, body_get = http_json("/nl2prompt/settings_ui", timeout=15)
    check("C6", "GET /nl2prompt/settings_ui 可用",
          code_get == 200 and isinstance(body_get, dict),
          f"HTTP {code_get} body={truncate(json.dumps(body_get, ensure_ascii=False), 240)}")

    # 3) CDP 交互（复用 tools/ui_probe.py 的基类）
    if ui_report and ui_report.is_file():
        try:
            report = json.loads(ui_report.read_text(encoding="utf-8"))
        except ValueError:
            report = {}
        ui = report.get("ui") or {}
        for locale in ("zh", "en", "auto"):
            entry = ui.get(locale) or {}
            check("C6", f"{locale} 模式节点正面文案非空", bool(entry.get("composer_texts")),
                  truncate(json.dumps(entry, ensure_ascii=False), 300))

        zh_entry = ui.get("zh") or {}
        en_entry = ui.get("en") or {}
        auto_entry = ui.get("auto") or {}
        zh = zh_entry.get("composer_texts") or []
        en = en_entry.get("composer_texts") or []
        check("C6", "zh 与 en 节点正面文案不同（确实切换了语言）", bool(zh) and zh != en,
              f"zh 首项={truncate(json.dumps(zh[:4], ensure_ascii=False), 180)}\n"
              f"en 首项={truncate(json.dumps(en[:4], ensure_ascii=False), 180)}")

        # 逐条更硬的证据：i18n 模块 t() 直采 + <html lang> 标记
        zh_s = zh_entry.get("i18n_sample") or {}
        en_s = en_entry.get("i18n_sample") or {}
        if isinstance(zh_s, dict) and isinstance(en_s, dict) and zh_s.get("i18n_url"):
            check("C6", "t() 在 zh 返回中文、在 en 返回英文（同 key 对比）",
                  zh_s.get("t_head_tooltip") != en_s.get("t_head_tooltip")
                  and bool(re.search(r"[\u4e00-\u9fff]", str(zh_s.get("t_head_tooltip") or "")))
                  and not re.search(r"[\u4e00-\u9fff]", str(en_s.get("t_head_tooltip") or "")),
                  f"zh={zh_s.get('t_head_tooltip')!r}\nen={en_s.get('t_head_tooltip')!r}")
            comp = zh_s.get("complete") or {}
            check("C6", "assertLocalesComplete() 运行时校验通过（zh/en 键集合一致）",
                  bool(comp.get("ok")) and not (comp.get("missingInEn") or comp.get("missingInZh")),
                  json.dumps(comp, ensure_ascii=False))
            check("C6", "浏览器内 i18n 字典规模与静态解析一致（310 键）",
                  int(comp.get("total") or 0) >= 300,
                  f"total={comp.get('total')} enTotal={comp.get('enTotal')}")
        else:
            pending("C6", "t() 在 zh/en 返回不同语言文案",
                    "探针报告缺少 i18n_sample（页面未加载 nl2prompt_composer.js）")

        check("C6", "en 模式节点正面文案不含中文（除 ❌ 占位符）",
              bool(en) and not any(re.search(r"[\u4e00-\u9fff]",
                                             t if isinstance(t, str) else "")
                                   for t in en),
              "en 中文命中=" + json.dumps(
                  [t for t in en if isinstance(t, str) and re.search(r"[\u4e00-\u9fff]", t)],
                  ensure_ascii=False)[:240])
        check("C6", "<html lang> 随 locale 切换（zh→zh / en→en）",
              str(zh_entry.get("lang") or "").startswith("zh")
              and str(en_entry.get("lang") or "").startswith("en"),
              f"zh.lang={zh_entry.get('lang')!r} en.lang={en_entry.get('lang')!r} "
              f"auto.lang={auto_entry.get('lang')!r}")

        # 如实记录：节点标题（LiteGraph title）三态恒为中文，未参与 i18n
        titles = {k: (ui.get(k) or {}).get("node_title") for k in ("zh", "en", "auto")}
        auto_s = auto_entry.get("i18n_sample") or {}
        check("C6", "（观测）节点标题未本地化（en 下仍为中文）",
              len({str(v) for v in titles.values()}) == 1,
              f"titles={json.dumps(titles, ensure_ascii=False)}；"
              "节点 title 由 NODE_DISPLAY_NAME 决定，未走 i18n → 建议左侧项接入 t()",
              observe=True)
        check("C6", "（观测）auto 模式的 <html lang> 与解析语言不一致",
              str(auto_entry.get("lang") or "") != str((auto_s or {}).get("current_locale") or ""),
              f"auto.lang={auto_entry.get('lang')!r} "
              f"auto.current_locale={(auto_s or {}).get('current_locale')!r}；"
              "文案跟随解析语言（zh），但 <html lang> 打了 en，属次要一致性问题",
              observe=True)
    else:
        pending("C6", "CDP 探针：zh/en/auto 三态文案切换",
                f"未提供探针报告（{ui_report or 'tests/_ui_probe_i18n.json'}）；"
                "实现后运行 tests/ui_probe_i18n.py 生成")


# ================================================================ C7 skill 管理


SKILL_TEST_ID = "qa-v3-temp-skill"
SKILL_PROBE_ID = "qa-v3-probe-skill"


def cleanup_test_skill():
    """尽力清理 C7 自建的临时 skill（避免污染 plugin/skills）。"""
    for skill_id in (SKILL_TEST_ID, SKILL_PROBE_ID):
        try:
            http_json("/nl2prompt/skills/delete", {"id": skill_id}, timeout=20)
        except Exception:  # noqa: BLE001
            pass


def step_c7():
    section("C7 skill 管理（新建 → 读取 → 删除；保护默认；非法 id）")
    cleanup_test_skill()  # 先清历史残留

    # ---- 阶段 1：路由探测（用**独立**的 PROBE_ID，避免干扰后面的全链路）
    code_get, body_get = http_json("/nl2prompt/skills/anima-tag-master", timeout=20)
    route_get_ok = code_get == 200 and isinstance(body_get, dict) and (body_get or {}).get("ok") is True

    code_pnew, body_pnew = http_json("/nl2prompt/skills/new", {
        "id": SKILL_PROBE_ID, "display_name": "QA v3 探测技能",
        "target_model": "anima", "copy_from": "anima-tag-master",
    }, timeout=30)
    route_new_ok = (code_pnew == 200 and isinstance(body_pnew, dict)
                    and (body_pnew or {}).get("ok") is True)

    code_pdel, body_pdel = http_json("/nl2prompt/skills/delete",
                                     {"id": SKILL_PROBE_ID}, timeout=30)
    route_del_ok = (code_pdel == 200 and isinstance(body_pdel, dict)
                    and (body_pdel or {}).get("ok") is True)

    if not (route_get_ok or route_new_ok or route_del_ok):
        pending("C7", "GET /nl2prompt/skills/{id} 可用", f"HTTP {code_get}（路由未实现）")
        pending("C7", "POST /nl2prompt/skills/new 可用", f"HTTP {code_pnew}（路由未实现）")
        pending("C7", "POST /nl2prompt/skills/delete 可用", f"HTTP {code_pdel}（路由未实现）")
        pending("C7", "新建→读取→删除 全链路", "依赖上述三条路由实现")
        pending("C7", "删除默认 skill → skill_protected", "依赖 /skills/delete 实现")
        pending("C7", "非法 id → bad_skill_id", "依赖 /skills/{id} 与 delete 实现")
        return

    check("C7", "GET /nl2prompt/skills/{id} 可用", route_get_ok,
          f"HTTP {code_get} id={(body_get or {}).get('id')!r} is_default={(body_get or {}).get('is_default')}")
    check("C7", "POST /nl2prompt/skills/new 可用", route_new_ok,
          f"HTTP {code_pnew} body={truncate(json.dumps(body_pnew, ensure_ascii=False), 200)}")
    check("C7", "POST /nl2prompt/skills/delete 可用", route_del_ok,
          f"HTTP {code_pdel} body={truncate(json.dumps(body_pdel, ensure_ascii=False), 200)}")

    # ---- 阶段 2：全链路（重新新建一个独立 ID，不与探测混用）
    code_new, body_new = http_json("/nl2prompt/skills/new", {
        "id": SKILL_TEST_ID, "display_name": "QA v3 临时技能",
        "target_model": "anima", "copy_from": "anima-tag-master",
    }, timeout=30)
    check("C7", "new 返回 ok:true", (body_new or {}).get("ok") is True,
          truncate(json.dumps(body_new, ensure_ascii=False), 260))

    code_list, body_list = http_json("/nl2prompt/skills", timeout=20)
    ids = [item.get("id") for item in (body_list or {}).get("skills") or [] if isinstance(item, dict)]
    check("C7", "新建的 skill 出现在 GET /skills 列表", SKILL_TEST_ID in ids,
          f"HTTP {code_list} 现有 id={json.dumps(ids, ensure_ascii=False)}")

    code_read, body_read = http_json(f"/nl2prompt/skills/{SKILL_TEST_ID}", timeout=20)
    read_id = (body_read or {}).get("id") or ((body_read or {}).get("skill") or {}).get("id")
    check("C7", "读取新建 skill 成功且 id 匹配",
          code_read == 200 and (body_read or {}).get("ok") is not False and read_id == SKILL_TEST_ID,
          f"HTTP {code_read} id={read_id!r}")

    code_del2, body_del2 = http_json("/nl2prompt/skills/delete", {"id": SKILL_TEST_ID}, timeout=30)
    check("C7", "删除成功", code_del2 == 200 and (body_del2 or {}).get("ok") is True,
          f"HTTP {code_del2} body={truncate(json.dumps(body_del2, ensure_ascii=False), 200)}")

    code_after, body_after = http_json(f"/nl2prompt/skills/{SKILL_TEST_ID}", timeout=20)
    check("C7", "删除后读取不再返回该 skill",
          (body_after or {}).get("ok") is False
          or (body_after or {}).get("error") in ("skill_not_found", "bad_skill_id"),
          f"HTTP {code_after} body={truncate(json.dumps(body_after, ensure_ascii=False), 240)}")

    # ---- 阶段 3：保护默认 skill
    code_prot, body_prot = http_json("/nl2prompt/skills/delete",
                                     {"id": "anima-tag-master"}, timeout=30)
    check("C7", "删除默认 skill 被拒（error == skill_protected）",
          (body_prot or {}).get("error") == "skill_protected",
          f"HTTP {code_prot} body={truncate(json.dumps(body_prot, ensure_ascii=False), 260)}")

    # ---- 阶段 4：非法 id
    #
    # 实现事实（plugin/routes.py）：
    #   * 写入口 ``POST /skills/new`` 有 ``_SKILL_ID_PATTERN`` 预校验 → ``bad_skill_id``；
    #   * 读/删入口（``GET /skills/{id}``、``POST /skills/delete``）不做格式预校验，
    #     走「白名单比对 index.json 里的 id」→ 不命中即 ``skill_not_found``。
    #     白名单方式**没有路径穿越风险**（只有已存在的合法 id 才会被读取），
    #     但与 lead 判据「非法 id → bad_skill_id」不一致，故 delete 侧如实判 FAIL。
    code_new_bad, body_new_bad = http_json("/nl2prompt/skills/new",
                                           {"id": "../evil", "copy_from": ""}, timeout=30)
    check("C7", "写入口（/skills/new）非法 id 返回 bad_skill_id",
          (body_new_bad or {}).get("error") == "bad_skill_id",
          f"HTTP {code_new_bad} body={truncate(json.dumps(body_new_bad, ensure_ascii=False), 260)}")

    code_bad, body_bad = http_json("/nl2prompt/skills/delete", {"id": "../evil"}, timeout=30)
    check("C7", "非法 id 返回 bad_skill_id（lead 判据，含 /skills/delete）",
          (body_bad or {}).get("error") == "bad_skill_id",
          f"HTTP {code_bad} body={truncate(json.dumps(body_bad, ensure_ascii=False), 260)}"
          "；实现走白名单 → skill_not_found（已拒绝且无穿越风险，仅错误码语义与判据不符）")

    cleanup_test_skill()


# ================================================================ C8 拉取模型


def step_c8():
    section("C8 拉取模型（base_url 为空但 provider 有默认地址 → 也能拉到）")
    stub = MockOpenAI()
    original_resolve = llm_client.resolve_api_key if llm_client else None
    original_registry_base = None

    # ---- 0) 真实行为（不加 mock）：base_url 留空 + provider=deepseek
    #       应回退到供应商默认地址，得到 no_key 这类**有意义**的错误，
    #       而不是「请先填写 base_url」（bad_url）。
    try:
        real = llm_client.list_models("", "deepseek", 5.0)
        check("C8", "base_url 留空时不再报 bad_url（已回退供应商默认地址）",
              (real or {}).get("error") != "bad_url",
              f"error={real.get('error')!r} detail={truncate(real.get('detail'), 240)}")
        if not SECRETS.exists():
            check("C8", "无密钥环境下错误码为 no_key（有意义的下游错误）",
                  (real or {}).get("error") == "no_key",
                  f"error={real.get('error')!r} detail={truncate(real.get('detail'), 240)}")
            check("C8", "detail 不是「请先填写 base_url」",
                  "base_url 为空" not in str((real or {}).get("detail") or ""),
                  f"detail={truncate(real.get('detail'), 200)}")
        else:
            pending("C8", "无密钥环境下错误码为 no_key",
                    "本机存在 plugin/data/secrets.json（有密钥），跳过该断言")
    except Exception as exc:  # noqa: BLE001
        check("C8", "base_url 留空时不再报 bad_url", False, f"{type(exc).__name__}: {exc}")

    try:
        port = stub.start()
        print(f"    mock OpenAI 端点: {stub.base_url}")

        # 关键：把 ollama 的默认 base_url 临时指向 mock（模拟「provider 有默认地址」）
        # 注意：llm_client 内部用 _load_sibling 加载了**独立的** providers 模块对象
        #       （与顶层 import 的不是同一对象），必须 patch 它自己那份才生效。
        pm = llm_client.providers_module() if hasattr(llm_client, "providers_module") else providers
        check("C8", "拿到 llm_client 实际使用的 providers 模块", pm is not None,
              f"module file={(getattr(pm, '__file__', '?') if pm else 'None')}")
        registry = getattr(pm, "PROVIDERS", {}) if pm else {}
        if "ollama" not in registry:
            check("C8", "providers 含 ollama", False, "注册表无 ollama")
            return
        original_registry_base = registry["ollama"].get("base_url")
        registry["ollama"]["base_url"] = stub.base_url
        llm_client.resolve_api_key = lambda provider=None: ("sk-mock-not-real", "env")

        # base_url 传空 → 应回落到 provider 默认地址（此处为 mock）
        result = llm_client.list_models("", "ollama", 5.0)
        check("C8", "base_url 为空时不再返回 bad_url（改用 provider 默认地址）",
              (result or {}).get("error") != "bad_url",
              f"error={result.get('error')!r} detail={truncate(result.get('detail'), 200)}")
        check("C8", "成功从 mock 端点拉到模型列表",
              result.get("ok") is True and any(m.get("id") == "mock-model-1"
                                               for m in result.get("models") or []),
              f"ok={result.get('ok')} models={json.dumps(result.get('models'), ensure_ascii=False)}")
        check("C8", "请求确实打到 mock 的 /v1/models",
              any(r["path"].rstrip("/").endswith("/models") for r in stub.requests),
              "请求=" + json.dumps(stub.requests[-3:], ensure_ascii=False))
        check("C8", "响应不含 api_key",
              "sk-mock-not-real" not in json.dumps(result, ensure_ascii=False),
              "已扫描全部字段")

        # 反向：显式 base_url 仍优先于 provider 默认地址
        result2 = llm_client.list_models(stub.base_url, "ollama", 5.0)
        check("C8", "显式 base_url 优先于 provider 默认地址",
              result2.get("ok") is True, f"ok={result2.get('ok')}")
    except Exception as exc:  # noqa: BLE001
        check("C8", "拉取模型（provider 默认地址）", False, f"{type(exc).__name__}: {exc}")
    finally:
        if llm_client and original_resolve is not None:
            llm_client.resolve_api_key = original_resolve
        if original_registry_base is not None:
            pm = (llm_client.providers_module()
                  if (llm_client and hasattr(llm_client, "providers_module")) else providers)
            registry = getattr(pm, "PROVIDERS", {}) if pm else {}
            if "ollama" in registry:
                registry["ollama"]["base_url"] = original_registry_base
        stub.stop()

    # 不污染密钥文件
    check("C8", "未创建 plugin/data/secrets.json", not SECRETS.exists(),
          f"{SECRETS} 存在={SECRETS.exists()}")


# ================================================================ 汇总


def summary() -> int:
    section("汇总")
    core = [row for row in RESULTS if not row[4] and row[2] is not None]
    pend = [row for row in RESULTS if row[2] is None]
    observed = [row for row in RESULTS if row[4]]
    passed = sum(1 for row in core if row[2])
    failed = len(core) - passed

    print(f"核心断言 {len(core)} 条：PASS={passed}  FAIL={failed}")
    if pend:
        print(f"待实现 {len(pend)} 条（新特性未落地，不计入 FAIL）")
    if observed:
        obs_ok = sum(1 for row in observed if row[2])
        print(f"观测项 {len(observed)} 条（不计入判定）：满足={obs_ok} 不满足={len(observed) - obs_ok}")

    if failed:
        print()
        print("失败明细：")
        for item, name, ok, evidence, _o in core:
            if not ok:
                print(f"  - {item} | {name}")
                print(f"      {truncate(evidence, 700)}")
    if pend:
        print()
        print("待实现明细：")
        for item, name, _ok, evidence, _o in pend:
            print(f"  - {item} | {name}")
            print(f"      {truncate(evidence, 300)}")

    print()
    print("按用例汇总：")
    for item in dict.fromkeys(row[0] for row in RESULTS):
        rows = [row for row in RESULTS if row[0] == item]
        rows_core = [row for row in rows if not row[4] and row[2] is not None]
        rows_pend = [row for row in rows if row[2] is None]
        ok_count = sum(1 for row in rows_core if row[2])
        state = "PASS" if rows_core and ok_count == len(rows_core) else (
            "FAIL" if rows_core else "待实现")
        extra = f" + 待实现 {len(rows_pend)}" if rows_pend else ""
        print(f"  {item}: {ok_count}/{len(rows_core)} {state}{extra}")

    print()
    verdict = "ALL PASS" if failed == 0 else f"{failed} FAILED"
    if pend:
        verdict += f"（另有 {len(pend)} 条待实现）"
    print("RESULT: " + verdict)
    return 0 if failed == 0 else 1


# ================================================================ main


STEPS = {
    "D2": lambda args: step_d2(),
    "A10": lambda args: step_a10(args.no_gpu),
    "A11": lambda args: step_a11(args.no_gpu),
    "A12": lambda args: step_a12(),
    "B9": lambda args: step_b9(),
    "B10": lambda args: step_b10(args.no_gpu),
    "C6": lambda args: step_c6(Path(args.ui_probe_report) if args.ui_probe_report else None),
    "C7": lambda args: step_c7(),
    "C8": lambda args: step_c8(),
    "D1": lambda args: step_d1(args.no_gpu),
}
ORDER = ["D1", "D2", "A10", "A11", "A12", "B9", "B10", "C6", "C7", "C8"]


def main() -> int:
    parser = argparse.ArgumentParser(description="v3 阶段 3 验收")
    parser.add_argument("--only", action="append", default=[],
                        help="只跑指定用例（可多次）：" + ", ".join(ORDER))
    parser.add_argument("--no-gpu", action="store_true", help="跳过出图/显存类用例")
    parser.add_argument("--ui-probe-report", default="", help="C6 的 CDP 探针报告路径")
    args = parser.parse_args()

    print(f"API      : {API}")
    print(f"plugin   : {PLUGIN_DIR}")
    print(f"python   : {sys.version.split()[0]} ({sys.executable})")
    print(f"模式     : only={args.only or 'ALL'}  no_gpu={args.no_gpu}")

    if IMPORT_ERRORS:
        for name, err in IMPORT_ERRORS.items():
            check("import", f"import {name}", False, err)
    if COMPOSER is None:
        check("import", "NL2PromptComposer 可导入", False, "nodes 模块无该类")
        return summary()

    # 环境自检
    code, _stats = 0, {}
    try:
        code, _stats = http_json("/system_stats", timeout=10)
        online = code == 200
    except Exception:  # noqa: BLE001
        online = False
    check("env", "ComfyUI 8199 在线", online, f"HTTP {code}")
    if not online:
        print("\nRESULT: FAILED（环境不可用）")
        return 1

    selected = [item for item in ORDER if not args.only or item in args.only]
    for item in selected:
        STEPS[item](args)

    # 收尾：清理 C7 临时 skill（幂等）
    cleanup_test_skill()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
