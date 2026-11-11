import json
import threading

import pytest
from astrbot_plugin_life.components.chat.proactive.settings import LEGACY_DEFAULT_PROMPT
from astrbot_plugin_life.migrate import NEW_KEYWORDS, OLD_KEYWORDS, migrate_config, migrate_sections


def old_config():
    return {
        "chat": {
            "enabled": True,
            "target_session": "aiocqhttp:FriendMessage:123",
            "proactive_prompt": LEGACY_DEFAULT_PROMPT,
            "voice_enabled": False,
        },
        "schedule": {"change_trigger_keywords": list(OLD_KEYWORDS), "city": "上海"},
        "photo": {"api_key": "sk-keep"},
    }


def test_migrate_sections_upgrades_and_is_idempotent():
    config = old_config()
    changed = migrate_sections(config)
    assert len(changed) == 3
    assert config["chat"]["target_sessions"] == ["aiocqhttp:FriendMessage:123"]
    assert config["chat"]["target_session"] == ""
    assert config["chat"]["proactive_prompt"] == ""
    assert config["chat"]["voice_enabled"] is False
    assert config["schedule"]["change_trigger_keywords"] == OLD_KEYWORDS + NEW_KEYWORDS
    assert config["schedule"]["city"] == "上海"
    assert config["photo"] == {"api_key": "sk-keep"}
    assert migrate_sections(config) == []


def test_custom_values_untouched():
    config = {
        "chat": {"target_sessions": ["x:FriendMessage:1"], "proactive_prompt": "我的提示词"},
        "schedule": {"change_trigger_keywords": ["只要这个"]},
    }
    assert migrate_sections(config) == []
    assert config["chat"]["proactive_prompt"] == "我的提示词"


def test_legacy_target_merges_into_existing_list():
    config = {"chat": {"target_session": "a:FriendMessage:1", "target_sessions": ["b:FriendMessage:2"]}}
    migrate_sections(config)
    assert config["chat"]["target_sessions"] == ["a:FriendMessage:1", "b:FriendMessage:2"]


class FakeConfig(dict):
    def __init__(self, data, path):
        super().__init__(data)
        self.config_path = str(path)
        self.saved = 0

    def save_config(self):
        self.saved += 1
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump(dict(self), stream, ensure_ascii=False)


def test_migrate_config_saves_file(tmp_path):
    path = tmp_path / "astrbot_plugin_life_config.json"
    path.write_text(json.dumps(old_config(), ensure_ascii=False), encoding="utf-8")
    config = FakeConfig(old_config(), path)
    assert migrate_config(config, threading.Lock())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["chat"]["target_sessions"] == ["aiocqhttp:FriendMessage:123"]
    assert saved["photo"]["api_key"] == "sk-keep"
    assert migrate_config(config, threading.Lock()) == []
    assert config.saved == 1


def test_migrate_config_without_file(tmp_path):
    config = FakeConfig(old_config(), tmp_path / "missing.json")
    assert migrate_config(config, threading.Lock())
    assert config["chat"]["target_sessions"] == ["aiocqhttp:FriendMessage:123"]


# -- v1.4.3: old BreezeBlue default -> choupi ---------------------------------
PHOTO_NEW = "https://choupi.tech/v1/images/generations"
VOICE_NEW = "https://choupi.tech"


@pytest.mark.parametrize(
    "old",
    [
        "https://api.breeze.blue",
        "https://api.breeze.blue/",
        "https://api.breeze.blue/v1",
        "https://api.breeze.blue/v1/",
        "  https://api.breeze.blue  ",
        "HTTPS://API.Breeze.Blue/V1",
        "https://api.breeze.blue:443/v1",
    ],
)
def test_old_default_service_moves_to_choupi(old):
    config = {"photo": {"base_url": old, "model": "m"}, "voice": {"base_url": old, "voice_id": "v"}}
    changed = migrate_sections(config)
    assert config == {
        "photo": {"base_url": PHOTO_NEW, "model": "m", "base_url_migrated": True},
        "voice": {"base_url": VOICE_NEW, "voice_id": "v", "base_url_migrated": True},
    }
    assert len(changed) == 2
    assert migrate_sections(config) == []


def test_service_migration_runs_once():
    config = {"voice": {"base_url": "https://api.breeze.blue"}}
    migrate_sections(config)
    config["voice"]["base_url"] = "https://api.breeze.blue"  # the user switches back on purpose
    assert migrate_sections(config) == []
    assert config["voice"]["base_url"] == "https://api.breeze.blue"


@pytest.mark.parametrize(
    "old", ["https://api.breeze.blue/v1/images/generations", " https://API.breeze.blue/v1/Images/Generations/ "]
)
def test_old_default_full_images_path(old):
    config = {"photo": {"base_url": old}}
    migrate_sections(config)
    assert config["photo"]["base_url"] == PHOTO_NEW


@pytest.mark.parametrize(
    "own",
    [
        "",
        "https://tts.example.com/v1/",
        "https://img.example.com/custom/v1/",
        "http://api.breeze.blue",
        "https://api.breeze.blue/proxy/v1",
        "https://api.breeze.blue:8443",
        "https://user:pw@api.breeze.blue",
        "https://api.breeze.blue/v1?x=1",
        "https://cn.api.breeze.blue",
        "https://choupi.tech",
    ],
)
def test_other_service_addresses_untouched(own):
    config = {"photo": {"base_url": own}, "voice": {"base_url": own}}
    assert migrate_sections(config) == []
    assert config == {"photo": {"base_url": own}, "voice": {"base_url": own}}


def test_voice_images_path_is_not_a_voice_default():
    config = {"voice": {"base_url": "https://api.breeze.blue/v1/images/generations"}}
    assert migrate_sections(config) == []


def test_service_migration_saves_file_and_keeps_others(tmp_path):
    data = {
        "chat": {"target_sessions": ["x:FriendMessage:1"], "daily_max": 7},
        "photo": {"base_url": "https://img.example.com/custom/v1/", "api_keys": ["sk-photo"], "model": "m"},
        "voice": {"base_url": "https://api.breeze.blue/v1/", "api_key": "sk-voice", "voice_id": "v"},
    }
    path = tmp_path / "astrbot_plugin_life_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config = FakeConfig(json.loads(json.dumps(data)), path)
    assert migrate_config(config, threading.Lock())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["voice"] == {"base_url": VOICE_NEW, "api_key": "sk-voice", "voice_id": "v", "base_url_migrated": True}
    assert saved["photo"] == {**data["photo"], "base_url_migrated": True}  # checked, address kept
    assert saved["chat"] == data["chat"]
    assert config["voice"]["base_url"] == VOICE_NEW
    assert migrate_config(config, threading.Lock()) == []
    assert config.saved == 1


def test_service_migration_when_only_saved_file_is_old(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"voice": {"base_url": "https://api.breeze.blue"}}), encoding="utf-8")
    config = FakeConfig({"voice": {"base_url": "https://api.breeze.blue"}}, path)
    migrate_config(config, threading.Lock())
    assert json.loads(path.read_text(encoding="utf-8"))["voice"]["base_url"] == VOICE_NEW


def test_migration_marker_is_declared_in_schema():
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    for section in ("photo", "voice"):
        marker = schema[section]["items"]["base_url_migrated"]
        assert marker["invisible"] is True and marker["default"] is False


def load(config, path):
    """One plugin load: migrate, then what AstrBot keeps on disk."""
    migrate_config(config, threading.Lock())
    return json.loads(path.read_text(encoding="utf-8"))


def install(tmp_path, photo, voice):
    data = {"photo": {"base_url": photo}, "voice": {"base_url": voice}}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return FakeConfig(json.loads(json.dumps(data)), path), path


def user_saves(config, path, section, value):
    config[section]["base_url"] = value
    config.save_config()


def test_fresh_install_then_breeze_is_kept(tmp_path, caplog):
    config, path = install(tmp_path, PHOTO_NEW, VOICE_NEW)
    saved = load(config, path)
    assert saved["photo"]["base_url_migrated"] is True and saved["voice"]["base_url_migrated"] is True
    assert "已升级旧配置" not in caplog.text  # marking alone is silent
    for section in ("photo", "voice"):
        user_saves(config, path, section, "https://api.breeze.blue/v1")
    saved = load(config, path)
    assert saved["photo"]["base_url"] == saved["voice"]["base_url"] == "https://api.breeze.blue/v1"


def test_empty_address_is_marked_too(tmp_path):
    config, path = install(tmp_path, "", "")
    saved = load(config, path)
    assert saved["photo"] == {"base_url": "", "base_url_migrated": True}
    user_saves(config, path, "voice", "https://api.breeze.blue")
    assert load(config, path)["voice"]["base_url"] == "https://api.breeze.blue"


def test_own_address_then_breeze_is_kept(tmp_path):
    config, path = install(tmp_path, "https://img.example.com/v1", "https://tts.example.com/v1/")
    saved = load(config, path)
    assert saved["voice"] == {"base_url": "https://tts.example.com/v1/", "base_url_migrated": True}
    user_saves(config, path, "voice", "https://api.breeze.blue")
    user_saves(config, path, "photo", "https://api.breeze.blue/v1/images/generations")
    saved = load(config, path)
    assert saved["voice"]["base_url"] == "https://api.breeze.blue"
    assert saved["photo"]["base_url"] == "https://api.breeze.blue/v1/images/generations"


def test_old_default_still_migrates_only_once(tmp_path):
    config, path = install(tmp_path, "https://api.breeze.blue/v1", "https://api.breeze.blue")
    saved = load(config, path)
    assert (saved["photo"]["base_url"], saved["voice"]["base_url"]) == (PHOTO_NEW, VOICE_NEW)
    assert config.saved == 1
    assert load(config, path) == saved and config.saved == 1  # reloading changes nothing
    user_saves(config, path, "voice", "https://api.breeze.blue")
    assert load(config, path)["voice"]["base_url"] == "https://api.breeze.blue"
