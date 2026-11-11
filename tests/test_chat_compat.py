import asyncio
from types import SimpleNamespace

from astrbot_plugin_life.components.chat import main as chat_main
from astrbot_plugin_life.components.common.contracts import NORMAL_CHAT

TARGET = "qq:FriendMessage:10001"


class Context:
    def get_platform_inst(self, platform_id):
        return None

    def get_registered_star(self, name):
        return None

    def get_config(self, umo=None):
        return {}


def make_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_main, "StarTools", SimpleNamespace(get_data_dir=lambda name: tmp_path))
    chat = chat_main.LifeChat(Context(), {"target_sessions": [TARGET], "proactive_enabled": False})
    asyncio.run(chat.initialize())
    return chat, chat.targets[TARGET]


class Event:
    unified_msg_origin = TARGET

    def __init__(self, **extra):
        self.__dict__.update(extra)

    def get_sender_id(self):
        return "10001"

    def is_private_chat(self):
        return True

    def get_extra(self, key):
        return key == NORMAL_CHAT

    def get_result(self):
        raise AssertionError("should not read the reply")


def test_busy_check_survives_registry_change(tmp_path, monkeypatch):
    chat, target = make_chat(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_main, "active_event_registry", SimpleNamespace())
    assert chat._busy(target) is False
    replying = SimpleNamespace(get_extra=lambda key: None, get_sender_id=lambda: "10001")
    monkeypatch.setattr(chat_main, "active_event_registry", SimpleNamespace(_events={TARGET: [replying]}))
    assert chat._busy(target) is True


def test_reply_record_skips_when_event_field_missing(tmp_path, monkeypatch):
    chat, _ = make_chat(tmp_path, monkeypatch)
    asyncio.run(chat.observe_normal_reply(Event()))
    asyncio.run(chat.observe_normal_reply(Event(_has_send_oper=False)))


def test_reset_binding_takes_target_lock(tmp_path, monkeypatch):
    chat, target = make_chat(tmp_path, monkeypatch)
    target.ledger.data["bot_self_id"] = "42"

    async def scenario():
        await target.lock.acquire()
        task = asyncio.create_task(chat.reset_binding(TARGET))
        await asyncio.sleep(0)
        assert target.bot_id == "42"
        target.lock.release()
        assert await task is True
        assert await chat.reset_binding("qq:FriendMessage:404") is False

    asyncio.run(scenario())
    assert target.bot_id == ""
