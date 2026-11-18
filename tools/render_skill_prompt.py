"""用真实 LLM 产出（经 skill 校验）的提示词渲染一张效果图。

提示词取自 v2 复验 A1 用例的实际输出（`tests\\_rewrite_real.json`），
所以这张图等于「v2 skill 链路最终产物 → 出图」的端到端展示。

用法：python -X utf8 tools/render_skill_prompt.py [--seed 123456789]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8199"

# —— v2 复验 A1「雨夜礼裙」的实际 LLM 输出（skill_check ok=True，negative 覆盖 11/11）
POSITIVE = (
    "masterpiece, best quality, 1girl, solo, castorice, purple hair, colored inner hair, "
    "twintails, braid, hair flower, butterfly hair ornament, hair bow, crown, purple eyes, "
    "pointy ears, earrings, layered dress, white skirt, long sleeves, sideboob, bare legs, "
    "jewelry, purple flower, purple footwear, heels, standing, rain, neon street, city, night, "
    "wet, from front, soft light, @nixeu"
)
NEGATIVE = (
    "worst quality, low quality, lowres, blurry, jpeg artifacts, bad anatomy, bad hands, "
    "extra digits, watermark, signature, english text, logo, strapless dress, purple dress, "
    "frilled dress, detached collar, detached sleeves, purple sleeves, clothing cutout, "
    "elbow gloves, mismatched gloves, white thighhighs, gradient legwear"
)


def post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=123456789)
    ap.add_argument("--steps", type=int, default=24)
    ap.add_argument("--cfg", type=float, default=7.0)
    args = ap.parse_args()

    with open(os.path.join(ROOT, "workflows", "NL2Prompt_MVP.json"), encoding="utf-8") as fh:
        wf = json.load(fh)["prompt"]

    # 节点 5 = 正向、节点 6 = 负向：把链接换成字面量
    wf["5"]["inputs"]["text"] = POSITIVE
    wf["6"]["inputs"]["text"] = NEGATIVE
    wf["9"]["inputs"]["seed"] = args.seed
    wf["9"]["inputs"]["steps"] = args.steps
    wf["9"]["inputs"]["cfg"] = args.cfg
    wf["4"]["inputs"]["use_llm"] = False  # 出图阶段不需要再调 LLM

    client_id = uuid.uuid4().hex
    res = post_json("/prompt", {"prompt": wf, "client_id": client_id})
    pid = res.get("prompt_id")
    print("submitted:", pid)

    deadline = time.time() + 300
    hist = None
    while time.time() < deadline:
        time.sleep(2)
        try:
            h = get_json("/history/" + pid)
        except Exception:
            continue
        if pid in h:
            hist = h[pid]
            break

    if not hist:
        print(json.dumps({"ok": False, "error": "timeout"}))
        return 1

    status = (hist.get("status") or {})
    print("status:", status.get("status_str"), "completed:", status.get("completed"))
    images = []
    for node_id, out in (hist.get("outputs") or {}).items():
        for img in (out.get("images") or []):
            images.append(img.get("filename"))
            print("image:", node_id, img.get("subfolder"), img.get("filename"))

    # 顺便把最终提示词打印出来，便于核对
    print("--- positive ---")
    print(POSITIVE)
    print("--- negative ---")
    print(NEGATIVE)
    print(json.dumps({"ok": bool(images), "images": images}, ensure_ascii=False))
    return 0 if images else 1


if __name__ == "__main__":
    sys.exit(main())
