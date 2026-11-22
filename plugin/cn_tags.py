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

# ===========================================================================
# v13：长句场景扩展（按类别组织，便于长期追加）
# ===========================================================================
# 设计说明：
#   * 组合词（颜色 + 品类）优先，一条命中就同时给「颜色标签 + 款式标签」
#   * 短词也单独收录，用户说「穿着一袭长裙」时由最长优先自动回退
#   * 只收**确定性可翻译**的中文，不收录开放集合（具体动作细节交给 LLM）
#   * 段归属：服装/表情/动作/发饰 -> character 行；天气/器物/场景/画风 -> scene 行

_CN_V13 = {}


def _put(category, segment, mapping):
    """把一个类别的中文->英文映射并入总表。"""
    for zh, tags in mapping.items():
        if isinstance(tags, str):
            tags = (tags,)
        if segment == SEGMENT_SCENE:
            entry = _s(*tags)
        elif segment == SEGMENT_FRAMING:
            entry = _f(*tags)
        elif segment == SEGMENT_QUALITY:
            entry = _q(*tags)
        else:
            entry = _c(*tags)
        # 已有条目优先（前面的表是手工精调过的），扩展只补空缺
        _CN_V13.setdefault(zh, entry)


# ---------------------------------------------------------------- 服装品类
#: 品类 -> (英文核心名词, 附加标签...)
_CANON_GARMENT = {
    "长裙": ("dress", ("long dress",)),
    "短裙": ("skirt", ("short skirt",)),
    "连衣裙": ("dress", ()),
    "裙子": ("skirt", ()),
    "礼服": ("dress", ("evening gown",)),
    "晚礼服": ("dress", ("evening gown",)),
    "衬衫": ("shirt", ()),
    "T恤": ("t-shirt", ()),
    "上衣": ("top", ()),
    "外套": ("coat", ()),
    "大衣": ("coat", ("long coat",)),
    "风衣": ("coat", ("trench coat",)),
    "西装": ("suit", ()),
    "夹克": ("jacket", ()),
    "皮夹克": ("jacket", ("leather jacket",)),
    "毛衣": ("sweater", ()),
    "卫衣": ("hoodie", ()),
    "牛仔裤": ("jeans", ()),
    "长裤": ("pants", ()),
    "短裤": ("shorts", ()),
    "百褶裙": ("skirt", ("pleated skirt",)),
    "吊带裙": ("dress", ("sundress",)),
    "蕾丝裙": ("dress", ("lace",)),
    "校服": ("uniform", ("school uniform",)),
    "制服": ("uniform", ()),
    "和服": ("kimono", ()),
    "旗袍": ("china dress", ()),
    "汉服": ("hanfu", ()),
    "泳装": ("swimsuit", ()),
    "泳衣": ("swimsuit", ()),
    "比基尼": ("bikini", ()),
    "睡衣": ("pajamas", ()),
    "女仆装": ("maid outfit", ()),
    "盔甲": ("armor", ()),
    "战袍": ("armor", ("battle dress",)),
    "围巾": ("scarf", ()),
    "斗篷": ("cape", ()),
    "披风": ("cape", ()),
    "手套": ("gloves", ()),
    "过膝袜": ("thighhighs", ()),
    "长袜": ("thighhighs", ()),
    "丝袜": ("pantyhose", ()),
    "高跟鞋": ("high heels", ()),
    "靴子": ("boots", ()),
    "帽子": ("hat", ()),
    "贝雷帽": ("beret", ()),
    "草帽": ("straw hat", ()),
}

for _zh, _pair in _CANON_GARMENT.items():
    _core, _extra = _pair
    _put("outfit", SEGMENT_CHARACTER, {_zh: (_core,) + tuple(_extra)})

# ---------------------------------------------------------------- 服装颜色
#: 服装用的颜色词（注意不是发色：金色在衣服上写 gold，头发才是 blonde）
_CANON_OUTFIT_COLOR = {
    "黑色": "black",
    "白色": "white",
    "红色": "red",
    "蓝色": "blue",
    "绿色": "green",
    "紫色": "purple",
    "粉色": "pink",
    "黄色": "yellow",
    "灰色": "grey",
    "棕色": "brown",
    "银色": "silver",
    "金色": "gold",
}

#: 参与「颜色 + 品类」展开的高频品类（避免生成 silver_jeans 这类怪组合）
_CANON_COLORABLE = ("长裙", "短裙", "连衣裙", "裙子", "衬衫", "外套", "礼服", "泳装")

#: 单独收录的常见颜色裙装简称（用户常说「白裙」「黑裙」）
_CANON_COLOR_SHORT = {
    "黑裙": "black",
    "白裙": "white",
    "红裙": "red",
    "蓝裙": "blue",
    "紫裙": "purple",
    "粉裙": "pink",
}

# v13.1（lead 复核）：**只给一个主标签**。
# 中文里「白裙」最常指连衣裙；同时给 dress + skirt 等于让模型画一件
# 既是连衣裙又是半身裙的衣服（自相矛盾）。要半身裙的用户会说
# 「百褶裙」「短裙」「半身裙」，那些词有各自的映射。
for _zh, _en in _CANON_COLOR_SHORT.items():
    _put("outfit", SEGMENT_CHARACTER, {_zh: ("%s %s" % (_en, "dress"),)})

for _cn_color, _en_color in _CANON_OUTFIT_COLOR.items():
    for _cn_item in _CANON_COLORABLE:
        _core, _extra = _CANON_GARMENT[_cn_item]
        _tags = ["%s %s" % (_en_color, _core)] + list(_extra)
        _put("outfit", SEGMENT_CHARACTER, {_cn_color + _cn_item: tuple(_tags)})

# ---------------------------------------------------------------- 表情
_put("expression", SEGMENT_CHARACTER, {
    "忧郁": ("sad",),
    "悲伤": ("sad",),
    "难过": ("sad",),
    "失落": ("sad",),
    "开心": ("happy",),
    "高兴": ("happy",),
    "愉快": ("happy",),
    "快乐": ("happy",),
    "害羞": ("blush",),
    "脸红": ("blush",),
    "羞涩": ("blush",),
    "生气": ("angry",),
    "愤怒": ("angry",),
    "恼怒": ("angry",),
    "惊讶": ("surprised",),
    "吃惊": ("surprised",),
    "震惊": ("surprised",),
    "微笑": ("smile",),
    "轻轻微笑": ("smile",),
    "笑着": ("smile",),
    "浅笑": ("smile",),
    "大笑": ("grin",),
    "笑容": ("smile",),
    "流泪": ("tears",),
    "眼泪": ("tears",),
    "哭泣": ("crying",),
    "哭": ("crying",),
    "皱眉": ("frown",),
    "蹙眉": ("frown",),
    "闭眼": ("closed eyes",),
    "闭着眼睛": ("closed eyes",),
    "张嘴": ("open mouth",),
    "面无表情": ("expressionless",),
    "无表情": ("expressionless",),
    "得意": ("smug",),
    "嘟嘴": ("pout",),
    "尴尬": ("embarrassed",),
    "疑惑": ("confused",),
    "沉思": ("thinking",),
    "认真": ("serious",),
    "温柔": ("gentle",),
})

# ---------------------------------------------------------------- 天气 / 光线
_put("weather", SEGMENT_SCENE, {
    "雨": ("rain",),
    "下雨": ("rain",),
    "雨天": ("rain",),
    "雨中": ("rain",),
    "大雨": ("heavy rain",),
    "毛毛雨": ("drizzle",),
    "雨滴": ("rain",),
    "雨点": ("rain",),
    "雪花": ("snow",),
    "雪": ("snow",),
    "下雪": ("snow",),
    "雪天": ("snow",),
    "晴天": ("day", "sunlight"),
    "晴朗": ("day", "sunlight"),
    "阳光明媚": ("sunlight", "day"),
    "阳光": ("sunlight",),
    "日光": ("sunlight",),
    "晨光": ("morning light",),
    "阴天": ("cloudy",),
    "多云": ("cloudy",),
    "乌云": ("cloudy",),
    "黄昏": ("sunset", "golden hour"),
    "傍晚": ("sunset", "golden hour"),
    "日落": ("sunset",),
    "夕阳": ("sunset",),
    "日出": ("sunrise",),
    "黎明": ("dawn",),
    "拂晓": ("dawn",),
    "夜晚": ("night",),
    "夜里": ("night",),
    "深夜": ("night",),
    "夜空": ("night",),
    "星空": ("starry sky",),
    "星星": ("stars",),
    "月光": ("moonlight",),
    "月亮": ("moon",),
    "逆光": ("backlighting",),
    "侧光": ("sidelighting",),
    "柔光": ("soft lighting",),
    "昏暗": ("dim lighting",),
    "阴暗": ("dim lighting",),
    "明亮": ("bright",),
    "烛光": ("candlelight",),
    "烛火": ("candlelight",),
    "霓虹": ("neon lights",),
    "霓虹灯": ("neon lights",),
    "风景": ("scenery",),
    "彩虹": ("rainbow",),
    "闪电": ("lightning",),
    "打雷": ("thunder",),
    "风": ("wind",),
    "微风": ("wind",),
    "风吹": ("wind",),
    "大风": ("wind",),
    "雾气": ("fog",),
    "薄雾": ("fog",),
    "浓雾": ("fog",),
    "水汽": ("steam",),
})

# ---------------------------------------------------------------- 器物 / 道具
_put("prop", SEGMENT_SCENE, {
    "玻璃": ("glass",),
    "窗户": ("window",),
    "窗外": ("window",),
    "窗口": ("window",),
    "窗边": ("window",),
    "玻璃窗": ("window",),
    "窗帘": ("curtain",),
    "伞": ("umbrella",),
    "雨伞": ("umbrella",),
    "花": ("flower",),
    "花朵": ("flower",),
    "花瓣": ("flower petals",),
    "花束": ("bouquet",),
    "书": ("book",),
    "书本": ("book",),
    "杯子": ("cup",),
    "咖啡杯": ("coffee cup",),
    "茶": ("tea",),
    "镜子": ("mirror",),
    "灯": ("lamp",),
    "灯光": ("lamp",),
    "街灯": ("streetlight",),
    "路灯": ("streetlight",),
    "蜡烛": ("candle",),
    "水珠": ("water drop",),
    "水洼": ("puddle",),
    "积水": ("puddle",),
    "反光": ("reflection",),
    "倒影": ("reflection",),
    "树叶": ("leaf",),
    "落叶": ("falling leaves",),
    "火焰": ("fire",),
    "烟雾": ("smoke",),
    "剑": ("sword",),
    "手机": ("phone",),
    "相机": ("camera",),
    "耳机": ("headphones",),
    "沙发": ("sofa",),
    "床": ("bed",),
    "地毯": ("carpet",),
    "楼梯": ("stairs",),
    "樱花": ("cherry blossoms",),
    "树叶飘落": ("falling leaves",),
})

# ---------------------------------------------------------------- 动作 / 姿态
_put("action", SEGMENT_CHARACTER, {
    "回头": ("looking back",),
    "回头看": ("looking back",),
    "站立": ("standing",),
    "站着": ("standing",),
    "坐着": ("sitting",),
    "坐下": ("sitting",),
    "躺下": ("lying",),
    "躺着": ("lying",),
    "奔跑": ("running",),
    "跑": ("running",),
    "行走": ("walking",),
    "走路": ("walking",),
    "散步": ("walking",),
    "漫步": ("walking",),
    "挥手": ("waving",),
    "招手": ("waving",),
    "拥抱": ("hug",),
    "抱住": ("hug",),
    "牵手": ("holding hands",),
    "牵着手": ("holding hands",),
    "跳跃": ("jumping",),
    "跳起": ("jumping",),
    "蹲下": ("squatting",),
    "蹲着": ("squatting",),
    "倚靠": ("leaning",),
    "靠在": ("leaning",),
    "仰望": ("looking up",),
    "抬头": ("looking up",),
    "低头": ("looking down",),
    "俯视": ("looking down",),
    "看着镜头": ("looking at viewer",),
    "看向镜头": ("looking at viewer",),
    "望向镜头": ("looking at viewer",),
    "看着远方": ("looking afar",),
    "望向远方": ("looking afar",),
    "转身": ("turning around",),
    "伸手": ("reaching out",),
    "抱着手臂": ("crossed arms",),
    "双手叉腰": ("hands on hips",),
    "托腮": ("hand to own cheek",),
    "摸头发": ("hand in own hair",),
    "整理头发": ("hand in own hair",),
    "拿着": ("holding",),
    "抱着": ("holding",),
    "看书": ("reading",),
    "读书": ("reading",),
    "思索": ("thinking",),
})

# ---------------------------------------------------------------- 场景 / 地点
_put("scene", SEGMENT_SCENE, {
    "教室": ("classroom",),
    "学校": ("school",),
    "校园": ("school",),
    "街道": ("street",),
    "街头": ("street",),
    "城市": ("city",),
    "都市": ("city",),
    "小巷": ("alley",),
    "胡同": ("alley",),
    "公园": ("park",),
    "房间": ("room",),
    "屋子": ("room",),
    "室内": ("indoors",),
    "户外": ("outdoors",),
    "室外": ("outdoors",),
    "海边": ("beach", "ocean"),
    "海滩": ("beach", "ocean"),
    "沙滩": ("beach",),
    "海边沙滩": ("beach", "ocean"),
    "森林": ("forest",),
    "树林": ("forest",),
    "山顶": ("mountain",),
    "山上": ("mountain",),
    "山中": ("mountain",),
    "图书馆": ("library",),
    "咖啡厅": ("cafe",),
    "咖啡馆": ("cafe",),
    "餐厅": ("restaurant",),
    "车站": ("station",),
    "地铁站": ("subway station",),
    "走廊": ("hallway",),
    "阳台": ("balcony",),
    "卧室": ("bedroom",),
    "厨房": ("kitchen",),
    "浴室": ("bathroom",),
    "泳池": ("pool",),
    "游泳池": ("pool",),
    "河流": ("river",),
    "河边": ("river", "water"),
    "湖": ("lake",),
    "湖边": ("lake",),
    "湖面": ("lake",),
    "沙漠": ("desert",),
    "雪地": ("snow",),
    "天空": ("sky",),
    "蓝天": ("sky", "blue sky"),
    "云": ("cloud",),
    "云层": ("cloud",),
    "医院": ("hospital",),
    "教堂": ("church",),
    "城堡": ("castle",),
    "废墟": ("ruins",),
    "庭院": ("garden",),
    "花园": ("garden",),
    "田野": ("field",),
    "花田": ("flower field",),
    "花海": ("flower field",),
    "地板": ("floor",),
    "墙壁": ("wall",),
    "天花板": ("ceiling",),
    "门口": ("doorway",),
    "舞台": ("stage",),
    "天台": ("rooftop",),
    "屋顶": ("rooftop",),
    "工厂": ("factory",),
    "废墟城市": ("ruins", "city"),
    "神社": ("shrine",),
    "寺庙": ("temple",),
    "城堡内部": ("castle", "indoors"),
})

# ---------------------------------------------------------------- 画风
_put("style", SEGMENT_SCENE, {
    "水彩": ("watercolor",),
    "水彩画": ("watercolor",),
    "油画": ("oil painting",),
    "赛璐璐": ("cel shading",),
    "厚涂": ("thick paint",),
    "线稿": ("lineart",),
    "像素风": ("pixel art",),
    "电影感": ("cinematic",),
    "复古": ("retro",),
    "写实": ("realistic",),
    "动漫": ("anime",),
    "插画": ("illustration",),
    "素描": ("sketch",),
    "水墨": ("ink painting",),
    "水墨画": ("ink painting",),
    "漫画": ("manga",),
    "手绘": ("hand drawn",),
    "极简": ("minimalist",),
    "唯美风格": ("beautiful",),
    "黑白": ("monochrome",),
    "单色": ("monochrome",),
})

# ---------------------------------------------------------------- 角色属性
_put("attribute", SEGMENT_CHARACTER, {
    "长发": ("long hair",),
    "长头发": ("long hair",),
    "短发": ("short hair",),
    "短头发": ("short hair",),
    "双马尾": ("twintails",),
    "单马尾": ("ponytail",),
    "马尾辫": ("ponytail",),
    "马尾": ("ponytail",),
    "卷发": ("curly hair",),
    "直发": ("straight hair",),
    "刘海": ("bangs",),
    "呆毛": ("ahoge",),
    "辫子": ("braid",),
    "麻花辫": ("braid",),
    "丸子头": ("hair bun",),
    "眼镜": ("glasses",),
    "耳环": ("earrings",),
    "耳饰": ("earrings",),
    "项链": ("necklace",),
    "发饰": ("hair ornament",),
    "发带": ("hairband",),
    "兽耳": ("animal ears",),
    "猫耳": ("cat ears",),
    "天使翅膀": ("angel wings",),
    "翅膀": ("wings",),
    "尾巴": ("tail",),
})

# ---------------------------------------------------------------- 视距补充
_put("shot", SEGMENT_FRAMING, {
    "近景": ("close-up",),
    "中近景": ("upper body",),
    "大远景": ("very wide shot",),
    "全身立绘": ("full body",),
    "半身像": ("upper body",),
    "脸部特写": ("close-up",),
    "眼神特写": ("close-up", "eyes",),
})

#: 把扩展表并入总表（手工精调过的条目优先，扩展只补空缺）
for _zh, _entry in _CN_V13.items():
    CN_TAG_MAP.setdefault(_zh, _entry)


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
