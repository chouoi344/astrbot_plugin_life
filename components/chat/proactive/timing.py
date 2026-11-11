"""When to look for the next proactive message: random intervals and quiet hours."""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

_SEPARATORS = re.compile(r"[,，、;；\s]+")
_RANGE = re.compile(r"(\d{1,2})\s*[-~～—–到至]\s*(\d{1,2})")


def parse_quiet_hours(text: str) -> tuple[tuple[int, int], ...]:
    """Parse ``"1-7"`` or ``"23-7，12-13"`` into whole-hour ranges.

    Each range covers ``start <= hour < end``; ``start > end`` crosses midnight.
    Commas, Chinese commas and 、 separate ranges. Invalid text raises ValueError.
    """
    ranges = []
    for item in _SEPARATORS.split(str(text or "").strip()):
        if not item:
            continue
        match = _RANGE.fullmatch(item)
        if not match:
            raise ValueError(f"免打扰时段“{item}”格式不对，请按“1-7”或“23-7”这样的整点填写")
        start, end = int(match[1]), int(match[2])
        if not (0 <= start <= 24 and 0 <= end <= 24) or start % 24 == end % 24:
            raise ValueError(f"免打扰时段“{item}”需要是 0 到 24 之间、首尾不同的整点")
        ranges.append((start % 24, end % 24))
    return tuple(ranges)


def in_quiet_hours(now: datetime, ranges) -> bool:
    hour = now.hour
    for start, end in ranges:
        if start < end and start <= hour < end:
            return True
        if start > end and (hour >= start or hour < end):
            return True
    return False


def random_delay(min_minutes: int, max_minutes: int, rng: random.Random | None = None) -> timedelta:
    rng = rng or random
    low, high = sorted((max(1, min_minutes), max(1, max_minutes)))
    return timedelta(seconds=rng.uniform(low * 60, high * 60))


def next_due(now: datetime, min_minutes: int, max_minutes: int, rng=None) -> datetime:
    """Pick the next moment to consider a proactive message, counted from ``now``."""
    return now + random_delay(min_minutes, max_minutes, rng)


def next_day_due(now: datetime, min_minutes: int, max_minutes: int, rng=None) -> datetime:
    """Today's rounds are used up: start counting again from tomorrow's midnight."""
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + random_delay(min_minutes, max_minutes, rng)


def free_grace(rng=None) -> timedelta:
    """Once the character is free again, wait a few minutes instead of the slot edge."""
    rng = rng or random
    return timedelta(seconds=rng.uniform(60, 300))
