"""v1.4.4 prompt rewrite: user choices, hard output lines and the two tool docstrings (C12)."""

import ast
import inspect
from pathlib import Path

from astrbot_plugin_life.components.chat.proactive.astrbot_bridge import _PROTOCOL
from astrbot_plugin_life.components.qzone.core import autonomy, caption, service, sharing
from astrbot_plugin_life.components.schedule.core import domain, prompts

ROOT = Path(__file__).resolve().parents[1]


def _docstring(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(ast.get_docstring(n) for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_tts_docstrings_stay_in_sync():
    root = _docstring("main.py", "tts_speak")
    assert root == _docstring("components/voice/main.py", "tts_speak")
    assert "[惨叫]" not in root and "[干呕]" not in root and "情绪真的很重时才用 [抽泣] [哭声]" in root


def test_interaction_boundary_uses_option_b():
    text = prompts.interaction_boundary()
    assert "可以留在 day_summary 或当时那段的 mood 里" in text and "只写心情，不写对方是谁" in text
    assert "只写进 day_summary，一句带过" not in text
    assert text.endswith(prompts.voice_boundary())


def test_voice_boundary_both_modes(monkeypatch):
    monkeypatch.setattr(domain, "RESTRICT_VOICE_MESSAGES", True)
    assert prompts.voice_boundary().startswith("你们目前只用文字和图片联系")
    monkeypatch.setattr(domain, "RESTRICT_VOICE_MESSAGES", False)
    assert prompts.voice_boundary().startswith("你们之间可以发文字、图片和语音条")


def test_schedule_prompts_keep_contracts():
    inputs = {"weather": {"city": "上海"}}
    text = prompts.schedule_prompt(inputs)
    assert "在家待着" in text and "没什么安排" in text and "至少 6 段" in text
    assert "\n输出：" + prompts.SCHEDULE_SKELETON + "\n" in text and text.endswith(prompts.dump(inputs))
    for source in ("chat", "weather"):
        change = prompts.change_prompt(inputs, source)
        assert "只输出 JSON：" + prompts.CHANGE_SKELETON + "\n输入：" in change
    assert prompts.life_rules(["见面"], "可以看手机").startswith("<life_rules>\n")


def test_comment_reply_uses_option_b():
    source = inspect.getsource(service)
    assert "像熟人那样随意，亲近程度和你们私聊时一样，分寸按人设来。" in source
    assert "公开场合比私聊收着一点" not in source and "陌生人礼貌简短" not in source


def test_json_prompts_say_json_only():
    assert "前后不加任何文字，也不要用代码块包裹" in inspect.getsource(sharing.choose_share_intent)
    assert "只输出一个 JSON 对象" in _PROTOCOL
    assert "不要“好的”“配文：”之类的开头" in inspect.getsource(caption.generate_caption)


def test_selfie_prompt_has_no_gender_template():
    source = inspect.getsource(autonomy)
    assert "男性" not in source and "女性" not in source and "不按性别套模板" in source
