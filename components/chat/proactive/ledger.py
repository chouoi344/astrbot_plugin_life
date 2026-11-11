"""Atomic per-chat journal for proactive timing, counters and confirmed deliveries."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from .triggers import hold_attempts

_DEFAULTS = {
    "last_seen": "",
    "bot_self_id": "",
    "platform_name": "",
    "message_version": 0,
    "last_attempt": "",
    "unanswered": 0,
    "unanswered_since": "",
    "last_user_text": "",
    "last_user_at": "",
    "completed": {},
    "days": {},
    "pending": None,
    "unmatched_history": {},
    "considered": {},
    "last_reply": "",
    "last_proactive": "",
    "hold": "",
    "hold_started": "",
    "hold_notified": False,
    "hold_attempts": 0,
    "hold_retry_at": "",
    "blocked_after_message": None,
    "next_due": "",
    "waiting_free_since": "",
}
# Fields of v1.3.11 that are no longer used.
_OBSOLETE = ("waiting_reply", "hold_until")


def _valid_time(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _clean_hold_fields(data: dict) -> bool:
    """Type fallback for the "wait for me" fields; a broken value must not stop the target."""
    clean = {
        "hold": data.get("hold") if data.get("hold") in ("", "user") else "",
        "hold_started": data.get("hold_started") if _valid_time(data.get("hold_started")) else "",
        "hold_notified": data.get("hold_notified") is True,
        "hold_attempts": hold_attempts(data),
        "hold_retry_at": data.get("hold_retry_at") if _valid_time(data.get("hold_retry_at")) else "",
    }
    changed = {key: value for key, value in clean.items() if data.get(key) != value}
    if changed:
        logger.warning("[自然私聊] 主动消息记录里的等待字段格式不对，已改回默认值：%s", "、".join(changed))
        data.update(changed)
    return bool(changed)


class Ledger:
    """One JSON file per target private chat; every change is fsync'ed then renamed."""

    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = path
        source = path if path.exists() else legacy_path if legacy_path and legacy_path.is_file() else None
        data = {}
        if source is not None:
            try:
                data = json.loads(source.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                logger.warning("[自然私聊] 主动消息记录无法读取，已重新开始计数（%s）。", type(error).__name__)
                data = {}
        if not isinstance(data, dict):
            data = {}
        migrated = source is not path and source is not None
        # Existing 0.1.0 bindings were exclusively OneBot.
        if data.get("bot_self_id") and not data.get("platform_name"):
            data["platform_name"] = "aiocqhttp"
        if data.get("hold") == "night":
            # v1.4.0 no longer pauses after "good night" (item 3).
            data["hold"] = ""
            migrated = True
        for key in _OBSOLETE:
            if key in data:
                data.pop(key)
                migrated = True
        for key, value in _DEFAULTS.items():
            if key not in data:
                data[key] = value.copy() if isinstance(value, dict) else value
        migrated |= _clean_hold_fields(data)
        if not data["last_proactive"] and data["last_attempt"]:
            data["last_proactive"] = data["last_attempt"]
        self.data = data
        if migrated:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    # -- counters -----------------------------------------------------------
    def day(self, key: str) -> dict:
        value = self.data["days"].get(key, {})
        return {"rounds": 0, "photos": 0, "miss_you": 0, **value}

    def _prune(self, today: str) -> None:
        keep_since = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
        for name in ("days",):
            self.data[name] = {key: value for key, value in self.data[name].items() if key >= keep_since}
        for name in ("completed", "considered"):
            self.data[name] = {key: value for key, value in self.data[name].items() if value >= keep_since}

    # -- timing -------------------------------------------------------------
    def due(self) -> datetime | None:
        return datetime.fromisoformat(self.data["next_due"]) if self.data["next_due"] else None

    def set_due(self, when: datetime | None, *, save: bool = True) -> None:
        self.data["next_due"] = when.isoformat() if when else ""
        self.data["waiting_free_since"] = ""
        if save:
            self.save()

    # -- incoming -----------------------------------------------------------
    def bind(self, bot_self_id: str, platform_name: str) -> bool:
        bound = self.data["bot_self_id"]
        if bound and (bound, self.data["platform_name"]) != (bot_self_id, platform_name):
            return False
        if not bound:
            self.data["bot_self_id"] = bot_self_id
            self.data["platform_name"] = platform_name
            self.save()
        return True

    def message_seen(self, now: datetime, text: str, hold: str, next_due: datetime) -> dict | None:
        """Record one user message; return facts about unanswered proactive messages."""
        data = self.data
        facts = None
        if data["unanswered"] > 0:
            facts = {
                "count": data["unanswered"],
                "first_sent_at": data["unanswered_since"] or data["last_proactive"],
                "last_sent_at": data["last_proactive"],
                "user_last_before": data["last_user_text"],
                "user_last_before_at": data["last_user_at"],
            }
        data["last_seen"] = now.isoformat()
        data["message_version"] += 1
        data["unanswered"] = 0
        data["unanswered_since"] = ""
        data["last_user_text"] = text[-200:]
        data["last_user_at"] = now.isoformat()
        if hold == "user":
            if data["hold"] != "user":
                data["hold_started"] = now.isoformat()
                self._reset_hold_ask()
            data["hold"] = "user"
        else:
            data["hold"] = ""
            data["hold_started"] = ""
            self._reset_hold_ask()
        self.set_due(next_due, save=False)
        self.save()
        return facts

    def reply_seen(self, now: datetime) -> None:
        self.data["last_reply"] = now.isoformat()
        self.save()

    # -- proactive turns ----------------------------------------------------
    def reserve(self, turn: dict) -> None:
        self._prune(turn["day"])
        for key in turn.get("topics", []):
            self.data["considered"][key] = turn["day"]
        self.data["last_attempt"] = turn["started_at"]
        self.data["pending"] = turn
        self.save()

    def archive_unmatched(self, turn: dict) -> None:
        self.data["unmatched_history"][turn["id"]] = {
            "conversation_id": turn["conversation_id"],
            "persona_id": turn["persona_id"],
            "delivered_at": turn.get("delivered_at", ""),
            "deliveries": turn["deliveries"],
            "history_parts_recorded": turn["recorded"],
        }
        self.data["pending"] = None
        self.save()

    def start_send(self, turn: dict) -> None:
        turn["send_in_flight"] = True
        self.save()

    def finish_send(self, turn: dict) -> None:
        turn["send_in_flight"] = False
        self.save()

    def wait_for_message(self, turn: dict) -> None:
        self.data["blocked_after_message"] = turn["message_version"]
        self.finish_send(turn)

    def _reset_hold_ask(self) -> None:
        self.data["hold_notified"] = False
        self.data["hold_attempts"] = 0
        self.data["hold_retry_at"] = ""

    def hold_attempt(self, now: datetime, retry_after: timedelta) -> int:
        """Count one try at the expired-hold reminder before asking the model (C2)."""
        self.data["hold_attempts"] = hold_attempts(self.data) + 1
        self.data["hold_retry_at"] = (now + retry_after).isoformat()
        self.save()
        return self.data["hold_attempts"]

    def _hold_sent(self, turn: dict) -> None:
        # C2: only a message that went out (or may have) settles the reminder.
        if turn.get("kind") == "hold":
            self.data["hold_notified"] = True

    def unknown_send(self, turn: dict, now: datetime) -> None:
        self._hold_sent(turn)
        self.data["completed"][turn["key"]] = turn["day"]
        self.data["last_attempt"] = now.isoformat()
        self.data["last_proactive"] = now.isoformat()
        self.finish_send(turn)

    def accept(self, turn: dict, deliveries: list, actual_day: str) -> None:
        if not deliveries:
            return
        day = self.data["days"].setdefault(actual_day, {"rounds": 0, "photos": 0, "miss_you": 0})
        day.setdefault("miss_you", 0)
        if not turn["deliveries"]:
            self.data["last_proactive"] = turn["delivered_at"]
            # A promise the user asked for is not counted (item 10 extension).
            if turn.get("counted", True):
                day["rounds"] += 1
                if self.data["message_version"] == turn["message_version"]:
                    if not self.data["unanswered"]:
                        self.data["unanswered_since"] = turn["delivered_at"]
                    self.data["unanswered"] += 1
            if turn.get("miss_you"):
                day["miss_you"] += 1
        day["photos"] += sum(item["medium"] == "photo" for item in deliveries)
        self._hold_sent(turn)
        self.data["completed"][turn["key"]] = turn["day"]
        turn["send_in_flight"] = False
        turn["deliveries"].extend(deliveries)
        self.data["last_attempt"] = turn["delivered_at"]
        self.save()
