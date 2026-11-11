import json
from pathlib import Path

from astrbot_plugin_life.components.photo.core.config import DEFAULT_BASE_URL as PHOTO_DEFAULT
from astrbot_plugin_life.components.photo.core.config import Settings as PhotoSettings
from astrbot_plugin_life.components.photo.core.provider import normalize_base_url
from astrbot_plugin_life.components.voice.core.config import DEFAULT_BASE_URL as VOICE_DEFAULT
from astrbot_plugin_life.components.voice.core.config import Settings as VoiceSettings

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))


def test_schema_defaults():
    assert SCHEMA["photo"]["items"]["base_url"]["default"] == "https://choupi.tech/v1/images/generations"
    assert SCHEMA["voice"]["items"]["base_url"]["default"] == "https://choupi.tech"
    assert SCHEMA["photo"]["items"]["base_url"]["default"] == PHOTO_DEFAULT
    assert SCHEMA["voice"]["items"]["base_url"]["default"] == VOICE_DEFAULT


def test_empty_photo_address_falls_back():
    for config in ({}, {"base_url": ""}, {"base_url": "   "}):
        assert PhotoSettings.from_config(config).base_url == PHOTO_DEFAULT
    assert normalize_base_url(PHOTO_DEFAULT) == "https://choupi.tech/v1"
    assert PhotoSettings.from_config({"base_url": "https://example.com/v1"}).base_url == "https://example.com/v1"


def test_empty_voice_address_falls_back():
    for config in ({}, {"base_url": ""}, {"base_url": "  "}):
        assert VoiceSettings.from_config(config).base_url == "https://choupi.tech"
    assert VoiceSettings.from_config({"base_url": "https://api.breeze.blue/v1"}).base_url == "https://api.breeze.blue"


def test_old_voice_cache_keys_unchanged(tmp_path, monkeypatch):
    """Audio cached under the pre-1.4.2 default (no base_url in the key) is still reused."""
    import asyncio
    import hashlib

    from astrbot_plugin_life.components.voice.core import provider

    names = {}

    async def fake(self, payload, out_path, pending, voice_id):
        names[self._base_url] = out_path.name
        return out_path

    monkeypatch.setattr(provider.BreezeClient, "_synthesize", fake)
    for url in ("https://api.breeze.blue", "https://choupi.tech"):
        asyncio.run(provider.BreezeClient("k", "v", tmp_path, base_url=url).synthesize("你好"))
    identity = {
        "credential": hashlib.sha256(b"k").hexdigest(),
        "voice": "v",
        "payload": provider.build_tts_body("你好"),
        "format": "mp3",
    }
    legacy = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert names["https://api.breeze.blue"] == f"{legacy}.mp3"
    assert names["https://choupi.tech"] != f"{legacy}.mp3"


def test_hint_texts_follow_user_wording():
    schedule = SCHEMA["schedule"]["items"]
    assert schedule["owner_ids"]["hint"] == "QQ 个人号填主人 QQ 号；机器人则给对话框发送/sid，获得后填入UID即可。"
    assert schedule["default_reference_umo"]["hint"] == "在目标 QQ 个人号或 QQ 官方机器人发送UID，之后填入UMO即可"
    assert "调到 600 秒" in SCHEMA["photo"]["items"]["generate_enabled"]["hint"]


def test_photo_request_timeout_is_600_seconds():
    from astrbot_plugin_life.components.photo.core import provider

    assert provider.REQUEST_TIMEOUT == 600
    client = provider.PhotoClient("https://choupi.tech/v1", ("k",), "m")
    assert client._api.timeout.read == 600 and client._api.timeout.connect == 15
    assert client._downloads.timeout.read == 600


def test_no_old_reference_session_hint_left():
    root = Path(__file__).resolve().parents[1]
    for path in [*root.rglob("*.py"), *root.rglob("*.json"), *root.rglob("*.js"), *root.rglob("*.html")]:
        if "tests" in path.parts:
            continue
        assert "发送「参考会话 set」" not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_voice_url_hint_follows_user_wording():
    assert (
        SCHEMA["voice"]["items"]["base_url"]["hint"]
        == "填写兼容当前语音接口的服务地址，可带 /v1；保存后重新打开设置选择音色。（此URL为中转站的，并非官方）"
    )
