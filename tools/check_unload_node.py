# -*- coding: utf-8 -*-
"""check_unload_node.py — NL2PromptUnloadModels 自检。

覆盖：
1. 节点契约：INPUT_TYPES（required 空 / optional 有 any_input）、RETURN_TYPES == ("*",)
2. **原样透传**：`unload(None)` / `unload("test")` / dict / 数字 都等于输入
3. 卸载副作用：调用了 unload_all_models 与 soft_empty_cache（用假 mm 模块验证）
4. 卸载失败也透传：假 mm 抛异常时返回值仍等于输入
5. IS_CHANGED 返回 NaN（强制每次都执行，不被执行缓存跳过）
6. 注册：__init__ 的 _EXPECTED_NODES 含该节点，且 nodes 模块能取到类

用法：``python tools/check_unload_node.py``
"""
from __future__ import annotations

import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'plugin'))

import nodes  # noqa: E402
import nodes_unload  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def section(title):
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


class _FakeMM(types.ModuleType):
    """假的 comfy.model_management，记录调用并按剧本返回/抛异常。"""

    def __init__(self, raise_on_unload=False, start_loaded=3):
        super().__init__("comfy.model_management")
        self.calls = []
        self.raise_on_unload = raise_on_unload
        self.current_loaded_models = [object() for _ in range(start_loaded)]

    def unload_all_models(self):
        self.calls.append("unload_all_models")
        if self.raise_on_unload:
            raise RuntimeError("fake unload failure")
        self.current_loaded_models = []

    def soft_empty_cache(self):
        self.calls.append("soft_empty_cache")

    def cleanup_models(self):
        self.calls.append("cleanup_models")


def _install_fake_mm(fake):
    """把假模块塞进 sys.modules 的 comfy.model_management。"""
    comfy = sys.modules.get("comfy") or types.ModuleType("comfy")
    real_mm = getattr(comfy, "model_management", None)
    comfy.model_management = fake
    sys.modules["comfy"] = comfy
    sys.modules["comfy.model_management"] = fake
    return real_mm


def _restore_mm(real_mm):
    if real_mm is None:
        sys.modules.pop("comfy.model_management", None)
    else:
        sys.modules["comfy.model_management"] = real_mm
        comfy = sys.modules.get("comfy")
        if comfy is not None:
            comfy.model_management = real_mm


def main():
    node_class = getattr(nodes, "NL2PromptUnloadModels", None)

    section("1. 节点契约")
    check("nodes 模块能取到 NL2PromptUnloadModels", node_class is not None)
    if node_class is None:
        print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
        return 1

    spec = node_class.INPUT_TYPES()
    check("INPUT_TYPES.required 为空", spec.get("required") == {}, str(spec.get("required")))
    optional = spec.get("optional") or {}
    check("optional 含 any_input", "any_input" in optional, str(optional))
    check("any_input 类型为通配 *",
          optional.get("any_input") == ("*",), str(optional.get("any_input")))
    check('RETURN_TYPES == ("*",)', tuple(node_class.RETURN_TYPES) == ("*",),
          str(node_class.RETURN_TYPES))
    check("RETURN_NAMES == ('any_output',)",
          tuple(node_class.RETURN_NAMES) == ("any_output",), str(node_class.RETURN_NAMES))
    check("FUNCTION == 'unload'", node_class.FUNCTION == "unload", node_class.FUNCTION)
    check("CATEGORY == 'NL2Prompt'", node_class.CATEGORY == "NL2Prompt", node_class.CATEGORY)

    section("2. 原样透传（输入什么输出什么）")
    payloads = [
        ("None", None),
        ("字符串", "test"),
        ("中文", "遐蝶穿着长裙"),
        ("整数", 42),
        ("浮点", 3.14),
        ("布尔 False", False),
        ("空列表", []),
        ("dict", {"positive": "a, b", "n": 1}),
        ("元组", (1, "x")),
    ]
    fake = _FakeMM()
    real_mm = _install_fake_mm(fake)
    try:
        node = node_class()
        for label, value in payloads:
            out = node.unload(value)
            ok = (isinstance(out, tuple) and len(out) == 1 and out[0] is value)
            check("透传 %s" % label, ok, repr(out))
        # 不传参（可选输入未连接）
        out = node.unload()
        check("不传参时输出 (None,)", out == (None,) and len(out) == 1, repr(out))
    finally:
        _restore_mm(real_mm)

    section("3. 卸载副作用（标准 API 组合）")
    fake = _FakeMM(start_loaded=3)
    real_mm = _install_fake_mm(fake)
    try:
        before = len(fake.current_loaded_models)
        node_class().unload("x")
        check("调用了 unload_all_models", "unload_all_models" in fake.calls, str(fake.calls))
        check("调用了 soft_empty_cache", "soft_empty_cache" in fake.calls, str(fake.calls))
        check("卸载后模型列表为空", len(fake.current_loaded_models) == 0,
              str(len(fake.current_loaded_models)))
        check("卸载前确有已加载模型（用例有效）", before == 3, str(before))
    finally:
        _restore_mm(real_mm)

    section("4. 卸载失败也必须原样透传")
    fake = _FakeMM(raise_on_unload=True)
    real_mm = _install_fake_mm(fake)
    try:
        out = node_class().unload("must-pass-through")
        check("异常时仍透传输入", out == ("must-pass-through",), repr(out))
        check("异常时尝试过 unload_all_models",
              "unload_all_models" in fake.calls, str(fake.calls))
    finally:
        _restore_mm(real_mm)

    section("5. IS_CHANGED 强制每次执行")
    value = node_class.IS_CHANGED()
    check("IS_CHANGED 返回 NaN（恒不相等 -> 每次执行）",
          isinstance(value, float) and math.isnan(value), repr(value))
    check("NaN 自比较为 False（ComfyUI 据此判定已变化）", not (value == value))

    section("6. 注册")
    import importlib
    import importlib.util

    init_path = os.path.join(ROOT, 'plugin', '__init__.py')
    spec_obj = importlib.util.spec_from_file_location('nl2p_init_probe', init_path)
    module = importlib.util.module_from_spec(spec_obj)
    try:
        spec_obj.loader.exec_module(module)
        names = [pair[0] for pair in module._EXPECTED_NODES]
        check("_EXPECTED_NODES 含 NL2PromptUnloadModels",
              "NL2PromptUnloadModels" in names, str(names))
        display = dict(module._EXPECTED_NODES).get("NL2PromptUnloadModels", "")
        check("中文显示名含「卸载」", "卸载" in display, display)
        check("NODE_CLASS_MAPPINGS 已注册该节点",
              "NL2PromptUnloadModels" in module.NODE_CLASS_MAPPINGS,
              str(sorted(module.NODE_CLASS_MAPPINGS)))
        check("已注册节点类就是本文件的类",
              module.NODE_CLASS_MAPPINGS.get("NL2PromptUnloadModels") is node_class)
        # 其余三个老节点没被影响
        for old in ("NL2PromptComposer", "NL2PromptLoRAStack"):
            check("老节点 %s 仍在" % old, old in module.NODE_CLASS_MAPPINGS)
    except Exception as exc:  # noqa: BLE001
        check("__init__ 可加载", False, "%s: %s" % (type(exc).__name__, exc))

    section("7. GBK 安全（日志字符串）")
    src = open(os.path.join(ROOT, 'plugin', 'nodes_unload.py'), encoding='utf-8').read()
    risky = []
    for char in src:
        try:
            char.encode("gbk")
        except UnicodeEncodeError:
            risky.append(char)
    check("nodes_unload.py 无非 GBK 字符", not risky, str(sorted(set(risky))[:8]))

    print()
    print("=" * 70)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
