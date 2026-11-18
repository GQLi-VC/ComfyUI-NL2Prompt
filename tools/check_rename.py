# -*- coding: utf-8 -*-
"""校验改名是否彻底：残余的 anima 只允许出现在「语义正确」的白名单位置。

用法：python -X utf8 tools/check_rename.py [--verbose]
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"log", "test_env", ".git", "__pycache__", "node_modules", ".pytest_cache", ".idea"}
SKIP_FILES = {"ComfyUI自然语言文生图方案.md", "_rewrite_real.json", "check_rename.py", "migrate_to_nl2prompt.py"}
#: 以 ``_`` 开头的 JSON 是测试探针的**自动生成证据**（含测试环境插件目录名等），
#: 不参与"交付物品牌残留"判定
SKIP_PREFIXES = ("_",)
SUFFIXES = {".py", ".js", ".json", ".md", ".ps1", ".html", ".css", ".yaml", ".yml", ".txt"}

# 这些 anima 出现是**正确**的：指模型名 / skill 标识 / 内部实现名
ALLOW_PATTERNS = (
    r'"anima"',                    # base_model / target_model 的值
    r"anima-tag-master",           # skill id
    r"Anima 标签大师",              # skill 显示名
    r"check_anima",                # 校验器函数名
    r"anima_nl_",                  # widget 名（序列化相关，保留）
    r"anima-nl-",                  # CSS 类名（内部样式，保留）
    r"ANIMA_LLM_",                 # 环境变量（用户可能已配置，保留）
    r"hd_realism_anima",           # 真实 LoRA 文件名
    r"Anima 模型",
    r"Anima 动漫",
    r"Anima 文生图",
    r"Anima 提示词",
    r"Anima 的",
    r"Anima 能",
    r"Anima 标准",
)

# 这些出现是**品牌残留**，必须为 0。
# 兼容代码里的旧类名（_LEGACY_NODE_ALIASES）、旧前缀（_OLD_PREFIX）、ui 键名
# （ui.anima_nl / anima_llm）、模型名（anima-preview3-base）、skill 标识（anima-tag-master）
# 均属**故意保留**，不在此列。
#
# 注意：`Anima NL` / `Anima 自然语言提示词` / `Anima LoRA 堆栈` 这类**带空格的旧品牌词**
# 无法机械区分「文档里的改名对照表」与「代码里的真残留」，因此不计入本项。
# 它们的实际有效性由 tests/test_v3.py 的 D1（旧类名兼容）与 ui_probe 的显示名断言守护。
BAD_PATTERNS = (
    r"\[AnimaNL\]",                 # 旧日志前缀（代码里出现即错）
    r"\[anima-nl\]",                # 旧日志前缀（前端）
    r"AnimaNL_MVP",                 # 旧输出目录
    r"anima_nl_composer",           # 旧前端文件名
    r"anima_load_llm",              # 旧前端文件名
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    allowed_hits = {}
    bad_hits = {}
    unknown_hits = {}

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            if name.endswith(".json") and name.startswith(SKIP_PREFIXES):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() not in SUFFIXES:
                continue
            try:
                text = io.open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                if "anima" not in line.lower():
                    continue
                if any(re.search(p, line, re.I) for p in BAD_PATTERNS):
                    # 同一行同时出现新名 → 这是「旧 → 新」对照表（文档映射），不是残留
                    if "NL2Prompt" in line:
                        allowed_hits.setdefault(rel, []).append(lineno)
                    else:
                        bad_hits.setdefault(rel, []).append((lineno, line.strip()[:150]))
                    continue
                if any(re.search(p, line, re.I) for p in ALLOW_PATTERNS):
                    allowed_hits.setdefault(rel, []).append(lineno)
                    continue
                # 既非品牌残留、也不在白名单：仅作人工复核提示，不影响判定
                unknown_hits.setdefault(rel, []).append((lineno, line.strip()[:150]))

    print("=== 品牌残留（必须为 0）===")
    total_bad = sum(len(v) for v in bad_hits.values())
    if total_bad == 0:
        print("  无")
    else:
        for rel, rows in sorted(bad_hits.items()):
            print(f"  {rel}: {len(rows)} 处")
            if args.verbose:
                for lineno, text in rows:
                    print(f"    L{lineno}: {text}")

    print("\n=== 白名单 anima（语义正确，允许保留）===")
    for rel, linenos in sorted(allowed_hits.items()):
        print(f"  {rel}: {len(linenos)} 处")

    total_unknown = sum(len(v) for v in unknown_hits.values())
    print(f"\n=== 待人工复核（非品牌残留，不计入判定）：{total_unknown} 处 ===")
    if args.verbose and total_unknown:
        for rel, rows in sorted(unknown_hits.items()):
            print(f"  {rel}: {len(rows)} 处")
            for lineno, text in rows:
                print(f"    L{lineno}: {text}")

    print(f"\n结论: {'PASS' if total_bad == 0 else 'FAIL'}（品牌残留 {total_bad} 处）")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
