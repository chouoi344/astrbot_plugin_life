"""Confirmed QQ Official C2C sends without a reply message ID."""

from __future__ import annotations

import re
from random import randint

from aiohttp import ClientTimeout
from botpy.http import Route

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.platform import PlatformStatus

from ...photo.core.qq_official_delivery import qq_media_upload_payload
from .delivery_error import DeliveryError, ReplyRequired

_OPENID = re.compile(r"[A-Za-z0-9_-]+")
_REJECTION_STATUSES = {401, 403, 404, 405, 413, 415, 422, 429}
_WAIT_FOR_REPLY_CODES = {"40034100", "40034105", "40054004", "40054013"}
_DEDUPLICATED = "40054005"


def _check_response(status: int, result) -> dict:
    if not isinstance(result, dict):
        raise DeliveryError("unknown", "QQ 官方接口结果未确认")
    codes = {str(result[key]).strip() for key in ("code", "err_code") if key in result}
    codes.discard("0")
    if _DEDUPLICATED in codes:
        raise DeliveryError("unknown", "QQ 官方接口报告消息被去重，实际送达未确认")
    if (codes and codes <= _WAIT_FOR_REPLY_CODES) or (not codes and status == 403):
        raise ReplyRequired()
    if not codes and status in _REJECTION_STATUSES:
        raise DeliveryError("failed", "QQ 官方接口拒绝本次请求")
    if codes or not 200 <= status < 300:
        raise DeliveryError("unknown", "QQ 官方接口结果未确认")
    return result


class QQOfficialTransport:
    """Use the configured botpy client's credentials and C2C endpoints."""

    supports_voice = True
    supports_photo = True

    def __init__(self, platform, recipient: str, bot_self_id: str):
        self.platform = platform
        self.recipient = recipient
        self.bot_self_id = bot_self_id

    def _http(self):
        if (
            not self.bot_self_id
            or _OPENID.fullmatch(self.recipient) is None
            or self.platform.meta().name not in {"qq_official", "qq_official_webhook"}
            or self.platform.status != PlatformStatus.RUNNING
            or str(self.platform.appid) != self.bot_self_id
        ):
            return None
        client = self.platform.get_client()
        http = getattr(getattr(client, "api", None), "_http", None)
        token = getattr(http, "_token", None)
        if token is not None and str(token.app_id) == self.bot_self_id:
            return http
        return None

    def available(self) -> bool:
        return self._http() is not None

    async def _post_once(self, http, endpoint: str, payload: dict):
        route = Route(
            "POST",
            f"/v2/users/{{openid}}/{endpoint}",
            is_sandbox=http.is_sandbox,
            openid=self.recipient,
        )
        # botpy's request helper retries after a dropped connection. A C2C push
        # may already be visible in that case, so use its authenticated session once.
        try:
            async with http._session.request(
                method=route.method,
                url=route.url,
                headers=http._headers,
                timeout=ClientTimeout(total=http.timeout),
                allow_redirects=False,
                json=payload,
            ) as response:
                result = await response.json(content_type=None)
                return _check_response(response.status, result)
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError("unknown", "QQ 官方接口结果未确认") from exc

    async def _payload(self, http, message: MessageChain) -> dict:
        if len(message.chain) != 1:
            raise DeliveryError("failed", "QQ 官方主动消息一次只发送一种内容")
        component = message.chain[0]
        if isinstance(component, Plain) and component.text.strip():
            return {"msg_type": 0, "content": component.text}
        payload = await qq_media_upload_payload(component)
        if payload is None:
            raise DeliveryError("failed", "QQ 官方附件格式或大小超过直接上传限制")
        result = await self._post_once(
            http,
            "files",
            {"openid": self.recipient, **payload},
        )
        file_info = result.get("file_info") if isinstance(result, dict) else None
        if not isinstance(file_info, str) or not file_info.strip():
            raise DeliveryError("failed", "QQ 官方媒体上传未返回文件信息")
        return {"msg_type": 7, "media": {"file_info": file_info}}

    async def send(self, message: MessageChain, *, before_send, on_attempt=None) -> str:
        http = self._http()
        if http is None:
            raise DeliveryError("failed", "QQ 官方机器人账号未就绪或已变更")
        try:
            await http.check_session()
            payload = await self._payload(http, message)
        except ReplyRequired:
            raise
        except Exception as exc:
            # Nothing visible was sent. In particular, uploads use srv_send_msg=False.
            raise DeliveryError("failed", "QQ 官方主动消息准备失败") from exc

        if before_send is not None and not await before_send():
            raise DeliveryError("stopped", "当前条件已变化，停止发送")
        if self._http() is not http:
            raise DeliveryError("failed", "QQ 官方机器人账号已变更")
        if on_attempt is not None:
            on_attempt()
        result = await self._post_once(
            http, "messages", {**payload, "msg_seq": randint(1, 10000)}
        )
        message_id = result.get("id") if isinstance(result, dict) else None
        if (
            not isinstance(message_id, str)
            or not message_id
            or any(char.isspace() for char in message_id)
        ):
            raise DeliveryError("unknown", "QQ 官方接口未返回有效消息编号")
        return message_id
