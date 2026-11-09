"""Send photos and resume the original conversation without retaining its event."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiohttp import ClientConnectionError
from httpx import TransportError
from PIL import Image as PillowImage
from PIL import ImageOps

from .qq_official_delivery import can_send_qq_official_photo, send_qq_official_photo

log = logging.getLogger("astrbot")

SEND_TIMEOUT = 60
SEND_MAX_DIMENSION = 4096
SEND_MAX_BYTES = 10 * 1024 * 1024
FILE_THRESHOLD = 20 * 1024 * 1024
MAX_DECODE_PIXELS = 64_000_000
QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
CONFIRMED_IMAGE_PLATFORMS = QQ_OFFICIAL_PLATFORMS
PHOTO_ACK_CAPABILITY = "life_photo_ack_v1"


class DeliveryUnknown(RuntimeError):
    """The adapter may already have sent the image; retrying could duplicate it."""


class DeliveryCancelled(RuntimeError):
    """A send guard stopped the operation before its platform call."""


class DeliveryRejected(RuntimeError):
    """The caller's platform bridge confirmed that this attempt did not send."""


@dataclass(frozen=True)
class DeliveryTarget:
    platform_id: str
    name: str
    umo: str
    session_id: str
    type: str
    self_id: str
    sender_id: str
    sender_name: str
    group_id: str
    conversation_id: str | None
    source_message_id: str


async def capture_target(context, event) -> DeliveryTarget:
    """Keep identifiers only, so a queued task owns no user image or event."""
    original = event.message_obj
    conversation_id = await asyncio.wait_for(
        context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin),
        SEND_TIMEOUT,
    )
    return DeliveryTarget(
        platform_id=str(event.get_platform_id()),
        name=str(event.get_platform_name()),
        umo=str(event.unified_msg_origin),
        session_id=str(event.session_id),
        type=str(original.type.value),
        self_id=str(original.self_id),
        sender_id=str(original.sender.user_id),
        sender_name=original.sender.nickname or "",
        group_id=str(original.group_id or ""),
        conversation_id=conversation_id,
        source_message_id=str(original.message_id),
    )


async def rebuild_event(context, target: DeliveryTarget):
    """Build an empty incoming chain on the captured adapter and session."""
    from astrbot.api.star import StarTools
    from astrbot.core.platform.astrbot_message import MessageMember

    adapter = context.get_platform_inst(target.platform_id)
    if adapter is None:
        raise RuntimeError("发送照片的聊天平台已离线")
    message = await StarTools.create_message(
        type=target.type,
        self_id=target.self_id,
        session_id=target.session_id,
        sender=MessageMember(user_id=target.sender_id, nickname=target.sender_name),
        message=[],
        message_str="",
        group_id=target.group_id,
    )
    event = adapter.create_event(message)
    if event.unified_msg_origin != target.umo:
        raise RuntimeError("照片发送会话与原会话不一致")
    return event


def _prepare_jpeg(path: Path) -> bytes | None:
    """Work in memory: a cancelled to_thread call can never leave a late file."""
    with PillowImage.open(path) as source:
        if (
            max(source.size) <= SEND_MAX_DIMENSION
            and path.stat().st_size <= SEND_MAX_BYTES
        ):
            return None
        if source.width * source.height > MAX_DECODE_PIXELS:
            raise ValueError("照片尺寸过大，改用原文件发送")
        source.load()
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
        image = PillowImage.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
        image.thumbnail(
            (SEND_MAX_DIMENSION, SEND_MAX_DIMENSION), PillowImage.Resampling.LANCZOS
        )
        while True:
            output = io.BytesIO()
            image.save(output, "JPEG", quality=95, subsampling=0)
            data = output.getvalue()
            if len(data) <= SEND_MAX_BYTES:
                return data
            scale = min(0.9, math.sqrt(SEND_MAX_BYTES / len(data)) * 0.95)
            size = max(1, int(image.width * scale)), max(1, int(image.height * scale))
            if size == image.size:
                raise ValueError("照片压缩后仍然过大，改用原文件发送")
            image = image.resize(size, PillowImage.Resampling.LANCZOS)


def _is_unknown(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            TimeoutError,
            asyncio.TimeoutError,
            ConnectionError,
            TransportError,
            ClientConnectionError,
        ),
    )


def _wrapped_photo_event(event) -> bool:
    return (
        event.get_extra(PHOTO_ACK_CAPABILITY) is True
        or event.get_extra("life_delivery_v1") is True
        or event.get_extra("life_busy_replay_v1") is not None
        or event.get_extra("life_busy_media_guard_v1") is not None
    )


def can_send_photo(event) -> bool:
    if not event.is_private_chat():
        return False
    platform = event.get_platform_name()
    if platform in {"aiocqhttp", "webchat"}:
        return True
    if platform not in CONFIRMED_IMAGE_PLATFORMS:
        return False
    if _wrapped_photo_event(event):
        return (
            event.get_extra(PHOTO_ACK_CAPABILITY) is True
            and bool(event.get_sender_id())
            and event.session_id == event.get_sender_id()
            and callable(event.send)
        )
    return can_send_qq_official_photo(event)


def can_send_proactive_photo(event) -> bool:
    platform = event.get_platform_name()
    if platform == "aiocqhttp":
        return (
            event.get_sender_id().isdigit()
            and event.session_id == event.get_sender_id()
            and str(event.message_obj.self_id).isdigit()
            and not event.get_group_id()
            and callable(getattr(getattr(event, "bot", None), "send_private_msg", None))
        )
    if platform == "webchat":
        from astrbot.core.platform.sources.webchat.webchat_event import (
            WebChatMessageEvent,
            webchat_queue_mgr,
        )

        active_requests = getattr(webchat_queue_mgr, "list_back_request_ids", None)
        session = event.session_id.split("!", 2)
        conversation = (
            session[2]
            if len(session) == 3 and session[0] == "webchat"
            else event.session_id
        )
        request_id = str(event.message_obj.message_id)
        return (
            isinstance(event, WebChatMessageEvent)
            and callable(active_requests)
            and request_id.startswith("ws_sub_")
            and request_id in active_requests(conversation)
        )
    return (
        platform in CONFIRMED_IMAGE_PLATFORMS
        and event.get_extra("life_delivery_v1") is True
        and can_send_photo(event)
    )


async def _send_component(event, component, *, before_send=None, proactive=False):
    started = False
    platform = event.get_platform_name()
    if platform == "weixin_oc":
        raise DeliveryRejected("当前微信会话请用普通文字回复。")
    strict_platform = platform in CONFIRMED_IMAGE_PLATFORMS

    async def begin():
        nonlocal started
        if before_send is not None and await before_send() is False:
            raise DeliveryCancelled("发送许可已撤回，本次照片已停止。")
        started = True

    async def send():
        if strict_platform:
            if not can_send_photo(event):
                raise DeliveryRejected("当前私聊没有可确认的照片发送目标。")
            if _wrapped_photo_event(event):
                message = event.chain_result([component])
                await begin()
                accepted = await event.send(message)
                status = getattr(event, "delivery_status", None)
                if accepted is True:
                    return
                if accepted is False and status == "cancelled":
                    raise DeliveryCancelled("发送许可已撤回，本次照片已停止。")
                if accepted is False and status != "unknown":
                    raise DeliveryRejected("平台明确拒绝发送本次照片。")
                raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复")

            status = await send_qq_official_photo(event, component, before_send=begin)
            if status == "sent":
                return
            if status == "failed":
                raise DeliveryRejected("平台未接受本次照片。")
            raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复")

        if proactive and platform == "aiocqhttp":
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            segment = await AiocqhttpMessageEvent._from_segment_to_dict(component)
            await begin()
            receipt = await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                self_id=int(event.message_obj.self_id),
                message=[segment],
            )
            message_id = (
                receipt.get("message_id") if isinstance(receipt, dict) else None
            )
            if type(message_id) not in (int, str) or not message_id:
                raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复")
            return

        message = event.chain_result([component])
        if before_send is not None:
            await begin()
        if platform == "webchat":
            from astrbot.api.event import AstrMessageEvent, MessageChain
            from astrbot.core.platform.sources.webchat.webchat_event import (
                WebChatMessageEvent,
                webchat_queue_mgr,
            )

            if isinstance(event, WebChatMessageEvent):
                request_id = str(event.message_obj.message_id)
                legacy = proactive and not callable(
                    getattr(webchat_queue_mgr, "put_back_queue", None)
                )
                if legacy:
                    original_queue = webchat_queue_mgr.back_queues.get(request_id)
                    if original_queue is None or not can_send_proactive_photo(event):
                        raise DeliveryCancelled("WebChat 已断开，本次照片已停止。")
                # Both supported SDKs expose this nonterminal queue operation.
                # event.send flushes the photo before its tool result arrives;
                # send_streaming also emits complete when its generator ends.
                queued = await WebChatMessageEvent._send(
                    message_id=event.message_obj.message_id,
                    message=message,
                    session_id=event.session_id,
                    streaming=True,
                    emit_complete=False,
                )
                # Older SDKs return data even after putting it into a detached
                # queue. Only the same, still subscribed queue can confirm this send.
                if queued is None or (
                    legacy
                    and (
                        webchat_queue_mgr.back_queues.get(request_id) is not original_queue
                        or not can_send_proactive_photo(event)
                    )
                ):
                    raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复")
                if not proactive:
                    await AstrMessageEvent.send(event, MessageChain([]))
                return
        await event.send(message)

    try:
        await asyncio.wait_for(send(), SEND_TIMEOUT)
    except asyncio.CancelledError:
        if started:
            raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复") from None
        raise
    except (DeliveryCancelled, DeliveryUnknown, DeliveryRejected):
        raise
    except Exception as exc:
        if strict_platform and not started:
            raise DeliveryRejected("准备发送的图片失败。") from None
        if before_send is not None and not started and isinstance(exc, TimeoutError):
            raise DeliveryCancelled("发送检查超时，本次照片已停止。") from None
        onebot_network_error = False
        rejected = False
        if platform == "aiocqhttp":
            from aiocqhttp.exceptions import ActionFailed, NetworkError

            onebot_network_error = isinstance(exc, NetworkError)
            rejected = isinstance(exc, ActionFailed)
        if _is_unknown(exc) or onebot_network_error:
            raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复") from exc
        bridge_status = getattr(event, "delivery_status", None)
        wrapped_status = (
            strict_platform
            and bridge_status in ("cancelled", "failed")
            and _wrapped_photo_event(event)
        )
        onebot_status = (
            proactive
            and platform == "aiocqhttp"
            and bridge_status in ("stopped", "cancelled", "failed")
            and event.get_extra("life_delivery_v1") is True
        )
        if started and (wrapped_status or onebot_status):
            if bridge_status in ("stopped", "cancelled"):
                raise DeliveryCancelled("发送许可已撤回，本次照片已停止。") from None
            raise DeliveryRejected("平台明确拒绝发送本次照片。") from None
        if strict_platform and started:
            raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复") from exc
        if before_send is not None and started and not rejected:
            if not proactive:
                await before_send()
            raise DeliveryUnknown("照片发送结果未确认，已停止重发以免重复") from exc
        raise


async def _send_image(event, path: Path, **send_options) -> None:
    from astrbot.api.message_components import File, Image

    if event.get_platform_name() in CONFIRMED_IMAGE_PLATFORMS:
        await _send_component(event, Image.fromFileSystem(str(path)), **send_options)
        return

    try:
        await _send_component(event, Image.fromFileSystem(str(path)), **send_options)
        return
    except (DeliveryUnknown, DeliveryCancelled, DeliveryRejected):
        raise
    except Exception as exc:
        log.warning("本地照片发送被拒绝，改用图片内容（%s）", type(exc).__name__)
    try:
        await _send_component(event, Image.fromBytes(path.read_bytes()), **send_options)
        return
    except (DeliveryUnknown, DeliveryCancelled, DeliveryRejected):
        raise
    except Exception as exc:
        log.warning("图片内容发送被拒绝，改用原文件（%s）", type(exc).__name__)
    await _send_component(event, File(name=path.name, file=str(path)), **send_options)


async def send_photo(event, path: Path, *, before_send=None, proactive=False):
    """Send once confirmed, retry only definite failures, and keep the original."""
    from astrbot.api.message_components import File

    send_options = (
        {"before_send": before_send, "proactive": proactive}
        if before_send is not None
        else {}
    )
    image_only = event.get_platform_name() in CONFIRMED_IMAGE_PLATFORMS
    if not image_only and path.stat().st_size > FILE_THRESHOLD:
        await _send_component(
            event, File(name=path.name, file=str(path)), **send_options
        )
        return
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_prepare_jpeg, path), SEND_TIMEOUT
        )
    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as exc:
        if image_only:
            raise DeliveryRejected("照片暂时无法转换成可发送的图片。") from None
        log.warning("照片发送副本准备失败，改用原文件：%s", exc)
        await _send_component(
            event, File(name=path.name, file=str(path)), **send_options
        )
        return
    if data is None:
        await _send_image(event, path, **send_options)
        return
    prepared = path.with_name(f"{path.stem}-send-{uuid.uuid4().hex}.jpg")
    try:
        prepared.write_bytes(data)
        await _send_image(event, prepared, **send_options)
    finally:
        prepared.unlink(missing_ok=True)


CAPTION_GUIDANCE = (
    "按本次实际操作自然配话。resend是重发原图；新拍、新画或刚改好的图，别说成翻找出来的旧照。"
    "拍摄计划描述的是画面，不能证明以前拍过、做过这道菜或到过现场。沿用聊天或日程明确提供的事实。"
    "没看过新成片时，把拍法当作计划，别断言成片已经换手、换姿势或实现了某个画面变化。"
)


async def dispatch_completion(context, target: DeliveryTarget, facts: dict) -> bool:
    """Queue one tool-free persona reply only while the original chat is current."""
    from astrbot.core.agent.tool import ToolSet

    manager = context.conversation_manager
    current_id = await asyncio.wait_for(
        manager.get_curr_conversation_id(target.umo), SEND_TIMEOUT
    )
    if not target.conversation_id or current_id != target.conversation_id:
        log.info("照片配话已跳过：原对话已切换或丢失（%s）", target.umo)
        return False
    conversation = await asyncio.wait_for(
        manager.get_conversation(target.umo, target.conversation_id), SEND_TIMEOUT
    )
    if conversation is None:
        log.info("照片配话已跳过：原对话已删除（%s）", target.umo)
        return False
    event = await rebuild_event(context, target)
    current_id = await asyncio.wait_for(
        manager.get_curr_conversation_id(target.umo), SEND_TIMEOUT
    )
    if current_id != target.conversation_id:
        log.info("照片配话已跳过：准备期间切换了对话（%s）", target.umo)
        return False
    completion_facts = {
        "sent_count": facts.get("sent_count", 0),
        "partial": facts.get("partial", False),
        "status": facts.get("status", "failed"),
        "action": facts.get("action"),
        "mode": facts.get("mode"),
    }
    instructions = (
        "这是原先拍照请求的完成通知。继续当前人设，只说一句自然的话。"
        "以下是唯一已确认的结果："
        + json.dumps(completion_facts, ensure_ascii=False)
        + "。"
        "sent_count 是实际成功发出的照片数量；partial 表示只有部分成功。"
        "只有 sent_count 大于 0 才能说照片已发出。"
        "status 为 need_user_image 时请对方提供照片；need_character_image 时说明暂时没拍好。"
        "发送结果不明确时，不声称成功或失败，也不要要求重发。"
        "你没有看过成图，不要评价照片画面、质量或声称某张糊了。"
        "只根据这些结果和原请求回应，不补充天气、地点、衣着等未确认事实。"
        f"{CAPTION_GUIDANCE}"
        "不提生成、处理、AI、接口或任务，不再调用工具，也不重新发图。"
    )
    request = event.request_llm(
        prompt=str(facts.get("user_prompt", "")),
        system_prompt=instructions,
        tool_set=ToolSet(tools=[]),
        conversation=conversation,
    )
    event.is_wake = True
    event.set_extra("life_photo_completion", True)
    event.set_extra("provider_request", request)
    context.get_platform_inst(target.platform_id).commit_event(event)
    return True
