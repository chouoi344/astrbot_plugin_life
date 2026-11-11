import asyncio

from astrbot_plugin_life import runtime
from astrbot_plugin_life.components.common.proxy import astrbot_proxy


class Context:
    def __init__(self, config):
        self.config = config

    def get_config(self, umo=None):
        return self.config


def test_proxy_from_astrbot_setting(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    context = Context({"http_proxy": "http://127.0.0.1:7890", "no_proxy": ["localhost", "*.lan"]})
    assert astrbot_proxy(context, "https://api.breeze.blue/v1") == "http://127.0.0.1:7890"
    assert astrbot_proxy(context, "http://localhost:8000") is None
    assert astrbot_proxy(context, "http://nas.lan/img") is None
    assert astrbot_proxy(Context({}), "https://x.com") is None


def test_proxy_from_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    assert astrbot_proxy(None, "https://a.example") == "http://proxy:1"
    assert astrbot_proxy(None, "https://internal.example") is None


class Event:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


def test_component_enabled_checked_once_per_message(monkeypatch):
    calls = []

    async def check(context, legacy_id, umo, event, allow_disabled):
        calls.append(legacy_id)
        return True

    monkeypatch.setattr(runtime, "_component_enabled", check)
    event = Event()
    for _ in range(3):
        assert asyncio.run(runtime.component_enabled(None, "photo", "u", event))
    assert calls == ["photo"]
    asyncio.run(runtime.component_enabled(None, "photo", "u", event, fresh=True))
    asyncio.run(runtime.component_enabled(None, "photo", "u", Event()))
    asyncio.run(runtime.component_enabled(None, "photo", "u", None))
    assert len(calls) == 4


def test_contract_constants_are_unique():
    from astrbot_plugin_life.components.common import contracts

    values = [value for name, value in vars(contracts).items() if name.isupper()]
    assert len(values) == len(set(values))


def test_disabled_once_logs_once(caplog):
    from astrbot_plugin_life.components.common.compat import disabled_once

    with caplog.at_level("WARNING"):
        disabled_once("测试功能", "原因")
        disabled_once("测试功能", "原因")
    assert sum("测试功能" in record.getMessage() for record in caplog.records) == 1


def test_aiohttp_proxy_skips_socks(monkeypatch):
    from astrbot_plugin_life.components.common.proxy import aiohttp_proxy

    assert aiohttp_proxy(Context({"http_proxy": "socks5://127.0.0.1:1080"}), "https://a.b") is None
    assert aiohttp_proxy(Context({"http_proxy": "http://127.0.0.1:7890"}), "https://a.b") == "http://127.0.0.1:7890"
