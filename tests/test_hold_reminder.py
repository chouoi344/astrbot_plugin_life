"""C2: the expired "wait for me" reminder is settled only by a real send, with a bounded retry."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from astrbot_plugin_life.components.chat.proactive.coordinator import Coordinator
from astrbot_plugin_life.components.chat.proactive.ledger import Ledger
from astrbot_plugin_life.components.chat.proactive.settings import Settings
from astrbot_plugin_life.components.chat.proactive.triggers import HOLD_ASK_ATTEMPTS, expired_hold, hold_status

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
FREE = {
    "phone": "free",
    "activity": "在咖啡店看书",
    "location": "街角咖啡店",
    "start": "2026-11-11T14:00:00+08:00",
    "end": "2026-11-11T16:00:00+08:00",
}
TARGET = "qq:FriendMessage:1"


def held(**extra):
    return {
        "hold": "user",
        "hold_started": (NOW - timedelta(hours=7)).isoformat(),
        "hold_notified": False,
        "last_user_text": "等我忙完找你",
        **extra,
    }


def test_expired_hold_waits_for_retry_and_stops_after_the_limit():
    assert expired_hold(held(), 6, NOW).kind == "hold"
    assert expired_hold(held(hold_retry_at=(NOW + timedelta(minutes=1)).isoformat()), 6, NOW) is None
    assert expired_hold(held(hold_retry_at=(NOW - timedelta(minutes=1)).isoformat()), 6, NOW).kind == "hold"
    assert expired_hold(held(hold_attempts=HOLD_ASK_ATTEMPTS - 1), 6, NOW).kind == "hold"
    assert expired_hold(held(hold_attempts=HOLD_ASK_ATTEMPTS), 6, NOW) is None


def test_ledger_settles_hold_only_on_a_hold_send(tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(held())
    base = {"day": "2026-11-11", "message_version": 0, "counted": True, "miss_you": False, "deliveries": []}
    ledger.accept(
        {**base, "key": "timed:x", "kind": "timed", "delivered_at": NOW.isoformat()}, [{"medium": "text"}], "2026-11-11"
    )
    assert not ledger.data["hold_notified"]
    ledger.accept(
        {**base, "key": "hold:x", "kind": "hold", "delivered_at": NOW.isoformat()}, [{"medium": "text"}], "2026-11-11"
    )
    assert ledger.data["hold_notified"]
    # An unknown send may have reached the user: never ask a second time.
    ledger.data.update(held(), hold_attempts=1)
    ledger.unknown_send({"key": "hold:y", "kind": "hold", "day": "2026-11-11"}, NOW)
    assert ledger.data["hold_notified"]


def test_new_hold_message_resets_the_attempts(tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(held(hold_attempts=2, hold_retry_at=NOW.isoformat()))
    ledger.message_seen(NOW, "我忙完了", "", NOW + timedelta(hours=1))
    assert (ledger.data["hold_attempts"], ledger.data["hold_retry_at"], ledger.data["hold_notified"]) == (0, "", False)
    ledger.message_seen(NOW, "等我忙完找你", "user", NOW + timedelta(hours=1))
    assert ledger.data["hold_attempts"] == 0 and ledger.data["hold_started"] == NOW.isoformat()


def test_old_ledger_gets_the_new_fields(tmp_path):
    path = tmp_path / "l.json"
    path.write_text('{"hold": "user", "hold_notified": false}', encoding="utf-8")
    ledger = Ledger(path)
    assert ledger.data["hold_attempts"] == 0 and ledger.data["hold_retry_at"] == ""


# -- a full tick with fakes ----------------------------------------------------
class Chat:
    async def claim_proactive_send(self, target, bot_self_id, epoch):
        return object()

    def release_proactive_send(self, ticket):
        pass


class Linked:
    def __init__(self):
        self.now = NOW

    def state(self):
        return {"now": self.now.isoformat(), "current_slot": FREE, "persona_id": "p"}

    def proactive_scope(self, target, bot_self_id):
        return self.scope

    def user_events(self, target):
        return []

    def is_owner(self, event):
        return True

    async def blocked(self, target, bot_self_id):
        return False

    async def recall_for(self, target):
        return None

    async def memory_epoch(self, target):
        return 0

    async def voice(self, target):
        return None

    async def photo(self, target):
        return None

    async def remember(self, event, **kwargs):
        pass


class Bridge:
    platform_id = "qq"
    context = None

    def __init__(self, replies):
        self.replies = list(replies)
        self.generated = 0
        self.sent = []

    def target_available(self, bot_self_id, platform_name):
        return True

    def build_media_event(self, **kwargs):
        return SimpleNamespace(supports_voice=False, supports_photo=False)

    async def current_conversation_id(self):
        return "c1"

    async def persona_matches(self, conversation_id, persona_id):
        return True

    async def generate(self, *args, **kwargs):
        self.generated += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def send_text(self, part, **kwargs):
        if kwargs["before_send"] is not None and not await kwargs["before_send"]():
            return {"status": "cancelled"}
        self.sent.append(part)
        return {"status": "sent", "medium": "text", "text": part}

    async def record(self, key, deliveries, conversation_id):
        pass


class History:
    async def recent_lines(self, *args):
        return []

    async def record_outgoing(self, *args):
        pass


def hold_chat(tmp_path, replies):
    settings = Settings.from_config({"quiet_hours": "1-7", "min_interval_minutes": 60})
    ledger = Ledger(tmp_path / "l.json")
    ledger.data.update(
        held(),
        bot_self_id="bot",
        platform_name="qq",
        last_seen=(NOW - timedelta(hours=7)).isoformat(),
        next_due=(NOW + timedelta(hours=5)).isoformat(),
    )
    linked = Linked()
    linked.scope = (Chat(), 0)
    bridge = Bridge(replies)
    return Coordinator(settings, TARGET, ledger, linked, bridge, History()), linked, bridge


def run_tick(coordinator, *, error=False):
    if error:
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(coordinator.tick())
    else:
        asyncio.run(coordinator.tick())


def test_failure_and_skip_do_not_swallow_the_reminder(tmp_path):
    coordinator, linked, bridge = hold_chat(
        tmp_path,
        [
            RuntimeError("model down"),
            "这不是 JSON",
            '{"medium": "skip"}',
            '好的\n{"medium": "text", "text": "忙完了吗"}',
        ],
    )
    data = coordinator.ledger.data
    run_tick(coordinator, error=True)  # generation failed
    assert (data["hold_notified"], data["hold_attempts"], bridge.generated) == (False, 1, 1)
    run_tick(coordinator)  # still waiting for the retry time: the model is not asked again
    assert bridge.generated == 1
    linked.now += timedelta(minutes=61)
    run_tick(coordinator, error=True)  # unparsable output
    assert (data["hold_notified"], data["hold_attempts"], bridge.generated) == (False, 2, 2)
    linked.now += timedelta(minutes=61)
    run_tick(coordinator)  # the model chose skip
    assert (data["hold_notified"], data["hold_attempts"], bridge.generated) == (False, 3, 3)
    linked.now += timedelta(minutes=61)
    run_tick(coordinator)  # limit reached: no endless asking
    assert bridge.generated == 3 and not bridge.sent


def test_reminder_is_settled_after_a_real_send(tmp_path):
    coordinator, linked, bridge = hold_chat(tmp_path, ['{"medium": "skip"}', '{"medium": "text", "text": "忙完了吗"}'])
    data = coordinator.ledger.data
    run_tick(coordinator)
    assert not data["hold_notified"] and data["hold_attempts"] == 1
    linked.now += timedelta(minutes=61)
    run_tick(coordinator)
    assert bridge.sent == ["忙完了吗"] and data["hold_notified"]
    linked.now += timedelta(minutes=61)
    run_tick(coordinator)
    assert bridge.generated == 2 and bridge.sent == ["忙完了吗"]  # never sent twice


@pytest.mark.parametrize(
    "bad",
    [
        {"hold_attempts": "2"},
        {"hold_attempts": None},
        {"hold_attempts": True},
        {"hold_attempts": -1},
        {"hold_retry_at": "明天"},
        {"hold_retry_at": 5},
        {"hold_started": "坏时间"},
        {"hold_notified": "yes"},
        {"hold": 1},
    ],
)
def test_broken_hold_fields_fall_back(tmp_path, bad):
    path = tmp_path / "l.json"
    path.write_text(json.dumps({**held(), **bad}), encoding="utf-8")
    ledger = Ledger(path)
    data = ledger.data
    assert isinstance(data["hold_attempts"], int) and not isinstance(data["hold_attempts"], bool)
    assert data["hold_attempts"] >= 0 and isinstance(data["hold_retry_at"], str)
    assert isinstance(data["hold_notified"], bool) and data["hold"] in ("", "user")
    # Nothing raises, and the in-memory helpers are just as forgiving.
    expired_hold(data, 6, NOW)
    hold_status({**held(), **bad}, 6, NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["hold_attempts"] == data["hold_attempts"]
