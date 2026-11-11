"""Smoke tests: every dashboard route answers without a 500, with real chat settings."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_life import dashboard
from astrbot_plugin_life.components.chat import main as chat_main
from astrbot_plugin_life.components.photo.core.config import Settings as PhotoSettings
from astrbot_plugin_life.components.voice.core.config import Settings as VoiceSettings
from astrbot_plugin_life.runtime import CHAT_ID, PHOTO_ID, QZONE_ID, SCHEDULE_ID, VOICE_ID

TARGET = "qq:FriendMessage:10001"


class Context:
    def __init__(self):
        self.persona_manager = SimpleNamespace(get_persona=lambda persona_id: {"name": "小林"})
        self.conversation_manager = SimpleNamespace()

    def get_config(self, umo=None):
        return {"provider_settings": {"default_personality": "p1"}, "admins_id": ["1"]}

    def get_platform_inst(self, platform_id):
        return None

    def get_registered_star(self, name):
        return None


class Request:
    def __init__(self, body=None):
        self.username = ""
        self.headers = {}
        self.cookies = {}
        self.content_type = "application/json"
        self.query = SimpleNamespace(getlist=lambda name: [])
        self._body = json.dumps(body or {}).encode()

    async def body(self):
        return self._body


class Scheduler:
    def __init__(self):
        self.config = {"enabled": False}
        self.events = {
            TARGET: [
                {
                    "id": "e1",
                    "content": "面试",
                    "time": "2026-11-11T14:00:00+08:00",
                    "follow_up_at": "2026-11-11T16:00:00+08:00",
                    "status": "pending",
                }
            ]
        }

    def user_events(self, umo):
        return self.events.get(umo, [])

    async def delete_user_event(self, umo, event_id):
        before = len(self.events.get(umo, []))
        self.events[umo] = [item for item in self.events.get(umo, []) if item["id"] != event_id]
        return len(self.events[umo]) != before


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "json_response", lambda data, status_code=200, headers=None: (status_code, data))
    monkeypatch.setattr(dashboard, "request", Request())
    monkeypatch.setattr(chat_main, "StarTools", SimpleNamespace(get_data_dir=lambda name: tmp_path / name))
    context = Context()
    chat_config = {
        "enabled": True,
        "target_sessions": [TARGET],
        "proactive_enabled": True,
        "daily_max": 5,
        "max_unanswered": 2,
    }
    chat = chat_main.LifeChat(context, chat_config)

    async def setup():
        await chat.initialize()
        if chat.worker:
            chat.worker.cancel()
            chat.worker = None

    asyncio.run(setup())
    voice = SimpleNamespace(
        settings=VoiceSettings(enabled=True, api_key="", voice_id=""),
        config=SimpleNamespace(schema={"voice_id": {"options": [""], "labels": [""]}}),
        _closing=False,
        client=None,
    )
    plugin = SimpleNamespace(
        context=context,
        components={CHAT_ID: chat, SCHEDULE_ID: Scheduler(), VOICE_ID: voice, PHOTO_ID: None, QZONE_ID: None},
        config={"chat": chat_config, "schedule": {"enabled": False}, "qzone": {"enabled": False}},
    )
    return dashboard.NaturalLifeDashboard(plugin)


GET_ROUTES = ("overview", "chat", "photos", "voice", "qzone")


@pytest.mark.parametrize("route", GET_ROUTES)
def test_get_routes_do_not_fail(board, route):
    status, data = asyncio.run(getattr(board, route)())
    assert status == 200, data
    assert data["status"] != "error"


def test_chat_route_with_target(board):
    status, data = asyncio.run(board.chat())
    assert status == 200, data
    assert data["status"] == "ready"
    (target,) = data["targets"]
    assert target["target"] == TARGET
    assert target["reason"]
    assert target["events"][0]["id"] == "e1"


def test_photo_image_is_404_not_500(board):
    status, _ = asyncio.run(board.photo_image())
    assert status in (400, 404)


@pytest.mark.parametrize("route", ("chat_event_delete", "chat_binding_reset", "voice_save"))
def test_writes_need_admin(board, route):
    status, data = asyncio.run(getattr(board, route)())
    assert status == 403, data


def test_admin_writes(board, monkeypatch):
    monkeypatch.setattr(dashboard, "_dashboard_admin", lambda context: True)
    monkeypatch.setattr(dashboard, "request", Request({"target": TARGET, "id": "e1"}))
    assert asyncio.run(board.chat_event_delete())[0] == 200
    assert asyncio.run(board.chat_event_delete())[0] == 404
    assert asyncio.run(board.chat_binding_reset())[0] == 200
    monkeypatch.setattr(dashboard, "request", Request({"target": "qq:FriendMessage:404"}))
    assert asyncio.run(board.chat_binding_reset())[0] == 404


def test_component_attributes_used_by_dashboard_exist():
    """Catch field-name drift like ``settings.proactive_enabled`` vs ``enabled``."""
    from astrbot_plugin_life.components.chat.proactive.settings import Settings

    chat = Settings.from_config({})
    for name in ("proactive_enabled", "targets", "daily_max", "max_unanswered", "unanswered_reached"):
        assert hasattr(chat, name), name
    photo = PhotoSettings()
    for name in ("generate_enabled", "edit_enabled", "selfie_enabled"):
        assert hasattr(photo, name), name
    voice = VoiceSettings(enabled=True, api_key="k", voice_id="v")
    for name in ("enabled", "api_key", "voice_id", "base_url", "configured"):
        assert hasattr(voice, name), name


def _reason(board, monkeypatch, **data):
    adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    monkeypatch.setattr(board.plugin.context, "get_platform_inst", lambda platform_id: adapter, raising=False)
    chat = board.plugin.components[CHAT_ID]
    chat.targets[TARGET].ledger.data.update({"bot_self_id": "42", **data})
    status, result = asyncio.run(board.chat())
    assert status == 200, result
    return result["targets"][0]["reason"]


def test_chat_hold_reason_tells_expired_apart(board, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).astimezone()
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=7)).isoformat()
    assert "暂时不主动打扰" in _reason(board, monkeypatch, hold="user", hold_started=recent)
    assert "已等满 6 小时" in _reason(board, monkeypatch, hold="user", hold_started=old)
    after = _reason(board, monkeypatch, hold="user", hold_started=old, hold_notified=True)
    assert "忙完" not in after


def test_chat_hold_reason_after_retries_run_out(board, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from astrbot_plugin_life.components.chat.proactive.triggers import HOLD_ASK_ATTEMPTS

    old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=7)).isoformat()
    retrying = _reason(board, monkeypatch, hold="user", hold_started=old, hold_attempts=HOLD_ASK_ATTEMPTS - 1)
    assert "已等满 6 小时" in retrying
    given_up = _reason(board, monkeypatch, hold="user", hold_started=old, hold_attempts=HOLD_ASK_ATTEMPTS)
    assert "已等满" not in given_up


def test_chat_hold_reason_while_retrying_and_after_giving_up(board, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).astimezone()
    old = (now - timedelta(hours=7)).isoformat()
    retry = now + timedelta(minutes=40)
    text = _reason(board, monkeypatch, hold="user", hold_started=old, hold_attempts=1, hold_retry_at=retry.isoformat())
    assert "没发出" in text and "1/3" in text and retry.strftime("%H:%M") in text and "不发其他主动消息" in text
    text = _reason(board, monkeypatch, hold="user", hold_started=old, hold_attempts=3, hold_retry_at="")
    assert "试了 3 次都没发出" in text and "不再追问" in text and "照常" in text
    long_ago = (now - timedelta(hours=13)).isoformat()
    text = _reason(board, monkeypatch, hold="user", hold_started=long_ago, hold_attempts=1, hold_retry_at="")
    assert "超过 6 小时" in text and "不再追问" in text
    text = _reason(board, monkeypatch, hold="user", hold_started=old, hold_attempts="坏", hold_retry_at=7)
    assert "已等满 6 小时" in text  # broken fields do not break the panel
