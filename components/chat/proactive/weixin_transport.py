"""Confirmed text and photo delivery through AstrBot's personal WeChat adapter."""

from __future__ import annotations

import re
from uuid import uuid4

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Plain
from astrbot.core.platform.platform import PlatformStatus

from .delivery_error import DeliveryError

_IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".webp"}


def _confirmed_message_id(reply, client_id: str) -> str:
    if not isinstance(reply, dict):
        raise DeliveryError("unknown", "微信发送回执无法确认")

    codes = {}
    for field in ("ret", "errcode"):
        if field not in reply:
            continue
        value = reply[field]
        if type(value) is int or (
            isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value)
        ):
            codes[field] = int(value)

    if any(code != 0 for code in codes.values()):
        raise DeliveryError("failed", "微信接口拒绝发送消息")
    if any(field in reply and field not in codes for field in ("ret", "errcode")):
        raise DeliveryError("unknown", "微信发送回执无法确认")

    message_id = reply.get("message_id")
    if type(message_id) in (int, str):
        number = str(message_id)
        if re.fullmatch(r"[1-9][0-9]{0,19}", number) and int(number) < 2**64:
            return number

    # iLink can acknowledge the request with ret=0 and omit its own message ID.
    # Preserve the exact client_id of that request as an acknowledgement reference.
    if codes.get("ret") == 0:
        return client_id
    raise DeliveryError("unknown", "微信发送回执无法确认")


class WeixinTransport:
    supports_voice = False
    supports_photo = True

    def __init__(self, platform, recipient: str, bot_self_id: str):
        self.platform = platform
        self.recipient = recipient
        # The native event self_id is the adapter instance ID. The delivery
        # identity is the actual iLink account ID returned by QR login.
        self.bot_self_id = bot_self_id

    def _contact_token(self) -> str | None:
        platform = self.platform
        if (
            platform.meta().name != "weixin_oc"
            or platform.status is not PlatformStatus.RUNNING
            or not self.bot_self_id
            or platform.account_id != self.bot_self_id
            or not platform.token
            or platform.client.token != platform.token
        ):
            return None
        return platform._context_tokens.get(self.recipient)

    def available(self) -> bool:
        return bool(self._contact_token())

    async def _prepare_item(self, message: MessageChain) -> dict:
        platform = self.platform
        if all(isinstance(segment, Plain) for segment in message.chain):
            text = "".join(segment.text for segment in message.chain).strip()
            if text:
                return platform._build_plain_text_item(text)

        if len(message.chain) != 1 or not isinstance(message.chain[0], (Image, File)):
            raise DeliveryError("failed", "微信主动消息只支持纯文字或单张照片")

        segment = message.chain[0]
        try:
            path = await platform._resolve_media_file_path(segment)
            if path is None:
                raise DeliveryError("failed", "微信照片文件不可用")
            if isinstance(segment, Image):
                upload_type = platform.IMAGE_UPLOAD_TYPE
                item_type = platform.IMAGE_ITEM_TYPE
                file_name = path.name
            else:
                # The photo plugin sends its largest photos as original files.
                if path.suffix.lower() not in _IMAGE_SUFFIXES:
                    raise DeliveryError("failed", "微信主动消息只支持照片文件")
                upload_type = platform.FILE_UPLOAD_TYPE
                item_type = platform.FILE_ITEM_TYPE
                file_name = segment.name or path.name
            return await platform._prepare_media_item(
                self.recipient, path, upload_type, item_type, file_name
            )
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError("failed", "微信照片上传失败，消息未发送") from None

    async def send(self, message: MessageChain, *, before_send, on_attempt=None) -> str:
        context_token = self._contact_token()
        if not context_token:
            raise DeliveryError("failed", "目标微信账号未就绪或没有有效的历史联系")
        account_token = self.platform.token
        item = await self._prepare_item(message)
        if before_send is not None and not await before_send():
            raise DeliveryError("stopped", "当前条件已变化，停止发送")
        if self._contact_token() != context_token or self.platform.token != account_token:
            raise DeliveryError("stopped", "微信账号或会话已变化，停止发送")

        client_id = uuid4().hex
        payload = {
            "base_info": {"channel_version": "astrbot"},
            "msg": {
                "from_user_id": "",
                "to_user_id": self.recipient,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [item],
            },
        }
        if on_attempt is not None:
            on_attempt()
        try:
            reply = await self.platform.client.request_json(
                "POST",
                "ilink/bot/sendmessage",
                payload=payload,
                token_required=True,
            )
        except Exception:
            # The server may have accepted the message before the response was lost.
            raise DeliveryError("unknown", "微信发送结果未知") from None
        return _confirmed_message_id(reply, client_id)
