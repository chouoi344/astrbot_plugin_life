"""Validated proactive chat settings, read once per (re)load."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from astrbot.api import logger

from .timing import parse_quiet_hours

PRIVATE_UMO = re.compile(r"[^:,\s]+:FriendMessage:[^:,\s]+")
DEFAULT_PROMPT = (
    "你正过着自己的一天，忽然想跟对方说点什么。用你的性格、说话习惯和你们之间的关系开口，让对方感觉到你有自己的生活、情绪和念头：可以是手头刚忙完的一个细节、眼前正看着的东西、突然冒出来的一个话头，也可以问问对方之前提过的那件事；真有契机的时候，流露一点想念也很好。\n"
    "别用“在吗”“在干嘛呢”这种空开场，也别把关心写成“注意休息”“早点睡哦”这类例行叮嘱或查岗。快睡觉时想起对方、说一句睡前的话是可以的，说的是你此刻真实的状态和心情就好，不是提醒对方作息。\n"
    "开口前看一眼最近的聊天、现在几点、你在做什么：约定稍后再聊且还没到时候、话已经聊透又没有新进展，或者你此刻确实不方便，就 skip，安静一会儿也很正常。对方说忙完来找你却超时没来，可以自然地问一句忙完了没有。\n"
    "同一次忙碌、出发、开会不要拆成好几条连着汇报，也别追问对方为什么没回。"
)
# The v1.3.11 default. A saved copy is cleared on upgrade so users get the new default.
LEGACY_DEFAULT_PROMPT = "你正处在自己真实运转的生活节奏中，此刻主动联系对方。请完全以你的人设性格、说话习惯与双方关系自然开口，展现出你有自己正在忙的事、当下的情绪温度或突然起意的主体性：优先从手头刚忙完的细节、眼前的生活切片、突然想起的某个具体话头或随口逗弄切入，严禁发送“在吗”“在干嘛呢”“注意休息/早点睡”等毫无灵魂的客服式问候或查岗，严禁出现“这笔账先记下/留着以后算”等记账体套话。若选择发文字（medium为text），严格遵守单行零空格、零换行与日常零逗号短句规则（普通分享用1~3个句号/问号短句；若吐槽翻车、讲趣事或情绪上头1~3句说不完，直接用句号/问号拆成4~6个零逗号短句连发）；若当前手头占着、在走动、刚睡醒或适合用声音传达而选择发语音（medium为voice），则切换为句内带逗号自然换气的一整句连贯口语并搭配1~2个声音标签。先结合最近聊天怎么收尾、当前时间与你的生活状态判断此刻是否适合打扰：若双方刚道过晚安、约定稍后再聊、话已聊透且无新生活进展，或当前状态不便联系，请直接选择 skip 保持安静；不要把同一次忙碌、集合、出发、训练、会议或未回复拆成多条连续汇报，也绝不追问对方为什么没回。"


def _boolean(config: Mapping, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是开关")
    return value


def _integer(config: Mapping, key: str, default: int, minimum: int, maximum: int | None = None) -> int:
    value = config.get(key, default)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum} 到 {maximum}" if maximum is not None else f"大于或等于 {minimum}"
        raise ValueError(f"{key} 必须是{bound}的整数")
    return value


def _number(config: Mapping, key: str, default: float, minimum: float) -> float:
    value = config.get(key, default)
    if type(value) not in (int, float) or value < minimum:
        raise ValueError(f"{key} 必须是大于或等于 {minimum} 的数字")
    return float(value)


def _text(config: Mapping, key: str) -> str:
    value = config.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文字")
    return value


def target_list(config: Mapping) -> tuple[str, ...]:
    """Target private chats; the v1.3.11 single ``target_session`` still counts."""
    raw = config.get("target_sessions")
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    legacy = config.get("target_session")
    if isinstance(legacy, str) and legacy.strip():
        items.append(legacy)
    seen = []
    for item in items:
        value = str(item).strip()
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    proactive_enabled: bool = True
    targets: tuple[str, ...] = ()
    min_interval_minutes: int = 60
    max_interval_minutes: int = 90
    quiet_hours_enabled: bool = True
    quiet_hours: tuple[tuple[int, int], ...] = ((1, 7),)
    max_unanswered: int = 2
    daily_max: int = 5
    voice_enabled: bool = True
    photo_enabled: bool = True
    daily_photo_max: int = 1
    custom_prompt: str = ""
    history_count: int = 20
    hold_hours: float = 6.0
    invalid_targets: tuple[str, ...] = field(default=())

    @property
    def proactive_prompt(self) -> str:
        return self.custom_prompt or DEFAULT_PROMPT

    def unanswered_reached(self, unanswered: int) -> bool:
        return bool(self.max_unanswered) and unanswered >= self.max_unanswered

    @classmethod
    def from_config(cls, config: Mapping) -> Settings:
        """Read settings; a malformed item falls back to its default with a warning."""

        def read(key, parse, default):
            try:
                return parse()
            except ValueError as error:
                logger.warning("[自然私聊] 设置项 %s 填写有误（%s），本次改用默认值 %r。", key, error, default)
                return default

        targets = target_list(config)
        valid = tuple(item for item in targets if PRIVATE_UMO.fullmatch(item))
        min_interval = read("min_interval_minutes", lambda: _integer(config, "min_interval_minutes", 60, 1), 60)
        max_interval = read("max_interval_minutes", lambda: _integer(config, "max_interval_minutes", 90, 1), 90)
        if max_interval < min_interval:
            logger.warning(
                "[自然私聊] 设置项 max_interval_minutes（%s）小于 min_interval_minutes（%s），本次两项都改用默认值 60 和 90。",
                max_interval,
                min_interval,
            )
            min_interval, max_interval = 60, 90
        prompt = read("proactive_prompt", lambda: _text(config, "proactive_prompt"), "").strip()
        if prompt == LEGACY_DEFAULT_PROMPT:
            prompt = ""
        return cls(
            proactive_enabled=read("proactive_enabled", lambda: _boolean(config, "proactive_enabled", True), True),
            targets=valid,
            min_interval_minutes=min_interval,
            max_interval_minutes=max_interval,
            quiet_hours_enabled=read(
                "quiet_hours_enabled", lambda: _boolean(config, "quiet_hours_enabled", True), True
            ),
            quiet_hours=read("quiet_hours", lambda: parse_quiet_hours(config.get("quiet_hours", "1-7")), ((1, 7),)),
            max_unanswered=read("max_unanswered", lambda: _integer(config, "max_unanswered", 2, 0), 2),
            daily_max=read("daily_max", lambda: _integer(config, "daily_max", 5, 1), 5),
            voice_enabled=read("voice_enabled", lambda: _boolean(config, "voice_enabled", True), True),
            photo_enabled=read("photo_enabled", lambda: _boolean(config, "photo_enabled", True), True),
            daily_photo_max=read("daily_photo_max", lambda: _integer(config, "daily_photo_max", 1, 0), 1),
            custom_prompt=prompt,
            history_count=read("history_count", lambda: _integer(config, "history_count", 20, 0, 200), 20),
            hold_hours=read("hold_hours", lambda: _number(config, "hold_hours", 6, 0.5), 6.0),
            invalid_targets=tuple(item for item in targets if item not in valid),
        )
