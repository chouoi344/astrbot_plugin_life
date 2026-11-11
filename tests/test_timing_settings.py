import random
from datetime import datetime, timedelta, timezone

import pytest
from astrbot_plugin_life.components.chat.proactive import timing
from astrbot_plugin_life.components.chat.proactive.settings import (
    DEFAULT_PROMPT,
    LEGACY_DEFAULT_PROMPT,
    Settings,
    target_list,
)

TZ = timezone(timedelta(hours=8))


def at(hour, minute=0, day=11):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1-7", ((1, 7),)),
        ("23-7，12-13", ((23, 7), (12, 13))),
        ("23-7、12-13; 0-1", ((23, 7), (12, 13), (0, 1))),
        ("22~24", ((22, 0),)),
        ("", ()),
    ],
)
def test_parse_quiet_hours(text, expected):
    assert timing.parse_quiet_hours(text) == expected


@pytest.mark.parametrize("text", ["1", "7-7", "25-3", "1:30-7", "晚上"])
def test_parse_quiet_hours_rejects(text):
    with pytest.raises(ValueError):
        timing.parse_quiet_hours(text)


def test_in_quiet_hours_wraps_midnight():
    ranges = timing.parse_quiet_hours("23-7")
    assert timing.in_quiet_hours(at(23), ranges)
    assert timing.in_quiet_hours(at(3), ranges)
    assert not timing.in_quiet_hours(at(7), ranges)
    assert not timing.in_quiet_hours(at(22, 59), ranges)


def test_next_due_within_interval():
    rng = random.Random(1)
    now = at(10)
    for _ in range(50):
        due = timing.next_due(now, 60, 90, rng)
        assert now + timedelta(minutes=60) <= due <= now + timedelta(minutes=90)


def test_next_day_due_starts_after_midnight():
    due = timing.next_day_due(at(22), 60, 90, random.Random(2))
    assert due.date() == at(0, day=12).date()
    assert 1 <= due.hour <= 2


def test_free_grace_range():
    for seed in range(20):
        grace = timing.free_grace(random.Random(seed))
        assert timedelta(minutes=1) <= grace <= timedelta(minutes=5)


def test_settings_defaults():
    settings = Settings.from_config({})
    assert (settings.min_interval_minutes, settings.max_interval_minutes) == (60, 90)
    assert settings.quiet_hours == ((1, 7),)
    assert settings.max_unanswered == 2
    assert settings.daily_max == 5
    assert settings.proactive_prompt == DEFAULT_PROMPT


def test_settings_legacy_prompt_and_target():
    settings = Settings.from_config(
        {
            "proactive_prompt": LEGACY_DEFAULT_PROMPT,
            "target_session": "qq:FriendMessage:1",
            "target_sessions": ["qq:FriendMessage:2", "qq:FriendMessage:1", "bad"],
        }
    )
    assert settings.custom_prompt == ""
    assert settings.targets == ("qq:FriendMessage:2", "qq:FriendMessage:1")
    assert settings.invalid_targets == ("bad",)


def test_settings_custom_prompt_kept():
    assert Settings.from_config({"proactive_prompt": " 自己写的 "}).proactive_prompt == "自己写的"


def test_unanswered_zero_is_unlimited():
    assert not Settings.from_config({"max_unanswered": 0}).unanswered_reached(99)
    assert Settings.from_config({"max_unanswered": 2}).unanswered_reached(2)


@pytest.mark.parametrize(
    ("config", "key", "field", "expected"),
    [
        ({"min_interval_minutes": 90, "max_interval_minutes": 60}, "max_interval_minutes", "min_interval_minutes", 60),
        ({"quiet_hours": "abc"}, "quiet_hours", "quiet_hours", ((1, 7),)),
        ({"daily_max": 0}, "daily_max", "daily_max", 5),
        ({"history_count": 201}, "history_count", "history_count", 20),
        ({"proactive_enabled": "yes"}, "proactive_enabled", "proactive_enabled", True),
        ({"proactive_prompt": 3}, "proactive_prompt", "custom_prompt", ""),
        ({"hold_hours": "x"}, "hold_hours", "hold_hours", 6.0),
    ],
)
def test_bad_setting_falls_back_and_names_key(config, key, field, expected, caplog):
    good = {"daily_max": 3, "target_sessions": ["qq:FriendMessage:1"]}
    settings = Settings.from_config({**good, **config})
    assert getattr(settings, field) == expected
    assert settings.targets == ("qq:FriendMessage:1",)
    if "daily_max" not in config:
        assert settings.daily_max == 3
    assert key in caplog.text and "默认值" in caplog.text


def test_bad_setting_does_not_stop_chat_component():
    from astrbot_plugin_life.components.chat.main import LifeChat

    chat = LifeChat(object(), {"quiet_hours": "abc", "daily_max": "很多", "target_sessions": ["qq:FriendMessage:1"]})
    assert chat.settings.quiet_hours == ((1, 7),) and chat.settings.daily_max == 5
    assert chat.settings.targets == ("qq:FriendMessage:1",)


def test_target_list_dedupes():
    assert target_list({"target_sessions": [" a ", "a"], "target_session": "b"}) == ("a", "b")
