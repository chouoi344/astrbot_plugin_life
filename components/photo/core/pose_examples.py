"""Manually curated camera and pose notes, independent of sample photos."""

import json
from dataclasses import asdict, dataclass
from typing import Literal

from .planner import ShotSpec


@dataclass(frozen=True)
class PoseExample:
    example_id: str
    shot: ShotSpec
    photo_types: tuple[Literal["selfie", "outfit"], ...] = ("selfie", "outfit")
    # Match any listed term in the location, activity or current user message.
    # An empty tuple makes the pose available in general.
    when_any: tuple[str, ...] = ()


# Add reviewed examples here with stable IDs. Keep only reusable camera, pose
# and expression details. Fold gaze into action or expression.
POSE_EXAMPLES: tuple[PoseExample, ...] = ()


def pose_examples_prompt(state: dict, user_text: str) -> str:
    """Offer up to three relevant examples in the existing chat planning turn."""
    if not POSE_EXAMPLES:
        return ""

    slot = state.get("current_slot", {})
    context = f"{slot.get('location', '')} {slot.get('activity', '')} {user_text}".casefold()
    matching = [item for item in POSE_EXAMPLES if any(term.casefold() in context for term in item.when_any)]
    general = [item for item in POSE_EXAMPLES if not item.when_any]
    candidates = (matching + general)[:3]
    if not candidates:
        return ""

    return (
        "\n\n可选拍法资料（仅供本次自拍或穿搭照规划）："
        "只借用与当前场景、拍摄类型和角色相符的拍法；明确用户要求、本次场景和表情优先。"
        "可选择或调整其中的shot五字段，写入对应照片的shot_plan；不传资料编号。"
        "同组可选择不同拍法，未找到合适资料时按原方式规划。\n"
        + json.dumps([asdict(item) for item in candidates], ensure_ascii=False)
    )
