"""Connect buffered messages to the AstrBot 4.29 OneBot adapter."""

import re
from collections.abc import Iterator

from aiocqhttp import Event, Message
from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable, NetworkError

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, MessageType, Platform
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from .replay_transport import ReplayTransport

_RECALL_EVENT = "notice.friend_recall"
_INCOMING_EVENT = "message.private"
_REPLAY_EXTRA = "life_busy_qq_replay"
_REPLAY_LOCAL_READS = {"get_msg", "get_image", "get_record", "get_forward_msg"}
_MESSAGE_ID = re.compile(r"-?[1-9][0-9]*")
_MESSAGE_SEND_ACTIONS = {
    "send_msg",
    "send_private_msg",
    "send_forward_msg",
    "send_private_forward_msg",
}
_PRIVATE_SEND_ACTIONS = _MESSAGE_SEND_ACTIONS | {"upload_private_file"}


def _visible_parts(message: object) -> Iterator[tuple[str, str]]:
    if isinstance(message, str):
        message = Message(message)
    if not isinstance(message, list):
        return
    for segment in message:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data") or {}
        kind = segment.get("type")
        if kind == "node":
            yield from _visible_parts(data.get("content"))
        elif isinstance(kind, str):
            text = data.get("text") if kind == "text" else None
            yield kind, text if isinstance(text, str) else ""


class QQTransport(ReplayTransport):
    def is_current_replay(self, event: AstrMessageEvent) -> bool:
        adapter = self.context.get_platform_inst(event.get_platform_id())
        return (
            isinstance(adapter, AiocqhttpAdapter) and adapter.get_client() is event.bot
        )

    def is_incoming(self, event: AstrMessageEvent) -> bool:
        raw = event.message_obj.raw_message
        return (
            isinstance(raw, Event)
            and raw.get("post_type") == "message"
            and raw.get("message_type") == "private"
            and _MESSAGE_ID.fullmatch(str(event.message_obj.message_id)) is not None
        )

    def attach_all(self) -> None:
        for adapter in self.context.platform_manager.platform_insts:
            self.attach(adapter)

    def attach(self, adapter: Platform | None) -> None:
        if self._closed or not isinstance(adapter, AiocqhttpAdapter):
            return
        platform_id = adapter.meta().id
        if attached := self._attached.get(platform_id):
            if attached[0] is adapter:
                return
            attached[1]()

        bot = adapter.get_client()
        api = bot.api
        call_action = api.call_action
        old_override = vars(api).get("call_action")
        commit_event = adapter.commit_event
        old_commit_override = vars(adapter).get("commit_event")

        def source(raw: Event) -> tuple[str, str, str, str]:
            return (
                platform_id,
                str(raw["self_id"]),
                str(raw["user_id"]),
                str(raw["message_id"]),
            )

        async def handle_recall(raw: Event) -> None:
            if not self._closed:
                await self.on_recall(*source(raw))

        async def handle_incoming(raw: Event) -> None:
            if not self._closed and self.on_incoming is not None:
                self.on_incoming(*source(raw))

        def observed_commit(event: AstrMessageEvent) -> None:
            raw = getattr(event.message_obj, "raw_message", None)
            if (
                self._closed
                or self.on_incoming_done is None
                or event.get_extra(_REPLAY_EXTRA)
                or not isinstance(raw, Event)
                or raw.get("post_type") != "message"
                or raw.get("message_type") != "private"
            ):
                commit_event(event)
                return

            self.observe_cleanup(event, source(raw))
            try:
                commit_event(event)
            except Exception:
                event.cleanup_temporary_local_files()
                raise

        async def guarded_action(action: str, **params):
            delivery = self._active.get()
            if delivery is None or delivery.event.get_platform_id() != platform_id:
                return await call_action(action, **params)
            if action in _REPLAY_LOCAL_READS and delivery.event.get_extra(
                _REPLAY_EXTRA
            ):
                return None
            if (
                action not in _PRIVATE_SEND_ACTIONS
                or params.get("group_id") is not None
                or params.get("message_type") == "group"
                or str(params.get("user_id")) != delivery.event.get_sender_id()
            ):
                return await call_action(action, **params)

            self_id = delivery.event.get_self_id()
            if "self_id" in params and str(params["self_id"]) != self_id:
                return await call_action(action, **params)
            if self_id:
                params.setdefault("self_id", int(self_id))

            def validate_ack(result):
                if action in _MESSAGE_SEND_ACTIONS:
                    message_id = (
                        result.get("message_id") if isinstance(result, dict) else None
                    )
                    if _MESSAGE_ID.fullmatch(str(message_id)) is None:
                        raise NetworkError("QQ 接口未返回有效消息编号。")

            parts = list(_visible_parts(params.get("message", params.get("messages"))))
            if action == "upload_private_file":
                parts.append(("file", ""))
            return await self.send_confirmed(
                delivery,
                lambda: call_action(action, **params),
                validate_ack=validate_ack,
                parts=parts,
                is_rejection=lambda exc: isinstance(
                    exc, (ActionFailed, ApiNotAvailable)
                ),
            )

        bot.hook_before(_RECALL_EVENT, handle_recall)
        if self.on_incoming is not None:
            bot.hook_before(_INCOMING_EVENT, handle_incoming)
        if self.on_incoming_done is not None:
            adapter.commit_event = observed_commit
        api.call_action = guarded_action

        def detach() -> None:
            bot.unhook_before(_RECALL_EVENT, handle_recall)
            if self.on_incoming is not None:
                bot.unhook_before(_INCOMING_EVENT, handle_incoming)
            if vars(adapter).get("commit_event") is observed_commit:
                if old_commit_override is None:
                    del adapter.commit_event
                else:
                    adapter.commit_event = old_commit_override
            if vars(api).get("call_action") is guarded_action:
                if old_override is None:
                    del api.call_action
                else:
                    api.call_action = old_override

        self._attached[platform_id] = (adapter, detach)

    def create_replay(
        self, platform_id: str, message: AstrBotMessage, routing=None
    ) -> AstrMessageEvent:
        if (
            message.type != MessageType.FRIEND_MESSAGE
            or message.session_id != message.sender.user_id
        ):
            raise ValueError("补答消息必须保留原 QQ 私聊会话及发送人。")
        adapter = self._adapter(platform_id)
        raw = getattr(message, "raw_message", None)
        raw = Event(raw or {})
        raw.update(
            post_type="message",
            message_type="private",
            self_id=int(message.self_id),
            user_id=int(message.sender.user_id),
            message_id=int(message.message_id),
            time=message.timestamp,
        )
        message.raw_message = raw
        event = adapter.create_event(message)
        event.set_extra(_REPLAY_EXTRA, True)
        return event

    def enqueue(self, event: AstrMessageEvent) -> None:
        try:
            adapter = self._adapter(event.get_platform_id())
            if adapter.get_client() is not event.bot:
                raise RuntimeError("QQ 适配器已重载，需要重新创建补答事件。")
            adapter.commit_event(event)
        except Exception:
            event.cleanup_temporary_local_files()
            raise

    def _adapter(self, platform_id: str) -> AiocqhttpAdapter:
        if self._closed:
            raise RuntimeError("QQ 补答已停止。")
        adapter = self.context.get_platform_inst(platform_id)
        if not isinstance(adapter, AiocqhttpAdapter):
            raise RuntimeError(f"QQ 适配器 {platform_id} 当前不可用。")
        self.attach(adapter)
        return adapter
