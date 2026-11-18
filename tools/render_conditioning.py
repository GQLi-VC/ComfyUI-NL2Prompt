"""验证 v3 改进 3：`positive_cond` / `negative_cond` 直连 K 采样器出图。

与 v2 的 `render_skill_prompt.py` 的区别：
  * v2 路径：Composer.positive/negative (STRING) → CLIPTextEncode → KSampler
  * v3 路径：Composer.positive_cond/negative_cond (CONDITIONING) → KSampler  ← 本脚本

即**完全跳过 CLIPTextEncode**，直接验证 CLIP 编码在节点内部完成。

用法：python -X utf8 tools/render_conditioning.py [--seed N]
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
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=123456789)
    parser.add_argument("--steps", type=int, default=24)
    args = parser.parse_args()

    path = os.path.join(ROOT, "workflows", "NL2Prompt_MVP.json")
    with open(path, encoding="utf-8") as fh:
        wf = json.load(fh)["prompt"]

    # 找出 Composer / KSampler / CLIPLoader / CLIPTextEncode
    composer_id = next(k for k, v in wf.items() if v["class_type"] == "NL2PromptComposer")
    sampler_id = next(k for k, v in wf.items() if v["class_type"] == "KSampler")
    clip_loader_id = next(k for k, v in wf.items() if v["class_type"] == "CLIPLoader")
    text_encode_ids = [k for k, v in wf.items() if v["class_type"] == "CLIPTextEncode"]

    # ---- 关键改动：删掉两个 CLIPTextEncode，让采样器直接吃 Composer 的 CONDITIONING
    for k in text_encode_ids:
        wf.pop(k)

    composer = wf[composer_id]["inputs"]
    composer["natural_language"] = POSITIVE          # 模板模式原样进 positive
    composer["use_llm"] = False
    composer["clip"] = [clip_loader_id, 0]           # ← 提供 CLIP，节点内部编码
    ui = json.loads(composer.get("_ui_json") or "{}")
    ui["negative_base"] = NEGATIVE
    ui["text_map"] = []
    ui["sampling"] = {"seed": args.seed}
    composer["_ui_json"] = json.dumps(ui, ensure_ascii=False)

    sampler = wf[sampler_id]["inputs"]
    sampler["positive"] = [composer_id, 5]           # RETURN_NAMES[5] = positive_cond
    sampler["negative"] = [composer_id, 6]           # RETURN_NAMES[6] = negative_cond
    sampler["seed"] = args.seed
    sampler["steps"] = args.steps

    print("节点拓扑：")
    print(f"  CLIPLoader({clip_loader_id}) -> Composer({composer_id}).clip")
    print(f"  Composer({composer_id})[5] -> KSampler({sampler_id}).positive")
    print(f"  Composer({composer_id})[6] -> KSampler({sampler_id}).negative")
    print(f"  已删除 CLIPTextEncode 节点：{text_encode_ids}")

    pid = post_json("/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex}).get("prompt_id")
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
        print(json.dumps({"ok": False, "error": "timeout"}, ensure_ascii=False))
        return 1

    status = hist.get("status") or {}
    print("status:", status.get("status_str"), "completed:", status.get("completed"))
    if status.get("status_str") != "success":
        print(json.dumps(status, ensure_ascii=False)[:800])
        return 1

    images = []
    for node_id, out in (hist.get("outputs") or {}).items():
        for img in (out.get("images") or []):
            images.append(img.get("filename"))
            print("image:", node_id, img.get("subfolder"), img.get("filename"))
    print(json.dumps({"ok": bool(images), "images": images}, ensure_ascii=False))
    return 0 if images else 1


if __name__ == "__main__":
    sys.exit(main())
