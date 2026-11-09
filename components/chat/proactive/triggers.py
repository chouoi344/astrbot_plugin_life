"""Find a concrete reason to start a conversation during an available slot."""

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from unicodedata import normalize

_NIGHT = re.compile(
    r"^(?:(?:夜间|夜里|晚上|深夜|凌晨|上床|关灯|熄灯)[，、 ]*)?"
    r"(?:睡觉|睡眠|入睡|就寝|熟睡|安睡)(?!前|后)|^继续睡|^睡到"
)
_NAP = re.compile(r"^(?:午睡|午休|午觉|小睡|小憩|打盹|补觉)")
_ROUTINE = re.compile(
    r"^(?:起床|醒来|晨起|早起|上下班|上下课|上班|下班|通勤|洗漱|"
    r"工作|办公|休息|自由活动|有空|看手机|待机|空闲|回家|"
    r"早餐|午餐|晚餐|吃饭|吃早餐|吃午饭|吃晚饭|准备睡觉|"
    r"在家待着|在家休息|没什么安排)(?:时间|中)?$"
)
_SENTENCES = re.compile(r"[。！？!?；;…\n]+")
_CLAUSES = re.compile(r"[,，:：]|但是|不过|可是|但")
_QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"')
_REPORTED = re.compile(r"(?:他|她|他们|她们|别人|对方|我)(?:刚才|刚|之前|曾经)?(?:说|问|喊|讲|发|回)(?:过|了)?")
_WAIT = re.compile(
    r"(?:(?:你|先)?(?:等我|等我忙完|等我找你)|"
    r"(?:我|等我)?(?:忙完|忙完了|好了|有空了|忙好了)(?:再|就)?(?:来)?找你|"
    r"等我(?:来)?找你)(?:[吧呀哈哦啊啦喔~～])?"
)
_CLOSE_NIGHT = re.compile(
    r"(?:(?:那|那就)?晚安|我(?:就|先)?(?:去)?睡(?:了|觉了|觉)?|"
    r"我要(?:去)?睡(?:了|觉了|觉)?)(?:[吧呀哈哦啊啦喔~～])?"
)
_REOPEN = re.compile(
    r"(?:(?:我)?(?:现在|还是|先|暂时|就|还)?不(?:去)?睡(?:了|觉)?|"
    r"(?:现在|还是|先|再)聊(?:会儿|一会儿)?|不用等我(?:了)?|我回来(?:了)?)"
    r"(?:[吧呀哈哦啊啦喔~～])?"
)


@dataclass(frozen=True)
class Trigger:
    key: str
    reason: str
    valid_until: datetime
    promise: str = ""
    fact: dict | None = None


def slot_time(value: str, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def rest_type(slot: dict) -> str:
    if "rest_type" in slot:
        return slot["rest_type"]
    activity = slot.get("activity", "")
    if _NIGHT.search(activity):
        return "night_sleep"
    if _NAP.search(activity):
        return "nap"
    return "none"


def contact_hold(text: str) -> str:
    mode = ""
    for sentence in _SENTENCES.split(_QUOTED.sub(" ", text)):
        for item in _CLAUSES.split(sentence):
            clause = item.strip()
            if _REPORTED.fullmatch(clause):
                break
            if _REOPEN.fullmatch(clause):
                mode = "open"
            elif _WAIT.fullmatch(clause):
                mode = "user"
            elif _CLOSE_NIGHT.fullmatch(clause):
                mode = "night"
    return mode


def _activity_fact(slot: dict, adjacent: dict | None) -> dict | None:
    activity = slot.get("activity", "").strip()
    location = slot.get("location", "").strip()
    if not activity or _ROUTINE.fullmatch(activity) or rest_type(slot) != "none":
        return None
    if adjacent and (activity, location) == (
        adjacent.get("activity", "").strip(), adjacent.get("location", "").strip(),
    ):
        return None
    return {"activity": activity, "location": location, "start": slot["start"], "end": slot["end"]}


def _activity_key(fact: dict, day) -> str:
    topic = f"{fact['activity']}\n{fact['location']}"
    signature = hashlib.sha256(topic.encode()).hexdigest()[:16]
    return f"activity:{day}:{signature}"


def choose_trigger(
    state: dict, considered: Collection[str], after: datetime | None = None,
    *, last_seen: str | None = None, silence_hours: float = 5 / 60,
) -> Trigger | None:
    """Prioritize dated promises, then recent facts the existing planner can decline."""
    current = state.get("current_slot")
    if not current or current["phone"] != "free" or rest_type(current) != "none":
        return None
    now = datetime.fromisoformat(state["now"])
    end = slot_time(current["end"], now)
    if now >= end:
        return None

    def fresh(anchor: datetime) -> bool:
        return anchor <= now and (after is None or after < anchor)

    for promise in state.get("promises_due", []):
        if promise["type"] != "tell" or promise["status"] != "pending" or not promise.get("due"):
            continue
        key = f"promise:{promise['id']}"
        due = slot_time(promise["due"], now)
        deadline = min(end, due + timedelta(minutes=30))
        if key not in considered and fresh(due) and now < deadline:
            return Trigger(key, "到了此前约好的联系或告知时间", deadline, promise["content"])

    seen = slot_time(last_seen, now) if last_seen else None
    cooldown = current.get("proactive_cooldown_minutes", 0)
    if seen and cooldown and now - seen < timedelta(minutes=cooldown):
        return None

    for change in reversed(state.get("changes", [])):
        if change["source"] != "surprise" or not change.get("id") or not change["description"]:
            continue
        key = f"change:{change['id']}"
        occurred = slot_time(change["time"], now)
        deadline = min(end, occurred + timedelta(minutes=30))
        if key not in considered and fresh(occurred) and now < deadline:
            return Trigger(key, "生活安排刚有已记录的变化", deadline, fact=change)

    start = slot_time(current["start"], now)
    if state.get("persona_changed"):
        return None
    if fresh(start):
        previous = state.get("previous_slot")
        if previous and slot_time(previous["end"], now) != start:
            previous = None
        candidates = [(current, previous, "现在正在发生的生活片段")]
        if previous and previous["phone"] in {"glance", "offline"}:
            candidates.append((previous, current, "上一段已结束的实际生活片段"))
        for slot, adjacent, reason in candidates:
            fact = _activity_fact(slot, adjacent)
            if fact is None:
                continue
            key = _activity_key(fact, start.date())
            if key not in considered:
                return Trigger(key, reason, end, fact=fact)

    day = state.get("day_key") or now.date().isoformat()
    mood = current.get("mood")
    intensity = current.get("mood_intensity")
    if mood and intensity in {"有波动", "强烈"}:
        label = " ".join(normalize("NFKC", mood).split()).casefold()
        signature = hashlib.sha256(label.encode()).hexdigest()[:16]
        mood_key = f"mood:{day}:{signature}"
        if label and mood_key not in considered:
            return Trigger(
                mood_key, "当前心情有值得自然流露的变化", end,
                fact={"mood": mood, "mood_intensity": intensity},
            )

    silence_key = f"silence:{day}"
    if (
        not silence_hours or not seen or silence_key in considered
        or now - seen <= timedelta(hours=silence_hours)
    ):
        return None
    reason = "隔了一阵子，有一件今天发生的新事可以聊"
    for change in reversed(state.get("surprise_changes", [])):
        if f"change:{change['id']}" not in considered and fresh(slot_time(change["time"], now)):
            return Trigger(silence_key, reason, end, fact=change)
    for slot in reversed(state.get("past_slots", [])):
        if not fresh(slot_time(slot["end"], now)) or not (fact := _activity_fact(slot, None)):
            continue
        if _activity_key(fact, slot_time(slot["start"], now).date()) not in considered:
            return Trigger(silence_key, reason, end, fact=fact)
    for promise in state.get("promises_due", []):
        if (
            promise["status"] == "pending" and promise.get("due")
            and f"promise:{promise['id']}" not in considered
            and fresh(slot_time(promise["due"], now))
        ):
            return Trigger(silence_key, reason, end, fact={"到期但待完成的约定": promise})
    return None
