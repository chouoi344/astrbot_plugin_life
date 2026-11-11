"""Things the user said they will do at a set time (item 8), per private chat.

Stored in ``user_events.json`` as ``{umo: [event, ...]}``. An event is
``pending`` until the character asks about it (``asked``) or the user tells
the result first (``done``). Old events are dropped after a week.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .timeline import parse_local

FILE = "user_events.json"
STATUSES = ("pending", "asked", "done")
KEEP_DAYS = 7
MAX_PER_CHAT = 20
MIN_FOLLOW_UP = timedelta(hours=1)
# The model sets the follow-up time; keep it between 1 and 15 hours after the start.
MAX_FOLLOW_UP = timedelta(hours=15)


class UserEvents:
    def __init__(self, store, tz):
        self.store = store
        self.tz = tz

    def _all(self) -> dict:
        data = self.store.read(FILE, {})
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.store.write(FILE, data)

    def list(self, umo: str, now: datetime) -> list[dict]:
        cutoff = now - timedelta(days=KEEP_DAYS)
        rows = []
        for item in self._all().get(umo, []):
            try:
                when = parse_local(item["time"], self.tz)
            except (KeyError, TypeError, ValueError):
                continue
            if when >= cutoff:
                rows.append(item)
        return sorted(rows, key=lambda row: row["time"])

    def add(self, umo: str, raw_items, now: datetime) -> list[dict]:
        """Validate model output and keep new, future events only."""
        if not isinstance(raw_items, list):
            return []
        data = self._all()
        rows = [item for item in data.get(umo, []) if isinstance(item, dict)]
        known = {(item.get("content"), item.get("time", "")[:13]) for item in rows}
        added = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            content = " ".join(str(raw.get("content", "")).split())[:40]
            try:
                when = parse_local(str(raw.get("time", "")), self.tz)
            except (TypeError, ValueError):
                continue
            if not content or when < now - timedelta(hours=1) or when > now + timedelta(days=30):
                continue
            try:
                follow_up = parse_local(str(raw.get("follow_up_at", "")), self.tz)
            except (TypeError, ValueError):
                follow_up = when + timedelta(hours=2)
            follow_up = min(max(follow_up, when + MIN_FOLLOW_UP), when + MAX_FOLLOW_UP)
            key = (content, when.isoformat()[:13])
            if key in known:
                continue
            known.add(key)
            item = {
                "id": uuid.uuid4().hex[:12],
                "content": content,
                "time": when.isoformat(),
                "follow_up_at": follow_up.isoformat(),
                "status": "pending",
                "created_at": now.isoformat(),
            }
            rows.append(item)
            added.append(item)
        if added:
            data[umo] = rows[-MAX_PER_CHAT:]
            self._save(data)
        return added

    def set_status(self, umo: str, event_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError("事件状态无效")
        data = self._all()
        for item in data.get(umo, []):
            if item.get("id") == event_id:
                if item.get("status") == status or item.get("status") == "done":
                    return True
                item["status"] = status
                self._save(data)
                return True
        return False

    def delete(self, umo: str, event_id: str) -> bool:
        data = self._all()
        rows = data.get(umo, [])
        kept = [item for item in rows if item.get("id") != event_id]
        if len(kept) == len(rows):
            return False
        data[umo] = kept
        self._save(data)
        return True
