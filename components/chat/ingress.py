"""Notice private messages as soon as the adapter receives them.

Proactive chat must not start a send while a user message is still on its way
through AstrBot's pipeline. These hooks only *observe* arrivals; they never
change, delay or replay a message.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from astrbot.api import logger

_TIMEOUT = 120
_ONEBOT_EVENT = "message.private"


class IngressWatcher:
    def __init__(self, on_arrival: Callable[[str, str, str], None] | None = None):
        self._pending: dict[tuple[str, str], float] = {}
        self._detach: dict[str, tuple[object, Callable[[], None]]] = {}
        self._on_arrival = on_arrival
        self.closed = False

    # -- state ---------------------------------------------------------------
    def arrived(self, platform_id: str, user_id: str) -> None:
        if self.closed:
            return
        self._pending[(platform_id, user_id)] = time.monotonic() + _TIMEOUT
        if self._on_arrival is not None:
            self._on_arrival(platform_id, user_id, "")

    def handled(self, platform_id: str, user_id: str) -> None:
        self._pending.pop((platform_id, user_id), None)

    def pending(self, platform_id: str, user_id: str) -> bool:
        deadline = self._pending.get((platform_id, user_id))
        if deadline is None:
            return False
        if deadline <= time.monotonic():
            self._pending.pop((platform_id, user_id), None)
            return False
        return True

    # -- adapters ------------------------------------------------------------
    def attach(self, adapter) -> None:
        if self.closed or adapter is None:
            return
        platform_id = adapter.meta().id
        current = self._detach.get(platform_id)
        if current and current[0] is adapter:
            return
        if current:
            current[1]()
        name = adapter.meta().name
        try:
            if name == "aiocqhttp":
                detach = self._attach_onebot(adapter, platform_id)
            elif name in ("qq_official", "qq_official_webhook"):
                detach = self._attach_official(adapter, platform_id)
            else:
                return
        except Exception as error:
            logger.warning(
                "[自然私聊] 无法监听 %s 的新消息（%s），主动消息仍按管线状态判断。", platform_id, type(error).__name__
            )
            return
        self._detach[platform_id] = (adapter, detach)

    def _attach_onebot(self, adapter, platform_id):
        bot = adapter.get_client()

        async def on_private(raw):
            self.arrived(platform_id, str(raw.get("user_id", "")))

        bot.hook_before(_ONEBOT_EVENT, on_private)
        return lambda: bot.unhook_before(_ONEBOT_EVENT, on_private)

    def _attach_official(self, adapter, platform_id):
        bot = adapter.get_client()
        original = bot.on_c2c_message_create
        previous = vars(bot).get("on_c2c_message_create")

        async def on_c2c(raw):
            author = getattr(getattr(raw, "author", None), "user_openid", "")
            if author:
                self.arrived(platform_id, str(author))
            return await original(raw)

        bot.on_c2c_message_create = on_c2c

        def detach():
            if vars(bot).get("on_c2c_message_create") is on_c2c:
                if previous is None:
                    del bot.on_c2c_message_create
                else:
                    bot.on_c2c_message_create = previous

        return detach

    def close(self) -> None:
        self.closed = True
        for _, detach in self._detach.values():
            try:
                detach()
            except Exception:
                pass
        self._detach.clear()
        self._pending.clear()
