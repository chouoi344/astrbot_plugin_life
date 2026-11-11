"""Identity-reference face preservation, adapted from the user's photo_face_lock.py."""

from __future__ import annotations

_START_PREFIX = "严格保持"
_END_PREFIX = "再次强调："


def face_lock_lines(ref_indices=(1,), *, multi_person: bool = False) -> tuple[str, str]:
    if multi_person:
        label = "参考图" + "、".join(str(index) for index in ref_indices)
        return (
            f"严格保持每位人物的身份识别特征与其对应身份参考图一致（{label}），逐人绑定，不互换。画风沿用原图，用户明确要求换风格时遵从用户；表情、目光和嘴角按本张方案变化。",
            "再次强调：每位人物的脸必须与其对应身份参考图属于同一人，允许自然表情变化，不复制参考图神态，不跨人物复制外貌或穿搭。",
        )
    primary, *supporting = ref_indices
    detail_note = ""
    if supporting:
        label = "参考图" + "、".join(str(index) for index in supporting)
        detail_note = f"{label}只补主参考图看不到的新角度或遮挡细节，不混合改造主参考图的脸。"
    return (
        f"严格保持参考图{primary}中同一人物的身份识别特征，以该图为人物身份主锚。"
        "保持脸型、眼鼻嘴相对比例和下颌轮廓，延续主参考图的画风和质感。"
        "用户明确要求换风格时遵从用户。" + detail_note + "表情、目光和嘴角按本张方案自然变化。",
        f"再次强调：人物的脸必须与参考图{primary}属于同一人，以主参考图的脸型、"
        "五官相对比例和下颌轮廓为准，允许自然表情变化，不复制参考图神态。",
    )


def split_face_lock(prompt: str) -> tuple[str | None, str, str | None]:
    """Separate existing preservation lines so appending notes stays idempotent."""
    lines = prompt.split("\n")
    start = end = None
    if lines and lines[0].startswith(_START_PREFIX) and "人物" in lines[0]:
        start = lines.pop(0)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith(_END_PREFIX) and "的脸必须与" in lines[index]:
            end = lines.pop(index)
            break
    return start, "\n".join(lines).strip("\n"), end


def apply_face_lock(prompt: str, ref_indices=(1,), *, multi_person: bool = False) -> str:
    """Wrap an identity-reference prompt; an empty reference set never locks faces."""
    if not ref_indices:
        return prompt
    _, body, _ = split_face_lock(prompt)
    start, end = face_lock_lines(ref_indices, multi_person=multi_person)
    return f"{start}\n{body}\n{end}"


def relock(prompt: str) -> str:
    """Restore existing preservation lines after an extra reference note is appended."""
    start, body, end = split_face_lock(prompt)
    if start is None or end is None:
        return prompt
    return f"{start}\n{body}\n{end}"
