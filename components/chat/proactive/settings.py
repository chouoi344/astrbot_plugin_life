from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_PRIVATE_UMO = re.compile(r"[^:,\s]+:FriendMessage:[^:,\s]+")
_DEFAULT_PROMPT = (
    "你正处在自己真实运转的生活节奏中，此刻主动联系对方。"
    "请完全以你的人设性格、说话习惯与双方关系自然开口，展现出你有自己正在忙的事、当下的情绪温度或突然起意的主体性："
    "优先从手头刚忙完的细节、眼前的生活切片、突然想起的某个具体话头或随口逗弄切入，严禁发送“在吗”“在干嘛呢”“注意休息/早点睡”等毫无灵魂的客服式问候或查岗，严禁出现“这笔账先记下/留着以后算”等记账体套话。"
    "若选择发文字（medium为text），严格遵守单行零空格、零换行与日常零逗号短句规则（普通分享用1~3个句号/问号短句；"
    "若吐槽翻车、讲趣事或情绪上头1~3句说不完，直接用句号/问号拆成4~6个零逗号短句连发）；"
    "若当前手头占着、在走动、刚睡醒或适合用声音传达而选择发语音（medium为voice），则切换为句内带逗号自然换气的一整句连贯口语并搭配1~2个声音标签。"
    "先结合最近聊天怎么收尾、当前时间与你的生活状态判断此刻是否适合打扰："
    "若双方刚道过晚安、约定稍后再聊、话已聊透且无新生活进展，或当前状态不便联系，请直接选择 skip 保持安静；"
    "不要把同一次忙碌、集合、出发、训练、会议或未回复拆成多条连续汇报，也绝不追问对方为什么没回。"
)


def _boolean(config: Mapping[str, object], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是开关")
    return value


def _integer(
    config: Mapping[str, object], key: str, default: int, minimum: int
) -> int:
    value = config.get(key, default)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{key} 必须是大于或等于 {minimum} 的整数")
    return value


def _text(config: Mapping[str, object], key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文字")
    return value.strip()


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    target_umo: str = ""
    daily_max: int = 5
    max_unanswered: int = 2
    voice_enabled: bool = True
    photo_enabled: bool = True
    daily_photo_max: int = 1
    proactive_prompt: str = _DEFAULT_PROMPT
    silence_hours: float = 5 / 60

    def __post_init__(self) -> None:
        if (self.enabled or self.target_umo) and not _PRIVATE_UMO.fullmatch(
            self.target_umo
        ):
            raise ValueError("目标会话须填写完整私聊标识，格式为 平台实例:FriendMessage:对方标识")

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> Settings:
        if "silence_minutes" in config:
            silence_minutes = config["silence_minutes"]
            if type(silence_minutes) not in (int, float) or not 0 <= silence_minutes < float("inf"):
                raise ValueError("安静等待时间必须是非负分钟数")
            silence_hours = silence_minutes / 60
        else:
            silence_hours = config.get("silence_hours", 5 / 60)
            if type(silence_hours) not in (int, float) or not 0 <= silence_hours < float("inf"):
                raise ValueError("silence_hours 必须是非负小时数")
        return cls(
            enabled=_boolean(config, "proactive_enabled", True),
            target_umo=_text(config, "target_session", ""),
            daily_max=_integer(config, "daily_max", 5, 1),
            max_unanswered=_integer(config, "max_unanswered", 2, 1),
            voice_enabled=_boolean(config, "voice_enabled", True),
            photo_enabled=_boolean(config, "photo_enabled", True),
            daily_photo_max=_integer(config, "daily_photo_max", 1, 0),
            silence_hours=silence_hours,
        )
