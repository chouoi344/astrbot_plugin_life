"""C1 / C3: proactive plans and share intents survive a preface, a code fence or trailing words."""

import asyncio

import pytest
from astrbot_plugin_life.components.chat.proactive.coordinator import parse_plan
from astrbot_plugin_life.components.common.text import extract_json
from astrbot_plugin_life.components.qzone.core.sharing import choose_share_intent

ALLOWED = {"voice": False, "photo": False}
PLAN = '{"medium": "text", "text": "刚到家", "topic": "life"}'
WRAPPED = [
    PLAN,
    "```json\n" + PLAN + "\n```",
    "好的，这是这轮的计划：\n```json\n" + PLAN + "\n```\n希望合适。",
    "好的～" + PLAN + " 就这样",
]


def test_extract_json_finds_the_first_object():
    assert extract_json('说明 {"a": 1} 然后 {"b": 2}') == {"a": 1}
    assert extract_json('[1, 2] {"a": {"b": 1}}') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        extract_json("没有 JSON")
    with pytest.raises(ValueError):
        extract_json(None)


@pytest.mark.parametrize("raw", WRAPPED)
def test_parse_plan_tolerates_wrapping(raw):
    plan = parse_plan(raw, ALLOWED)
    assert plan["medium"] == "text" and plan["text"] == "刚到家"


def test_parse_plan_skip_and_garbage():
    assert parse_plan('好的。\n```json\n{"medium": "skip"}\n```', ALLOWED) is None
    with pytest.raises(ValueError):
        parse_plan("我觉得这次先不发了", ALLOWED)


INTENT = '{"share": true, "focus": "刚下班的路灯", "photo_type": "scene", "related_evidence": []}'


def choose(reply):
    async def llm(event, prompt):
        return reply

    return asyncio.run(
        choose_share_intent(llm, None, activity="下班路上", outfit="", sharing_context={}, trigger="after_activity")
    )


@pytest.mark.parametrize(
    "reply",
    [
        INTENT,
        "```json\n" + INTENT + "\n```",
        "好的，我想发：\n```json\n" + INTENT + "\n```\n以上。",
        "嗯…" + INTENT,
    ],
)
def test_share_intent_tolerates_wrapping(reply):
    result = choose(reply)
    assert result["share"] is True and result["photo_type"] == "scene" and result["focus"] == "刚下班的路灯"


def test_share_intent_still_rejects_garbage():
    with pytest.raises(ValueError, match="分享意图格式无效"):
        choose("今天不想发")
    assert choose('先不发了 {"share": false}') == {"share": False, "focus": "", "related_evidence": []}
