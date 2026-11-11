"""复用试听工具的声音标签与清理规则，提供语音和展示两个通道。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ...common.text import LEGACY_VOICE_PREFIX_RE, VOICE_TAG_RE, VOICE_TAGS
from ...common.text import strip_voice_tags as _strip_voice_tags

MAX_TEXT_LENGTH = 1000
# One shared list for chat, photo and voice (item 24).
TAGS = list(VOICE_TAGS)
_VOICE_TAG_RE = VOICE_TAG_RE
_LEGACY_PREFIX_RE = LEGACY_VOICE_PREFIX_RE

_ACTION_ALIASES = {
    **{tag[1:-1]: tag for tag in TAGS},
    "笑了笑": "[笑]",
    "笑了一下": "[笑]",
    "笑出声": "[笑]",
    "轻轻笑": "[轻笑]",
    "轻轻地笑": "[轻笑]",
    "轻笑一声": "[轻笑]",
    "咯咯地笑": "[咯咯笑]",
    "叹了口气": "[叹气]",
    "叹了一口气": "[叹气]",
    "轻叹": "[叹气]",
    "吸了吸鼻子": "[吸鼻子]",
    "打了个哈欠": "[打哈欠]",
    "打了一个哈欠": "[打哈欠]",
    "清了清嗓子": "[清嗓]",
    "咳了一声": "[咳嗽]",
    "哼了一声": "[哼鼻子]",
    "低声": "[耳语]",
    "小声说": "[耳语]",
    "哭": "[哭声]",
    "哭泣": "[哭声]",
}
_ACTION_DESCRIPTIONS = frozenset({"看着手机", "看着屏幕", "拿起手机", "低头看手机", "点头", "摇头"})


@dataclass(frozen=True)
class PreparedSpeech:
    tts_text: str
    display_text: str


def _action_tag(content: str) -> str:
    return _ACTION_ALIASES.get(content.strip(" \t\r\n。.!！"), "")


def _replace_parentheses(text: str) -> str:
    """沿用平衡括号解析，只清理已识别动作，保留普通括注。"""
    pairs = {"(": ")", "（": "）"}
    result = []
    i = 0
    while i < len(text):
        if text[i] not in pairs:
            result.append(text[i])
            i += 1
            continue
        start = i
        stack = [pairs[text[i]]]
        i += 1
        while i < len(text) and stack:
            char = text[i]
            if char in pairs:
                stack.append(pairs[char])
            elif char == stack[-1]:
                stack.pop()
            i += 1
        raw = text[start:i]
        if stack:
            result.append(raw)
            continue
        content = _replace_parentheses(raw[1:-1])
        action = _action_tag(content)
        if action:
            result.append(action)
        elif content.strip(" \t\r\n。.!！") not in _ACTION_DESCRIPTIONS:
            result.append(raw)
    return "".join(result)


def _replace_star_action(match: re.Match) -> str:
    content = match.group(1)
    action = _action_tag(content)
    if action:
        return action
    return "" if content.strip(" \t\r\n。.!！") in _ACTION_DESCRIPTIONS else content


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2300 <= code <= 0x23FF
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0020 <= code <= 0xE007F
        or code in (0x200D, 0x20E3)
        or code
        in (
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        )
    )


def strip_voice_tags(text: str) -> str:
    """普通回复只去已知声音标签与旧前缀，保留其他内容和排版。"""
    if not isinstance(text, str):
        raise ValueError("请填写要处理的文字")
    return _strip_voice_tags(text)


def split_voice_tag_chunk(text: str) -> tuple[str, str]:
    """清理完整声音标签，保留可能跨片段的尾巴。"""
    last_bracket = text.rfind("[")
    if last_bracket != -1:
        tail = text[last_bracket:]
        if any(tag != tail and tag.startswith(tail) for tag in TAGS):
            return _VOICE_TAG_RE.sub("", text[:last_bracket]), tail
    return _VOICE_TAG_RE.sub("", text), ""


def prepare_text(text: str) -> PreparedSpeech:
    """清理一次完整回复，不拆句、不按人设字数限制截断。"""
    if not isinstance(text, str):
        raise ValueError("请填写要念的文字")
    text = _LEGACY_PREFIX_RE.sub("", text)
    # 沿用试听规则：链接保留可读标题，Markdown 加粗保留正文。
    text = re.sub(r"!?\[([^\[\]]+)\]\([^\n)]*\)", lambda m: m.group(1), text)
    text = re.sub(r"```[^\n`]*\n?([\s\S]*?)```", lambda m: m.group(1), text)
    text = re.sub(r"(\*\*|__|~~)(.+?)\1", lambda m: m.group(2), text, flags=re.DOTALL)
    # 网址须先于颜文字处理，避免 :/ 被当成表情后留下网址碎片。
    text = re.sub(r"https?://[^\s<>()（）【】\[\]，。！？、；：…]+", "", text)
    # 先清理颜文字，避免把括号外的颜文字碎片留在正文里。
    text = re.sub(
        r"(?:[oO]?\([\^╥〒ಥT；;´｀><＞＜・ω▽﹏益Дд◕♥♡°￣_\s]+\)[oO]?|[:;=8xX][-^']?[)DdpP(/\\]|[>＞][_.﹏＿-][<＜]|[TtQq][_.＿-][TtQq]|\^{2,}|[囧ಠಥ]{1,3})",
        "",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_])(?:QAQ|QwQ|QvQ|TwT|TAT)(?![A-Za-z0-9_])|[=＝]?[￣＾^][ω▽﹏益Дд][￣＾^][=＝]?",
        "",
        text,
    )
    text = _replace_parentheses(text)
    text = re.sub(r"\*([^*\n]+)\*", _replace_star_action, text)
    # 仅识别白名单声音标签，普通的 [数学] 等内容不会被当成控制标签删除。
    text = re.sub(r"[\U0001F1E6-\U0001F1FF]{2}|[0-9#*]\ufe0f?\u20e3", "", text)
    text = "".join(char for char in text if not _is_emoji(char))
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s*|>\s*|[-+*]\s+|\d+[.)]\s+)", "", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[`*_#]", "", text)
    text = re.sub(r"~{2,}", "", text)
    text = text.replace("～", "~")
    text = re.sub(r"。{3,}", "……", text)
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs"))
    text = re.sub(r"\s+", " ", text).strip()

    count = 0

    def keep_two_tags(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(0) if count <= 2 else ""

    tts_text = re.sub(r"\s+", " ", _VOICE_TAG_RE.sub(keep_two_tags, text)).strip()
    display_text = re.sub(r"\s+", " ", _VOICE_TAG_RE.sub("", tts_text)).strip()
    if not any(char.isalnum() for char in display_text):
        return PreparedSpeech("", "")
    return PreparedSpeech(tts_text, display_text)


def build_tts_body(text: str) -> dict:
    """试听第三种：只发文字和声音标签，固定参数，不传语气说明。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请填写要念的文字")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError("要念的文字最多 1000 字，请先缩短")
    return {
        "text": text,
        "model_id": "breeze-tts-2",
        "language_code": "zh",
        "voice_settings": {
            "speed": 1.0,
            "guidance_scale": 2.0,
            "volume": 1.0,
            "use_speaker_boost": False,
        },
    }
