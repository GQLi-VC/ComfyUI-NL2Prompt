# -*- coding: utf-8 -*-
"""生成 v3 直连示例工作流：Composer 的 CONDITIONING 直接接 KSampler。

精简版：不含 LoRA 堆栈节点（LoRA 是可选能力，需要时自己加一个
`NL2PromptLoRAStack`，把 Composer 的 `lora_stack` 输出接进去即可）。

运行：python tools/gen_v3_direct_workflow.py
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "workflows"

UNET_NAME = "anima-preview3-base.safetensors"
CLIP_NAME = "Qwen3-06B-Base.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
NL_TEXT = "银发少女站在雨后的霓虹街道上，赛博朋克风格，电影感打光"

nodes = [
    {
        "id": 1, "type": "UNETLoader",
        "pos": [60, 120], "size": [340, 82], "flags": {}, "order": 0, "mode": 0,
        "inputs": [],
        "outputs": [{"name": "MODEL", "type": "MODEL", "links": [7], "slot_index": 0}],
        "properties": {"Node name for S&R": "UNETLoader"},
        "widgets_values": [UNET_NAME, "default"],
    },
    {
        "id": 2, "type": "CLIPLoader",
        "pos": [60, 250], "size": [340, 106], "flags": {}, "order": 1, "mode": 0,
        "inputs": [],
        "outputs": [{"name": "CLIP", "type": "CLIP", "links": [1], "slot_index": 0}],
        "properties": {"Node name for S&R": "CLIPLoader"},
        "widgets_values": [CLIP_NAME, "stable_diffusion", "default"],
    },
    {
        "id": 6, "type": "EmptyLatentImage",
        "pos": [60, 400], "size": [340, 106], "flags": {}, "order": 2, "mode": 0,
        "inputs": [],
        "outputs": [{"name": "LATENT", "type": "LATENT", "links": [2], "slot_index": 0}],
        "properties": {"Node name for S&R": "EmptyLatentImage"},
        "widgets_values": [1024, 1024, 1],
    },
    {
        "id": 4, "type": "NL2PromptComposer",
        "pos": [470, 120], "size": [480, 560], "flags": {}, "order": 3, "mode": 0,
        "inputs": [
            {"name": "llm", "type": "LLM_MODEL", "link": None},
            {"name": "clip", "type": "CLIP", "link": 1},
            {"name": "model", "type": "MODEL", "link": None},
        ],
        "outputs": [
            {"name": "positive", "type": "STRING", "links": [], "slot_index": 0},
            {"name": "negative", "type": "STRING", "links": [], "slot_index": 1},
            {"name": "text", "type": "STRING", "links": [], "slot_index": 2},
            {"name": "lora_stack", "type": "LORA_STACK", "links": [], "slot_index": 3},
            {"name": "debug", "type": "STRING", "links": [], "slot_index": 4},
            {"name": "positive_cond", "type": "CONDITIONING", "links": [5], "slot_index": 5},
            {"name": "negative_cond", "type": "CONDITIONING", "links": [6], "slot_index": 6},
            {"name": "model", "type": "MODEL", "links": [], "slot_index": 7},
        ],
        "properties": {"Node name for S&R": "NL2PromptComposer"},
        "widgets_values": [NL_TEXT, False, "{}"],
    },
    {
        "id": 3, "type": "KSampler",
        "pos": [1030, 120], "size": [320, 262], "flags": {}, "order": 4, "mode": 0,
        "inputs": [
            {"name": "model", "type": "MODEL", "link": 7},
            {"name": "positive", "type": "CONDITIONING", "link": 5},
            {"name": "negative", "type": "CONDITIONING", "link": 6},
            {"name": "latent_image", "type": "LATENT", "link": 2},
        ],
        "outputs": [{"name": "LATENT", "type": "LATENT", "links": [4], "slot_index": 0}],
        "properties": {"Node name for S&R": "KSampler"},
        "widgets_values": [156680208700286, "randomize", 30, 4.0, "euler", "simple", 1.0],
    },
    {
        "id": 7, "type": "VAELoader",
        "pos": [1030, 430], "size": [320, 60], "flags": {}, "order": 5, "mode": 0,
        "inputs": [],
        "outputs": [{"name": "VAE", "type": "VAE", "links": [3], "slot_index": 0}],
        "properties": {"Node name for S&R": "VAELoader"},
        "widgets_values": [VAE_NAME],
    },
    {
        "id": 8, "type": "VAEDecode",
        "pos": [1400, 120], "size": [210, 46], "flags": {}, "order": 6, "mode": 0,
        "inputs": [
            {"name": "samples", "type": "LATENT", "link": 4},
            {"name": "vae", "type": "VAE", "link": 3},
        ],
        "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [8], "slot_index": 0}],
        "properties": {"Node name for S&R": "VAEDecode"},
        "widgets_values": [],
    },
    {
        "id": 9, "type": "SaveImage",
        "pos": [1660, 120], "size": [360, 330], "flags": {}, "order": 7, "mode": 0,
        "inputs": [{"name": "images", "type": "IMAGE", "link": 8}],
        "outputs": [],
        "properties": {"Node name for S&R": "SaveImage"},
        "widgets_values": ["NL2Prompt_v3"],
    },
]

# [link_id, origin_id, origin_slot, target_id, target_slot, type]
links = [
    [1, 2, 0, 4, 1, "CLIP"],
    [2, 6, 0, 3, 3, "LATENT"],
    [3, 7, 0, 8, 1, "VAE"],
    [4, 3, 0, 8, 0, "LATENT"],
    [5, 4, 5, 3, 1, "CONDITIONING"],
    [6, 4, 6, 3, 2, "CONDITIONING"],
    [7, 1, 0, 3, 0, "MODEL"],
    [8, 8, 0, 9, 0, "IMAGE"],
]

ui = {
    "last_node_id": 9,
    "last_link_id": 8,
    "nodes": nodes,
    "links": links,
    "groups": [],
    "config": {},
    "extra": {"ds_annotation": {"groups": [], "links_ontop": False}},
    "version": 0.4,
    "extra_data": {
        "ds_generator": "tools/gen_v3_direct_workflow.py",
        "ds_note": "v3 直连示例：Composer 的 positive_cond / negative_cond 直接接 KSampler，无需 CLIPTextEncode。"
                   "需要角色/画师 LoRA 时，自行加一个 NL2PromptLoRAStack 节点，"
                   "把 Composer 的 lora_stack 输出接进去，再把它的 model / clip 分别接到 KSampler 和 Composer。",
        "ds_models": {"unet": UNET_NAME, "clip": CLIP_NAME, "vae": VAE_NAME},
        "ds_title": "NL2Prompt v3 直连示例",
    },
}

OUT.mkdir(parents=True, exist_ok=True)
p = OUT / "NL2Prompt_v3_Direct.workflow.json"
p.write_text(json.dumps(ui, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已生成: {p}")
print(f"  节点 {len(nodes)} 个，连线 {len(links)} 条")
print("  对比旧 MVP（11 节点）：少了 2 个 CLIPTextEncode + 1 个 LoRAStack")
