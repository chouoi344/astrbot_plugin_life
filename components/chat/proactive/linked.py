"""Calls from proactive chat into the sibling components and LivingMemory."""

from astrbot.api import logger
from astrbot.core import sp
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from ....runtime import (
    PHOTO_ID,
    SCHEDULE_ID,
    VOICE_ID,
    component_enabled,
    get_component,
)

MEMORY_ID = "astrbot_plugin_livingmemory"
MEMORY_DEGRADED = "当前 LivingMemory 不支持送达后记忆，主动消息不会写入长期记忆"


class LinkedPlugins:
    def __init__(self, context, chat):
        self.context = context
        self.chat = chat
        self._degraded_logged = False

    def state(self):
        scheduler = get_component(self.context, SCHEDULE_ID)
        return scheduler.get_current_state() if scheduler else {}

    def is_owner(self, event):
        scheduler = get_component(self.context, SCHEDULE_ID)
        return scheduler is not None and scheduler.is_owner(event)

    def user_events(self, target_umo):
        scheduler = get_component(self.context, SCHEDULE_ID)
        events = getattr(scheduler, "user_events", None)
        return events(target_umo) if callable(events) else []

    async def care_followed(self, target_umo, event_id):
        scheduler = get_component(self.context, SCHEDULE_ID)
        update = getattr(scheduler, "set_user_event_status", None)
        if callable(update) and event_id:
            await update(target_umo, event_id, "asked")

    async def blocked(self, target_umo, bot_self_id):
        return await self.chat.should_pause_proactive(target_umo, bot_self_id)

    def proactive_scope(self, target_umo, bot_self_id):
        epoch = self.chat.proactive_epoch(target_umo, bot_self_id)
        return (self.chat, epoch) if epoch is not None else None

    def memory(self, target_umo):
        plugins = self.context.get_config(target_umo).get("plugin_set", ["*"])
        if plugins != ["*"] and MEMORY_ID not in plugins:
            return None
        meta = self.context.get_registered_star(MEMORY_ID)
        return meta.star_cls if meta and meta.activated else None

    def memory_degraded(self, target_umo=None) -> bool:
        """LivingMemory is on but cannot take confirmed proactive deliveries (item 4)."""
        memory = self.memory(target_umo) if target_umo else None
        if memory is None:
            meta = self.context.get_registered_star(MEMORY_ID)
            memory = meta.star_cls if meta and meta.activated else None
        return memory is not None and not callable(getattr(memory, "record_delivered_exchange", None))

    async def recall_for(self, target_umo):
        """Recall needs only ``handle_memory_recall``; writing is checked separately."""
        memory = self.memory(target_umo)
        recall = getattr(memory, "handle_memory_recall", None)
        if not callable(recall):
            return None
        session = await sp.get_async(
            scope="umo",
            scope_id=target_umo,
            key="session_service_config",
            default={},
        )
        if not session.get("session_enabled", True):
            return None
        if not await SessionPluginManager.is_plugin_enabled_for_session(target_umo, MEMORY_ID):
            return None
        return recall

    async def memory_epoch(self, target_umo):
        get_epoch = getattr(self.memory(target_umo), "get_delivery_epoch", None)
        return await get_epoch(target_umo) if callable(get_epoch) else None

    async def remember(self, event, **exchange):
        memory = self.memory(event.unified_msg_origin)
        if memory is None:
            return True
        submit = getattr(memory, "record_delivered_exchange", None)
        if not callable(submit):
            if not self._degraded_logged:
                self._degraded_logged = True
                logger.info("[自然私聊] 记忆降级：%s。", MEMORY_DEGRADED)
            return False
        accepted = await submit(event, **exchange)
        if not accepted:
            logger.info("[自然私聊] LivingMemory 当前未接收本轮，原对话记录已保存")
        return accepted

    async def voice(self, target_umo):
        if await component_enabled(self.context, VOICE_ID, target_umo):
            return getattr(get_component(self.context, VOICE_ID), "send_proactive_voice", None)
        return None

    async def photo(self, target_umo):
        if await component_enabled(self.context, PHOTO_ID, target_umo):
            return getattr(get_component(self.context, PHOTO_ID), "send_proactive_photo", None)
        return None

    async def confirm_tell(self, **snapshot):
        confirm = getattr(get_component(self.context, SCHEDULE_ID), "confirm_tell_delivered", None)
        return await confirm(**snapshot) if confirm else False
