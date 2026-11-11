"""Reasons and topics for proactive messages.

Two kinds exist since v1.4.0 (items 8, 9 and the item 10 extension):

* fixed-time reasons that keep their own clock: a due "tell" promise, a
  follow-up on something the user mentioned, an expired "wait for me" hold,
  and (v1.4.2) a natural point in the character's own day where missing the
  user would come up;
* topics for the randomly timed message, chosen by the model: the current
  life, something new today, the user's own events, or missing the user when
  something reminds the character of an earlier chat.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
_REOPEN = re.compile(
    r"(?:(?:现在|还是|先|再)聊(?:会儿|一会儿)?|不用等我(?:了)?|我回来(?:了)?|我忙完了)"
    r"(?:[吧呀哈哦啊啦喔~～])?"
)
_MEAL = re.compile(
    r"早餐|午餐|晚餐|早饭|午饭|晚饭|早点心|早茶|下午茶|夜宵|宵夜|用餐|就餐|聚餐|约饭|饭局|"
    r"食堂|外卖|便当|火锅|烧烤|烤肉|串串|麻辣烫|自助餐|"
    r"吃(?:个|顿|点|了|过)?(?:饭|东西|早饭|午饭|晚饭|宵夜|夜宵|面|粉|饺子|小吃)"
)
# Work or chores around food are not a meal for the character.
_NOT_MEAL = re.compile(r"送外卖|外卖(?:骑手|员|小哥)|打工|兼职|上班|值班|备菜|做饭|煮饭|洗碗|收拾餐")
PROMISE_WINDOW = timedelta(minutes=30)
CARE_EXPIRY = timedelta(hours=12)
# A schedule point (something ended, a meal, bedtime) stays a chance this long.
MISS_YOU_WINDOW = timedelta(minutes=30)
# C2: an expired "wait for me" is tried at most this many rounds (generation
# failure, bad output and skip each use one); it counts as done only once a
# message really went out (Ledger.accept / unknown_send).
HOLD_ASK_ATTEMPTS = 3
# G-2: retries wait the shortest proactive interval, kept within 30–120 minutes,
# and the reminder is dropped 6 hours after it first fell due (whichever first).
HOLD_RETRY_MIN = timedelta(minutes=30)
HOLD_RETRY_MAX = timedelta(minutes=120)
HOLD_GIVE_UP_AFTER = timedelta(hours=6)


@dataclass(frozen=True)
class Trigger:
    key: str
    reason: str
    kind: str  # promise / care / hold / timed
    valid_until: datetime | None = None
    promise: str = ""
    fact: dict | None = None
    counted: bool = True
    topics: tuple = field(default=())


def slot_time(value: str, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def rest_type(slot: Mapping) -> str:
    if "rest_type" in slot:
        return slot["rest_type"]
    activity = slot.get("activity", "")
    if _NIGHT.search(activity):
        return "night_sleep"
    if _NAP.search(activity):
        return "nap"
    return "none"


def is_free(slot: Mapping | None) -> bool:
    """The character can look at the phone right now (not busy, not asleep)."""
    return bool(slot) and slot.get("phone") == "free" and rest_type(slot) == "none"


def contact_hold(text: str) -> str:
    """``"user"`` when the user asked to wait for them, ``""`` otherwise.

    Saying good night no longer pauses proactive messages (item 3).
    """
    mode = ""
    for sentence in _SENTENCES.split(_QUOTED.sub(" ", text)):
        for item in _CLAUSES.split(sentence):
            clause = item.strip()
            if _REPORTED.fullmatch(clause):
                break
            if _REOPEN.fullmatch(clause):
                mode = ""
            elif _WAIT.fullmatch(clause):
                mode = "user"
    return mode


def _activity_fact(slot: Mapping, adjacent: Mapping | None) -> dict | None:
    activity = slot.get("activity", "").strip()
    location = slot.get("location", "").strip()
    if not activity or _ROUTINE.fullmatch(activity) or rest_type(slot) != "none":
        return None
    if adjacent and (activity, location) == (
        adjacent.get("activity", "").strip(),
        adjacent.get("location", "").strip(),
    ):
        return None
    return {"activity": activity, "location": location, "start": slot["start"], "end": slot["end"]}


def _activity_key(fact: Mapping, day) -> str:
    topic = f"{fact['activity']}\n{fact['location']}"
    return f"activity:{day}:{hashlib.sha256(topic.encode()).hexdigest()[:16]}"


def due_promise(state: Mapping, considered: Collection[str], now: datetime) -> Trigger | None:
    """A "tell" promise whose time has come. The user asked for it, so it is
    not counted and ignores quiet hours (item 10 extension supplement)."""
    for promise in state.get("promises_due", []):
        if promise.get("type") != "tell" or promise.get("status") != "pending" or not promise.get("due"):
            continue
        key = f"promise:{promise['id']}"
        due = slot_time(promise["due"], now)
        if key not in considered and due <= now < due + PROMISE_WINDOW:
            return Trigger(
                key,
                "到了此前约好的联系或告知时间",
                "promise",
                due + PROMISE_WINDOW,
                promise=promise["content"],
                counted=False,
                topics=(key,),
            )
    return None


def due_care(events: list[Mapping], considered: Collection[str], now: datetime) -> Trigger | None:
    """Follow up on something the user mentioned once it should be over."""
    for item in sorted(events, key=lambda row: row.get("follow_up_at", "")):
        if item.get("status") != "pending" or not item.get("follow_up_at"):
            continue
        key = f"care:{item['id']}"
        follow_up = slot_time(item["follow_up_at"], now)
        if key in considered or not follow_up <= now < follow_up + CARE_EXPIRY:
            continue
        fact = {"对方提到的事": item.get("content", ""), "发生时间": item.get("time", "")}
        return Trigger(
            key,
            "对方之前提到的那件事，这会儿应该有结果了。像一直记着这件事的人那样问一句，用你们平时说话的方式，别像提醒事项或回访",
            "care",
            follow_up + CARE_EXPIRY,
            fact=fact,
            topics=(key,),
        )
    return None


def _safe_time(value, now: datetime) -> datetime | None:
    """A stored time, or None when it is missing or broken (old or hand-edited ledgers)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return slot_time(value, now)
    except ValueError:
        return None


def hold_attempts(data: Mapping) -> int:
    value = data.get("hold_attempts", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def hold_retry_delay(min_interval_minutes) -> timedelta:
    """G-2: the shortest proactive interval, kept within 30–120 minutes."""
    return min(max(timedelta(minutes=min_interval_minutes), HOLD_RETRY_MIN), HOLD_RETRY_MAX)


def hold_status(data: Mapping, hold_hours: float, now: datetime) -> str:
    """State of the "wait for me" reminder.

    "" no hold; "waiting" before it expires; "due" expired, not tried yet;
    "retrying" tried, waiting for the retry time; "ready" tried, may try again;
    "given_up" out of attempts or past HOLD_GIVE_UP_AFTER; "done" sent.
    """
    if data.get("hold") != "user":
        return ""
    started = _safe_time(data.get("hold_started"), now)
    if started is None:
        return ""
    first_due = started + timedelta(hours=hold_hours)
    if now < first_due:
        return "waiting"
    if data.get("hold_notified") is True:
        return "done"
    attempts = hold_attempts(data)
    if attempts >= HOLD_ASK_ATTEMPTS or now >= first_due + HOLD_GIVE_UP_AFTER:
        return "given_up"
    retry = _safe_time(data.get("hold_retry_at"), now)
    if retry is not None and now < retry:
        return "retrying"
    return "ready" if attempts else "due"


def hold_pending(data: Mapping, hold_hours: float, now: datetime) -> bool:
    """G-1: the reminder fell due and is neither sent nor dropped; other messages wait."""
    return hold_status(data, hold_hours, now) in ("due", "retrying", "ready")


def expired_hold(data: Mapping, hold_hours: float, now: datetime) -> Trigger | None:
    """The user said "wait for me" and has not come back for ``hold_hours``."""
    if hold_status(data, hold_hours, now) not in ("due", "ready"):
        return None
    started = slot_time(data["hold_started"], now)
    hours = max(1, round((now - started).total_seconds() / 3600))
    return Trigger(
        f"hold:{data['hold_started']}",
        f"对方 {hours} 小时前说忙完会来找你，到现在还没来。你大概一直惦记着这件事，除非此刻真的腾不出手，就问一句。",
        "hold",
        None,
        fact={"对方原话": data.get("last_user_text", "")},
    )


def miss_you_moment(state: Mapping, considered: Collection[str], now: datetime) -> Trigger | None:
    """A natural point in the character's own day to maybe say they miss the user.

    Plan A of v1.4.2 ("有契机才想"): something just ended (including leaving
    work), a meal starts, or bedtime is near. The caller checks the daily
    limit, recent chat, quiet hours and the other limits.
    """
    current = state.get("current_slot") or {}
    if not is_free(current) or not current.get("start"):
        return None
    moment = None
    upcoming = state.get("next_slot") or {}
    if upcoming.get("start") and rest_type(upcoming) == "night_sleep":
        bedtime = slot_time(upcoming["start"], now)
        if now <= bedtime <= now + MISS_YOU_WINDOW:
            moment = (
                "sleep",
                upcoming["start"],
                bedtime,
                {"契机": "快要睡觉了", "接下来": upcoming.get("activity", "")},
            )
    started = slot_time(current["start"], now)
    activity = current.get("activity", "").strip()
    if moment is None and started <= now < started + MISS_YOU_WINDOW:
        previous = state.get("previous_slot") or {}
        if _MEAL.search(activity) and not _NOT_MEAL.search(activity):
            moment = ("meal", current["start"], started + MISS_YOU_WINDOW, {"契机": "到饭点了", "正在": activity})
        elif previous.get("activity") and rest_type(previous) == "none":
            moment = (
                "done",
                current["start"],
                started + MISS_YOU_WINDOW,
                {"契机": "刚忙完一件事", "刚结束": previous["activity"].strip(), "现在": activity},
            )
    if moment is None:
        return None
    kind, at, until, fact = moment
    key = f"miss_you:{now.date().isoformat()}:{kind}:{at}"
    if key in considered:
        return None
    return Trigger(
        key,
        "日程走到了一个节点（见本轮具体事实），你可能会突然想起对方。按人设判断此刻会不会想：会的话可以说，用你自己的话把这个节点里的真实细节带进去（手边在做什么、刚结束了什么、准备睡了在干嘛），不要套固定说法；不会就 skip",
        "miss_you",
        until,
        fact=fact,
        topics=(key,),
    )


def hold_active(data: Mapping, hold_hours: float, now: datetime) -> bool:
    return hold_status(data, hold_hours, now) == "waiting"


def topic_candidates(
    state: Mapping,
    considered: Collection[str],
    events: list[Mapping],
    now: datetime,
    *,
    miss_you_allowed: bool,
) -> list[dict]:
    """Content sources for a randomly timed message; the model picks one or skips."""
    topics: list[dict] = []
    current = state.get("current_slot") or {}
    fact = _activity_fact(current, None) if current else None
    if fact:
        topics.append({"id": "life", "类型": "当下的生活", "内容": fact})
    else:
        topics.append({"id": "life", "类型": "当下的生活", "内容": "按生活事实里的当前状态"})
    day = state.get("day_key") or now.date().isoformat()
    for change in reversed(state.get("surprise_changes", [])):
        key = f"change:{change['id']}"
        if key not in considered and change.get("description"):
            topics.append({"id": key, "类型": "今天的新鲜事", "内容": change["description"]})
            break
    for slot in reversed(state.get("past_slots", [])):
        if slot_time(slot["end"], now) > now or not (past := _activity_fact(slot, None)):
            continue
        key = _activity_key(past, slot_time(slot["start"], now).date())
        if key not in considered:
            topics.append({"id": key, "类型": "今天刚做完的事", "内容": past})
            break
    for item in events:
        if item.get("status") != "pending":
            continue
        key = f"care:{item['id']}"
        # Only events whose result should be known. Asking about one ahead of
        # time would mark it followed and cancel the follow-up afterwards.
        ready_at = item.get("follow_up_at") or item.get("time")
        if key in considered or not ready_at or slot_time(ready_at, now) > now:
            continue
        topics.append(
            {
                "id": key,
                "类型": "关心对方的事",
                "内容": {"对方提到的事": item.get("content", ""), "发生时间": item.get("time", "")},
            }
        )
        if sum(topic["类型"] == "关心对方的事" for topic in topics) >= 2:
            break
    if miss_you_allowed:
        topics.append(
            {
                "id": f"miss_you:{day}",
                "类型": "想对方了",
                "内容": "只有眼前的某件事、某样东西让你想起和对方聊过的话时才用；消息里要带出这个具体的来处，用你自己的话说，不套固定句式",
            }
        )
    return topics
