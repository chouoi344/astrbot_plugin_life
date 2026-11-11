"""Deterministic prompt assembly from one life-state snapshot; no planning model.

Adapted from the user's preserved stage2-photo core/photo_prompts.py and
photo_poses.py. Portrait texture/capture wording originates from the authorized
astrbot_plugin_gitee_aiimg v5.7.8 main.py:_build_selfie_prompt, by 木有知、Zhalslar:
https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg
The 1K/2K/4K size idea follows that plugin's core/output_spec.py (see core/sizes.py).
"""

import re
from dataclasses import dataclass

from .face_lock import apply_face_lock
from .poses import (
    REACTION_GUIDANCE,
    SMILE_GUIDANCE,
    choose_pose,
    expression_options,
    group_field_key,
    is_handheld_capture,
    keeps_expression,
    natural_smile_text,
    ordinary_smile_requested,
    shot_field_key,
    strong_smile_request,
    suppresses_smile,
    suppresses_teeth,
    user_constraints,
    varied_choice,
    wants_visible_reaction,
    without_expression_cues,
)
from .sizes import DEFAULT_RATIO, SIZES_4K, SizeSetting

# Ratios accepted from chat; actual pixel sizes come from the size setting.
SIZE_MAP = SIZES_4K

_RATIO_LITERAL = re.compile(r"(?<![0-9])([0-9]{1,2})\s*[:：比]\s*([0-9]{1,2})(?![0-9])")
_RATIO_NEGATION = re.compile(
    r"(?:不要|别|不用|不想(?:要)?)(?:再)?\s*"
    r"(?:(?:用|选|按|拍(?:成)?|改成|做成|采用)\s*)?"
    r"(?:比例\s*(?:为|用|[:：])?\s*)?$"
)


def _affirmative_ratio_match(pattern: str, clause: str) -> bool:
    return any(not _RATIO_NEGATION.search(clause[: match.start()]) for match in re.finditer(pattern, clause))


def _requested_ratio(text: str, proposed: str, default: str = DEFAULT_RATIO) -> str:
    clauses = re.split(r"[，,。；;！？!?]", text)
    rejected = set()
    for clause in clauses:
        for match in _RATIO_LITERAL.finditer(clause):
            ratio = f"{match[1]}:{match[2]}"
            if ratio in SIZE_MAP:
                if _RATIO_NEGATION.search(clause[: match.start()]):
                    rejected.add(ratio)
                else:
                    return ratio
    if proposed in rejected:
        proposed = ""
    width, height = map(int, (proposed or default).split(":"))
    for clause in clauses:
        if _affirmative_ratio_match(r"正方形|方形(?:照片|图)|方图", clause):
            return "1:1"
        if _affirmative_ratio_match(r"横屏|横图|横幅|横版|横向|宽屏(?!显示器|电视)", clause):
            if proposed and width > height:
                return proposed
            return "4:3" if "16:9" in rejected else "16:9"
        if _affirmative_ratio_match(r"竖屏|竖图|竖幅|竖版|竖向", clause):
            return default
        if proposed and _affirmative_ratio_match(
            r"(?:保持|沿用|按|还是).{0,8}(?:原图|原来|刚才|上[一]?张).{0,4}比例", clause
        ):
            return proposed
    return default


# Adapted from the authorized reference author's capture policy, preserving the
# explicit-user-direction priority while keeping ordinary phones out of frame.
CAPTURE_POLICY = (
    "拍摄逻辑：近景手持自拍采用真实的一臂距离、自然透视与随手构图，手机通常在画面外；"
    "全身取景可用手机定时或场景中可用的镜面自拍，不强行用一臂距离拍全身。"
    "对镜时允许手机自然入镜，持握、数量和镜面反射必须合理；不把每张都改成对镜。"
    "定时、第三人或恋人视角的设备通常在画面外，不生成多余手机、屏幕或相机界面。"
    "用户明确指定的拍法、设备入镜要求优先。手机感指生活化的距离、构图与动作。"
)

FOOD_GUIDANCE = (
    "日常食物按现场自然呈现，油光适度，别自行增添强反光、浓烈红亮或大量蒸汽。"
    "用户明确要求的质感、蒸汽、广告风或其他拍法优先。"
)
_FOOD_STYLING = re.compile(
    r"酱汁浓郁[、 ]*色泽红亮|色泽红亮|油光(?:锃亮|四溢|强烈|很重|明显)|"
    r"(?:散发着|冒着)?腾腾热气|热气腾腾|大量蒸汽|浓烈蒸汽"
)
_FOOD_STYLE_REQUEST = re.compile(r"广告|美食海报|油光|红亮|热气|蒸汽")
_LIFE_ANCHOR = re.compile(r"此刻|现在|当前|眼前|窗外|你(?:的周围|那边|那里|所在|正在|在吃)|日程")


def _ordinary_food_text(text: str) -> str:
    if not _FOOD_STYLING.search(text):
        return text
    return "，".join(
        rest for clause in re.split(r"[，,；;。]", text) if (rest := _FOOD_STYLING.sub("", clause).strip())
    )


_OUTFIT_PARTS = (
    ("overall_style", "整体风格"),
    ("clothing", "服装"),
    ("footwear", "鞋袜"),
    ("accessories", "配饰"),
    ("hair", "发型"),
    ("makeup", "妆容"),
    ("details", "颜色材质与版型"),
    ("constraints", "穿戴约束"),
)


@dataclass(frozen=True)
class ShotSpec:
    capture: str = ""
    framing: str = ""
    angle: str = ""
    action: str = ""
    expression: str = ""


_SPEC_FIELDS = ("capture", "framing", "angle", "action", "expression")


def parse_shot_plan(value, count: int) -> tuple[ShotSpec, ...]:
    """Validate the optional tool input once; internal plans are trusted dataclasses."""
    if value is None or value == []:
        return ()
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("逐张拍摄方案应为列表，数量与照片张数一致")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) - set(_SPEC_FIELDS):
            raise ValueError("逐张方案只支持capture、framing、angle、action、expression五个字段")
        if any(not isinstance(text, str) or len(text) > 240 or "\x00" in text for text in row.values()):
            raise ValueError("逐张方案的每项应为不超过240字的文字")
        cleaned = {field: text.strip() for field, text in row.items()}
        if not any(cleaned.values()):
            raise ValueError("每张方案至少填写一个具体拍摄细节")
        result.append(ShotSpec(**cleaned))
    return tuple(result)


@dataclass(frozen=True)
class Shot:
    prompt: str
    size: str
    pose_id: str = ""
    spec: ShotSpec | None = None


def selected_outfit(state: dict) -> dict:
    """Return the current wardrobe row, including image_path, without guessing home."""
    slot = state.get("current_slot")
    if not slot:
        return {}
    if isinstance(state.get("current_outfit"), dict):
        return state["current_outfit"]
    outfits = state.get("outfit", {})
    if isinstance(outfits, str):
        return {"description": outfits}
    key = slot.get("outfit_key", "")
    if key.startswith("sports:"):
        value = next((row for row in outfits.get("sports", []) if row.get("key") == key), {})
    else:
        value = outfits.get(key or slot.get("outfit_type"), {})
    return {"description": value} if isinstance(value, str) else value


def _outfit_description(value: dict | str) -> str:
    if isinstance(value, str):
        return re.sub(r"^(?:临时搭配\s*[:：]\s*)+", "", value).strip()
    description = value.get("description", "").strip()
    if description:
        return re.sub(r"^(?:临时搭配\s*[:：]\s*)+", "", description).strip()
    parts = value.get("parts", value)
    return "；".join(label + "：" + parts[key].strip() for key, label in _OUTFIT_PARTS if parts.get(key))


def has_portrait_scene_text(state: dict, life_status: str) -> bool:
    """Require the frozen text to carry both the setting and the clothing."""
    slot = state.get("current_slot") or {}
    return (
        life_status == "ready"
        and bool(str(slot.get("location") or "").strip())
        and bool(_outfit_description(selected_outfit(state)) or _outfit_description(slot.get("outfit", "")))
    )


def _hair(state: dict, outfit: dict, wearing: str) -> str:
    slot = state["current_slot"]
    parts = outfit.get("parts", {})
    for value in (
        slot.get("hair"),
        state.get("hair"),
        outfit.get("hair"),
        parts.get("hair"),
    ):
        if value:
            return value.strip()
    match = re.search(r"(?:^|[；;\n])\s*发型\s*[:：]\s*([^；;\n]+)", wearing)
    return match.group(1).strip() if match else ""


def _fixed_context(
    state: dict,
    photo_type: str,
    life_status: str,
    source_continuity: bool,
    use_current_scene: bool,
) -> tuple[str, dict]:
    people = photo_type in {"selfie", "outfit"}
    dimensions = "地点、天气、时间、光线与环境物件"
    if people:
        dimensions += "、穿搭与发型"
    lines = [
        "整组固定部分（每张共用，用户明确要求最高优先级）：",
        dimensions
        + (
            "整组保持连续，保持同一人物和原有画风。"
            if people
            else "整组保持连续，画面风格按用户要求，默认不继承人物参考图画风。"
        ),
        (
            "同一生活活动内允许自然的小拍照动作；这不是改写日程或安排新事件。"
            "背景保持同一空间关系，按逐张方案决定拍法、机位、距离、动作与表情。"
        )
        if people
        else "同一地点与主体保持连续，背景物件保持同一空间关系；可变取景距离与角度，不复制同一构图。",
        "只固定用户原话明确指定的维度，其他维度按本张方案变化。",
    ]
    slot = state.get("current_slot") or {}
    if source_continuity:
        visible_details = "环境与穿搭" if people else "环境与主体"
        lines.append(f"本组的{visible_details}以选中的来源成片实际可见内容为准。")
        return "\n".join(lines), {}
    if life_status != "ready" or not slot:
        status = {
            "not_installed": "日程插件未安装",
            "not_ready": "日程生活尚未就绪",
            "disabled": "日程联动已关闭",
        }.get(life_status, "日程生活尚未就绪")
        lines.extend(
            [
                status + "。没有可用生活状态，仅按用户要求和本次参考图用途创作。",
                ("没有用户明确指定的环境、衣服与画风" if people else "没有用户明确指定的环境、主体与风格")
                + "由首张确定，后续沿用；不得声称这些细节来自角色此刻的生活状态。",
            ]
        )
        return "\n".join(lines), {}
    if not people and (
        not use_current_scene or (photo_type == "food" and not re.search(r"吃|餐|饭|宵夜", slot.get("activity") or ""))
    ):
        lines.append("按用户本轮指定的画面取景，环境与主体由用户要求及本组首张确定。")
        return "\n".join(lines), {}
    lines.append("以下生活状态仅作为默认背景；与用户本次要求冲突的内容以用户要求为准：")
    weather = state.get("weather", {})
    known = (
        ("地点", slot.get("location")),
        ("活动", slot.get("activity")),
        ("时间", state.get("now")),
        ("天气", weather.get("summary")),
        (
            "光线",
            slot.get("lighting") or slot.get("light") or state.get("lighting") or state.get("light"),
        ),
        ("背景", slot.get("background") or state.get("background")),
    )
    lines.extend(label + "：" + str(value) for label, value in known if value)
    if people:
        outfit = selected_outfit(state)
        wearing = _outfit_description(outfit) or _outfit_description(slot.get("outfit", ""))
        hair = _hair(state, outfit, wearing)
        if wearing:
            lines.append("角色穿搭：" + wearing)
        if hair:
            lines.append("角色发型：" + hair)
    lines.append(
        "未记录的环境和穿搭按用户要求在首张确定，后续沿用；拍摄动作和镜头按逐张方案变化。"
        if people
        else "未记录的环境与主体细节按用户要求在首张确定，后续沿用；镜头按逐张方案变化。"
    )
    return "\n".join(lines), slot


def reference_roles_note(roles: list[str]) -> str:
    return "本次参考图用途（严格按各自编号绑定）：\n" + "\n".join(
        f"第{index}张参考图：{role}" for index, role in enumerate(roles, 1)
    )


BODY_REFERENCE_ROLE = (
    "角色体态：只参考角色肩宽、腰身、腿部比例和身体轮廓。"
    "脸与原有质感按角色身份图，衣服、动作、背景和光线按本轮拍摄方案。"
    "不复制这张图的脸、发型、衣服、姿势或背景。"
)
_FULL_BODY_CUE = r"(?:全身|头到脚|full[- ]?body)"
_NO_FULL_BODY = re.compile(
    rf"(?:不要|别|不用|不必|不拍|不想)[^，,。！？!?；;\n]{{0,4}}{_FULL_BODY_CUE}",
    re.I,
)
_OUTFIT_DETAIL_CUE = re.compile(
    r"特写|局部|(?:领口|衣领|袖口|面料|纹理)[^，,。！？!?；;\n]{0,5}(?:细节|看清|拍清)"
    r"|(?:拍|看看|想看|只看)[^，,。！？!?；;\n]{0,5}(?:领口|衣领|袖口)"
)


def body_reference_framing(framing: str, user_text: str, *, single_outfit: bool = False) -> bool:
    """Use the optional body reference only when the final shot shows the whole body."""
    return (
        group_field_key("framing", framing) == "full"
        and not _NO_FULL_BODY.search(framing)
        and not _NO_FULL_BODY.search(user_text)
        and not (
            single_outfit and _OUTFIT_DETAIL_CUE.search(user_text) and not re.search(_FULL_BODY_CUE, user_text, re.I)
        )
    )


def continuation_role(
    photo_type: str,
    *,
    keep_expression: bool,
    generic: bool = False,
    has_identity_references: bool = False,
) -> str:
    prefix = "上一张成功照片："
    if photo_type == "food":
        return prefix + "延续原图实际可见的同一份食物、容器、摆放和环境，按本张方案换取景。"
    if photo_type == "scene" or generic:
        return prefix + "以原图实际可见的主体、画风和环境为准，按用户原话调整本张画面。"
    return (
        prefix
        + (
            "只参考实际可见的衣服、环境及现场光线；人物身份和质感仍按对应的原始身份参考图。"
            if has_identity_references
            else "沿用成片实际可见的外貌和画风、衣服与环境。"
        )
        + "未要求更改的衣服与场景从这张成片延续；与方案补写的衣服或场景冲突时，以成片为准。"
        + (
            "保持成片实际可见的表情，其他维度按本张方案。"
            if keep_expression
            else "拍法、构图、动作和表情按本张方案及用户原话。"
        )
    )


def scene_reference_note(
    index: int,
    photo_type: str = "selfie",
    *,
    generic: bool = False,
    has_identity_references: bool = False,
) -> str:
    """Keep group context continuous without competing with original identity images."""
    if generic:
        return f"第{index}张参考图是本组首张成片，延续同一主体与用户要求，按本张方案改变未锁定的取景。"
    if photo_type in {"scene", "food"}:
        subject = "同一份食物、已有容器、摆放" if photo_type == "food" else "同一主体、地点与主要物件"
        return (
            f"第{index}张参考图是本组首张成片，仅延续{subject}及环境、时间与光线。"
            "画面风格按用户要求，不绑定人物参考图的身份或画风。"
            "本张取景距离和角度按本张方案变化，不复制首张构图，不额外加入人物或物件；用户原话优先。"
        )
    return (
        f"第{index}张参考图是本组首张成片，"
        + (
            "只参考衣服、场景、时间与光线，人物身份和质感仍按对应的原始身份参考图。"
            if has_identity_references
            else "沿用首张已确定的角色外貌与原有画风，以及衣服、场景、时间和光线。"
        )
        + "首张实际可见的衣服与环境优先于日程默认。用户原话明确固定的维度保持。"
        "其余机位、距离、动作或视线以本张具体方案为准，不要复制首张的姿势与构图。"
        "表情按用户原话和本张方案自然表达。"
    )


_FRAMINGS = (
    "近景，肩颈以上入画，脸部接近镜头",
    "半身，腰部以上入画，留出手部动作空间",
    "中景，膝盖以上及一部分环境入画",
    "全身，头到脚完整入画，留出身体姿态空间",
)
_ANGLES = (
    "自然平视",
    "左前侧约45度",
    "右前侧约45度",
    "从上方约30度俯拍",
    "从稍低处向上约15度",
)
_FOOD_FRAMINGS = (
    "普通吃饭距离，食物及已有容器完整入画，保留适量已有桌面",
    "普通吃饭距离，稍偏左取景，保留已有容器边缘与少量桌面背景",
    "普通吃饭距离，稍宽取景，保留已有容器及适量桌面环境",
    "普通吃饭距离，稍靠近食物，仍保留已有容器边缘与少量桌面",
)
_FOOD_ANGLES = (
    "就餐者餐桌前视角，自然向下约25度轻俯拍",
    "餐桌前从左前侧向下约30度轻俯拍",
    "餐桌前从右前侧向下约35度轻俯拍",
    "就餐者视角，向下约40度自然俯拍",
)
_NEGATIVES = (
    (r"(?:不要|别|不拍).{0,3}全身", r"全身", "framing"),
    (r"(?:不要|别).{0,3}托[着]?腮", r"托[着]?腮", "action"),
    (
        r"(?:不|不要|别).{0,2}看(?:向)?镜头",
        r"(?:看(?:向)?|直视|望向)镜头",
        "expression",
    ),
    (r"(?:不要|别|不).{0,2}对镜", r"对镜|镜前自拍", "capture"),
)


def _wide_framing(framing: str) -> bool:
    return bool(re.search(r"全身|头到脚|中景|远景|full[- ]body", framing, re.I)) and not bool(
        re.search(r"(?:不要|别|不拍|不想).{0,3}全身", framing)
    )


def _capture(framing: str, slot: dict, used: list[str], requested: str = "") -> str:
    if re.search(r"不要自拍|别自拍", requested):
        return "第三人用手机拍摄，设备位于画面外"
    no_mirror = bool(re.search(r"不要对镜|别对镜|不对镜", requested))
    full = _wide_framing(framing)
    if no_mirror and re.search(r"定时|第三人|他拍", requested):
        return requested
    if requested and not no_mirror:
        if full and is_handheld_capture(requested):
            return "手机前置镜头定时拍摄，手机固定在画面外，距离足以完整取景"
        return requested
    if not full:
        return "手机前置手持自拍，一臂距离，拍摄设备位于画面外"
    context = str(slot.get("location", "")) + " " + str(slot.get("background", ""))
    choices = ["手机定时拍摄，手机固定在画面外，距离足以完整取景"]
    if not no_mirror and re.search(r"镜", context):
        choices.append("利用场景中的镜面对镜自拍，手机自然入镜，反射与持握合理")
    return varied_choice(choices, used)


def _without_handheld(text: str) -> str:
    return "，".join(
        clause.strip() for clause in re.split(r"[，,；;。]", text) if clause.strip() and not is_handheld_capture(clause)
    )


def _composition(spec: ShotSpec) -> tuple[str, ...]:
    return (
        shot_field_key("framing", spec.framing),
        shot_field_key("angle", spec.angle),
        shot_field_key("action", spec.action),
        shot_field_key("gaze", spec.action + "，" + spec.expression),
    )


_SAME_GROUP_COMPOSITION = re.compile(
    r"(?:同(?:一|个|样)|相同)的?构图|构图(?:不变|别变|一致)|固定构图"
    r"|(?:保持|沿用|按)(?:原图|首图|首张|第一张|第1张|上一张|相同)?的?构图"
)


def _keeps_group_composition(user_text: str) -> bool:
    for clause in re.split(r"[，,。.!！?？；;\n]", user_text):
        match = _SAME_GROUP_COMPOSITION.search(clause)
        if match and not re.search(
            r"(?:不要|别|不用|不必|不能|不想)(?:(?:每张|四张|全都|都|用)|\s)*$",
            clause[: match.start()],
        ):
            return True
    return False


def keeps_group_camera(user_text: str) -> bool:
    return bool(
        _keeps_group_composition(user_text)
        or re.search(
            r"(?:保持|沿用|按)原图|机位不变|距离不变|同一机位|同一距离|固定机位|固定距离|同一角度|固定角度",
            user_text,
        )
    )


def group_camera_changed(first: ShotSpec, later: ShotSpec) -> bool:
    for field in ("angle", "framing"):
        earlier_key = group_field_key(field, getattr(first, field))
        later_key = group_field_key(field, getattr(later, field))
        if earlier_key and later_key and earlier_key != later_key:
            return True
    return False


def _portrait_group_composition(spec: ShotSpec) -> tuple[str, ...]:
    framing = group_field_key("framing", spec.framing) or "close"
    action = group_field_key("action", spec.action)
    if framing == "close" and action in {
        "lean_back",
        "cross_arms",
        "hand_hip",
        "sit",
        "stand",
        "crouch",
    }:
        action = ""
    return (
        framing,
        group_field_key("angle", spec.angle) or "level",
        action,
        group_field_key("gaze", spec.action + "，" + spec.expression) or "camera",
    )


def _concrete_group_framing(framing: str, action: str) -> str:
    key = group_field_key("framing", framing)
    if key == "close":
        return re.sub(r"(?:近景){2,}", "近景", framing.replace("半身", "近景"))
    if key != "half":
        return framing
    if not re.search(r"腰(?:部|线)?以上|腰部入画", framing):
        framing += "，腰部以上入画，手部动作清楚可见"
    for prop in ("桌沿", "椅背"):
        if prop in action and prop not in framing:
            framing += f"，画面可见已有{prop}"
    return framing


def _new_value(field, pool, history, *, key=shot_field_key):
    used = [key(field, getattr(spec, field)) for spec in history]
    choices = [value for value in pool if key(field, value) not in used]
    return varied_choice(choices or pool, [getattr(spec, field) for spec in history])


def _specs(
    slot,
    mood,
    user_prompt,
    user_text,
    photo_type,
    count,
    shot_plan,
    recent,
    previous,
    *,
    transform,
    keep_expression,
):
    locks = user_constraints(user_text)
    if photo_type == "food" and "framing" not in locks:
        detail = [clause.strip() for clause in re.split(r"[，,。.!！?？；;\n]", user_text) if "特写" in clause]
        if detail:
            locks["framing"] = "；".join(detail)
    people = photo_type in {"selfie", "outfit"}
    portrait_group = people and count > 1
    group_composition_fixed = portrait_group and _keeps_group_composition(user_text)
    composition = _portrait_group_composition if portrait_group else _composition
    compare_key = group_field_key if portrait_group else shot_field_key
    if people:
        strong_smile = strong_smile_request(user_text)
        if strong_smile:
            locks["expression"] = strong_smile
        if suppresses_smile(user_text) and "expression" not in locks:
            locks["expression"] = "神态自然放松，不微笑"
        if keep_expression:
            locks["expression"] = "保持上一张成片实际可见的表情"
    else:
        locks.pop("expression", None)
    angle_pool = _FOOD_ANGLES if photo_type == "food" else _ANGLES
    expressions = expression_options(mood, user_prompt) if people else ()
    ordinary_smile = people and ordinary_smile_requested(user_text)
    if ordinary_smile:
        expressions = ("眉眼柔和明亮，嘴角轻轻扬起，带自然笑意",)
    history = [previous] if previous is not None else []
    used_ids = []
    result = []
    for index in range(count):
        proposed = (
            ShotSpec(**{field: transform(getattr(shot_plan[index], field)) for field in _SPEC_FIELDS})
            if shot_plan
            else ShotSpec()
        )
        needs_seed = any(field not in locks and not getattr(proposed, field) for field in ("angle", "action"))
        seed = choose_pose(slot, recent, user_text, photo_type, used_ids) if needs_seed else None
        framing_pool = (
            _FRAMINGS
            if people
            else _FOOD_FRAMINGS
            if photo_type == "food"
            else (
                "近景，突出主体局部细节",
                "中景，主体与周边环境一同入画",
                "远景，完整记录环境关系",
                "特写，突出主体材质与纹理",
            )
        )
        if portrait_group and is_handheld_capture(locks.get("capture", "")):
            framing_pool = _FRAMINGS[:2]
        if count == 1 and previous is None:
            default_frame = _FRAMINGS[3 if photo_type == "outfit" else 0] if people else framing_pool[0]
        else:
            default_frame = varied_choice(framing_pool, [spec.framing for spec in history])
        framing = locks.get("framing") or proposed.framing or default_frame
        if portrait_group and "framing" not in locks:
            framing = _concrete_group_framing(framing, proposed.action)
        defaults = {
            "capture": _capture(
                framing,
                slot,
                [spec.capture for spec in history],
                locks.get("capture") or proposed.capture,
            )
            if people
            else "就餐者在餐桌前用手机后置镜头随手拍，设备位于画面外"
            if photo_type == "food"
            else "手机后置镜头取景，设备位于画面外",
            "framing": framing,
            "angle": seed["angle"] if seed else varied_choice(angle_pool, [spec.angle for spec in history]),
            "action": seed["action"]
            if seed
            else "保持同一份食物与摆盘"
            if photo_type == "food"
            else "同一场景内换位置取景，环境物件保持连续",
            "expression": varied_choice(
                expressions,
                [spec.expression for spec in history],
            )
            if people
            else "",
        }
        values = {field: locks.get(field) or getattr(proposed, field) or defaults[field] for field in _SPEC_FIELDS}
        values["framing"] = framing
        fixed_camera = result[0][0] if group_composition_fixed and result else None
        if fixed_camera:
            for field in ("capture", "framing", "angle"):
                values[field] = getattr(fixed_camera, field)
            framing = fixed_camera.framing
        if (
            ordinary_smile
            and "expression" not in locks
            and (
                not re.search(
                    r"笑|嘴角.*(?:扬|翘)|露[齿牙]|露出(?:一点|少量)?牙",
                    values["expression"],
                )
                or suppresses_smile(values["expression"])
            )
        ):
            values["expression"] = expressions[0]
        if not people:
            values["expression"] = ""
        elif _wide_framing(framing) and "action" not in locks:
            capture = locks.get("capture") or proposed.capture
            if not re.search(r"对镜|镜前|镜面", capture):
                values["action"] = _without_handheld(values["action"]) or "自然站立拍摄"
        # Common negative instructions apply across fields: a gaze hidden inside
        # an action must not reverse an explicit expression instruction.
        for negative, opposite, owner in _NEGATIVES:
            if re.search(negative, user_text):
                clause = locks.get(owner, user_text)
                for field, value in values.items():
                    if re.search(opposite, value) and not re.search(negative, value):
                        values[field] = locks.get(field) or clause
        values["capture"] = (
            _capture(framing, slot, [spec.capture for spec in history], values["capture"])
            if people and not fixed_camera
            else values["capture"]
        )
        if (
            people
            and not fixed_camera
            and "capture" not in locks
            and is_handheld_capture(values["capture"])
            and re.search(r"侧后|背后|正后", values["angle"])
        ):
            values["angle"] = varied_choice(_ANGLES, [spec.angle for spec in history])
        if (
            previous is not None
            and not fixed_camera
            and re.search(r"换.*(?:角度|机位)|另.*角度", user_text)
            and "angle" not in locks
        ):
            angle_key = group_field_key if people else shot_field_key
            previous_angle = angle_key("angle", previous.angle)
            current_angle = angle_key("angle", values["angle"])
            if people:
                previous_angle = previous_angle or "level"
                current_angle = current_angle or previous_angle
            if current_angle == previous_angle:
                if people:
                    distinct_angles = tuple(
                        angle for angle in angle_pool if angle_key("angle", angle) != previous_angle
                    )
                else:
                    distinct_angles = _ANGLES[1:] if previous_angle == "微侧" else angle_pool
                values["angle"] = _new_value("angle", distinct_angles, history, key=angle_key)
        spec = ShotSpec(**values)
        used_compositions = {composition(item) for item in history}
        if composition(spec) in used_compositions and not group_composition_fixed:
            for field, options in (("angle", angle_pool), ("framing", framing_pool)):
                if field in locks:
                    continue
                values[field] = _new_value(field, options, history, key=compare_key)
                if field == "framing" and portrait_group:
                    values[field] = _concrete_group_framing(values[field], values["action"])
                if field == "framing" and people:
                    values["capture"] = _capture(
                        values["framing"],
                        slot,
                        [item.capture for item in history],
                        locks.get("capture") or proposed.capture,
                    )
                    if (
                        _wide_framing(values["framing"])
                        and "action" not in locks
                        and not re.search(r"对镜|镜前|镜面", values["capture"])
                    ):
                        values["action"] = _without_handheld(values["action"]) or "自然站立拍摄"
                spec = ShotSpec(**values)
                if composition(spec) not in used_compositions:
                    break
        uses_seed = seed is not None and any(
            field not in locks and not getattr(proposed, field) for field in ("angle", "action")
        )
        pose_id = seed["id"] if uses_seed else ""
        if seed is not None:
            used_ids.append(seed["id"])
        history.append(spec)
        result.append((spec, pose_id))
    return result


def make_plan(
    state: dict,
    user_prompt: str,
    photo_type: str,
    count: int,
    aspect_ratio: str,
    reference_roles: list[str],
    recent: list[str],
    life_status: str = "ready",
    *,
    shot_plan: tuple[ShotSpec, ...] = (),
    user_text: str | None = None,
    previous_shot: ShotSpec | None = None,
    current_mood: str | None = None,
    current_mood_intensity: str | None = None,
    source_continuity: bool = False,
    use_current_scene: bool | None = None,
    size_setting: SizeSetting | None = None,
) -> list[Shot]:
    """Build a set from already-validated input and one frozen life-state snapshot."""
    people = photo_type in {"selfie", "outfit"}
    actual_text = (user_prompt if user_text is None else user_text).strip()
    size_setting = size_setting or SizeSetting()
    ratio = (
        aspect_ratio
        if user_text is None and aspect_ratio
        else _requested_ratio(actual_text, aspect_ratio, size_setting.ratio)
    )
    size = size_setting.size_for(ratio)
    if use_current_scene is None:
        use_current_scene = bool(_LIFE_ANCHOR.search(actual_text))
    fixed, slot = _fixed_context(state, photo_type, life_status, source_continuity, use_current_scene)
    mood = str(slot.get("mood") or "") if current_mood is None else current_mood
    intensity = slot.get("mood_intensity") if current_mood_intensity is None else current_mood_intensity
    keep_expression = people and source_continuity and keeps_expression(actual_text)
    no_smile = people and suppresses_smile(actual_text)
    no_teeth = people and suppresses_teeth(actual_text)
    strong_smile = people and bool(strong_smile_request(actual_text))
    natural_food = photo_type == "food" and not any(
        _FOOD_STYLE_REQUEST.search(clause) and not re.search(r"不要|别|不用|避免", clause)
        for clause in re.split(r"[，,。；;]", actual_text)
    )

    def model_text(text: str) -> str:
        if keep_expression:
            return without_expression_cues(text)
        if people and (no_smile or no_teeth or not strong_smile):
            return natural_smile_text(text, no_smile=no_smile, no_teeth=no_teeth)
        if natural_food:
            return _ordinary_food_text(text)
        return text

    summary = model_text(user_prompt.strip())
    identity_indices = (
        [index for index, role in enumerate(reference_roles, 1) if role.startswith(("角色身份", "用户身份"))]
        if people
        else []
    )
    identities = (
        {role.split("：", 1)[0] for role in reference_roles if role.startswith(("角色身份", "用户身份"))}
        if people
        else set()
    )
    if people:
        prefix = (
            "请按用户原话与本张方案创作角色图片。延续身份参考图实际可见的面部立体关系、"
            "皮肤材质、发丝与自然受光层次，保持角色外貌和原有画风。"
            "半身或全身取景时，人物脸在画面里变小，仍应呈现可见的五官相对比例与材质细节。"
            "没有身份参考图时按用户文字与角色设定表现；用户明确指定的风格优先。"
            "不要拼图，不要水印，避免明显失真或不合理细节。"
        )
        if identity_indices:
            prefix += (
                "\n仅标记为角色身份或用户身份的参考图提供各自人脸身份。"
                "同一角色的多张身份图仍指同一个人，不增加人物。严格逐人绑定，"
                "外貌以各自身份图和明确文字为准，不根据姓名、关系或照片猜测性别、年龄、脸型，不跨人物复制外貌或穿搭。"
                "性格与自拍习惯跟随当前人设和聊天；性别明确时也只作弱参考，不按性别决定冷脸、微笑或动作。"
            )
    elif photo_type == "food":
        prefix = (
            "按用户要求拍食物，不默认加入人物。未明确指定拍法或风格时，采用就餐者在餐桌前用手机后置镜头随手拍的感觉："
            "普通吃饭距离、自然轻俯角，保留适量已有桌面和原有容器背景，自然景深与环境光。"
            f"{FOOD_GUIDANCE}"
            "默认不做广告精修或重新美化摆盘；保持用户指定或参考图已有的食物、容器和摆放。"
            "不为营造随手感添加餐具、菜品、脏乱污渍、模糊或手。"
            "以上仅为默认，用户明确要求的近景、特写、广告风或其他风格优先，不强行拉远或改容器。"
            "默认不继承人物参考图画风，用户明确指定风格优先；不拼图、不加水印。"
        )
    else:
        prefix = "按用户要求拍摄风景或环境，不默认加入人物。未指定风格时保持自然照片光影，不拼图、不加水印。"
    shared = [prefix, fixed]
    if reference_roles:
        shared.append(reference_roles_note(reference_roles))
        if people:
            shared.append(
                "身份图提供各自身份、外貌和人物质感；穿搭按本次日程、成片场景或用户要求。"
                "衣服参考只用于角色，不给用户复制角色的穿搭；用户穿搭按用户本次要求。"
                "场景、衣服或原图编辑参考均不得被当成人脸身份图。"
            )
    shared.append("用户原话（最高优先级，只锁定明确指定的维度）：" + (actual_text or "记录这一刻"))
    if people:
        if count > 1:
            shared.append(
                "组照变化要能在画面上看见。胸口以上仍是近景，半身需清楚看到腰部以上。"
                "轻微偏头或换一句平视的说法不算换机位。按本张方案做明确机位、取景、可见手势或视线变化。"
                "用户明确固定的维度保持；笑幅按本张自然神态。"
            )
        if keep_expression:
            shared.append("表情按来源成片实际可见的神态保持，当前聊天和日程心情不覆盖。")
        elif mood:
            shared.append(f"本次心情：{mood}。用户指定的表情优先，其次采用本张聊天规划的表情，心情只补未指定的表情。")
        if not keep_expression:
            expression_strength = {
                "中性": "神态自然放松，保留细微表情",
                "有波动": "通过眼神或轻微的面部变化流露心情",
                "强烈": "神态可以更鲜明，用眼神与面部细节体现情绪幅度，笑容依旧自然",
            }.get(intensity)
            if expression_strength:
                shared.append(
                    f"本次情绪强度：{intensity}。{expression_strength}。"
                    "低落或疲惫时保留对应状态，不强行微笑；用户指定的表情优先。"
                )
            if wants_visible_reaction(actual_text, summary):
                shared.append(REACTION_GUIDANCE)
            shared.append(SMILE_GUIDANCE)
        shared.append(CAPTURE_POLICY)
    shots = []
    specs = _specs(
        slot,
        mood,
        summary,
        actual_text,
        photo_type,
        count,
        shot_plan,
        recent,
        previous_shot,
        transform=model_text,
        keep_expression=keep_expression,
    )
    for number, (spec, pose_id) in enumerate(specs, 1):
        sections = list(shared)
        shot_summary = (
            _without_handheld(summary)
            if people and _wide_framing(spec.framing) and not re.search(r"对镜|镜前|镜面", spec.capture)
            else summary
        )
        if user_text is not None and shot_summary and shot_summary != actual_text:
            sections.append(
                "聊天理解摘要（用于当前聊天、人设语气和情绪的补充；其中扩写不是用户新增的硬要求）：" + shot_summary
            )
        labels = ("拍法", "取景与距离", "角度", "小拍照动作", "可见表情")
        sections.append(
            "本张具体拍摄方案：\n"
            + "\n".join(
                label + "：" + getattr(spec, field)
                for label, field in zip(labels, _SPEC_FIELDS)
                if getattr(spec, field)
            )
        )
        sections.append(
            f"本组第{number}张，共{count}张。"
            + ("人物身份、原画风、衣服和环境连续；" if people else "主体、既有容器与环境连续，画面风格按用户要求；")
            + ("未被用户固定的角度、距离、动作与视线" if people else "未被用户固定的取景距离与角度")
            + "按本张方案形成可见差异，不能只是首张的轻微抖动或重复构图。"
        )
        prompt = "\n\n".join(sections)
        if identity_indices:
            prompt = apply_face_lock(prompt, identity_indices, multi_person=len(identities) > 1)
        shots.append(Shot(prompt, size, pose_id, spec))
    return shots
