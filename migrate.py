"""Upgrade a saved older configuration in place (hard requirement 2; v1.4.3 service defaults).

AstrBot drops keys that are no longer in the schema when it loads a plugin
config, so every key read here is still declared (invisible) in the schema.
Each step is idempotent: running it again changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger

from .components.chat.proactive.settings import LEGACY_DEFAULT_PROMPT
from .components.photo.core.config import DEFAULT_BASE_URL as PHOTO_DEFAULT_URL
from .components.voice.core.config import DEFAULT_BASE_URL as VOICE_DEFAULT_URL

OLD_KEYWORDS = [
    "今晚",
    "今天",
    "明天",
    "后天",
    "等会",
    "待会",
    "一会儿",
    "下班",
    "放学",
    "下课",
    "睡前",
    "起床",
    "记得",
    "答应",
    "约好",
    "说好",
    "一言为定",
    "早点睡",
    "别熬夜",
    "陪我",
    "一起",
    "改天",
    "周末",
    "下周",
    "提醒",
    "语音约定",
]
MIGRATED_SECTIONS = ("chat", "schedule", "photo", "voice")
NEW_KEYWORDS = ["面试", "考试", "看病", "体检", "出差", "答辩"]


LEGACY_SERVICE_HOST = "api.breeze.blue"
_VOICE_LEGACY_PATHS = {"", "/v1"}
_PHOTO_LEGACY_PATHS = {"", "/v1", "/v1/images/generations"}


def _is_legacy_service(value, paths) -> bool:
    """True for the old default ``https://api.breeze.blue`` written any equivalent way.

    Accepted: surrounding spaces, any letter case, a trailing slash, ``/v1``,
    the default port, and (photo) the full ``/v1/images/generations`` path.
    Anything else, including other hosts, paths or http://, is the user's own.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        url = urlsplit(value.strip())
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme.lower() == "https"
        and (url.hostname or "") == LEGACY_SERVICE_HOST
        and port in (None, 443)
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
        and url.path.rstrip("/").lower() in paths
    )


def migrate_sections(config: dict) -> list[str]:
    """Apply all upgrades to a plain config dict; return what changed."""
    changed = []
    chat = config.get("chat")
    if isinstance(chat, dict):
        legacy = chat.get("target_session")
        if isinstance(legacy, str) and legacy.strip():
            targets = chat.get("target_sessions")
            targets = list(targets) if isinstance(targets, list) else []
            if legacy.strip() not in targets:
                targets.insert(0, legacy.strip())
            chat["target_sessions"] = targets
            chat["target_session"] = ""
            changed.append("目标私聊改为列表")
        if isinstance(chat.get("proactive_prompt"), str) and chat["proactive_prompt"].strip() == LEGACY_DEFAULT_PROMPT:
            chat["proactive_prompt"] = ""
            changed.append("主动聊天提示词恢复为内置默认")
    for section, paths, default, label in (
        ("photo", _PHOTO_LEGACY_PATHS, PHOTO_DEFAULT_URL, "生图服务地址"),
        ("voice", _VOICE_LEGACY_PATHS, VOICE_DEFAULT_URL, "语音服务 URL"),
    ):
        values = config.get(section)
        # Done at most once: mark_services_checked() records the first check,
        # so a BreezeBlue address entered afterwards is the user's choice.
        if (
            isinstance(values, dict)
            and values.get("base_url_migrated") is not True
            and _is_legacy_service(values.get("base_url"), paths)
        ):
            values["base_url"] = default
            values["base_url_migrated"] = True
            changed.append(f"{label}从旧默认 BreezeBlue 换成新默认 {default}")
    schedule = config.get("schedule")
    if isinstance(schedule, dict) and schedule.get("change_trigger_keywords") == OLD_KEYWORDS:
        schedule["change_trigger_keywords"] = OLD_KEYWORDS + NEW_KEYWORDS
        changed.append("日程关键词补充了面试、考试等")
    return changed


def mark_services_checked(config: dict) -> bool:
    """Record that each service address has had its one legacy check.

    Set whether or not anything was migrated (fresh installs, empty or
    user-entered addresses too). Returns True when a marker was added.
    """
    marked = False
    for section in ("photo", "voice"):
        values = config.get(section)
        if isinstance(values, dict) and values.get("base_url_migrated") is not True:
            values["base_url_migrated"] = True
            marked = True
    return marked


def migrate_config(config, write_lock) -> list[str]:
    """Migrate both the live config and the saved file, keeping unrelated values."""
    with write_lock:
        path = Path(config.config_path)
        try:
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            saved = None
        changed = migrate_sections(config)
        marked = mark_services_checked(config)
        if saved is not None:
            changed_saved = migrate_sections(saved)
            changed = changed or changed_saved
            marked = mark_services_checked(saved) or marked
        if not changed and not marked:
            return []
        if saved is not None:
            for name, value in saved.items():
                if name in MIGRATED_SECTIONS and isinstance(value, dict) and isinstance(config.get(name), dict):
                    config[name].update(value)
        config.save_config()
    if changed:
        logger.info("[拟人生活] 已升级旧配置：%s。", "、".join(changed))
    return changed
