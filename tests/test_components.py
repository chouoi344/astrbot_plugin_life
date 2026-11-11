import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_life.components.chat.history import format_lines
from astrbot_plugin_life.components.common.text import (
    SegmentSettings,
    media_side_note,
    split_bubbles,
    strip_media_markers,
    strip_voice_tags,
)
from astrbot_plugin_life.components.photo.core.sizes import SizeSetting, smaller_size
from astrbot_plugin_life.components.qzone.core import sharing
from astrbot_plugin_life.components.qzone.core.caption import valid_caption
from astrbot_plugin_life.components.qzone.core.settings import Settings as QzoneSettings
from astrbot_plugin_life.components.schedule.core.engine import LIFE_BUDGET, LifeEngine
from astrbot_plugin_life.components.schedule.core.user_events import UserEvents
from astrbot_plugin_life.components.voice.core.provider import cleanup_cache
from astrbot_plugin_life.components.voice.core.text import TAGS

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


# -- common text ---------------------------------------------------------------
def test_voice_tags_shared():
    assert "[轻笑]" in TAGS
    assert strip_voice_tags("【语音】[轻笑]你好[耳语]呀") == "你好呀"


def test_media_markers_and_side_note():
    assert strip_media_markers("[语音] 你好") == "你好"
    assert strip_media_markers("【照片】你看") == "你看"
    assert media_side_note("voice", "你好") == "你好\n（以上是你发的一条语音）"
    assert media_side_note("photo", "猫").startswith("（你发了一张照片")
    assert media_side_note("text", "你好") == "你好"


def test_split_bubbles_follows_segmented_reply():
    off = SegmentSettings({"enable": False})
    assert split_bubbles("第一句。第二句。\n第三行", off) == ["第一句。第二句。", "第三行"]
    on = SegmentSettings({"enable": True, "split_mode": "regex"})
    assert split_bubbles("第一句。第二句！", on) == ["第一句。", "第二句！"]
    words = SegmentSettings({"enable": True, "split_mode": "words", "split_words": ["。"]})
    assert split_bubbles("一。二。", words) == ["一", "二"]
    long_text = "很长。" * 100
    assert split_bubbles(long_text, on) == [long_text]


# -- photo sizes ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "ratio", "size"),
    [
        ("", None, "1536x2048"),
        ("2K", None, "1536x2048"),
        ("4K", None, "2448x3264"),
        ("16:9 1K", None, "1024x576"),
        ("3:4", "16:9", "2048x1152"),
        ("1536x2048", None, "1536x2048"),
    ],
)
def test_size_setting(text, ratio, size):
    assert SizeSetting.parse(text).size_for(ratio) == size


@pytest.mark.parametrize("text", ["8K", "7:3", "abc", "100x100", 12])
def test_size_setting_rejects(text):
    with pytest.raises(ValueError):
        SizeSetting.parse(text)


def test_smaller_size_steps_down():
    assert smaller_size("2448x3264") == "1536x2048"
    assert smaller_size("1536x2048") == "768x1024"
    assert smaller_size("768x1024") is None
    assert smaller_size("4000x3000") == "3264x2448"


# -- platform history lines ----------------------------------------------------
def row(kind, text, created):
    return SimpleNamespace(content={"type": kind, "message": [{"type": "plain", "text": text}]}, created_at=created)


def test_format_lines():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
    rows = [
        row("user", "昨天的话", datetime(2026, 11, 10, 12, 0)),  # naive UTC
        row("bot", "[Image]", datetime(2026, 11, 11, 6, 30)),
        row("user", "", datetime(2026, 11, 11, 6, 31)),
    ]
    assert format_lines(rows, now) == ["11-10 20:00 对方: 昨天的话", "14:30 你: [图片]"]


# -- user events -----------------------------------------------------------------
class MemoryStore:
    def __init__(self):
        self.files = {}

    def read(self, name, default=None):
        return json.loads(json.dumps(self.files.get(name, default)))

    def write(self, name, value):
        self.files[name] = json.loads(json.dumps(value))


def test_user_events_lifecycle():
    events = UserEvents(MemoryStore(), TZ)
    now = datetime(2026, 11, 11, 9, 0, tzinfo=TZ)
    added = events.add(
        "u",
        [
            {"content": "面试", "time": "2026-11-11 14:00", "follow_up_at": "2026-11-11 14:10"},
            {"content": "面试", "time": "2026-11-11 14:30"},
            {"content": "太久以前", "time": "2026-11-01 14:00"},
            {"content": "", "time": "2026-11-11 15:00"},
            "bad",
        ],
        now,
    )
    assert len(added) == 1
    item = added[0]
    assert datetime.fromisoformat(item["follow_up_at"]) == datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
    assert events.set_status("u", item["id"], "asked")
    assert events.list("u", now)[0]["status"] == "asked"
    assert events.set_status("u", item["id"], "done")
    assert events.set_status("u", item["id"], "pending")  # done stays done
    assert events.list("u", now)[0]["status"] == "done"
    with pytest.raises(ValueError):
        events.set_status("u", item["id"], "unknown")
    assert events.delete("u", item["id"])
    assert not events.delete("u", item["id"])
    assert events.list("u", now + timedelta(days=8)) == []


# -- life injection budget -----------------------------------------------------
class Promises:
    def __init__(self, items):
        self.items = items

    def refresh(self, now, changes):
        pass

    def for_injection(self, now, owner=False):
        return self.items


def fake_engine(promises=(), phone="free"):
    now = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
    current = {
        "start": "2026-11-11T14:00:00+08:00",
        "end": "2026-11-11T16:00:00+08:00",
        "activity": "在咖啡店看书",
        "location": "街角咖啡店",
        "mood": "放松",
        "phone": phone,
        "outfit": "白色毛衣" * 40,
    }
    return SimpleNamespace(
        life_ready=lambda: True,
        now=lambda: now,
        tz=TZ,
        persona_changed=False,
        config={"forbidden_interactions": []},
        current_day=lambda: {"actual": [], "changes": [], "date": "2026-11-11"},
        window=lambda: (now.replace(hour=7), now.replace(hour=23)),
        promises=Promises(list(promises)),
    ), {
        "current_slot": current,
        "weather": {"city": "上海", "summary": "晴"},
        "day_summary": "普通的一天",
        "events": [],
        "previous_slot": None,
    }


def life_block(text):
    return text.split("<character_life>\n", 1)[1].split("\n</character_life>", 1)[0]


def test_injection_keeps_budget_and_priority():
    promises = [{"id": f"p{i}", "content": f"约定{i}" * 20, "due": "今晚", "status": "pending"} for i in range(20)]
    promises.append({"id": "m", "content": "说好打电话", "due": "昨晚", "status": "missed", "note": "睡着了"})
    engine, state = fake_engine(promises)
    text, missed = LifeEngine.injection(engine, owner=True, state=state)
    block = life_block(text)
    assert len(block) <= LIFE_BUDGET
    assert block.startswith("现在：2026-11-11 15:00")
    assert "正在做：14:00—16:00" in block
    assert missed == ["m"] and "已失约" in block


def test_injection_busy_background_line():
    engine, state = fake_engine(phone="glance")
    text, _ = LifeEngine.injection(engine, state=state)
    assert "你看到了这条消息" in life_block(text)


# -- voice cache cleanup -------------------------------------------------------
def test_voice_cleanup_cache(tmp_path):
    old, fresh, other = tmp_path / "old.mp3", tmp_path / "new.mp3", tmp_path / "keep.txt"
    for path in (old, fresh, other):
        path.write_bytes(b"x")
    stamp = time.time() - 8 * 86400
    os.utime(old, (stamp, stamp))
    os.utime(other, (stamp, stamp))
    assert cleanup_cache(tmp_path) == 1
    assert not old.exists() and fresh.exists() and other.exists()
    assert cleanup_cache(tmp_path / "missing") == 0


# -- qzone -----------------------------------------------------------------------
def test_caption_only_checks_length():
    assert valid_caption("第一句。第二句！\n第三行")
    assert not valid_caption("  ")
    assert not valid_caption("字" * 2001)


def test_qzone_private_context_defaults_off():
    assert QzoneSettings.from_config({}).use_private_context is False
    assert QzoneSettings.from_config({"use_private_context": True}).use_private_context is True


def test_sharing_skips_private_sources_when_off(monkeypatch):
    called = []

    async def spy(*args, **kwargs):
        called.append(True)
        return [{"text": "私聊内容"}]

    monkeypatch.setattr(sharing, "_recent_chat", spy)
    monkeypatch.setattr(sharing, "_related_memory", spy)
    integrations = SimpleNamespace(resolve_binding=spy)
    posts = [{"binding": {"bot_id": "1", "persona_id": "p"}, "content": "以前的说说"}]
    binding = {"bot_id": "1", "persona_id": "p", "umo": "u"}
    result = asyncio.run(sharing.build_sharing_context(None, integrations, None, binding, posts, "看书"))
    assert result == {"evidence": [], "recent_posts": ["以前的说说"]}
    assert called == []


# -- packaging -------------------------------------------------------------------
def test_schema_has_every_chat_setting():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    chat = schema["chat"]["items"]
    for key in (
        "target_sessions",
        "target_session",
        "min_interval_minutes",
        "max_interval_minutes",
        "quiet_hours_enabled",
        "quiet_hours",
        "max_unanswered",
        "daily_max",
        "history_count",
        "hold_hours",
        "proactive_prompt",
    ):
        assert key in chat, key
    assert "miss_you_hours" not in chat
    assert chat["target_session"].get("invisible") is True
    assert chat["proactive_prompt"]["default"] == ""
    assert schema["qzone"]["items"]["use_private_context"]["default"] is False
    assert "image_size" in schema["photo"]["items"]


def test_versions_match():
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "version: v1.4.4" in metadata
    assert '"v1.4.4"' in main
    assert "## v1.4.4" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_plugin_imports():
    from astrbot_plugin_life import dashboard, main  # noqa: F401


def test_platform_history_stops_on_incompatible_api():
    from astrbot_plugin_life.components.chat.history import PlatformHistory

    class OldManager:
        calls = 0

        async def insert(self, **kwargs):
            OldManager.calls += 1
            raise TypeError("max_messages")

        async def get(self, **kwargs):
            return []

    history = PlatformHistory(SimpleNamespace(message_history_manager=OldManager()))
    for _ in range(3):
        asyncio.run(history.record_outgoing("u", "p", "你好"))
    assert OldManager.calls == 1
    now = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)
    assert asyncio.run(history.recent_lines("u", "p", 20, now)) is None


def test_normal_reply_marker_cleanup():
    from astrbot.api.message_components import Plain
    from astrbot_plugin_life.components.chat.main import LifeChat

    first, second = Plain(), Plain()
    first.text, second.text = "[语音] 刚到家", "[照片]保留中间的"
    marker_only = Plain()
    marker_only.text = "【照片】"
    result = SimpleNamespace(chain=[first, second], is_llm_result=lambda: True)
    event = SimpleNamespace(get_result=lambda: result)
    owner = SimpleNamespace(target_for=lambda event: object())
    asyncio.run(LifeChat.clean_reply_markers(owner, event))
    assert [item.text for item in result.chain] == ["刚到家", "[照片]保留中间的"]

    result.chain = [marker_only]
    asyncio.run(LifeChat.clean_reply_markers(owner, event))
    assert result.chain == []

    result.chain = [first]
    first.text = "[语音] 别的私聊"
    asyncio.run(LifeChat.clean_reply_markers(SimpleNamespace(target_for=lambda event: None), event))
    assert first.text == "[语音] 别的私聊"
