# -*- coding: utf-8 -*-
"""v10：把 skill 的 3 个 examples 改成多行分段结构（LLM few-shot 会照抄格式）。"""
import io
import json

PATH = 'plugin/skills/anima-tag-master.json'

EXAMPLES = [
    # 遐蝶 / 花田
    ('masterpiece, best quality',
     '1girl, solo, cowboy shot',
     'castorice, purple hair, colored inner hair, twintails, purple eyes, pointy ears, '
     'black dress, long dress, standing, looking at viewer',
     'flower field, purple flower, soft lighting'),
    # 雨夜霓虹
    ('masterpiece, best quality',
     '1girl, solo, full body',
     'white hair, long hair, black coat, long coat, standing',
     'night, city, neon lights, rain, wet, street, puddle, reflection, '
     'from below, backlighting, sidelighting, lens flare, depth of field, @wlop'),
    # 初音 / 教室
    ('masterpiece, best quality',
     '1girl, solo, cowboy shot',
     'hatsune miku, blue hair, twintails, serafuku, white shirt, sailor collar, '
     'pleated skirt, necktie, standing, looking at viewer',
     'classroom, window, indoors, from side, golden hour, sunlight, depth of field'),
]


def main():
    data = json.load(io.open(PATH, encoding='utf-8'))
    examples = data.get('examples') or []
    print('examples:', len(examples))
    for index, item in enumerate(examples):
        if index >= len(EXAMPLES):
            break
        lines = EXAMPLES[index]
        item['positive'] = '\n'.join(line for line in lines if line)
        print('example %d -> %d 行' % (index, len(lines)))
    io.open(PATH, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print('已写入', PATH)


if __name__ == '__main__':
    main()
