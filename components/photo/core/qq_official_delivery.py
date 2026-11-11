"""Confirm a passive reply on AstrBot's QQ Official C2C transport."""

from __future__ import annotations

import random
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

QQ_DIRECT_UPLOAD_LIMIT = 5_000_000
_REJECTION_STATUSES = {400, 401, 403, 404, 405, 413, 415, 422, 429}
_REJECTION_CODES = {
    "22009",
    "304080",
    "304082",
    "304083",
    "304103",
    "40034004",
    "40034005",
    "40034024",
    "40034100",
    "40034105",
    "40034128",
    "40054004",
    "40054013",
}
_REPLY_SEQUENCE = "_life_qq_official_reply_seq"


def next_qq_official_reply_seq(event) -> int:
    message_id = event.message_obj.message_id
    previous = event.get_extra(_REPLY_SEQUENCE)
    if previous is None or previous[0] != message_id:
        sequence = random.randint(1, 10000)
    else:
        sequence = previous[1] % 10000 + 1
    event.set_extra(_REPLY_SEQUENCE, (message_id, sequence))
    return sequence


def qq_direct_upload_payload(encoded: str, *, file_type: int) -> dict | None:
    if len(encoded) > QQ_DIRECT_UPLOAD_LIMIT:
        return None
    if file_type == 1:
        if encoded.startswith("iVBORw0KGgo"):
            file_name = "photo.png"
        elif encoded.startswith("/9j/"):
            file_name = "photo.jpg"
        else:
            return None
    elif file_type == 3:
        file_name = "voice.silk"
    else:
        return None
    return {
        "file_name": file_name,
        "file_data": encoded,
        "file_type": file_type,
        "srv_send_msg": False,
    }


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(char.isspace() for char in value)


def _reply_target(event):
    platform = event.get_platform_name()
    if platform not in ("qq_official", "qq_official_webhook"):
        return None

    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from botpy import BotAPI, Client
    from botpy.http import BotHttp
    from botpy.message import C2CMessage

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
    bot = event.bot
    if not isinstance(raw, C2CMessage) or not isinstance(bot, Client) or bot.is_closed():
        return None
    adapter = getattr(bot, "platform", None)
    if adapter is None or adapter.get_client() is not bot:
        return None
    meta = adapter.meta()
    appid = adapter.appid
    if meta.name != platform or meta.id != event.get_platform_id() or not appid:
        return None
    api = bot.api
    if raw._api is not api or not isinstance(api, BotAPI):
        return None
    if not isinstance(api._http, BotHttp):
        return None
    openid = raw.author.user_openid
    if not _valid_id(openid) or openid != event.session_id or openid != event.get_sender_id():
        return None
    if not _valid_id(raw.id) or raw.id != message.message_id:
        return None

    return openid, raw.id, api._http, str(appid)


def can_send_qq_official_photo(event) -> bool:
    return _reply_target(event) is not None


async def send_qq_official_photo(event, component, *, before_send) -> str:
    from astrbot.api.message_components import Image

    if not isinstance(component, Image):
        return "failed"
    return await send_qq_official_reply(event, component, before_send=before_send)


async def qq_media_upload_payload(component) -> dict | None:
    from astrbot.api.message_components import Image, Record
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot.core.utils.media_utils import MediaResolver

    if isinstance(component, Image) and component.file:
        source = component.file
        resolver = MediaResolver(source, media_type="image")
        file_type = QQOfficialMessageEvent.IMAGE_FILE_TYPE
        target_format = None
    elif isinstance(component, Record) and (source := component.url or component.file):
        resolver = MediaResolver(source, media_type="audio", default_suffix=".wav")
        file_type = QQOfficialMessageEvent.VOICE_FILE_TYPE
        target_format = "tencent_silk"
    else:
        return None
    if source.startswith(("http://", "https://")):
        return None
    encoded = await resolver.to_base64(target_format=target_format)
    return qq_direct_upload_payload(encoded, file_type=file_type)


async def send_qq_official_reply(event, component, *, before_send) -> str:
    from astrbot.api.message_components import Plain
    from botpy.http import Route

    target = _reply_target(event)
    if target is None:
        return "failed"

    openid, message_id, http, appid = target
    parameters = {"is_sandbox": http.is_sandbox, "openid": quote(openid, safe="")}
    timeout = ClientTimeout(total=min(http.timeout, 20))
    try:
        if isinstance(component, Plain) and component.text.strip():
            payload = {"msg_type": 0, "content": component.text}
            media_upload = None
        else:
            media_upload = await qq_media_upload_payload(component)
            if media_upload is None:
                return "failed"
        await http.check_session()
        session, headers = http._session, http._headers
        if session is None or str(headers.get("X-Union-Appid", "")) != appid:
            return "failed"
        if media_upload is not None:
            upload_route = Route("POST", "/v2/users/{openid}/files", **parameters)
            # Upload without displaying a message or logging the media contents.
            async with session.request(
                upload_route.method,
                upload_route.url,
                headers=headers,
                json={"openid": openid, **media_upload},
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    return "failed"
                upload = await response.json(content_type=None)
            if not isinstance(upload, dict):
                return "failed"
            file_info = upload.get("file_info")
            if not isinstance(file_info, str) or not file_info.strip():
                return "failed"
            payload = {"msg_type": 7, "media": {"file_info": file_info}}
    except (ClientError, OSError, RuntimeError, ValueError):
        return "failed"

    if (
        await before_send() is False
        or _reply_target(event) != target
        or http._session is not session
        or str(http._headers.get("X-Union-Appid", "")) != appid
    ):
        return "failed"
    payload.update(openid=openid, msg_id=message_id, msg_seq=next_qq_official_reply_seq(event))
    send_route = Route("POST", "/v2/users/{openid}/messages", **parameters)

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
    if not isinstance(receipt, dict) or status == 408 or not (200 <= status < 300 or 400 <= status < 500):
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
    if 200 <= status < 300 and _valid_id(receipt.get("id")):
        return "sent"
    return "unknown"
