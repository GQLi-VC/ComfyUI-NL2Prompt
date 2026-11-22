# -*- coding: utf-8 -*-
"""中文 -> danbooru 标签的**确定性映射**（不经过 LLM、不联网、无第三方依赖）。

由来
----
用户反馈：AI 写的提示词有时不精确，希望用数据库精确化；并且明确了
「中文转译要按确定性映射，不是靠 LLM 猜」（示例内容.csv 的类型示例表）。

设计原则
--------
1. **只做确定性映射**：表里有的短语 -> 固定英文标签；表里没有的中文 ->
   原样留在 ``remaining`` 里，交给调用方决定（现有的 ``strip_cjk_segments``
   会把它清掉，LLM 路径则由 LLM 依据 skill 自行翻译）。
2. **最长匹配优先**：避免「全身」抢走「全身照」，「安全」抢走「不安全」。
3. **不编造**：本模块只翻译用户**真的说出口**的短语，绝不补充用户没提到的
   视距、场景、角色数量。
4. **GBK 红线**：中文只出现在本文件的映射表里，不进入任何 ``print`` /
   ``_log`` / ``_warn`` 调用；对外返回的一律是英文标签。

分段（对应「正面提示词结构」）
------------------------------
``quality``   第 1 行：质量标签
``framing``   第 2 行：图片视距 + 角色数量 + 多角色的大致动作
``character`` 第 3 行起：每个角色一行（着装 / 表情 / 动作细节）
``scene``     最后一行：场景 / 背景 / 画风
"""

#: 段名常量（与 ``nodes._SEG_*`` 保持一致）
SEGMENT_QUALITY = "quality"
SEGMENT_FRAMING = "framing"
SEGMENT_CHARACTER = "character"
SEGMENT_SCENE = "scene"

#: 特殊标记：命中但**不写进 positive**（例如「安全」-> safe）
DROP = "__drop__"


def _q(*tags):
    return (SEGMENT_QUALITY, list(tags))


def _f(*tags):
    return (SEGMENT_FRAMING, list(tags))


def _c(*tags):
    return (SEGMENT_CHARACTER, list(tags))


def _s(*tags):
    return (SEGMENT_SCENE, list(tags))


def _d(*tags):
    """命中但丢弃：``tags`` 只用于 ``dropped`` 记录，不写进 positive。"""
    return (DROP, list(tags) or ["safe"])


#: 中文短语 -> (目标段, 英文标签列表)
#:
#: 只收录**能确定性翻译**的表达。动作类（「做的动作」）是开放集合，不在表里，
#: 由 LLM 或 ``extract_garment_tags`` 这类专用逻辑处理。
CN_TAG_MAP = {
    # ---------------- 图片视距 -> framing
    "极特写": _f("extreme close-up"),
    "超特写": _f("extreme close-up"),
    "脸部特写": _f("close-up"),
    "面部特写": _f("close-up"),
    "特写": _f("close-up"),
    "全身照": _f("full body"),
    "全身": _f("full body"),
    "全景": _f("full body"),
    "半身像": _f("upper body"),
    "上半身": _f("upper body"),
    "半身": _f("upper body"),
    "远景": _f("wide shot"),
    "广角": _f("wide shot"),
    "中景": _f("cowboy shot"),
    "大头照": _f("portrait"),
    # ---------------- 质量 -> quality
    "超高清": _q("best quality", "masterpiece", "highres"),
    "高画质": _q("best quality", "masterpiece", "highres"),
    "高质量": _q("best quality", "masterpiece", "highres"),
    "高清": _q("best quality", "masterpiece", "highres"),
    "超精细": _q("highly detailed"),
    "高细节": _q("highly detailed"),
    # ---------------- 角色数量 -> framing
    "两个女角色": _f("2girls"),
    "两个男角色": _f("2boys"),
    "两个女孩": _f("2girls"),
    "两个男孩": _f("2boys"),
    "三个女角色": _f("3girls"),
    "三个男角色": _f("3boys"),
    "一个女角色": _f("1girl"),
    "一个男角色": _f("1boy"),
    "一个女孩": _f("1girl"),
    "一个男孩": _f("1boy"),
    "一个少女": _f("1girl"),
    "一个女生": _f("1girl"),
    "一个女孩子": _f("1girl"),
    "一个少年": _f("1boy"),
    "一个男生": _f("1boy"),
    "一名女性": _f("1girl"),
    "一名少女": _f("1girl"),
    "一名男性": _f("1boy"),
    "单人": _f("solo"),
    # 注：「双人」不映射 —— 性别未知（1girl 1boy / 2girls / 2boys 都可能），
    # 按「不编造」原则留给调用方或 LLM 决定。

    # ---------------- 发色（角色行，属于角色固有特征）
    "蓝色头发": _c("blue hair"),
    "蓝头发": _c("blue hair"),
    "蓝发": _c("blue hair"),
    "黑色头发": _c("black hair"),
    "黑头发": _c("black hair"),
    "黑发": _c("black hair"),
    "白色头发": _c("white hair"),
    "白头发": _c("white hair"),
    "白发": _c("white hair"),
    "银色头发": _c("silver hair"),
    "银发": _c("silver hair"),
    "金色头发": _c("blonde hair"),
    "金发": _c("blonde hair"),
    "红色头发": _c("red hair"),
    "红发": _c("red hair"),
    "粉色头发": _c("pink hair"),
    "粉发": _c("pink hair"),
    "紫色头发": _c("purple hair"),
    "紫发": _c("purple hair"),
    "绿色头发": _c("green hair"),
    "绿发": _c("green hair"),
    "棕色头发": _c("brown hair"),
    "棕发": _c("brown hair"),
    "双马尾": _c("twintails"),
    "马尾辫": _c("ponytail"),
    "马尾": _c("ponytail"),
    "长头发": _c("long hair"),
    "长发": _c("long hair"),
    "短头发": _c("short hair"),
    "短发": _c("short hair"),
    "呆毛": _c("ahoge"),

    # ---------------- 瞳色（角色行）
    "蓝色眼睛": _c("blue eyes"),
    "蓝眼": _c("blue eyes"),
    "蓝瞳": _c("blue eyes"),
    "红色眼睛": _c("red eyes"),
    "红眼": _c("red eyes"),
    "红瞳": _c("red eyes"),
    "紫色眼睛": _c("purple eyes"),
    "紫眼": _c("purple eyes"),
    "紫瞳": _c("purple eyes"),
    "金色眼睛": _c("yellow eyes"),
    "黄眼": _c("yellow eyes"),
    "绿色眼睛": _c("green eyes"),
    "绿眼": _c("green eyes"),
    "异色瞳": _c("heterochromia"),
    "闭眼": _c("closed eyes"),

    # ---------------- 表情（角色行）
    "在微笑": _c("smile"),
    "微笑": _c("smile"),
    "笑着": _c("smile"),
    "笑容": _c("smile"),
    "大笑": _c("grin"),
    "害羞": _c("blush"),
    "脸红": _c("blush"),
    "生气": _c("angry"),
    "哭泣": _c("crying"),
    "流泪": _c("tears"),
    "惊讶": _c("surprised"),
    "张嘴": _c("open mouth"),

    # ---------------- 动作 / 姿态（角色行）
    "站立": _c("standing"),
    "站着": _c("standing"),
    "坐着": _c("sitting"),
    "坐下": _c("sitting"),
    "走路": _c("walking"),
    "行走": _c("walking"),
    "散步": _c("walking"),
    "跑步": _c("running"),
    "奔跑": _c("running"),
    "回头看我": _c("looking back", "looking at viewer"),
    "回头": _c("looking back"),
    "躺下": _c("lying"),
    "躺着": _c("lying"),
    "蹲着": _c("squatting"),
    "挥手": _c("waving"),
    "举手": _c("arm up"),
    "看着镜头": _c("looking at viewer"),
    "看镜头": _c("looking at viewer"),
    "望向镜头": _c("looking at viewer"),
    "抱着手臂": _c("crossed arms"),
    "双手叉腰": _c("hands on hips"),

    # ---------------- 场景 -> scene
    # v11：来源校验收紧后，用户说的场景**必须**能映射出来，否则会被当成
    # 「无来源标签」删掉。所以常见场景词要补齐（含「雨夜」「霓虹」这类组合词）。
    "赛博朋克": _s("cyberpunk"),
    "霓虹灯": _s("neon lights"),
    "霓虹": _s("neon lights"),
    "雨夜": _s("rain", "night"),
    "雪夜": _s("snow", "night"),
    "街头": _s("street"),
    "街灯": _s("streetlight"),
    "路灯": _s("streetlight"),
    "小巷": _s("alley"),
    "胡同": _s("alley"),
    "招牌": _s("sign"),
    "屋顶": _s("rooftop"),
    "天台": _s("rooftop"),
    "天桥": _s("bridge"),
    "车流": _s("traffic"),
    "水洼": _s("puddle"),
    "水坑": _s("puddle"),
    "积水": _s("puddle"),
    "反光": _s("reflection"),
    "倒影": _s("reflection"),
    "湿漉漉": _s("wet"),
    "潮湿": _s("wet"),
    "雾气": _s("fog"),
    "薄雾": _s("fog"),
    "云": _s("cloud"),
    "云层": _s("cloud"),
    "大树": _s("tree"),
    "树木": _s("tree"),
    "花朵": _s("flower"),
    "教室窗边": _s("classroom", "window"),
    "窗户": _s("window"),
    "窗边": _s("window"),
    "工厂": _s("factory"),
    "废弃工厂": _s("factory"),
    "地铁站": _s("subway station"),
    "车站": _s("station"),
    "桥上": _s("bridge"),
    "河边": _s("river", "water"),
    "湖面": _s("lake"),
    "湖边": _s("lake"),
    "沙滩上": _s("beach"),
    "城市街道": _s("street", "city"),
    "街道": _s("street"),
    "城市": _s("city"),
    "校园": _s("school"),
    "学校": _s("school"),
    "教室": _s("classroom"),
    "海边": _s("beach", "ocean"),
    "海滩": _s("beach", "ocean"),
    "沙滩": _s("beach"),
    "森林": _s("forest"),
    "树林": _s("forest"),
    "夜晚": _s("night"),
    "晚上": _s("night"),
    "夜景": _s("night"),
    "白天": _s("day"),
    "雨天": _s("rain"),
    "下雨": _s("rain"),
    "雪天": _s("snow"),
    "下雪": _s("snow"),
    "室内": _s("indoors"),
    "户外": _s("outdoors"),
    "室外": _s("outdoors"),
    "公园": _s("park"),
    "卧室": _s("bedroom"),
    "咖啡厅": _s("cafe"),
    "咖啡店": _s("cafe"),
    "图书馆": _s("library"),
    "神社": _s("shrine"),
    "寺庙": _s("temple"),
    "废墟": _s("ruins"),
    "花田": _s("flower field"),
    "花海": _s("flower field"),
    "星空": _s("starry sky"),
    "夕阳": _s("sunset"),
    "日落": _s("sunset"),
    "日出": _s("sunrise"),
    "黄昏": _s("sunset", "golden hour"),
    "天空": _s("sky"),
    "雨中": _s("rain"),
    "雪中": _s("snow"),
    "水边": _s("water"),
    "泳池": _s("pool"),
    # ---------------- 安全标签（CSV 明确要求**不进 positive**）
    "不安全": _d(),
    "安全": _d(),
}

#: 中文 -> 英文的半角/全角标点归一（用于匹配前预处理）
_PUNCT_NORMALIZE = {
    "，": " ",
    "。": " ",
    "、": " ",
    "；": " ",
    "：": " ",
    "！": " ",
    "？": " ",
}

#: 按长度倒序的短语表（最长匹配优先）
_SORTED_PHRASES = sorted(CN_TAG_MAP, key=len, reverse=True)


def _normalize_punct(text):
    for src, dst in _PUNCT_NORMALIZE.items():
        text = text.replace(src, dst)
    return text


def translate(text):
    """把中文描述里的**确定性短语**翻译成标签，并按段归位。

    :param text: 用户自然语言（可含中英文）
    :returns: ``dict``：:

        {
          "quality": [...],     # 第 1 行
          "framing": [...],     # 第 2 行
          "character": [...],   # 第 3 行起
          "scene": [...],       # 最后一行
          "matched": ["full body", ...],   # 命中的英文标签（去重用）
          "dropped": ["safe"],              # 命中但按需求丢弃的
          "remaining": "...",               # 未匹配的原文（含英文和未识别中文）
        }

    **不抛异常**：任何内部错误都退化为「什么都没翻译」。
    """
    result = {
        SEGMENT_QUALITY: [],
        SEGMENT_FRAMING: [],
        SEGMENT_CHARACTER: [],
        SEGMENT_SCENE: [],
        "matched": [],
        "dropped": [],
        "remaining": text if isinstance(text, str) else "",
    }
    try:
        if not isinstance(text, str) or not text.strip():
            return result
        remaining = _normalize_punct(text)
        seen = set()
        for phrase in _SORTED_PHRASES:
            if phrase not in remaining:
                continue
            segment, tags = CN_TAG_MAP[phrase]
            # 用空格替换，保持其它文本的相对位置（也避免二次命中）
            remaining = remaining.replace(phrase, " ")
            if segment == DROP:
                for tag in tags:
                    if tag not in result["dropped"]:
                        result["dropped"].append(tag)
                continue
            for tag in tags:
                key = tag.lower()
                if key in seen:
                    continue
                seen.add(key)
                result[segment].append(tag)
                result["matched"].append(tag)
        result["remaining"] = " ".join(remaining.split())
        return result
    except Exception:  # noqa: BLE001 - 纯增强功能，绝不抛异常
        return {
            SEGMENT_QUALITY: [],
            SEGMENT_FRAMING: [],
            SEGMENT_CHARACTER: [],
            SEGMENT_SCENE: [],
            "matched": [],
            "dropped": [],
            "remaining": text if isinstance(text, str) else "",
        }
