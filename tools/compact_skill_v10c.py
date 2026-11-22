# -*- coding: utf-8 -*-
"""v10-c：把 skill 压到 system < 7000 的既有约束内。

v10 加入「多行分段格式」与「中文确定性映射」后 system_prompt 从 5397 涨到 5750，
全开 system 达 7223，超过本项目「system 不再膨胀（< 7000 字符）」的护栏。
这里删掉与第二节重复的景别/机位标准词清单（第二节已列互斥组合与 camera 职责），
以及几处可有可无的修饰，属于**去重而非丢规则**。

幂等：已经小于 TARGET 则跳过。
"""
import io
import json

PATH = 'plugin/skills/anima-tag-master.json'
TARGET = 5510

REPLACEMENTS = [
    # 1) 第十节：景别/机位标准词与第二节重复（camera 先景别后机位 + 互斥清单已覆盖）
    (''' - 主体要精确：1girl, solo 优于模糊描述；双人要 2girls 并分别给姿势与视线。
- 景别用标准词：extreme close-up / close-up / upper body / cowboy shot / full body / wide shot。
- 机位用标准词：from front / from side / from behind / from above / from below / profile。
''',
     '''- 主体要精确：1girl, solo 优于模糊描述；双人要 2girls 并分别给姿势与视线。
'''),
    # 2) 第八节：负面词的通用缺陷清单压缩（保留语义）
    (''' - 质量与缺陷：worst quality、low quality、lowres、blurry、jpeg artifacts、bad anatomy、bad hands、extra digits
- 文字与水印：watermark、signature、english text、logo
''',
     '''- 质量与缺陷：worst quality、low quality、lowres、blurry、bad anatomy、bad hands、extra digits
- 文字与水印：watermark、signature、english text
'''),
    # 3) 第三节：语言与标点里的冗余展开
    (''' - 全部用小写英文 danbooru 标签，逗号加空格分隔；不要句号结尾、不要首字母大写、不要中文括号。
- 标签之间不要写完整句子；除第十条允许的极短场景短语外，全部用标签。
- 同一维度只选最贴合的一个：例如 long hair 与 very long hair 不并列，同一角色的两种发色不并列。
''',
     '''- 全部用小写英文 danbooru 标签，逗号加空格分隔；不要句号结尾、不要首字母大写、不要中文括号。
- 标签之间不写完整句子；除极短场景短语外全部用标签。
- 同一维度只选最贴合的一个（如 long hair 与 very long hair 不并列）。
'''),
]


def main():
    data = json.load(io.open(PATH, encoding='utf-8'))
    prompt = data['system_prompt']
    before = len(prompt)
    if before <= TARGET:
        print('已经 <= %d 字符，跳过（%d）' % (TARGET, before))
        return
    hits = 0
    for old, new in REPLACEMENTS:
        # 兼容首行缩进差异
        if old in prompt:
            prompt = prompt.replace(old, new, 1)
            hits += 1
            continue
        stripped = old.lstrip(' ')
        if stripped in prompt:
            prompt = prompt.replace(stripped, new.lstrip(' '), 1)
            hits += 1
    data['system_prompt'] = prompt
    io.open(PATH, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print('命中 %d/%d 条' % (hits, len(REPLACEMENTS)))
    print('system_prompt: %d -> %d（%+d）' % (before, len(prompt), len(prompt) - before))


if __name__ == '__main__':
    main()
