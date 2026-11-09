"""Configuration-backed wardrobe and weather-aware daily outfit selection."""

from __future__ import annotations

import copy
import html
import random
import re
import uuid
from datetime import date, timedelta

from .outfits import description_key, recent_actual_outfits

SEASONS = ("春", "夏", "秋", "冬")
CATEGORIES = ("外出", "居家", "睡衣", "运动")
COLD_INCOMPATIBLE = ("短袖", "短裙", "短裤", "凉鞋", "吊带", "背心", "比基尼", "露脐")
HOT_INCOMPATIBLE = ("羽绒", "厚毛衣", "羊绒大衣", "棉袄", "皮草", "厚棉服")
OUTFIT_CATEGORIES = {"outdoor": "外出", "home": "居家", "sleepwear": "睡衣"}
EMPTY_WARDROBE_TEXT = "衣柜还是空的。没有衣服时，角色每天会由 AI 临时搭配；在『今日日程』里看到喜欢的，可以一键收进衣柜。也可以点右上角自己添加。"


def season_for_day(day: date) -> str:
    return "春" if 3 <= day.month <= 5 else "夏" if 6 <= day.month <= 8 else "秋" if 9 <= day.month <= 11 else "冬"


def _seasons(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return list(SEASONS)
    return [item for item in SEASONS if item in value] or list(SEASONS)


class WardrobeManager:
    """Keep stable IDs while allowing the panel to reorder and edit entries."""

    def __init__(self, config, save_config=None):
        self.config = config
        self.save_config = save_config
        self._normalize()

    def _save(self):
        save = self.save_config or getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _normalize(self):
        entries = []
        seen_ids = set()
        raw = self.config.get("wardrobe", [])
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description", "") or "").strip()
            # Keep blank panel entries so editing one never renumbers the rest.
            entry_id = str(item.get("id", "") or "")
            if not re.fullmatch(r"wardrobe-[A-Za-z0-9_-]+", entry_id) or entry_id in seen_ids:
                entry_id = "wardrobe-" + uuid.uuid4().hex
            seen_ids.add(entry_id)
            entries.append(
                {
                    "__template_key": "outfit",
                    "id": entry_id,
                    "description": description,
                    "category": item.get("category") if item.get("category") in CATEGORIES else "外出",
                    "seasons": _seasons(item.get("seasons")),
                    "image_upload": [value for value in item.get("image_upload", []) if isinstance(value, str)] if isinstance(item.get("image_upload"), list) else [],
                    "image_processed_hash": str(item.get("image_processed_hash", "") or ""),
                }
            )
        if entries != raw:
            self.config["wardrobe"] = entries
            self._save()
        return entries

    def all(self) -> list[dict]:
        return copy.deepcopy(self._normalize())

    def display_entries(self, *, limit=None) -> list[dict]:
        entries = self.all()
        return entries if limit is None else entries[:limit]

    def resolve(self, query) -> dict:
        text = str(query or "").strip()
        entries = self.all()
        match = re.fullmatch(r"(?:穿搭\s*)?(\d+)", text)
        if match:
            number = int(match.group(1))
            if 1 <= number <= len(entries):
                return entries[number - 1]
        else:
            for entry in entries:
                if text and text in (entry["id"], entry["description"]):
                    return entry
            matches = [entry for entry in entries if text and text in entry["description"]]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError("多套穿搭包含这个描述，请使用明确编号")
        raise ValueError("没有找到对应穿搭，请使用查看衣柜里的编号")

    def add(self, description, category="外出", seasons=None) -> dict:
        description = str(description or "").strip()
        if not description:
            raise ValueError("穿搭描述为空")
        entries = self.all()
        for entry in entries:
            if entry["description"].casefold() == description.casefold():
                return entry
        entry = {
            "__template_key": "outfit",
            "id": "wardrobe-" + uuid.uuid4().hex,
            "description": description,
            "category": category if category in CATEGORIES else "外出",
            "seasons": _seasons(seasons),
            "image_upload": [],
            "image_processed_hash": "",
        }
        entries.append(entry)
        self.config["wardrobe"] = entries
        self._save()
        return copy.deepcopy(entry)

    def update(self, entry_id, description, category=None, seasons=None) -> dict:
        description = str(description or "").strip()
        if not description:
            raise ValueError("穿搭描述为空")
        entries = self.all()
        for entry in entries:
            if entry["id"] == entry_id:
                entry["description"] = description
                if category is not None:
                    entry["category"] = category if category in CATEGORIES else "外出"
                if seasons is not None:
                    entry["seasons"] = _seasons(seasons)
                self.config["wardrobe"] = entries
                self._save()
                return copy.deepcopy(entry)
        raise KeyError(entry_id)

    def remove(self, entry_id) -> dict:
        entries = self.all()
        for index, entry in enumerate(entries):
            if entry["id"] == entry_id:
                removed = entries.pop(index)
                self.config["wardrobe"] = entries
                self._save()
                return removed
        raise KeyError(entry_id)

    def _select_outfits(self, day: date, weather: dict, history: list[dict], no_repeat_days, categories) -> dict:
        recent = recent_actual_outfits(history, day, no_repeat_days)
        recent_ids = {item["id"] for item in recent if item["id"]}
        recent_descriptions = {description_key(item["description"]) for item in recent if item["description"]}
        cutoff = day - timedelta(days=max(0, no_repeat_days))
        for record in history:
            try:
                previous_day = date.fromisoformat(record.get("date", ""))
            except (TypeError, ValueError):
                continue
            if not cutoff <= previous_day < day or no_repeat_days <= 0:
                continue
            # Older summary-only history has no actual slots to inspect.
            if "actual" not in record:
                for outfit in (record.get("outfits") or {}).values():
                    if isinstance(outfit, dict) and outfit.get("id"):
                        recent_ids.add(outfit["id"])
            for slot in record.get("actual", []):
                if isinstance(slot, dict) and slot.get("outfit_id"):
                    recent_ids.add(slot["outfit_id"])

        def temperature_safe(entry):
            text = entry["description"]
            low, high = weather.get("temp_min"), weather.get("temp_max")
            if isinstance(low, (float, int)) and low < 10 and any(term in text for term in COLD_INCOMPATIBLE):
                return False
            if isinstance(high, (float, int)) and high >= 30 and any(term in text for term in HOT_INCOMPATIBLE):
                return False
            return True

        entries, result = self.all(), {}
        season = season_for_day(day)
        for outfit_type, category in categories.items():
            candidates = [
                entry
                for entry in entries
                if entry["category"] == category and entry["description"] and temperature_safe(entry)
            ]
            seasonal = [entry for entry in candidates if season in entry["seasons"]]
            fresh = [entry for entry in seasonal if entry["id"] not in recent_ids and description_key(entry["description"]) not in recent_descriptions]
            pool = fresh or seasonal or candidates
            if pool:
                result[outfit_type] = dict(random.choice(pool), temporary=False)
            else:
                result[outfit_type] = {"id": "", "description": "", "category": category, "temporary": True}
        return result

    def select_outfits(self, day: date, weather: dict, history: list[dict], no_repeat_days=3) -> dict:
        return self._select_outfits(day, weather, history, no_repeat_days, OUTFIT_CATEGORIES)

    def select_sports(self, day: date, weather: dict, history: list[dict], no_repeat_days=3) -> list[dict]:
        result = self._select_outfits(day, weather, history, no_repeat_days, {"sports": "运动"})["sports"]
        return [] if result["temporary"] else [result]

    def format_text(self) -> str:
        entries = self.all()
        return (
            "\n".join(
                f"穿搭{i}｜{entry['category']}｜{''.join(entry['seasons'])}：{entry['description'] or '（待填写）'}"
                for i, entry in enumerate(entries, 1)
            )
            or EMPTY_WARDROBE_TEXT
        )


def wardrobe_html(entries: list[dict], image_store) -> str:
    """Self-contained, escaped HTML for AstrBot's existing HTML renderer."""
    cards = []
    for index, entry in enumerate(entries, 1):
        image_uri = image_store.data_uri(entry["id"]) if image_store else ""
        picture = (
            f'<img src="{image_uri}" alt="穿搭展示图">' if image_uri else '<div class="placeholder">穿搭方案</div>'
        )
        cards.append(
            f'<article>{picture}<section><h2>穿搭{index}</h2><p class="tag">{html.escape(entry.get("category", "外出"))} · {html.escape(" / ".join(entry.get("seasons", SEASONS)))}</p><p>{html.escape(entry.get("description", "（待填写）"))}</p></section></article>'
        )
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><style>
    *{box-sizing:border-box}body{margin:0;padding:32px;width:900px;background:#f4f1ed;color:#34322f;font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif}
    h1{margin:0 0 8px;font-size:32px}.hint{color:#777;margin-bottom:24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
    article{background:#fff;border-radius:16px;overflow:hidden;border:1px solid #e6e1da}img{width:100%;height:280px;object-fit:contain;background:#f8f6f3}.placeholder{height:84px;background:#e5ddd2;color:#7a6f61;display:flex;align-items:center;justify-content:center}
    section{padding:18px}h2{margin:0;font-size:22px}.tag{color:#927a5b;font-size:15px}p{white-space:pre-wrap;line-height:1.7;font-size:17px;overflow-wrap:anywhere}
    </style><h1>我的衣柜</h1><div class="hint">用「重写日程 穿搭N」换上喜欢的搭配 · 用「展示 穿搭N」绑定图片</div><div class="grid">"""
        + "".join(cards)
        + "</div></html>"
    )
