"""Recent private-chat lines from AstrBot's platform message history (item 17).

AstrBot 4.29 only stores *group* messages in ``platform_message_history``.
For the target private chats this plugin writes both sides itself, using the
same table and row format, so the proactive prompt sees the real chat flow.
When the table has no rows yet it falls back to the LLM conversation history.
"""

from __future__ import annotations

from datetime import datetime, timezone

from astrbot.api import logger

from ..common.compat import disabled_once
from ..common.text import strip_media_markers

MAX_ROWS = 200
_PLACEHOLDERS = {
    "[Image]": "[图片]",
    "[Record]": "[语音]",
    "[Video]": "[视频]",
    "[File]": "[文件]",
    "[Face]": "[表情]",
    "[Forward]": "[聊天记录]",
}


def _when(value: datetime, now: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(now.tzinfo) if now.tzinfo else value


def row_text(content) -> str:
    if not isinstance(content, dict):
        return ""
    parts = []
    for part in content.get("message", []):
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "plain":
            text = str(part.get("text", ""))
            parts.append(_PLACEHOLDERS.get(text, text))
        elif kind == "at":
            parts.append(f"@{part.get('name') or part.get('user_id', '')}")
        elif kind == "reply":
            quoted = str(part.get("text", "")).strip()
            parts.append(f"[回复：{quoted[:40]}]" if quoted else "[回复]")
    return " ".join(" ".join(parts).split())


def format_lines(rows, now: datetime, max_chars: int = 300) -> list[str]:
    """``HH:MM 对方: text`` per row, oldest first; month-day added on other days."""
    lines = []
    for row in rows:
        text = row_text(row.content)
        if not text:
            continue
        when = _when(row.created_at, now)
        stamp = when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%m-%d %H:%M")
        speaker = "你" if row.content.get("type") == "bot" else "对方"
        lines.append(f"{stamp} {speaker}: {text[:max_chars]}")
    return lines


class PlatformHistory:
    def __init__(self, context):
        self.context = context
        self._fallback_logged: set[str] = set()
        self._unsupported = False

    @property
    def _manager(self):
        if self._unsupported:
            return None
        return getattr(self.context, "message_history_manager", None)

    def _write_failed(self, error: Exception) -> None:
        if isinstance(error, (TypeError, AttributeError)):
            # Older AstrBot without these parameters: stop trying, log once.
            self._unsupported = True
            disabled_once("写入私聊平台消息记录", f"当前 AstrBot 的消息记录接口不兼容（{type(error).__name__}）")
            return
        logger.warning("[自然私聊] 写入平台消息记录失败：%s", type(error).__name__)

    async def record_incoming(self, event) -> None:
        manager = self._manager
        if manager is None or event.get_extra("_current_platform_message_history_id"):
            return  # AstrBot already stored it (group history enabled).
        try:
            from astrbot.api.event import MessageChain

            await manager.insert_message_chain(
                platform_id=event.get_platform_id(),
                user_id=event.unified_msg_origin,
                message_chain=MessageChain(chain=list(event.get_messages())),
                role="user",
                sender_id=event.get_sender_id(),
                sender_name=event.get_sender_name(),
                max_messages=MAX_ROWS,
            )
        except Exception as error:
            self._write_failed(error)

    async def record_outgoing(self, umo: str, platform_id: str, text: str, self_id: str = "") -> None:
        manager = self._manager
        text = strip_media_markers(text or "").strip()
        if manager is None or not text:
            return
        try:
            await manager.insert(
                platform_id=platform_id,
                user_id=umo,
                content={"type": "bot", "message": [{"type": "plain", "text": text}]},
                sender_id=self_id or "bot",
                sender_name="bot",
                max_messages=MAX_ROWS,
            )
        except Exception as error:
            self._write_failed(error)

    async def recent_lines(self, umo: str, platform_id: str, limit: int, now: datetime) -> list[str] | None:
        """Last ``limit`` lines, or None when there is no platform history to use."""
        if limit <= 0:
            return []
        manager = self._manager
        rows = []
        if manager is not None:
            try:
                rows = await manager.get(platform_id=platform_id, user_id=umo, page=1, page_size=limit)
            except Exception as error:
                logger.warning("[自然私聊] 读取平台消息记录失败：%s", type(error).__name__)
                rows = []
        lines = format_lines(rows, now)
        if lines:
            return lines
        if umo not in self._fallback_logged:
            self._fallback_logged.add(umo)
            logger.info("[自然私聊] %s 还没有平台消息记录，主动消息暂时参考对话历史。", umo)
        return None
