"""Coordinate replies and proactive turns for an opted-in private chat."""

import asyncio
import hashlib
import inspect
import json
import random
import re
import time
from contextlib import aclosing
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.pipeline.content_safety_check.strategies.strategy import (
    StrategySelector,
)
from astrbot.core.star.filter.custom_filter import CustomFilter
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.utils.session_lock import session_lock_manager

from ...runtime import (
    QQ_OFFICIAL_PLATFORMS,
    QQ_PLATFORM,
    component_enabled,
    get_component,
    is_qq_event,
    qq_platform,
)
from .event_snapshot import capture, orphan_media_names, restore, snapshot_media_names
from .official_transport import OfficialTransport, QQReplyRejected
from .proactive.astrbot_bridge import AstrBotBridge
from .proactive.coordinator import Coordinator
from .proactive.ledger import Ledger
from .proactive.linked import LinkedPlugins
from .proactive.settings import Settings
from .proactive.triggers import rest_type
from .qq_transport import QQTransport
from .queue_store import Batch, QueuedMessage, QueueStore, Scope
from .replay_transport import (
    _STATUS_CHAINS,
    PHOTO_ACK,
    DeliveryBlocked,
    ReplayTransport,
)

PLUGIN_ID = "astrbot_plugin_life_busy"
SCHEDULER = "astrbot_plugin_life_scheduler_plus"
LEGACY_PROACTIVE = "astrbot_plugin_life_proactive"
NORMAL_CHAT = "life_private_normal_reply_v1"
WAIT_REPLY_RECOVERY = "life_private_wait_reply_recovery_v1"
NATIVE_BATCH = "life_private_native_batch_v1"
NATIVE_CONFIRMED_SEND = "life_private_native_confirmed_send_v1"
PHOTO = "astrbot_plugin_life_photo"
REPLAY = "life_busy_replay_v1"
MEDIA_GUARD = "life_busy_media_guard_v1"
VOICE_SUPPORTED = "life_busy_voice_supported_v1"
DEFERRED_MEMORY = "life_delivery_v1"
LIVING_MEMORY = "astrbot_plugin_livingmemory"
QUIET_SECONDS = 1.5
POLL_SECONDS = 5
RETRY_SECONDS = 90
MAX_ATTEMPTS = 2
ATTENTION_SECONDS = 45
ATTENTION_CHANCE = 0.35
MEDIA_TOOLS = {"tts_speak", "photo_generate"}
MEMORY_READ_TOOLS = {"recall_long_term_memory", "read_memory_evidence"}
RAW_INGRESS_TIMEOUT = 120
REPLAY_TIMEOUT = 300
PROACTIVE_POLL_SECONDS = 20
MEDIA_SEND = ContextVar("life_busy_media_send", default=None)


class AlreadyAwakePrivate(CustomFilter):
    def filter(self, event, cfg):
        return (
            is_qq_event(event)
            and event.is_at_or_wake_command
            and event.is_private_chat()
            and event.get_self_id() != event.get_sender_id()
        )


@dataclass
class FreeBatch:
    scope: Scope
    wakeup: asyncio.Event
    captured: set[str]


@dataclass
class NativeReply:
    batch: Batch
    captured: set[str]
    epoch: int
    request: ProviderRequest | None = None
    started: bool = False
    sent: bool = False
    superseded: bool = False
    resumable: bool = False
    tools_started: int = 0


@dataclass
class Replay:
    batch: Batch
    epoch: int
    recall_epoch: int
    glance: bool
    frozen: bool = False
    started: bool = False
    superseded: bool = False
    glance_slot_start: float | None = None
    result_ready: bool = False
    conversation_id: str | None = None
    incomplete: bool = False
    memory_epoch: int | None = None
    schedule: dict | None = None


class LifeBusy:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.umo = config["target_session"].strip()
        self.bot_id = ""
        parts = self.umo.split(":", 2)
        self.enabled = (
            config["enabled"]
            and len(parts) == 3
            and bool(parts[0])
            and parts[1] == "FriendMessage"
            and bool(parts[2])
        )
        self.busy_enabled = config.get("busy_enabled", True)
        self.attention_chance = config.get("attention_chance", ATTENTION_CHANCE)
        if type(self.attention_chance) not in (int, float) or not 0 <= self.attention_chance <= 1:
            raise ValueError("attention_chance 必须是 0 到 1 之间的数字")
        self.reply_delay_min = config.get("reply_delay_min_seconds", 8)
        self.reply_delay_max = config.get("reply_delay_max_seconds", 45)
        if (
            type(self.reply_delay_min) is not int
            or type(self.reply_delay_max) is not int
            or not 0 <= self.reply_delay_min <= self.reply_delay_max
        ):
            raise ValueError("补回延迟秒数必须是非负整数，且最短时间不能超过最长时间")
        self.proactive_settings = Settings.from_config(config) if self.enabled else None
        self.proactive: Coordinator | None = None
        self.proactive_worker: asyncio.Task | None = None
        self.chat_ledger: Ledger | None = None
        self.account_conflict = False
        self.platform_id = parts[0] if self.enabled else ""
        self.user_id = parts[2] if self.enabled else ""
        self.session = ""
        self.platform_type = ""
        self.store: QueueStore | None = None
        self.worker: asyncio.Task | None = None
        self.transport: ReplayTransport | None = None
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.closed = False
        self.inflight = False
        self.free_batch: FreeBatch | None = None
        self.native_reply: NativeReply | None = None
        self.resumable_free_scope: Scope | None = None
        self.ready_after = 0.0
        self.busy_scope: Scope | None = None
        self.busy_reply_delay: tuple[Scope, float, float] | None = None
        self.retry_after = 0.0
        self.failed_batch = None
        self.failed_attempts = 0
        self.epoch = 0
        self.recall_epoch = 0
        self.clock_wall = time.time()
        self.clock_monotonic = time.monotonic()
        self.ingress = {}
        self.recalled_media = set()
        self.proactive_ticket = None
        self.proactive_ready = asyncio.Event()
        self.proactive_ready.set()
        self.invalid_schedule_logged = False
        self.memory_version_logged = False

    async def initialize(self):
        self._start_for_qq()

    def _start_for_qq(self):
        if not self.enabled or self.closed:
            return
        adapter = qq_platform(self.context, self.platform_id)
        if adapter is None:
            return
        if self.store is None:
            self._open_records()
        self._attach_platform(adapter)
        if self.transport is None or not self.transport.is_attached(adapter):
            return
        if self.busy_enabled and self.worker is None:
            self.worker = asyncio.create_task(self._run())
        if self.proactive_settings.enabled and self.proactive_worker is None:
            self.proactive = Coordinator(
                self.proactive_settings,
                self.chat_ledger,
                LinkedPlugins(self.context, self),
                AstrBotBridge(self.context, self.umo),
            )
            self.proactive_worker = asyncio.create_task(self._poll_proactive())

    def _open_records(self):
        data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self.media_dir = data_dir / "media"
        self.media_dir.mkdir(exist_ok=True)
        self.store = QueueStore(data_dir / "pending.sqlite3")
        self.store.recover_interrupted_attention()
        retained = {
            name
            for payload in self.store.retained_payloads()
            for name in snapshot_media_names(payload)
        }
        self._remove_media(orphan_media_names(self.media_dir, retained))
        recipient = hashlib.sha256(self.umo.encode()).hexdigest() + ".json"
        self.chat_ledger = Ledger(
            data_dir / "proactive" / recipient,
            data_dir.parent / LEGACY_PROACTIVE / recipient,
        )

    async def on_platform_loaded(self):
        if self.enabled and not self.closed:
            self._start_for_qq()
            self.wakeup.set()

    def _attach_platform(self, adapter):
        platform_name = adapter.meta().name
        if self.transport is None:
            transport_type = (
                OfficialTransport if platform_name in QQ_OFFICIAL_PLATFORMS else QQTransport
            )
            transport = transport_type(
                self.context,
                on_recall=self._on_recall,
                on_incoming=self._on_incoming,
                on_incoming_done=self._on_incoming_done,
            )
            transport.attach(adapter)
            if not transport.is_attached(adapter):
                return
            self.transport = transport
            self.platform_type = platform_name
            bound = self.store.bound_bot(self.umo, platform_name)
            active = self.chat_ledger.data
            active_bot = active["bot_self_id"]
            if active_bot and (
                active["platform_name"] != platform_name
                or (bound and bound != active_bot)
            ):
                self.account_conflict = True
                logger.error("[自然私聊] 暂存与主动消息记录中的机器人账号或平台不一致，已停止接管目标私聊。")
                return
            if not bound and active_bot:
                bound = self.store.bind_bot(self.umo, platform_name, active_bot)
            self._set_bot(bound)
            if (
                bound and platform_name in QQ_OFFICIAL_PLATFORMS
                and (legacy_message_id := active.get("last_seen_message_id"))
            ):
                self.store.remember_qq_inbound(self.session, legacy_message_id)
                del active["last_seen_message_id"]
                self.chat_ledger.save()
        elif platform_name == self.platform_type:
            self.transport.attach(adapter)

    def _set_bot(self, bot_id):
        self.bot_id = bot_id
        # Preserve existing OneBot queue keys. Other adapters include their
        # type as well as the UMO's instance ID and the authenticated account.
        key = (
            (self.umo, bot_id)
            if self.platform_type == "aiocqhttp"
            else (self.platform_type, self.umo, bot_id)
        )
        self.session = json.dumps(key, separators=(",", ":")) if bot_id else ""

    async def _poll_proactive(self):
        while not self.closed:
            try:
                if qq_platform(self.context, self.platform_id) and await component_enabled(
                    self.context, PLUGIN_ID, self.umo
                ):
                    await self.proactive.tick()
            except Exception as error:
                logger.warning("[自然私聊] 主动轮停止，已确认的送达记录保留（%s）。", type(error).__name__)
            await asyncio.sleep(PROACTIVE_POLL_SECONDS)

    async def terminate(self):
        self.closed = True
        if self.proactive:
            self.proactive.close()
        self.proactive_ready.set()
        if self.free_batch:
            self.free_batch.wakeup.set()
        if self.transport is not None:
            self.transport.close()
        workers = [worker for worker in (self.worker, self.proactive_worker) if worker]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self.lock:
            if self.store:
                self.store.close()
                self.store = None

    def _matches(self, umo, bot_id):
        return (
            self.store is not None
            and self.transport is not None
            and (adapter := qq_platform(self.context, self.platform_id)) is not None
            and adapter.meta().name == self.platform_type
            and not self.closed
            and not self.account_conflict
            and bool(self.bot_id)
            and str(umo) == self.umo
            and str(bot_id) == self.bot_id
        )

    def _matches_event(self, event):
        return (
            self.transport is not None
            and event.get_platform_name() == self.platform_type
            and event.get_sender_id() == self.user_id
            and self._matches(event.unified_msg_origin, self.transport.self_id(event))
        )

    async def _current_role(self):
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(self.umo)
        conv = await manager.get_conversation(self.umo, cid) if cid else None
        (
            persona_id,
            persona,
            _,
            _,
        ) = await self.context.persona_manager.resolve_selected_persona(
            umo=self.umo,
            conversation_persona_id=conv.persona_id if conv else None,
            platform_name=self.platform_type,
        )
        return (str(persona_id) if persona_id and persona else ""), (
            str(cid) if conv else ""
        )

    async def _memory_epoch(self) -> int | None:
        meta = self.context.get_registered_star(LIVING_MEMORY)
        enabled = self.context.get_config(self.umo).get("plugin_set", ["*"])
        if (
            not meta
            or not meta.activated
            or not meta.star_cls
            or (enabled != ["*"] and LIVING_MEMORY not in enabled)
            or not await SessionPluginManager.is_plugin_enabled_for_session(
                self.umo, LIVING_MEMORY
            )
        ):
            return None
        get_epoch = getattr(meta.star_cls, "get_delivery_epoch", None)
        if not callable(get_epoch):
            return None
        try:
            epoch = await get_epoch(self.umo)
        except Exception as error:
            logger.warning(
                "[忙碌状态] 未能取得记忆清空代数（%s）。", type(error).__name__
            )
            return None
        return epoch if type(epoch) is int and epoch >= 0 else None

    async def _state(self):
        """Return (scope, phone, slot start) only for a usable matching schedule."""
        resolved = await self._resolve_state()
        return resolved[0] if resolved else None

    async def _resolve_state(self):
        config = self.context.get_config(self.umo)
        provider_settings = config.get("provider_settings", {})
        runner = config.get("agent_runner")
        runner_type = (
            runner.get("runner_type", "local")
            if runner
            else provider_settings.get("agent_runner_type", "local")
        )
        if runner_type != "local":
            return None
        if not provider_settings.get("enable", True):
            return None
        if not await SessionServiceManager.is_llm_enabled_for_session(self.umo):
            return None
        enabled_plugins = config.get("plugin_set", ["*"])
        if not await component_enabled(self.context, PLUGIN_ID, self.umo):
            return None
        if not await component_enabled(self.context, SCHEDULER, self.umo):
            return None
        memory = self.context.get_registered_star(LIVING_MEMORY)
        if (
            memory
            and memory.activated
            and (enabled_plugins == ["*"] or LIVING_MEMORY in enabled_plugins)
            and await SessionPluginManager.is_plugin_enabled_for_session(
                self.umo, LIVING_MEMORY
            )
            and not all(
                callable(getattr(memory.star_cls, method, None))
                for method in ("record_delivered_exchange", "get_delivery_epoch")
            )
        ):
            if not self.memory_version_logged:
                logger.warning(
                    "[忙碌状态] 当前会话的 LivingMemory 尚未适配送达后记忆，暂不接管。"
                )
                self.memory_version_logged = True
            return None
        self.memory_version_logged = False
        scheduler = get_component(self.context, SCHEDULER)
        if scheduler is None:
            return None
        persona, conversation = await self._current_role()
        if not persona:
            return None
        snapshot = scheduler.get_current_state()
        slot = snapshot.get("current_slot") or {}
        # 1.0.3 binds the ready setup to a persona internally; newer builds publish it.
        schedule_persona = snapshot.get("persona_id")
        if schedule_persona is None:
            setup = getattr(getattr(scheduler, "engine", None), "setup", None)
            schedule_persona = setup.state.get("persona_id") if setup else None
        phone = "offline" if rest_type(slot) != "none" else slot.get("phone")
        if persona != schedule_persona or phone not in {"free", "glance", "offline"}:
            return None
        return (
            (
                Scope(self.session, persona, conversation),
                phone,
                datetime.fromisoformat(slot["start"]).timestamp(),
            ),
            snapshot,
        )

    def _pending_reply(self):
        return any(
            not event.get_extra(REPLAY)
            and not event.get_extra("life_photo_completion")
            and self._matches_event(event)
            and self.transport.is_incoming(event)
            for event in active_event_registry._events.get(self.umo, ())
        )

    async def should_pause_proactive(
        self, unified_msg_origin: str, bot_self_id: str
    ) -> bool:
        """Read-only integration gate for the same adapter, chat and bot account."""
        if (
            self.transport is not None
            and not self.closed
            and str(unified_msg_origin) == self.umo
            and (not self.bot_id or self.account_conflict)
        ):
            return True
        if not self._matches(unified_msg_origin, bot_self_id):
            return False
        if (
            self.lock.locked()
            or self.inflight
            or self._pending_ingress()
            or self._pending_reply()
            or self.proactive_ticket
        ):
            return True
        async with self.lock:
            if not self._matches(unified_msg_origin, bot_self_id):
                return False
            state = await self._state()
            if state:
                scope, phone, _ = state
                return (
                    phone != "free"
                    or self._pending_ingress()
                    or self._pending_reply()
                    or (
                        self.busy_enabled
                        and bool(scope.conversation)
                        and self.store.has_pending(scope)
                    )
                )
            return True

    def proactive_epoch(self, unified_msg_origin: str, bot_self_id: str) -> int | None:
        if self._matches(unified_msg_origin, bot_self_id):
            return self.epoch
        return None

    async def claim_proactive_send(
        self, unified_msg_origin: str, bot_self_id: str, expected_epoch: int
    ):
        if not self._matches(unified_msg_origin, bot_self_id):
            return None
        async with self.lock:
            if (
                not self._matches(unified_msg_origin, bot_self_id)
                or self.inflight
                or self.proactive_ticket
            ):
                return None
            state = await self._state()
            if (
                self.closed
                or self.epoch != expected_epoch
                or self._pending_ingress()
                or self._pending_reply()
                or not state
                or state[1] != "free"
                or (
                    self.busy_enabled
                    and state[0].conversation
                    and self.store.has_pending(state[0])
                )
            ):
                return None
            self.proactive_ticket = object()
            self.proactive_ready.clear()
            return self.proactive_ticket

    def release_proactive_send(self, ticket) -> None:
        if ticket is not None and self.proactive_ticket is ticket:
            self.proactive_ticket = None
            self.proactive_ready.set()
            self.wakeup.set()

    def _pending_ingress(self, captured=()):
        now = time.monotonic()
        expired = [
            message_id
            for message_id, (_, _, deadline) in self.ingress.items()
            if deadline <= now
        ]
        for message_id in expired:
            self.ingress.pop(message_id)
        return any(
            (not self.bot_id or key[0] == self.bot_id) and key[1] not in captured
            for key in self.ingress
        ) or bool(
            self.transport
            and self.transport.has_pending_native_ingress(
                self.platform_id, self.bot_id, self.user_id
            )
        )

    def _arrival_time(self):
        # A wall-clock adjustment between two adapter callbacks must not
        # reverse their order. The timestamp still shares the schedule epoch.
        return self.clock_wall + (time.monotonic() - self.clock_monotonic)

    def _on_incoming(self, platform_id, self_id, user_id, message_id):
        if (
            self.closed
            or platform_id != self.platform_id
            or user_id != self.user_id
            or (self.bot_id and self_id != self.bot_id)
        ):
            return
        if (
            self.platform_type in QQ_OFFICIAL_PLATFORMS
            and self.store is not None
            and self.session
            and self.store.seen_qq_inbound(self.session, message_id)
        ):
            return False
        key = (self_id, message_id)
        count, received, _ = self.ingress.get(key, (0, self._arrival_time(), 0))
        self.ingress[key] = (
            count + 1,
            received,
            time.monotonic() + RAW_INGRESS_TIMEOUT,
        )
        self.epoch += 1
        self.wakeup.set()
        if self.free_batch:
            self.free_batch.wakeup.set()

    def _on_incoming_done(self, platform_id, self_id, user_id, message_id):
        if platform_id == self.platform_id and user_id == self.user_id:
            key = (self_id, message_id)
            if incoming := self.ingress.get(key):
                count, received, deadline = incoming
                if count > 1:
                    self.ingress[key] = (count - 1, received, deadline)
                else:
                    self.ingress.pop(key)
            self.wakeup.set()
            if self.free_batch:
                self.free_batch.wakeup.set()

    async def on_private(self, event: AstrMessageEvent):
        if event.get_extra(REPLAY):
            self.transport.activate(event)
            # Buffered chat text is already woken and accepted as conversation.
            # A command newly registered during the wait cannot take ownership.
            event.get_extra("activated_handlers").clear()
            await self.transport.check(event)
            return
        message_id = str(event.message_obj.message_id)
        if (
            not self.transport
            or str(event.unified_msg_origin) != self.umo
            or event.get_platform_name() != self.platform_type
            or event.get_sender_id() != self.user_id
            or event.get_extra("handlers_parsed_params")
            or event.get_extra("life_photo_completion")
            or not self.transport.is_incoming(event)
            or not event.get_messages()
        ):
            return
        async with self.lock:
            if self.closed or self.store is None or self.account_conflict:
                return
            bot_id = self.transport.self_id(event)
            if not bot_id or bot_id == self.user_id:
                return
            if not self.bot_id:
                self._set_bot(self.store.bind_bot(self.umo, self.platform_type, bot_id))
            if not self._matches_event(event):
                return
            official = self.platform_type in QQ_OFFICIAL_PLATFORMS
            free_batch = None
            if official:
                if self.store.seen_qq_inbound(self.session, message_id):
                    event.stop_event()
                    return
                # Commit any busy queue entry before suppressing future copies.
                if self.busy_enabled:
                    await self._route_reply(event, bot_id, message_id)
                self.store.remember_qq_inbound(self.session, message_id)
            if self.proactive is not None:
                text = "".join(
                    part.text for part in event.get_messages() if isinstance(part, Plain)
                )
                self.proactive.message_seen(bot_id, self.platform_type, text)
            if self.busy_enabled:
                if not official:
                    free_batch = await self._route_reply(event, bot_id, message_id)
            else:
                event.set_extra(NORMAL_CHAT, True)
        if free_batch is not None:
            await self._wait_free_batch(event, free_batch)
        elif not self.busy_enabled:
            await self.proactive_ready.wait()

    async def _route_reply(self, event, bot_id, message_id):
        state = await self._state()
        start_free_batch = False
        if state:
            scope, phone, _ = state
            official = self.platform_type in QQ_OFFICIAL_PLATFORMS
            pending = scope.conversation and self.store.has_pending(scope)
            if phone == "free" and not pending and self.busy_scope == scope:
                self.busy_scope = None
            pending_is_free = scope == self.resumable_free_scope or (
                self.native_reply is not None and scope == self.native_reply.batch.scope
            )
            pending_is_free = pending_is_free and self.busy_scope != scope
            if phone == "free" and not (
                self.inflight
                or self.proactive_ticket
                or (pending and (official or not pending_is_free))
            ):
                if (
                    self.platform_type != QQ_PLATFORM
                    or QUIET_SECONDS <= 0
                    or self.free_batch is not None
                ):
                    event.set_extra(NORMAL_CHAT, True)
                    return None
                start_free_batch = True
        else:
            persona, conversation = await self._current_role()
            scope = Scope(self.session, persona, conversation)
            if not persona or not conversation or not self.store.has_pending(scope):
                event.set_extra(NORMAL_CHAT, True)
                return None
        if not scope.conversation:
            manager = self.context.conversation_manager
            await manager.new_conversation(self.umo, self.platform_id)
            state = await self._state()
            if not state or not state[0].conversation:
                return None
            scope = state[0]
            start_free_batch = start_free_batch and state[1] == "free"

        incoming = self.ingress.get((bot_id, message_id))
        received_at = incoming[1] if incoming else self._arrival_time()
        if not start_free_batch:
            event.stop_event()
        payload = await capture(
            event,
            self.media_dir,
            routing=self.transport.routing(event),
            memory_epoch=await self._memory_epoch(),
        )
        added = self.store.add(scope, QueuedMessage(message_id, payload, received_at))
        if self.free_batch is not None and self.free_batch.scope == scope:
            self.free_batch.captured.add(message_id)
            self.free_batch.wakeup.set()
        if not added:
            self._remove_media(snapshot_media_names(payload))
            event.stop_event()
            return None
        if state and state[1] != "free":
            self._observe_busy(scope)
        self.ready_after = time.monotonic() + QUIET_SECONDS
        self.retry_after = 0.0
        if start_free_batch:
            self.free_batch = FreeBatch(scope, asyncio.Event(), {message_id})
            return self.free_batch
        self.wakeup.set()
        return None

    async def _wait_free_batch(self, event, free_batch):
        try:
            while True:
                async with self.lock:
                    resolved = None if self.closed else await self._resolve_state()
                    if not resolved or resolved[0][0] != free_batch.scope:
                        event.stop_event()
                        return
                    state, snapshot = resolved
                    if state[1] != "free":
                        self._observe_busy(free_batch.scope)
                        event.stop_event()
                        return
                    remaining = self.ready_after - time.monotonic()
                    if (
                        remaining <= 0
                        and self.native_reply is None
                        and not self._pending_ingress(free_batch.captured)
                    ):
                        batch = self.store.claim(free_batch.scope)
                        if batch is None or self._defer_busy_reply(state, snapshot, batch):
                            event.stop_event()
                            return
                        self._handoff_free_batch(event, batch, free_batch.captured)
                        return
                    free_batch.wakeup.clear()
                try:
                    await asyncio.wait_for(
                        free_batch.wakeup.wait(),
                        timeout=remaining if remaining > 0 else POLL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            async with self.lock:
                self.free_batch = None
                self.wakeup.set()

    def _handoff_free_batch(self, event, batch, captured):
        combined = self._build_message(batch)
        reply = NativeReply(batch, captured, self.epoch)
        self.native_reply = reply
        self.resumable_free_scope = None
        event.message_obj.message = combined.message
        event.message_obj.message_str = event.message_str = combined.message_str
        if self.platform_type == "aiocqhttp":
            event.message_obj.message_id = combined.message_id
        event.set_extra(NORMAL_CHAT, True)
        event.set_extra(NATIVE_BATCH, reply)
        cleanup = event.cleanup_temporary_local_files
        send = event.send
        send_streaming = event.send_streaming

        async def send_native(message):
            visible = message.type not in _STATUS_CHAINS and bool(message.chain)
            if not await self._check_native_reply(event, reply, sending=visible):
                return
            result = await send(message)
            if visible and result is not False and event._has_send_oper:
                reply.sent = True
            return result

        async def send_native_confirmed(message, send_confirmed):
            async def before_native_submit():
                return await self._check_native_reply(event, reply, sending=True)

            accepted = False
            if await self._check_native_reply(event, reply):
                accepted = await send_confirmed(send, message, before_native_submit)
            if accepted is True:
                reply.sent = True
            elif accepted is False and not reply.started and reply.tools_started:
                # This tool stops before sending the notice or starting its work.
                # Any earlier tool still keeps the batch out of automatic replay.
                reply.tools_started -= 1
            return accepted

        async def stream_native(generator, use_fallback=False):
            async with aclosing(generator):
                if not await self._check_native_reply(event, reply):
                    return

                async def guarded_chunks():
                    async for message in generator:
                        visible = message.type not in _STATUS_CHAINS and bool(message.chain)
                        if not await self._check_native_reply(event, reply, sending=visible):
                            return
                        yield message

                await send_streaming(guarded_chunks(), use_fallback)
                if reply.started and event._has_send_oper:
                    reply.sent = True

        def finish_native_batch():
            try:
                cleanup()
            finally:
                self.native_reply = None
                self.wakeup.set()
                if self.free_batch:
                    self.free_batch.wakeup.set()
                try:
                    if self.store is None:
                        pass
                    elif reply.sent:
                        self.store.finish_delivery(batch)
                        self._remove_media(
                            name
                            for row in batch.messages
                            for name in snapshot_media_names(row.payload)
                        )
                        if self.proactive is not None:
                            self.proactive.reply_seen(event.get_extra(WAIT_REPLY_RECOVERY))
                    elif not reply.started:
                        if reply.tools_started:
                            # A native tool may already have changed state.
                            self.store.begin_delivery(batch, allow_newer=True)
                        elif reply.resumable:
                            self.resumable_free_scope = batch.scope
                        elif not reply.superseded:
                            self._retry_later(batch)
                except Exception as error:
                    logger.warning("[自然私聊] 空闲批次结算失败（%s）。", type(error).__name__)

        event.send = send_native
        event.set_extra(NATIVE_CONFIRMED_SEND, send_native_confirmed)
        event.send_streaming = stream_native
        event.cleanup_temporary_local_files = finish_native_batch

    async def _check_native_reply(self, event, reply, *, sending=False):
        if reply.started:
            return True
        if reply.superseded:
            return False
        async with self.lock:
            if reply.started:
                return True
            if reply.superseded:
                return False
            resolved = None if self.closed else await self._resolve_state()
            state, snapshot = resolved if resolved else (None, None)
            if state and state[0] == reply.batch.scope and state[1] != "free":
                self._observe_busy(reply.batch.scope)
            same_free_chat = (
                state and state[0] == reply.batch.scope and state[1] == "free"
                and self._matches_event(event)
            )
            waiting = same_free_chat and self._defer_busy_reply(state, snapshot, reply.batch)
            if (
                same_free_chat
                and not waiting
                and self.epoch == reply.epoch
                and not self._pending_ingress(reply.captured)
                and (not sending or self.store.begin_delivery(reply.batch))
            ):
                if sending:
                    reply.started = True
                return True
            reply.superseded = True
            reply.resumable = bool(same_free_chat and not waiting)
            if reply.request is not None:
                reply.request.conversation = None
            event.stop_event()
            return False

    async def _on_recall(self, platform_id, self_id, user_id, message_id):
        if (platform_id, self_id, user_id) != (
            self.platform_id,
            self.bot_id,
            self.user_id,
        ):
            return
        self.recall_epoch += 1
        self.epoch += 1
        async with self.lock:
            if self.closed or not self.store:
                return
            payload = self.store.payload_for_recall(self.session, message_id)
            media = snapshot_media_names(payload) if payload else ()
            if self.store.recall(self.session, message_id, remember_missing=True):
                if self.inflight:
                    self.recalled_media.update(media)
                else:
                    self._remove_media(media)
                self.wakeup.set()
                if self.free_batch:
                    self.free_batch.wakeup.set()

    async def busy_status(self, event: AstrMessageEvent):
        if not self._matches_event(event):
            return
        async with self.lock:
            if self.closed:
                return
            state = await self._state()
            scope = state[0] if state and state[0].conversation else None
            batch = self.store.claim(scope) if scope else None
            current = len(batch.messages) if batch else 0
            old = sum(
                s.session == self.session and s != scope
                for s in self.store.pending_scopes()
            )
            if current or old:
                text = f"当前角色有 {current} 条消息待回复。"
                if old:
                    text += "还有其他聊天记录、角色或旧版本保留的待回消息。"
            else:
                text = "当前没有待回复的忙碌消息。"
            if self.inflight:
                text += "正在处理一轮回复。"
            if (
                batch
                and batch.revision == self.failed_batch
                and self.failed_attempts >= MAX_ATTEMPTS
            ):
                text += "补回暂未送出，收到新消息后会再尝试。"
            uncertain = self.store.uncertain_count(self.session)
            if uncertain:
                text += f"另有 {uncertain} 条原消息的发送结果待确认，已保留。"
        yield event.plain_result(text)

    async def _run(self):
        while not self.closed:
            self.wakeup.clear()
            try:
                await self._try_release()
                self.invalid_schedule_logged = False
            except Exception as exc:
                if not self.invalid_schedule_logged:
                    logger.warning(
                        "[忙碌状态] 暂时无法补回，队列仍保留（%s）。",
                        type(exc).__name__,
                    )
                    self.invalid_schedule_logged = True
                self.retry_after = time.monotonic() + RETRY_SECONDS
            delay = POLL_SECONDS
            remaining = self.ready_after - time.monotonic()
            if remaining > 0:
                delay = min(delay, remaining)
            if self.busy_reply_delay:
                remaining = self.busy_reply_delay[2] - time.monotonic()
                if remaining > 0:
                    delay = min(delay, remaining)
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _observe_busy(self, scope):
        self.busy_scope = scope
        if self.resumable_free_scope == scope:
            self.resumable_free_scope = None
        if self.busy_reply_delay and self.busy_reply_delay[0] == scope:
            self.busy_reply_delay = None

    def _defer_busy_reply(self, state, snapshot, batch):
        scope, _, start = state
        now = time.monotonic()
        previous = snapshot.get("previous_slot")
        backed_up = (
            previous and previous["end"] == snapshot["current_slot"]["start"]
            and (previous["phone"] != "free" or rest_type(previous) != "none")
            and batch.messages[0].received_at < start
        )
        if (self.busy_scope == scope or backed_up) and (
            not self.busy_reply_delay or self.busy_reply_delay[:2] != (scope, start)
        ):
            delay = (
                random.uniform(self.reply_delay_min, self.reply_delay_max)
                if self.reply_delay_max else 0
            )
            self.busy_reply_delay = (scope, start, now + delay)
            if self.busy_scope == scope:
                self.busy_scope = None
        return bool(
            self.busy_reply_delay and self.busy_reply_delay[0] == scope
            and now < self.busy_reply_delay[2]
        )

    async def _try_release(self):
        if time.monotonic() < max(self.ready_after, self.retry_after):
            return
        async with self.lock:
            if (
                not self.session
                or qq_platform(self.context, self.platform_id) is None
                or not self.transport.can_deliver(
                    self.platform_id, self.bot_id, self.user_id
                )
                or self.proactive_ticket
                or self.free_batch
                or self.native_reply
                or self._pending_ingress()
                or self._pending_reply()
                or not self.store.has_pending_for_session(self.session)
            ):
                return
            resolved = await self._resolve_state()
            if not resolved:
                return
            (scope, phone, start), snapshot = resolved
            if phone != "free" and self.store.has_pending(scope):
                self._observe_busy(scope)
            if (
                phone == "offline"
                or not scope.conversation
                or not (batch := self.store.claim(scope))
            ):
                return
            if self.platform_type in QQ_OFFICIAL_PLATFORMS and self.store.waiting_for_qq_reply(batch):
                return
            if phone == "free" and self._defer_busy_reply((scope, phone, start), snapshot, batch):
                return
            self.resumable_free_scope = None
            if batch.revision != self.failed_batch:
                self.failed_attempts = 0
            elif self.failed_attempts >= MAX_ATTEMPTS:
                return
            if phone == "glance" and (
                not self.store.try_attention(
                    scope, max(start, time.time() - ATTENTION_SECONDS)
                )
                or random.random() >= snapshot.get("attention_chance", self.attention_chance)
            ):
                return
            epoch = await self._memory_epoch()
            snapshots = [json.loads(row.payload) for row in batch.messages]
            memory_epoch = (
                epoch
                if epoch is not None
                and all(snapshot.get("memory_epoch") == epoch for snapshot in snapshots)
                else None
            )
            replay = Replay(
                batch,
                self.epoch,
                self.recall_epoch,
                phone == "glance",
                conversation_id=scope.conversation,
                memory_epoch=memory_epoch,
                glance_slot_start=start if phone == "glance" else None,
            )
            message = self._build_message(batch)
            event = self.transport.create_replay(
                self.platform_id, message, routing=snapshots[-1].get("routing")
            )
            if event is None:
                return
            event.set_extra(REPLAY, replay)
            event.set_extra(DEFERRED_MEMORY, True)
            event.set_extra(VOICE_SUPPORTED, self.platform_type != "weixin_oc")
            if self.platform_type != "aiocqhttp":
                event.set_extra(PHOTO_ACK, True)
            event.set_extra("enable_streaming", False)
            event.set_extra("enable_reasoning", False)
            event.get_message_outline = lambda: "[忙碌补回]"

            async def media_guard():
                return not delivery.done.done() and await self.transport.check(event)

            event.set_extra(MEDIA_GUARD, media_guard)
            delivery = self.transport.watch(
                event,
                self._before_send,
                validate=self._validate_replay,
                suppress_status=True,
                guard_each_send=replay.glance,
            )
            self.inflight = True
        try:
            self.transport.enqueue(event)
            try:
                await asyncio.wait_for(asyncio.shield(delivery.done), REPLAY_TIMEOUT)
            except asyncio.TimeoutError:
                if replay.started:
                    await asyncio.shield(delivery.done)
                else:
                    self.transport.abandon(event)
                    await asyncio.shield(delivery.done)
                    if not replay.glance:
                        async with self.lock:
                            self._retry_later(batch)
                    return
            confirmed = delivery.sent and not delivery.failed and not replay.incomplete
            async with self.lock:
                reply_rejected = isinstance(delivery.error, QQReplyRejected)
                if reply_rejected:
                    self.store.wait_for_qq_reply(
                        batch, restore=delivery.rejected and not replay.glance,
                    )
                    logger.info("[忙碌状态] QQ 官方拒绝当前回复，等待对方发来新消息。")
                if replay.glance:
                    if delivery.sent_texts:
                        self.store.record_attention(scope, "".join(delivery.sent_texts))
                elif delivery.rejected:
                    if not reply_rejected:
                        self.store.restore_rejected_delivery(batch)
                        self._retry_later(batch)
                elif confirmed:
                    names = [
                        name
                        for row in batch.messages
                        for name in snapshot_media_names(row.payload)
                    ]
                    self.store.finish_delivery(batch)
                    self.store.close_attention(scope)
                    self._remove_media(names)
                elif replay.started:
                    self.store.close_attention(scope)
                    if not reply_rejected:
                        logger.warning("[忙碌状态] 本轮发送结果未确认，原消息已留存。")
                elif not replay.superseded:
                    self._retry_later(batch)
            if delivery.sent:
                user_messages, assistant_text, medium = self._delivered_content(
                    event, replay, delivery
                )
                if not replay.glance and self.proactive is not None:
                    self.proactive.reply_seen(event.get_extra(WAIT_REPLY_RECOVERY))
                if not assistant_text:
                    return
                if not replay.glance and not confirmed and user_messages:
                    try:
                        await self._record_native_partial(
                            replay, user_messages, assistant_text
                        )
                    except Exception as exc:
                        logger.warning(
                            "[忙碌状态] 部分内容已送达，聊天历史提交失败（%s）。",
                            type(exc).__name__,
                        )
                try:
                    await self._record_delivered(
                        event, replay, user_messages, assistant_text, medium
                    )
                except Exception as exc:
                    logger.warning(
                        "[忙碌状态] 回复已确认送达，长期记忆提交失败（%s）。",
                        type(exc).__name__,
                    )
        finally:
            async with self.lock:
                self.inflight = False
                self._remove_media(self.recalled_media)
                self.recalled_media.clear()
                self.wakeup.set()

    def _retry_later(self, batch):
        current = self.store.claim(batch.scope)
        if not current or current.revision != batch.revision:
            return
        self.failed_batch = batch.revision
        self.failed_attempts += 1
        self.retry_after = time.monotonic() + RETRY_SECONDS

    def _remove_media(self, names):
        for name in names:
            try:
                (self.media_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "[忙碌状态] 无法回收一份缓存附件（%s）。", type(exc).__name__
                )

    def _delivered_content(self, event, replay, delivery):
        voice = event.get_extra("life_voice_delivery_v1", [])
        voice_text = [item["text"] for item in voice if item.get("medium") == "voice"]
        image_count = delivery.sent_kinds.count("image")
        has_voice = "record" in delivery.sent_kinds
        parts = list(delivery.sent_texts)
        if has_voice:
            parts.extend(voice_text or ["[发来语音]"])
        if image_count:
            parts.append(f"[发来 {image_count} 张照片]")
        recalled = self.store.recalled_ids(self.session)
        user_messages = (
            []
            if replay.glance
            else [
                self._memory_message(row.payload, recalled)
                for row in replay.batch.messages
                if row.message_id not in recalled
            ]
        )
        medium = "photo" if image_count else "voice" if has_voice else "text"
        return [text for text in user_messages if text], "\n".join(parts), medium

    async def _record_native_partial(self, replay, user_messages, assistant_text):
        if not replay.conversation_id:
            return
        manager = self.context.conversation_manager
        # The watched pipeline has already exited. Use the same conversation
        # lock only for appending this confirmed subset, never around enqueue.
        async with session_lock_manager.acquire_lock(self.umo):
            if await self._current_role() != (
                replay.batch.scope.persona,
                replay.conversation_id,
            ):
                return
            await manager.add_message_pair(
                replay.conversation_id,
                {"role": "user", "content": "\n".join(user_messages)},
                {"role": "assistant", "content": assistant_text},
            )

    async def _record_delivered(
        self, event, replay, user_messages, assistant_text, medium
    ):
        meta = self.context.get_registered_star(LIVING_MEMORY)
        if not meta or not meta.activated or not meta.star_cls:
            return
        commit = getattr(meta.star_cls, "record_delivered_exchange", None)
        if not commit or (
            event.plugins_name is not None and LIVING_MEMORY not in event.plugins_name
        ):
            return
        if not await SessionPluginManager.is_plugin_enabled_for_session(
            self.umo, LIVING_MEMORY
        ):
            return
        if replay.memory_epoch is None:
            logger.info("[忙碌状态] 本批消息没有匹配的记忆清空代数，保留原生聊天历史。")
            return
        batch = replay.batch
        transaction = json.dumps(
            (
                batch.scope.session,
                batch.scope.persona,
                batch.scope.conversation,
                replay.glance,
                [row.message_id for row in batch.messages],
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        transaction_id = "life-busy-" + hashlib.sha256(transaction.encode()).hexdigest()
        await commit(
            event,
            transaction_id=transaction_id,
            expected_persona_id=batch.scope.persona,
            user_messages=user_messages,
            assistant_text=assistant_text,
            medium=medium,
            expected_conversation_id=replay.conversation_id,
            expected_delivery_epoch=replay.memory_epoch,
        )

    @staticmethod
    def _memory_message(payload, recalled):
        parts = []
        for item in json.loads(payload)["components"]:
            if item["kind"] == "plain":
                parts.append(item["text"])
            elif item["kind"] == "reply":
                if item["id"] not in recalled:
                    parts.append("[引用消息]")
            elif item["kind"] == "image":
                parts.append("[图片]")
            elif item["kind"] == "record":
                parts.append("[语音]")
        return "".join(parts).strip()

    def _build_message(self, batch):
        recalled = self.store.recalled_ids(self.session)
        messages = [
            restore(row.payload, self.media_dir, recalled_ids=recalled)
            for row in batch.messages
        ]
        anchor = messages[-1]
        if len(messages) == 1:
            return anchor
        chain = []
        parts = []
        for message in messages:
            if chain:
                chain.append(Plain("\n"))
            chain.extend(message.message)
            text = message.message_str
            # The agent gives only the first quote a full text prompt. Preserve the
            # relationship of every other quote to its own buffered message here.
            quotes = [
                part.message_str
                or "".join(
                    nested.text
                    for nested in part.chain or []
                    if isinstance(nested, Plain)
                )
                for part in message.message
                if isinstance(part, Reply)
            ]
            parts.append(
                "\n".join([*(f"[引用] {quote}" for quote in quotes if quote), text])
            )
        anchor.message = chain
        anchor.message_str = "\n".join(part for part in parts if part)
        return anchor

    async def _valid_locked(self, replay):
        if self.closed or not self.store:
            return False
        if replay.started and not replay.glance:
            return True
        resolved = await self._resolve_state()
        state, snapshot = resolved if resolved else (None, None)
        same_conversation = (
            not replay.conversation_id
            or await self.context.conversation_manager.get_curr_conversation_id(
                self.umo
            )
            == replay.conversation_id
        )
        current = self.store.claim(replay.batch.scope)
        if state and state[0] == replay.batch.scope and state[1] != "free":
            self._observe_busy(replay.batch.scope)
        valid = (
            state is not None
            and same_conversation
            and state[0] == replay.batch.scope
            and state[1] == ("glance" if replay.glance else "free")
            and (not replay.glance or state[2] == replay.glance_slot_start)
            and current is not None
            and (replay.glance or not self._defer_busy_reply(state, snapshot, current))
            and (
                current.revision[: len(replay.batch.revision)] == replay.batch.revision
                if replay.frozen
                else current.revision == replay.batch.revision
            )
            and self.recall_epoch == replay.recall_epoch
            and (
                replay.frozen
                or (self.epoch == replay.epoch and not self._pending_ingress())
            )
        )
        replay.superseded = not valid
        if valid and not replay.glance:
            replay.schedule = snapshot
        return valid

    async def _validate_replay(self, event):
        async with self.lock:
            return await self._valid_locked(event.get_extra(REPLAY))

    async def _before_send(self, event):
        replay = event.get_extra(REPLAY)
        async with self.lock:
            if not (replay.result_ready or MEDIA_SEND.get() is replay):
                return False
            if not await self._valid_locked(replay):
                return False
            if replay.glance and (
                not self._matches_event(event) or not self.transport.is_current_replay(event)
            ):
                return False
            if not replay.glance and not self.store.begin_delivery(
                replay.batch, allow_newer=replay.frozen
            ):
                replay.superseded = True
                return False
            replay.started = True
            return True

    async def check_request(self, event: AstrMessageEvent, request):
        if native := event.get_extra(NATIVE_BATCH):
            native.request = request
            if not request.conversation or request.conversation.cid != native.batch.scope.conversation:
                native.superseded = True
                request.conversation = None
                event.stop_event()
            else:
                await self._check_native_reply(event, native)
        if replay := event.get_extra(REPLAY):
            self.transport.track_request(event, request)
            if (
                not request.conversation
                or request.conversation.cid != replay.conversation_id
            ):
                replay.superseded = True
                self.transport.reject(event)
                return
            await self.transport.check(event)

    @staticmethod
    def _previous_activity(snapshot, batch):
        if not snapshot or not isinstance(
            previous := snapshot.get("previous_slot"), dict
        ):
            return ""
        activity = previous.get("activity")
        if (
            previous.get("phone") not in ("offline", "glance")
            or not isinstance(activity, str)
            or not activity.strip()
        ):
            return ""
        try:
            start = datetime.fromisoformat(previous["start"])
            end = datetime.fromisoformat(previous["end"])
            current_start = datetime.fromisoformat(snapshot["current_slot"]["start"])
            now = datetime.fromisoformat(snapshot["now"])
        except (KeyError, TypeError, ValueError):
            return ""
        if any(value.tzinfo is None for value in (start, end, current_start, now)):
            return ""
        if not start < end == current_start <= now:
            return ""
        start_timestamp, end_timestamp = start.timestamp(), end.timestamp()
        if not any(
            start_timestamp <= row.received_at < end_timestamp for row in batch.messages
        ):
            return ""
        return json.dumps(
            {"活动": activity, "结束时间": previous["end"], "当前时间": snapshot["now"]},
            ensure_ascii=False,
        )

    async def guide_replay(self, event: AstrMessageEvent, request):
        replay = event.get_extra(REPLAY)
        if not replay:
            if event.get_extra(NORMAL_CHAT):
                guidance = (
                    "严格以你的人设性格、当下生活状态与真实脾气自然回应，保持角色自身的主体性与说话节奏，拒绝客服式顺从或模板化安慰。"
                    "对方更正、撤回或说已经解决的事，按最新意思回应。"
                    "聊天已经自然收尾时，按你原本的性格干脆收束即可。"
                )
                guidance += self._wait_reply_guidance(event, request)
                request.extra_user_content_parts.append(TextPart(text=guidance).mark_as_temp())
            return
        if not await self.transport.check(event):
            return
        if request.func_tool:
            photo = get_component(self.context, PHOTO)
            supported_tools = set(MEDIA_TOOLS)
            if not event.get_extra(VOICE_SUPPORTED):
                supported_tools.discard("tts_speak")
            if not photo or not getattr(photo, MEDIA_GUARD, False):
                supported_tools.discard("photo_generate")
            request.func_tool = copy(request.func_tool)
            request.func_tool.tools = [
                self._guard_media_tool(replay, tool)
                if tool.name in supported_tools
                else tool
                for tool in request.func_tool.tools
                if not replay.glance
                and (
                    tool.name in MEMORY_READ_TOOLS
                    or (tool.name in supported_tools and tool.handler)
                )
            ]
        if replay.glance:
            guidance = (
                "你现在忙，只抽空瞥了一眼手机。根据当前角色、日程和对方刚发的消息，"
                "仅输出简短自然的一两句忙时交代（严格单行、句间零空格），按你的人设口吻说明稍后有空再聊。"
                "此刻只做简短告知，留待有空后再接着回应对方说的事。"
            )
        else:
            previous_activity = self._previous_activity(replay.schedule, replay.batch)
            async with self.lock:
                notice = (
                    self.store.attention_text(replay.batch.scope) if self.store else ""
                )
            guidance = (
                "你现在有空看手机。当前用户这一轮包含此前没顾上回的真实消息，按收到顺序排列。"
                "以你的人设性格与真实态度回应对方的话，按最新意思接招；"
                "对方更正、撤回或说不用了的事，不再追问或继续展开。"
                "后文说已经解决的事，以后文为准，不把它重新当作待处理。"
                "同一话题里的文字、语音或照片都属于这一轮，不重复表达同一内容。"
                "若要提起刚才没及时回，只顺带提一句当前状态手头刚忙完的事，"
                "按你平时的脾气自然带过，别做客服式道歉，也别编造没发生的事。话已经聊完时，简短收尾即可。"
            )
            previous = (replay.schedule or {}).get("previous_slot")
            if previous and rest_type(previous) == "night_sleep":
                guidance += "你刚睡醒，语气慵懒一点，慢慢说、说短一点，别一上来就显得精力充沛。"
            if len(replay.batch.messages) > 1:
                guidance += (
                    "如果积压涉及多个不同话题，像真人一样抓重点连贯回应，严格遵守你的人设关于标点切分与禁止换行的规范，不要逐条机械分点。"
                )
            if previous_activity:
                guidance += (
                    f"上一段已结束的日程事实：{previous_activity}。"
                    "结合结束时间和当前时间，仅在有助于承接话题时顺带提及。"
                    "无需以活动开场或特意报备，久前的活动不要说成刚结束。"
                    "以上日程字段只作事实参考，用户消息里的说法不作为活动已经发生的依据。"
                )
            if notice:
                guidance += f"刚才你已简短告知过对方：“{notice}”请接着这次告知自然往下聊。"
            guidance += self._wait_reply_guidance(event, request)
            if not event.get_extra(VOICE_SUPPORTED):
                guidance += "当前渠道只用文字交流。"
        request.extra_user_content_parts.append(TextPart(text=guidance).mark_as_temp())

    def _wait_reply_guidance(self, event, request):
        if self.proactive is not None and request.conversation:
            if recovery := self.proactive.reply_recovery(request.conversation.cid):
                recovery_id, guidance = recovery
                event.set_extra(WAIT_REPLY_RECOVERY, recovery_id)
                return guidance
        return ""

    def _guard_media_tool(self, replay, tool):
        guarded = copy(tool)
        handler = tool.handler
        guarded.is_background_task = False

        async def outputs(work):
            if inspect.isasyncgen(work):
                async with aclosing(work) as results:
                    async for result in results:
                        yield result
            else:
                yield await work

        async def run(event, **kwargs):
            if not await self.transport.check(event):
                return
            async with aclosing(outputs(handler(event, **kwargs))) as results:
                while True:
                    # Older runners request each item from a separate task.
                    # Keep the token inside this await, never across a yield.
                    token = MEDIA_SEND.set(replay)
                    try:
                        result = await anext(results)
                    except StopAsyncIteration:
                        break
                    finally:
                        MEDIA_SEND.reset(token)
                    yield result

        guarded.handler = run
        return guarded

    async def check_tool(self, event: AstrMessageEvent, tool, tool_args):
        if (native := event.get_extra(NATIVE_BATCH)) and await self._check_native_reply(event, native):
            native.tools_started += 1
        if event.get_extra(REPLAY):
            await self.transport.check(event)

    async def check_response(self, event: AstrMessageEvent, response):
        if native := event.get_extra(NATIVE_BATCH):
            await self._check_native_reply(event, native)
        replay = event.get_extra(REPLAY)
        if not replay:
            return
        # Older AstrBot result decorators read this event field even when
        # enable_reasoning is False. The runner has already populated it.
        event.set_extra("_llm_reasoning_content", None)
        if response is None or not await self.transport.check(event):
            return
        if not replay.glance:
            if response.role == "assistant" and (
                response.completion_text or response.result_chain
            ):
                async with self.lock:
                    if await self._valid_locked(replay):
                        replay.frozen = True
                    else:
                        self.transport.reject(event)
            return
        try:
            text = " ".join((response.completion_text or "").split())
            if re.search(r"[A-Za-z\u00c0-\u024f]", text):
                text = re.sub(
                    r"(?<=[\u3400-\u9fff，。？！~…]) +| +(?=[\u3400-\u9fff，。？！~…])",
                    "",
                    text,
                )
            else:
                text = text.replace(" ", "")
            if response.role == "assistant" and 0 < len(text) <= 60:
                safety = self.context.get_config(self.umo)["content_safety"]
                if (
                    safety["also_use_in_response"]
                    and not StrategySelector(safety).check(text)[0]
                ):
                    return
                parts = [text]
                if "，" not in text and "," not in text:
                    parts = re.findall(r".*?[~。？！…]+|.+$", text)
                replay.result_ready = True
                for index, part in enumerate(parts):
                    if index:
                        await asyncio.sleep(1)
                    if not await event.send(MessageChain().message(part)):
                        break
        except DeliveryBlocked:
            return
        finally:
            # Keep the pending messages for the full turn and skip core history.
            event.stop_event()

    async def check_result(self, event: AstrMessageEvent):
        if native := event.get_extra(NATIVE_BATCH):
            await self._check_native_reply(event, native)
        replay = event.get_extra(REPLAY)
        if not replay:
            return
        result = event.get_result()
        if not result or result.result_content_type != ResultContentType.LLM_RESULT:
            replay.incomplete = True
            self.transport.reject(event)
            return
        replay.result_ready = await self.transport.check(event)
        if replay.result_ready and not event.get_extra(VOICE_SUPPORTED):
            # AstrBot 4.29 gates automatic TTS on LLM_RESULT. Keep the
            # genuine text/chain and normal history, without synthesizing.
            result.set_result_content_type(ResultContentType.GENERAL_RESULT)


    async def observe_normal_reply(self, event: AstrMessageEvent):
        if (
            self.proactive is not None
            and event.get_extra(NORMAL_CHAT)
            and not event.get_extra(NATIVE_BATCH)
            and event._has_send_oper
            and self._matches_event(event)
        ):
            self.proactive.reply_seen(event.get_extra(WAIT_REPLY_RECOVERY))
