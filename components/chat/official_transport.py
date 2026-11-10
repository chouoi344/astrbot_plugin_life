"""Replay private messages through AstrBot's QQ Official adapters."""

from contextvars import ContextVar
from copy import deepcopy
from urllib.parse import quote

import aiohttp
from botpy.http import Route
from botpy.message import C2CMessage

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.platform import AstrBotMessage, MessageType, Platform
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
)
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_adapter import (
    QQOfficialWebhookPlatformAdapter,
)

from ..photo.core.qq_official_delivery import (
    next_qq_official_reply_seq,
    qq_media_upload_payload,
)
from .replay_transport import Delivery, DeliveryBlocked, ReplayTransport

_OfficialAdapter = QQOfficialPlatformAdapter | QQOfficialWebhookPlatformAdapter
_REPLAY = "life_busy_qq_official_replay"
_REJECTION_STATUSES = {400, 401, 403, 404, 405, 413, 415, 422, 429}
# QQ may return a business rejection in a non-4xx HTTP response.
_REJECTION_CODES = {
    "304103", "40034005", "40034024", "40034100", "40034105", "40034128",
    "40054004", "40054013",
}
_DEDUPLICATED = "40054005"


class _SendRejected(RuntimeError):
    """QQ explicitly rejected this request."""


class QQReplyRejected(_SendRejected):
    """The platform rejected the current reply context."""


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(char.isspace() for char in value)
    )


def _validate_ack(result: object) -> None:
    if (
        not isinstance(result, dict)
        or not _valid_id(result.get("id"))
    ):
        raise RuntimeError("QQ 官方接口未返回有效消息编号。")


def _response_codes(result) -> set[str]:
    if not isinstance(result, dict):
        return set()
    return {str(result[key]).strip() for key in ("code", "err_code") if key in result} - {"0"}


def _restore_hook(instance, name, hook, previous) -> None:
    if vars(instance).get(name) is hook:
        if previous is None:
            delattr(instance, name)
        else:
            setattr(instance, name, previous)


class OfficialTransport(ReplayTransport):
    def attach(self, adapter: Platform | None) -> None:
        if self._closed or not isinstance(adapter, _OfficialAdapter):
            return
        platform_id = adapter.meta().id
        if attached := self._attached.get(platform_id):
            if attached[0] is adapter:
                return
            attached[1]()

        bot = adapter.get_client()
        on_c2c = bot.on_c2c_message_create
        commit = adapter.commit_event
        send_by_session = adapter.send_by_session
        old_c2c = vars(bot).get("on_c2c_message_create")
        old_commit = vars(adapter).get("commit_event")
        old_send = vars(adapter).get("send_by_session")
        incoming_raw = ContextVar("life_busy_official_incoming_raw", default=None)

        def source(raw):
            return platform_id, str(adapter.appid), raw.author.user_openid, raw.id

        async def observed_c2c(raw):
            if (
                self._closed
                or self.on_incoming is None
                or not isinstance(raw, C2CMessage)
                or not _valid_id(raw.author.user_openid)
                or not _valid_id(raw.id)
            ):
                return await on_c2c(raw)

            origin = source(raw)
            if self.on_incoming(*origin) is False:
                return await on_c2c(raw)
            token = incoming_raw.set((raw, origin))
            try:
                return await on_c2c(raw)
            finally:
                pending = incoming_raw.get() is not None
                incoming_raw.reset(token)
                if pending and self.on_incoming_done is not None and not self._closed:
                    self.on_incoming_done(*origin)

        def observed_commit(event):
            raw = event.message_obj.raw_message
            incoming = incoming_raw.get()
            if self._closed or incoming is None or raw is not incoming[0]:
                return commit(event)
            incoming_raw.set(None)
            self.observe_cleanup(event, incoming[1])
            try:
                return commit(event)
            except Exception:
                event.cleanup_temporary_local_files()
                raise

        async def guarded_session_send(session, message_chain):
            delivery = self._active.get()
            if (
                delivery is None
                or delivery.event.session != session
                or delivery.event.bot is not bot
            ):
                return await send_by_session(session, message_chain)
            if await self._send(delivery, adapter, message_chain):
                await Platform.send_by_session(adapter, session, message_chain)

        bot.on_c2c_message_create = observed_c2c
        adapter.commit_event = observed_commit
        adapter.send_by_session = guarded_session_send

        def detach():
            _restore_hook(bot, "on_c2c_message_create", observed_c2c, old_c2c)
            _restore_hook(adapter, "commit_event", observed_commit, old_commit)
            _restore_hook(adapter, "send_by_session", guarded_session_send, old_send)

        self._attached[platform_id] = (adapter, detach)

    def _adapter(self, platform_id: str) -> _OfficialAdapter:
        if self._closed:
            raise RuntimeError("QQ 官方补答已停止。")
        adapter = self.context.get_platform_inst(platform_id)
        if not isinstance(adapter, _OfficialAdapter):
            raise RuntimeError(f"QQ 官方适配器 {platform_id} 当前不可用。")
        self.attach(adapter)
        return adapter

    def can_deliver(self, platform_id: str, bot_id: str, user_id: str) -> bool:
        adapter = self.context.get_platform_inst(platform_id)
        if self._closed or not isinstance(adapter, _OfficialAdapter):
            return False
        bot = adapter.get_client()
        token = bot.api._http._token
        return (
            bool(adapter.appid)
            and str(adapter.appid) == bot_id
            and _valid_id(user_id)
            and not bot.is_closed()
            and token is not None
            and str(token.app_id) == bot_id
        )

    def self_id(self, event: AstrMessageEvent) -> str:
        adapter = self.context.get_platform_inst(event.get_platform_id())
        if (
            isinstance(adapter, _OfficialAdapter)
            and adapter.appid
            and adapter.get_client() is getattr(event, "bot", None)
        ):
            return str(adapter.appid)
        return ""

    def is_incoming(self, event: AstrMessageEvent) -> bool:
        if not isinstance(event, QQOfficialMessageEvent) or event.get_extra(_REPLAY):
            return False
        raw = event.message_obj.raw_message
        return (
            isinstance(raw, C2CMessage)
            and event.is_private_chat()
            and _valid_id(raw.id)
            and _valid_id(raw.author.user_openid)
            and event.message_obj.message_id == raw.id
            and event.get_session_id()
            == event.get_sender_id()
            == raw.author.user_openid
            and bool(self.self_id(event))
        )

    def routing(self, event: AstrMessageEvent) -> dict | None:
        if not self.is_incoming(event):
            return None
        return {"appid": self.self_id(event), "openid": event.get_sender_id()}

    def create_replay(
        self, platform_id: str, message: AstrBotMessage, routing: dict | None = None
    ) -> AstrMessageEvent:
        adapter = self._adapter(platform_id)
        openid = message.sender.user_id
        if (
            message.type != MessageType.FRIEND_MESSAGE
            or message.session_id != openid
            or not _valid_id(openid)
            or not _valid_id(message.message_id)
            or routing != {"appid": str(adapter.appid), "openid": openid}
        ):
            raise ValueError("补答消息必须保留原 QQ 官方机器人和单聊会话。")

        bot = adapter.get_client()
        message.raw_message = C2CMessage(
            bot.api,
            None,
            {
                "id": message.message_id,
                "author": {"user_openid": openid},
                "content": message.message_str,
            },
        )
        # Webhook create_event consumes extras for a freshly arrived message
        # with this ID. The restored event must not take those live extras.
        message_id = message.message_id
        message.message_id = ""
        try:
            event = adapter.create_event(message)
        finally:
            message.message_id = message_id
        event.set_extra(_REPLAY, True)

        async def send(message_chain):
            delivery = self._active.get()
            if delivery is None or delivery.event is not event:
                raise RuntimeError("QQ 官方补答事件尚未注册发送检查。")
            if await self._send(delivery, adapter, message_chain):
                await AstrMessageEvent.send(event, message_chain)

        async def send_streaming(generator, use_fallback=False):
            # Delayed sends do not use QQ's reply-only streaming protocol.
            message_chain = None
            async for chunk in generator:
                if message_chain is None:
                    message_chain = chunk.derive()
                message_chain.chain.extend(deepcopy(chunk.chain))
            if message_chain is not None:
                await event.send(message_chain)

        event.send = send
        event.send_streaming = send_streaming
        return event

    def enqueue(self, event: AstrMessageEvent) -> None:
        try:
            adapter = self._adapter(event.get_platform_id())
            if adapter.get_client() is not event.bot:
                raise RuntimeError("QQ 官方适配器已重载，需要重新创建补答事件。")
            adapter.commit_event(event)
        except Exception:
            event.cleanup_temporary_local_files()
            raise

    async def _upload(self, http, event, adapter, component, route_params):
        payload = await qq_media_upload_payload(component)
        if payload is None:
            raise RuntimeError("QQ 官方附件格式或大小超过直接上传限制。")
        if (
            not await self.check(event)
            or self.context.get_platform_inst(event.get_platform_id()) is not adapter
            or event.bot.api._http is not http
        ):
            raise DeliveryBlocked("这轮补答已结束。")
        route = Route("POST", "/v2/users/{openid}/files", **route_params)
        async with http._session.request(
            method=route.method,
            url=route.url,
            headers=http._headers,
            timeout=aiohttp.ClientTimeout(total=http.timeout),
            json={"openid": event.get_sender_id(), **payload},
            allow_redirects=False,
        ) as response:
            result = await response.json(content_type=None)
            if not 200 <= response.status < 300 or _response_codes(result):
                raise RuntimeError("QQ 官方接口拒绝上传附件。")
            return result

    async def _send(
        self, delivery: Delivery, adapter: _OfficialAdapter, message_chain: MessageChain
    ) -> bool:
        event = delivery.event
        if self.context.get_platform_inst(event.get_platform_id()) is not adapter:
            raise RuntimeError("QQ 官方适配器已重载，需要重新创建补答事件。")
        openid = event.get_sender_id()
        sent = False
        for chunk in event._split_message_chain_by_media(message_chain):
            text = "".join(part.text for part in chunk.chain if isinstance(part, Plain))
            component = next(
                (part for part in chunk.chain if isinstance(part, (Image, Record, Video, File))),
                None,
            )
            parts = [("text", text)] if text.strip() else []
            if component is None and not parts:
                continue
            http = event.bot.api._http
            try:
                await http.check_session()
            except Exception:
                # botpy can include the token response in an authentication error.
                raise RuntimeError("QQ 官方客户端未能准备发送凭证。") from None
            if str(http._headers["X-Union-Appid"]) != str(adapter.appid):
                raise RuntimeError("QQ 官方客户端的 AppID 与原会话不一致。")
            route_params = {"is_sandbox": http.is_sandbox, "openid": quote(openid, safe="")}
            if isinstance(component, (Image, Record)):
                media = await self._upload(http, event, adapter, component, route_params)
                parts.append(("image" if isinstance(component, Image) else "record", ""))
            elif component is not None:
                _, _, _, _, video, file, filename = await event._parse_to_qqofficial(chunk)
                if isinstance(component, Video):
                    kind, path, file_type = "video", video, event.VIDEO_FILE_TYPE
                else:
                    kind, path, file_type = "file", file, event.FILE_FILE_TYPE
                if not path:
                    raise RuntimeError("QQ 官方附件未能转换为可发送的文件。")
                media = await event.upload_group_and_c2c_media(
                    path, file_type, file_name=filename, openid=openid
                )
                parts.append((kind, ""))
            if component is not None:
                file_info = media.get("file_info") if isinstance(media, dict) else None
                if not _valid_id(file_info):
                    raise RuntimeError("QQ 官方附件上传未返回有效文件信息。")
                payload = {"msg_type": 7, "media": {"file_info": file_info}}
                if text:
                    payload["content"] = text
            elif chunk.use_markdown_ is True:
                payload = {"msg_type": 2, "markdown": {"content": text}}
            else:
                payload = {"msg_type": 0, "content": text}

            payload["msg_id"] = event.message_obj.message_id
            payload["msg_seq"] = next_qq_official_reply_seq(event)
            route = Route("POST", "/v2/users/{openid}/messages", **route_params)

            async def post():
                if (
                    self.context.get_platform_inst(event.get_platform_id())
                    is not adapter
                    or event.bot.api._http is not http
                ):
                    raise _SendRejected("QQ 官方适配器已重载，本次补答未发送。")
                # BotHttp.request retries connection resets. Keep the SDK's
                # authenticated session but make this visible POST only once.
                async with http._session.request(
                    method=route.method,
                    url=route.url,
                    headers=http._headers,
                    timeout=aiohttp.ClientTimeout(total=http.timeout),
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    try:
                        result = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        if response.status not in _REJECTION_STATUSES:
                            raise
                        result = None
                    codes = _response_codes(result)
                    if _DEDUPLICATED in codes:
                        raise RuntimeError(
                            "QQ 官方接口报告消息已去重，实际送达未确认。"
                        )
                    if codes and codes <= _REJECTION_CODES:
                        raise QQReplyRejected(f"QQ 官方接口拒绝回复（{', '.join(sorted(codes))}）。")
                    if not codes and response.status in _REJECTION_STATUSES:
                        raise QQReplyRejected(
                            f"QQ 官方接口拒绝发送（HTTP {response.status}）。"
                        )
                    if codes or not 200 <= response.status < 300:
                        raise RuntimeError(
                            f"QQ 官方接口发送未确认（HTTP {response.status}）。"
                        )
                    return result

            result = await self.send_confirmed(
                delivery,
                post,
                validate_ack=_validate_ack,
                parts=parts,
                is_rejection=lambda error: isinstance(error, _SendRejected),
            )
            adapter.remember_session_message_id(openid, result["id"])
            sent = True
        return sent
