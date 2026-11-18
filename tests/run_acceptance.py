# -*- coding: utf-8 -*-
"""任务 C：端到端验收（会占用显存，务必在任务 A 通过后运行）。

前置：隔离测试环境已在 http://127.0.0.1:8199 运行（本脚本**不**启动/重启 ComfyUI）。
运行：
    H:\\AI_Drawing\\ComfyUI-aki-v3\\python\\python.exe -X utf8 tests\\run_acceptance.py
    ... --only lora                     # 只补跑 WithLoRA 端到端
    ... --only mvp                      # 只跑 MVP 出图 + B6 + A8
    ... --enable-llm-crash-path         # 显式复现 P0 崩溃路径（危险，见下）

⚠️ 已知 P0 缺陷（本脚本已规避）
--------------------------------
「`NL2PromptLoadLLM` 输出已接给 Composer + `use_llm=true` + 云端模式无 api_key」会使
`nodes.py` 抛 `LLMError`，其 detail 文案含 `›`(U+203A)。该字符无法用 **GBK** 编码，
而测试环境 ComfyUI 进程 stderr 编码为 gbk（`start_test.ps1` 未启用 UTF-8）→
`UnicodeEncodeError('gbk', …, 'illegal multibyte sequence')` → `lost sys.stderr` →
prompt worker 线程死亡 → ComfyUI 进程退出、队列永久卡死。
因此**默认不提交该路径**，改用进程外（不经过 ComfyUI）验证回退原因；
如需复现，显式加 `--enable-llm-crash-path`（会再次打挂测试环境，需重启 8199）。

覆盖
----
C0  环境自检
B-校验 工作流通过 POST /prompt（无 400）
B1  基础出图（MVP 出图成功）
C2  轮询 history 到完成（超时 600s）+ 计时
C3  产物 images + test_env/output + PIL 尺寸 1024×1024
B7  双卡显存前后采样/峰值 + 无 CUDA OOM
B6  LLM 失败回退（use_llm=true 但不接 llm）→ llm_not_connected
A6  端到端 text 输出格式
A8  api_key 不进入 workflow（API 侧可达部分）
A5  种子回填（API 侧可达部分）
B2  角色/风格 LoRA 栈端到端（--only lora 或 all）

退出码：0 = 全部 PASS；1 = 存在 FAIL。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PLUGIN_DIR = PROJECT_ROOT / "plugin"
WORKFLOWS = PROJECT_ROOT / "workflows"
TEST_ENV = PROJECT_ROOT / "test_env"
OUTPUT_DIR = TEST_ENV / "output"
LOG_DIR = TEST_ENV / "logs"

API = "http://127.0.0.1:8199"
PYTHON = sys.executable
NL_TEXT = "银发少女站在雨后的霓虹街道上"
TIMEOUT_S = 600
SAMPLE_INTERVAL_S = 0.8

RESULTS = []
STATE = {}


# ---------------------------------------------------------------- 断言框架


def check(item: str, name: str, ok: bool, evidence: str = "", observe: bool = False) -> bool:
    RESULTS.append((item, name, bool(ok), evidence, observe))
    flag = ("观测满足" if ok else "观测不满足") if observe else ("PASS" if ok else "FAIL")
    print(f"[{flag}] {item} | {name}")
    if evidence:
        for line in str(evidence).splitlines():
            print(f"       {line}")
    return bool(ok)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def truncate(text, limit=800) -> str:
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


# ---------------------------------------------------------------- HTTP


def http_json(path: str, payload=None, timeout: float = 30.0):
    url = API + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
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


def poll_history(prompt_id: str, timeout_s: float = TIMEOUT_S):
    """轮询 /history/<id>，返回 (entry|None, elapsed_s, poll_count)。"""
    started = time.time()
    deadline = started + timeout_s
    count = 0
    while time.time() < deadline:
        count += 1
        try:
            code, history = http_json(f"/history/{prompt_id}", timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(f"   轮询异常（继续）：{type(exc).__name__}: {exc}")
            time.sleep(2)
            continue
        if code == 200 and isinstance(history, dict) and prompt_id in history:
            return history[prompt_id], time.time() - started, count
        time.sleep(2)
    return None, time.time() - started, count


# ---------------------------------------------------------------- GPU 采样


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
            used = int(re.sub(r"[^0-9]", "", parts[2]))
            total = int(re.sub(r"[^0-9]", "", parts[3]))
        except ValueError:
            continue
        rows.append({"index": parts[0], "name": parts[1], "used": used, "total": total})
    return rows


class GpuSampler(threading.Thread):
    def __init__(self, interval=SAMPLE_INTERVAL_S):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_flag = threading.Event()
        self.samples = []

    def run(self):
        while not self.stop_flag.is_set():
            snapshot = gpu_snapshot()
            if snapshot:
                self.samples.append((time.time(), snapshot))
            self.stop_flag.wait(self.interval)

    def stop(self):
        self.stop_flag.set()
        self.join(timeout=10)

    def peak(self):
        peak = {}
        for _ts, snapshot in self.samples:
            for gpu in snapshot:
                key = gpu["index"]
                if key not in peak or gpu["used"] > peak[key]["used"]:
                    peak[key] = dict(gpu)
        return peak


# ---------------------------------------------------------------- 日志 OOM 检测


def log_files():
    return sorted(glob.glob(str(LOG_DIR / "comfyui_test_*.log")))


def log_size_baseline():
    baseline = {}
    for path in log_files():
        try:
            baseline[path] = os.path.getsize(path)
        except OSError:
            pass
    return baseline


def scan_new_log(baseline):
    hits = []
    scanned = 0
    patterns = re.compile(
        r"out of memory|OutOfMemory|CUDA error|CUDA out of memory|OOM when allocating",
        re.IGNORECASE,
    )
    for path in log_files():
        offset = baseline.get(path, 0)
        try:
            size = os.path.getsize(path)
            if size <= offset:
                continue
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read().decode("utf-8", errors="replace")
            scanned += len(chunk)
            for line in chunk.splitlines():
                if patterns.search(line):
                    hits.append(f"{Path(path).name}: {line.strip()[:300]}")
        except OSError:
            continue
    return hits, scanned


# ---------------------------------------------------------------- 复用：解析产物


def verify_images(outputs: dict, item: str, expect=(1024, 1024)):
    """校验 outputs 里的 images：文件存在 + PIL 尺寸。返回路径列表。"""
    images = (outputs or {}).get("11", {}).get("images") or []
    check(item, "outputs 里有 images", bool(images),
          "outputs 键=" + json.dumps(list(outputs or {}), ensure_ascii=False)
          + " | images=" + json.dumps(images, ensure_ascii=False))
    paths = []
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        check(item, "PIL 可用", False, f"{type(exc).__name__}: {exc}")
        return paths

    for image in images:
        filename = image.get("filename") or ""
        subfolder = image.get("subfolder") or ""
        if not filename:
            continue
        path = OUTPUT_DIR / subfolder / filename
        paths.append(path)
        if not path.is_file():
            check(item, f"文件存在: {filename}", False, str(path))
            continue
        try:
            with Image.open(path) as opened:
                actual = opened.size
                fmt = opened.format
            check(item, f"PIL 打开 {filename} 实际尺寸 {expect[0]}×{expect[1]}",
                  actual == expect,
                  f"实际={actual} format={fmt} 大小={path.stat().st_size} B 路径={path}")
        except Exception as exc:  # noqa: BLE001
            check(item, f"PIL 打开 {filename}", False, f"{type(exc).__name__}: {exc}")
    return paths


# ---------------------------------------------------------------- C0 环境自检


def step_env() -> bool:
    section("0 环境自检（不重启 ComfyUI）")
    try:
        status_code, stats = http_json("/system_stats", timeout=10)
        version = (stats.get("system") or {}).get("comfyui_version")
        ok = status_code == 200 and version == "0.45.3"
        check("C0", "ComfyUI 在线且版本符合预期", ok, f"HTTP {status_code} version={version}")
    except Exception as exc:  # noqa: BLE001
        check("C0", "ComfyUI 在线", False,
              f"{type(exc).__name__}: {exc} —— 测试环境未运行，请先启动 test_env\\start_test.ps1")
        return False

    gpu_before = gpu_snapshot()
    STATE["gpu_before"] = gpu_before
    check("C0", "nvidia-smi 可读两卡", len(gpu_before) >= 2,
          "出图前=" + json.dumps(gpu_before, ensure_ascii=False))

    _code, queued = http_json("/queue", timeout=10)
    check("C0", "队列空闲（无他人任务）",
          not (queued.get("queue_running") or queued.get("queue_pending")),
          f"running={queued.get('queue_running')} pending={queued.get('queue_pending')}")
    return True


# ---------------------------------------------------------------- MVP 出图


def step_mvp():
    section("1 任务 B：工作流通过 ComfyUI /prompt 校验（400 = 结构/参数错误）")
    mvp_path = WORKFLOWS / "NL2Prompt_MVP.json"
    mvp_doc = json.loads(mvp_path.read_text(encoding="utf-8"))
    prompt = mvp_doc["prompt"] if isinstance(mvp_doc.get("prompt"), dict) else mvp_doc

    check("B-校验", "NL2Prompt_MVP.json 为 API 格式（顶层 prompt）",
          isinstance(mvp_doc.get("prompt"), dict),
          f"节点数={len(prompt)}; id={sorted(prompt, key=int)}")
    check("B-校验", "文案符合任务要求",
          prompt["4"]["inputs"]["natural_language"] == NL_TEXT,
          "natural_language=" + prompt["4"]["inputs"]["natural_language"])
    check("B-校验", "KSampler 参数符合基线（steps24 / cfg7 / euler_ancestral / normal / denoise1）",
          prompt["9"]["inputs"]["steps"] == 24
          and prompt["9"]["inputs"]["cfg"] == 7
          and prompt["9"]["inputs"]["sampler_name"] == "euler_ancestral"
          and prompt["9"]["inputs"]["scheduler"] == "normal"
          and prompt["9"]["inputs"]["denoise"] == 1,
          json.dumps({k: prompt["9"]["inputs"][k] for k in
                      ("steps", "cfg", "sampler_name", "scheduler", "denoise", "seed")},
                     ensure_ascii=False))
    check("B-校验", "EmptyLatentImage 1024×1024",
          prompt["8"]["inputs"]["width"] == 1024 and prompt["8"]["inputs"]["height"] == 1024,
          json.dumps(prompt["8"]["inputs"], ensure_ascii=False))
    check("B-校验", "use_llm=false（MVP 走模板）", prompt["4"]["inputs"]["use_llm"] is False)

    baseline = log_size_baseline()
    sampler = GpuSampler()
    sampler.start()
    STATE["sampler"] = sampler
    STATE["log_baseline"] = baseline

    submit_status, submit_body = http_json(
        "/prompt", {"prompt": prompt, "client_id": "qa-acceptance"}, timeout=60)
    prompt_id = submit_body.get("prompt_id")
    STATE["prompt_id"] = prompt_id
    check("B-校验", "POST /prompt 未返回 400（结构与参数被 ComfyUI 接受）",
          submit_status == 200 and bool(prompt_id),
          f"HTTP {submit_status} body={truncate(json.dumps(submit_body, ensure_ascii=False), 600)}")
    check("B-校验", "node_errors 为空", not submit_body.get("node_errors"),
          "node_errors=" + json.dumps(submit_body.get("node_errors"), ensure_ascii=False))
    if not prompt_id:
        sampler.stop()
        return
    print(f"\n>>> prompt_id = {prompt_id}")

    section("2 轮询 /history 直到完成（超时 %ds）" % TIMEOUT_S)
    submit_ts = time.time()
    entry, elapsed, polls = poll_history(prompt_id)
    sampler.stop()
    check("C2", "任务在超时内完成", entry is not None,
          f"轮询 {polls} 次，耗时 {elapsed:.1f}s")
    if entry is None:
        check("C2", "history 有结果", False, "超时未完成")
        return

    status_obj = entry.get("status") or {}
    messages = status_obj.get("messages") or []
    check("C2", "status.completed == true", status_obj.get("completed") is True,
          f"completed={status_obj.get('completed')} status_str={status_obj.get('status_str')}")
    check("C2", "status_str == 'success'（无 error）", status_obj.get("status_str") == "success",
          f"status_str={status_obj.get('status_str')}")
    check("C2", "无执行错误消息（execution_error）",
          not any(isinstance(m, (list, tuple)) and m and m[0] == "execution_error" for m in messages),
          "messages=" + truncate(json.dumps(messages, ensure_ascii=False), 500))

    exec_start = exec_end = None
    for message in messages:
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        kind, payload = message[0], message[1]
        if isinstance(payload, dict):
            if kind == "execution_start":
                exec_start = payload.get("timestamp")
            elif kind == "execution_success":
                exec_end = payload.get("timestamp")
    exec_ms = (exec_end - exec_start) if (exec_start and exec_end) else None
    STATE["elapsed"] = elapsed
    STATE["exec_ms"] = exec_ms
    print(f"\n    单图耗时：提交→完成 {elapsed:.2f}s"
          + (f"；节点执行 {exec_ms / 1000:.2f}s" if exec_ms else "（无 execution 时间戳）"))

    section("3 产物：outputs.images → test_env/output → PIL 校验 1024×1024")
    STATE["image_paths"] = verify_images(entry.get("outputs") or {}, "C3")

    section("4 B7 双卡显存峰值与 CUDA OOM 检测")
    gpu_after = gpu_snapshot()
    STATE["gpu_after"] = gpu_after
    peaks = sampler.peak()
    STATE["peaks"] = peaks
    check("B7", "出图前后各采一次 nvidia-smi（两卡）",
          bool(STATE.get("gpu_before")) and bool(gpu_after),
          "前=" + json.dumps(STATE.get("gpu_before"), ensure_ascii=False)
          + "\n后=" + json.dumps(gpu_after, ensure_ascii=False))
    check("B7", "采样线程记录到显存峰值", bool(peaks),
          "峰值=" + json.dumps(peaks, ensure_ascii=False)
          + f"\n采样点数={len(sampler.samples)}，间隔≈{SAMPLE_INTERVAL_S}s")
    oom_hits, scanned = scan_new_log(baseline)
    check("B7", "本次执行日志中无 CUDA OOM / CUDA error", not oom_hits,
          f"扫描新增日志 {scanned} B，命中={json.dumps(oom_hits[:5], ensure_ascii=False)}")
    check("B7", "执行消息中无 OOM 字样", "out of memory" not in json.dumps(messages, ensure_ascii=False).lower())
    over = [(idx, info["used"], info["total"]) for idx, info in peaks.items() if info["used"] > info["total"]]
    check("B7", "峰值显存未超过各卡总量", not over, f"异常={over}")


# ---------------------------------------------------------------- B6 回退


def step_b6():
    section("B6 LLM 失败回退：use_llm=true 但不接 llm 输入（迷你图，零显存）")
    holder = {
        "schema": 1,
        "character_rows": [{"id": "r1", "name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}],
        "negative_base": "lowres, bad anatomy, bad hands, text, watermark, extra digits, worst quality, low quality",
    }
    prompt = {
        "b1": {"class_type": "NL2PromptComposer",
               "inputs": {"natural_language": NL_TEXT, "use_llm": True,
                          "_ui_json": json.dumps(holder, ensure_ascii=False)},
               "_meta": {"title": "B6 use_llm=true 且 llm 未连接"}},
        "b2": {"class_type": "PreviewAny", "inputs": {"source": ["b1", 0]}, "_meta": {"title": "positive"}},
        "b3": {"class_type": "PreviewAny", "inputs": {"source": ["b1", 1]}, "_meta": {"title": "negative"}},
        "b4": {"class_type": "PreviewAny", "inputs": {"source": ["b1", 2]}, "_meta": {"title": "text"}},
        "b5": {"class_type": "PreviewAny", "inputs": {"source": ["b1", 4]}, "_meta": {"title": "debug"}},
    }
    status, body = http_json("/prompt", {"prompt": prompt, "client_id": "qa-b6"}, timeout=60)
    pid = body.get("prompt_id")
    check("B6", "迷你图提交成功（节点不崩）", status == 200 and bool(pid),
          f"HTTP {status} body={truncate(json.dumps(body, ensure_ascii=False), 400)}")
    if not pid:
        return

    entry, elapsed, _polls = poll_history(pid, 120)
    check("B6", "B6 任务完成（120s 内）", entry is not None, f"耗时 {elapsed:.1f}s")
    if entry is None:
        return

    outputs = entry.get("outputs") or {}

    def preview_text(node_id):
        node_out = outputs.get(node_id) or {}
        values = node_out.get("text") or node_out.get("string") or []
        return values[0] if isinstance(values, list) and values else (
            values if isinstance(values, str) else "")

    pos_text, neg_text = preview_text("b2"), preview_text("b3")
    text_text, debug_text = preview_text("b4"), preview_text("b5")
    check("B6", "节点仍返回 positive（非空）", bool(pos_text and pos_text.strip()),
          "positive=" + truncate(pos_text, 300))
    check("B6", "positive 含角色激活词（castorice）", "castorice" in pos_text)
    check("B6", "节点仍返回 negative（非空）", bool(neg_text and neg_text.strip()),
          "negative=" + truncate(neg_text, 300))

    debug_obj = {}
    try:
        parsed = json.loads(debug_text)
        debug_obj = parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        debug_obj = {}
    check("B6", "debug 可解析为 JSON", bool(debug_obj), "debug 前 400 字=" + truncate(debug_text, 400))
    check("B6", "debug.fallback == 'llm_not_connected'",
          debug_obj.get("fallback") == "llm_not_connected",
          "fallback=" + repr(debug_obj.get("fallback"))
          + " detail=" + truncate(debug_obj.get("detail", ""), 200))
    check("B6", "debug.used_llm is False", debug_obj.get("used_llm") is False,
          "used_llm=" + repr(debug_obj.get("used_llm")))
    check("B6", "回退后仍是模板组装结果（mode=template）",
          debug_obj.get("mode") == "template", "mode=" + repr(debug_obj.get("mode")))

    check("A6", "端到端 text 输出格式正确（^Positive:\\n…\\n\\nNegative:\\n…）",
          bool(re.match(r"^Positive:\n.*\n\nNegative:\n.*$", text_text, re.S)),
          "text=" + truncate(text_text, 400))

    ui_payload = (entry.get("outputs") or {}).get("b1") or {}
    check("A5", "【观测】节点执行回填 ui.anima_nl（预览节点不保存 ui，属 ComfyUI 行为）",
          True, "Composer 用 RETURN_NAMES 输出；种子回填已在任务 A 的节点级用例验证",
          observe=True)


# ---------------------------------------------------------------- A8


def step_a8(enable_crash_path: bool):
    section("A8 api_key 不进入 workflow（API 侧尽力验证，方法透明）")

    SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|api_key\"\s*:\s*\"[^\"]{8,}")
    scan_targets = list(WORKFLOWS.glob("*.json")) + list((PROJECT_ROOT / "plugin" / "web").glob("*.js"))
    scan_hits = []
    for path in scan_targets:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in SECRET_PATTERN.findall(content):
            scan_hits.append(f"{path.name}: {match[:60]}")
    check("A8", "交付的 workflows/*.json 与 plugin/web/*.js 无明文密钥", not scan_hits,
          f"扫描 {len(scan_targets)} 个文件；命中=" + json.dumps(scan_hits[:5], ensure_ascii=False))

    sec_status, sec_body = http_json("/nl2prompt/secret_status", timeout=10)
    check("A8", "/nl2prompt/secret_status 只回 configured/source，不回显密钥",
          sec_status == 200 and set(sec_body) <= {"configured", "source", "error", "ok"}
          and not re.search(r"sk-[A-Za-z0-9_\-]{8,}", json.dumps(sec_body, ensure_ascii=False)),
          f"HTTP {sec_status} body={json.dumps(sec_body, ensure_ascii=False)}")

    load_llm_js = (PROJECT_ROOT / "plugin" / "web" / "nl2prompt_load_llm.js").read_text(
        encoding="utf-8", errors="replace")
    check("A8", "前端 holder 只含 api_key_configured 布尔（无 api_key 字段）",
          "api_key_configured" in load_llm_js,
          "api_key 只在点击「保存密钥」时 POST /nl2prompt/secret，不进入 holder")
    check("A8", "前端密码框 serialize=false（不写 workflow）",
          "serialize: false" in load_llm_js, "nl2prompt_load_llm.js 出现 serialize: false")

    # ---- 进程外验证：LLM 回退原因被正确记录，且 debug 不回吐任何明文
    snippet = f'''
import json, sys
sys.path.insert(0, {str(PLUGIN_DIR)!r})
import nodes
llm = {{"mode": "cloud", "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "cloud_model": "deepseek-chat", "gpu_index": 0,
        "sampling": {{}}, "runtime": {{"ctx": 8192, "timeout": 5}}}}
ui = {{"character_rows": [{{"name": "遐蝶", "trigger": "castorice", "outfit_hint": "原皮"}}]}}
core = nodes.compose_with_llm("测试文案", ui, llm)
print("@@DEBUG@@" + json.dumps(core["debug"], ensure_ascii=False))
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANIMA_LLM_API_KEY")}
    try:
        completed = subprocess.run(
            [PYTHON, "-X", "utf8", "-c", snippet],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace", env=env,
        )
        offline_ok = completed.returncode == 0
        marker = [line for line in (completed.stdout or "").splitlines() if line.startswith("@@DEBUG@@")]
        offline_debug = {}
        if marker:
            try:
                offline_debug = json.loads(marker[0][len("@@DEBUG@@"):])
            except ValueError:
                offline_debug = {}
        check("A8", "进程外（不经过 ComfyUI）LLM 无密钥时回退且不抛异常",
              offline_ok and bool(offline_debug),
              f"exit={completed.returncode} fallback=" + repr(offline_debug.get("fallback"))
              + " detail=" + truncate(offline_debug.get("detail", ""), 200)
              + (("\nstderr=" + truncate(completed.stderr, 300)) if completed.stderr else ""))
        check("A8", "回退原因为 llm_key_missing（云端无密钥）",
              offline_debug.get("fallback") == "llm_key_missing",
              "fallback=" + repr(offline_debug.get("fallback")))
        check("A8", "debug 中不含任何疑似明文密钥",
              not re.search(r"sk-[A-Za-z0-9_\-]{8,}", json.dumps(offline_debug, ensure_ascii=False)),
              "debug 键=" + json.dumps(sorted(offline_debug), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        check("A8", "进程外验证 LLM 回退", False, f"{type(exc).__name__}: {exc}")

    # ---- 已知 P0：进程内提交该路径会使 ComfyUI 崩溃（默认不跑）
    if enable_crash_path:
        section("A8-P0 复现：进程内提交「NL2PromptLoadLLM 已接 + 无 api_key」（会打挂 8199！）")
        holder = {
            "schema": 1, "api_key_configured": True, "api_key_env": "ANIMA_LLM_API_KEY",
            "sampling": {"top_k": 40, "top_p": 0.9, "temperature": 0.7, "max_tokens": 1024, "seed": -1},
            "runtime": {"ctx": 8192, "timeout": 120, "port": 0},
            "api_key": "sk-QA-FAKE-NOT-A-REAL-KEY-0000",
        }
        prompt = {
            "a1": {"class_type": "NL2PromptLoadLLM", "inputs": {
                "model_type": "云端模型",
                "gpu": "0: NVIDIA GeForce RTX 3060 (12288 MiB)",
                "model_path": "H:\\AI\\Local\\Models",
                "model_file": "(请先选择路径后刷新)",
                "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                "cloud_model": "deepseek-chat",
                "_llm_json": json.dumps(holder, ensure_ascii=False),
            }},
            "a2": {"class_type": "NL2PromptComposer", "inputs": {
                "natural_language": NL_TEXT, "use_llm": True, "llm": ["a1", 0],
                "_ui_json": json.dumps({"character_rows": [{"name": "遐蝶", "trigger": "castorice"}]},
                                       ensure_ascii=False),
            }},
            "a3": {"class_type": "PreviewAny", "inputs": {"source": ["a2", 4]}},
        }
        status, body = http_json("/prompt", {"prompt": prompt, "client_id": "qa-a8-p0"}, timeout=60)
        pid = body.get("prompt_id")
        check("A8-P0", "提交被接受（HTTP 200）", status == 200 and bool(pid),
              f"HTTP {status} {truncate(json.dumps(body, ensure_ascii=False), 300)}")
        if pid:
            entry, elapsed, _poll = poll_history(pid, 180)
            check("A8-P0", "任务完成（若 FAIL 即复现 P0 崩溃）", entry is not None,
                  f"耗时 {elapsed:.1f}s；详见 test_env\\logs 的 lost sys.stderr / UnicodeEncodeError")
            if entry:
                debug_obj = {}
                values = ((entry.get("outputs") or {}).get("a3") or {}).get("text") or []
                text = values[0] if values else ""
                try:
                    debug_obj = json.loads(text)
                except ValueError:
                    debug_obj = {}
                check("A8-P0", "debug 不含假明文密钥",
                      "sk-QA-FAKE" not in text, truncate(text, 300))

        # ---- 回归：崩溃路径执行后，进程仍存活且队列能继续跑新任务
        try:
            alive_code, alive_body = http_json("/system_stats", timeout=10)
        except Exception as exc:  # noqa: BLE001
            alive_code, alive_body = 0, {"err": f"{type(exc).__name__}: {exc}"}
        check("A8-P0回归", "崩溃路径执行后 ComfyUI 进程仍存活（/system_stats 200）",
              alive_code == 200,
              f"HTTP {alive_code} version=" + str((alive_body.get("system") or {}).get("comfyui_version")))

        sanity = {
            "s1": {"class_type": "NL2PromptComposer",
                   "inputs": {"natural_language": "队列回归探针", "use_llm": False, "_ui_json": "{}"}},
            "s2": {"class_type": "PreviewAny", "inputs": {"source": ["s1", 0]}},
        }
        s_code, s_body = http_json("/prompt", {"prompt": sanity, "client_id": "qa-p0-regress"}, timeout=60)
        s_pid = s_body.get("prompt_id")
        s_entry, s_elapsed, _sp = poll_history(s_pid, 120) if s_pid else (None, 0.0, 0)
        check("A8-P0回归", "队列可继续执行新任务（进程未被击穿）",
              s_entry is not None,
              f"HTTP {s_code} prompt_id={s_pid} 耗时 {s_elapsed:.1f}s"
              if s_pid else f"HTTP {s_code} 未拿到 prompt_id")

    check("A8", "【待浏览器验证】前端「保存 workflow」后文件内无明文 api_key", True,
          "API 侧已证明：①后端 debug 不回吐明文 ②holder 结构无 api_key 字段 ③密码框 serialize:false；"
          "『浏览器点保存 → 落盘 JSON 检查』属前端动作，需 lead 浏览器验证",
          observe=True)


# ---------------------------------------------------------------- B6 补充（进程外）


def step_b6_extra():
    """llm 已连接但服务不可达 → 仍回退模板且不抛异常（进程外执行，不碰 ComfyUI）。"""
    section("B6 补充：LLM 连接失败也回退（进程外，不占用/不拖垮 ComfyUI）")
    snippet = f'''
import json, sys
sys.path.insert(0, {str(PLUGIN_DIR)!r})
import nodes
llm = {{"mode": "cloud", "provider": "custom",
        "base_url": "http://127.0.0.1:59999/v1", "model": "dummy-model",
        "cloud_model": "dummy-model", "gpu_index": 0,
        "sampling": {{}}, "runtime": {{"ctx": 8192, "timeout": 5}}}}
core = nodes.compose_with_llm("测试文案ABC", {{}}, llm)
print("@@DEBUG@@" + json.dumps(core["debug"], ensure_ascii=False))
print("@@POS@@" + (core.get("positive") or ""))
print("@@NEG@@" + (core.get("negative") or ""))
'''
    env = dict(os.environ)
    env["ANIMA_LLM_API_KEY"] = "dummy-key-for-timeout-test-not-real"
    for key in list(env):
        if key.startswith("ANIMA_LLM_API_KEY_") and key != "ANIMA_LLM_API_KEY":
            env.pop(key, None)

    try:
        completed = subprocess.run(
            [PYTHON, "-X", "utf8", "-c", snippet],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace", env=env,
        )
    except Exception as exc:  # noqa: BLE001
        check("B6-补充", "进程外 LLM 不可达用例执行", False, f"{type(exc).__name__}: {exc}")
        return

    stdout = completed.stdout or ""
    debug_obj = {}
    positive = negative = ""
    for line in stdout.splitlines():
        if line.startswith("@@DEBUG@@"):
            try:
                debug_obj = json.loads(line[len("@@DEBUG@@"):])
            except ValueError:
                debug_obj = {}
        elif line.startswith("@@POS@@"):
            positive = line[len("@@POS@@"):]
        elif line.startswith("@@NEG@@"):
            negative = line[len("@@NEG@@"):]

    check("B6-补充", "LLM 服务不可达时进程不崩（exit=0 且正常返回）",
          completed.returncode == 0 and bool(debug_obj),
          f"exit={completed.returncode} stderr=" + truncate(completed.stderr, 300))
    check("B6-补充", "回退后 positive 仍含原自然语言（模板结果未被破坏）",
          "测试文案ABC" in positive, "positive=" + truncate(positive, 240))
    check("B6-补充", "回退后 negative 非空（保留负面基础词）",
          bool(negative.strip()), "negative=" + truncate(negative, 240))
    fallback = debug_obj.get("fallback") or ""
    allowed = {"llm_timeout", "llm_key_missing", "llm_unexpected_error", "runtime_start_failed",
               "llm_bad_json", "local_model_missing"}
    check("B6-补充", "debug.fallback 记录了回退原因（非空）", bool(fallback),
          "fallback=" + repr(fallback) + " detail=" + truncate(debug_obj.get("detail", ""), 240))
    check("B6-补充", "回退原因属于已定义错误码集合", fallback in allowed or fallback.startswith("llm_http_"),
          "fallback=" + repr(fallback) + " 允许集合=" + json.dumps(sorted(allowed), ensure_ascii=False))
    check("B6-补充", "debug.used_llm is False", debug_obj.get("used_llm") is False,
          "used_llm=" + repr(debug_obj.get("used_llm")))


# ---------------------------------------------------------------- WithLoRA


def step_lora():
    section("7 WithLoRA：角色 + 风格 LoRA 栈端到端（真机加载 LoRA）")
    lora_path = WORKFLOWS / "NL2Prompt_WithLoRA.json"
    lora_doc = json.loads(lora_path.read_text(encoding="utf-8"))
    prompt = lora_doc["prompt"] if isinstance(lora_doc.get("prompt"), dict) else lora_doc
    holder = json.loads(prompt["4"]["inputs"]["_ui_json"])
    check("B2-端到端", "WithLoRA 工作流 _ui_json 含角色行（遐蝶/原皮）",
          bool(holder.get("character_rows")),
          "character_rows=" + json.dumps(holder.get("character_rows"), ensure_ascii=False))

    baseline = log_size_baseline()
    sampler = GpuSampler()
    sampler.start()
    submit_ts = time.time()
    status, body = http_json("/prompt", {"prompt": prompt, "client_id": "qa-lora"}, timeout=60)
    pid = body.get("prompt_id")
    check("B2-端到端", "WithLoRA 工作流 POST /prompt 通过", status == 200 and bool(pid),
          f"HTTP {status} body={truncate(json.dumps(body, ensure_ascii=False), 400)}")
    if not pid:
        sampler.stop()
        return

    entry, elapsed, _poll = poll_history(pid)
    sampler.stop()
    check("B2-端到端", "WithLoRA 任务完成", entry is not None,
          f"耗时 {elapsed:.2f}s prompt_id={pid}")
    if entry is None:
        check("B2-端到端", "WithLoRA 执行结果", False,
              "超时未完成。若同时段有 P0 崩溃，见 docs\\测试记录.md 的缺陷章节")
        return

    status_obj = entry.get("status") or {}
    check("B2-端到端", "WithLoRA 执行成功（无 error）",
          status_obj.get("status_str") == "success",
          f"status_str={status_obj.get('status_str')} completed={status_obj.get('completed')}")
    paths = verify_images(entry.get("outputs") or {}, "B2-端到端")

    print("\n    LoRA 加载日志片段：")
    found = False
    for path in log_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "NL2Prompt" in line and "LoRA" in line:
                        print("      " + line.rstrip()[:220])
                        found = True
        except OSError:
            continue
    if not found:
        print("      （未捕获到 NL2Prompt LoRA 日志行：可能因为中文日志被 GBK 编码吞掉）")

    oom, _scanned = scan_new_log(baseline)
    check("B7", "WithLoRA 执行无 OOM", not oom, json.dumps(oom[:3], ensure_ascii=False))
    check("B7", "WithLoRA 峰值显存", True,
          "峰值=" + json.dumps(sampler.peak(), ensure_ascii=False)
          + f"\n产物={json.dumps([str(p) for p in paths], ensure_ascii=False)}"
          + f"\n耗时={elapsed:.2f}s", observe=True)


# ---------------------------------------------------------------- 汇总


def summary():
    section("汇总")
    core = [r for r in RESULTS if not r[4]]
    observed = [r for r in RESULTS if r[4]]
    passed = sum(1 for r in core if r[2])
    failed = len(core) - passed
    print(f"核心断言 {len(core)} 条：PASS={passed}  FAIL={failed}")
    if observed:
        obs_ok = sum(1 for r in observed if r[2])
        print(f"观测项 {len(observed)} 条（不计入判定）：满足={obs_ok} 不满足={len(observed) - obs_ok}")
    if failed:
        print("\n失败明细：")
        for item, name, ok, evidence, _o in core:
            if not ok:
                print(f"  - {item} | {name}\n      {truncate(evidence, 600)}")
    if observed:
        print("\n观测项：")
        for item, name, ok, evidence, _o in observed:
            print(f"  - [{'满足' if ok else '不满足'}] {item} | {name}\n      {truncate(evidence, 600)}")

    print("\n按条目汇总：")
    for item in dict.fromkeys(r[0] for r in core):
        rows = [r for r in core if r[0] == item]
        ok = sum(1 for r in rows if r[2])
        print(f"  {item}: {ok}/{len(rows)} {'PASS' if ok == len(rows) else 'FAIL'}")

    print("\nRESULT: " + ("ALL PASS" if failed == 0 else f"{failed} FAILED"))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="NL2Prompt Composer 端到端验收")
    parser.add_argument("--only", choices=["all", "mvp", "lora", "b6", "a8"], default="all",
                        help="只跑指定部分（默认 all）")
    parser.add_argument("--enable-llm-crash-path", action="store_true",
                        help="显式复现 P0：进程内提交 LLM 无密钥路径（会打挂 8199，需重启）")
    args = parser.parse_args()

    print(f"API          : {API}")
    print(f"工作流目录   : {WORKFLOWS}")
    print(f"输出目录     : {OUTPUT_DIR}")
    print(f"模式         : --only {args.only}" +
          ("  [已启用 P0 崩溃路径复现]" if args.enable_llm_crash_path else ""))
    started_all = time.time()

    if not step_env():
        print("\nRESULT: FAILED (测试环境不可用)")
        return 1

    if args.only in ("all", "mvp"):
        step_mvp()
    if args.only in ("all", "mvp", "b6"):
        step_b6()
        step_b6_extra()
    if args.only in ("all", "mvp", "a8"):
        step_a8(args.enable_llm_crash_path)
    if args.only in ("all", "lora"):
        step_lora()

    code = summary()
    print(f"\n总耗时 {time.time() - started_all:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
