"""私聊原生适配器的单次发送和交付确认。"""

from __future__ import annotations

import asyncio
import base64
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Record

from ...common.contracts import PROACTIVE_DELIVERY

if TYPE_CHECKING:
    from astrbot.api.event import MessageChain

_QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
_CONFIRMED_PRIVATE_PLATFORMS = _QQ_OFFICIAL_PLATFORMS | {"aiocqhttp"}
_NATIVE_EVENTS = {
    "aiocqhttp": ("aiocqhttp.aiocqhttp_message_event", "AiocqhttpMessageEvent"),
    "qq_official": ("qqofficial.qqofficial_message_event", "QQOfficialMessageEvent"),
    "qq_official_webhook": ("qqofficial.qqofficial_message_event", "QQOfficialMessageEvent"),
    "webchat": ("webchat.webchat_event", "WebChatMessageEvent"),
}


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


def supports_native_target(event: AstrMessageEvent) -> bool:
    """QQ 官方原生语音只支持 C2C 私聊。"""
    if event.get_platform_name() not in _QQ_OFFICIAL_PLATFORMS:
        return True
    if event.get_extra(PROACTIVE_DELIVERY) is True and getattr(event, "supports_voice", False):
        return True
    from ...photo.core.qq_official_delivery import can_send_qq_official_photo

    return can_send_qq_official_photo(event)


def uses_confirmed_transport(event: AstrMessageEvent) -> bool:
    return event.get_platform_name() in _CONFIRMED_PRIVATE_PLATFORMS or _native_platform(event) == "webchat"


def _prepare_failed(platform: str, exc: Exception) -> None:
    logger.warning("[自然语音] %s 发送准备失败（%s）", platform, type(exc).__name__)


async def _send_onebot(
    event: AstrMessageEvent,
    message: MessageChain,
    before_submit,
    allow_submit,
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
        response = await event.bot.send_private_msg(user_id=int(sender), message=messages, **routing)
    except (ActionFailed, ApiNotAvailable):
        return False
    message_id = response.get("message_id") if isinstance(response, dict) else None
    if isinstance(message_id, str) and message_id.removeprefix("-").isdecimal():
        message_id = int(message_id)
    return True if type(message_id) is int and message_id else None


async def _send_qq_official(
    event: AstrMessageEvent,
    message: MessageChain,
    allow_submit,
) -> bool | None:
    from ...photo.core.qq_official_delivery import send_qq_official_reply

    if len(message.chain) != 1:
        return False
    status = await send_qq_official_reply(event, message.chain[0], before_send=allow_submit)
    if status == "unknown":
        return None
    return status == "sent"


async def _send_webchat(
    event: AstrMessageEvent,
    message: MessageChain,
    before_submit,
    allow_submit,
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
            await asyncio.to_thread((Path(attachments_dir) / filename).write_bytes, base64.b64decode(encoded))
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
    event: AstrMessageEvent,
    message: MessageChain,
    *,
    before_submit: Callable[[], None],
    original_send=None,
    before_native_submit: Callable[[], Awaitable[bool]] | None = None,
) -> bool | None:
    """True 确认发出，False 明确未发，None 表示结果无法确认。"""
    if not event.is_private_chat():
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
        accepted = await _send_qq_official(event, message, allow_submit)
    elif platform == "webchat":
        accepted = await _send_webchat(event, message, before_submit, allow_submit)
    else:
        if event.get_platform_name() in _QQ_OFFICIAL_PLATFORMS:
            return False
        if not await allow_submit():
            return False
        send = event.send if original_send is None else original_send
        accepted = await send(message)
        return accepted if isinstance(accepted, bool) else None
    if accepted is True:
        await AstrMessageEvent.send(event, message)
    return accepted
