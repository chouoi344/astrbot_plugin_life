"""Timezone-aware business days, timeline repair and future-only edits."""

import copy
import datetime as dt
import re
from zoneinfo import ZoneInfo

from astrbot.api import logger

_NAP = re.compile(
    r"^(?:(?:吃完午饭|吃完午餐|午饭后|午餐后|午间|午后|中午)[，、 ]*)?"
    r"(?:(?:午睡|午觉|小睡|小憩|打盹|补觉|眯一会儿|眯会儿)(?!前|后|时间)|午休(?:一下|一会儿)?$)"
)
_NIGHT_SLEEP = re.compile(
    r"^(?:(?:夜间|夜里|晚上|深夜|凌晨|上床|关灯|熄灯)?[，、 ]*"
    r"(?:睡觉|睡眠|入睡|就寝|熟睡|安睡)(?!前|后|准备|时间|质量|不足)|继续睡|睡到)"
)


def project_slot_rest(slot):
    """Fill the rest status of legacy slots without rewriting saved records."""
    kind = slot.get("rest_type")
    if kind not in ("night_sleep", "nap"):
        activity = str(slot.get("activity", "") or "").strip()
        kind = "nap" if _NAP.match(activity) else "night_sleep" if _NIGHT_SLEEP.match(activity) else "none"
    result = {**slot, "rest_type": kind}
    if kind != "none":
        result["phone"] = "offline"
    return result


def parse_local(value, tz):
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def business_window(now, schedule_time="07:00"):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must include timezone")
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(schedule_time))
    if not match:
        raise ValueError("日程时间请使用 HH:MM（00:00—23:59）")
    hour, minute = map(int, match.groups())
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < start:
        start -= dt.timedelta(days=1)
    return start, start + dt.timedelta(days=1)


def _sleep_slot(start, end):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "activity": "睡觉",
        "location": "住处",
        "phone": "offline",
        "rest_type": "night_sleep",
        "mood": "平静",
        "outfit_type": "sleepwear",
    }


def _normalize_slot(slot, tz, *, new_slot=False):
    if not isinstance(slot, dict):
        raise ValueError("时间段必须是对象")
    start = parse_local(slot.get("start", ""), tz)
    end = parse_local(slot.get("end", ""), tz)
    if end <= start:
        raise ValueError("时间段结束时间必须晚于开始时间")
    result = copy.deepcopy(slot)
    for key in ("activity", "location", "mood"):
        result[key] = str(result.get(key, "") or "").strip()
    if not result["activity"]:
        raise ValueError("时间段缺少活动")
    if "mood_intensity" in result and result["mood_intensity"] not in ("中性", "有波动", "强烈"):
        raise ValueError("mood_intensity只可填中性、有波动、强烈")
    # v1.3.x slots carried a proactive cooldown; proactive timing no longer uses it.
    result.pop("proactive_cooldown_minutes", None)
    text = result["activity"] + result["location"]
    if result.get("phone") not in {"free", "glance", "offline"}:
        result["phone"] = (
            "offline"
            if re.search(r"睡|考试|游泳|驾车|开车", text)
            else "free"
            if re.search(r"休息|玩手机|刷手机|闲聊", text)
            else "glance"
        )
    if result.get("outfit_type") not in {"sleepwear", "home", "outdoor"}:
        result["outfit_type"] = (
            "sleepwear" if "睡" in text else "home" if re.search(r"家|宿舍|住处", text) else "outdoor"
        )
    result["start"], result["end"] = start.isoformat(), end.isoformat()
    result = project_slot_rest(result)
    if not new_slot and "rest_type" not in slot and result["rest_type"] == "none":
        del result["rest_type"]
    return result


def repair_slots(slots, start, end, forbidden=(), min_slots=6, user_aliases=()):
    from .domain import contains_forbidden

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("时间轴边界必须为带时区的递增时间")
    if not isinstance(slots, list):
        raise ValueError("slots 必须是数组")
    normalized = []
    replaced = 0
    for candidate in slots:
        item = _normalize_slot(candidate, start.tzinfo, new_slot=True)
        a, b = parse_local(item["start"], start.tzinfo), parse_local(item["end"], start.tzinfo)
        if b <= start or a >= end:
            continue
        if contains_forbidden(item, forbidden, user_aliases):
            # Keep its exact interval and the rest of the day. Optional model
            # metadata is intentionally dropped together with the invalid act.
            kind = item["rest_type"]
            if kind != "none":
                item = _sleep_slot(a, b)
                if kind == "nap":
                    item.update(activity="午睡", rest_type="nap")
            else:
                item = {
                    "start": a.isoformat(),
                    "end": b.isoformat(),
                    "activity": "处理自己的日常事务",
                    "location": "住处",
                    "phone": "glance",
                    "rest_type": "none",
                    "mood": "平静",
                    "outfit_type": "home",
                }
            replaced += 1
        item["start"], item["end"] = max(start, a).isoformat(), min(end, b).isoformat()
        normalized.append(item)
    normalized.sort(key=lambda item: (parse_local(item["start"], start.tzinfo), parse_local(item["end"], start.tzinfo)))
    if len(normalized) < min_slots:
        raise ValueError("有效时间段少于 %d 段" % min_slots)
    if not normalized:
        return [_sleep_slot(start, end)]
    repaired = []
    for item in normalized:
        a, b = parse_local(item["start"], start.tzinfo), parse_local(item["end"], start.tzinfo)
        if not repaired:
            if a > start:
                repaired.append(_sleep_slot(start, a))
        else:
            previous_end = parse_local(repaired[-1]["end"], start.tzinfo)
            if a < previous_end:
                # Later activity takes effect at its declared start.
                while repaired and parse_local(repaired[-1]["start"], start.tzinfo) >= a:
                    repaired.pop()
                if repaired:
                    repaired[-1]["end"] = a.isoformat()
            elif a > previous_end:
                gap = a - previous_end
                if gap >= dt.timedelta(minutes=30):
                    raise ValueError("时间轴有 %d 分钟空档，需重写" % int(gap.total_seconds() / 60))
                repaired[-1]["end"] = a.isoformat()
        repaired.append(item)
    last_end = parse_local(repaired[-1]["end"], start.tzinfo)
    if last_end < end:
        repaired.append(_sleep_slot(last_end, end))
    if len(repaired) < min_slots:
        raise ValueError("修复重叠后有效时间段少于 %d 段" % min_slots)
    if replaced:
        logger.info("[生活日程] 已替换 %s 段角色与主人的受限互动，其余日程保留", replaced)
    return repaired


def merge_future(actual, changes, now, end, forbidden=(), user_aliases=()):
    from .domain import contains_forbidden

    if now.tzinfo is None or end.tzinfo is None:
        raise ValueError("修改时间必须带时区")
    if not isinstance(changes, list):
        raise ValueError("修改时间段必须是数组")
    result = copy.deepcopy(actual)
    for raw in changes:
        if not isinstance(raw, dict):
            raise ValueError("修改时间段必须是对象")
        if contains_forbidden(raw, forbidden, user_aliases):
            continue
        change = _normalize_slot(raw, now.tzinfo)
        a = max(now, parse_local(change["start"], now.tzinfo))
        b = min(end, parse_local(change["end"], now.tzinfo))
        if b <= a:
            continue
        change["start"], change["end"] = a.isoformat(), b.isoformat()
        updated = []
        for original in result:
            left, right = parse_local(original["start"], now.tzinfo), parse_local(original["end"], now.tzinfo)
            if right <= a or left >= b:
                updated.append(original)
                continue
            if left < a:
                front = copy.deepcopy(original)
                front["end"] = a.isoformat()
                updated.append(front)
            if right > b:
                back = copy.deepcopy(original)
                back["start"] = b.isoformat()
                updated.append(back)
        updated.append(change)
        result = sorted(updated, key=lambda item: parse_local(item["start"], now.tzinfo))
    # Validate continuity without re-normalizing frozen records.
    for left, right in zip(result, result[1:]):
        if parse_local(left["end"], now.tzinfo) != parse_local(right["start"], now.tzinfo):
            raise ValueError("合并后的时间轴存在空档或重叠")
    if actual and result:
        if parse_local(actual[0]["start"], now.tzinfo) != parse_local(result[0]["start"], now.tzinfo) or parse_local(
            actual[-1]["end"], now.tzinfo
        ) != parse_local(result[-1]["end"], now.tzinfo):
            raise ValueError("修改超出了原有时间轴")
    return result


def find_slots(slots, now):
    if now.tzinfo is None:
        raise ValueError("当前时间必须带时区")
    current = following = None
    for item in slots:
        a, b = parse_local(item["start"], now.tzinfo), parse_local(item["end"], now.tzinfo)
        if a <= now < b:
            current = copy.deepcopy(item)
        elif a > now:
            following = copy.deepcopy(item)
            break
    return current, following


def format_timeline(slots, now=None, outfit_label=None):
    from .outfits import slot_outfit_label

    lines = []
    outfit_label = outfit_label or slot_outfit_label
    phones = {"free": "能秒回", "glance": "偶尔看手机", "offline": "看不了手机"}
    for slot in slots:
        item = project_slot_rest(slot)
        zone = now.tzinfo if now is not None else dt.timezone.utc
        a, b = parse_local(item["start"], zone), parse_local(item["end"], zone)
        if now is None:
            # Preserve recorded local clock times when no reference is supplied.
            a, b = dt.datetime.fromisoformat(item["start"]), dt.datetime.fromisoformat(item["end"])
        marker = "▶ " if now is not None and a <= now < b else "✓ " if now is not None and b <= now else "  "
        next_day = "次日" if b.date() > a.date() else ""
        lines.append(
            "%s%s—%s%s %s｜%s｜%s｜%s｜%s"
            % (
                marker,
                a.strftime("%H:%M"),
                next_day,
                b.strftime("%H:%M"),
                item.get("activity", ""),
                item.get("location", ""),
                phones.get(item.get("phone"), "偶尔看手机"),
                item.get("mood", ""),
                outfit_label(item),
            )
        )
    return "\n".join(lines)
