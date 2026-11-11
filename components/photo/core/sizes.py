"""The "照片尺寸" setting (item 19): a ratio, a resolution tier, both, or WxH.

Examples: ``3:4``, ``2K``, ``3:4 2K``, ``1536x2048``. The default is ``2K``
with a 3:4 portrait. When the image service refuses a size, the request steps
down one tier at a time and keeps the ratio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 4K: the sizes the plugin used before v1.4.0. 2K: the earlier fallback sizes.
SIZES_4K = {
    "1:1": "2880x2880",
    "16:9": "3840x2160",
    "9:16": "2160x3840",
    "5:4": "3200x2560",
    "4:5": "2560x3200",
    "4:3": "3264x2448",
    "3:4": "2448x3264",
    "3:2": "3504x2336",
    "2:3": "2336x3504",
    "21:9": "3808x1632",
}
SIZES_2K = {
    "1:1": "2048x2048",
    "16:9": "2048x1152",
    "9:16": "1152x2048",
    "5:4": "2080x1664",
    "4:5": "1664x2080",
    "4:3": "2048x1536",
    "3:4": "1536x2048",
    "3:2": "2064x1376",
    "2:3": "1376x2064",
    "21:9": "2016x864",
}


def _half(size: str) -> str:
    width, height = (int(value) for value in size.split("x"))
    return f"{width // 32 * 16}x{height // 32 * 16}"


SIZES_1K = {ratio: _half(size) for ratio, size in SIZES_2K.items()}
TIERS = {"1K": SIZES_1K, "2K": SIZES_2K, "4K": SIZES_4K}
ORDER = ("4K", "2K", "1K")
DEFAULT_RATIO = "3:4"
DEFAULT_TIER = "2K"
_RATIO = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
_TIER = re.compile(r"([124])\s*[kK]")
_EXACT = re.compile(r"(\d{3,4})\s*[xX×*]\s*(\d{3,4})")


def _pixels(size: str) -> int:
    width, height = (int(value) for value in size.split("x"))
    return width * height


def _nearest_ratio(width: int, height: int) -> str:
    target = width / height
    return min(SIZES_2K, key=lambda ratio: abs(target - _ratio_value(ratio)))


def _ratio_value(ratio: str) -> float:
    width, height = (int(value) for value in ratio.split(":"))
    return width / height


@dataclass(frozen=True)
class SizeSetting:
    ratio: str = DEFAULT_RATIO
    tier: str = DEFAULT_TIER
    exact: str = ""

    @classmethod
    def parse(cls, text) -> SizeSetting:
        if text is None:
            return cls()
        if not isinstance(text, str):
            raise ValueError("照片尺寸应填写文字，例如 2K、3:4 或 1536x2048")
        value = text.strip()
        if not value:
            return cls()
        exact = _EXACT.fullmatch(value)
        if exact:
            width, height = int(exact[1]), int(exact[2])
            if not (256 <= width <= 4096 and 256 <= height <= 4096):
                raise ValueError("照片尺寸的宽和高需要在 256 到 4096 之间")
            return cls(ratio=_nearest_ratio(width, height), exact=f"{width}x{height}")
        ratio, tier = DEFAULT_RATIO, DEFAULT_TIER
        rest = value
        if match := _RATIO.search(rest):
            ratio = f"{int(match[1])}:{int(match[2])}"
            if ratio not in SIZES_2K:
                raise ValueError("照片比例暂不支持，请用 3:4、4:3、1:1、16:9、9:16 等常见比例")
            rest = rest[: match.start()] + rest[match.end() :]
        if match := _TIER.search(rest):
            tier = f"{match[1]}K"
            rest = rest[: match.start()] + rest[match.end() :]
        if rest.strip(" ,，、/"):
            raise ValueError("照片尺寸格式不对，例如 2K、3:4、3:4 2K 或 1536x2048")
        return cls(ratio=ratio, tier=tier)

    def size_for(self, ratio: str | None = None) -> str:
        """The size to request. A ratio asked for in chat overrides the setting's."""
        if self.exact and (not ratio or ratio == self.ratio):
            return self.exact
        ratio = ratio if ratio in SIZES_2K else self.ratio
        tier = self.tier
        if self.exact:
            pixels = _pixels(self.exact)
            tier = next((name for name in ORDER if _pixels(TIERS[name][ratio]) <= pixels * 1.1), "1K")
        return TIERS[tier][ratio]


def smaller_size(size: str) -> str | None:
    """The next smaller tier with the same ratio, or None at the bottom."""
    for index, name in enumerate(ORDER):
        for ratio, value in TIERS[name].items():
            if value == size:
                return TIERS[ORDER[index + 1]][ratio] if index + 1 < len(ORDER) else None
    match = _EXACT.fullmatch(size or "")
    if not match:
        return None
    width, height = int(match[1]), int(match[2])
    ratio = _nearest_ratio(width, height)
    for name in ORDER:
        candidate = TIERS[name][ratio]
        if _pixels(candidate) < width * height:
            return candidate
    return None
