import json
import os
from datetime import date, timedelta
from pathlib import Path


class Ledger:
    """Atomic, per-recipient journal for actual deliveries and uncertain sends."""

    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = path
        migrated = not path.exists() and legacy_path is not None and legacy_path.is_file()
        if path.exists() or migrated:
            source = legacy_path if migrated else path
            self.data = json.loads(source.read_text(encoding="utf-8-sig"))
        else:
            self.data = {
                "last_seen": "",
                "bot_self_id": "",
                "message_version": 0,
                "last_attempt": "",
                "unanswered": 0,
                "completed": {},
                "days": {},
                "pending": None,
            }

        # Existing 0.1.0 bindings were exclusively OneBot. Never silently
        # carry them across an adapter change under the same instance ID.
        self.data.setdefault("platform_name", "aiocqhttp" if self.data["bot_self_id"] else "")
        self.data.setdefault("unmatched_history", {})
        self.data.setdefault("considered", {})
        self.data.setdefault("last_reply", "")
        self.data.setdefault("last_proactive", self.data["last_attempt"])
        self.data.setdefault("hold", "")
        self.data.setdefault("hold_until", "")
        self.data.setdefault("hold_started", "")
        self.data.setdefault("waiting_reply", None)
        self.data.setdefault("blocked_after_message", None)
        if migrated:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def day(self, key):
        return self.data["days"].get(key, {"rounds": 0, "photos": 0})

    def reserve(self, turn):
        today = date.fromisoformat(turn["day"])
        keep_since = (today - timedelta(days=7)).isoformat()
        self.data["days"] = {
            key: value for key, value in self.data["days"].items() if key >= keep_since
        }
        self.data["completed"] = {
            key: value for key, value in self.data["completed"].items() if value >= keep_since
        }
        self.data["considered"] = {
            key: value for key, value in self.data["considered"].items() if value >= keep_since
        }
        self.data["considered"][turn["key"]] = turn["day"]
        self.data["last_attempt"] = turn["started_at"]
        self.data["pending"] = turn
        self.save()

    def message_seen(self, now, bot_self_id, platform_name, hold="", hold_until=""):
        bound = self.data["bot_self_id"]
        if bound and (bound, self.data["platform_name"]) != (bot_self_id, platform_name):
            return
        self.data["bot_self_id"] = bot_self_id
        self.data["platform_name"] = platform_name
        self.data["last_seen"] = now.isoformat()
        self.data["message_version"] += 1
        self.data["unanswered"] = 0
        if self.data["waiting_reply"] and not self.data["waiting_reply"]["applied"]:
            self.data["waiting_reply"] = None
        if hold != self.data["hold"] or hold_until != self.data["hold_until"]:
            self.data["hold_started"] = now.isoformat() if hold else ""
        self.data["hold"] = hold
        self.data["hold_until"] = hold_until
        self.save()

    def reply_seen(self, now, recovery_id=None):
        self.data["last_reply"] = now.isoformat()
        if (waiting := self.data["waiting_reply"]) and waiting["id"] == recovery_id:
            self.data["waiting_reply"] = None
        self.save()

    def archive_unmatched(self, turn):
        self.data["unmatched_history"][turn["id"]] = {
            "conversation_id": turn["conversation_id"],
            "persona_id": turn["persona_id"],
            "delivered_at": turn["delivered_at"],
            "deliveries": turn["deliveries"],
            "history_parts_recorded": turn["recorded"],
        }
        self.data["pending"] = None
        self.save()

    def start_send(self, turn):
        turn["send_in_flight"] = True
        self.save()

    def finish_send(self, turn):
        turn["send_in_flight"] = False
        self.save()

    def wait_for_message(self, turn):
        self.data["blocked_after_message"] = turn["message_version"]
        self.finish_send(turn)

    def unknown_send(self, turn, now):
        self.data["completed"][turn["key"]] = turn["day"]
        self.data["last_attempt"] = now.isoformat()
        self.data["last_proactive"] = now.isoformat()
        self.finish_send(turn)

    def accept(self, turn, deliveries, actual_day, waiting_reply=None):
        if not deliveries:
            return
        day = self.data["days"].setdefault(actual_day, {"rounds": 0, "photos": 0})
        if not turn["deliveries"]:
            day["rounds"] += 1
            self.data["last_proactive"] = turn["delivered_at"]
            if self.data["message_version"] == turn["message_version"]:
                self.data["unanswered"] += 1
                if waiting_reply is not None:
                    self.data["waiting_reply"] = waiting_reply
        day["photos"] += sum(item["medium"] == "photo" for item in deliveries)
        self.data["completed"][turn["key"]] = turn["day"]
        turn["send_in_flight"] = False
        turn["deliveries"].extend(deliveries)
        self.data["last_attempt"] = turn["delivered_at"]
        self.save()
