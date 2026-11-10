"""Resolve components under the one installed Natural Life plugin."""

from pathlib import Path

from astrbot.api.platform import MessageType
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

ROOT_PLUGIN_ID = "astrbot_plugin_life"
SCHEDULE_ID = "astrbot_plugin_life_scheduler_plus"
PHOTO_ID = "astrbot_plugin_life_photo"
VOICE_ID = "astrbot_plugin_life_voice"
CHAT_ID = "astrbot_plugin_life_busy"
QZONE_ID = "astrbot_plugin_qzone_life"
QQ_PLATFORM = "aiocqhttp"
QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
QQ_PLATFORMS = QQ_OFFICIAL_PLATFORMS | {QQ_PLATFORM}
SECTIONS = {
    SCHEDULE_ID: "schedule",
    PHOTO_ID: "photo",
    VOICE_ID: "voice",
    CHAT_ID: "chat",
    QZONE_ID: "qzone",
}
LEGACY_IDS = (*SECTIONS, "astrbot_plugin_life_proactive")


def is_qq_event(event):
    platform = event.get_platform_name()
    if platform == QQ_PLATFORM:
        return True
    if platform not in QQ_OFFICIAL_PLATFORMS:
        return False
    from botpy.message import C2CMessage

    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    return isinstance(raw, C2CMessage) and event.get_message_type() == MessageType.FRIEND_MESSAGE


def qq_platform(context, platform_id):
    adapter = context.get_platform_inst(platform_id)
    return adapter if adapter is not None and adapter.meta().name in QQ_PLATFORMS else None


def legacy_conflicts(context, *, check_unloaded=False):
    root = Path(get_astrbot_plugin_path()) if check_unloaded else None
    blocked = []
    for plugin_id in LEGACY_IDS:
        plugin = context.get_registered_star(plugin_id)
        if plugin is not None and plugin.activated:
            blocked.append(plugin_id)
        elif (
            plugin is None
            and root is not None
            and (root / plugin_id / "metadata.yaml").is_file()
        ):
            blocked.append(plugin_id)
    return tuple(blocked)


def get_component(context, legacy_id):
    plugin = context.get_registered_star(ROOT_PLUGIN_ID)
    if (
        not plugin
        or not plugin.activated
        or not plugin.star_cls
        or legacy_conflicts(context)
    ):
        return None
    return plugin.star_cls.components.get(legacy_id)


async def component_enabled(
    context, legacy_id, umo, event=None, *, allow_disabled=False
):
    component = get_component(context, legacy_id)
    if component is None:
        return False
    config = component.config
    if not allow_disabled:
        if legacy_id == PHOTO_ID:
            if not any(
                config[key]
                for key in ("generate_enabled", "edit_enabled", "selfie_enabled")
            ):
                return False
        elif not config["enabled"]:
            return False

    if event is not None:
        allowed = event.plugins_name
        if allowed is not None and ROOT_PLUGIN_ID not in allowed:
            return False
    scope = str(umo)
    allowed = context.get_config(scope).get("plugin_set", ["*"])
    if "*" not in allowed:
        if ROOT_PLUGIN_ID not in allowed:
            return False
        legacy_allowed = set(allowed).intersection(SECTIONS)
        if legacy_allowed and legacy_id not in legacy_allowed:
            return False
    return await SessionPluginManager.is_plugin_enabled_for_session(
        scope, ROOT_PLUGIN_ID
    ) and await SessionPluginManager.is_plugin_enabled_for_session(scope, legacy_id)
