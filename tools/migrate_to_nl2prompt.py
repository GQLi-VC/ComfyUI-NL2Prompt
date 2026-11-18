# -*- coding: utf-8 -*-
"""阶段 0 迁移：Anima NL -> NL2Prompt（一次性、幂等、可 dry-run）。

只把**用户可见的品牌名**改掉，下面这些内部标识**故意保留**（语义正确或改动风险大于收益）：
  * ``base_model: "anima"`` / ``target_model: "anima"``  —— 指 Anima 模型，不是插件名
  * skill id ``anima-tag-master`` / ``Anima 标签大师``    —— skill 是给 Anima 写的
  * ``check_anima()``                                    —— 校验 Anima 提示词语义
  * widget 名 ``anima_nl_*``                              —— 工作流序列化相关，测试断言大量引用
  * CSS 类 ``anima-nl-*``                                 —— 纯内部样式
  * 环境变量 ``ANIMA_LLM_*``                              —— 用户可能已配置
  * ``hd_realism_anima_v7c.safetensors``                  —— 真实文件名

用法：
    python -X utf8 tools/migrate_to_nl2prompt.py            # dry-run，只报告
    python -X utf8 tools/migrate_to_nl2prompt.py --apply    # 实跑
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {"log", "test_env", ".git", "__pycache__", "node_modules", ".pytest_cache", ".idea"}
SKIP_FILES = {
    "ComfyUI自然语言文生图方案.md",  # 用户任务书，只读
    "_rewrite_real.json",           # 实测证据
    "migrate_to_nl2prompt.py",      # 本脚本
}

# 顺序敏感：长串在前、带空格的形式在前
RULES = (
    ("AnimaNLComposer", "NL2PromptComposer"),
    ("AnimaLoRAStackLoader", "NL2PromptLoRAStack"),
    ("AnimaLoadLLM", "NL2PromptLoadLLM"),
    ("AnimaNL.", "NL2Prompt."),
    ("Anima 自然语言提示词", "自然语言提示词"),
    ("Anima LoRA 堆栈加载", "LoRA 堆栈加载"),
    ("Anima NL 插件", "NL2Prompt 插件"),
    ("Anima NL", "NL2Prompt"),
    ("[AnimaNL]", "[NL2Prompt]"),
    ("[anima-nl]", "[nl2prompt]"),
    ("/nl2prompt/", "/nl2prompt/"),
    ("anima_nl_composer", "nl2prompt_composer"),
    ("anima_load_llm", "nl2prompt_load_llm"),
    ("Anima_NL_", "NL2Prompt_"),
)

RENAMES = (
    ("plugin/web/nl2prompt_composer.js", "plugin/web/nl2prompt_composer.js"),
    ("plugin/web/anima_load_llm.js", "plugin/web/nl2prompt_load_llm.js"),
    ("workflows/Anima_NL_MVP.json", "workflows/NL2Prompt_MVP.json"),
    ("workflows/Anima_NL_MVP.workflow.json", "workflows/NL2Prompt_MVP.workflow.json"),
    ("workflows/Anima_NL_WithLoRA.json", "workflows/NL2Prompt_WithLoRA.json"),
    ("workflows/Anima_NL_WithLoRA.workflow.json", "workflows/NL2Prompt_WithLoRA.workflow.json"),
)

TEXT_SUFFIXES = {".py", ".js", ".json", ".md", ".ps1", ".html", ".css", ".yaml", ".yml", ".txt", ".gitignore"}


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if name in SKIP_FILES:
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and name != ".gitignore":
                continue
            yield path


def transform(text):
    out = text
    hits = {}
    for old, new in RULES:
        count = out.count(old)
        if count:
            out = out.replace(old, new)
            hits[old] = hits.get(old, 0) + count
    return out, hits


def describe_lines(path, text):
    """列出该文件里将被改动的行（用于人工核对）。"""
    rows = []
    for lineno, line in enumerate(text.splitlines(), 1):
        new_line = line
        hit_rules = []
        for old, new in RULES:
            if old in new_line:
                new_line = new_line.replace(old, new)
                hit_rules.append(old)
        if hit_rules:
            rows.append((lineno, line.strip(), new_line.strip(), hit_rules))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    parser.add_argument("--show", default="", help="只显示该文件（相对路径）将改动的行")
    args = parser.parse_args()

    if args.show:
        target = ROOT / args.show
        text = io.open(target, encoding="utf-8").read()
        rows = describe_lines(target, text)
        print(f"=== {args.show}：{len(rows)} 行将改动 ===")
        for lineno, old_line, new_line, rules in rows:
            old_txt = old_line if len(old_line) <= 160 else old_line[:160] + "…"
            print(f"L{lineno} {sorted(set(rules))}")
            print(f"   - {old_txt}")
            if old_line != new_line:
                new_txt = new_line if len(new_line) <= 160 else new_line[:160] + "…"
                print(f"   + {new_txt}")
        return 0

    total_files = 0
    total_hits = 0
    per_rule = {}
    changed_files = []

    for path in iter_files():
        try:
            text = io.open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        new_text, hits = transform(text)
        if not hits:
            continue
        total_files += 1
        total_hits += sum(hits.values())
        for rule, count in hits.items():
            per_rule[rule] = per_rule.get(rule, 0) + count
        changed_files.append((path.relative_to(ROOT).as_posix(), sum(hits.values())))
        if args.apply:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)

    renamed = []
    for old, new in RENAMES:
        src = ROOT / old
        dst = ROOT / new
        if src.is_file():
            if args.apply:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            renamed.append((old, new))

    print("模式:", "APPLY" if args.apply else "DRY-RUN")
    print("待改文件数:", total_files, " 替换处数:", total_hits)
    print("\n--- 按规则 ---")
    for rule, count in sorted(per_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5d}  {rule!r}")
    print("\n--- 按文件（前 40）---")
    for rel, count in sorted(changed_files, key=lambda kv: -kv[1])[:40]:
        print(f"  {count:5d}  {rel}")
    print("\n--- 文件改名 ---")
    for old, new in renamed:
        print(f"  {old} -> {new}")
    if not args.apply:
        print("\n（dry-run，未写盘；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
