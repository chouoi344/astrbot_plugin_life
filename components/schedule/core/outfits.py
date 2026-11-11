"""Pure outfit compatibility, live wardrobe projection and reference binding."""

import copy
import re
import unicodedata
from datetime import date, timedelta

OUTFIT_CATEGORIES = {"outdoor": "外出", "home": "居家", "sleepwear": "睡衣"}
OUTFIT_PARTS = (
    ("overall_style", "整体风格"),
    ("clothing", "服装"),
    ("footwear", "鞋袜"),
    ("accessories", "配饰"),
    ("hair", "发型"),
    ("makeup", "妆容"),
)
_EXTRA_PARTS = (("details", "颜色材质与版型"), ("constraints", "穿戴约束"))


def strip_temporary_prefix(value):
    text = str(value or "").strip()
    return re.sub(r"^(?:临时搭配\s*[:：]\s*)+", "", text).strip()


def format_outfit_description(value):
    if isinstance(value, str):
        return strip_temporary_prefix(value)
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("description"), str) and value["description"].strip():
        return strip_temporary_prefix(value["description"])
    parts = value.get("parts", value)
    if not isinstance(parts, dict):
        return ""
    return "；".join(
        label + "：" + text
        for key, label in OUTFIT_PARTS + _EXTRA_PARTS
        if isinstance(parts.get(key), str) and (text := parts[key].strip())
    )


def description_key(value):
    text = unicodedata.normalize("NFKC", format_outfit_description(value)).casefold()
    labels = "|".join(label for _, label in OUTFIT_PARTS + _EXTRA_PARTS)
    text = re.sub(r"(^|[;。\n])\s*(?:" + labels + r")\s*:\s*", r"\1", text)
    return re.sub(r"[\s;:，,。.!！]+", "", text)


def normalize_outfit(value, kind):
    raw = copy.deepcopy(value) if isinstance(value, dict) else {}
    identifier = str(raw.get("id", "") or "")
    raw.update(
        id=identifier,
        description=format_outfit_description(value),
        temporary=bool(raw.get("temporary", not identifier)) or not identifier,
        category=raw.get("category") or OUTFIT_CATEGORIES.get(kind, "运动"),
    )
    return raw


def _raw_outfit(day, key):
    if key in OUTFIT_CATEGORIES:
        values = day.get("outfits", {})
        if isinstance(values, dict) and key in values:
            return values[key]
        for slot in day.get("actual", []):
            if isinstance(slot, dict) and slot.get("outfit_type", "home") == key and slot.get("outfit"):
                return {"description": slot["outfit"], "id": slot.get("outfit_id", "")}
        return None
    if re.fullmatch(r"sports:\d+", key):
        index = int(key.split(":")[1])
        values = day.get("sport_outfits", [])
        if isinstance(values, list) and index < len(values):
            return values[index]
    return None


def _keys(day):
    yield from OUTFIT_CATEGORIES
    sports = day.get("sport_outfits", [])
    if isinstance(sports, list):
        yield from ("sports:" + str(index) for index in range(len(sports)))


def project_day_outfits(day, entries, image_store=None):
    """Return display rows without mutating configuration or stored day data."""
    day = day if isinstance(day, dict) else {}
    indexed = {
        str(entry.get("id")): (number, entry)
        for number, entry in enumerate(entries or [], 1)
        if isinstance(entry, dict) and entry.get("id")
    }
    result = []
    for key in _keys(day):
        kind = "sports" if key.startswith("sports:") else key
        outfit = normalize_outfit(_raw_outfit(day, key), kind)
        number, entry = indexed.get(outfit["id"], (None, None))
        if entry is not None:
            source, temporary = "wardrobe", False
            description = format_outfit_description(entry) or outfit["description"]
        elif outfit["id"] and not outfit["temporary"]:
            source, temporary, description = "deleted", False, outfit["description"]
        else:
            source, temporary, description = "temporary", True, outfit["description"]
        image_path = ""
        if entry is not None and image_store is not None:
            path_for = getattr(image_store, "_path", None)
            if callable(path_for):
                path = path_for(outfit["id"])
                if path.is_file():
                    image_path = str(path.resolve())
        result.append(
            {
                "key": key,
                "kind": kind,
                "label": OUTFIT_CATEGORIES.get(kind, "运动"),
                "category": (entry or outfit).get("category") or OUTFIT_CATEGORIES.get(kind, "运动"),
                "id": outfit["id"],
                "description": description,
                "temporary": temporary,
                "source": source,
                "source_label": "衣柜·穿搭" + str(number)
                if source == "wardrobe"
                else "已从衣柜删除"
                if source == "deleted"
                else "AI 临时搭配",
                "wardrobe_number": number,
                "image_path": image_path,
                "can_collect": source == "temporary" and bool(description),
            }
        )
    return result


def slot_outfit_key(slot, day=None):
    day = day or {}
    explicit = slot.get("outfit_key", "")
    if explicit in OUTFIT_CATEGORIES or re.fullmatch(r"sports:\d+", str(explicit)):
        return explicit
    identifier = slot.get("outfit_id")
    if identifier:
        for key in _keys(day):
            raw = _raw_outfit(day, key)
            if isinstance(raw, dict) and raw.get("id") == identifier:
                return key
    return slot.get("outfit_type") if slot.get("outfit_type") in OUTFIT_CATEGORIES else "home"


def slot_outfit_label(slot, day=None):
    key = slot_outfit_key(slot, day)
    return "运动服" if key.startswith("sports:") else {"outdoor": "外出服", "home": "居家服", "sleepwear": "睡衣"}[key]


def bind_outfit_reference(day, key, entry):
    """Replace reference fields only; all recorded activities/times stay exact."""
    if key not in OUTFIT_CATEGORIES and not re.fullmatch(r"sports:\d{1,2}", str(key)):
        raise ValueError("没有找到这套今日穿搭")
    result = copy.deepcopy(day)
    kind = "sports" if key.startswith("sports:") else key
    value = normalize_outfit(entry, kind)
    value.update(temporary=False, description=strip_temporary_prefix(value["description"]))
    value.pop("parts", None)
    if kind == "sports":
        index = int(key.split(":")[1])
        sports = result.setdefault("sport_outfits", [])
        while len(sports) <= index:
            sports.append({})
        sports[index] = value
    else:
        result.setdefault("outfits", {})[key] = value
    sequences = [result.get("plan", []), result.get("actual", [])]
    sequences.extend(record.get("slots", []) for record in result.get("changes", []) if isinstance(record, dict))
    for sequence in sequences:
        for slot in sequence if isinstance(sequence, list) else []:
            if isinstance(slot, dict) and slot_outfit_key(slot, day) == key:
                slot.update(outfit_id=value["id"], outfit=value["description"], outfit_key=key)
    return result


def rebase_collected_outfits(candidate, live):
    """A model result must not undo a collection made while it was waiting."""
    result = copy.deepcopy(candidate)
    for key in _keys(live or {}):
        kind = "sports" if key.startswith("sports:") else key
        saved = normalize_outfit(_raw_outfit(live, key), kind)
        pending = normalize_outfit(_raw_outfit(result, key), kind)
        if saved["id"] and not saved["temporary"] and pending["temporary"]:
            result = bind_outfit_reference(result, key, saved)
    return result


def recent_actual_outfits(history, today, no_repeat_days):
    """Recent worn snapshots include temporary outfits and exclude unused plans."""
    cutoff = today - timedelta(days=max(0, no_repeat_days))
    result, seen = [], set()
    for record in history:
        try:
            previous = date.fromisoformat(record.get("date", ""))
        except (ValueError, TypeError):
            continue
        if no_repeat_days <= 0 or not cutoff <= previous < today:
            continue
        for slot in record.get("actual", []):
            if not isinstance(slot, dict):
                continue
            key = slot_outfit_key(slot, record)
            raw = normalize_outfit(_raw_outfit(record, key), "sports" if key.startswith("sports:") else key)
            description = format_outfit_description(slot.get("outfit")) or raw["description"]
            identifier = str(slot.get("outfit_id") or raw["id"])
            signature = (record["date"], identifier, description_key(description))
            if signature in seen or not (description or identifier):
                continue
            seen.add(signature)
            result.append(
                {
                    "date": record["date"],
                    "key": key,
                    "id": identifier,
                    "description": description,
                    "temporary": not bool(identifier) or raw["temporary"],
                }
            )
    return result
