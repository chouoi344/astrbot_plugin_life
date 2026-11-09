"""Small Images-compatible client with bounded, explicit retries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import re
import warnings
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("astrbot")

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_RESPONSE_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 1024 * 1024
REQUEST_TIMEOUT = 180

# Exact dimensions retained from the earlier photo provider's fixed policy.
_SMALLER_SIZES = {
    "2880x2880": "2048x2048",
    "3840x2160": "2048x1152",
    "2160x3840": "1152x2048",
    "3200x2560": "2080x1664",
    "2560x3200": "1664x2080",
    "3264x2448": "2048x1536",
    "2448x3264": "1536x2048",
    "3504x2336": "2064x1376",
    "2336x3504": "1376x2064",
    "3808x1632": "2016x864",
    "4096x4096": "2048x2048",
}


class PhotoError(RuntimeError):
    """A user-facing error that never includes upstream content or credentials."""


def _http_url(value: str):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        parsed.port
    except (ValueError, TypeError, AttributeError):
        raise PhotoError(
            "图片服务地址需要有效的 HTTP 或 HTTPS 地址，地址中不能带账号密码"
        ) from None
    return parsed


def normalize_base_url(value: str) -> str:
    """Accept a host, custom service prefix, or a complete Images endpoint."""
    parsed = _http_url(value)
    if parsed.query or parsed.fragment:
        raise PhotoError("图片服务地址请移除问号后的参数和井号后的内容")
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/chat/completions"):
        raise PhotoError("这里需要生图服务地址，请检查是否填成了聊天接口")
    for suffix in ("/images/generations", "/images/edits"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path or "/v1", "", ""))


def _verified_image(data: bytes) -> tuple[bytes, str, str]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise PhotoError("图片为空或超过 50 MB，请缩小图片后再试")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise PhotoError("图片的宽高过大，请缩小图片后再试")
                mime = Image.MIME.get(image.format)
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise PhotoError("图片需要 JPG、PNG 或 WebP 格式")
                image.load()
                return data, mime, f"{image.width}x{image.height}"
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise PhotoError("图片内容无效或已经损坏，请换一张图片") from None


def validate_image(data: bytes) -> tuple[bytes, str]:
    """Validate external image bytes once and retain their original encoding."""
    data, mime, _ = _verified_image(data)
    return data, mime


def _reference_mime(data: bytes) -> tuple[str, str]:
    # The reference upload entry point already calls validate_image.
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise PhotoError("参考图需要 JPG、PNG 或 WebP 格式")


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_RESPONSE_BYTES:
        raise PhotoError("生图服务返回的图片过大或格式有误")
    text = value.strip()
    if text.lower().startswith("data:"):
        matched = re.fullmatch(r"data:image/[a-z0-9.+-]+;base64,([\s\S]+)", text, re.I)
        if not matched:
            raise PhotoError("生图服务返回的图片编码有误")
        text = matched[1]
    text = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
    try:
        data = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (ValueError, binascii.Error):
        raise PhotoError("生图服务返回的图片编码有误") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise PhotoError("生图服务返回的图片超过 50 MB")
    return data


def _error_details(body: bytes) -> dict | str:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return body.decode("utf-8", "replace")
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, (dict, str)):
            return error
    return ""


def _invalid_size(status: int, body: bytes) -> bool:
    if status not in {400, 422}:
        return False
    error = _error_details(body)
    if isinstance(error, dict):
        param = str(error.get("param") or "").lower()
        if param and param != "size":
            return False
        code = str(error.get("code") or "").lower()
        if code in {"invalid_size", "unsupported_size"} or (
            param == "size" and code == "invalid_value"
        ):
            return True
        message = str(error.get("message") or error.get("detail") or "").lower()
    else:
        message = error.lower()
    return (
        re.search(r"\bsize\b", message) is not None
        and any(
            term in message
            for term in (
                "invalid",
                "unsupported",
                "not supported",
                "allowed",
                "must be",
                "not one of",
            )
        )
    ) or (
        "尺寸" in message
        and any(term in message for term in ("无效", "不支持", "不允许", "超出"))
    )


def _unaccepted_rate_limit(status: int, body: bytes) -> bool:
    if status != 429:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(payload, dict) or any(
        name in payload for name in ("data", "id", "task_id", "job_id")
    ):
        return False
    error = payload.get("error")
    return isinstance(error, dict) and error.get("code") in {
        "rate_limit_exceeded",
        "rate_limit_error",
        "too_many_requests",
        "requests_limit_exceeded",
    }


class _HTTPError(PhotoError):
    def __init__(self, status: int, body: bytes, retry_after: str | None):
        self.invalid_size = _invalid_size(status, body)
        self.retryable = _unaccepted_rate_limit(status, body)
        self.retry_after = retry_after
        if self.invalid_size:
            message = "生图服务不支持这次图片尺寸，请换一个尺寸"
        elif status in {401, 403}:
            message = "生图服务拒绝了访问，请检查密钥和账号权限"
        elif status == 429:
            message = "生图服务暂时繁忙或额度不足，请稍后再试"
        elif status >= 500:
            message = "生图服务暂时出错，本次没有自动重试，请稍后查看服务记录"
        else:
            message = f"生图服务未能完成请求（HTTP {status}），请检查服务设置"
        super().__init__(message)


async def _read_response(response: httpx.Response, maximum: int) -> bytes:
    length = response.headers.get("Content-Length", "")
    if length.isdecimal() and int(length) > maximum:
        raise PhotoError("生图服务返回的内容超过允许大小")
    chunks, total = [], 0
    async for chunk in response.aiter_bytes(64 * 1024):
        total += len(chunk)
        if total > maximum:
            raise PhotoError("生图服务返回的内容超过允许大小")
        chunks.append(chunk)
    return b"".join(chunks)


class PhotoClient:
    def __init__(
        self,
        base_url: str,
        api_keys: tuple[str, ...],
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise PhotoError("请先填写生图模型名称")
        if (
            not isinstance(api_keys, tuple)
            or not api_keys
            or any(
                not isinstance(key, str) or not re.fullmatch(r"[\x21-\x7e]+", key)
                for key in api_keys
            )
        ):
            raise PhotoError("请填写有效的生图密钥，每把密钥单独填写")
        self.model, self.api_keys = model, api_keys
        self._key_index = 0
        options = {
            "timeout": httpx.Timeout(REQUEST_TIMEOUT, connect=15),
            "follow_redirects": False,
            "trust_env": False,
            "transport": transport,
        }
        self._api = httpx.AsyncClient(**options)
        # Keep service cookies and authentication out of image downloads.
        self._downloads = httpx.AsyncClient(**options)

    async def _request(self, prompt: str, references: list[bytes], size: str, key: str):
        fields = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "output_format": "jpeg",
        }
        if references:
            files = []
            for index, data in enumerate(references, 1):
                mime, extension = _reference_mime(data)
                files.append(
                    ("image[]", (f"reference_{index}.{extension}", data, mime))
                )
            kwargs = {"data": fields, "files": files}
            endpoint = "/images/edits"
        else:
            kwargs = {"json": fields}
            endpoint = "/images/generations"
        # A cancellation queued before a retry must stop before another paid POST.
        await asyncio.sleep(0)
        async with self._api.stream(
            "POST",
            self.base_url + endpoint,
            headers={"Authorization": "Bearer " + key},
            **kwargs,
        ) as response:
            if not 200 <= response.status_code < 300:
                body = await _read_response(response, 64 * 1024)
                raise _HTTPError(
                    response.status_code, body, response.headers.get("Retry-After")
                )
            body = await _read_response(response, MAX_RESPONSE_BYTES)
            if response.headers.get("Content-Type", "").lower().startswith("image/"):
                return body
            try:
                return json.loads(body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise PhotoError(
                    "生图服务返回的内容格式有误，请稍后检查服务记录"
                ) from None

    async def _download(self, url: str) -> bytes:
        # Private addresses are allowed; every hop still needs a credential-free HTTP URL.
        for redirect in range(6):
            _http_url(url)
            async with self._downloads.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == 5:
                        raise PhotoError("图片下载跳转次数过多，请稍后再试")
                    location = response.headers.get("Location")
                    if not location:
                        raise PhotoError("图片下载地址已失效，请稍后再试")
                    try:
                        url = urljoin(url, location)
                    except ValueError:
                        raise PhotoError("图片下载跳转地址无效，请稍后再试") from None
                    continue
                if not 200 <= response.status_code < 300:
                    raise PhotoError("图片下载失败，请稍后检查服务记录")
                return await _read_response(response, MAX_IMAGE_BYTES)
        raise PhotoError("图片下载跳转次数过多，请稍后再试")

    async def _result(self, payload) -> tuple[bytes, str]:
        if isinstance(payload, bytes):
            data = payload
        else:
            values = payload.get("data") if isinstance(payload, dict) else None
            if (
                not isinstance(values, list)
                or not values
                or not isinstance(values[0], dict)
            ):
                raise PhotoError("生图服务没有返回有效图片，请检查服务记录")
            first = values[0]
            if first.get("b64_json"):
                data = _decode_base64(first["b64_json"])
            elif isinstance(first.get("url"), str) and first["url"]:
                data = await self._download(first["url"])
            else:
                raise PhotoError("生图服务没有返回有效图片，请检查服务记录")
        data, _, actual_size = await asyncio.to_thread(_verified_image, data)
        return data, actual_size

    async def _generate(
        self, prompt: str, references: list[bytes], size: str, key: str
    ) -> bytes:
        size_fallback_used, rate_retries = False, 0
        while True:
            try:
                payload = await self._request(prompt, references, size, key)
                data, actual_size = await self._result(payload)
                if actual_size != size:
                    log.warning(
                        "生图服务返回尺寸与请求不符：请求 %s，实际 %s",
                        size,
                        actual_size,
                    )
                return data
            except _HTTPError as exc:
                smaller = _SMALLER_SIZES.get(size)
                if exc.invalid_size and smaller and not size_fallback_used:
                    log.warning(
                        "生图服务不支持图片尺寸 %s，保持比例降到 %s 再试一次",
                        size,
                        smaller,
                    )
                    size, size_fallback_used = smaller, True
                    continue
                if exc.retryable and rate_retries < 2:
                    rate_retries += 1
                    delay = (
                        float(exc.retry_after)
                        if exc.retry_after
                        and re.fullmatch(r"\d+(?:\.\d+)?", exc.retry_after)
                        else rate_retries
                    )
                    await asyncio.sleep(min(delay, 30))
                    continue
                raise

    async def generate(self, prompt: str, references: list[bytes], size: str) -> bytes:
        # Caller-owned configuration and references have already been validated at entry.
        key = self.api_keys[self._key_index]
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        try:
            return await asyncio.wait_for(
                self._generate(prompt, references, size, key), REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise PhotoError(
                "等待生图服务超时，本次没有自动重试，请稍后查看服务记录"
            ) from None
        except httpx.TimeoutException:
            raise PhotoError(
                "等待生图服务超时，本次没有自动重试，请稍后查看服务记录"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise PhotoError(
                "连接图片服务中断，本次没有自动重试，请检查网络后查看服务记录"
            ) from None

    async def close(self) -> None:
        try:
            await self._api.aclose()
        finally:
            await self._downloads.aclose()
