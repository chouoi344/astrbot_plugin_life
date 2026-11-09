"""AstrBot 4.26–4.28 直接发送工具的历史保存兼容。"""

from astrbot import __version__
from packaging.version import Version

_NEEDS_DIRECT_TOOL_HISTORY = Version("4.26.0") <= Version(__version__) < Version("4.29.0")


def preserve_direct_tool_history(event, tool_name: str) -> None:
    if not _NEEDS_DIRECT_TOOL_HISTORY:
        return

    from astrbot.api.provider import LLMResponse
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS

    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if (
        runner is None
        or runner.run_context.context.event is not event
        or runner._last_tool_name != tool_name
        or runner.get_final_llm_resp() is not None
    ):
        return

    # 旧版核心要求最终响应存在才保存历史；工具调用和结果仍由核心完整配对。
    # 不追加消息、不改状态；yield None 继续负责结束本轮模型循环。
    runner.final_llm_resp = LLMResponse(role="assistant", completion_text="")
