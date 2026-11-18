"""ComfyUI 前端 UI 探针（CDP）。

用途：在无人类交互的情况下，真实驱动 ComfyUI 前端完成 A1/A2/A4/A5/A7/A8 的 UI 侧验证。
实现要点：
  * 用 Chrome headless + --remote-debugging-port 启动
  * 用纯标准库手写 WebSocket 客户端（不装任何 pip 包）
  * 通过 Runtime.evaluate 在页面内操作 LiteGraph 图与 DOM

用法：
  python -X utf8 tools/ui_probe.py [--url http://127.0.0.1:8199] [--shot <png path>] [--keep]
输出：一行 JSON 到 stdout（ASCII 转义，避免控制台编码问题）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class WS:
    """最小 RFC6455 客户端：仅支持文本帧 + 分片聚合。"""

    def __init__(self, url: str, timeout: float = 40.0):
        if not url.startswith("ws://"):
            raise ValueError("only ws:// supported")
        hostport, path = url[5:].split("/", 1)
        path = "/" + path
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws handshake: connection closed")
            buf += chunk
        head, self._rest = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError("ws handshake failed: " + status)

    def _read(self, n: int) -> bytes:
        while len(self._rest) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise RuntimeError("ws closed")
            self._rest += chunk
        out, self._rest = self._rest[:n], self._rest[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x8:
                raise RuntimeError("ws close frame")
            if opcode in (0x1, 0x0):
                parts.append(payload)
            if fin:
                break
        return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws: WS):
        self.ws = ws
        self._id = 0

    def call(self, method: str, params=None, timeout_msg=""):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']} {timeout_msg}")
                return msg.get("result", {})

    def eval(self, expression: str, await_promise: bool = False):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            raise RuntimeError("JS exception: " + json.dumps(det.get("exception", {}).get("description") or det)[:400])
        return res.get("result", {}).get("value")


def start_chrome(port: int, profile: str):
    exe = next((p for p in CHROME_CANDIDATES if os.path.isfile(p)), None)
    if not exe:
        raise RuntimeError("no chrome/edge found")
    args = [
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--mute-audio",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--window-size=1600,1100",
        "about:blank",
    ]
    proc = subprocess.Popen(
        [exe] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as r:
                json.load(r)
            return proc, base
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("chrome devtools endpoint did not come up")


def page_target(base: str):
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/json/list", timeout=3) as r:
                targets = json.load(r)
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("no page target")


def wait_js(cdp: CDP, expression: str, want=None, timeout=90.0, label="") -> bool:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = cdp.eval(expression)
        except Exception as exc:
            last = f"err:{exc}"
        if want is None:
            if last:
                return True
        elif last == want:
            return True
        time.sleep(0.5)
    print(f"[wait_js] timeout {label} last={last!r}", file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--shot", default="")
    ap.add_argument("--shot2", default="")
    ap.add_argument("--keep-profile", action="store_true")
    args = ap.parse_args()

    profile = os.path.join(os.environ.get("TEMP", "."), "anima_ui_probe_profile")
    report = {}
    proc = None
    ws = None
    try:
        proc, base = start_chrome(args.port, profile)
        tgt = page_target(base)
        ws = WS(tgt["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 1100, "deviceScaleFactor": 1, "mobile": False})

        cdp.call("Page.navigate", {"url": args.url})
        ok = wait_js(cdp, "!!(window.app && window.app.graph && window.LiteGraph)", True, 120, "app-ready")
        report["app_ready"] = ok
        if not ok:
            report["error"] = "app not ready"
            print(json.dumps(report, ensure_ascii=True))
            return 2

        # 关掉启动时的模板弹窗（若有），并关掉右上角菜单
        report["dialogs_removed"] = cdp.eval(
            """(() => {
                let n = 0;
                document.querySelectorAll('.p-dialog-mask, .comfy-modal, [role="dialog"]').forEach(e => { e.remove(); n++; });
                return n;
            })()"""
        )

        # 添加主节点
        report["node_added"] = cdp.eval(
            """(() => {
                const n = LiteGraph.createNode('NL2PromptComposer');
                if (!n) return 'createNode-null';
                n.pos = [80, 80];
                app.graph.add(n);
                app.graph.setDirtyCanvas(true, true);
                return n.id;
            })()"""
        )
        time.sleep(1.2)

        # 节点结构
        report["composer"] = cdp.eval(
            """(() => {
                const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
                if (!n) return {missing: true};
                const holder = n.widgets.find(w => w.name === '_ui_json');
                return {
                    widget_count: n.widgets.length,
                    widget_names: n.widgets.map(w => w.name),
                    serialized_names: n.widgets.filter(w => w.serialize !== false).map(w => w.name),
                    holder_hidden: holder ? holder.hidden === true : null,
                    holder_size: holder && holder.computeSize ? holder.computeSize(n.size[0]) : null,
                    dom_widget_heights: n.widgets.map(w => {
                        try { const s = w.computeSize ? w.computeSize(n.size[0]) : null; return s ? s[1] : null; } catch(e) { return 'err'; }
                    }),
                    node_size: n.size,
                    holder_value: holder ? String(holder.value).slice(0, 200) : null,
                    has_anima_nl_state: !!n.__animaNl
                };
            })()"""
        )

        # 动态行：初始数量 → 点两次「添加角色」→ 点一次「添加风格」→ 读数量 → 删一行
        report["rows"] = cdp.eval(
            """(() => {
                const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
                const st = () => {
                    try { return JSON.parse(n.widgets.find(w => w.name === '_ui_json').value); }
                    catch (e) { return null; }
                };
                const before = st();
                const btns = [...document.querySelectorAll('button')].filter(b => /添加指定的角色|添加指定的画师/.test(b.textContent || ''));
                const counts = {buttons_found: btns.length, labels: btns.map(b => (b.textContent || '').trim())};
                if (btns[0]) btns[0].click();
                if (btns[0]) btns[0].click();
                if (btns[1]) btns[1].click();
                const after = st();
                return {
                    buttons: counts,
                    char_rows_before: before ? (before.character_rows || []).length : null,
                    char_rows_after: after ? (after.character_rows || []).length : null,
                    style_rows_after: after ? (after.style_rows || []).length : null
                };
            })()"""
        )
        time.sleep(0.8)
        report["node_size_after_rows"] = cdp.eval(
            "(() => { const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer'); return n ? n.size : null; })()"
        )

        if args.shot:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(args.shot, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot"] = args.shot

        # 设置窗口：点 ⚙ → 检查 overlay → Esc → 检查消失
        report["settings_window"] = cdp.eval(
            """(async () => {
                const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
                const gear = [...document.querySelectorAll('button')].find(b => (b.textContent || '').trim().includes('设置'));
                if (!gear) return {gear_found: false};
                gear.click();
                await new Promise(r => setTimeout(r, 400));
                const overlay = document.querySelector('.anima-nl-overlay');
                const tabs = overlay ? [...overlay.querySelectorAll('button, [role="tab"]')].map(e => (e.textContent || '').trim()).filter(Boolean) : [];
                const opened = !!overlay && getComputedStyle(overlay).display !== 'none';
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                await new Promise(r => setTimeout(r, 400));
                const afterEsc = !!document.querySelector('.anima-nl-overlay');
                return {gear_found: true, overlay_opened: opened, tab_labels: tabs, overlay_after_esc: afterEsc};
            })()""",
            await_promise=True,
        )

        if args.shot2:
            shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(args.shot2, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            report["shot2"] = args.shot2

        # 序列化（A8：检查落盘数据里没有明文密钥；A2：holder 会进 widgets_values）
        report["serialize"] = cdp.eval(
            """(() => {
                const s = app.graph.serialize();
                const node = (s.nodes || []).find(x => x.type === 'NL2PromptComposer');
                if (!node) return {missing: true};
                const raw = JSON.stringify(node);
                return {
                    widget_count: (node.widgets_values || []).length,
                    widgets_values_preview: (node.widgets_values || []).map(v => String(v).slice(0, 120)),
                    has_ui_json: (node.widgets_values || []).some(v => { try { return !!JSON.parse(v).character_rows; } catch(e) { return false; } }),
                    contains_api_key_like: /sk-[A-Za-z0-9_\\-]{8,}|api_key"\\s*:\\s*"[^"]+"/.test(raw)
                };
            })()"""
        )

        # LLM 节点：显隐切换 + 双卡 + 窗口
        report["load_llm"] = cdp.eval(
            """(async () => {
                const n = LiteGraph.createNode('NL2PromptLoadLLM');
                if (!n) return {create: 'null'};
                n.pos = [520, 80];
                app.graph.add(n);
                app.graph.setDirtyCanvas(true, true);
                await new Promise(r => setTimeout(r, 500));
                const vis = () => n.widgets.filter(w => !w.hidden && w.serialize !== false).map(w => w.name);
                const gpu = n.widgets.find(w => w.name === 'gpu');
                const mt = n.widgets.find(w => w.name === 'model_type');
                const visible_local = vis();
                if (mt) { mt.value = '云端模型'; if (mt.callback) mt.callback(mt.value); }
                await new Promise(r => setTimeout(r, 500));
                const visible_cloud = vis();
                const prov = n.widgets.find(w => w.name === 'provider');
                const burl = n.widgets.find(w => w.name === 'base_url');
                let base_url_after_provider = null;
                if (prov && burl) { prov.value = 'openai'; if (prov.callback) prov.callback(prov.value); base_url_after_provider = burl.value; }
                return {
                    gpu_options: gpu ? gpu.options.values : null,
                    gpu_default: gpu ? gpu.value : null,
                    visible_local: visible_local,
                    visible_cloud: visible_cloud,
                    base_url_after_provider: base_url_after_provider
                };
            })()""",
            await_promise=True,
        )

        # LLM 设置窗口：采样参数 Tab 是否存在 + top_k 是否在
        report["llm_window"] = cdp.eval(
            """(async () => {
                const gears = [...document.querySelectorAll('button')].filter(b => (b.textContent || '').trim().includes('设置'));
                const gear = gears[gears.length - 1];
                if (!gear) return {gear_found: false};
                gear.click();
                await new Promise(r => setTimeout(r, 500));
                const overlay = document.querySelector('.anima-nl-overlay');
                if (!overlay) return {overlay: false};
                const text = overlay.innerText || '';
                const inputs = [...overlay.querySelectorAll('input')].map(i => ({type: i.type, value: i.value, placeholder: i.placeholder}));
                return {
                    overlay: true,
                    has_topk: /top_k|TopK/i.test(text),
                    has_topp: /top_p|TopP/i.test(text),
                    has_seed: /种子|seed/i.test(text),
                    has_ctx: /ctx|上下文/i.test(text),
                    has_timeout: /timeout|超时/i.test(text),
                    password_inputs: inputs.filter(i => i.type === 'password').length,
                    input_count: inputs.length,
                    text_head: text.replace(/\\s+/g, ' ').slice(0, 300)
                };
            })()""",
            await_promise=True,
        )

        # 关闭窗口 + 内部一致性：holder 内容与 node.properties
        report["final"] = cdp.eval(
            """(() => {
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                const n = app.graph._nodes.find(x => x.type === 'NL2PromptComposer');
                let state = null;
                try { state = JSON.parse(n.widgets.find(w => w.name === '_ui_json').value); } catch(e) {}
                return {
                    overlay_left: !!document.querySelector('.anima-nl-overlay'),
                    state_keys: state ? Object.keys(state) : null,
                    char_rows: state ? (state.character_rows || []).length : null,
                    style_rows: state ? (state.style_rows || []).length : null,
                    has_system_prompt: state ? Object.prototype.hasOwnProperty.call(state, 'system_prompt') : null,
                    has_text_map: state ? Object.prototype.hasOwnProperty.call(state, 'text_map') : null,
                    has_sampling: state ? Object.prototype.hasOwnProperty.call(state, 'sampling') : null
                };
            })()"""
        )

        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, ensure_ascii=True))
        return 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        if proc and proc.poll() is None:
            proc.kill()
        if not args.keep_profile:
            pass


if __name__ == "__main__":
    sys.exit(main())
