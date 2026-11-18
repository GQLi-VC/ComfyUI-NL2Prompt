# -*- coding: utf-8 -*-
"""生成示例工作流（任务 B）。

产出 4 个文件：
    workflows/NL2Prompt_MVP.json             API 格式（可直接 POST /prompt）
    workflows/NL2Prompt_MVP.workflow.json    UI 格式（拖进 ComfyUI 打开）
    workflows/NL2Prompt_WithLoRA.json        API 格式（带角色/风格 LoRA 栈）
    workflows/NL2Prompt_WithLoRA.workflow.json
API 格式文件内容形如 ``{"prompt": {<节点id>: {...}}, "extra_data": {...}}``，
即任务书所说的「``{"prompt": {...}}`` 里的 prompt 部分」再套一层，方便直接 POST。

图结构（两份相同）：
    UNETLoader ┐
    CLIPLoader ┤→ NL2PromptComposer(positive/negative) → CLIPTextEncode ×2
    VAELoader  ┘                └(lora_stack)→ NL2PromptLoRAStack ← CLIPLoader
    EmptyLatentImage → KSampler(steps24,cfg7,euler_ancestral,normal,denoise1) → VAEDecode → SaveImage

用法：
    <ComfyUI python> -X utf8 tests/gen_workflows.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = PROJECT_ROOT / "workflows"
WORKFLOWS.mkdir(exist_ok=True)

# ----------------------------------------------------------------- 真实模型名（不可编造）
UNET_NAME = "anima-preview3-base.safetensors"
CLIP_NAME = "Qwen3-06B-Base.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
CLIP_TYPE = "stable_diffusion"
CLIP_DEVICE = "default"

NL_TEXT = "银发少女站在雨后的霓虹街道上"

# KSampler 基线（对齐参考工作流 @以Anima为模型的文生图.json）
KSAMPLER = {
    "steps": 24,
    "cfg": 7.0,
    "sampler_name": "euler_ancestral",
    "scheduler": "normal",
    "denoise": 1.0,
    "seed": 123456789,
}

TEXT_MAP = [
    {
        "id": "t1",
        "find": "霓虹街道",
        "prompt": "neon-lit street, rain, wet ground, night",
        "target": "positive",
    }
]

MVP_UI_JSON = {
    "schema": 1,
    "character_rows": [],
    "style_rows": [],
    "text_map": TEXT_MAP,
    "system_prompt": "",
    "negative_base": "lowres, bad anatomy, bad hands, text, watermark, extra digits, worst quality, low quality",
    "sampling": {"seed": -1},
    "ui": {"open_main_window": False},
}

LORA_UI_JSON = {
    "schema": 1,
    "character_rows": [
        {
            "id": "r1",
            "name": "遐蝶",
            "trigger": "castorice, purple hair, purple eyes",
            "outfit_hint": "原皮",
        }
    ],
    "style_rows": [{"id": "s1", "name": "@nixeu", "trigger": "@nixeu, soft light"}],
    "text_map": TEXT_MAP,
    "system_prompt": "",
    "negative_base": "lowres, bad anatomy, bad hands, text, watermark, extra digits, worst quality, low quality",
    "sampling": {"seed": -1},
    "ui": {"open_main_window": False},
}


def holder(obj) -> str:
    """holder JSON 文本（前端/后端都按字符串序列化）。"""
    return json.dumps(obj, ensure_ascii=False, indent=None, separators=(", ", ": "))


def build_api(ui_json: dict, filename_prefix: str) -> dict:
    p = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"},
            "_meta": {"title": "UNET加载（Anima 主模型）"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": CLIP_NAME, "type": CLIP_TYPE, "device": CLIP_DEVICE},
            "_meta": {"title": "CLIP加载（Qwen3-06B）"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": VAE_NAME},
            "_meta": {"title": "VAE加载（qwen_image_vae）"},
        },
        "4": {
            "class_type": "NL2PromptComposer",
            "inputs": {
                "natural_language": NL_TEXT,
                "use_llm": False,
                "_ui_json": holder(ui_json),
            },
            "_meta": {"title": "自然语言提示词"},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ["4", 0], "clip": ["2", 0]},
            "_meta": {"title": "正向提示词编码"},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ["4", 1], "clip": ["2", 0]},
            "_meta": {"title": "负向提示词编码"},
        },
        "7": {
            "class_type": "NL2PromptLoRAStack",
            "inputs": {
                "model": ["1", 0],
                "clip": ["2", 0],
                "lora_stack": ["4", 3],
                "enabled": True,
                "strength_scale": 1.0,
            },
            "_meta": {"title": "LoRA 堆栈加载"},
        },
        "8": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
            "_meta": {"title": "空 Latent 1024x1024"},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["7", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["8", 0],
                "seed": KSAMPLER["seed"],
                "steps": KSAMPLER["steps"],
                "cfg": KSAMPLER["cfg"],
                "sampler_name": KSAMPLER["sampler_name"],
                "scheduler": KSAMPLER["scheduler"],
                "denoise": KSAMPLER["denoise"],
            },
            "_meta": {"title": "KSampler"},
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["9", 0], "vae": ["3", 0]},
            "_meta": {"title": "VAE 解码"},
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {"images": ["10", 0], "filename_prefix": filename_prefix},
            "_meta": {"title": "保存图像"},
        },
    }
    return p


# ----------------------------------------------------------------- UI（LiteGraph）格式


def build_ui_api_pair(ui_json: dict, prefix: str, title: str):
    """返回 (api_prompt, ui_workflow)。UI 版本的 links/widgets_values 与 API 版一一对应。"""
    prompt = build_api(ui_json, prefix)
    holder_text = holder(ui_json)

    # 节点 id 与 API 版保持相同，便于人工比对
    # pos/size 只影响外观
    nodes = [
        {
            "id": 1,
            "type": "UNETLoader",
            "title": "UNET加载（Anima 主模型）",
            "pos": [60, 120],
            "size": [340, 82],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [9], "slot_index": 0}],
            "properties": {"Node name for S&R": "UNETLoader"},
            "widgets_values": [UNET_NAME, "default"],
        },
        {
            "id": 2,
            "type": "CLIPLoader",
            "title": "CLIP加载（Qwen3-06B）",
            "pos": [60, 250],
            "size": [340, 106],
            "flags": {},
            "order": 1,
            "mode": 0,
            "inputs": [],
            "outputs": [
                {"name": "CLIP", "type": "CLIP", "links": [3, 5, 8], "slot_index": 0}
            ],
            "properties": {"Node name for S&R": "CLIPLoader"},
            "widgets_values": [CLIP_NAME, CLIP_TYPE, CLIP_DEVICE],
        },
        {
            "id": 3,
            "type": "VAELoader",
            "title": "VAE加载（qwen_image_vae）",
            "pos": [60, 400],
            "size": [340, 58],
            "flags": {},
            "order": 2,
            "mode": 0,
            "inputs": [],
            "outputs": [{"name": "VAE", "type": "VAE", "links": [10], "slot_index": 0}],
            "properties": {"Node name for S&R": "VAELoader"},
            "widgets_values": [VAE_NAME],
        },
        {
            "id": 4,
            "type": "NL2PromptComposer",
            "title": "自然语言提示词",
            "pos": [440, 120],
            "size": [420, 320],
            "flags": {},
            "order": 3,
            "mode": 0,
            "inputs": [
                {"name": "llm", "type": "LLM_MODEL", "link": None},
                {"name": "clip", "type": "CLIP", "link": None},
            ],
            "outputs": [
                {"name": "positive", "type": "STRING", "links": [1], "slot_index": 0},
                {"name": "negative", "type": "STRING", "links": [2], "slot_index": 1},
                {"name": "text", "type": "STRING", "links": [], "slot_index": 2},
                {"name": "lora_stack", "type": "LORA_STACK", "links": [4], "slot_index": 3},
                {"name": "debug", "type": "STRING", "links": [], "slot_index": 4},
            ],
            "properties": {"Node name for S&R": "NL2PromptComposer"},
            "widgets_values": [NL_TEXT, False, holder_text],
        },
        {
            "id": 5,
            "type": "CLIPTextEncode",
            "title": "正向提示词编码",
            "pos": [900, 120],
            "size": [400, 150],
            "flags": {},
            "order": 5,
            "mode": 0,
            "inputs": [
                {"name": "clip", "type": "CLIP", "link": 3},
                {"name": "text", "type": "STRING", "link": 1, "widget": {"name": "text"}},
            ],
            "outputs": [
                {"name": "CONDITIONING", "type": "CONDITIONING", "links": [6], "slot_index": 0}
            ],
            "properties": {"Node name for S&R": "CLIPTextEncode"},
            "widgets_values": [""],
        },
        {
            "id": 6,
            "type": "CLIPTextEncode",
            "title": "负向提示词编码",
            "pos": [900, 310],
            "size": [400, 150],
            "flags": {},
            "order": 6,
            "mode": 0,
            "inputs": [
                {"name": "clip", "type": "CLIP", "link": 5},
                {"name": "text", "type": "STRING", "link": 2, "widget": {"name": "text"}},
            ],
            "outputs": [
                {"name": "CONDITIONING", "type": "CONDITIONING", "links": [7], "slot_index": 0}
            ],
            "properties": {"Node name for S&R": "CLIPTextEncode"},
            "widgets_values": [""],
        },
        {
            "id": 7,
            "type": "NL2PromptLoRAStack",
            "title": "LoRA 堆栈加载",
            "pos": [440, 480],
            "size": [420, 106],
            "flags": {},
            "order": 4,
            "mode": 0,
            "inputs": [
                {"name": "model", "type": "MODEL", "link": 9},
                {"name": "clip", "type": "CLIP", "link": 8},
                {"name": "lora_stack", "type": "LORA_STACK", "link": 4},
            ],
            "outputs": [
                {"name": "model", "type": "MODEL", "links": [11], "slot_index": 0},
                {"name": "clip", "type": "CLIP", "links": [], "slot_index": 1},
            ],
            "properties": {"Node name for S&R": "NL2PromptLoRAStack"},
            "widgets_values": [True, 1.0],
        },
        {
            "id": 8,
            "type": "EmptyLatentImage",
            "title": "空 Latent 1024x1024",
            "pos": [440, 620],
            "size": [420, 106],
            "flags": {},
            "order": 7,
            "mode": 0,
            "inputs": [],
            "outputs": [{"name": "LATENT", "type": "LATENT", "links": [12], "slot_index": 0}],
            "properties": {"Node name for S&R": "EmptyLatentImage"},
            "widgets_values": [1024, 1024, 1],
        },
        {
            "id": 9,
            "type": "KSampler",
            "title": "KSampler",
            "pos": [1340, 120],
            "size": [360, 262],
            "flags": {},
            "order": 8,
            "mode": 0,
            "inputs": [
                {"name": "model", "type": "MODEL", "link": 11},
                {"name": "positive", "type": "CONDITIONING", "link": 6},
                {"name": "negative", "type": "CONDITIONING", "link": 7},
                {"name": "latent_image", "type": "LATENT", "link": 12},
            ],
            "outputs": [{"name": "LATENT", "type": "LATENT", "links": [13], "slot_index": 0}],
            "properties": {"Node name for S&R": "KSampler"},
            "widgets_values": [
                KSAMPLER["seed"],
                "randomize",
                KSAMPLER["steps"],
                KSAMPLER["cfg"],
                KSAMPLER["sampler_name"],
                KSAMPLER["scheduler"],
                KSAMPLER["denoise"],
            ],
        },
        {
            "id": 10,
            "type": "VAEDecode",
            "title": "VAE 解码",
            "pos": [1740, 120],
            "size": [240, 46],
            "flags": {},
            "order": 9,
            "mode": 0,
            "inputs": [
                {"name": "samples", "type": "LATENT", "link": 13},
                {"name": "vae", "type": "VAE", "link": 10},
            ],
            "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [14], "slot_index": 0}],
            "properties": {"Node name for S&R": "VAEDecode"},
            "widgets_values": [],
        },
        {
            "id": 11,
            "type": "SaveImage",
            "title": "保存图像",
            "pos": [1740, 220],
            "size": [400, 400],
            "flags": {},
            "order": 10,
            "mode": 0,
            "inputs": [{"name": "images", "type": "IMAGE", "link": 14}],
            "outputs": [],
            "properties": {"Node name for S&R": "SaveImage"},
            "widgets_values": [prefix],
        },
    ]

    # 连线：[link_id, origin_id, origin_slot, target_id, target_slot, type]
    links = [
        [1, 4, 0, 5, 1, "STRING"],
        [2, 4, 1, 6, 1, "STRING"],
        [3, 2, 0, 5, 0, "CLIP"],
        [4, 4, 3, 7, 2, "LORA_STACK"],
        [5, 2, 0, 6, 0, "CLIP"],
        [6, 5, 0, 9, 1, "CONDITIONING"],
        [7, 6, 0, 9, 2, "CONDITIONING"],
        [8, 2, 0, 7, 1, "CLIP"],
        [9, 1, 0, 7, 0, "MODEL"],
        [10, 3, 0, 10, 1, "VAE"],
        [11, 7, 0, 9, 0, "MODEL"],
        [12, 8, 0, 9, 3, "LATENT"],
        [13, 9, 0, 10, 0, "LATENT"],
        [14, 10, 0, 11, 0, "IMAGE"],
    ]

    ui_workflow = {
        "last_node_id": 11,
        "last_link_id": 14,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {"ds_annotation": {"groups": [], "links_ontop": False}},
        "version": 0.4,
        "extra_data": {
            "ds_generator": "qa-integration/tests/gen_workflows.py",
            "ds_note": "API 格式为主（同名 .json），本文件供 ComfyUI 前端拖入打开",
            "ds_models": {
                "unet": UNET_NAME,
                "clip": CLIP_NAME,
                "vae": VAE_NAME,
            },
            "ds_title": title,
        },
    }
    return prompt, ui_workflow


def validate_ui(ui: dict) -> list:
    """UI 格式自检：连线两端存在、slot 类型匹配、必填键齐全。"""
    problems = []
    node_by_id = {n["id"]: n for n in ui["nodes"]}
    link_by_id = {l[0]: l for l in ui["links"]}

    for link_id, origin, oslot, target, tslot, ltype in ui["links"]:
        if origin not in node_by_id:
            problems.append(f"link {link_id}: 上游节点 {origin} 不存在")
        else:
            outs = node_by_id[origin].get("outputs") or []
            if oslot >= len(outs):
                problems.append(f"link {link_id}: 上游 {origin} 无输出槽 {oslot}")
            elif outs[oslot]["type"] != ltype:
                problems.append(f"link {link_id}: 上游类型 {outs[oslot]['type']} != {ltype}")
        if target not in node_by_id:
            problems.append(f"link {link_id}: 下游节点 {target} 不存在")
        else:
            ins = node_by_id[target].get("inputs") or []
            if tslot >= len(ins):
                problems.append(f"link {link_id}: 下游 {target} 无输入槽 {tslot}")
            elif ins[tslot]["type"] != ltype:
                problems.append(f"link {link_id}: 下游类型 {ins[tslot]['type']} != {ltype}")
            elif ins[tslot].get("link") != link_id:
                problems.append(
                    f"link {link_id}: 下游 {target} 输入槽 {tslot} 记录的 link={ins[tslot].get('link')}"
                )

    for n in ui["nodes"]:
        for i, out in enumerate(n.get("outputs") or []):
            for lid in out.get("links") or []:
                if lid not in link_by_id:
                    problems.append(f"节点 {n['id']} 输出槽 {i} 引用了不存在的 link {lid}")
    for node in ui["nodes"]:
        for key in ("id", "type", "pos", "size", "flags", "order", "mode", "inputs",
                    "outputs", "properties", "widgets_values"):
            if key not in node:
                problems.append(f"节点 {node.get('id')} 缺字段 {key}")
    return problems


def validate_api(prompt: dict) -> list:
    """API 格式自检：连线引用存在、ID 为字符串数字、必填 class_type。"""
    problems = []
    for nid, node in prompt.items():
        if not (isinstance(nid, str) and nid.isdigit()):
            problems.append(f"节点 id 不是字符串数字: {nid!r}")
        if "class_type" not in node:
            problems.append(f"节点 {nid} 缺 class_type")
        if not isinstance(node.get("inputs"), dict):
            problems.append(f"节点 {nid} 的 inputs 不是对象")
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in prompt:
                    problems.append(f"节点 {nid}.{key} 引用不存在的上游 {value[0]}")
    return problems


def main() -> int:
    cases = [
        ("NL2Prompt_MVP", MVP_UI_JSON, "NL2Prompt_MVP/NL_MVP", "NL2Prompt MVP（纯自然语言 + 文字替换，无 LoRA）"),
        ("NL2Prompt_WithLoRA", LORA_UI_JSON, "NL2Prompt_MVP/NL_WithLoRA", "NL2Prompt + 角色/风格 LoRA 栈"),
    ]
    failures = 0
    for name, ui_json, prefix, title in cases:
        prompt, ui = build_ui_api_pair(ui_json, prefix, title)

        api_problems = validate_api(prompt)
        ui_problems = validate_ui(ui)

        api_doc = {
            "prompt": prompt,
            "extra_data": {
                "ds_generator": "qa-integration/tests/gen_workflows.py",
                "ds_title": title,
                "ds_note": "可直接 POST http://127.0.0.1:8199/prompt（body 即本文件）",
                "ds_models": {"unet": UNET_NAME, "clip": CLIP_NAME, "vae": VAE_NAME},
                "ds_ksampler": KSAMPLER,
            },
        }

        api_path = WORKFLOWS / f"{name}.json"
        ui_path = WORKFLOWS / f"{name}.workflow.json"
        api_path.write_text(json.dumps(api_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        ui_path.write_text(json.dumps(ui, ensure_ascii=False, indent=2), encoding="utf-8")

        status = "OK" if not (api_problems or ui_problems) else "PROBLEM"
        print(f"[{status}] {name}: api={len(prompt)} 节点, ui={len(ui['nodes'])} 节点 / {len(ui['links'])} 连线")
        print(f"        {api_path.name} ({api_path.stat().st_size} B)")
        print(f"        {ui_path.name} ({ui_path.stat().st_size} B)")
        for problem in api_problems:
            print(f"        !! API: {problem}")
        for problem in ui_problems:
            print(f"        !! UI : {problem}")
        if api_problems or ui_problems:
            failures += 1

    print("\nRESULT: " + ("OK" if failures == 0 else f"{failures} PROBLEM"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
