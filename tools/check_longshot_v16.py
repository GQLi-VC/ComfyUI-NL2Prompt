# -*- coding: utf-8 -*-
"""check_longshot_v16.py — v16-B 长句转化覆盖自检。

覆盖 20 条不同风格的真实长句（动作/多角色/天气/情绪/空间/复合场景/科幻/校园/
宫崎骏/赛博朋克/战场/和风/教堂/夜市/咖啡厅/冬季/海边/实验室）。

断言：
1. ``zh_unmapped`` 全部清零（虚词已被忽略表剔除，剩下的必须真的识别了）；
2. 每句至少有 1 个标签被翻译出来（防「静默丢失」）；
3. 关键组合词命中（``abandoned factory`` / ``stained glass`` 等）；
4. 不编造：用户没说的角色特征（发色/瞳色）不得出现；
5. 多行结构保持（每个角色一行 + 场景行）。

用法：``python tools/check_longshot_v16.py``
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'plugin'))

import nodes  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def section(title):
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


def make_ui():
    return {
        'schema': 1, 'character_rows': [], 'style_rows': [], 'text_map': [],
        'system_prompt': '', 'negative_base': '',
        'sampling': {'top_k': 40, 'top_p': 0.9, 'seed': -1},
        'ui': {'skill_id': 'anima-tag-master'},
    }


#: (标签, 长句, 必须出现的标签)
SAMPLES = [
    ("动作复杂", "她转过身，裙摆扬起，风吹乱了头发", ["wind"]),
    ("多角色互动", "两个人背靠背坐着，一个看书一个弹琴",
     ["back-to-back", "reading book"]),
    ("时间天气", "黄昏时分，夕阳把街道染成橘红色，远处有飞鸟",
     ["sunset", "bird"]),
    ("情绪细腻", "眼神里带着一丝忧郁，嘴角却是笑着的", ["sad", "smile"]),
    ("空间关系", "站在阳台边，透过玻璃看着楼下的雨", ["balcony", "rain"]),
    ("复合场景", "废弃的工厂里长满了藤蔓，阳光从破损的屋顶漏下来",
     ["abandoned factory", "vines"]),
    ("现代科幻", "霓虹灯牌闪烁，地面反射着水光，远处有飞行器",
     ["neon signs", "aircraft"]),
    ("校园", "教室里只有她一个人，黑板上还留着没擦干净的板书",
     ["classroom", "blackboard"]),
    ("动作复杂2", "她低头整理手套，指尖微微发抖，然后抬起头露出微笑",
     ["looking down", "trembling", "smile"]),
    ("天气细节", "细雨打湿了薄雾中的石板路，路灯的光晕在积水里摇晃",
     ["light rain", "mist", "streetlight"]),
    ("宫崎骏风", "少女站在长满青草的山坡上，远处是缓缓飘动的白云和湛蓝的天空",
     ["hill", "cloud", "blue sky"]),
    ("赛博朋克", "高耸的建筑遮蔽了天空，全息广告牌在雨中闪烁，机械义体泛着冷光",
     ["building", "holographic", "rain"]),
    ("战场", "硝烟弥漫的废墟上，残破的旗帜在风中飘动，远处传来爆炸声",
     ["smoke", "ruins", "flag"]),
    ("和风", "樱花飘落的庭园里，她跪坐在榻榻米上，手边放着一把折扇",
     ["cherry blossoms", "garden", "tatami"]),
    ("教堂", "阳光透过彩绘玻璃窗洒进空旷的教堂，尘埃在光柱中漂浮",
     ["stained glass", "church", "dust"]),
    ("夜市", "热闹的夜市街头，灯笼高高挂起，摊位上冒着热气",
     ["night market", "lantern"]),
    ("咖啡厅", "靠窗的座位上，她一手托腮一手翻着书，杯中咖啡还冒着热气",
     ["coffee"]),
    ("冬季", "大雪纷飞的深夜，她裹着厚厚的围巾，呼出的白气在路灯下消散",
     ["snowing", "late night", "scarf"]),
    ("海边黄昏", "海浪拍打着礁石，夕阳的余晖把海面染成金红色，海鸥掠过低空",
     ["waves", "rock", "ocean", "seagull"]),
    ("科幻实验室", "冰冷的金属舱室内，蓝色光带沿着墙壁延展，仪器发出规律的滴答声",
     ["metal", "wall"]),
]


def main():
    section("1. zh_unmapped 清零（虚词被忽略表剔除后，剩余必须真的识别）")
    for label, text, _expected in SAMPLES:
        core = nodes._compose_core(text, make_ui())
        unmapped = core['debug'].get('zh_unmapped')
        check("%s 无未映射残片" % label, not unmapped, str(unmapped))
    print("  （上面若全静默即全部通过）")

    section("2. 每句至少翻译出标签（防静默丢失）")
    for label, text, _expected in SAMPLES:
        core = nodes._compose_core(text, make_ui())
        positive = core['positive']
        tags = [t.strip() for line in positive.split('\n')
                for t in line.split(',') if t.strip()]
        check("%s 有标签输出" % label, len(tags) >= 3, str(tags[:6]))

    section("3. 关键组合词命中（接受库名归一后的写法）")
    # 注意：cn_tags 产出的是「自然写法」（blackboard / mist / streetlight），
    # `_normalize_tag_names` 会把它们换成 danbooru 标准名
    # （chalkboard / fog / lamppost / stone_walkway）。两者都算命中。
    missing = []
    for label, text, expected in SAMPLES:
        core = nodes._compose_core(text, make_ui())
        positive = core['positive'].lower().replace('_', ' ')
        mapped = " ".join(core['debug'].get('cn_mapped') or ()).lower()
        for tag in expected:
            ok = tag.lower() in positive or tag.lower() in mapped
            if not ok:
                missing.append("%s/%s" % (label, tag))
            check("%s 含 %s" % (label, tag), ok, positive[:110])
    if not missing:
        print("  （全部命中）")

    section("4. 不编造角色特征（用户没说发色/瞳色）")
    for label, text, _expected in SAMPLES:
        core = nodes._compose_core(text, make_ui())
        tags = [t.strip().lower() for line in core['positive'].split('\n')
                for t in line.split(',') if t.strip()]
        invented = [t for t in tags
                    if (t.endswith(' hair') or t.endswith(' eyes')
                        or 'hair' in t and 'ornament' not in t)
                    and t not in ('hair',)]
        check("%s 无编造发色/瞳色" % label, not invented, str(invented))

    section("5. 多行结构")
    core = nodes._compose_core(SAMPLES[5][1], make_ui())   # 复合场景
    lines = [line for line in core['positive'].split('\n') if line.strip()]
    check("positive 至少 3 行（质量/人数/场景）", len(lines) >= 3, str(len(lines)))
    check("第 1 行是质量词", 'quality' in lines[0].lower(), lines[0][:60])
    check("第 2 行含人数", 'girl' in lines[1].lower() or 'boy' in lines[1].lower(),
          lines[1][:60])

    section("6. 性能（20 条长句总耗时）")
    import time
    started = time.time()
    for _label, text, _expected in SAMPLES:
        nodes._compose_core(text, make_ui())
    total_ms = (time.time() - started) * 1000
    check("20 句总耗时 < 3000ms", total_ms < 3000, "%.0f ms" % total_ms)
    print("  实际 %.0f ms（平均 %.1f ms/句）" % (total_ms, total_ms / len(SAMPLES)))

    print()
    print("=" * 70)
    print("汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
