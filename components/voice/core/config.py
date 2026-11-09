"""插件设置只在加载入口校验，不读取旧 MiniMax 的配置。"""

from dataclasses import dataclass
from typing import Any, Mapping

from yarl import URL

DEFAULT_BASE_URL = "https://api.breeze.blue"


def _normalize_base_url(value: str) -> str:
    if not value:
        return DEFAULT_BASE_URL
    try:
        url = URL(value)
        authority = value.partition("://")[2].partition("/")[0]
        if (
            url.scheme not in {"http", "https"}
            or not url.raw_host
            or url.port == 0
            or "@" in authority
            or any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in value)
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "语音服务 URL 需要是有效的 HTTP 或 HTTPS 地址，地址中不能带账号密码、参数或片段"
        ) from None
    path = url.raw_path.rstrip("/").removesuffix("/v1").rstrip("/")
    return str(url.with_path(path, encoded=True))


@dataclass(frozen=True)
class Settings:
    enabled: bool
    api_key: str
    voice_id: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Settings":
        enabled = config.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("启用语音需要使用开关设置")
        values = {}
        for name in ("api_key", "voice_id", "base_url"):
            value = config.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"设置 {name} 需要填写文字")
            values[name] = value.strip()
        if any(char.isspace() for char in values["api_key"]):
            raise ValueError("BreezeBlue 密钥中不能含有空格或换行")
        values["base_url"] = _normalize_base_url(values["base_url"])
        return cls(enabled=enabled, **values)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.voice_id)
