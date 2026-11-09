"""私聊原生适配器的单次发送和交付确认。"""

from __future__ import annotations

import asyncio
import base64
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Record

if TYPE_CHECKING:
    from astrbot.api.event import MessageChain

TEXT_ONLY_PLATFORM = "weixin_oc"
_QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
_CONFIRMED_PRIVATE_PLATFORMS = _QQ_OFFICIAL_PLATFORMS | {"aiocqhttp"}
_NATIVE_EVENTS = {
    "aiocqhttp": ("aiocqhttp.aiocqhttp_message_event", "AiocqhttpMessageEvent"),
    "qq_official": ("qqofficial.qqofficial_message_event", "QQOfficialMessageEvent"),
    "qq_official_webhook": ("qqofficial.qqofficial_message_event", "QQOfficialMessageEvent"),
    "webchat": ("webchat.webchat_event", "WebChatMessageEvent"),
}
_QQ_REJECTED_STATUSES = frozenset({401, 403, 404, 405, 413, 415, 422, 429})
_QQ_REJECTED_CODES = frozenset({40034100, 40034105, 40054004, 40054013})


class SendCancelled(Exception):
    """插件在提交消息前关闭，明确没有发送。"""


def _native_platform(event: AstrMessageEvent, original_send=None) -> str:
    get_platform_name = getattr(event, "get_platform_name", None)
    platform = get_platform_name() if get_platform_name else ""
    if platform not in _NATIVE_EVENTS:
        return ""
    module_name, class_name = _NATIVE_EVENTS[platform]
    # 原生类创建事件时已经加载。只识别原方法，保留其他插件挂在事件上的发送包装。
    module = sys.modules.get(f"astrbot.core.platform.sources.{module_name}")
    native_class = getattr(module, class_name, None)
    send = event.send if original_send is None else original_send
    if native_class and getattr(send, "__func__", None) is native_class.send:
        return platform
    return ""


def _qq_openid(event: AstrMessageEvent) -> str:
    from botpy.message import C2CMessage

    raw = event.message_obj.raw_message
    if isinstance(raw, C2CMessage):
        return raw.author.user_openid or ""
    return ""


def supports_native_target(event: AstrMessageEvent) -> bool:
    """QQ 官方原生语音只支持 C2C 私聊。"""
    if _native_platform(event) in _QQ_OFFICIAL_PLATFORMS:
        return bool(_qq_openid(event) and event.message_obj.message_id)
    return True


def uses_confirmed_transport(event: AstrMessageEvent) -> bool:
    return (
        event.get_platform_name() in _CONFIRMED_PRIVATE_PLATFORMS
        or _native_platform(event) == "webchat"
    )


def _prepare_failed(platform: str, exc: Exception) -> None:
    logger.warning("[自然语音] %s 发送准备失败（%s）", platform, type(exc).__name__)


def _qq_send_result(status: int, body: object) -> bool | None:
    if status == 408 or not (200 <= status < 300 or 400 <= status < 500):
        return None
    if not isinstance(body, dict):
        return None
    rejection = False
    for name in ("code", "err_code"):
        code = body.get(name, 0)
        if isinstance(code, str) and code.isascii() and code.isdecimal():
            code = int(code)
        if type(code) is not int:
            return None
        if code:
            # 去重（40054005）及未知业务错误无法判断原消息是否送达。
            if code not in _QQ_REJECTED_CODES:
                return None
            rejection = True
    if rejection or status in _QQ_REJECTED_STATUSES:
        return False
    message_id = body.get("id")
    if 200 <= status < 300 and isinstance(message_id, str) and message_id.strip():
        return True
    return None


async def _send_onebot(
    event: AstrMessageEvent, message: MessageChain, before_submit, allow_submit,
) -> bool | None:
    from aiocqhttp import Event
    from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable

    sender = event.get_sender_id()
    if not sender.isdecimal():
        return False
    raw = event.message_obj.raw_message
    routing = {"self_id": raw["self_id"]} if isinstance(raw, Event) and raw.get("self_id") else {}
    try:
        messages = await event._parse_onebot_json(message)
    except asyncio.CancelledError:
        before_submit()
        raise
    except Exception as exc:
        _prepare_failed("aiocqhttp", exc)
        return False
    if not messages:
        return False
    if not await allow_submit():
        return False
    try:
        response = await event.bot.send_private_msg(
            user_id=int(sender), message=messages, **routing
        )
    except (ActionFailed, ApiNotAvailable):
        return False
    message_id = response.get("message_id") if isinstance(response, dict) else None
    if isinstance(message_id, str) and message_id.removeprefix("-").isdecimal():
        message_id = int(message_id)
    return True if type(message_id) is int and message_id else None


async def _send_qq_official(
    event: AstrMessageEvent, message: MessageChain, before_submit, allow_submit,
) -> bool | None:
    from aiohttp import ClientTimeout
    from botpy.http import Route

    openid = _qq_openid(event)
    if not openid or not event.message_obj.message_id:
        return False
    if len(message.chain) != 1:
        return False
    segment = message.chain[0]
    payload = {"msg_id": event.message_obj.message_id, "msg_seq": random.randint(1, 10000)}
    try:
        if isinstance(segment, Record):
            from astrbot.core.utils.media_utils import MediaResolver

            source = segment.url or segment.file
            if not source:
                return False
            resolver = MediaResolver(source, media_type="audio", default_suffix=".wav")
            async with resolver.as_path(target_format="tencent_silk") as resolved:
                before_submit()
                media = await event.upload_group_and_c2c_media(
                    str(resolved.path), 3, openid=openid, srv_send_msg=False
                )
            file_info = media.get("file_info") if isinstance(media, dict) else None
            if not isinstance(file_info, str) or not file_info.strip():
                return False
            payload.update(msg_type=7, media=media)
        elif isinstance(segment, Plain) and segment.text.strip():
            payload.update(msg_type=0, content=segment.text)
        else:
            return False

        http = event.bot.api._http
        await http.check_session()
    except asyncio.CancelledError:
        before_submit()
        raise
    except SendCancelled:
        raise
    except Exception as exc:
        _prepare_failed("qq_official", exc)
        return False

    # SDK 的高层方法会丢失回执或重试。复用已认证会话，只请求一次消息接口。
    route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
    route.is_sandbox = http.is_sandbox
    if not await allow_submit():
        return False
    async with http._session.request(
        method=route.method,
        url=route.url,
        headers=http._headers,
        json=payload,
        timeout=ClientTimeout(total=http.timeout),
        allow_redirects=False,
    ) as response:
        try:
            result = await response.json(content_type=None)
        except ValueError:
            return None
        return _qq_send_result(response.status, result)


async def _send_webchat(
    event: AstrMessageEvent, message: MessageChain, before_submit, allow_submit,
) -> bool:
    from astrbot.core.platform.sources.webchat.webchat_event import attachments_dir
    from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr

    message_id = event.message_obj.message_id
    request_id = str(message_id)
    queue = webchat_queue_mgr.back_queues.get(request_id)
    if queue is None or queue.full() or len(message.chain) != 1:
        return False
    segment = message.chain[0]
    payload = {"streaming": False, "message_id": message_id}
    if isinstance(segment, Record):
        filename = f"{uuid4()}.wav"
        try:
            encoded = await segment.convert_to_base64()
            before_submit()
            await asyncio.to_thread(
                (Path(attachments_dir) / filename).write_bytes, base64.b64decode(encoded)
            )
        except asyncio.CancelledError:
            before_submit()
            raise
        except SendCancelled:
            raise
        except Exception as exc:
            _prepare_failed("webchat", exc)
            return False
        payload.update(type="record", data=f"[RECORD]{filename}")
    elif isinstance(segment, Plain):
        payload.update(type="plain", data=segment.text, chain_type=message.type)
    else:
        return False

    # 直接用当前请求队列。等待满队列的独立任务可能在关闭后才入队。
    if not await allow_submit():
        return False
    if webchat_queue_mgr.back_queues.get(request_id) is not queue or queue.full():
        return False
    queue.put_nowait(payload)
    return True


async def send_native_private(
    event: AstrMessageEvent, message: MessageChain, *, before_submit: Callable[[], None],
    original_send=None,
    before_native_submit: Callable[[], Awaitable[bool]] | None = None,
) -> bool | None:
    """True 确认发出，False 明确未发，None 表示结果无法确认。"""
    if not event.is_private_chat() or event.get_platform_name() == TEXT_ONLY_PLATFORM:
        return False
    before_submit()

    async def allow_submit():
        if before_native_submit is not None:
            before_submit()
            if not await before_native_submit():
                return False
        before_submit()
        return True

    platform = _native_platform(event, original_send)
    if platform == "aiocqhttp":
        accepted = await _send_onebot(event, message, before_submit, allow_submit)
    elif platform in _QQ_OFFICIAL_PLATFORMS:
        accepted = await _send_qq_official(event, message, before_submit, allow_submit)
    elif platform == "webchat":
        accepted = await _send_webchat(event, message, before_submit, allow_submit)
    else:
        if not await allow_submit():
            return False
        send = event.send if original_send is None else original_send
        accepted = await send(message)
        return accepted if isinstance(accepted, bool) else None
    if accepted is True:
        await AstrMessageEvent.send(event, message)
    return accepted
