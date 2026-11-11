import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_life.components.common.persona import (
    PersonaResultError,
    current_conversation,
    selected_persona,
)


def context(result, calls):
    def resolve(**kwargs):
        calls.append(kwargs)
        return result

    async def cid(umo):
        return "c1"

    async def conversation(umo, cid):
        return SimpleNamespace(cid=cid, persona_id="conv-persona")

    return SimpleNamespace(
        persona_manager=SimpleNamespace(resolve_selected_persona=resolve),
        conversation_manager=SimpleNamespace(get_curr_conversation_id=cid, get_conversation=conversation),
        get_config=lambda umo: {"provider_settings": {"default_personality": "p"}},
    )


def test_selected_persona_passes_session_settings():
    calls = []
    ctx = context(("p1", {"name": "小林"}, None, False), calls)
    assert asyncio.run(
        selected_persona(ctx, "qq:FriendMessage:1", conversation_persona_id="x", platform_name="qq")
    ) == (
        "p1",
        {"name": "小林"},
    )
    assert calls == [
        {
            "umo": "qq:FriendMessage:1",
            "conversation_persona_id": "x",
            "platform_name": "qq",
            "provider_settings": {"default_personality": "p"},
        }
    ]


def test_selected_persona_accepts_coroutine_and_rejects_bad_shape():
    async def later():
        return ("p2", None)

    assert asyncio.run(selected_persona(context(later(), []), "u")) == ("p2", None)
    with pytest.raises(PersonaResultError):
        asyncio.run(selected_persona(context("p3", []), "u"))


def test_current_conversation():
    conversation = asyncio.run(current_conversation(context(None, []), "u"))
    assert conversation.persona_id == "conv-persona"


def test_all_components_use_shared_resolution():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "components"
    users = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "resolve_selected_persona(" in path.read_text(encoding="utf-8")
    ]
    assert users == ["common/persona.py"]
