"""G-1: while the "wait for me" reminder is still unsent, nothing else goes out in a burst.

Fake clock, the user never replies, the coordinator ticks every 20 s like
PROACTIVE_POLL_SECONDS. Reproduces the review run (shortest interval 1 minute:
skip, miss-you, bad output, reminder, random message = 3 messages in 4 minutes).
"""

import asyncio
from datetime import timedelta

from astrbot_plugin_life.components.chat.proactive.coordinator import Coordinator
from astrbot_plugin_life.components.chat.proactive.ledger import Ledger
from astrbot_plugin_life.components.chat.proactive.settings import Settings
from astrbot_plugin_life.components.chat.proactive.triggers import expired_hold, hold_retry_delay, hold_status
from test_hold_reminder import FREE, NOW, TARGET, Chat, History, Linked, held

DONE = {**FREE, "start": NOW.isoformat(), "end": (NOW + timedelta(hours=3)).isoformat()}
MEETING = {**FREE, "phone": "glance", "activity": "开会", "start": (NOW - timedelta(hours=1)).isoformat()}


class Life(Linked):
    def state(self):
        # Something just ended at NOW, so a miss-you point is open for 30 minutes.
        return {"now": self.now.isoformat(), "current_slot": DONE, "previous_slot": MEETING, "persona_id": "p"}


class Bridge:
    platform_id = "qq"
    context = None

    def __init__(self, hold_replies):
        self.hold_replies = list(hold_replies)
        self.sent = []  # (time, kind, text)
        self.asked = []
        self.linked = None

    def target_available(self, bot_self_id, platform_name):
        return True

    def build_media_event(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(supports_voice=False, supports_photo=False)

    async def current_conversation_id(self):
        return "c1"

    async def persona_matches(self, conversation_id, persona_id):
        return True

    async def generate(self, state, task, *args, **kwargs):
        kind = (
            "hold"
            if "说忙完会来找你" in task.get("触发原因", "")
            else "miss_you"
            if "节点" in task.get("触发原因", "")
            else "timed"
        )
        self.asked.append((self.linked.now, kind))
        self.kind = kind
        if kind == "hold":
            reply = self.hold_replies.pop(0) if self.hold_replies else '{"medium": "skip"}'
            if reply == "bad":
                return "这不是 JSON"
            return reply
        return '{"medium": "text", "text": "在干嘛"}'

    async def send_text(self, part, **kwargs):
        if not await kwargs["before_send"]():
            return {"status": "cancelled"}
        self.sent.append((self.linked.now, self.kind, part))
        return {"status": "sent", "medium": "text", "text": part}

    async def record(self, *args):
        pass


def simulate(tmp_path, minutes, hold_replies, **config):
    settings = Settings.from_config(
        {"quiet_hours": "1-7", "min_interval_minutes": 1, "max_interval_minutes": 2, "max_unanswered": 0, **config}
    )
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(
        held(),
        bot_self_id="bot",
        platform_name="qq",
        last_seen=(NOW - timedelta(hours=7)).isoformat(),
        next_due=(NOW + timedelta(minutes=2)).isoformat(),
    )
    linked = Life()
    linked.scope = (Chat(), 0)
    bridge = Bridge(hold_replies)
    bridge.linked = linked
    coordinator = Coordinator(settings, TARGET, ledger, linked, bridge, History())
    for _ in range(minutes * 3):
        try:
            asyncio.run(coordinator.tick())
        except ValueError:
            pass  # bad model output, logged by the caller in production
        linked.now += timedelta(seconds=20)
    return coordinator, bridge


def test_no_burst_while_the_reminder_is_unsent(tmp_path):
    coordinator, bridge = simulate(
        tmp_path, 10, ['{"medium": "skip"}', "bad", '{"medium": "text", "text": "忙完了没"}']
    )
    # Only the reminder itself may be tried in these 10 minutes; miss-you and random wait for it.
    assert [kind for _, kind in bridge.asked] == ["hold"]
    assert bridge.sent == []
    assert coordinator.ledger.data["hold_attempts"] == 1


def test_reminder_respects_chatting_and_spacing(tmp_path):
    coordinator, bridge = simulate(
        tmp_path, 8 * 60, ['{"medium": "skip"}', "bad", '{"medium": "text", "text": "忙完了没"}']
    )
    holds = [at for at, kind in bridge.asked if kind == "hold"]
    assert len(holds) == 3
    # Retries are at least 30 minutes apart even with a 1-minute shortest interval.
    assert all(b - a >= timedelta(minutes=30) for a, b in zip(holds, holds[1:]))
    first_send = bridge.sent[0]
    assert first_send[1] == "hold" and first_send[2] == "忙完了没"
    assert coordinator.ledger.data["hold_notified"] is True
    # Nothing at all went out before the reminder, and afterwards messages keep the shortest gap.
    times = [at for at, _, _ in bridge.sent]
    assert all(b - a >= timedelta(minutes=1) for a, b in zip(times, times[1:]))
    assert all(kind == "hold" for at, kind in bridge.asked if at <= first_send[0])


def test_unsent_reminder_is_dropped_after_six_hours(tmp_path):
    # The model keeps skipping; with a 120-minute retry gap, attempts run out first...
    coordinator, bridge = simulate(tmp_path, 9 * 60, [], min_interval_minutes=600, max_interval_minutes=700)
    holds = [at for at, kind in bridge.asked if kind == "hold"]
    assert len(holds) == 3 and all(b - a == timedelta(minutes=120) for a, b in zip(holds, holds[1:]))
    assert hold_status(coordinator.ledger.data, 6, NOW + timedelta(hours=9)) == "given_up"


def test_six_hour_limit_comes_before_the_attempt_limit(tmp_path):
    settings_hours = 6
    data = {**held(), "hold_attempts": 1, "hold_retry_at": ""}
    first_due = NOW - timedelta(hours=1)  # held() started 7 hours ago, hold_hours 6
    assert hold_status(data, settings_hours, first_due + timedelta(hours=5, minutes=59)) == "ready"
    assert hold_status(data, settings_hours, first_due + timedelta(hours=6)) == "given_up"
    assert expired_hold(data, settings_hours, first_due + timedelta(hours=6)) is None


def test_retry_delay_is_kept_within_30_and_120_minutes():
    assert hold_retry_delay(1) == timedelta(minutes=30)
    assert hold_retry_delay(45) == timedelta(minutes=45)
    assert hold_retry_delay(600) == timedelta(minutes=120)


def test_due_reminder_waits_while_chatting(tmp_path):
    settings = Settings.from_config({"quiet_hours": "1-7", "min_interval_minutes": 60})
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(held(), last_seen=(NOW - timedelta(hours=7)).isoformat())
    chooser = Coordinator(settings, TARGET, ledger, Life(), None, None)
    state = {"current_slot": FREE}
    ledger.data["last_proactive"] = (NOW - timedelta(minutes=10)).isoformat()
    assert chooser.choose(state, NOW) is None  # a message went out 10 minutes ago
    ledger.data["last_proactive"] = (NOW - timedelta(minutes=61)).isoformat()
    assert chooser.choose(state, NOW).kind == "hold"
    # While it is pending, a due random time is moved on instead of firing.
    ledger.data["hold_attempts"], ledger.data["hold_retry_at"] = 1, (NOW + timedelta(minutes=30)).isoformat()
    ledger.set_due(NOW - timedelta(minutes=1))
    assert chooser.choose(state, NOW) is None and ledger.due() > NOW
