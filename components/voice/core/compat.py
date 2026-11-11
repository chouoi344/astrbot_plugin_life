"""为 AstrBot 4.26–4.29 的直接发送工具记录内部回执。

这里用到 AstrBot 核心循环的内部字段。版本不在已验证范围、或接口对不上时，
只关闭这一项（回落到 AstrBot 原生的结束方式），并记一次日志（第 24 条）。
"""

import time

from ...common.compat import disabled_once, version_between

FEATURE = "语音直接发送回执"
_SUPPORTS_DIRECT_TOOL_RECEIPT = version_between("4.26.0", "4.30.0")
_NEEDS_DIRECT_TOOL_HISTORY = version_between("0", "4.29.0")
_RUNNER_FIELDS = ("run_context", "_last_tool_name", "_transition_state", "stats")


def finish_direct_tool_delivery(event, tool_name: str, receipt: str | None):
    if not _SUPPORTS_DIRECT_TOOL_RECEIPT:
        disabled_once(FEATURE, "当前 AstrBot 版本不在 4.26–4.29 的已验证范围")
        return None
    try:
        from astrbot.api.provider import LLMResponse
        from astrbot.core.agent.runners.base import AgentState
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
        from mcp.types import CallToolResult, TextContent
    except ImportError as error:
        disabled_once(FEATURE, f"AstrBot 内部接口缺失（{error.name}）")
        return None

    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if not isinstance(runner, ToolLoopAgentRunner):
        # 找不到当前核心循环时，继续用原生 None 分支结束回复。
        return None
    if any(not hasattr(runner, name) for name in _RUNNER_FIELDS):
        disabled_once(FEATURE, "AstrBot 核心循环的内部字段已变化")
        return None
    if runner.run_context.context.event is not event or runner._last_tool_name != tool_name:
        return None

    if _NEEDS_DIRECT_TOOL_HISTORY and runner.get_final_llm_resp() is None:
        runner.final_llm_resp = LLMResponse(role="assistant", completion_text="")
    if receipt is None:
        return None

    # 复用核心直接发送分支的完成状态，核心仍负责当前批次工具及其历史和钩子。
    runner._transition_state(AgentState.DONE)
    runner.stats.end_time = time.time()
    return CallToolResult(content=[TextContent(type="text", text=receipt)])
