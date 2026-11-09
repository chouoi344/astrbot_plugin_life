"""Connect replay delivery to the AstrBot 4.29 personal Weixin adapter."""

from collections import Counter
from collections.abc import Mapping
from contextvars import ContextVar

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, MessageType, Platform
from astrbot.api.star import Context
from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import WeixinOCAdapter
from astrbot.core.platform.sources.weixin_oc.weixin_oc_event import (
    WeixinOCMessageEvent,
)

from .replay_transport import IncomingHandler, RecallHandler, ReplayTransport

_GET_UPDATES = "ilink/bot/getupdates"
_SEND_MESSAGE = "ilink/bot/sendmessage"
_REPLAY = "life_busy_weixin_replay"
_INCOMING_ACCOUNT = "life_busy_weixin_incoming_account"
_ACCOUNT = "account_id"
_Source = tuple[str, str, str, str]


class WeixinRejected(RuntimeError):
    """The request was refused or its route failed before the network call."""


class WeixinUnconfirmed(RuntimeError):
    """The network call returned without a readable application status."""


def _validate_ack(result: object) -> None:
    if not isinstance(result, Mapping):
        raise WeixinUnconfirmed("微信未返回可核实的消息回执。")
    codes = [result[key] for key in ("ret", "errcode") if key in result]
    if any(type(code) is int and code != 0 for code in codes):
        raise WeixinRejected("微信拒绝了这条消息。")
    if not codes or any(type(code) is not int for code in codes):
        raise WeixinUnconfirmed("微信未返回可核实的消息回执。")


def _incoming_source(adapter: WeixinOCAdapter, raw: object) -> _Source | None:
    if (
        not adapter.account_id
        or not isinstance(raw, dict)
        or raw.get("message_type", 1) != 1
        or raw.get("group_id")
        or raw.get("delete_time_ms")
    ):
        return None
    user_id = raw.get("from_user_id")
    items = raw.get("item_list")
    if (
        not isinstance(user_id, str)
        or not user_id.strip()
        or user_id.strip() == adapter.account_id
        or not isinstance(items, list)
        or not any(
            isinstance(item, dict) and item.get("type") in (1, 2, 3, 4, 5)
            for item in items
        )
    ):
        return None
    message_id = raw.get("message_id") or raw.get("msg_id")
    if not message_id:
        client_id = raw.get("client_id")
        sequence = raw.get("seq")
        if isinstance(client_id, str) and client_id:
            message_id = f"client:{client_id}"
        elif type(sequence) is int and sequence > 0:
            message_id = f"seq:{sequence}"
        else:
            return None
    if type(message_id) not in (str, int):
        return None
    return adapter.meta().id, adapter.account_id, user_id.strip(), str(message_id)


class WeixinTransport(ReplayTransport):
    def __init__(
        self,
        context: Context,
        *,
        on_recall: RecallHandler,
        on_incoming: IncomingHandler | None = None,
        on_incoming_done: IncomingHandler | None = None,
    ) -> None:
        super().__init__(
            context,
            on_recall=on_recall,
            on_incoming=on_incoming,
            on_incoming_done=on_incoming_done,
        )
        self._native_ingress: Counter[tuple[str, str, str]] = Counter()

    def has_pending_native_ingress(
        self, platform_id: str, account: str, user_id: str
    ) -> bool:
        return bool(self._native_ingress[platform_id, account, user_id])

    def _finish_conversion(self, source: _Source) -> None:
        key = source[:3]
        self._native_ingress[key] -= 1
        if not self._native_ingress[key]:
            del self._native_ingress[key]

    def attach(self, adapter: Platform | None) -> None:
        if self._closed or not isinstance(adapter, WeixinOCAdapter):
            return
        platform_id = adapter.meta().id
        if attached := self._attached.get(platform_id):
            if attached[0] is adapter:
                return
            self._detach_after_delivery(*attached)

        client = adapter.client
        request = client.request_json
        commit = adapter.commit_event
        poll = adapter._poll_inbound_updates
        handle = adapter._handle_inbound_message
        pending: ContextVar[dict[int, _Source] | None] = ContextVar(
            "life_busy_weixin_ingress", default=None
        )

        def reserve(batch: dict[int, _Source], raw: object) -> None:
            if (
                self._closed
                or self.on_incoming is None
                or self.context.get_platform_inst(platform_id) is not adapter
            ):
                return
            if (source := _incoming_source(adapter, raw)) is None:
                return
            # AstrBot falls back to a random UUID if the service only supplies
            # a client ID or sequence. Keep ingress and replay on the same ID.
            if not (raw.get("message_id") or raw.get("msg_id")):
                raw["message_id"] = source[3]
            batch[id(raw)] = source
            self._native_ingress[source[:3]] += 1
            self.on_incoming(*source)

        def finish(batch: dict[int, _Source]) -> None:
            for source in batch.values():
                self._finish_conversion(source)
                if self.on_incoming_done is not None and not self._closed:
                    self.on_incoming_done(*source)

        async def observed_poll() -> None:
            batch = {}
            token = pending.set(batch)
            try:
                await poll()
            finally:
                pending.reset(token)
                finish(batch)

        async def observed_handle(raw: dict) -> None:
            if pending.get() is not None:
                await handle(raw)
                return
            batch = {}
            token = pending.set(batch)
            try:
                reserve(batch, raw)
                await handle(raw)
            finally:
                pending.reset(token)
                finish(batch)

        def observed_commit(event: AstrMessageEvent) -> None:
            if not self._closed and (batch := pending.get()) is not None:
                source = batch.pop(id(event.message_obj.raw_message), None)
                if source is not None:
                    self._finish_conversion(source)
                    event.set_extra(_INCOMING_ACCOUNT, source[1])
                    if self.on_incoming_done is not None:
                        self.observe_cleanup(event, source)
                    try:
                        commit(event)
                    except Exception:
                        event.cleanup_temporary_local_files()
                        raise
                    return
            commit(event)

        async def guarded_request(method: str, endpoint: str, **kwargs):
            if endpoint == _GET_UPDATES:
                account = adapter.account_id
                result = await request(method, endpoint, **kwargs)
                batch = pending.get()
                if (
                    batch is not None
                    and account == adapter.account_id
                    and isinstance(result, dict)
                    and adapter._is_successful_api_payload(result)
                    and isinstance(result.get("msgs"), list)
                ):
                    # A long-poll response can contain other contacts' slow
                    # attachments ahead of this conversation's new messages.
                    for raw in result["msgs"]:
                        reserve(batch, raw)
                return result

            delivery = self._active.get()
            if delivery is None or endpoint != _SEND_MESSAGE:
                return await request(method, endpoint, **kwargs)
            event = delivery.event
            if not isinstance(event, WeixinOCMessageEvent):
                return await request(method, endpoint, **kwargs)

            msg = kwargs["payload"]["msg"]
            user_id = event.get_sender_id()
            parts = []
            for item in msg["item_list"]:
                if item["type"] == 1:
                    parts.append(("text", item["text_item"]["text"]))
                elif item["type"] == adapter.IMAGE_ITEM_TYPE:
                    parts.append(("image", ""))
                else:
                    raise WeixinRejected("微信补答只发送文字或图片。")

            async def submit():
                if (
                    method != "POST"
                    or not kwargs.get("token_required")
                    or msg["to_user_id"] != user_id
                    or event.platform is not adapter
                    or adapter.client is not client
                    or not self.is_current_replay(event)
                ):
                    raise WeixinRejected("微信补答会话或账号已变化。")
                # The permission check can yield, so use the current SDK
                # context token only after obtaining permission to send.
                msg["context_token"] = adapter._context_tokens[user_id]
                return await request(method, endpoint, **kwargs)

            return await self.send_confirmed(
                delivery,
                submit,
                validate_ack=_validate_ack,
                parts=parts,
                is_rejection=lambda error: isinstance(error, WeixinRejected),
            )

        overrides = (
            (client, "request_json", guarded_request),
            (adapter, "commit_event", observed_commit),
            (adapter, "_poll_inbound_updates", observed_poll),
            (adapter, "_handle_inbound_message", observed_handle),
        )
        previous = [vars(owner).get(name) for owner, name, _ in overrides]
        for owner, name, override in overrides:
            setattr(owner, name, override)

        def detach() -> None:
            for (owner, name, override), original in zip(overrides, previous):
                if vars(owner).get(name) is override:
                    if original is None:
                        delattr(owner, name)
                    else:
                        setattr(owner, name, original)

        self._attached[platform_id] = (adapter, detach)

    def _detach_after_delivery(self, adapter, detach) -> None:
        # An old event may still reach a direct native send from a tool task.
        # Keep its route check attached until that pipeline is cleaned up.
        events = {
            event
            for event in self._deliveries
            if isinstance(event, WeixinOCMessageEvent) and event.platform is adapter
        }
        if not events:
            detach()
            return
        for event in events:

            def cleanup(event=event, original=event.cleanup_temporary_local_files):
                try:
                    original()
                finally:
                    events.discard(event)
                    if not events:
                        detach()

            event.cleanup_temporary_local_files = cleanup

    def self_id(self, event: AstrMessageEvent) -> str:
        if isinstance(event, WeixinOCMessageEvent):
            return (
                event.get_extra(_REPLAY)
                or event.get_extra(_INCOMING_ACCOUNT)
                or event.platform.account_id
                or ""
            )
        return ""

    def is_incoming(self, event: AstrMessageEvent) -> bool:
        if (
            not isinstance(event, WeixinOCMessageEvent)
            or event.get_extra(_REPLAY)
            or not event.is_private_chat()
            or self.context.get_platform_inst(event.get_platform_id())
            is not event.platform
            or not event.get_messages()
        ):
            return False
        source = _incoming_source(event.platform, event.message_obj.raw_message)
        return (
            source
            == (
                event.get_platform_id(),
                self.self_id(event),
                event.get_sender_id(),
                str(event.message_obj.message_id),
            )
            and event.get_session_id() == event.get_sender_id()
        )

    def routing(self, event: AstrMessageEvent) -> dict | None:
        account = self.self_id(event)
        return {_ACCOUNT: account} if account else None

    def can_deliver(self, platform_id: str, account: str, user_id: str) -> bool:
        adapter = self.context.get_platform_inst(platform_id)
        return (
            isinstance(adapter, WeixinOCAdapter)
            and bool(account)
            and adapter.account_id == account
            and not adapter._shutdown_event.is_set()
            and bool(adapter.token)
            and adapter.client.token == adapter.token
            and bool(adapter._context_tokens.get(user_id))
        )

    def is_current_replay(self, event: AstrMessageEvent) -> bool:
        platform_id = event.get_platform_id()
        return (
            not self._closed
            and isinstance(event, WeixinOCMessageEvent)
            and self.context.get_platform_inst(platform_id) is event.platform
            and self.can_deliver(
                platform_id,
                event.get_extra(_REPLAY),
                event.get_sender_id(),
            )
        )

    def create_replay(
        self, platform_id: str, message: AstrBotMessage, routing: dict | None = None
    ) -> WeixinOCMessageEvent:
        account = (routing or {}).get(_ACCOUNT)
        if (
            message.type != MessageType.FRIEND_MESSAGE
            or message.session_id != message.sender.user_id
            or message.self_id not in (platform_id, account)
        ):
            raise ValueError("补答消息必须保留原微信私聊会话及发送人。")
        adapter = self._adapter(platform_id)
        if not self.can_deliver(platform_id, account, message.session_id):
            raise WeixinRejected("微信会话暂不可发送，等候账号或会话凭据恢复。")
        message.self_id = platform_id
        message.raw_message = None
        event = adapter.create_event(message)
        event.set_extra(_REPLAY, account)
        return event

    def enqueue(self, event: AstrMessageEvent) -> None:
        try:
            adapter = self._adapter(event.get_platform_id())
            if not self.is_current_replay(event):
                raise WeixinRejected("微信补答会话或账号已变化。")
            adapter.commit_event(event)
        except Exception:
            event.cleanup_temporary_local_files()
            raise

    def _adapter(self, platform_id: str) -> WeixinOCAdapter:
        adapter = self.context.get_platform_inst(platform_id)
        if self._closed or not isinstance(adapter, WeixinOCAdapter):
            raise WeixinRejected("个人微信适配器当前不可用。")
        self.attach(adapter)
        return adapter
