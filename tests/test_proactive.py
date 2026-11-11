import json
from datetime import datetime, timedelta, timezone

import pytest
from astrbot_plugin_life.components.chat.proactive import timing
from astrbot_plugin_life.components.chat.proactive.coordinator import (
    Coordinator,
    parse_plan,
    wait_reply_facts,
)
from astrbot_plugin_life.components.chat.proactive.ledger import Ledger
from astrbot_plugin_life.components.chat.proactive.settings import Settings
from astrbot_plugin_life.components.chat.proactive.triggers import (
    contact_hold,
    due_care,
    due_promise,
    expired_hold,
    hold_active,
    topic_candidates,
)

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
FREE = {
    "phone": "free",
    "activity": "在咖啡店看书",
    "location": "街角咖啡店",
    "start": "2026-11-11T14:00:00+08:00",
    "end": "2026-11-11T16:00:00+08:00",
}
BUSY = {**FREE, "phone": "glance", "activity": "开会"}


# -- triggers ----------------------------------------------------------------
def test_contact_hold():
    assert contact_hold("等我忙完找你") == "user"
    assert contact_hold("等我忙完找你。我忙完了") == ""
    assert contact_hold("晚安") == ""
    assert contact_hold("他说“等我忙完找你”") == ""


def promise(due, **extra):
    return {
        "id": "p1",
        "type": "tell",
        "status": "pending",
        "due": due.isoformat(),
        "content": "告诉对方面试结果",
        **extra,
    }


def test_due_promise_window_and_flags():
    trigger = due_promise({"promises_due": [promise(NOW - timedelta(minutes=5))]}, set(), NOW)
    assert trigger.kind == "promise" and not trigger.counted
    assert due_promise({"promises_due": [promise(NOW - timedelta(minutes=31))]}, set(), NOW) is None
    assert due_promise({"promises_due": [promise(NOW + timedelta(minutes=1))]}, set(), NOW) is None
    assert due_promise({"promises_due": [promise(NOW)]}, {"promise:p1"}, NOW) is None


def care(follow_up, status="pending"):
    return {
        "id": "e1",
        "content": "面试",
        "time": (follow_up - timedelta(hours=2)).isoformat(),
        "follow_up_at": follow_up.isoformat(),
        "status": status,
    }


def test_due_care_expires_after_12_hours():
    assert due_care([care(NOW - timedelta(hours=1))], set(), NOW).kind == "care"
    assert due_care([care(NOW - timedelta(hours=13))], set(), NOW) is None
    assert due_care([care(NOW - timedelta(hours=1), "asked")], set(), NOW) is None
    assert due_care([care(NOW + timedelta(hours=1))], set(), NOW) is None


def test_hold_expiry():
    data = {
        "hold": "user",
        "hold_started": (NOW - timedelta(hours=7)).isoformat(),
        "hold_notified": False,
        "last_user_text": "等我忙完找你",
    }
    assert expired_hold(data, 6, NOW).kind == "hold"
    assert not hold_active(data, 6, NOW)
    assert hold_active(data, 8, NOW)
    assert expired_hold({**data, "hold_notified": True}, 6, NOW) is None


def test_topic_candidates():
    topics = topic_candidates(
        {"current_slot": FREE},
        set(),
        [care(NOW - timedelta(hours=1))],
        NOW,
        miss_you_allowed=True,
    )
    ids = [topic["id"] for topic in topics]
    assert ids[0] == "life" and "care:e1" in ids and ids[-1] == "miss_you:2026-11-11"


def test_future_care_is_not_a_topic_and_follow_up_survives():
    upcoming = [care(NOW + timedelta(hours=1))]
    topics = topic_candidates({"current_slot": FREE}, set(), upcoming, NOW, miss_you_allowed=False)
    assert not any(topic["id"].startswith("care:") for topic in topics)
    later = NOW + timedelta(hours=1, minutes=5)
    assert due_care(upcoming, set(), later).key == "care:e1"
    without = topic_candidates({"current_slot": FREE}, set(), [], NOW, miss_you_allowed=False)
    assert not any(topic["id"].startswith("miss_you") for topic in without)


# -- ledger ------------------------------------------------------------------
def test_ledger_migrates_v1_3_fields(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text(
        json.dumps(
            {"hold": "night", "waiting_reply": {"x": 1}, "hold_until": "z", "bot_self_id": "42", "unanswered": 1}
        ),
        encoding="utf-8",
    )
    ledger = Ledger(tmp_path / "new.json", legacy)
    assert ledger.data["hold"] == ""
    assert "waiting_reply" not in ledger.data and "hold_until" not in ledger.data
    assert ledger.data["platform_name"] == "aiocqhttp"
    assert ledger.data["unanswered"] == 1
    assert (tmp_path / "new.json").is_file()


def test_ledger_message_seen_returns_unanswered_facts(tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(
        unanswered=2,
        unanswered_since="2026-11-11T10:00:00+08:00",
        last_proactive="2026-11-11T12:00:00+08:00",
        last_user_text="晚安",
    )
    facts = ledger.message_seen(NOW, "早", "", NOW + timedelta(hours=1))
    assert facts["count"] == 2 and facts["user_last_before"] == "晚安"
    assert ledger.data["unanswered"] == 0
    assert ledger.message_seen(NOW, "又来了", "", NOW) is None


def turn(**extra):
    return {
        "key": "timed:x",
        "day": "2026-11-11",
        "deliveries": [],
        "delivered_at": NOW.isoformat(),
        "message_version": 0,
        "counted": True,
        "miss_you": False,
        **extra,
    }


def test_ledger_accept_counts(tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.accept(turn(), [{"medium": "text"}], "2026-11-11")
    assert ledger.day("2026-11-11")["rounds"] == 1 and ledger.data["unanswered"] == 1
    ledger.accept(turn(key="promise:p", counted=False), [{"medium": "text"}], "2026-11-11")
    assert ledger.day("2026-11-11")["rounds"] == 1 and ledger.data["unanswered"] == 1
    ledger.accept(turn(key="timed:y", miss_you=True), [{"medium": "photo"}], "2026-11-11")
    day = ledger.day("2026-11-11")
    assert (day["rounds"], day["miss_you"], day["photos"]) == (2, 1, 1)


# -- coordinator.choose ------------------------------------------------------
class Linked:
    def __init__(self, events=()):
        self.events = list(events)

    def user_events(self, target):
        return self.events

    def state(self):
        return {"now": NOW.isoformat()}


def coordinator(tmp_path, events=(), **config):
    settings = Settings.from_config({"quiet_hours": "1-7", **config})
    ledger = Ledger(tmp_path / "l.json")
    return Coordinator(settings, "qq:FriendMessage:1", ledger, Linked(events), None, None)


def test_choose_schedules_first_due(tmp_path):
    chooser = coordinator(tmp_path)
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    assert NOW + timedelta(minutes=60) <= chooser.ledger.due() <= NOW + timedelta(minutes=90)


def test_choose_timed_when_due_and_free(tmp_path):
    chooser = coordinator(tmp_path)
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    trigger = chooser.choose({"current_slot": FREE}, NOW)
    assert trigger.kind == "timed" and trigger.topics


def test_choose_waits_for_free_then_grace(tmp_path):
    chooser = coordinator(tmp_path)
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    assert chooser.choose({"current_slot": BUSY}, NOW) is None
    assert chooser.ledger.data["waiting_free_since"] == "busy"
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    grace_end = datetime.fromisoformat(chooser.ledger.data["waiting_free_since"])
    assert NOW + timedelta(minutes=1) <= grace_end <= NOW + timedelta(minutes=5)
    assert chooser.choose({"current_slot": FREE}, grace_end + timedelta(seconds=1)).kind == "timed"


def test_choose_quiet_hours_repicks(tmp_path):
    night = NOW.replace(hour=3)
    chooser = coordinator(tmp_path)
    chooser.ledger.set_due(night - timedelta(minutes=1))
    assert chooser.choose({"current_slot": FREE}, night) is None
    assert chooser.ledger.due() > night


def test_choose_quiet_hours_can_be_disabled(tmp_path):
    night = NOW.replace(hour=3)
    chooser = coordinator(tmp_path, quiet_hours_enabled=False)
    chooser.ledger.set_due(night - timedelta(minutes=1))
    assert chooser.choose({"current_slot": FREE}, night).kind == "timed"


def test_choose_unanswered_limit_stops(tmp_path):
    chooser = coordinator(tmp_path)
    chooser.ledger.data["unanswered"] = 2
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    assert chooser.ledger.due() is None
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    assert chooser.ledger.due() is None


def test_choose_daily_limit_moves_to_tomorrow(tmp_path):
    chooser = coordinator(tmp_path, daily_max=1)
    chooser.ledger.data["days"]["2026-11-11"] = {"rounds": 1}
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    assert chooser.ledger.due().date().isoformat() == "2026-11-12"


def test_choose_promise_ignores_quiet_hours_and_limits(tmp_path):
    night = NOW.replace(hour=3)
    chooser = coordinator(tmp_path)
    chooser.ledger.data["unanswered"] = 5
    state = {"current_slot": FREE, "promises_due": [promise(night - timedelta(minutes=1))]}
    assert chooser.choose(state, night).kind == "promise"
    assert chooser.choose({**state, "current_slot": BUSY}, night) is None


def test_choose_care_respects_quiet_hours(tmp_path):
    chooser = coordinator(tmp_path, events=[care(NOW - timedelta(hours=1))])
    assert chooser.choose({"current_slot": FREE}, NOW).kind == "care"
    night = NOW.replace(hour=3)
    chooser = coordinator(tmp_path, events=[care(night - timedelta(hours=1))])
    assert chooser.choose({"current_slot": FREE}, night) is None


def test_choose_miss_you_topic_needs_a_chat_gap(tmp_path):
    chooser = coordinator(tmp_path)
    chooser.ledger.data["last_seen"] = (NOW - timedelta(hours=2)).isoformat()
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    topics = [json.loads(topic) for topic in chooser.choose({"current_slot": FREE}, NOW).topics]
    miss = next(topic for topic in topics if topic["id"] == "miss_you:2026-11-11")
    assert miss["类型"] == "想对方了" and miss["内容"]


# -- miss you at a natural point (v1.4.2, plan A) ------------------------------
DONE = {**FREE, "start": (NOW - timedelta(minutes=10)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}
MEETING = {**BUSY, "start": (NOW - timedelta(hours=2)).isoformat(), "end": DONE["start"]}
SLEEP = {"activity": "睡觉", "phone": "offline", "rest_type": "night_sleep", "start": "", "end": ""}


def quiet_chat(tmp_path, **config):
    chooser = coordinator(tmp_path, **config)
    chooser.ledger.data["last_seen"] = (NOW - timedelta(hours=3)).isoformat()
    chooser.ledger.set_due(NOW + timedelta(hours=1))  # the random time is not what triggers it
    return chooser


def test_miss_you_after_something_ends(tmp_path):
    chooser = quiet_chat(tmp_path)
    trigger = chooser.choose({"current_slot": DONE, "previous_slot": MEETING}, NOW)
    assert trigger.kind == "miss_you" and trigger.counted
    assert trigger.fact == {"契机": "刚忙完一件事", "刚结束": "开会", "现在": DONE["activity"]} and trigger.reason
    assert trigger.valid_until == datetime.fromisoformat(DONE["start"]) + timedelta(minutes=30)
    state = {"current_slot": DONE, "previous_slot": MEETING}
    assert chooser.choose(state, NOW + timedelta(minutes=15)).kind == "miss_you"
    assert chooser.choose(state, NOW + timedelta(minutes=21)) is None  # 30 minutes after it ended


def test_miss_you_meal_and_bedtime(tmp_path):
    chooser = quiet_chat(tmp_path)
    meal = {**DONE, "activity": "吃晚饭"}
    assert chooser.choose({"current_slot": meal}, NOW).fact["契机"] == "到饭点了"
    long_free = {**FREE, "start": (NOW - timedelta(hours=1)).isoformat()}
    bed = {**SLEEP, "start": (NOW + timedelta(minutes=20)).isoformat()}
    assert chooser.choose({"current_slot": long_free, "next_slot": bed}, NOW).fact["契机"] == "快要睡觉了"
    assert chooser.choose({"current_slot": long_free}, NOW) is None  # no natural point


def test_miss_you_once_a_day(tmp_path):
    chooser = quiet_chat(tmp_path)
    state = {"current_slot": DONE, "previous_slot": MEETING}
    trigger = chooser.choose(state, NOW)
    chooser.ledger.data["considered"][trigger.key] = "2026-11-11"
    assert chooser.choose(state, NOW) is None  # the same point is not offered twice
    chooser.ledger.data["considered"].clear()
    sent = turn(key=trigger.key, miss_you=True, day="2026-11-11")
    chooser.ledger.accept(sent, [{"medium": "text"}], "2026-11-11")
    chooser.ledger.data["last_proactive"] = (NOW - timedelta(hours=3)).isoformat()
    meal = {**DONE, "activity": "吃晚饭", "start": (NOW + timedelta(hours=2)).isoformat()}
    timed = chooser.choose({"current_slot": meal}, NOW + timedelta(hours=2, minutes=5))
    assert timed.kind == "timed"  # the meal is not a miss-you point any more today
    assert not any(json.loads(topic)["id"].startswith("miss_you") for topic in timed.topics)


def test_miss_you_not_while_chatting(tmp_path):
    chooser = quiet_chat(tmp_path)
    state = {"current_slot": DONE, "previous_slot": MEETING}
    for field in ("last_seen", "last_reply", "last_proactive"):
        chooser.ledger.data.update(last_seen=(NOW - timedelta(hours=3)).isoformat(), last_reply="", last_proactive="")
        chooser.ledger.data[field] = (NOW - timedelta(minutes=30)).isoformat()  # within min interval (60)
        assert chooser.choose(state, NOW) is None, field
    chooser.ledger.data["last_proactive"] = ""
    chooser.ledger.data["last_seen"] = (NOW - timedelta(minutes=61)).isoformat()
    assert chooser.choose(state, NOW).kind == "miss_you"


def test_miss_you_not_when_busy_or_asleep(tmp_path):
    chooser = quiet_chat(tmp_path)
    busy = {**DONE, "phone": "glance", "activity": "开会"}
    assert chooser.choose({"current_slot": busy, "previous_slot": FREE}, NOW) is None
    asleep = {**DONE, "activity": "睡觉", "phone": "offline", "rest_type": "night_sleep"}
    assert chooser.choose({"current_slot": asleep, "previous_slot": FREE}, NOW) is None
    nap_done = {"current_slot": DONE, "previous_slot": {**MEETING, "activity": "午睡", "rest_type": "nap"}}
    assert chooser.choose(nap_done, NOW) is None  # waking up is not an "ended" point
    night = NOW.replace(hour=3)
    chooser.ledger.data["last_seen"] = (night - timedelta(hours=3)).isoformat()
    at_night = {**DONE, "start": (night - timedelta(minutes=5)).isoformat()}
    assert chooser.choose({"current_slot": at_night, "previous_slot": MEETING}, night) is None


def test_old_miss_you_hours_setting_is_ignored(caplog):
    settings = Settings.from_config({"miss_you_hours": "很久", "daily_max": 3})
    assert not hasattr(settings, "miss_you_hours") and settings.daily_max == 3
    assert "miss_you_hours" not in caplog.text


def test_choose_hold_active_repicks(tmp_path):
    chooser = coordinator(tmp_path)
    chooser.ledger.data.update(hold="user", hold_started=(NOW - timedelta(hours=1)).isoformat())
    chooser.ledger.set_due(NOW - timedelta(minutes=1))
    assert chooser.choose({"current_slot": FREE}, NOW) is None
    assert chooser.ledger.due() > NOW


# -- plan and reply facts ----------------------------------------------------
def test_parse_plan():
    allowed = {"voice": False, "photo": False}
    plan = parse_plan(json.dumps({"medium": "voice", "text": "[语音] 刚到家", "topic": " life "}), allowed)
    assert plan["medium"] == "text" and plan["text"] == "刚到家" and plan["topic"] == "life"
    assert parse_plan('{"medium": "skip"}', allowed) is None
    with pytest.raises(ValueError):
        parse_plan('{"medium": "text", "text": " "}', allowed)


def test_wait_reply_facts_text():
    facts = {
        "count": 2,
        "first_sent_at": (NOW - timedelta(hours=3)).isoformat(),
        "user_last_before": "晚安",
        "user_last_before_at": (NOW - timedelta(hours=10)).isoformat(),
    }
    text = wait_reply_facts(facts, {"wait_reply_reaction": "有点在意"}, NOW)
    assert "2 轮" in text and "3 小时前" in text and "晚安" in text and "有点在意" in text
    assert "现在是 15:00" in text


def test_next_due_reexported():
    assert timing.next_due(NOW, 1, 1) == NOW + timedelta(minutes=1)


def test_expired_hold_is_not_blocked_by_prompt_rules():
    from astrbot_plugin_life.components.chat.proactive.astrbot_bridge import _PROTOCOL
    from astrbot_plugin_life.components.chat.proactive.settings import DEFAULT_PROMPT

    started = (NOW - timedelta(hours=7)).isoformat()
    trigger = expired_hold({"hold": "user", "hold_started": started}, 6, NOW)
    assert "说忙完会来找你" in trigger.reason
    exception = next(line for line in _PROTOCOL.splitlines() if line.startswith("例外："))
    assert "说忙完会来找你" in exception and "不受第 1、2 条" in exception
    assert "还没到时候" in DEFAULT_PROMPT and "超时没来" in DEFAULT_PROMPT


@pytest.mark.parametrize(
    "activity",
    [
        "和同事聚餐",
        "吃火锅",
        "烧烤",
        "在食堂吃午饭",
        "点外卖",
        "下午茶",
        "吃夜宵",
        "早餐",
        "午餐",
        "晚餐",
        "吃个饭",
        "吃点东西",
        "和朋友约饭",
        "喝早茶",
        "吃麻辣烫",
        "吃顿饭",
    ],
)
def test_meal_moment_common_words(tmp_path, activity):
    chooser = quiet_chat(tmp_path)
    slot = {**DONE, "activity": activity}
    assert chooser.choose({"current_slot": slot}, NOW).fact["契机"] == "到饭点了", activity


@pytest.mark.parametrize(
    "activity", ["送外卖", "在火锅店打工", "做饭", "洗碗", "开会", "看书", "饭后散步", "整理房间", "写饭店点评稿"]
)
def test_meal_moment_not_for_other_activities(tmp_path, activity):
    chooser = quiet_chat(tmp_path)
    trigger = chooser.choose({"current_slot": {**DONE, "activity": activity}}, NOW)
    assert trigger is None or trigger.fact["契机"] != "到饭点了", activity
