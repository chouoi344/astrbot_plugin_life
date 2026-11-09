"""Single-image delivery for the native AstrBot personal WeChat adapter."""

from __future__ import annotations

import asyncio


def can_send_weixin_photo(event) -> bool:
    if event.get_platform_name() != "weixin_oc":
        return False
    try:
        from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import (
            WeixinOCAdapter,
        )
        from astrbot.core.platform.sources.weixin_oc.weixin_oc_event import (
            WeixinOCMessageEvent,
        )
    except ImportError:
        return False

    if not isinstance(event, WeixinOCMessageEvent) or not isinstance(
        event.platform, WeixinOCAdapter
    ):
        return False
    platform = event.platform
    platform_id = platform.meta().id
    recipient = event.get_sender_id()
    return bool(
        event.is_private_chat()
        and not event.get_group_id()
        and event.get_platform_id() == event.get_self_id() == platform_id
        and recipient
        and event.session_id == recipient
        and platform.token
        and platform._context_tokens.get(recipient)
    )


async def send_weixin_photo(event, component, *, before_send) -> str:
    """Return the native SDK's acceptance outcome for one image."""
    from astrbot.api.message_components import Image

    if not isinstance(component, Image) or not can_send_weixin_photo(event):
        return "failed"
    platform = event.platform
    recipient = event.get_sender_id()
    account_id = platform.account_id
    try:
        path = await platform._resolve_media_file_path(component)
        if path is None:
            return "failed"
        item = await platform._prepare_media_item(
            recipient,
            path,
            platform.IMAGE_UPLOAD_TYPE,
            platform.IMAGE_ITEM_TYPE,
            path.name,
        )
    except Exception:
        return "failed"

    # The encrypted upload has not sent a message. Permission applies to the
    # final sendmessage call, including changes while the CDN upload was pending.
    await before_send()
    if (
        event.platform is not platform
        or event.get_sender_id() != recipient
        or platform.account_id != account_id
        or not can_send_weixin_photo(event)
    ):
        return "failed"
    try:
        accepted = await platform._send_items_to_session(
            recipient, [item], cache_components=[component]
        )
    except (Exception, asyncio.CancelledError):
        # The platform may already have accepted the final request.
        return "unknown"
    if accepted is True:
        return "sent"
    if accepted is False:
        return "failed"
    return "unknown"
