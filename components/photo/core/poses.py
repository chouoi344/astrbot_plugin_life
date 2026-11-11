"""Scene-filtered phone-photo habits adapted from the user's photo_poses.py.

The former female/male pools are combined into the hidden neutral default.
Historical IDs remain stable for recent-use records; they never select a gender.
Each habit combines an angle and a small photo action, without changing life data.
"""

import random
import re

POSES = (
    {
        "id": "female-high",
        "angle": "从面前偏高约20度俯拍",
        "action": "抬头看向镜头，空闲手自然放松",
        "tags": ("室内", "户外"),
    },
    {
        "id": "female-tilt",
        "angle": "镜头从左侧约30度取景",
        "action": "上身微侧，视线回到镜头",
        "tags": (),
    },
    {
        "id": "female-vsign",
        "angle": "自然平视",
        "action": "目光自然落向镜头，神态松弛随意",
        "tags": (),
    },
    {
        "id": "female-cheek",
        "angle": "从右前侧约30度取景",
        "action": "手肘自然支在桌边或膝上，姿态放松",
        "tags": ("餐桌", "床上"),
    },
    {
        "id": "female-cup",
        "angle": "机位接近桌面向上取景",
        "action": "身体微倾靠近桌面，视线自然看向镜头",
        "tags": ("餐桌",),
    },
    {
        "id": "female-mirror",
        "angle": "镜前向左侧转约30度",
        "action": "空闲手轻整衣摆",
        "tags": ("镜前",),
    },
    {
        "id": "female-low",
        "angle": "机位稍低，向上约15度",
        "action": "身体侧转，双肩自然展开",
        "tags": ("户外", "健身"),
    },
    {
        "id": "female-window",
        "angle": "从右侧约45度取景",
        "action": "侧身后回头看向镜头",
        "tags": ("室内",),
    },
    {
        "id": "female-glance",
        "angle": "镜头从斜前方取景",
        "action": "目光先偏向一侧，头部自然回转",
        "tags": (),
    },
    {
        "id": "female-relaxed",
        "angle": "从左前侧约45度取景",
        "action": "肩线自然放松，目光带着一点随意的停顿看向镜头",
        "tags": (),
    },
    {
        "id": "male-eye",
        "angle": "自然平视，镜头正对人物",
        "action": "下巴稍抬，双肩放松",
        "tags": ("室内", "户外"),
    },
    {
        "id": "male-casual",
        "angle": "画面轻微倾斜，从斜前方取景",
        "action": "一侧肩膀靠近镜头，姿态松弛",
        "tags": (),
    },
    {
        "id": "male-mirror",
        "angle": "镜前向右侧转约30度",
        "action": "空闲手搭在身侧，侧身看镜头",
        "tags": ("镜前",),
    },
    {
        "id": "male-hands",
        "angle": "从右前方略低的机位取景",
        "action": "空闲手伸向镜头做自然招呼",
        "tags": (),
    },
    {
        "id": "male-back",
        "angle": "从侧后方约45度取景",
        "action": "保持所在位置，回头看向镜头",
        "tags": ("户外", "健身"),
    },
    {
        "id": "male-arm",
        "angle": "从左侧斜上方取景",
        "action": "一只手轻扶另一侧手臂",
        "tags": (),
    },
    {
        "id": "male-neutral",
        "angle": "从正前方稍低处取景",
        "action": "轻轻歪头，目光看向镜头",
        "tags": (),
    },
    {
        "id": "male-table",
        "angle": "从桌边斜上方取景",
        "action": "一只手托腮，身体自然靠近桌边",
        "tags": ("餐桌",),
    },
    {
        "id": "male-rest",
        "angle": "从靠近床侧的高机位取景",
        "action": "保持休息位置，脸侧转向镜头",
        "tags": ("床上",),
    },
)

_SCENES = {
    "镜前": r"镜子|镜前|对镜|试衣镜|穿衣镜",
    "床上": r"床上|床边|床头|被窝|卧床|躺床",
    "餐桌": r"餐桌|饭桌|餐厅|食堂|咖啡|吃饭|吃早餐|吃午餐|吃晚餐|喝茶|喝饮料",
    "健身": r"健身|锻炼|运动|跑步|瑜伽|球场",
    "户外": r"户外|室外|公园|街道|路上|海边|山上|校园|散步|出门|操场|花园",
    "室内": r"室内|家里|居家|客厅|宿舍|卧室|办公室|教室|图书馆|书房|房间|窗边",
}


_EXPRESSION_MOOD = r"(?:严肃|冷脸|害羞|委屈|生气|开心)"
SMILE_GUIDANCE = (
    "笑容默认自然克制。笑一个、笑开一点时，通过眼神变亮、眉眼轻动和嘴角小幅变化表现笑意，"
    "按语境可以自然小开口或露出少量牙齿。避免咧嘴、大片露齿和夸张挤眼。"
    "用户明确要求大笑或指定了其他表情时，按原话的幅度表现。用户的否定要求优先。"
)
REACTION_GUIDANCE = (
    "当前聊天有明确反应时，按语气和人设挑一两个肉眼看得出的变化，"
    "例如眉梢轻抬、视线偏开或脸颊微提。反应随对话变化，笑幅自然。"
    "用户明确要求保持表情、别笑或大笑时，按原话执行。"
)
_TEETH_SMILE = r"(?:(?:轻微|微微|略微|少量)?露[齿牙](?:笑)?|露出(?:一点|少量)?(?:牙齿|白牙|牙))"
_EXAGGERATED_SMILE = (
    r"(?:(?:露[齿牙])?(?:开怀)?大笑|咧嘴(?:大笑|笑)?"
    r"|露出(?:一排|整排|满口|八颗|大片|大量)(?:牙齿|白牙|牙)|(?:大片|大量)露[齿牙](?:笑)?)"
)
_SMILE_REQUEST = re.compile(
    r"(?:请|给我|这次|这张|我想看|我想让|想看|让我看看|让我看|能不能|可以|要)?"
    r"(?:你)?(?:拍(?:一)?张?|来(?:一)?张)?(?:你)?"
    rf"(?:{_EXAGGERATED_SMILE}|{_TEETH_SMILE})(?:一点|一下|一张|的?(?:照片|自拍|照|笑容)|给我看)?"
)
_CLAUSE_BREAKS = re.compile(r"[，,。.!！?？；;\n]")
_SMILE_NEGATION = (
    r"(?:不要|别|不用|不想|不许|不能|无需|避免|禁止|不)"
    r"(?:再|特意|直接|太|过于|那么|这么|一直|刻意|画成)?"
)
_NEGATED_TEETH = re.compile(rf"{_SMILE_NEGATION}{_TEETH_SMILE}")
_NEGATED_SMILE = re.compile(rf"{_SMILE_NEGATION}(?:{_EXAGGERATED_SMILE}|{_TEETH_SMILE}|夸张|笑)")
_SMILE_SCALE = r"的?幅度|开|得|太|那么|这么"
_NO_SMILE = re.compile(
    r"(?:别|不要|不许|不用|无需|避免)(?:再|一直)?"
    rf"(?:微笑|笑(?!{_SMILE_SCALE}|话|我|你))"
)
_KEEP_EXPRESSION = re.compile(
    r"(?:保持|保留)(?:上一张|上张|原来|原本)?的?表情"
    r"|同一表情|表情(?:保持)?不变|表情别变|不换表情"
)
_ORDINARY_SMILE = re.compile(r"笑(?:一个|一下|开(?:一)?点)|看看?(?:完整的?)?笑脸")
_MODEL_SMILE_CUES = (
    (r"眉眼(?:彻底|完全)弯(?:下来|起来|起)?|眉眼弯弯", "眉眼轻弯"),
    (r"嘴角(?:完全|彻底|止不住地|大幅(?:度)?地?)(?:扬起|上扬|上翘)", "嘴角轻轻扬起"),
    (r"(完整(?:的)?)?灿烂(?:的)?笑容", r"\1自然的笑容"),
    (r"夸张(?:地)?(?:眯(?:眼|起眼睛)|挤眼)|(?:笑得)?眼睛眯成一条缝", "眼角轻弯"),
    (_EXAGGERATED_SMILE, "自然轻笑"),
)
_BEFORE_NEGATED_CUE = re.compile(rf"{_SMILE_NEGATION}$")
_SMILING_CUES = (
    r"眉眼(?:轻弯|弯弯|带笑|含笑)|眼角(?:轻弯|带笑|含笑)|眼神带笑"
    r"|嘴角(?:轻轻|自然)?(?:微扬|轻扬|扬起|上扬|上翘)(?:明朗的笑意)?"
    r"|(?:带一点|带点|带着)?(?:自然|轻松的|淡淡的|明朗的)?(?:微笑|浅笑|轻笑|笑意|笑容)"
    r"|" + _TEETH_SMILE
)
_EXPRESSION_CUES = (
    _SMILING_CUES + r"|眉头(?:微蹙|轻蹙|紧锁|轻皱)|眉眼舒展|嘴唇(?:轻抿|微抿|抿紧)"
    r"|眼神(?:明亮|柔和|带着纵容与打趣)|目光(?:柔和|认真)|嘴角放松|闭眼|眨眼"
)


def _cue_is_negated(match: re.Match) -> bool:
    return bool(_BEFORE_NEGATED_CUE.search(match.string[max(0, match.start() - 6) : match.start()]))


def _affirmative_smile_request(user_text: str) -> str:
    """Find an affirmative request, excluding negation and a description of past photos."""
    if _NEGATED_SMILE.search(user_text):
        return ""
    for clause in _CLAUSE_BREAKS.split(user_text):
        clause = clause.strip()
        if _SMILE_REQUEST.fullmatch(clause):
            return clause
    return ""


def strong_smile_request(user_text: str) -> str:
    request = _affirmative_smile_request(user_text)
    return request if re.search(_EXAGGERATED_SMILE, request) else ""


def suppresses_teeth(user_text: str) -> bool:
    return any(not re.search(r"太|过于|那么|这么", match.group()) for match in _NEGATED_TEETH.finditer(user_text))


def suppresses_smile(user_text: str) -> bool:
    return bool(_NO_SMILE.search(user_text)) or any(
        clause.strip() in {"不笑", "不微笑"} for clause in _CLAUSE_BREAKS.split(user_text)
    )


def keeps_expression(user_text: str) -> bool:
    return any(not _cue_is_negated(match) for match in _KEEP_EXPRESSION.finditer(user_text))


def ordinary_smile_requested(user_text: str) -> bool:
    return not suppresses_smile(user_text) and any(
        not _cue_is_negated(match) for match in _ORDINARY_SMILE.finditer(user_text)
    )


def wants_visible_reaction(user_text: str, summary: str) -> bool:
    if keeps_expression(user_text) or suppresses_smile(user_text) or strong_smile_request(user_text):
        return False
    return bool(
        re.search(r"(?:看看|想看)[^，,。；;\n]{0,12}(?:什么反应|的反应|怎么反应)", user_text)
        or re.search(
            r"角色[^，,。；;\n]{0,14}(?:逗笑|打趣|憋笑|愣了一下|惊讶)|后的(?:打趣|揶揄)(?:自拍|反应)",
            summary,
        )
    )


def _remove_expression_cues(text: str, cues: str) -> str:
    adjusted = re.sub(rf"(?:{_SMILE_NEGATION})?(?:{cues})", "", text)
    if adjusted == text:
        return text
    adjusted = re.sub(r"([，,。；;])(?:\s|[，,。；;])+", r"\1", adjusted)
    return adjusted.strip(" ，,。；;\n")


def natural_smile_text(text: str, *, no_smile: bool = False, no_teeth: bool = False) -> str:
    """Soften known exaggerated cues in model-authored text."""
    for pattern, replacement in _MODEL_SMILE_CUES:
        text = re.sub(
            pattern,
            lambda match: match.group() if _cue_is_negated(match) else match.expand(replacement),
            text,
        )
    if no_smile:
        return _remove_expression_cues(text, _SMILING_CUES + "|" + _EXAGGERATED_SMILE)
    if no_teeth:
        return _remove_expression_cues(text, _TEETH_SMILE)
    return text


def without_expression_cues(text: str) -> str:
    """Drop known facial cues when the previous photo supplies the expression."""
    return _remove_expression_cues(natural_smile_text(text), _EXPRESSION_CUES)


_CHEST_UP = r"(?:胸(?:部|口)?|肩(?:部|膀|颈)?)(?:及)?以上"
_WAIST_UP = r"腰(?:部)?(?:及)?以上"

_DIMENSIONS = {
    "capture": r"不要自拍|别自拍|不要对镜|别对镜|不对镜|前置(?:摄像头|镜头|自拍)?|手持自拍|对镜(?:自拍)?|镜前(?:自拍)?|镜子(?:前)?(?:自拍)?|定时(?:拍摄|自拍|拍)?|第三人(?:视角|拍摄)?|恋人视角|他拍",
    "framing": rf"固定距离|同一距离|距离不变|距离别变|(?:半身|近景)?(?:{_CHEST_UP}|{_WAIST_UP})(?:的?(?:半身|近景))?|全身|半身|脸部特写|大头照|头肩|近景|中景|远景|肩膀以下|肩以下|full[- ]body|half[- ]body|close[- ]?up",
    "angle": r"固定机位|同一机位|固定角度|同一角度|不要换角度|不换角度|别换角度|俯拍|仰拍|俯视|仰视|平视|正脸|正面|侧脸|背影|低角度|高角度|从上往下|从下往上|overhead|low[- ]angle|high[- ]angle",
    "action": r"同一动作|动作不变|动作别变|保持动作|比耶|比心|剪刀手|歪头|托腮|捧脸|回头|侧身|抱膝|叉腰|伸懒腰|蹲着|蹲下|坐着|坐姿|站着|站姿|躺着|趴着",
    "expression": (
        r"同一表情|表情不变|表情别变|不看(?:向)?镜头|不要看(?:向)?镜头|别看(?:向)?镜头|看(?:向)?镜头|"
        r"不要微笑|别微笑|不微笑|不要大笑|别大笑|"
        rf"(?:不要|别|不)笑(?!{_SMILE_SCALE})|"
        r"微笑|笑一个|笑一下|偷笑|闭眼|眨眼|smil(?:e|ing)|wink|"
        rf"{_EXPRESSION_MOOD}(?:一点|点|些|一下|的?(?:表情|神情|正脸照|自拍|照|样子))|"
        rf"(?:表情|神情)(?:要|带点)?{_EXPRESSION_MOOD}|"
        rf"(?:别|不要|不许)(?:一直|总是)?{_EXPRESSION_MOOD}|"
        rf"^{_EXPRESSION_MOOD}$"
    ),
}


def user_constraints(user_text: str) -> dict[str, str]:
    """Recognize independent explicit dimensions; one match never disables others."""
    result = {}
    for field, pattern in _DIMENSIONS.items():
        matches = list(dict.fromkeys(re.findall(pattern, user_text, re.IGNORECASE)))
        # Multiple alternatives belong in the per-shot plan, not a global lock.
        if matches and not (len(matches) > 1 and re.search(r"分别|各|都要|都来|或者|或是", user_text)):
            clauses = [
                clause.strip()
                for clause in _CLAUSE_BREAKS.split(user_text)
                if re.search(pattern, clause, re.IGNORECASE)
            ]
            # Keep the original clause, including negation and its context.
            result[field] = "；".join(dict.fromkeys(clauses))
    smile_request = _affirmative_smile_request(user_text)
    if smile_request:
        result.setdefault("expression", smile_request)
    return result


def varied_choice(values, used):
    """Choose the least-used available value, avoiding a fixed numbered routine."""
    least = min(used.count(value) for value in values)
    return random.choice([value for value in values if used.count(value) == least])


_CAPTURE_MODES = (
    ("mirror", r"对镜|镜前|镜面自拍"),
    ("timer", r"定时|倒计时|支架|三脚架"),
    ("other", r"第三人|他拍|别人拍|朋友拍"),
)
_HANDHELD_CAPTURE = r"手持|前置|手举(?:着)?手机|举(?:着|起)?手机|手机举在(?:身前|面前)|(?:伸手|单手)拿(?:着|起)?手机"
_GAZE_DIRECTION = r"看(?:向|着)?|望(?:向|着)|(?:视线|目光)(?:偏向|偏|转向|朝向?)"
_GAZE_MODES = (
    ("away", r"不(?:看(?:向|着)?|直视|望向)镜头|(?:避开|离开|移开)镜头"),
    ("left", rf"(?:{_GAZE_DIRECTION})(?:左边|左侧|左方|左)"),
    ("right", rf"(?:{_GAZE_DIRECTION})(?:右边|右侧|右方|右)"),
    ("far", rf"(?:{_GAZE_DIRECTION})(?:远方|远处|窗外)"),
    ("camera", r"(?:看(?:向|着)?|直视|望向)镜头"),
)


def is_handheld_capture(text: str) -> bool:
    return bool(re.search(_HANDHELD_CAPTURE, text)) and not any(
        re.search(pattern, text) for _, pattern in _CAPTURE_MODES
    )


def shot_field_key(field: str, value: str) -> str:
    """Compare common capture phrasing without erasing angle or distance changes."""
    key = re.sub(r"[\s，,。.!！?？；;]+", "", value.casefold())
    if field == "capture":
        for mode, pattern in _CAPTURE_MODES:
            if re.search(pattern, key):
                return mode
        return "handheld" if is_handheld_capture(key) else key
    if field == "gaze":
        return next((mode for mode, pattern in _GAZE_MODES if re.search(pattern, key)), "")
    if field == "framing":
        for frame, pattern in (
            ("full", r"全身|头到脚|full[- ]?body"),
            ("half", r"半身|腰部以上|half[- ]?body"),
            ("close", r"近景|头肩|肩颈以上|胸口以上|脸部特写"),
            ("medium", r"中景|膝盖以上"),
        ):
            if re.search(pattern, key):
                return frame
    key = re.sub(r"轻轻|轻微|稍微|微微|自然|轻(?=托|扬|弯)", "", key)
    if field == "angle":
        if re.fullmatch(r"(?:正面|正前方)?平视(?:正面|正前方)?", key):
            return "平视"
        if "微侧" in key and not re.search(
            r"[左右俯仰\d°]|侧后|背后|正后|从[上下](?:方|往)|[高低](?:机位|角度)"
            r"|[一二两三四五六七八九十百]+度",
            key,
        ):
            return "微侧"
    key = re.sub(r"托(?:着)?(?:下巴|下颌)", "托腮", key)
    if field == "action":
        for _, pattern in _GAZE_MODES:
            key = re.sub(pattern, "", key)
    return key


_GROUP_FRAMING = (
    ("close", _CHEST_UP),
    ("half", _WAIST_UP),
    ("medium", r"膝(?:部|盖)?(?:及)?以上"),
    ("full", r"头到脚"),
    ("close", r"近景|特写|大头照|头肩|close[- ]?up"),
    ("half", r"半身|half[- ]?body"),
    ("medium", r"中景"),
    ("full", r"全身|full[- ]?body"),
)
_GROUP_ACTIONS = (
    ("collar", r"(?:扯|拉|提|捏|整理|扶|拎)[^，。；]{0,8}(?:衣领|领口)"),
    ("wave", r"挥手|挥(?:了)?一下手|招手|手[^，。；]{0,9}(?:招呼|问好)"),
    ("v_sign", r"比耶|剪刀手|v(?:字|形)手势"),
    ("heart", r"比心|手指爱心"),
    ("chin_rest", r"托(?:着)?(?:下巴|下颌|腮)|抵(?:着)?(?:脸侧|下巴)"),
    ("face_hold", r"捧(?:着)?脸"),
    ("hair", r"(?:拨|撩|碰|捋|理|轻触)[^，。；]{0,5}(?:发梢|头发|发丝)"),
    ("cross_arms", r"抱臂|双臂交叉|双手交叉抱胸"),
    ("hand_hip", r"叉腰"),
    ("turn", r"侧身|转身|身体侧转|回头"),
    ("lean_back", r"靠(?:向|着|在)?椅背|后靠|向后仰靠"),
    ("stand", r"站着|站立|站直"),
    ("sit", r"坐着|坐下|坐直|坐在|坐姿"),
    ("crouch", r"蹲下|蹲着|半蹲"),
)


def group_field_key(field: str, value: str) -> str:
    """Classify visible group differences, leaving ambiguous descriptions uncounted."""
    if field == "gaze":
        return shot_field_key(field, value)
    if field in {"framing", "action"}:
        patterns = _GROUP_FRAMING if field == "framing" else _GROUP_ACTIONS
        return next((key for key, pattern in patterns if re.search(pattern, value, re.I)), "")
    if field != "angle":
        return ""

    side = next(
        (
            key
            for key, pattern in (
                ("left", r"从左|偏左|左侧|左前|左斜"),
                ("right", r"从右|偏右|右侧|右前|右斜"),
            )
            if re.search(pattern, value)
        ),
        "",
    )
    small_tilt = re.search(r"(?:微微|微|略微|略|稍微|稍)(?:俯|仰|向[上下])", value)
    height = ""
    if not small_tilt or re.search(r"\d|明显|[一二两三四五六七八九十]+度", value):
        height = next(
            (
                key
                for key, pattern in (
                    ("high", r"俯拍|俯视|高机位|高角度|从上方|斜上方|向下|从上往下"),
                    ("low", r"仰拍|仰视|低机位|低角度|低处|向上|从下往上"),
                )
                if re.search(pattern, value)
            ),
            "",
        )
    return "_".join(filter(None, (side, height))) or (
        "level" if small_tilt or re.search(r"平视|微侧|正面|眼平", value) else ""
    )


def expression_options(mood: str, summary: str) -> tuple[str, ...]:
    """Use an explicitly described character reaction, then the character's mood."""
    reaction = re.search(
        r"角色(?:被[^，,。；;\n]*?逗得|感到|显得|心情[：: ]*)([^，,。；;\n]+)",
        summary,
    )
    for text in (reaction.group(1) if reaction else "", mood):
        if re.search(r"疲惫|困倦|困了|犯困|疲倦|累了", text):
            return (
                "眼睑略沉，嘴角放松",
                "轻抿嘴唇，目光柔和带倦意",
                "眉眼放松，带一点困倦的浅笑",
            )
        if re.search(r"低落|难过|伤心|委屈", text):
            return (
                "眉眼略垂，目光安静",
                "嘴唇轻抿，眼神带一点委屈",
                "目光柔和，嘴角轻轻放松",
            )
        if re.search(r"生气|烦躁|不悦", text):
            return (
                "眉间轻拢，目光认真",
                "嘴角轻抿，眼神略带不满",
                "轻轻挑眉，表情认真",
            )
        if re.search(r"开心|高兴|兴奋|愉快|快乐|开怀|舒畅|喜悦", text):
            return (
                "眉眼舒展，眼角带笑，嘴角微扬",
                "眼神明亮，眉眼轻弯，嘴角轻轻扬起",
                "眼角含笑，嘴角轻扬，带着轻松的笑意",
            )
    return (
        "眉眼舒展，嘴角自然放松",
        "目光柔和，带一点浅笑",
        "轻抿嘴唇，眼神带一点好奇",
        "眼角带笑，神情自然明亮",
    )


def choose_pose(slot: dict, recent: list[str], user_prompt: str, photo_type: str, used: list[str]) -> dict | None:
    """Use scene-compatible neutral habits, excluding the last three and this set."""
    if photo_type not in {"selfie", "outfit"}:
        return None
    user_tags = {name for name, pattern in _SCENES.items() if re.search(pattern, user_prompt)}
    context = str(slot.get("location", "")) + " " + str(slot.get("activity", ""))
    tags = user_tags or {name for name, pattern in _SCENES.items() if re.search(pattern, context)}
    excluded = set(recent[-3:]) | set(used)
    candidates = [
        item for item in POSES if item["tags"] and tags.intersection(item["tags"]) and item["id"] not in excluded
    ]
    if not candidates:
        candidates = [item for item in POSES if not item["tags"] and item["id"] not in excluded]
    return random.choice(candidates) if candidates else None
