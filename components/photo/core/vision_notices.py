"""Remove echoed AstrBot image notices from user-visible text."""

import re
import sys
from collections.abc import AsyncGenerator
from dataclasses import replace
from itertools import groupby
from pathlib import PurePath

from astrbot.api.message_components import Plain
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.message.message_event_result import MessageChain

_START = "[Image "
_QUOTED_ORIGIN = " in quoted message"
_INDEX_DIGITS = len(str(sys.maxsize))
_INDEX = re.compile(rf"[1-9][0-9]{{0,{_INDEX_DIGITS - 1}}}")
_IMAGE_NAME = r"[0-9]{17}_[0-9a-f]{4}\.(?:jpe?g|png|webp)"


class VisionNoticeFilter:
    def __init__(self, temp_dir: PurePath):
        directories = [temp_dir.as_posix().rstrip("/") + "/"]
        if temp_dir.drive:
            directories.append(str(temp_dir).rstrip("\\") + "\\")
        self._path_starts = tuple(
            f"{origin}: original path {directory}media_image_"
            for origin in ("", _QUOTED_ORIGIN)
            for directory in directories
        )
        paths = "|".join(re.escape(path) for path in self._path_starts)
        self._notice = re.compile(rf"{re.escape(_START)}{_INDEX.pattern}(?:{paths}){_IMAGE_NAME}\]")
        self._max_length = (
            len(_START) + _INDEX_DIGITS + max(map(len, self._path_starts)) + len("20261107191332544_0638.jpeg]")
        )

    def clean_text(self, text: str) -> str:
        return self._notice.sub("", text)

    def clean_chain(self, chain: list[BaseMessageComponent]) -> None:
        """Clean adjacent text components without merging across media."""
        visible = []
        for is_text, group in groupby(chain, lambda part: isinstance(part, Plain)):
            if not is_text:
                visible.extend(group)
                continue
            parts = list(group)
            text = "".join(part.text for part in parts)
            notices = [match.span() for match in self._notice.finditer(text)]
            offset = 0
            for part in parts:
                end = offset + len(part.text)
                cursor = offset
                pieces = []
                for start, stop in notices:
                    if stop <= cursor:
                        continue
                    if start >= end:
                        break
                    pieces.append(text[cursor : max(cursor, start)])
                    cursor = stop
                if cursor == offset:
                    visible.append(part)
                else:
                    pieces.append(text[cursor:end])
                    if remaining := "".join(pieces):
                        visible.append(part.copy(update={"text": remaining}))
                offset = end
        chain[:] = visible

    def _split(self, text: str) -> tuple[str, str]:
        """Hold a bounded suffix only while it can start a notice."""
        start = text.rfind(_START)
        bracket = text.rfind("[")
        if bracket > start and _START.startswith(text[bracket:]):
            return self.clean_text(text[:bracket]), text[bracket:]
        if start >= 0 and len(text) - start < self._max_length:
            tail = text[start:]
            index = _INDEX.match(tail, len(_START))
            path = tail[index.end() if index else len(_START) :]
            if not path or (
                index
                and any(
                    prefix.startswith(path) or (path.startswith(prefix) and "]" not in path[len(prefix) :])
                    for prefix in self._path_starts
                )
            ):
                return self.clean_text(text[:start]), tail
        return self.clean_text(text), ""

    async def clean_stream(self, generator: AsyncGenerator[MessageChain, None]) -> AsyncGenerator[MessageChain, None]:
        pending = None
        async for chunk in generator:
            if chunk.type is not None:
                if pending is not None:
                    yield replace(pending[0], chain=[pending[1]])
                    pending = None
                yield chunk
                continue
            visible = []
            for part in chunk.chain:
                if not isinstance(part, Plain):
                    if pending is not None:
                        visible.append(pending[1])
                        pending = None
                    visible.append(part)
                    continue
                prefix = pending[1].text if pending is not None else ""
                text, tail = self._split(prefix + part.text)
                pending = (chunk, part.copy(update={"text": tail})) if tail else None
                if text == part.text:
                    visible.append(part)
                elif text:
                    visible.append(part.copy(update={"text": text}))
            if len(visible) == len(chunk.chain) and all(
                output is source for output, source in zip(visible, chunk.chain)
            ):
                yield chunk
            elif visible:
                yield replace(chunk, chain=visible)
        if pending is not None:
            yield replace(pending[0], chain=[pending[1]])
