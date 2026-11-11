"""One persona resolution shared by components."""

from __future__ import annotations

import inspect


class PersonaResultError(ValueError):
    """AstrBot returned a persona result in an unexpected shape."""


async def selected_persona(context, umo: str, *, conversation_persona_id=None, platform_name: str = ""):
    """Return AstrBot's ``(persona_id, persona)`` for ``umo`` as it is selected now.

    Wraps ``persona_manager.resolve_selected_persona`` with the session's own
    provider settings; values are passed through unchanged (either may be empty).
    """
    config = context.get_config(umo)
    provider_settings = config.get("provider_settings") if isinstance(config, dict) else None
    selected = context.persona_manager.resolve_selected_persona(
        umo=umo,
        conversation_persona_id=conversation_persona_id,
        platform_name=platform_name or "",
        provider_settings=provider_settings if isinstance(provider_settings, dict) else {},
    )
    if inspect.isawaitable(selected):
        selected = await selected
    if not isinstance(selected, (tuple, list)) or len(selected) < 2:
        raise PersonaResultError("AstrBot 人设解析结果不兼容")
    return selected[0], selected[1]


async def current_conversation(context, umo: str):
    """The session's current conversation, or None."""
    manager = context.conversation_manager
    cid = await manager.get_curr_conversation_id(umo)
    return await manager.get_conversation(umo, cid) if cid else None
