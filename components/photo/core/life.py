"""Read only the active scheduler instance; never initialize its life data."""

from ....runtime import component_enabled, get_component

SCHEDULER_ID = "astrbot_plugin_life_scheduler_plus"


def global_persona_id(context):
    config = context.get_config()
    runner = config.get("agent_runner")
    if isinstance(runner, dict):
        values = runner.get("config", {})
        if runner.get("runner_type") == "local":
            value = values.get("persona", {}).get("persona_id", "")
        else:
            value = values.get("persona_id", "")
    else:
        value = config.get("provider_settings", {}).get("default_personality", "")
    return str(value or "")


async def read_life(context, event, *, fresh=False):
    """Current life state; ``fresh=True`` re-reads instead of the per-message snapshot."""
    scheduler = get_component(context, SCHEDULER_ID)
    if scheduler is None:
        return {}, "not_installed"
    if not await component_enabled(context, SCHEDULER_ID, event.unified_msg_origin, event, fresh=fresh):
        return {}, "disabled"
    state = scheduler.get_current_state(None if fresh else event)
    if not isinstance(state, dict):
        raise ValueError("日程插件返回的生活状态格式无效")
    slot = state.get("current_slot")
    if not isinstance(slot, dict) or not slot:
        return {}, "not_ready"
    # The snapshot is already independent of the scheduler; shared per message.
    return state, "ready"
