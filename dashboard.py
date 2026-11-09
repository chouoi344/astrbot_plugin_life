"""AstrBot's authenticated routes for the Natural Life view."""

import asyncio
import base64
import inspect
import json
import math
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .components.chat.main import ATTENTION_CHANCE, MAX_ATTEMPTS
from .components.chat.proactive.triggers import rest_type as chat_rest_type
from .components.chat.queue_store import Scope
from .components.photo.core.life import global_persona_id
from .components.schedule.core.engine import UNRECORDED_ACTIVITY
from .components.schedule.core.outfits import (
    format_outfit_description, project_day_outfits, slot_outfit_key,
)
from .components.schedule.core.timeline import find_slots, project_slot_rest
from .components.voice.core.config import Settings as VoiceSettings
from .components.voice.core.provider import BreezeClient
from .config import ComponentConfig
from .dashboard_photos import PhotoHistory
from .dashboard_qzone import qzone_status
from .runtime import CHAT_ID, PHOTO_ID, QQ_PLATFORM, QZONE_ID, ROOT_PLUGIN_ID, SCHEDULE_ID, VOICE_ID

_NO_CACHE = {"Cache-Control": "no-store"}
_ASSET_ID = re.compile(r"[a-f0-9]{64}")
_PHONE = {"free", "glance", "offline"}
_CHAT_SETTING_DEFAULTS = {
    "attention_chance": ATTENTION_CHANCE,
    "reply_delay_min_seconds": 8,
    "reply_delay_max_seconds": 45,
}
_PLACEHOLDER_VOICES = {"已保存的音色", "原音色（暂不可用）"}


class DashboardError(ValueError):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _dashboard_admin(context):
    # Authentication and signature verification belong to AstrBot. Scoped
    # credentials, including the view's asset token, cannot change settings.
    username = request.username
    dashboard = context.get_config().get("dashboard", {})
    if (
        not username or username.startswith("api_key:") or not isinstance(dashboard, dict)
        or username != dashboard.get("username")
    ):
        return False
    authorization = request.headers.get("authorization", "").strip()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        token = request.cookies.get("astrbot_dashboard_jwt", "")
    try:
        _, payload, _ = token.strip().split(".")
        claims = json.loads(base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        ))
    except (ValueError, UnicodeError):
        return False
    return (
        isinstance(claims, dict)
        and claims.get("username") == username
        and claims.get("token_type") in (None, "dashboard", "access")
        and not {"scope", "scopes", "plugin_name", "page_name"} & claims.keys()
    )


def _query_id(name, *, required=False):
    values = request.query.getlist(name)
    if not values and not required:
        return None
    if len(values) != 1 or not _ASSET_ID.fullmatch(values[0]):
        raise DashboardError("成片或会话编号无效。")
    return values[0]


async def _global_persona(context):
    persona_id = global_persona_id(context)
    if not persona_id:
        return None, None
    manager = context.persona_manager
    lookup = getattr(manager, "get_persona_v3_by_id", None) or getattr(manager, "get_persona", None)
    if not callable(lookup):
        return persona_id, None
    persona = lookup(persona_id)
    if inspect.isawaitable(persona):
        persona = await persona
    if global_persona_id(context) != persona_id:
        return None, None
    if not persona:
        return persona_id, None
    name = persona.get("name") if isinstance(persona, dict) else getattr(persona, "name", None)
    return persona_id, str(name or persona_id)


def _saved_day(store, date):
    filename = f"days/{date.isoformat()}.json"
    if filename in store._cache:
        return store._cache[filename]
    # JsonStore.read moves corrupt files for recovery. Keep the original
    # in place when an administrator only opens the view.
    try:
        data = json.loads(store._path(filename).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError):
        raise DashboardError("已保存的日程暂时无法读取，请检查日程记录。", 500) from None
    if not isinstance(data, dict):
        raise DashboardError("已保存的日程暂时无法读取，请检查日程记录。", 500)
    return data


def _current_life(scheduler, persona_id):
    if scheduler is None or not scheduler.config["enabled"]:
        return None, {}
    engine = scheduler.engine
    if (
        engine.closed or not persona_id
        or engine.setup.state.get("persona_id") != persona_id
        or engine.setup.state.get("status") != "ready"
        or engine.config.get("profile_persona_id") != persona_id
        or engine.profile_empty()
    ):
        return None, {}
    # get_current_state() refreshes promise status. Read only the day and slot
    # needed by the view so opening a panel does not change any stored facts.
    business_day = engine.window()[0].date()
    day = _saved_day(engine.store, business_day)
    if day.get("status") != "ok":
        day = _saved_day(engine.store, business_day - timedelta(days=1))
    if day.get("status") != "ok":
        return None, {}
    current, _ = find_slots(day.get("actual", []), engine.now())
    if not current or current.get("activity") == UNRECORDED_ACTIVITY:
        return None, day
    return project_slot_rest(current), day


def _phone(slot):
    value = slot.get("phone") if slot else None
    return value if value in _PHONE else None


def _save_chat_settings(plugin, payload):
    if not isinstance(payload, dict) or payload.keys() != _CHAT_SETTING_DEFAULTS.keys():
        raise DashboardError("请填写偶尔看手机时的主动告知概率和补回等待时间。")
    chance = payload["attention_chance"]
    delay_min = payload["reply_delay_min_seconds"]
    delay_max = payload["reply_delay_max_seconds"]
    if type(chance) not in (int, float) or not 0 <= chance <= 1:
        raise DashboardError("主动告知概率请填 0 到 1 之间的数字。")
    if type(delay_min) is not int or type(delay_max) is not int or not 0 <= delay_min <= delay_max:
        raise DashboardError("等待时间请填非负整数，最短时间不能超过最长时间。")

    config = ComponentConfig(plugin.config, "chat", plugin._config_write_lock)
    with plugin._config_write_lock:
        before = config.copy()
        latest = {**before, **config.read_saved_config()}
        try:
            config.save_config({**latest, **payload})
        except Exception:
            plugin.config[config.section] = before
            raise
        chat = plugin.components.get(CHAT_ID)
        if chat is not None:
            chat.attention_chance = chance
            chat.reply_delay_min = delay_min
            chat.reply_delay_max = delay_max


def _voice_choices(voice):
    field = voice.config.schema["voice_id"]
    return {
        voice_id: name for voice_id, name in zip(field["options"], field["labels"])
        if voice_id
    }


def _voice_result(voice, admin):
    if voice is None:
        return {
            "status": "unavailable", "voice_id": None, "voice_name": None,
            "configured": False, "can_write": False, "is_admin": admin, "options": [],
            "availability": "unavailable", "message": "语音组件暂未就绪。",
        }
    settings = voice.settings
    choices = _voice_choices(voice)
    selected = choices.get(settings.voice_id)
    available = {key: label for key, label in choices.items() if label not in _PLACEHOLDER_VOICES}
    if not settings.enabled:
        availability, message = "disabled", "语音功能已关闭。"
    elif not settings.api_key:
        availability, message = "not_configured", "请在拟人生活设置中填写语音密钥。"
    elif not available:
        availability, message = "no_options", "暂无已加载的可选音色。可在拟人生活设置中保存语音配置，重新加载音色列表。"
    elif not settings.voice_id:
        availability, message = "not_configured", "请选择一个已加载的音色。"
    elif settings.voice_id not in available:
        availability, message = "unverified", "当前音色暂未在已加载清单中确认，可以重新选择。"
    else:
        availability, message = "cached", "当前音色在已加载清单中，未试听或核验实际合成。"
    return {
        "status": "ready" if settings.enabled else "disabled",
        "voice_id": settings.voice_id or None,
        "voice_name": selected or settings.voice_id or None,
        "configured": settings.configured,
        "is_admin": admin,
        "can_write": bool(admin and settings.api_key and any(key != settings.voice_id for key in available)),
        "options": [{"id": key, "name": name} for key, name in available.items()],
        "availability": availability,
        "message": message,
    }


def _save_voice(plugin, voice, voice_id):
    if voice is None or voice._closing:
        raise DashboardError("语音组件暂未就绪。", 409)
    choices = _voice_choices(voice)
    if voice_id not in choices or choices[voice_id] in _PLACEHOLDER_VOICES:
        raise DashboardError("这个音色不在已加载的列表中，请刷新后重新选择。", 409)

    config = voice.config
    with plugin._config_write_lock:
        latest = {**config.copy(), **config.read_saved_config()}
        saved = VoiceSettings.from_config(latest)
        if not saved.api_key or saved.api_key != voice.settings.api_key or saved.base_url != voice.settings.base_url:
            raise DashboardError("语音服务设置已改变，请刷新后重试。", 409)
        settings = replace(saved, voice_id=voice_id)
        client = voice.client
        if client is None:
            directory = Path(get_astrbot_data_path()) / "plugin_data" / VOICE_ID / "audio"
            client = BreezeClient(settings.api_key, voice_id, directory, base_url=settings.base_url)
        before = config.copy()
        try:
            config.save_config({**latest, "voice_id": voice_id})
        except Exception:
            plugin.config[config.section] = before
            raise
        client.select_voice(voice_id)
        voice.settings = settings
        voice.client = client


class NaturalLifeDashboard:
    def __init__(self, plugin):
        self.plugin = plugin
        self.photo_history = PhotoHistory(plugin)

    def register(self):
        context = self.plugin.context
        register = getattr(context, "register_web_api", None)
        if not callable(register) or not isinstance(getattr(context, "registered_web_apis", None), list):
            return
        for suffix, method, handler in (
            ("overview", "GET", self.overview),
            ("chat", "GET", self.chat),
            ("chat/save", "POST", self.chat_save),
            ("photos", "GET", self.photos),
            ("photos/image", "GET", self.photo_image),
            ("voice", "GET", self.voice),
            ("voice/save", "POST", self.voice_save),
            ("qzone", "GET", self.qzone),
        ):
            register(f"/{ROOT_PLUGIN_ID}/dashboard/{suffix}", handler, [method], "拟人生活视图")

    def unregister(self):
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [item for item in routes if getattr(item[1], "__self__", None) is not self]

    async def _respond(self, action):
        try:
            data = await asyncio.wait_for(action(), 12)
            return json_response(data, headers=_NO_CACHE)
        except DashboardError as error:
            return json_response(
                {"status": "error", "message": str(error)},
                status_code=error.code, headers=_NO_CACHE,
            )
        except Exception as error:
            logger.warning("[拟人生活] 视图读取或保存失败（%s，%s）。", action.__name__, type(error).__name__)
            return json_response(
                {"status": "error", "message": "这一区暂时无法读取或保存，请稍后刷新。"},
                status_code=500, headers=_NO_CACHE,
            )

    async def overview(self):
        return await self._respond(self._overview)

    async def _overview(self):
        persona_id, name = await _global_persona(self.plugin.context)
        result = {
            "status": "unavailable", "persona_id": persona_id, "persona_name": name,
            "activity": None, "location": None, "outfit": None, "mood": None,
            "mood_intensity": None, "phone": None,
        }
        scheduler = self.plugin.components.get(SCHEDULE_ID)
        if not self.plugin.config["schedule"]["enabled"]:
            return {**result, "status": "disabled", "message": "日程功能已关闭。"}
        if not name:
            return {**result, "status": "unbound", "message": "还没有可读取的 AstrBot 全局默认人设。"}
        current, day = _current_life(scheduler, persona_id)
        if current is None:
            return {**result, "message": "当前人设还没有可显示的日程片段。"}

        outfit_key = slot_outfit_key(current, day)
        outfits = project_day_outfits(day, scheduler.config.get("wardrobe", []))
        outfit = next((row["description"] for row in outfits if row["key"] == outfit_key), "")
        return {
            **result, "status": "ready",
            "activity": current.get("activity") or None,
            "location": current.get("location") or None,
            "outfit": outfit or format_outfit_description(current.get("outfit", "")) or None,
            "mood": current.get("mood") or None,
            "mood_intensity": current.get("mood_intensity") or None,
            "phone": _phone(current),
        }

    async def chat(self):
        return await self._respond(self._chat)

    async def _chat(self):
        component = self.plugin.components.get(CHAT_ID)
        config = self.plugin.config["chat"]
        result = {
            "status": "unavailable", "persona_id": None, "pending_count": None,
            "settings": {key: config.get(key, default) for key, default in _CHAT_SETTING_DEFAULTS.items()},
            "can_write": _dashboard_admin(self.plugin.context),
            "uncertain_count": None, "waiting_reason": "", "phone": None,
            "reply_wait_seconds": None, "proactive_sent_today": None,
            "proactive_daily_limit": config.get("daily_max") if config.get("proactive_enabled") else None,
            "proactive_waiting_response": None,
            "proactive_reason": None,
        }
        if not config["enabled"]:
            return {**result, "status": "disabled", "waiting_reason": "私聊功能已关闭。"}
        if component is None:
            return {**result, "waiting_reason": "私聊组件暂未就绪。"}
        adapter = self.plugin.context.get_platform_inst(component.platform_id) if component.enabled else None
        if adapter is not None and adapter.meta().name != QQ_PLATFORM:
            return {**result, "status": "unbound", "waiting_reason": "请选择 QQ 个人号私聊作为目标。"}
        if not component.enabled or not component.session or component.account_conflict:
            return {**result, "status": "unbound", "waiting_reason": "还没有可核对的 QQ 个人号目标私聊和机器人账号。"}

        session = component.session
        persona_id, conversation_id = await component._current_role()
        current_id = await self.plugin.context.conversation_manager.get_curr_conversation_id(component.umo)
        if (
            self.plugin.components.get(CHAT_ID) is not component or component.closed
            or session != component.session or not persona_id or not conversation_id
            or current_id != conversation_id
        ):
            return {**result, "status": "unbound", "waiting_reason": "当前私聊还没有可读取的人设或对话。"}

        scope = Scope(session, persona_id, conversation_id)
        counts = dict(component.store._db.execute(
            "SELECT status,COUNT(*) FROM messages WHERE session=? AND persona=? AND conversation=? "
            "AND status IN ('pending','uncertain') GROUP BY status",
            (scope.session, scope.persona, scope.conversation),
        ))
        pending = counts.get("pending", 0)
        uncertain = counts.get("uncertain", 0)
        scheduler = self.plugin.components.get(SCHEDULE_ID)
        slot, day = _current_life(scheduler, persona_id)
        phone = _phone(slot)
        if slot and chat_rest_type(slot) != "none":
            phone = "offline"
        remaining = None
        if component.busy_reply_delay and component.busy_reply_delay[0] == scope:
            remaining = max(0, math.ceil(component.busy_reply_delay[2] - time.monotonic())) or None

        if component.inflight and pending:
            reason = "正在处理这一轮待回消息。"
        elif not pending:
            reason = "有消息的送达结果待确认。" if uncertain else "当前没有待回消息。"
        elif not component.busy_enabled:
            reason = "忙碌补回已关闭，待回消息仍保留。"
        elif not component.transport.can_deliver(component.platform_id, component.bot_id, component.user_id):
            reason = "正在等私聊连接恢复。"
        elif phone == "offline":
            reason = "当前看不了手机，等可以看手机时处理。"
        elif phone == "glance":
            reason = "当前只能偶尔看手机，待回消息仍保留。"
        elif phone is None:
            reason = "正在等当前人设的日程可用。"
        elif remaining:
            reason = "刚空下来，稍后会检查待回消息。"
        elif component.failed_attempts >= MAX_ATTEMPTS and (
            batch := component.store.claim(scope)
        ) and batch.revision == component.failed_batch:
            reason = "补回暂未送出，收到新消息后会再尝试。"
        else:
            reason = "正在等当前聊天处理结束或下一次补回检查。"

        today_sent = waiting = None
        proactive_reason = None if component.proactive_settings.enabled else "主动消息已关闭。"
        ledger = component.chat_ledger
        if (
            component.proactive_settings.enabled and ledger is not None
            and ledger.data["bot_self_id"] == component.bot_id
            and ledger.data["platform_name"] == component.platform_type
        ):
            now = scheduler.engine.now() if scheduler else datetime.now(timezone.utc)
            today_sent = ledger.day(now.date().isoformat())["rounds"]
            max_unanswered = component.proactive_settings.max_unanswered
            if scheduler and day.get("date") == scheduler.engine.window()[0].date().isoformat():
                max_unanswered = day.get("max_unanswered", max_unanswered)
            waiting = ledger.data["unanswered"] >= max_unanswered
            if ledger.data["hold"] == "user":
                proactive_reason = "应用户要求，暂时不主动发消息。"
            elif waiting:
                proactive_reason = "已到连续主动未回应上限，正在等对方回应。"
            elif today_sent >= component.proactive_settings.daily_max:
                proactive_reason = "今天的主动消息次数已用完。"
            elif phone in {"offline", "glance"}:
                proactive_reason = "等有空看手机时再考虑发主动消息。"
            elif phone is None:
                proactive_reason = "正在等当前人设的日程可用。"
            else:
                proactive_reason = "正在等待合适的主动聊天时机。"

        return {
            **result, "status": "ready", "persona_id": persona_id, "pending_count": pending,
            "uncertain_count": uncertain, "waiting_reason": reason, "phone": phone,
            "reply_wait_seconds": remaining, "proactive_sent_today": today_sent,
            "proactive_waiting_response": waiting,
            "proactive_reason": proactive_reason,
        }

    async def chat_save(self):
        return await self._respond(self._chat_save)

    async def _chat_save(self):
        if not _dashboard_admin(self.plugin.context):
            raise DashboardError("请使用 AstrBot 面板管理员账号登录后再保存。", 403)
        content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise DashboardError("请通过私聊互动页面保存设置。", 415)
        length = request.headers.get("content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 1024):
            raise DashboardError("设置内容过长，请重新填写。", 413)
        body = await request.body()
        if len(body) > 1024:
            raise DashboardError("设置内容过长，请重新填写。", 413)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise DashboardError("请通过私聊互动页面保存设置。") from None
        _save_chat_settings(self.plugin, payload)
        return {"status": "ok", "settings": payload}

    async def photos(self):
        return await self._respond(self._photos)

    async def _photos(self):
        photo = self.plugin.components.get(PHOTO_ID)
        persona_id, name = await _global_persona(self.plugin.context)
        result = {"persona_id": persona_id, "scope_id": None, "scopes": [], "items": []}
        if photo is None:
            return {**result, "status": "unavailable", "message": "照片组件暂未就绪。"}
        if not name:
            return {**result, "status": "unbound", "message": "还没有可读取的全局默认人设。"}
        history = await self.photo_history.read(photo, persona_id, _query_id("scope"))
        if global_persona_id(self.plugin.context) != persona_id:
            return {**result, "status": "unbound", "message": "人设已改变，请刷新查看。"}
        enabled = photo.settings and any(
            (photo.settings.generate_enabled, photo.settings.edit_enabled, photo.settings.selfie_enabled)
        )
        return {**result, **history, "status": "ready" if enabled else "disabled"}

    async def photo_image(self):
        return await self._respond(self._photo_image)

    async def _photo_image(self):
        image_id = _query_id("id", required=True)
        scope_id = _query_id("scope", required=True)
        photo = self.plugin.components.get(PHOTO_ID)
        persona_id, name = await _global_persona(self.plugin.context)
        if photo and name:
            image = await self.photo_history.image(photo, persona_id, scope_id, image_id)
            if image and global_persona_id(self.plugin.context) == persona_id:
                return image
        raise DashboardError("这张成片暂时无法预览，请刷新后再试。", 404)

    async def voice(self):
        return await self._respond(self._voice)

    async def _voice(self):
        voice = self.plugin.components.get(VOICE_ID)
        return _voice_result(voice, _dashboard_admin(self.plugin.context))

    async def voice_save(self):
        return await self._respond(self._voice_save)

    async def _voice_save(self):
        if not _dashboard_admin(self.plugin.context):
            raise DashboardError("请使用 AstrBot 面板管理员账号登录后再保存。", 403)
        content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise DashboardError("请选择一个列表中的音色。", 415)
        length = request.headers.get("content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 1024):
            raise DashboardError("音色选择内容过长，请重新选择。", 413)
        body = await request.body()
        if len(body) > 1024:
            raise DashboardError("音色选择内容过长，请重新选择。", 413)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise DashboardError("请选择一个列表中的音色。") from None
        if (
            not isinstance(payload, dict) or payload.keys() != {"voice_id"}
            or not isinstance(payload["voice_id"], str) or not payload["voice_id"]
        ):
            raise DashboardError("请选择一个列表中的音色。")
        voice = self.plugin.components.get(VOICE_ID)
        _save_voice(self.plugin, voice, payload["voice_id"])
        return _voice_result(voice, True)

    async def qzone(self):
        return await self._respond(self._qzone)

    async def _qzone(self):
        return await qzone_status(self.plugin, self.plugin.components.get(QZONE_ID))

