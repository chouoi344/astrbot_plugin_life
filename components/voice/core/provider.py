"""BreezeBlue speech generation; never replay an uncertain paid request."""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import DEFAULT_BASE_URL, LEGACY_BASE_URL
from .text import build_tts_body


class VoiceError(Exception):
    """A short Chinese message safe to show in chat."""


@dataclass
class _Response:
    status: int
    headers: dict[str, str]
    data: bytes

    def payload(self) -> dict:
        try:
            value = json.loads(self.data)
        except (ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class _Pending:
    task: asyncio.Task[Path] = field(init=False)
    waiters: int = 0
    started: bool = False


SYNTHESIS_CONCURRENCY = 2
CACHE_MAX_AGE_DAYS = 7
CLEANUP_INTERVAL = 6 * 3600


def cleanup_cache(directory: Path, max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """Delete cached MP3 files older than ``max_age_days``; return how many."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        files = list(Path(directory).glob("*.mp3"))
    except OSError:
        return 0
    for path in files:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _error_message(status: int, payload: dict) -> str:
    code = payload.get("code", "")
    code = code.upper() if isinstance(code, str) else ""
    detail = payload.get("detail", payload.get("error", ""))
    detail = detail.lower() if isinstance(detail, str) else ""
    if status == 401:
        return "API Key 不对，请到设置里检查"
    if status == 403:
        if (
            payload.get("cloudflare_error")
            or payload.get("error_code") in (1010, "1010")
            or payload.get("error_name") == "browser_signature_banned"
        ):
            return "BreezeBlue 的访问防护拦住了请求，请稍后重试或联系平台客服"
        return "BreezeBlue 拒绝了当前请求，请检查密钥权限或联系平台客服"
    if code == "API_KEY_CREDIT_LIMIT_EXCEEDED":
        return "这把密钥的积分额度用完了，请到 BreezeBlue 调整额度"
    if status == 402:
        return "积分不够了，请到 BreezeBlue 充值"
    if "LANGUAGE" in code or "language" in detail:
        return "这个声音或模型暂时不支持中文，请换一个声音再试"
    if any(word in detail for word in ("too long", "max_length", "1000", "1,000", "maximum length", "character limit")):
        return "要念的文字太长了，请缩短到一千字以内"
    if status == 404:
        return "所选音色不可用，请到设置里重新选择"
    if status in (429, 503):
        return "服务有点忙，请稍后再试"
    if status in (400, 422):
        return "文字或语气参数有问题，请检查内容，缩短后再试"
    return "BreezeBlue 暂时没能生成语音，请稍后再试"


def _is_mp3(data: bytes) -> bool:
    """Check a complete Layer III frame, including MPEG 2 and 2.5 rates."""
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10 or data[3] not in (2, 3, 4) or any(value & 0x80 for value in data[6:10]):
            return False
        tag_size = 0
        for value in data[6:10]:
            tag_size = (tag_size << 7) | value
        offset = 10 + tag_size
        if data[3] == 4 and data[5] & 0x10:
            offset += 10
    if len(data) < offset + 4:
        return False
    first, second, third, _ = data[offset : offset + 4]
    version = (second >> 3) & 3
    bitrate_index = third >> 4
    rate_index = (third >> 2) & 3
    if (
        first != 0xFF
        or second & 0xE0 != 0xE0
        or version == 1
        or (second >> 1) & 3 != 1
        or bitrate_index in (0, 15)
        or rate_index == 3
    ):
        return False
    bitrates = (
        (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        if version == 3
        else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    )
    sample_rate = (44100, 48000, 32000)[rate_index] // {3: 1, 2: 2, 0: 4}[version]
    frame_size = (144000 if version == 3 else 72000) * bitrates[bitrate_index] // sample_rate + ((third >> 1) & 1)
    return len(data) >= offset + frame_size


class BreezeClient:
    REQUEST_TIMEOUT = 60
    TOTAL_TIMEOUT = 180
    MAX_RETRIES = 3
    MAX_RETRY_DELAY = 30
    MAX_AUDIO_BYTES = 16 * 1024 * 1024
    _RECOVERY_ERROR = "语音已经开始生成，暂时还没取回，请稍后到 BreezeBlue 的生成记录查看"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        cache_dir: Path,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: aiohttp.ClientSession | None = None,
        proxy: str | None = None,
    ):
        if not api_key.strip() or not voice_id.strip():
            raise VoiceError("请先填写 BreezeBlue 密钥并选择音色")
        self._api_key = api_key.strip()
        self._voice_id = voice_id.strip()
        self._base_url = base_url
        self._cache_dir = cache_dir.resolve()
        self._session = session
        self._proxy = proxy
        # Two different lines may be synthesized at once; the same line is
        # still shared through ``_pending`` (item 21).
        self._slots = asyncio.Semaphore(SYNTHESIS_CONCURRENCY)
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self._last_cleanup = 0.0

    async def close(self) -> None:
        self._closed = True
        pending = [request.task for request in self._pending.values()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._session is not None:
            await self._session.close()

    def select_voice(self, voice_id: str) -> None:
        self._voice_id = voice_id

    async def synthesize(self, text: str) -> Path:
        if self._closed:
            raise VoiceError("语音服务已经关闭")
        payload = build_tts_body(text)
        voice_id = self._voice_id
        cache_identity = {
            "credential": hashlib.sha256(self._api_key.encode()).hexdigest(),
            "voice": voice_id,
            "payload": payload,
            "format": "mp3",
        }
        if self._base_url != LEGACY_BASE_URL:
            # Keep the existing BreezeBlue cache usable after upgrading.
            cache_identity["base_url"] = self._base_url
        cache_key = hashlib.sha256(json.dumps(cache_identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        out_path = self._cache_dir / f"{cache_key}.mp3"
        pending = self._pending.get(cache_key)
        if pending is None:
            pending = _Pending(waiters=1)
            pending.task = asyncio.create_task(self._synthesize(payload, out_path, pending, voice_id))
            self._pending[cache_key] = pending
            pending.task.add_done_callback(lambda completed: self._finished(cache_key, completed))
        else:
            pending.waiters += 1
        try:
            return await asyncio.shield(pending.task)
        finally:
            pending.waiters -= 1
            if not pending.waiters and not pending.started and not pending.task.done():
                pending.task.cancel()
                if self._pending.get(cache_key) is pending:
                    self._pending.pop(cache_key, None)

    def _finished(self, cache_key: str, task: asyncio.Task[Path]) -> None:
        pending = self._pending.get(cache_key)
        if pending is not None and pending.task is task:
            self._pending.pop(cache_key, None)
        # Awaiters still receive the exception; this also retrieves it when all
        # awaiters have timed out and the paid request finishes in the background.
        if not task.cancelled():
            task.exception()

    async def _synthesize(self, payload: dict, out_path: Path, pending: _Pending, voice_id: str) -> Path:
        async with self._slots:
            if self._closed:
                raise VoiceError("语音服务已经关闭")
            if out_path.is_file() and _is_mp3(await asyncio.to_thread(out_path.read_bytes)):
                return out_path
            try:
                audio = await asyncio.wait_for(self._generate(payload, pending, voice_id), self.TOTAL_TIMEOUT)
            except asyncio.TimeoutError:
                raise VoiceError("BreezeBlue 等待时间太长，请稍后到生成记录查看") from None
            await asyncio.to_thread(self._save, out_path, audio)
        if time.monotonic() - self._last_cleanup > CLEANUP_INTERVAL:
            self._last_cleanup = time.monotonic()
            await asyncio.to_thread(cleanup_cache, self._cache_dir)
        return out_path

    @staticmethod
    def _save(path: Path, audio: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".mp3", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(audio)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    async def _request(self, method: str, path: str, payload: dict | None = None) -> _Response:
        if self._closed:
            raise VoiceError("语音服务已经关闭")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "audio/mpeg, application/json",
            "User-Agent": "AstrBotLifeVoice/0.1.2",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with self._session.request(
                method,
                self._base_url + path,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT, connect=15),
                allow_redirects=False,
                proxy=self._proxy,
            ) as response:
                limit = self.MAX_AUDIO_BYTES if response.status == 200 else 64 * 1024
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limit:
                        if response.status == 200:
                            raise VoiceError("BreezeBlue 返回的声音文件太大，请缩短文字后重试")
                        del data[limit:]
                        break
                return _Response(
                    response.status,
                    {name.lower(): value for name, value in response.headers.items()},
                    bytes(data),
                )
        except asyncio.TimeoutError:
            message = (
                "BreezeBlue 响应超时，语音可能已生成，请到生成记录查看" if method == "POST" else self._RECOVERY_ERROR
            )
            raise VoiceError(message) from None
        except (aiohttp.ClientError, OSError):
            message = (
                "连接 BreezeBlue 中断，语音可能已生成，请到生成记录查看" if method == "POST" else self._RECOVERY_ERROR
            )
            raise VoiceError(message) from None

    def _audio(self, response: _Response) -> bytes:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in (
            "",
            "audio/mpeg",
            "audio/mp3",
            "application/octet-stream",
        ) or not _is_mp3(response.data):
            raise VoiceError("BreezeBlue 返回的声音文件有问题，请稍后再试")
        return response.data

    async def _wait(self, response: _Response, payload: dict, attempt: int, *, recovery=False) -> None:
        value = response.headers.get("retry-after")
        if value is None:
            meta = payload.get("meta")
            value = meta.get("retry_after_seconds") if isinstance(meta, dict) else None
        try:
            seconds = float(value)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError
        except (TypeError, ValueError):
            seconds = float(2**attempt)
            if isinstance(value, str):
                try:
                    when = email.utils.parsedate_to_datetime(value)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=dt.timezone.utc)
                    seconds = max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        if seconds > self.MAX_RETRY_DELAY:
            raise VoiceError(self._RECOVERY_ERROR if recovery else "服务有点忙，请稍后再试")
        await asyncio.sleep(seconds)

    async def _recover(self, history_id: str) -> bytes:
        path = "/v1/history/" + quote(history_id, safe="") + "/audio"
        for attempt in range(self.MAX_RETRIES + 1):
            response = await self._request("GET", path)
            if response.status == 200:
                return self._audio(response)
            if response.status in (425, 429, 503) and attempt < self.MAX_RETRIES:
                await self._wait(response, response.payload(), attempt, recovery=True)
                continue
            raise VoiceError(self._RECOVERY_ERROR)
        raise AssertionError("unreachable")

    async def _generate(self, payload: dict, pending: _Pending, voice_id: str) -> bytes:
        path = "/v1/text-to-speech/" + quote(voice_id, safe="") + "?output_format=mp3"
        for attempt in range(self.MAX_RETRIES + 1):
            if not pending.waiters:
                raise asyncio.CancelledError
            pending.started = True
            response = await self._request("POST", path, payload)
            if response.status == 200:
                return self._audio(response)
            history_id = response.headers.get("history-item-id", "").strip()
            if response.status in (503, 504) and history_id:
                return await self._recover(history_id)
            error = response.payload()
            busy = response.status == 429 or (
                response.status == 503
                and error.get("code") in ("GENERATION_CAPACITY_EXCEEDED", "GENERATION_CONCURRENCY_EXCEEDED")
            )
            if busy and attempt < self.MAX_RETRIES:
                pending.started = False
                if not pending.waiters:
                    raise asyncio.CancelledError
                await self._wait(response, error, attempt)
                continue
            raise VoiceError(_error_message(response.status, error))
        raise AssertionError("unreachable")
