# -*- coding: utf-8 -*-
"""v10：skill 升级为「多行分段输出 + 中文确定性映射」，并控制 system 体积。

三个替换范围（互不重叠，避免误删「零、换装识别」）：
  A. [一、输出格式, 零、换装识别)      -> 多行格式说明
  B. [二、标签顺序, 三、语言与标点)    -> 紧凑标签顺序 + 中文映射
  C. 「零、换装识别」里的 outfit 段表述 -> 「该角色的那一行」

幂等：已经含「行尾不加逗号」则跳过。
"""
import io
import json

PATH = 'plugin/skills/anima-tag-master.json'

# ---------------------------------------------------------------- A
A_START = '一、输出格式（硬约束，违反即失败）'
A_END = '零、换装识别'
A_NEW = '''一、输出格式（硬约束，违反即失败）
1. 只输出一个 JSON 对象，键固定为 positive / negative / analysis：
{"positive":"...","negative":"...","analysis":"..."}
2. **positive 是多行分段结构**（真实换行、不要空行、行序不许打乱）：
   第 1 行 质量标签；第 2 行 图片视距 + 角色数量 + 多角色的大致动作；
   第 3 行起 每个角色单独一行（角色触发词, 动作, 着装与表情）；最后一行 场景 / 背景 / 画风。
   单角色通常 3 到 4 行；多角色每人一行；信息不足可省整行，但**不许编造用户没描述的内容**。
   行内用「逗号加空格」，**行尾不加逗号**。例：
   best quality, masterpiece, highres
   full body
   castorice, standing, black_dress, long_dress, smile
   street, city, night
3. **negative 保持单行**逗号分隔（它只是压制词清单，不要换行）。
4. positive / negative 不得含中文、markdown、代码块、引号或解释文字。
5. analysis 是给用户看的中文说明，不超过 100 字（40 字内最好），讲清这轮改了什么、为什么。
6. analysis 必填；negative 必须有内容；analysis 只放分析，不写进 positive / negative。
7. 输出前后不加任何文字，不以「好的」「以下是」开头，不输出 ```json 代码块。

'''

# ---------------------------------------------------------------- B
B_START = '二、标签顺序'
B_END = '三、语言与标点'
B_NEW = '''二、标签顺序（严格按此顺序，不要打乱）
按行：质量行 -> 视距与人数行 -> 每个角色一行 -> 场景画风行。
行内：subject -> character -> outfit -> pose -> expression；场景行：scene -> camera -> lighting -> style。
正例（/ 表示换行）：masterpiece, best quality, 1girl, solo / cowboy shot / hatsune miku, serafuku, pleated skirt, standing / classroom, golden hour, @wlop
各段职责：quality 取 2 到 3 个（禁 8k、4k、uhd 等无效渲染词）；subject 先 1girl 或 1boy 再 solo；character 只放当前触发词；outfit 只放当前服装；pose 放动作姿势表情视线；scene 放地点时间天气；camera 先景别后机位；lighting 放光照氛围；style 放画风画师（@ 前缀，最末）。
严禁互斥组合：from above 与 from below；from front 与 from behind；looking at viewer 与 facing away；close-up 与 full body；upper body 与 full body；cowboy shot 与 close-up；open mouth 与 clenched teeth；standing 与 lying；solo 与 2girls 或 multiple girls；day 与 night。
同一维度只留一个（发色、瞳色同理，heterochromia、multicolored hair 例外）。重要标签放前面。

【中文确定性映射】插件已在送给你之前按固定表翻译，你看到的就是英文，**不要重复翻译或改写成别的标签**：
视距（全身照 -> full body、半身 -> upper body、特写 -> close-up、中景 -> cowboy shot、远景 -> wide shot）；
质量（高清 -> best quality, masterpiece, highres）；密度（超精细 -> highly detailed）；
人数（一个女孩 -> 1girl、两个女孩 -> 2girls、x个女角色y个男角色 -> xgirls, yboys）；
场景（城市街道 -> street, city、教室 -> classroom、海边 -> beach, ocean、森林 -> forest、夜晚 -> night、公园 -> park）；
画风（「按照某某的画风来画」-> @某某，不要翻译成描述句）。
安全 -> safe，但**一般不打进 positive**，除非用户明确要求。同维度只留一个，不要并列互斥标签。

'''

# ---------------------------------------------------------------- C
C_OLD = '''3. **加**：用户描述的新服装**必须出现在 positive 的 outfit 段**，用标准 danbooru 标签
   （黑色长裙 -> black dress, long dress）。'''
C_NEW = '''3. **加**：用户描述的新服装**必须出现在该角色的那一行里**（不是场景行），用标准 danbooru 标签
   （黑色长裙 -> black dress, long dress）。'''


def splice(text, start_anchor, end_anchor, replacement):
    start = text.find(start_anchor)
    end = text.find(end_anchor, start + 1 if start >= 0 else 0)
    if start < 0 or end < 0 or end <= start:
        return text, '锚点缺失(%s..%s: %d/%d)' % (start_anchor[:6], end_anchor[:6], start, end)
    return text[:start] + replacement + text[end:], 'ok'


def main():
    data = json.load(io.open(PATH, encoding='utf-8'))
    prompt = data['system_prompt']
    before = len(prompt)

    if '行尾不加逗号' in prompt:
        print('已是 v10 版本，跳过（%d 字符）' % before)
        return

    prompt, msg_a = splice(prompt, A_START, A_END, A_NEW)
    print('A（输出格式）:', msg_a, '->', len(prompt))
    prompt, msg_b = splice(prompt, B_START, B_END, B_NEW)
    print('B（标签顺序+映射）:', msg_b, '->', len(prompt))
    if C_OLD in prompt:
        prompt = prompt.replace(C_OLD, C_NEW, 1)
        print('C（换装 outfit 段）: ok')
    else:
        print('C（换装 outfit 段）: 锚点缺失')

    # 自检清单加一条
    old_check = '2. positive 里有没有中文、markdown、解释文字。'
    new_check = '2. positive 里有没有中文、markdown、解释文字；是否多行、行尾有没有多余逗号。'
    if old_check in prompt:
        prompt = prompt.replace(old_check, new_check, 1)
        print('D（自检清单）: ok')

    data['system_prompt'] = prompt
    io.open(PATH, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print('system_prompt: %d -> %d 字符（%+d）' % (before, len(prompt), len(prompt) - before))
    for key in ('零、换装识别', '换裙子不是换发色', '颜色/款式是否不同', '不能按换装处理',
                '行尾不加逗号', '中文确定性映射', '五、服装隔离', '一、输出格式', '二、标签顺序'):
        print('  %-20s -> %d' % (key, prompt.find(key)))


if __name__ == '__main__':
    main()
