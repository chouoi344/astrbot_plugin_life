"""Confirm one passive photo reply on AstrBot's QQ Official C2C transport."""

from __future__ import annotations

import random
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

_REJECTION_STATUSES = {400, 401, 403, 404, 405, 413, 415, 422, 429}
_REJECTION_CODES = {
    "22009",
    "304082",
    "304083",
    "40034100",
    "40034105",
    "40054004",
    "40054013",
}


def _reply_target(event):
    platform = event.get_platform_name()
    if platform not in ("qq_official", "qq_official_webhook"):
        return None

    from botpy import BotAPI, Client
    from botpy.http import BotHttp
    from botpy.message import C2CMessage

    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    event_type = QQOfficialMessageEvent
    if platform == "qq_official_webhook":
        try:
            from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import (
                QQOfficialWebhookMessageEvent,
            )
        except ImportError:
            return None
        event_type = QQOfficialWebhookMessageEvent
    if type(event) is not event_type:
        return None
    if not event.is_private_chat() or event.get_group_id():
        return None

    message = event.message_obj
    raw = message.raw_message
    if not isinstance(raw, C2CMessage) or not isinstance(event.bot, Client):
        return None
    api = event.bot.api
    if raw._api is not api or not isinstance(api, BotAPI):
        return None
    if not isinstance(api._http, BotHttp):
        return None
    openid = raw.author.user_openid
    if not openid or openid != event.session_id or openid != event.get_sender_id():
        return None
    if (
        not isinstance(raw.id, str)
        or not raw.id.strip()
        or raw.id != message.message_id
    ):
        return None

    return openid, raw.id, api._http


def can_send_qq_official_photo(event) -> bool:
    return _reply_target(event) is not None


async def send_qq_official_photo(event, component, *, before_send) -> str:
    from botpy.http import Route

    from astrbot.api.message_components import Image
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot.core.utils.media_utils import MediaResolver

    target = _reply_target(event)
    if target is None or not isinstance(component, Image) or not component.file:
        return "failed"
    if component.file.startswith(("http://", "https://")):
        return "failed"

    openid, message_id, http = target
    parameters = {"is_sandbox": http.is_sandbox, "openid": quote(openid, safe="")}
    upload_route = Route("POST", "/v2/users/{openid}/files", **parameters)
    send_route = Route("POST", "/v2/users/{openid}/messages", **parameters)
    timeout = ClientTimeout(total=min(http.timeout, 20))
    try:
        image_base64 = await MediaResolver(
            component.file, media_type="image"
        ).to_base64()
        await http.check_session()
        session, headers = http._session, http._headers
        if session is None or not headers:
            return "failed"
        # The SDK's upload helper retries and can log the entire image on failure.
        async with session.request(
            upload_route.method,
            upload_route.url,
            headers=headers,
            json={
                "openid": openid,
                "file_data": image_base64,
                "file_type": QQOfficialMessageEvent.IMAGE_FILE_TYPE,
                "srv_send_msg": False,
            },
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            if not 200 <= response.status < 300:
                return "failed"
            upload = await response.json(content_type=None)
    except (ClientError, OSError, RuntimeError, ValueError):
        return "failed"
    if not isinstance(upload, dict):
        return "failed"
    file_info, file_uuid = upload.get("file_info"), upload.get("file_uuid")
    if not all(isinstance(value, str) and value for value in (file_info, file_uuid)):
        return "failed"

    payload = {
        "openid": openid,
        "content": " ",
        "msg_type": 7,
        "media": {
            "file_info": file_info,
            "file_uuid": file_uuid,
            "ttl": upload.get("ttl", 0),
        },
        "msg_id": message_id,
        "msg_seq": random.randint(1, 10000),
    }
    await before_send()
    if _reply_target(event) != target:
        return "failed"

    # Both higher-level SDK send functions can retry a request with a lost reply.
    try:
        async with session.request(
            send_route.method,
            send_route.url,
            headers=headers,
            json=payload,
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            receipt = await response.json(content_type=None)
            status = response.status
    except (ClientError, OSError, RuntimeError, ValueError):
        return "unknown"
    if not isinstance(receipt, dict):
        return "unknown"

    rejected = False
    for field in ("code", "err_code"):
        code = receipt.get(field, 0)
        if type(code) is int:
            code = str(code)
        if code == "0":
            continue
        # Deduplication (40054005) and other unrecognized codes leave delivery unknown.
        if not isinstance(code, str) or code not in _REJECTION_CODES:
            return "unknown"
        rejected = True
    if rejected or status in _REJECTION_STATUSES:
        return "failed"
    acknowledgement = receipt.get("id")
    if (
        200 <= status < 300
        and isinstance(acknowledgement, str)
        and acknowledgement
        and not any(char.isspace() for char in acknowledgement)
    ):
        return "sent"
    return "unknown"
