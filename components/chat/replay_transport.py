"""Delivery lifecycle shared by the supported AstrBot private-chat adapters."""

import asyncio
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import Platform
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context
from astrbot.core.db.po import Conversation

_STATUS_CHAINS = {"tool_call", "reasoning", "agent_stats", "break"}
PHOTO_ACK = "life_photo_ack_v1"
Guard = Callable[[AstrMessageEvent], Awaitable[bool]]
RecallHandler = Callable[[str, str, str, str], Awaitable[None]]
IncomingHandler = Callable[[str, str, str, str], None]


class DeliveryBlocked(RuntimeError):
    """The watched event lost permission to send its buffered reply."""


@dataclass
class _SendReceipt:
    accepted: bool = False


@dataclass
class Delivery:
    event: AstrMessageEvent
    before_send: Guard
    validate: Guard | None
    done: asyncio.Future[None]
    sent: bool = False
    blocked: bool = False
    started: bool = False
    rejected: bool = False
    explicitly_rejected: bool = False
    error: BaseException | None = None
    sent_texts: list[str] = field(default_factory=list)
    sent_kinds: list[str] = field(default_factory=list)
    request: ProviderRequest | None = field(default=None, repr=False)
    original_conversation: Conversation | None = field(default=None, repr=False)
    pipeline_task: asyncio.Task | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    guard_each_send: bool = False

    @property
    def failed(self) -> bool:
        return self.error is not None

    def suppress_history(self) -> None:
        # AstrBot normally saves even stopped events when the agent reports a user abort.
        if self.request is not None:
            if self.request.conversation is not None:
                self.original_conversation = self.request.conversation
            self.request.conversation = None
        self.event.stop_event()

    def stop(self) -> None:
        self.blocked = True
        if self.explicitly_rejected:
            self.event.delivery_status = "failed"
        else:
            cancelled = "cancelled" if self.event.get_extra(PHOTO_ACK) else "stopped"
            self.event.delivery_status = cancelled if self.error is None else "unknown"
        self.suppress_history()

    def cancel_pipeline_from_other_task(self) -> None:
        task = self.pipeline_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def fail_send(
        self, error: BaseException, *, definite_rejection: bool = False
    ) -> None:
        if self.error is not error:
            self.error = error
            self.explicitly_rejected = definite_rejection
            self.rejected = definite_rejection and not self.sent
            self.stop()
            self.cancel_pipeline_from_other_task()


class ReplayTransport:
    def __init__(
        self,
        context: Context,
        *,
        on_recall: RecallHandler,
        on_incoming: IncomingHandler | None = None,
        on_incoming_done: IncomingHandler | None = None,
    ) -> None:
        self.context = context
        self.on_recall = on_recall
        self.on_incoming = on_incoming
        self.on_incoming_done = on_incoming_done
        self._active: ContextVar[Delivery | None] = ContextVar(
            "life_busy_delivery", default=None
        )
        self._receipt: ContextVar[_SendReceipt | None] = ContextVar(
            "life_busy_receipt", default=None
        )
        self._deliveries: dict[AstrMessageEvent, Delivery] = {}
        self._attached: dict[str, tuple[Platform, Callable[[], None]]] = {}
        self._closed = False

    def self_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_self_id())

    def routing(self, event: AstrMessageEvent) -> dict | None:
        return None

    def can_deliver(self, platform_id: str, bot_id: str, user_id: str) -> bool:
        return self.context.get_platform_inst(platform_id) is not None

    def is_attached(self, adapter: Platform) -> bool:
        attached = self._attached.get(adapter.meta().id)
        return bool(attached and attached[0] is adapter)

    def has_pending_native_ingress(
        self, platform_id: str, bot_id: str, user_id: str
    ) -> bool:
        return False

    def is_current_replay(self, event: AstrMessageEvent) -> bool:
        return True

    def observe_cleanup(
        self, event: AstrMessageEvent, source: tuple[str, str, str, str]
    ) -> None:
        if self.on_incoming_done is None:
            return
        cleanup = event.cleanup_temporary_local_files

        def complete_incoming() -> None:
            try:
                cleanup()
            finally:
                if not self._closed:
                    self.on_incoming_done(*source)

        event.cleanup_temporary_local_files = complete_incoming

    async def send_confirmed(
        self,
        delivery: Delivery,
        action: Callable[[], Awaitable],
        *,
        validate_ack: Callable[[object], None],
        parts: Iterable[tuple[str, str]],
        is_rejection: Callable[[Exception], bool] | None = None,
    ):
        """Guard the first send or every send when requested, and count only ACKs."""
        async with delivery.lock:
            if (
                self._closed
                or delivery.blocked
                or delivery.done.done()
                or not self.is_current_replay(delivery.event)
            ):
                delivery.stop()
                raise DeliveryBlocked("这轮补答已结束。")
            if delivery.guard_each_send or not delivery.started:
                try:
                    allowed = await delivery.before_send(delivery.event)
                except Exception as exc:
                    delivery.fail_send(exc)
                    raise
                if not allowed or self._closed:
                    delivery.stop()
                    delivery.cancel_pipeline_from_other_task()
                    raise DeliveryBlocked("收到新消息或忙碌状态已变化。")
                delivery.started = True
            try:
                result = await action()
                validate_ack(result)
            except asyncio.CancelledError as exc:
                delivery.fail_send(exc)
                raise
            except Exception as exc:
                delivery.fail_send(
                    exc,
                    definite_rejection=is_rejection is not None and is_rejection(exc),
                )
                raise
            delivery.sent = True
            if (receipt := self._receipt.get()) is not None:
                receipt.accepted = True
            parts = tuple(parts)
            delivery.sent_kinds.extend(kind for kind, _ in parts)
            text = "".join(text for _, text in parts).strip()
            if text:
                delivery.sent_texts.append(text)
            return result

    def close(self) -> None:
        self._closed = True
        for delivery in self._deliveries.values():
            if not delivery.started:
                delivery.stop()
        if not self._deliveries:
            self._detach_all()

    def _detach_all(self) -> None:
        for _, detach in self._attached.values():
            detach()
        self._attached.clear()

    def watch(
        self,
        event: AstrMessageEvent,
        before_send: Guard,
        *,
        validate: Guard | None = None,
        suppress_status: bool = False,
        guard_each_send: bool = False,
    ) -> Delivery:
        if not event.is_private_chat():
            raise ValueError("只有私聊事件可以注册补答检查。")
        delivery = Delivery(
            event, before_send, validate, asyncio.get_running_loop().create_future(),
            guard_each_send=guard_each_send,
        )
        self._deliveries[event] = delivery
        send = event.send
        cleanup = event.cleanup_temporary_local_files

        async def watched_send(message: MessageChain) -> bool | None:
            strict = event.get_extra(PHOTO_ACK) is True
            if suppress_status and message.type in _STATUS_CHAINS:
                return False
            if (
                self._closed
                or delivery.blocked
                or delivery.done.done()
                or not self.is_current_replay(event)
            ):
                delivery.stop()
                if strict:
                    return None if event.delivery_status == "unknown" else False
                raise DeliveryBlocked("这轮补答已结束。")
            active_token = self._active.set(delivery)
            receipt = _SendReceipt()
            receipt_token = self._receipt.set(receipt)
            try:
                await send(message)
                return receipt.accepted
            except DeliveryBlocked:
                if strict:
                    return None if event.delivery_status == "unknown" else False
                raise
            except Exception as exc:
                delivery.fail_send(exc)
                if strict and delivery.explicitly_rejected:
                    return False
                raise
            finally:
                self._receipt.reset(receipt_token)
                self._active.reset(active_token)

        def watched_cleanup() -> None:
            try:
                cleanup()
            finally:
                error = sys.exc_info()[1]
                if delivery.error is None and not isinstance(error, DeliveryBlocked):
                    delivery.error = error
                self._deliveries.pop(event, None)
                if self._active.get() is delivery:
                    self._active.set(None)
                if not delivery.done.done():
                    delivery.done.set_result(None)
                if self._closed and not self._deliveries:
                    self._detach_all()

        event.send = watched_send
        event.cleanup_temporary_local_files = watched_cleanup
        if suppress_status:
            send_streaming = event.send_streaming

            async def watched_streaming(
                generator: AsyncGenerator[MessageChain, None],
                use_fallback: bool = False,
            ) -> None:
                async def visible_chunks():
                    async for chain in generator:
                        if chain.type not in _STATUS_CHAINS:
                            yield chain

                await send_streaming(visible_chunks(), use_fallback)

            event.send_streaming = watched_streaming
        return delivery

    def activate(self, event: AstrMessageEvent) -> None:
        """Bind the guard to this pipeline and its child tool tasks."""
        if delivery := self._deliveries.get(event):
            delivery.pipeline_task = asyncio.current_task()
            self._active.set(delivery)

    def track_request(self, event: AstrMessageEvent, request: ProviderRequest) -> None:
        if delivery := self._deliveries.get(event):
            delivery.request = request
            if delivery.blocked:
                delivery.suppress_history()

    def reject(self, event: AstrMessageEvent) -> None:
        if (delivery := self._deliveries.get(event)) is not None:
            delivery.stop()

    def abandon(self, event: AstrMessageEvent) -> None:
        """Stop a timed-out replay that has not begun its first OneBot send."""
        if (delivery := self._deliveries.get(event)) is None or delivery.started:
            return
        delivery.stop()
        if delivery.pipeline_task is None:
            # The event bus may drop an event when its selected scheduler is absent.
            event.cleanup_temporary_local_files()
        else:
            delivery.cancel_pipeline_from_other_task()

    async def check(self, event: AstrMessageEvent) -> bool:
        """Check staleness without claiming the first visible send."""
        delivery = self._deliveries.get(event)
        if delivery is None:
            return True
        if self._closed or delivery.blocked:
            delivery.stop()
            return False
        if delivery.started or delivery.validate is None:
            return True
        try:
            allowed = await delivery.validate(event)
        except Exception as exc:
            delivery.error = exc
            delivery.stop()
            raise
        if not allowed:
            delivery.stop()
            return False
        return True
