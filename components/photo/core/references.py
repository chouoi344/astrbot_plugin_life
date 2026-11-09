"""Read native files and event pictures into a task-owned immutable snapshot."""

import base64
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from astrbot.api.message_components import Image, Reply

from .provider import PhotoError, validate_image

MAX_BYTES = 50 * 1024 * 1024


def unique_references(
    images: list[bytes], roles: list[str]
) -> tuple[list[bytes], list[str]]:
    """Keep the first occurrence and its role when sources share image bytes."""
    seen = set()
    unique_images, unique_roles = [], []
    for image, role in zip(images, roles, strict=True):
        if image in seen:
            continue
        seen.add(image)
        unique_images.append(image)
        unique_roles.append(role)
    return unique_images, unique_roles


async def read_image(location):
    try:
        return await _read_image(location)
    except httpx.HTTPError:
        raise PhotoError("参考图片下载未完成，请重新发送图片") from None


async def _read_image(location):
    if location.startswith("base64://"):
        try:
            data = base64.b64decode(location[9:], validate=True)
        except ValueError:
            raise PhotoError("图片内容无效") from None
    elif location.startswith(("http://", "https://")):
        # This client never has a provider Authorization header.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            url = location
            for _ in range(6):
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.username
                    or parsed.password
                ):
                    raise PhotoError("图片地址无效")
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PhotoError("图片下载地址已失效")
                        url = str(response.url.join(location))
                        continue
                    if response.status_code != 200:
                        raise PhotoError("图片暂时下载不到，请重新发送")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise PhotoError("参考图片超过50MB")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise PhotoError("图片地址跳转次数过多")
    else:
        text = location
        if text.startswith("file://"):
            text = unquote(urlsplit(text).path)
            if len(text) > 3 and text[0] == "/" and text[2] == ":":
                text = text[1:]
        path = Path(text)
        if path.stat().st_size > MAX_BYTES:
            raise PhotoError("参考图片超过50MB")
        data = path.read_bytes()
    return validate_image(data)[0]


async def native_references(data_dir, values):
    root = (data_dir / "files").resolve()
    images = []
    for value in values:
        path = (data_dir / value.replace("\\", "/")).resolve()
        if not path.is_relative_to(root):
            raise PhotoError("参考人像路径无效，请重新上传")
        try:
            images.append(await read_image(str(path)))
        except OSError:
            raise PhotoError("参考人像文件找不到，请重新上传并保存") from None
    return images


def image_locations(chain):
    """Quoted pictures precede current pictures, matching the existing plugin."""
    quoted, current = [], []
    for component in chain:
        if isinstance(component, Reply):
            quoted.extend(image_locations(component.chain or []))
        elif isinstance(component, Image):
            location = component.url or component.file
            if location:
                current.append(location)
    return quoted + current


async def event_references(
    event,
    *,
    prefer_current: bool = False,
    current_only: bool = False,
    only_single: bool = False,
):
    from .message_images import collect_images

    images = await collect_images(
        event, prefer_current=prefer_current, current_only=current_only
    )
    locations = list(
        dict.fromkeys(
            image.url or image.file for image in images if image.url or image.file
        )
    )
    if only_single and len(locations) != 1:
        return []
    if len(locations) > 8:
        raise PhotoError("一次最多参考八张消息图片")
    return [await read_image(value) for value in locations]
