"""为 AstrBot 4.26–4.29 的直接发送工具记录内部回执。"""

import time

from astrbot import __version__
from mcp.types import CallToolResult, TextContent
from packaging.version import Version

_VERSION = Version(__version__)
_SUPPORTS_DIRECT_TOOL_RECEIPT = Version("4.26.0") <= _VERSION < Version("4.30.0")
_NEEDS_DIRECT_TOOL_HISTORY = _VERSION < Version("4.29.0")


def finish_direct_tool_delivery(event, tool_name: str, receipt: str | None) -> CallToolResult | None:
    if not _SUPPORTS_DIRECT_TOOL_RECEIPT:
        return None

    from astrbot.api.provider import LLMResponse
    from astrbot.core.agent.runners.base import AgentState
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS

    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if (
        not isinstance(runner, ToolLoopAgentRunner)
        or runner.run_context.context.event is not event
        or runner._last_tool_name != tool_name
    ):
        # 找不到当前核心循环时，继续用原生 None 分支结束回复。
        return None

    if _NEEDS_DIRECT_TOOL_HISTORY and runner.get_final_llm_resp() is None:
        runner.final_llm_resp = LLMResponse(role="assistant", completion_text="")
    if receipt is None:
        return None

    # 复用核心直接发送分支的完成状态，核心仍负责当前批次工具及其历史和钩子。
    runner._transition_state(AgentState.DONE)
    runner.stats.end_time = time.time()
    return CallToolResult(content=[TextContent(type="text", text=receipt)])
