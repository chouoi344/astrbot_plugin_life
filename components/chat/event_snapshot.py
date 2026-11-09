"""忙碌时留存私聊消息和附件，供恢复会话使用。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
from collections.abc import Collection, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from astrbot.api import logger
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    Image,
    Plain,
    Record,
    Reply,
)
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_temp_path
from astrbot.core.utils.media_utils import (
    MEDIA_MIME_EXTENSIONS,
    detect_image_mime_type,
    file_uri_to_path,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_MEDIA_BYTES = 64 * 1024 * 1024
ATTACHMENT_TIMEOUT = 6.0
MESSAGE_MEDIA_TIMEOUT = 12.0
_MEDIA_NAME = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{1,8}")
_QQ_MEDIA_HOSTS = {
    "c2cpicdw.qpic.cn",
    "gchat.qpic.cn",
    "grouptalk.c2c.qq.com",
    "multimedia.nt.qq.com.cn",
    "multimedia.nt.qq.com",
}
_UNAVAILABLE = {"image": "[图片未能保留]", "record": "[语音未能保留]"}
_UNSUPPORTED = {
    "At": "[@提及]",
    "Face": "[QQ表情]",
    "File": "[文件]",
    "Forward": "[合并转发]",
    "Json": "[卡片]",
    "Video": "[视频]",
}


def _local_media_path(source: str, kind: str) -> Path:
    path = Path(file_uri_to_path(source))
    if not path.is_absolute() or path.anchor.startswith(("\\\\", "//")):
        raise ValueError("附件需要使用允许的本地路径")
    path = path.resolve()

    astrbot_media_roots = (
        Path(get_astrbot_temp_path()),
        Path(get_astrbot_data_path()) / "attachments",
    )
    if any(path.is_relative_to(root.resolve()) for root in astrbot_media_roots):
        return path

    napcat_roots = (
        Path("/app/.config/QQ"),
        Path.home() / ".config" / "QQ",
        Path.home() / "Documents" / "Tencent Files",
    )
    media_folder = "pic" if kind == "image" else "ptt"
    for root in napcat_roots:
        root = root.resolve()
        if path.is_relative_to(root):
            parts = [part.casefold() for part in path.relative_to(root).parts]
            if any(pair == ("nt_data", media_folder) for pair in zip(parts, parts[1:])):
                return path

    raise ValueError("附件位于允许的媒体目录之外")


def _read_local(path: Path, limit: int) -> bytes:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("附件不是常规文件或超过大小上限")
    with path.open("rb") as stream:
        return stream.read(limit + 1)


async def _download(source: str, limit: int) -> bytes:
    url = urlsplit(source)
    if (
        url.scheme != "https"
        or url.hostname not in _QQ_MEDIA_HOSTS
        or url.port not in (None, 443)
        or url.username is not None
    ):
        raise ValueError("附件不在允许的 QQ 下载地址范围内")

    async with aiohttp.ClientSession() as session:
        async with session.get(source, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("QQ 附件地址返回了非下载响应")
            if response.content_length is not None and response.content_length > limit:
                raise ValueError("附件超过大小上限")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(data) + len(chunk) > limit:
                    raise ValueError("附件超过大小上限")
                data.extend(chunk)
            return bytes(data)


async def _attachment_bytes(source: str, kind: str, limit: int) -> bytes:
    if source.startswith(("http://", "https://")):
        return await _download(source, limit)
    if source.startswith(("base64://", "data:")):
        if source.startswith("data:"):
            header, _, encoded = source.partition(",")
            mime_prefix = "data:image/" if kind == "image" else "data:audio/"
            if not header.startswith(mime_prefix) or not header.endswith(";base64"):
                raise ValueError("附件的数据类型无效")
        else:
            encoded = source.removeprefix("base64://")
        if len(encoded) > ((limit + 2) // 3) * 4:
            raise ValueError("附件超过大小上限")
        return base64.b64decode(encoded, validate=True)

    path = _local_media_path(source, kind)
    try:
        return await asyncio.to_thread(_read_local, path, limit)
    except FileNotFoundError:
        # NapCat 有时先给出语音路径，稍后才写入文件。
        if kind != "record":
            raise
        await asyncio.sleep(0.35)
        return await asyncio.to_thread(_read_local, path, limit)


def _attachment_suffix(data: bytes, kind: str) -> str:
    if kind == "image":
        mime = detect_image_mime_type(data, default_mime_type=None)
        if mime in MEDIA_MIME_EXTENSIONS:
            return MEDIA_MIME_EXTENSIONS[mime]
    else:
        if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
            return ".wav"
        if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
            return ".silk"
        if data.startswith(b"#!AMR"):
            return ".amr"
        if data.startswith(b"OggS"):
            return ".ogg"
        if data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
            return ".mp3"
        if data.startswith(b"fLaC"):
            return ".flac"
        if data[4:8] == b"ftyp" and data[8:12] in (b"M4A ", b"mp42", b"isom"):
            return ".m4a"
    raise ValueError("附件内容与消息类型不符")


def _plain(text: str) -> dict[str, str]:
    return {"kind": "plain", "text": text}


class _Capture:
    def __init__(self, media_dir: Path) -> None:
        self.media_dir = media_dir
        self.remaining = MAX_MESSAGE_MEDIA_BYTES
        self.deadline = asyncio.get_running_loop().time() + MESSAGE_MEDIA_TIMEOUT
        self.written: list[Path] = []

    async def components(
        self, chain: Iterable[BaseMessageComponent]
    ) -> list[dict[str, Any]]:
        saved = []
        for part in chain:
            if isinstance(part, Plain):
                saved.append(_plain(part.text))
            elif isinstance(part, Reply):
                saved.append(
                    {
                        "kind": "reply",
                        "id": str(part.id),
                        "sender_id": part.sender_id,
                        "sender_nickname": part.sender_nickname,
                        "time": part.time,
                        "message_str": part.message_str,
                        "text": part.text,
                        "chain": await self.components(part.chain or ()),
                    }
                )
            elif isinstance(part, (Image, Record)):
                saved.append(await self.attachment(part))
            else:
                saved.append(_plain(_UNSUPPORTED.get(part.type.value, "[其他消息]")))
        return saved

    async def attachment(self, part: Image | Record) -> dict[str, Any]:
        kind = "image" if isinstance(part, Image) else "record"
        loop = asyncio.get_running_loop()
        deadline = min(self.deadline, loop.time() + ATTACHMENT_TIMEOUT)
        limit = min(self.remaining, MAX_ATTACHMENT_BYTES)
        sources = (
            (part.url, part.path, part.file)
            if kind == "image"
            else (part.path, part.url, part.file)
        )
        last_error = "来源为空"
        for source in dict.fromkeys(sources):
            if not source:
                continue
            if limit <= 0 or loop.time() >= deadline:
                last_error = "已达大小或时间上限"
                break
            try:
                data = await asyncio.wait_for(
                    _attachment_bytes(source, kind, limit),
                    timeout=deadline - loop.time(),
                )
                if len(data) > limit:
                    raise ValueError("附件超过大小上限")
                suffix = _attachment_suffix(data, kind)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                ValueError,
            ) as error:
                last_error = type(error).__name__
                continue

            path = self.media_dir / f"{uuid4().hex}{suffix}"
            created = False
            try:
                self.media_dir.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    created = True
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                last_error = type(error).__name__
                if created:
                    _discard_file(path)
                break
            self.written.append(path)
            self.remaining -= len(data)
            saved = {"kind": kind, "name": path.name}
            if isinstance(part, Record):
                saved["text"] = part.text
            return saved

        logger.warning("[忙碌状态] 未能保留 %s 附件（%s）", kind, last_error)
        return _plain(_UNAVAILABLE[kind])


def _discard_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("[忙碌状态] 未能清理附件临时副本（%s）", type(error).__name__)


async def capture(
    event: AstrMessageEvent,
    media_dir: Path,
    *,
    routing: dict | None = None,
    memory_epoch: int | None = None,
) -> str:
    """在原始事件结束前生成快照，附件只记录插件目录内的文件名。"""
    message = event.message_obj
    snapshot = _Capture(media_dir)
    complete = False
    try:
        chain = event.get_messages()
        if event.get_platform_name() in {"qq_official", "qq_official_webhook"}:
            # The official C2C adapter adds a synthetic At to every inbound DM.
            chain = [
                part
                for part in chain
                if not (isinstance(part, At) and part.qq == "qq_official")
            ]
        components = await snapshot.components(chain)
        raw = getattr(message, "raw_message", None)
        sent_at = raw.get("time") if isinstance(raw, dict) else None
        payload = json.dumps(
            {
                "message_id": str(message.message_id),
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
                "session_id": event.get_session_id(),
                "self_id": event.get_self_id(),
                "timestamp": sent_at if isinstance(sent_at, int) else message.timestamp,
                "message_str": event.get_message_str(),
                "components": components,
                "routing": routing,
                "memory_epoch": memory_epoch,
            },
            ensure_ascii=False,
        )
        complete = True
        return payload
    finally:
        if not complete:
            for path in snapshot.written:
                _discard_file(path)


def _media_name(name: str) -> str:
    if not _MEDIA_NAME.fullmatch(name):
        raise ValueError("快照内的附件文件名无效")
    return name


def _media_names(chain: Iterable[dict[str, Any]]) -> Iterator[str]:
    for part in chain:
        if part["kind"] in ("image", "record"):
            yield _media_name(part["name"])
        elif part["kind"] == "reply":
            yield from _media_names(part["chain"])


def snapshot_media_names(payload: str) -> tuple[str, ...]:
    """列出可随队列记录一起清理的附件文件名。"""
    return tuple(_media_names(json.loads(payload)["components"]))


def orphan_media_names(media_dir: Path, retained: Collection[str]) -> tuple[str, ...]:
    return tuple(
        path.name
        for path in media_dir.iterdir()
        if _MEDIA_NAME.fullmatch(path.name) and path.name not in retained
    )


def _restore_chain(
    chain: Iterable[dict[str, Any]],
    media_dir: Path,
    recalled_ids: Collection[str],
) -> list[BaseMessageComponent]:
    restored: list[BaseMessageComponent] = []
    for part in chain:
        kind = part["kind"]
        if kind == "plain":
            restored.append(Plain(part["text"]))
        elif kind == "reply":
            if part["id"] in recalled_ids:
                # 不能保留 Reply，否则 AstrBot 可能再按 ID 读取撤回内容。
                restored.append(Plain("[引用消息已撤回]"))
                continue
            fields = {
                key: value
                for key, value in part.items()
                if key not in ("kind", "chain")
            }
            restored.append(
                Reply(
                    **fields,
                    chain=_restore_chain(part["chain"], media_dir, recalled_ids),
                )
            )
        elif kind in ("image", "record"):
            path = media_dir / _media_name(part["name"])
            if path.is_symlink() or not path.is_file():
                restored.append(Plain(_UNAVAILABLE[kind]))
            elif kind == "image":
                restored.append(Image.fromFileSystem(path))
            else:
                restored.append(Record.fromFileSystem(path, text=part["text"]))
        else:
            raise ValueError(f"未知的快照消息段类型：{kind}")
    return restored


def restore(
    payload: str,
    media_dir: Path,
    *,
    recalled_ids: Collection[str] = (),
) -> AstrBotMessage:
    """恢复私聊消息，由适配器创建真正的会话事件。"""
    snapshot = json.loads(payload)
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.message_id = snapshot["message_id"]
    message.self_id = snapshot["self_id"]
    message.session_id = snapshot["session_id"]
    message.timestamp = snapshot["timestamp"]
    message.sender = MessageMember(snapshot["sender_id"], snapshot["sender_name"])
    message.raw_message = None
    message.message = _restore_chain(snapshot["components"], media_dir, recalled_ids)
    message.message_str = (
        "".join(part.text for part in message.message if isinstance(part, Plain))
        or snapshot["message_str"]
    )
    return message
