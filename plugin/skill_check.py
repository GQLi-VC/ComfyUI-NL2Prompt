"""NL2Prompt 插件 - 提示词校验器（契约 14.2）。

对外函数
--------
``check_anima(positive, negative, required_tokens=(), forbidden_tokens=(), skill=None,
required_negative_tokens=(), strict_negative=False)``
    -> ``{"ok": bool, "issues": [{"code","detail"}], "stats": {"tag_count", "score"}}``
``score_prompt(positive, negative, skill=None)``
    -> ``{"score": 0-100, "missing": [], "warnings": []}``

问题码（契约固定）：``empty`` / ``not_english`` / ``contains_chinese`` /
``missing_required`` / ``forbidden_present`` / ``tag_count_low`` /
``tag_count_high`` / ``bad_weight`` / ``markdown`` / ``empty_phrase``。
新增观测码：``negative_missing_required``（仅在 ``strict_negative=True`` 且
``required_negative_tokens`` 有缺失时出现，默认不阻断）。

约定
----
* 任何输入都不抛异常；非字符串输入视为空提示词。
* ``required_tokens`` 大小写不敏感，标签被 ``(...:1.2)`` 包裹仍算命中。
* ``required_negative_tokens`` 与 ``required_tokens`` 对称，但**默认只观测不阻断**：
  缺失时写进 ``stats.negative_missing``，``ok`` 不变；``strict_negative=True`` 才判失败。
  不传该参数时返回结构与行为与旧版逐字段一致（向后兼容）。
* 本模块只做静态规则判定，不联网、不读标签库。
"""

from __future__ import annotations

import re

#: 中文（含全角标点 / 扩展 A / 兼容区）。
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

#: 非中文的非 ASCII 字符（假名 / 韩文 / emoji / 西里尔等）。
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

#: 单个标签最大长度，超长多半是整句自然语言。
_MAX_TAG_LEN = 160

#: 标签数量下限（少于此值判 ``tag_count_low``）。
_MIN_TAGS = 5

#: skill 缺省标签上限。
_DEFAULT_MAX_TAGS = 80

#: 权重允许范围。
_WEIGHT_MIN = 0.5
_WEIGHT_MAX = 1.5

#: markdown 痕迹。
_MARKDOWN_PATTERNS = (
    ("```", "代码块"),
    ("**", "粗体标记"),
    ("__", "下划线强调"),
    ("~~", "删除线"),
    ("##", "markdown 标题"),
    ("}](", "markdown 链接"),
)

#: 各槽位命中关键词（用于 ``score_prompt`` 的槽位覆盖评分）。
_SLOT_KEYWORDS = {
    "quality": (
        "masterpiece", "best quality", "very aesthetic", "high quality",
        "good quality", "normal quality", "absurdres", "highres",
        "incredibly absurdres", "aesthetic", "quality", "score_",
    ),
    "subject": (
        "1girl", "1boy", "1other", "solo", "2girls", "2boys", "3girls",
        "multiple girls", "multiple boys", "female", "male", "no humans",
        "group", "chibi", "animal", "monster",
    ),
    "outfit": (
        "shirt", "skirt", "dress", "coat", "jacket", "hoodie", "sweater",
        "cardigan", "blazer", "vest", "uniform", "serafuku", "necktie",
        "bowtie", "bow", "ribbon", "pantyhose", "thighhighs", "socks",
        "boots", "shoes", "sandals", "gloves", "hat", "cap", "beret",
        "apron", "kimono", "yukata", "hanfu", "swimsuit", "bikini",
        "lingerie", "armor", "cloak", "cape", "scarf", "belt", "choker",
        "glasses", "necklace", "earrings", "hairband", "hairclip",
        "hair ornament", "pants", "shorts", "jeans", "trousers", "leggings",
        "sailor collar", "collared", "button", "outfit", "clothes",
        "armor", "headband", "goggles", "mask", "earmuffs", "collar",
    ),
    "pose": (
        "standing", "sitting", "lying", "kneeling", "squatting", "crouching",
        "walking", "running", "jumping", "dancing", "leaning", "arms",
        "hand", "holding", "hugging", "crossed", "legs", "reach", "stretch",
        "pose", "looking", "smile", "closed eyes", "open mouth", "blush",
        "on back", "on stomach", "on bed", "sitting on", "spread", "bent",
        "bowing", "salute", "turning", "swinging", "riding", "lying on",
    ),
    "scene": (
        "indoors", "outdoors", "bedroom", "classroom", "room", "kitchen",
        "bathroom", "office", "city", "street", "alley", "park", "forest",
        "beach", "ocean", "mountain", "field", "garden", "rooftop",
        "balcony", "hallway", "corridor", "staircase", "stairs", "station",
        "train", "car", "school", "library", "shrine", "church", "castle",
        "night", "day", "sunset", "sunrise", "dusk", "dawn", "sky", "cloud",
        "rain", "snow", "fog", "wind", "window", "wall", "floor", "ceiling",
        "door", "curtain", "desk", "chair", "bed", "sofa", "table",
        "bookshelf", "mirror", "neon", "cherry blossoms", "petals",
        "background", "scenery", "light", "shadow", "reflection", "water",
        "puddle", "wet", "sakura", "grass", "tree", "stone", "wooden",
        "town", "village", "bridge", "tower", "shop", "market", "beach",
    ),
    "camera": (
        "close-up", "cowboy shot", "full body", "upper body", "wide shot",
        "portrait", "from front", "from side", "from behind", "from above",
        "from below", "profile", "dutch angle", "pov", "over shoulder",
        "depth of field", "focus", "framing", "composition", "close up",
        "lower body", "head out of frame", "cropped", "panorama",
        "transparent background", "aspect ratio",
    ),
    "lighting": (
        "lighting", "light", "backlight", "sidelight", "underlight",
        "sunlight", "sunbeam", "moonlight", "golden hour", "neon lights",
        "lens flare", "bloom", "bokeh", "glow", "glowing", "sparkle",
        "sparkles", "shadow", "silhouette", "chiaroscuro", "spotlight",
        "camera flash", "candlelight", "vignetting", "chromatic",
        "film grain", "contrast", "saturated", "pastel", "colorful",
        "dim lighting", "dappled", "rays", "lens", "flare", "dusk",
        "cinematic", "ambient", "exposure", "glare",
    ),
    "style": (
        "@", "artist", "anime", "manga", "sketch", "lineart", "painterly",
        "watercolor", "ink", "comic", "cel shading", "monochrome",
        "greyscale", "grayscale", "sepia", "retro", "pastel", "style",
        "realistic", "photorealistic", "3d", "render", "official art",
        "colorful", "flat color", "halftone", "screentone", "pixel",
        "ukiyo-e", "art nouveau", "impressionist", "chibi", "cartoon",
    ),
}

#: 具体度加分词（光照/镜头/材质/环境细节，命中即说明不是空泛描述）。
_CONCRETE_KEYWORDS = (
    "light", "shadow", "sunlight", "sunbeam", "backlight", "sidelight",
    "moonlight", "neon", "lens flare", "bloom", "bokeh", "glow",
    "silhouette", "chiaroscuro", "dappled", "cinematic", "depth of field",
    "close-up", "cowboy shot", "full body", "upper body", "wide shot",
    "from above", "from below", "from side", "from behind", "profile",
    "dutch angle", "pov", "over shoulder", "wind", "rain", "snow",
    "fog", "petals", "reflection", "wet", "puddle", "sparkle", "glitter",
    "colorful", "pastel", "gradient", "texture", "detailed", "reflection",
    "window", "curtain", "wooden", "stone", "brick", "tile", "fabric",
    "leather", "lace", "sheer", "translucent", "reflective", "iridescent",
)

#: ``score_prompt`` 违规扣分表（问题码 -> 分值）。
_DEDUCT = {
    "empty": 30,
    "contains_chinese": 12,
    "not_english": 10,
    "missing_required": 12,
    "forbidden_present": 8,
    "markdown": 8,
    "bad_weight": 8,
    "empty_phrase": 6,
    "tag_count_low": 6,
    "tag_count_high": 4,
    "negative_missing_required": 10,
}

#: 默认禁止的空洞词（skill 未提供时）。
_DEFAULT_FORBIDDEN = (
    "beautiful", "amazing", "masterpiece of art", "stunning", "gorgeous",
    "elegant", "pretty", "cute", "highly detailed", "intricate details",
    "8k", "4k", "octane render", "ray tracing", "best art",
)


# --------------------------------------------------------------------------- #
# 文本工具
# --------------------------------------------------------------------------- #

def _as_text(value) -> str:
    """非字符串一律当空串；绝不抛异常。"""
    if isinstance(value, str):
        return value
    return ""


def _norm(value) -> str:
    """归一化用于匹配：小写、下划线/多空白折叠成单个空格、去首尾标点。"""
    try:
        text = str(value or "").lower()
        text = text.replace("_", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip(" ,;:.")
    except Exception:
        return ""


def _split_tags(text: str) -> "list[str]":
    """按顶层逗号切分标签（括号内的逗号不切）。

    v10：positive 支持**多行分段结构**（质量 / 视距 / 角色 / 场景各一行），
    所以**换行也是分隔符**。否则 ``"\\ncastorice"`` 会被当成一个非法标签，
    导致 skill 校验误报。单行输入的行为与旧版完全一致。
    """
    tags = []
    try:
        buf = []
        depth = 0
        for char in text:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            if (char == "," or char == "\n" or char == "\r") and depth == 0:
                item = "".join(buf).strip()
                if item:
                    tags.append(item)
                buf = []
                continue
            buf.append(char)
        tail = "".join(buf).strip()
        if tail:
            tags.append(tail)
    except Exception:
        flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ",")
        return [item.strip() for item in flat.split(",") if item.strip()]
    return tags


def _bare_tag(tag: str) -> str:
    """剥掉合法权重包裹，返回裸标签（``(long hair:1.2)`` -> ``long hair``）。

    v15：同时**去掉反斜杠转义** —— 角色 trigger 在最终 positive 里是
    ``castorice_\\(honkai:_star_rail\\)``，而 required_tokens 是未转义的
    ``castorice_(honkai:_star_rail)``。两者必须能对上，否则 skill_check 误判缺失。
    """
    text = str(tag or "").strip().replace('\\', "").replace('\\\\', "")
    try:
        for _ in range(3):
            if not (text.startswith("(") and text.endswith(")")):
                break
            inner = text[1:-1].strip()
            matched = re.match(r"^(.*?)\s*:\s*[0-9]*\.?[0-9]+\s*$", inner)
            if matched:
                text = matched.group(1).strip()
            else:
                text = inner
    except Exception:
        return text
    return text


#: 合法权重写法：``(tag:1.2)``。冒号后必须紧跟数字才算权重语法；
#: 冒号后不是数字的一律视为标签文本本身（danbooru 系列名 ``robin (honkai: star rail)``
#: 就是这种形状，绝不能当权重解析，否则真实角色触发词会被误杀）。
_WEIGHT_FULL_RE = re.compile(
    r"^\(\s*(?P<tag>[^:()]+?)\s*:\s*(?P<value>[-+]?\d*\.?\d+)\s*\)$"
)

#: 空权重写法：``(tag:)``。
_WEIGHT_EMPTY_RE = re.compile(r"^\(\s*(?P<tag>[^:()]+?)\s*:\s*\)$")

#: 最内层圆括号内容（不含嵌套括号）。
_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _weight_issues(text: str) -> "list[str]":
    """检查权重语法：括号配平、权重越界、空权重。

    只有 ``(...)`` 内是 ``标签:数字`` 形式才算权重语法：
    * ``(1girl:1.2)``    -> 合法
    * ``(1girl:2.5)``    -> 越界
    * ``(1girl:)``       -> 空权重
    * ``(honkai: star rail)`` / ``(tag:abc)`` -> 视为标签文本，**不报**（宁可漏报不误杀）
    """
    issues = []
    try:
        depth = 0
        for char in text:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    issues.append("括号不配平：出现多余的右括号")
                    depth = 0
        if depth > 0:
            issues.append("括号不配平：缺少右括号")

        for matched in _PAREN_RE.finditer(text):
            inner = matched.group(1).strip()
            if ":" not in inner:
                continue

            full = _WEIGHT_FULL_RE.match(matched.group(0))
            if full is not None:
                value = full.group("value").strip()
                try:
                    number = float(value)
                except ValueError:
                    continue
                if number < _WEIGHT_MIN or number > _WEIGHT_MAX:
                    issues.append(
                        "权重越界 %.2f（允许 %.1f 到 %.1f）: (%s)"
                        % (number, _WEIGHT_MIN, _WEIGHT_MAX, inner)
                    )
                continue

            empty = _WEIGHT_EMPTY_RE.match(matched.group(0))
            if empty is not None:
                issues.append("权重值为空: (%s)" % inner)
    except Exception:
        pass
    return issues


def _has_markdown(text: str) -> str:
    """返回命中的 markdown 特征说明；无命中返回空串。"""
    for token, label in _MARKDOWN_PATTERNS:
        if token in text:
            return label
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") or stripped.startswith("- ") or stripped.startswith("* "):
            return "markdown 列表或标题"
        if stripped.startswith("> "):
            return "markdown 引用"
    if text.count("|") >= 2:
        return "markdown 表格"
    return ""


def _token_present(bare_tags, token) -> bool:
    """token 是否命中任一带标签（大小写不敏感；多词 token 允许子串命中）。"""
    target = _norm(token)
    if not target:
        return True
    for tag in bare_tags:
        current = _norm(tag)
        if current == target:
            return True
        if " " in target and target in current:
            return True
    return False


def _phrase_present(text: str, phrase: str) -> bool:
    """空洞短语检测：按词边界避免 ``cute`` 命中 ``execute``。"""
    target = _norm(phrase)
    if not target:
        return False
    body = _norm(text)
    if not body:
        return False
    try:
        pattern = r"(?<![a-z0-9])" + re.escape(target) + r"(?![a-z0-9])"
        return re.search(pattern, body) is not None
    except Exception:
        return target in body


def _skill_field(skill, field, default):
    """安全读取 skill 字段。"""
    try:
        if isinstance(skill, dict):
            return skill.get(field, default)
    except Exception:
        pass
    return default


def _max_tags(skill) -> int:
    try:
        value = int(_skill_field(skill, "max_tags", _DEFAULT_MAX_TAGS))
        return value if value > 0 else _DEFAULT_MAX_TAGS
    except Exception:
        return _DEFAULT_MAX_TAGS


def _forbidden_phrases(skill) -> "list[str]":
    try:
        raw = _skill_field(skill, "forbidden_phrases", None)
        if isinstance(raw, (list, tuple)):
            items = [str(item).strip() for item in raw if str(item).strip()]
            if items:
                return items
    except Exception:
        pass
    return list(_DEFAULT_FORBIDDEN)


# --------------------------------------------------------------------------- #
# 对外接口：check_anima
# --------------------------------------------------------------------------- #

def _token_list(raw) -> "list[str]":
    """把 token 参数规整成字符串列表；兼容 ``None`` / 字符串（逗号分隔）/ 迭代器。"""
    out = []
    try:
        if raw is None:
            return out
        if isinstance(raw, str):
            raw = raw.replace("\n", ",").split(",")
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        for item in raw:
            token = str(item).strip() if item is not None else ""
            if token:
                out.append(token)
    except Exception:
        return []
    return out


def check_anima(positive, negative, required_tokens=(), forbidden_tokens=(), skill=None,
                required_negative_tokens=(), strict_negative=False) -> dict:
    """检查一条正向/负向提示词，返回问题码与统计；绝不抛异常。

    ``required_negative_tokens`` 与 ``required_tokens`` 对称，但默认**只观测不阻断**：
    缺失项写进 ``stats.negative_missing``（供 ``debug.skill_check`` 使用），``ok`` 不受影响；
    ``strict_negative=True`` 时才报 ``negative_missing_required`` 并判失败。
    不传这两个新参数的调用，返回值与旧版逐字段一致。
    """
    stats = {"tag_count": 0, "score": 0}
    issues = []

    def _issue(code, detail):
        issues.append({"code": code, "detail": detail})

    try:
        text = _as_text(positive)
        neg = _as_text(negative)

        # 0) negative 观测（默认不阻断，仅新参数启用时才写 stats）
        neg_required = _token_list(required_negative_tokens)
        negative_bare = []
        missing_negative = []
        if neg_required:
            try:
                negative_bare = [_bare_tag(tag) for tag in _split_tags(neg)]
                negative_bare = [tag for tag in negative_bare if tag]
            except Exception:
                negative_bare = []
            missing_negative = [
                item for item in neg_required if not _token_present(negative_bare, item)
            ]
            stats["negative_missing"] = list(missing_negative)

        if not text.strip():
            _issue("empty", "positive 为空")
            if strict_negative and missing_negative:
                _issue(
                    "negative_missing_required",
                    "negative 缺少必需抑制词: %s" % ", ".join(missing_negative[:5]),
                )
            stats["tag_count"] = 0
            stats["score"] = 0
            return {"ok": not issues, "issues": issues, "stats": stats}

        tags = _split_tags(text)
        bare_tags = [_bare_tag(tag) for tag in tags]
        bare_tags = [tag for tag in bare_tags if tag]
        stats["tag_count"] = len(bare_tags)

        # 1) 中文
        if _CJK_RE.search(text):
            sample = (_CJK_RE.search(text).group(0) or "").strip()
            _issue("contains_chinese", "positive 含中文字符: %s" % sample)
        elif _CJK_RE.search(neg):
            _issue("contains_chinese", "negative 含中文字符")

        # 2) 非英文（去掉合法 ASCII 后仍有非 ASCII 字符）
        non_ascii = [char for char in text if _NON_ASCII_RE.match(char)]
        if non_ascii:
            foreign = [char for char in non_ascii if not _CJK_RE.match(char)]
            if not re.search(r"[A-Za-z]", text):
                _issue("not_english", "positive 不含英文字母")
            elif foreign:
                _issue("not_english", "positive 含非英文非中文字符: %s" % "".join(foreign[:5]))

        # 3) 必填触发词
        required = _token_list(required_tokens)
        missing = [item for item in required if not _token_present(bare_tags, item)]
        if missing:
            _issue("missing_required", "缺少必填触发词: %s" % ", ".join(missing[:5]))

        # 4) 禁止词
        forbidden = _token_list(forbidden_tokens)
        hits = [item for item in forbidden if _token_present(bare_tags, item)]
        if hits:
            _issue("forbidden_present", "命中禁止词: %s" % ", ".join(hits[:5]))

        # 5) 标签数量
        limit = _max_tags(skill)
        if stats["tag_count"] < _MIN_TAGS:
            _issue(
                "tag_count_low",
                "标签仅 %d 个（至少 %d 个）" % (stats["tag_count"], _MIN_TAGS),
            )
        elif stats["tag_count"] > limit:
            _issue(
                "tag_count_high",
                "标签 %d 个，超过上限 %d" % (stats["tag_count"], limit),
            )

        # 6) 权重语法
        for detail in _weight_issues(text):
            _issue("bad_weight", detail)

        # 7) markdown
        mark = _has_markdown(text)
        if mark:
            _issue("markdown", "positive 含 markdown：%s" % mark)
        elif _has_markdown(neg):
            _issue("markdown", "negative 含 markdown")

        # 8) 空洞词
        for phrase in _forbidden_phrases(skill):
            if _phrase_present(text, phrase):
                _issue("empty_phrase", "命中空洞词: %s" % phrase)
                break

        # 9) 单标签过长（整句自然语言冒充标签）
        overlong = [tag for tag in bare_tags if len(tag) > _MAX_TAG_LEN]
        if overlong:
            _issue("not_english", "存在超长标签（疑似整句）: %s" % overlong[0][:40])

        # 10) negative 必需抑制词（默认只观测，strict 才阻断）
        if strict_negative and missing_negative:
            _issue(
                "negative_missing_required",
                "negative 缺少必需抑制词: %s" % ", ".join(missing_negative[:5]),
            )

        stats["score"] = score_prompt(text, neg, skill=skill).get("score", 0)
        return {"ok": not issues, "issues": issues, "stats": stats}
    except Exception as exc:  # noqa: BLE001 - 校验器绝不抛异常
        return {
            "ok": False,
            "issues": [{"code": "empty", "detail": "校验异常: %s" % exc}],
            "stats": stats,
        }


# --------------------------------------------------------------------------- #
# 对外接口：score_prompt
# --------------------------------------------------------------------------- #

def _slot_of(tag: str, order) -> str:
    """判断单个标签属于哪个槽位；无命中返回空串。"""
    body = _norm(tag)
    if not body:
        return ""
    for slot in order:
        for keyword in _SLOT_KEYWORDS.get(slot, ()):
            key = _norm(keyword)
            if not key:
                continue
            if key == "@":
                if tag.strip().startswith("@"):
                    return slot
                continue
            if body == key or key in body.split() or (" " in key and key in body):
                return slot
    return ""


def _slot_coverage(bare_tags, order) -> dict:
    """按槽位归类标签，返回 ``{slot: [tags]}``。"""
    covered = {}
    rest = []
    for tag in bare_tags:
        slot = _slot_of(tag, order)
        if slot:
            covered.setdefault(slot, []).append(tag)
        else:
            rest.append(tag)
    # 剩余未归类标签视为角色/专有触发词（Anima 的角色名、LoRA 激活词不落在固定词表里）
    if rest:
        covered.setdefault("character", rest)
    return covered


def score_prompt(positive, negative, skill=None) -> dict:
    """给提示词打分（0-100）：槽位覆盖 40 + 数量 20 + 无违规 30 + 具体度 10。"""
    result = {"score": 0, "missing": [], "warnings": []}
    try:
        text = _as_text(positive)
        neg = _as_text(negative)
        if not text.strip():
            return {"score": 0, "missing": ["all"], "warnings": ["positive 为空"]}

        order = None
        raw_order = _skill_field(skill, "tag_order", None)
        if isinstance(raw_order, (list, tuple)):
            order = [str(item).strip() for item in raw_order if str(item).strip()]
        if not order:
            order = ["quality", "subject", "character", "outfit", "pose",
                     "scene", "camera", "lighting", "style"]
        order = [slot for slot in order if slot in _SLOT_KEYWORDS or slot == "character"]
        if not order:
            order = ["quality", "subject", "character", "outfit", "pose",
                     "scene", "camera", "lighting", "style"]

        tags = _split_tags(text)
        bare_tags = [tag for tag in (_bare_tag(item) for item in tags) if tag]
        covered = _slot_coverage(bare_tags, order)

        # a) 槽位覆盖 40
        hit_slots = [slot for slot in order if covered.get(slot)]
        missing = [slot for slot in order if not covered.get(slot)]
        result["missing"] = missing
        coverage = 40.0 * len(hit_slots) / len(order)

        # b) 标签数量 20
        count = len(bare_tags)
        limit = _max_tags(skill)
        if 15 <= count <= 60:
            count_score = 20
        elif 10 <= count < 15 or 60 < count <= limit:
            count_score = 14
        elif _MIN_TAGS <= count < 10 or limit < count <= limit + 20:
            count_score = 8
        else:
            count_score = 0

        # c) 无违规 30
        report = check_anima(text, neg, skill=skill)
        deduction = 0
        for issue in report.get("issues") or []:
            deduction += _DEDUCT.get(issue.get("code"), 4)
            result["warnings"].append(issue.get("detail") or issue.get("code"))
        violation_score = max(0, 30 - min(30, deduction))

        # d) 具体度 10
        concrete = 0
        for tag in bare_tags:
            body = _norm(tag)
            if any(_norm(word) in body for word in _CONCRETE_KEYWORDS):
                concrete += 1
        concrete_score = min(10, concrete * 2)
        for phrase in _forbidden_phrases(skill):
            if _phrase_present(text, phrase):
                concrete_score = max(0, concrete_score - 4)
                break

        total = coverage + count_score + violation_score + concrete_score
        result["score"] = int(round(max(0, min(100, total))))
        return result
    except Exception as exc:  # noqa: BLE001 - 打分器绝不抛异常
        return {"score": 0, "missing": [], "warnings": ["打分异常: %s" % exc]}
