"""Read-only, paginated BreezeBlue voice discovery."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import aiohttp

from .config import DEFAULT_BASE_URL
from .provider import VoiceError


class BreezeVoiceCatalog:
    REQUEST_TIMEOUT = 20
    MAX_JSON_BYTES = 2 * 1024 * 1024
    _INVALID_RESPONSE = "BreezeBlue 返回的声音目录有问题，请稍后再试"

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL, session: aiohttp.ClientSession | None = None
    ):
        self._base_url = base_url
        self._session = session
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()

    async def list_voices(
        self,
        api_key: str,
        *,
        search: str = "",
        voice_type: str = "all",
        page: int = 1,
        page_size: int = 50,
        next_page_token: str | None = None,
    ) -> dict:
        """Return one page; callers keep the filters when using its next token."""
        if not isinstance(search, str):
            raise VoiceError("声音搜索内容有问题，请重新输入")
        if voice_type not in ("all", "default", "personal"):
            raise VoiceError("请选择全部、公共或个人声音")
        if type(page) is not int or page < 1:
            raise VoiceError("声音目录页码有问题，请重新打开")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise VoiceError("声音目录每页数量应为 1 到 100")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not next_page_token.strip()
        ):
            raise VoiceError("声音目录翻页信息有问题，请重新搜索")
        params = {"voice_type": voice_type, "page": page, "page_size": page_size}
        if search.strip():
            params["search"] = search.strip()
        if next_page_token is not None:
            params["next_page_token"] = next_page_token
        payload = await self._request(api_key, "/v1/voices", params=params)
        voices = payload.get("voices")
        has_more = payload.get("has_more")
        total = payload.get("total")
        response_page = payload.get("page")
        response_size = payload.get("page_size")
        token = payload.get("next_page_token")
        if (
            not isinstance(voices, list)
            or type(has_more) is not bool
            or type(total) is not int
            or total < 0
            or type(response_page) is not int
            or response_page < 1
            or type(response_size) is not int
            or not 1 <= response_size <= 100
            or len(voices) > response_size
            or (token is not None and (not isinstance(token, str) or not token.strip()))
        ):
            raise VoiceError(self._INVALID_RESPONSE)
        return {
            "voices": [self._voice(voice) for voice in voices],
            "has_more": has_more,
            "total": total,
            "page": response_page,
            "page_size": response_size,
            "next_page_token": token,
        }

    async def get_voice(self, api_key: str, voice_id: str) -> dict:
        """Resolve a selected voice even when it is outside the visible page."""
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise VoiceError("请先选择一个声音")
        voice_id = voice_id.strip()
        payload = await self._request(api_key, "/v1/voices/" + quote(voice_id, safe=""))
        voice = self._voice(payload)
        if voice["voice_id"] != voice_id:
            raise VoiceError(self._INVALID_RESPONSE)
        return voice

    @classmethod
    def _voice(cls, payload: object) -> dict:
        required = ("voice_id", "name", "language_code", "origin", "voice_type")
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(field), str) or not payload[field].strip()
            for field in required
        ):
            raise VoiceError(cls._INVALID_RESPONSE)
        description = payload.get("description")
        if description is not None and not isinstance(description, str):
            raise VoiceError(cls._INVALID_RESPONSE)
        return {**{field: payload[field] for field in required}, "description": description}

    async def _request(self, api_key: str, path: str, *, params: dict | None = None) -> dict:
        if not isinstance(api_key, str) or not api_key.strip():
            raise VoiceError("请先填写 BreezeBlue 密钥")
        api_key = api_key.strip()
        if any(not 33 <= ord(character) <= 126 for character in api_key):
            raise VoiceError("BreezeBlue 密钥格式有问题，请重新填写")
        if self._closed:
            raise VoiceError("声音目录已经关闭")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.request(
                "GET",
                self._base_url + path,
                headers={"xi-api-key": api_key, "Accept": "application/json"},
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT, connect=10),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    messages = {
                        401: "API Key 不对，请到设置里检查",
                        403: "BreezeBlue 拒绝访问声音目录，请检查密钥权限",
                        404: "找不到这个声音，请重新选择",
                        429: "声音目录访问太频繁，请稍后再试",
                    }
                    raise VoiceError(
                        messages.get(response.status, "BreezeBlue 暂时无法读取声音目录，请稍后再试")
                    )
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > self.MAX_JSON_BYTES:
                        raise VoiceError("BreezeBlue 返回的声音目录太大，请缩小搜索范围")
        except asyncio.TimeoutError:
            raise VoiceError("读取 BreezeBlue 声音目录超时，请稍后再试") from None
        except (aiohttp.ClientError, OSError):
            raise VoiceError("连接 BreezeBlue 失败，请稍后再试") from None
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            raise VoiceError(self._INVALID_RESPONSE) from None
        if not isinstance(payload, dict):
            raise VoiceError(self._INVALID_RESPONSE)
        return payload
