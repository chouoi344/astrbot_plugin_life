"""Proactive messages and reply guidance for the opted-in private chats.

v1.4.0 removed the busy queue and delayed replay (item 5): replies are always
handled by AstrBot right away. What stays here:

* per-target proactive messages (``proactive/``), one ledger per target;
* the bot account each target is bound to;
* noticing incoming messages so a proactive send never overlaps a reply;
* recording the private chat into AstrBot's platform message history;
* facts for the reply after unanswered proactive messages (item 3).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.custom_filter import CustomFilter
from astrbot.core.utils.active_event_registry import active_event_registry

from ...runtime import QQ_OFFICIAL_PLATFORMS, component_enabled, is_qq_event, qq_platform
from ..common.compat import disabled_once
from ..common.contracts import NORMAL_CHAT, PHOTO_COMPLETION, PROACTIVE_DELIVERY, WAIT_REPLY_FACTS
from ..common.text import media_side_note, strip_media_markers
from .history import PlatformHistory
from .ingress import IngressWatcher
from .proactive.astrbot_bridge import AstrBotBridge
from .proactive.coordinator import Coordinator, wait_reply_facts
from .proactive.ledger import Ledger
from .proactive.linked import LinkedPlugins
from .proactive.settings import Settings
from .proactive.triggers import hold_active, hold_attempts, hold_status

PLUGIN_ID = "astrbot_plugin_life_busy"
LEGACY_PROACTIVE = "astrbot_plugin_life_proactive"
PROACTIVE_POLL_SECONDS = 20
NORMAL_GUIDANCE = (
    "以你的人设性格、当下生活状态与真实脾气自然回应，保持角色自己的主体性与说话节奏，不要客服式顺从或模板化安慰。"
    "对方更正、撤回或说已经解决的事，按最新意思回应。聊天已经自然收尾时，按你原本的性格干脆收束即可。"
)


class AlreadyAwakePrivate(CustomFilter):
    def filter(self, event, cfg):
        return (
            is_qq_event(event)
            and event.is_at_or_wake_command
            and event.is_private_chat()
            and event.get_self_id() != event.get_sender_id()
        )


def bot_account(context, event) -> str:
    """The bot account that received this event."""
    if event.get_platform_name() in QQ_OFFICIAL_PLATFORMS:
        adapter = context.get_platform_inst(event.get_platform_id())
        appid = getattr(adapter, "appid", "")
        return str(appid) if appid and adapter.get_client() is getattr(event, "bot", None) else ""
    return str(event.get_self_id() or "")


class Target:
    """State for one target private chat."""

    def __init__(self, umo: str, ledger: Ledger):
        self.umo = umo
        self.platform_id, _, self.user_id = umo.split(":", 2)
        self.ledger = ledger
        self.coordinator: Coordinator | None = None
        self.epoch = 0
        self.ticket = None
        self.ready = asyncio.Event()
        self.ready.set()
        self.lock = asyncio.Lock()

    @property
    def bot_id(self) -> str:
        return self.ledger.data["bot_self_id"]

    @property
    def platform_name(self) -> str:
        return self.ledger.data["platform_name"]


class LifeChat:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.settings = Settings.from_config(config)
        for item in self.settings.invalid_targets:
            logger.warning("[自然私聊] 目标私聊“%s”格式不对，应为 平台ID:FriendMessage:对方ID，已忽略。", item)
        self.targets: dict[str, Target] = {}
        self.history = PlatformHistory(context)
        self.linked = LinkedPlugins(context, self)
        self.ingress = IngressWatcher(self._on_arrival)
        self.worker: asyncio.Task | None = None
        self.closed = False

    # -- lifecycle -------------------------------------------------------------
    async def initialize(self):
        if not self.enabled or not self.settings.targets:
            return
        data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        for umo in self.settings.targets:
            name = hashlib.sha256(umo.encode()).hexdigest() + ".json"
            ledger = Ledger(data_dir / "proactive" / name, data_dir.parent / LEGACY_PROACTIVE / name)
            self.targets[umo] = Target(umo, ledger)
        if (data_dir / "pending.sqlite3").exists() or (data_dir / "media").exists():
            logger.info(
                "[自然私聊] v1.4.0 起不再暂存忙时消息，旧的 pending.sqlite3 与 media/ 已不再使用，可以手动删除。"
            )
        self._attach_platforms()
        if self.settings.proactive_enabled and self.worker is None:
            self.worker = asyncio.create_task(self._poll())

    def _attach_platforms(self):
        for target in self.targets.values():
            adapter = qq_platform(self.context, target.platform_id)
            if adapter is None:
                continue
            self.ingress.attach(adapter)
            if target.coordinator is None:
                target.coordinator = Coordinator(
                    self.settings,
                    target.umo,
                    target.ledger,
                    self.linked,
                    AstrBotBridge(self.context, target.umo),
                    self.history,
                )

    async def on_platform_loaded(self):
        if self.enabled and not self.closed:
            self._attach_platforms()

    async def terminate(self):
        self.closed = True
        for target in self.targets.values():
            if target.coordinator:
                target.coordinator.close()
            target.ready.set()
        self.ingress.close()
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    async def _poll(self):
        while not self.closed:
            for target in list(self.targets.values()):
                if self.closed or target.coordinator is None:
                    continue
                try:
                    if qq_platform(self.context, target.platform_id) and await component_enabled(
                        self.context, PLUGIN_ID, target.umo
                    ):
                        await target.coordinator.tick()
                except Exception as error:
                    logger.warning(
                        "[自然私聊] %s 本轮主动消息中止，已确认的送达记录保留（%s: %s）。",
                        target.umo,
                        type(error).__name__,
                        error,
                    )
            await asyncio.sleep(PROACTIVE_POLL_SECONDS)

    # -- matching --------------------------------------------------------------
    def target_for(self, event) -> Target | None:
        target = self.targets.get(str(event.unified_msg_origin))
        if target is None or self.closed or event.get_sender_id() != target.user_id or not event.is_private_chat():
            return None
        return target

    def _matches(self, umo, bot_id) -> Target | None:
        target = self.targets.get(str(umo))
        if (
            target is None
            or self.closed
            or not target.bot_id
            or str(bot_id) != target.bot_id
            or qq_platform(self.context, target.platform_id) is None
        ):
            return None
        return target

    def _on_arrival(self, platform_id, user_id, _):
        for target in self.targets.values():
            if target.platform_id == platform_id and target.user_id == user_id:
                target.epoch += 1

    def _busy(self, target: Target) -> bool:
        """A user message is arriving or AstrBot is still replying to one."""
        if self.ingress.pending(target.platform_id, target.user_id):
            return True
        events = getattr(active_event_registry, "_events", None)  # AstrBot internal
        if not isinstance(events, dict):
            disabled_once("私聊“AstrBot 正在回复”检测", "AstrBot 的 active_event_registry 接口已变化，只按新消息判断")
            return False
        return any(
            not event.get_extra(PROACTIVE_DELIVERY) and event.get_sender_id() == target.user_id
            for event in events.get(target.umo, ())
        )

    # -- proactive gate (used by proactive/linked.py) ---------------------------
    async def should_pause_proactive(self, umo: str, bot_self_id: str) -> bool:
        target = self.targets.get(str(umo))
        if target is None:
            return False
        if self._matches(umo, bot_self_id) is None:
            return True
        return target.lock.locked() or target.ticket is not None or self._busy(target)

    def proactive_epoch(self, umo: str, bot_self_id: str) -> int | None:
        target = self._matches(umo, bot_self_id)
        return target.epoch if target else None

    async def claim_proactive_send(self, umo: str, bot_self_id: str, expected_epoch: int):
        target = self._matches(umo, bot_self_id)
        if target is None:
            return None
        async with target.lock:
            if target.ticket is not None or target.epoch != expected_epoch or self._busy(target):
                return None
            target.ticket = object()
            target.ready.clear()
            return target.ticket

    def release_proactive_send(self, ticket) -> None:
        for target in self.targets.values():
            if ticket is not None and target.ticket is ticket:
                target.ticket = None
                target.ready.set()

    # -- incoming ----------------------------------------------------------------
    async def on_private(self, event: AstrMessageEvent):
        target = self.target_for(event)
        if (
            target is None
            or event.get_extra(PHOTO_COMPLETION)
            or event.get_extra(PROACTIVE_DELIVERY)
            or event.get_extra("handlers_parsed_params")
            or not event.get_messages()
        ):
            return
        self.ingress.handled(target.platform_id, target.user_id)
        bot_id = bot_account(self.context, event)
        if not bot_id or bot_id == target.user_id:
            return
        async with target.lock:
            target.epoch += 1
            if not target.ledger.bind(bot_id, event.get_platform_name()):
                logger.error(
                    "[自然私聊] %s 收到了另一个机器人账号的消息，与已绑定账号不一致，主动消息暂停。"
                    "如更换了机器人账号，请在面板里重置该私聊的绑定。",
                    target.umo,
                )
                return
            await self.history.record_incoming(event)
            event.set_extra(NORMAL_CHAT, True)
            if target.coordinator is not None:
                text = "".join(part.text for part in event.get_messages() if isinstance(part, Plain))
                facts = target.coordinator.message_seen(text)
                if facts:
                    event.set_extra(WAIT_REPLY_FACTS, facts)
        # Let a proactive send that already started finish first.
        await target.ready.wait()

    async def guide_reply(self, event: AstrMessageEvent, request):
        if not event.get_extra(NORMAL_CHAT) or self.target_for(event) is None:
            return
        guidance = NORMAL_GUIDANCE
        facts = event.get_extra(WAIT_REPLY_FACTS)
        if facts:
            state = self.linked.state()
            target = self.target_for(event)
            now = target.coordinator._now() if target and target.coordinator else None
            if now is not None:
                guidance += "\n" + wait_reply_facts(facts, state, now)
        request.extra_user_content_parts.append(TextPart(text=guidance).mark_as_temp())

    async def clean_reply_markers(self, event: AstrMessageEvent):
        """Item 15 fallback: drop a copied "[语音]" / "[照片]" prefix from a model reply."""
        if self.target_for(event) is None:
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        for component in result.chain:
            if isinstance(component, Plain):
                component.text = strip_media_markers(component.text)
                break
        result.chain = [item for item in result.chain if not isinstance(item, Plain) or item.text.strip()]

    async def observe_normal_reply(self, event: AstrMessageEvent):
        target = self.target_for(event)
        if target is None or not event.get_extra(NORMAL_CHAT):
            return
        sent = getattr(event, "_has_send_oper", None)  # AstrBot internal
        if not isinstance(sent, bool):
            disabled_once("私聊“记录角色回复”", "AstrBot 的消息事件接口已变化，无法确认回复是否已发出")
            return
        if not sent:
            return
        if target.coordinator is not None:
            target.coordinator.reply_seen()
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        text = "".join(part.text for part in chain if isinstance(part, Plain)).strip()
        if not text and any(type(part).__name__ == "Record" for part in chain):
            text = media_side_note("voice", "")
        if text:
            await self.history.record_outgoing(target.umo, target.platform_id, text, target.bot_id)

    # -- dashboard -----------------------------------------------------------------
    async def reset_binding(self, umo: str) -> bool:
        target = self.targets.get(umo)
        if target is None:
            return False
        async with target.lock:  # Same lock as a proactive tick writing the ledger.
            target.ledger.data["bot_self_id"] = ""
            target.ledger.data["platform_name"] = ""
            target.ledger.save()
        return True

    def status(self) -> list[dict]:
        rows = []
        now = datetime.now(timezone.utc).astimezone()
        for umo in self.settings.targets:
            target = self.targets.get(umo)
            data = target.ledger.data if target else {}
            today = None
            if target and target.coordinator:
                today = target.ledger.day(target.coordinator._now().date().isoformat())
            rows.append(
                {
                    "target": umo,
                    "bound": bool(data.get("bot_self_id")),
                    "platform_ready": qq_platform(self.context, umo.split(":", 1)[0]) is not None,
                    "next_due": data.get("next_due", ""),
                    "waiting_free": bool(data.get("waiting_free_since")),
                    "unanswered": data.get("unanswered", 0),
                    "stopped_for_unanswered": self.settings.unanswered_reached(data.get("unanswered", 0)),
                    "rounds_today": (today or {}).get("rounds", 0),
                    "photos_today": (today or {}).get("photos", 0),
                    "hold": data.get("hold", ""),
                    "hold_started": data.get("hold_started", ""),
                    "hold_waiting": hold_active(data, self.settings.hold_hours, now),
                    "hold_notified": bool(data.get("hold_notified")),
                    "hold_status": hold_status(data, self.settings.hold_hours, now),
                    "hold_given_up": hold_status(data, self.settings.hold_hours, now) == "given_up",
                    "hold_attempts": hold_attempts(data),
                    "hold_retry_at": data.get("hold_retry_at") if isinstance(data.get("hold_retry_at"), str) else "",
                    "last_proactive": data.get("last_proactive", ""),
                    "last_seen": data.get("last_seen", ""),
                }
            )
        return rows
