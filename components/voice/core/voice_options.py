"""Populate AstrBot's native voice selector from the current account."""

from __future__ import annotations

import asyncio
from collections import Counter

from .config import Settings
from .provider import VoiceError
from .voices import BreezeVoiceCatalog


async def _personal_voices(catalog: BreezeVoiceCatalog, api_key: str) -> list[dict]:
    voices = {}
    page = 1
    token = None
    seen_tokens = set()
    while True:
        result = await catalog.list_voices(
            api_key, voice_type="personal", page=page, page_size=100, next_page_token=token
        )
        for voice in result["voices"]:
            voices[voice["voice_id"]] = voice
        if not result["has_more"]:
            return list(voices.values())
        token = result["next_page_token"]
        if not result["voices"] or (token and token in seen_tokens):
            raise VoiceError("音色列表没有完整加载，请重新保存设置刷新")
        if token:
            seen_tokens.add(token)
        page += 1


async def populate_voice_options(schema: dict, settings: Settings, catalog: BreezeVoiceCatalog) -> str:
    """Change display metadata only; never replace a saved voice or credential."""
    field = schema["voice_id"]
    field.update(options=[""], labels=["请选择音色"])
    voices = []
    issue = ""
    if settings.voice_id:
        voices.append({"voice_id": settings.voice_id, "name": "已保存的音色"})
    if not settings.api_key:
        field["hint"] = "填写密钥并保存，重新打开设置后选择音色。"
    else:
        try:
            loaded = await asyncio.wait_for(_personal_voices(catalog, settings.api_key), timeout=30)
        except (VoiceError, asyncio.TimeoutError) as exc:
            issue = str(exc) if isinstance(exc, VoiceError) else "音色列表加载超时"
            field["hint"] = f"{issue}；保存设置可重试。"
        else:
            voices = loaded
            field["hint"] = "保存设置时会刷新“我的音色”。"
            if settings.voice_id and all(v["voice_id"] != settings.voice_id for v in voices):
                try:
                    selected = await catalog.get_voice(settings.api_key, settings.voice_id)
                except VoiceError as exc:
                    selected = {"voice_id": settings.voice_id, "name": "原音色（暂不可用）"}
                    issue = str(exc)
                    field["hint"] = f"{issue}；请重新选择音色。"
                voices.append(selected)
            if not voices:
                field["hint"] = "还没有个人音色，请先在 BreezeBlue 创建或克隆音色，再保存设置刷新。"
    counts = Counter(voice["name"] for voice in voices)
    for voice in sorted(voices, key=lambda item: (item["name"], item["voice_id"])):
        field["options"].append(voice["voice_id"])
        name = voice["name"]
        field["labels"].append(f"{name}（{voice['voice_id']}）" if counts[name] > 1 else name)
    return issue
