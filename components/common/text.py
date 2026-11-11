"""Text helpers shared by chat, photo and voice: voice tags, media markers, bubbles."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Mapping

VOICE_TAGS = (
    "[笑]",
    "[轻笑]",
    "[咯咯笑]",
    "[哭声]",
    "[抽泣]",
    "[呜咽]",
    "[呻吟]",
    "[哀号]",
    "[叹气]",
    "[惊呼]",
    "[吸气]",
    "[呼气]",
    "[喘息]",
    "[耳语]",
    "[喊叫]",
    "[惨叫]",
    "[唱歌]",
    "[哼歌]",
    "[结巴]",
    "[停顿]",
    "[清嗓]",
    "[咳嗽]",
    "[吸鼻子]",
    "[咂嘴]",
    "[啧嘴]",
    "[打哈欠]",
    "[打喷嚏]",
    "[打嗝]",
    "[打饱嗝]",
    "[吞咽]",
    "[干呕]",
    "[闷哼]",
    "[冷笑]",
    "[哼鼻子]",
)
VOICE_TAG_RE = re.compile("|".join(re.escape(tag) for tag in VOICE_TAGS))
LEGACY_VOICE_PREFIX_RE = re.compile(r"\A(?:\s*【语音】)+[ \t]*")
# Markers an older chat history used; a model may imitate them at the start.
_MEDIA_MARKER_RE = re.compile(
    r"\A(?:\s*(?:\[|【|\()(?:语音|照片|图片|发来语音|发来\s*\d*\s*张照片|引用消息)(?:\]|】|\)))+[ \t]*"
)
DEFAULT_SEGMENT_REGEX = r".*?[。？！~…]+|.+$"


def strip_voice_tags(text: str) -> str:
    """Remove known voice tags and the legacy 【语音】 prefix, keep everything else."""
    return VOICE_TAG_RE.sub("", LEGACY_VOICE_PREFIX_RE.sub("", text))


def strip_media_markers(text: str) -> str:
    """Drop leading [语音] / [照片] / [图片] style markers copied from old history."""
    return _MEDIA_MARKER_RE.sub("", text)


def media_side_note(medium: str, text: str) -> str:
    """Describe a delivered medium as an obvious side note, never as a prefix."""
    text = (text or "").strip()
    if medium == "voice":
        return f"{text}\n（以上是你发的一条语音）" if text else "（你发了一条语音）"
    if medium == "photo":
        return f"（你发了一张照片，拍的是：{text}）" if text else "（你发了一张照片）"
    return text


class SegmentSettings:
    """AstrBot's "分段回复" settings (platform_settings.segmented_reply)."""

    def __init__(self, raw: Mapping | None):
        raw = raw if isinstance(raw, Mapping) else {}
        self.enabled = raw.get("enable", False) is True
        self.split_mode = raw.get("split_mode", "regex")
        self.regex = raw.get("regex") or DEFAULT_SEGMENT_REGEX
        words = raw.get("split_words") or ["。", "？", "！", "~", "…"]
        self.split_words = [str(word) for word in words if str(word)]
        self.words_count_threshold = _int(raw.get("words_count_threshold"), 150)
        self.cleanup = raw.get("content_cleanup_rule") or ""
        self.interval = _interval(raw.get("interval"))

    @classmethod
    def from_context(cls, context, umo: str) -> SegmentSettings:
        try:
            config = context.get_config(umo)
            return cls(config.get("platform_settings", {}).get("segmented_reply"))
        except Exception:
            return cls(None)

    def delay(self) -> float:
        return random.uniform(*self.interval)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _interval(value) -> tuple[float, float]:
    try:
        low, high = (float(part) for part in str(value).replace(" ", "").split(","))
        if 0 <= low <= high:
            return low, high
    except (TypeError, ValueError):
        pass
    return 1.5, 3.5


def split_bubbles(text: str, settings: SegmentSettings) -> list[str]:
    """Split like AstrBot's segmented reply, or keep the model's own lines.

    When segmented reply is off the plugin does not invent bubbles: the text is
    sent as the model wrote it, one bubble per non-empty line.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not settings.enabled or len(text) > settings.words_count_threshold:
        return lines
    parts: list[str] = []
    for line in lines:
        if settings.split_mode == "words" and settings.split_words:
            words = sorted((re.escape(word) for word in settings.split_words), key=len, reverse=True)
            for match in re.findall(f"(.*?(?:{'|'.join(words)})|.+$)", line, re.DOTALL):
                segment = match
                for word in settings.split_words:
                    if segment.endswith(word):
                        segment = segment[: -len(word)]
                        break
                if segment.strip():
                    parts.append(segment.strip())
        else:
            try:
                found = re.findall(settings.regex, line, re.DOTALL | re.MULTILINE)
            except re.error:
                found = [line]
            parts.extend(
                segment.strip()
                for segment in (item if isinstance(item, str) else item[0] for item in found)
                if segment and segment.strip()
            )
    if settings.cleanup:
        try:
            parts = [re.sub(settings.cleanup, "", part) for part in parts]
        except re.error:
            pass
    return [part for part in parts if part.strip()] or lines


def extract_json(text) -> dict:
    """The first JSON object in a model reply, tolerating a preface, a code fence or trailing words."""
    decoder = json.JSONDecoder()
    text = str(text or "").strip()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型返回的内容缺少有效 JSON 对象")
