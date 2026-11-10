"""Shared schedule state, background work and persistent retry bookkeeping."""

import asyncio
import copy
import hashlib
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger as log

from .calendar_info import get_calendar_info
from .domain import (PROFILE_FIELDS, EventManager, PromiseManager, compute_routine,
                     contains_forbidden, has_change_trigger, DEFAULT_KEYWORDS,
                     effective_forbidden_interactions, sanitize_forbidden_fields,
                     sanitize_forbidden_text, user_aliases_from_config)
from .llm import LLMClient
from .images import WardrobeImageStore
from .outfits import (OUTFIT_CATEGORIES, OUTFIT_PARTS, description_key, format_outfit_description,
                      normalize_outfit, project_day_outfits, rebase_collected_outfits,
                      recent_actual_outfits, slot_outfit_key, slot_outfit_label)
from .prompts import change_prompt, dump, interaction_boundary, life_rules, schedule_prompt
from .storage import JsonStore
from .setup import LifeSetup
from .timeline import (business_window, find_slots, format_timeline, merge_future,
                       parse_local, project_slot_rest, repair_slots)
from .uploads import WardrobeUploadProcessor
from .wardrobe import WardrobeManager
from .weather import WeatherService

from .errors import describe_error, retry_delay_minutes, user_error

PROFILE_WARNING = "人设正文已更新，生活资料保留；后续生成使用最新设定，档案可按需手动更新。"
UNRECORDED_ACTIVITY = "此前安排未记录"
DAY_CHAT_FIELDS = ("attention_chance", "max_unanswered", "wait_reply_reaction", "wait_reply_recover")


def _day_chat_fields(payload):
    return {key: payload[key] for key in DAY_CHAT_FIELDS if key in payload}


def _validate_day_chat_fields(fields):
    if "attention_chance" in fields:
        chance = fields["attention_chance"]
        if type(chance) not in (int, float) or not 0 <= chance <= 1:
            raise ValueError("attention_chance应为0到1的数字")
    if "max_unanswered" in fields:
        limit = fields["max_unanswered"]
        if type(limit) is not int or not 1 <= limit <= 4:
            raise ValueError("max_unanswered应为1到4的整数")
    choices = {
        "wait_reply_reaction": ("无反应", "轻微在意", "有点在意", "会失落"),
        "wait_reply_recover": ("自动恢复", "对方回复后恢复", "需要对方主动一点"),
    }
    for key, allowed in choices.items():
        if key in fields and fields[key] not in allowed:
            raise ValueError(key + "只可填" + "、".join(allowed))


class LifeEngine:
    RUNTIME_FIELDS = ("profile", "profile_persona_hash", "profile_generated_at", "profile_persona_id", "wardrobe")

    def __init__(self, context, config, root, *, native_settings_saved=None):
        self.context, self.config = context, config
        self._native_settings_saved = config.get("settings_saved", False) is True if native_settings_saved is None else native_settings_saved
        # Native settings return this in-memory marker unchanged. Every plugin
        # save writes false, so a true disk value denotes a new native save.
        config["settings_saved"] = True
        self.store = JsonStore(root)
        self._runtime_snapshot = self.store.read("runtime_config.json", {})
        if self._native_settings_saved and self._runtime_snapshot and (
            config.get("runtime_revision", 0) < self._runtime_snapshot["revision"]
        ):
            # AstrBot saves the complete native form, including hidden runtime
            # fields captured before a background job completed. Keep the latest
            # life while accepting the form's visible model/city settings.
            config.update(copy.deepcopy(self._runtime_snapshot["values"]))
            config["runtime_revision"] = self._runtime_snapshot["revision"]
        self._initial_runtime = {key: copy.deepcopy(config.get(key)) for key in self.RUNTIME_FIELDS}
        try:
            self.tz = ZoneInfo(context.get_config().get("timezone") or "Asia/Shanghai")
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")
            log.info("未取得有效 AstrBot 时区，使用 Asia/Shanghai。")
        self.llm = LLMClient(context, config)
        self.wardrobe = WardrobeManager(config, save_config=self.save_config)
        self.wardrobe_uploads = WardrobeUploadProcessor(config, self.wardrobe, self.llm,
            WardrobeImageStore(self.store.root / "wardrobe_images"), self.store.root)
        self.weather = WeatherService(config, self.store, self.tz, persona_provider=lambda: self.llm.persona())
        self.events = EventManager(self.store, config, self.tz)
        self.promises = PromiseManager(self.store, self.tz)
        self.promises.forbidden = config.get("forbidden_interactions", [])
        self.promises.user_aliases = user_aliases_from_config(config)
        self.scheduler = AsyncIOScheduler(timezone=self.tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120})
        self.tasks = set()
        self.generation_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.profile_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.wardrobe_uploads.config_lock = self.config_lock
        self.rewrite_lock = asyncio.Lock()
        self._queued_rewrites = {}
        self.generation_task = None
        self.profile_task = None
        self._generation_window = None
        self.judge_task = None
        self.judge_queue = []
        self.last_enqueue = 0.0
        self.persona_changed = False
        self.closed = False
        self.setup = LifeSetup(self)

    def life_ready(self):
        return self.setup.ready()

    def _role_token(self):
        self.setup.assert_owner(self.setup.state.get("token"))
        return self.setup.state["token"]

    def _can_prepare(self):
        return self.life_ready() or (self.setup.state.get("status") == "preparing"
                                     and asyncio.current_task() is self.setup.task
                                     and self.setup.owns(self.setup.state.get("token")))

    def now(self):
        return datetime.now(self.tz)

    def window(self):
        return business_window(self.now(), self.config.get("schedule_time", "07:00"))

    def save_config(self, *, consume_native_event=False):
        save = getattr(self.config, "save_config", None)
        if callable(save):
            # Native save can wait for AstrBot's reload lock while an old worker
            # commits. Preserve its marker until explicit event consumption.
            path = getattr(self.config, "config_path", None)
            pending = False
            if not consume_native_event and path and Path(path).is_file():
                read_saved = getattr(self.config, "read_saved_config", None)
                disk = read_saved() if read_saved else json.loads(Path(path).read_text(encoding="utf-8-sig"))
                pending = disk.get("settings_saved") is True
                if (pending and not self._native_settings_saved and self._runtime_snapshot
                        and disk.get("runtime_revision", 0) < self._runtime_snapshot["revision"]):
                    # A native POST has overwritten the shared config with an
                    # old form while reload is waiting. This old worker stops;
                    # the new instance restores the latest completed life.
                    self.config.update(copy.deepcopy(self._runtime_snapshot["values"]))
                    self.config["runtime_revision"] = self._runtime_snapshot["revision"]
                    raise RuntimeError("原生设置已保存，插件正在重新加载，本次旧后台任务停止保存。")
            values = {key: copy.deepcopy(self.config.get(key)) for key in self.RUNTIME_FIELDS}
            changed = values != self._runtime_snapshot.get("values")
            revision = self._runtime_snapshot.get("revision", 0) + int(changed)
            previous_revision = self.config.get("runtime_revision", 0)
            self.config.update(settings_saved=pending, runtime_revision=revision)
            try:
                save()
                if changed:
                    snapshot = {"revision": revision, "values": values}
                    try:
                        self.store.write("runtime_config.json", snapshot)
                    except BaseException:
                        # Config and the protective snapshot must agree after a
                        # failed snapshot write; retain the last completed life.
                        self.config.update(copy.deepcopy(self._runtime_snapshot.get("values", self._initial_runtime)))
                        self.config["runtime_revision"] = self._runtime_snapshot.get("revision", previous_revision)
                        try:
                            save()
                        except Exception as restore_error:
                            raise OSError("生活资料快照和配置恢复都失败，准备已停止；已有快照保留，请检查文件写入权限。") from restore_error
                        raise
                    self._runtime_snapshot = snapshot
            except BaseException:
                self.config["runtime_revision"] = previous_revision
                raise
            finally:
                self.config["settings_saved"] = True

    def consume_native_settings_save(self):
        self.save_config(consume_native_event=True)

    def spawn(self, coroutine):
        if self.closed:
            coroutine.close()
            return None
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error:
                log.error("生活日程后台任务失败：%s", describe_error(error), exc_info=error)

    def start(self):
        self.reschedule()
        self.scheduler.start()
        self.spawn(self.bootstrap())

    def reschedule(self):
        try:
            hour, minute = map(int, self.config.get("schedule_time", "07:00").split(":"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (ValueError, TypeError):
            hour, minute = 7, 0
            self.config["schedule_time"] = "07:00"
            self.save_config()
            log.warning("日程时间无效，使用 07:00。")
        self.scheduler.add_job(self._daily_callback, "cron", hour=hour, minute=minute, id="daily", replace_existing=True)
        self.scheduler.add_job(self._weather_callback, "cron", hour="6,9,12,15,18,21", minute=0, id="weather", replace_existing=True)
        # A one-minute catch-up survives long sleep even when the daily cron's
        # original 120-second misfire window has elapsed.
        self.scheduler.add_job(self._maintenance_callback, "interval", minutes=1, id="maintenance", replace_existing=True)

    async def _daily_callback(self):
        await self.setup.refresh(automatic=True)
        if self.life_ready():
            self.ensure_background()

    async def _weather_callback(self):
        await self.setup.refresh(automatic=True)
        if self.life_ready():
            self.spawn(self.refresh_weather())

    async def _maintenance_callback(self):
        await self.setup.refresh(automatic=True)
        if not self.life_ready():
            return
        self.promises.refresh(self.now(), (self.current_day() or {}).get("changes", []))
        self.ensure_background()

    async def bootstrap(self):
        if self._native_settings_saved:
            self._native_settings_saved = False
            await self.setup.on_native_config_saved()
        else:
            await self.setup.refresh(automatic=True)
        if self.life_ready():
            self.ensure_background()
            self.spawn(self.wardrobe_uploads.process())
            self.restore_surprises()

    async def stop(self):
        self.closed = True
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.llm, "close", None)
        if callable(close):
            await close()
        self.judge_queue.clear()

    def profile_empty(self):
        profile = self.config.get("profile", {})
        return not any(str(profile.get(key, "") or "").strip() for key in PROFILE_FIELDS)

    async def check_persona(self):
        persona = await self.llm.persona()
        old_hash = self.config.get("profile_persona_hash", "")
        self.persona_changed = bool(old_hash and old_hash != self.llm.persona_hash(persona))
        return persona

    async def confirm_profile(self):
        await self.setup.refresh()
        if not self.life_ready():
            raise ValueError(self.setup.status()["message"])
        persona = await self.llm.persona()
        async with self.config_lock:
            self.config["profile_persona_hash"] = self.llm.persona_hash(persona)
            self.persona_changed = False
            self.save_config()

    async def generate_profile(self, force=False):
        task = self.ensure_profile_background(force=force)
        return await asyncio.shield(task) if task else False

    def ensure_profile_background(self, force=False):
        if self.closed or not self.life_ready():
            return None
        if force:
            return self.setup.begin_rebuild()
        if self.profile_task and not self.profile_task.done():
            return self.profile_task
        state = self._profile_state_for_today()
        if state.get("status") == "generating":
            self._cancel_attempt(state, "上次档案生成因重启中断")
            self.store.write("profile_generation.json", state)
        if not force:
            if not self.profile_empty() or state.get("auto_attempts", 0) >= 3:
                return None
            if state.get("retry_at") and parse_local(state["retry_at"], self.tz) > self.now():
                self._schedule_profile_retry(state)
                return None
        self.profile_task = self.spawn(self._generate_profile(force=force))
        return self.profile_task

    def _profile_state_for_today(self):
        state = self.store.read("profile_generation.json", {})
        if state.get("date") != self.now().date().isoformat():
            return {"date": self.now().date().isoformat(), "attempts": 0, "auto_attempts": 0}
        state.setdefault("auto_attempts", state.get("attempts", 0))
        return state

    def _cancel_attempt(self, state, message):
        """Refund the durable reservation once, including after a hard exit."""
        if "auto_attempts_before" in state:
            state["auto_attempts"] = state["auto_attempts_before"]
        elif state.get("automatic_attempt", True):
            state["auto_attempts"] = max(0, state.get("auto_attempts", state.get("fail_count", state.get("attempts", 0))) - 1)
        state.update(status="failed", last_error=message, retry_at=self.now().isoformat())
        state.pop("auto_attempts_before", None)
        state.pop("automatic_attempt", None)

    def _schedule_profile_retry(self, state):
        if state.get("auto_attempts", state.get("attempts", 0)) < 3 and state.get("retry_at") and self.scheduler.running:
            self.scheduler.add_job(self._profile_callback, "date", run_date=parse_local(state["retry_at"], self.tz), id="profile_retry", replace_existing=True)

    def profile_generation_state(self):
        state = self._profile_state_for_today()
        attempts = state.get("auto_attempts", 0)
        retry_at = state.get("retry_at")
        if self.profile_lock.locked() or (self.profile_task and not self.profile_task.done()):
            status, message, retry_at = "generating", "正在生成生活档案，完成后会自动显示。", None
        elif state.get("status") == "failed" or state.get("last_error"):
            exhausted = attempts >= 3
            status = "exhausted" if exhausted else "failed"
            message = "档案生成失败：" + user_error(state.get("last_error"), action="生成生活档案")
            if exhausted:
                message += " 今天的自动重试已用完，可以随时点『重新生成档案』重试。"
                retry_at = None
            elif retry_at:
                message += " 系统会在 " + parse_local(retry_at, self.tz).strftime("%H:%M") + " 自动重试，也可以现在点重新生成。"
        elif not self.profile_empty():
            status, message, retry_at = "ready", state.get("message") or "生活档案已准备好。", None
        else:
            status, message = "idle", "生活档案正在准备，可以点『重新生成档案』开始。"
        return {"status": status, "message": message, "attempts": attempts, "retry_at": retry_at}

    async def _generate_profile(self, force=False):
        if not self._can_prepare():
            raise ValueError(self.setup.status()["message"])
        token = self._role_token()
        async with self.profile_lock:
            now = self.now()
            state = self._profile_state_for_today()
            if not force:
                if not self.profile_empty() or state.get("auto_attempts", 0) >= 3:
                    return False
                if state.get("retry_at") and parse_local(state["retry_at"], self.tz) > now:
                    return False
            async with self.config_lock:
                old_profile = copy.deepcopy(self.config.get("profile", {}))
            if force:
                self.store.write("profile_backups/" + now.strftime("%Y%m%dT%H%M%S%f") + ".json", {
                    "profile": old_profile, "profile_persona_hash": self.config.get("profile_persona_hash", ""),
                    "profile_generated_at": self.config.get("profile_generated_at", "")})
            state["auto_attempts_before"] = state.get("auto_attempts", 0)
            state["automatic_attempt"] = not force
            state["attempts"] = state.get("attempts", 0) + 1
            if not force:
                state["auto_attempts"] = state.get("auto_attempts", 0) + 1
            state.update(status="generating", started_at=now.isoformat(), retry_at=None)
            self.store.write("profile_generation.json", state)
            try:
                persona = await self.llm.persona()
                prompt = (
                    "根据以下角色设定，提取或合理推演该角色的固定日常档案，档案内容简短。"
                    "若设定中已明确则严格沿用，未提及则必须紧扣其世界观、身份地位、职业属性与性格底色做同频推演，严禁给特殊身份角色强套普通学生或普通上班族模板。"
                    "请在下方对应的profile字段中体现以下维度："
                    "1. 居所环境与常驻地（格局、风格、常待角落或据点）；"
                    "2. 日常穿搭偏好（正装/制服、常服、居家服、运动/训练装的具体材质、色系与剪裁风格）；"
                    "3. 作息规律与生活习惯（惯常起床与入睡时段、核心事务节奏、饮食与下厨偏好、独处消遣方式）；"
                    "4. 常去场所与人际圈层（工作/行动核心场所、常去的休闲地点、身边固定往来的下属/搭档/朋友类型）。"
                    "注意：不要提及当前聊天对象（用户），只写角色自身独立存在的生活设定。"
                    "作息具体到几点，社交圈具体到名字，避免有时/可能等空话。"
                    + interaction_boundary() +
                    "identity含身份年龄段和城市，routine_workday/routine_restday明确起床和入睡时间。"
                    "social_circle每行名字｜关系｜描述。只输出JSON，各profile字段字符串，city为提取的城市（无则空）。"
                    "\n格式：" + dump({"profile": {key: "" for key in PROFILE_FIELDS}, "city": ""}) +
                    "\n人设：" + persona + "\nforbidden_interactions：" + dump(effective_forbidden_interactions(self.config.get("forbidden_interactions", []))) +
                    "\nuser_aliases（这些昵称也指用户本人）：" + dump(list(user_aliases_from_config(self.config))))
                result = await self.llm.json(prompt, purpose="profile")
                profile = result.get("profile", result)
                if not isinstance(profile, dict) or not any(profile.get(k) for k in PROFILE_FIELDS):
                    raise ValueError("档案内容为空")
                profile = {key: str(profile.get(key, "") or "") for key in PROFILE_FIELDS}
                profile, removed = sanitize_forbidden_fields(profile, self.config.get("forbidden_interactions", []), user_aliases_from_config(self.config))
                if removed:
                    log.info("生活档案已移除 %s 句与用户的受限互动，其余内容照常保存。", removed)
                if not any(value.strip() for value in profile.values()):
                    raise ValueError("生成的档案没有可保留的生活内容，请重新生成")
                async with self.config_lock:
                    self.setup.assert_owner(token)
                    await self.llm.selected_persona(self.setup.selected_id)
                    self.setup.assert_owner(token)
                    if self.config.get("profile", {}) != old_profile:
                        state.update(status="ready", retry_at=None, last_error="", message="已保留你刚刚修改的档案。")
                        state.pop("auto_attempts_before", None)
                        state.pop("automatic_attempt", None)
                        self.store.write("profile_generation.json", state)
                        self._trigger_profile_backfill()
                        return False
                    keys = ("profile", "profile_persona_hash", "profile_generated_at", "weather")
                    before = {key: copy.deepcopy(self.config.get(key)) for key in keys}
                    absent = {key for key in keys if key not in self.config}
                    try:
                        self.config["profile"] = profile
                        self.config["profile_persona_hash"] = self.llm.persona_hash(persona)
                        self.config["profile_generated_at"] = self.now().isoformat()
                        weather_config = self.config.setdefault("weather", {})
                        extracted_city = result.get("city")
                        if isinstance(extracted_city, str) and extracted_city.strip() and (
                            not weather_config.get("city") or self.weather.using_fallback()
                        ):
                            weather_config["city"] = extracted_city.strip()
                        self.save_config()
                    except Exception:
                        for key, value in before.items():
                            if key in absent:
                                self.config.pop(key, None)
                            else:
                                self.config[key] = value
                        raise
                    self.persona_changed = False
                state.update(status="ready", retry_at=None, last_error="", message="生活档案已生成。", setup_token=token)
                state.pop("auto_attempts_before", None)
                state.pop("automatic_attempt", None)
                self.store.write("profile_generation.json", state)
                self._trigger_profile_backfill()
                before_city = str((before.get("weather") or {}).get("city", "") or "").strip()
                current_city = str(self.config.get("weather", {}).get("city", "") or "").strip()
                refresh_location = current_city != before_city or self.weather.using_fallback()
                if refresh_location:
                    self.weather.invalidate_location()
                if refresh_location and not (self.generation_task and not self.generation_task.done()):
                    self.spawn(self.refresh_weather())
                return True
            except asyncio.CancelledError:
                if not self.setup.owns(token):
                    raise
                self._cancel_attempt(state, "档案生成因插件重载中断，可以重新生成")
                self.store.write("profile_generation.json", state)
                raise
            except Exception as exc:
                if not self.setup.owns(token):
                    raise
                delay = retry_delay_minutes(exc, state.get("auto_attempts", 0), content_minutes=30)
                state.update(status="failed", last_error=describe_error(exc), retry_at=(self.now() + timedelta(minutes=delay)).isoformat())
                state.pop("auto_attempts_before", None)
                state.pop("automatic_attempt", None)
                self.store.write("profile_generation.json", state)
                log.warning("生成生活档案失败：%s", describe_error(exc))
                self._schedule_profile_retry(state)
                if force:
                    raise
                return False

    async def _profile_callback(self):
        self.ensure_profile_background()

    def ensure_background(self, force=False):
        return self._ensure_generation(force=force)

    def _needs_profile_backfill(self, day):
        return bool(day and day.get("status") == "ok" and day.get("profile_missing_at_generation") and not self.profile_empty())

    def _trigger_profile_backfill(self):
        start, _ = self.window()
        if self._needs_profile_backfill(self.store.day(start.date().isoformat())):
            self.ensure_background()

    async def _run_schedule(self, **kwargs):
        if not self.life_ready():
            return None
        return await self._generate(**kwargs)

    def _recover_schedule_attempt(self, day):
        changed = False
        for state in [day, day.get("window_generation", {}), day.get("profile_backfill_generation", {})]:
            if state.get("status") == "generating":
                self._cancel_attempt(state, "上次生成因重启中断")
                changed = True
        if changed:
            self.store.save_day(day)

    def _ensure_generation(self, force=False, extra="", outfit_override=None, automatic=None):
        if self.closed or not self.life_ready():
            return None
        if self.generation_task and not self.generation_task.done():
            return self.generation_task
        start, _ = self.window()
        day = self.store.day(start.date().isoformat())
        if day:
            self._recover_schedule_attempt(day)
        automatic = not force if automatic is None else automatic
        if automatic or not force:
            if day and day.get("status") == "ok" and self._day_covers_window(day) and not self._needs_profile_backfill(day):
                return None
            retry_state = self._window_generation_state(day or {})
            if retry_state.get("status") in {"failed", "generating"}:
                if retry_state.get("auto_attempts", retry_state.get("fail_count", 0)) >= 4:
                    return None
                retry_at = retry_state.get("retry_at")
                if retry_at and parse_local(retry_at, self.tz) > self.now():
                    self.schedule_retry(retry_state)
                    return None
            if day and day.get("status") == "ok":
                force = True
        self._generation_window = start.isoformat()
        self._generation_automatic = automatic
        self.generation_task = self.spawn(self._run_schedule(force=force, extra=extra, outfit_override=outfit_override))
        if self.generation_task:
            self.generation_task.add_done_callback(self._generation_done)
        return self.generation_task

    def _window_generation_state(self, day):
        """A changed business window owns its retry budget without losing facts."""
        start, end = self.window()
        if day.get("plan") and (not self._day_covers_window(day) or self._needs_profile_backfill(day)):
            field = "window_generation" if not self._day_covers_window(day) else "profile_backfill_generation"
            state = day.get(field, {})
            if state.get("business_start") == start.isoformat() and state.get("business_end") == end.isoformat():
                return state
            return {}
        return day

    def _generation_done(self, task):
        if not self.closed and self.generation_task is task:
            if self._generation_window != self.window()[0].isoformat():
                self._ensure_generation(force=True, automatic=True)
            else:
                self._trigger_profile_backfill()

    def _day_covers_window(self, day):
        start, end = self.window()
        actual = day.get("actual", [])
        if not isinstance(actual, list) or not actual:
            return False
        try:
            return parse_local(actual[0]["start"], self.tz) <= start and parse_local(actual[-1]["end"], self.tz) == end
        except (KeyError, ValueError, TypeError):
            return False

    def _rewrite_base(self, old, start, end, now):
        """Preserve recorded history even when a changed boundary selects another file."""
        original = copy.deepcopy((old or {}).get("actual", []))
        sources = list(original)
        for offset in (-1, 0, 1):
            record = self.store.day((start.date() + timedelta(days=offset)).isoformat())
            if record and record.get("status") == "ok" and record.get("date") != (old or {}).get("date"):
                sources.extend(copy.deepcopy(record.get("actual", [])))
        if not original and not find_slots(sources, now)[0]:
            return None
        cursor = min(start, parse_local(original[0]["start"], self.tz)) if original else start
        result = []
        while cursor < now:
            slot = next((slot for slot in sources if parse_local(slot["start"], self.tz) <= cursor < parse_local(slot["end"], self.tz)), None)
            if slot:
                point = min(now, parse_local(slot["end"], self.tz))
                value = copy.deepcopy(slot)
                value.update(start=cursor.isoformat(), end=point.isoformat())
            else:
                following = [parse_local(slot["start"], self.tz) for slot in sources if parse_local(slot["start"], self.tz) > cursor]
                point = min([now] + following)
                value = {"start": cursor.isoformat(), "end": point.isoformat(), "activity": UNRECORDED_ACTIVITY, "location": "", "phone": "glance", "mood": "", "outfit_type": "home"}
            result.append(value)
            cursor = point
        if now < end:
            result.append({"start": now.isoformat(), "end": end.isoformat(), "activity": "等待更新后续安排", "location": "", "phone": "glance", "mood": "", "outfit_type": "home"})
        return result

    def schedule_generation_state(self, day=None):
        start, _ = self.window()
        day = day if day is not None else self.store.day(start.date().isoformat())
        day = day or {}
        state = self._window_generation_state(day)
        attempts = state.get("auto_attempts", state.get("fail_count", 0))
        retry_at = state.get("retry_at")
        if self.generation_lock.locked() or (self.generation_task and not self.generation_task.done()):
            status, message, retry_at = "generating", "正在生成今天的日程，完成后会自动显示。", None
            if self.profile_empty() and self.profile_task and not self.profile_task.done():
                message = "正在生成生活档案，完成后接着准备今天的日程。"
        elif day.get("status") == "ok" and self._day_covers_window(day) and not self._needs_profile_backfill(day):
            status, message, retry_at = "ready", "今天的日程已准备好。", None
        elif state.get("status") in {"failed", "generating"}:
            exhausted = attempts >= 4
            status = "exhausted" if exhausted else "failed"
            message = "日程生成失败：" + user_error(state.get("last_error"), action="生成日程")
            if exhausted:
                message += " 今天的自动重试已用完，可以随时点『立即重试』。"
                retry_at = None
            elif retry_at:
                message += " 系统会在 " + parse_local(retry_at, self.tz).strftime("%H:%M") + " 自动重试，也可以点『立即重试』。"
        else:
            status, message = "idle", "正在准备今天的日程。"
        return {"status": status, "message": message, "attempts": attempts, "retry_at": retry_at}

    def schedule_retry(self, day):
        if day.get("auto_attempts", day.get("fail_count", 0)) < 4 and day.get("retry_at") and self.scheduler.running:
            self.scheduler.add_job(self._daily_callback, "date", run_date=parse_local(day["retry_at"], self.tz), id="generation_retry", replace_existing=True)

    def current_day(self):
        start, _ = self.window()
        day = self.store.day(start.date().isoformat())
        if day and day.get("status") == "ok":
            return day
        previous = self.store.day((start.date() - timedelta(days=1)).isoformat())
        return previous if previous and previous.get("status") == "ok" else day

    async def recap_yesterday(self, today):
        if not self._can_prepare():
            return None
        token = self._role_token()
        previous = self.store.day((today - timedelta(days=1)).isoformat())
        if not previous or previous.get("status") != "ok" or previous.get("recap"):
            return previous
        try:
            text = await self.llm.call("把昨日实际日程和变更压缩为100字以内中文回顾，包含影响今天的晚睡和事件，只输出回顾：" + dump({
                "actual": previous.get("actual", []), "changes": previous.get("changes", [])}), purpose="recap")
            recap = text[:100]
        except Exception:
            recap = "；".join(str(slot.get("activity", "")) for slot in previous.get("actual", []))[:100]
        previous["recap"] = recap
        self.setup.assert_owner(token)
        self.store.save_day(previous)
        return previous

    async def generate(self, force=False, extra="", outfit_override=None):
        await self.setup.refresh()
        if not self.life_ready():
            raise ValueError(self.setup.status()["message"])
        if force and (extra or outfit_override):
            key = json.dumps({"extra": extra, "outfit": outfit_override}, sort_keys=True, ensure_ascii=False, default=str)
            task = self._queued_rewrites.get(key)
            if task is None or task.done():
                task = self.spawn(self._rewrite_after_current(extra, outfit_override))
                if task:
                    self._queued_rewrites[key] = task
                    task.add_done_callback(lambda done, key=key: self._queued_rewrites.pop(key, None) if self._queued_rewrites.get(key) is done else None)
            return await asyncio.shield(task) if task else None
        task = self._ensure_generation(force=force, extra=extra, outfit_override=outfit_override)
        if task:
            return await asyncio.shield(task)
        start, _ = self.window()
        return self.store.day(start.date().isoformat())

    async def _rewrite_after_current(self, extra, outfit_override):
        async with self.rewrite_lock:
            while self.generation_task and not self.generation_task.done():
                try:
                    await asyncio.shield(self.generation_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failed earlier request does not consume this explicit
                    # manual request's outfit or additional requirements.
                    pass
            task = self._ensure_generation(force=True, extra=extra, outfit_override=outfit_override)
            return await asyncio.shield(task) if task else None

    async def _generate(self, force=False, extra="", outfit_override=None):
        if not self._can_prepare() or self.profile_empty():
            raise ValueError(self.setup.status()["message"])
        token = self._role_token()
        async with self.generation_lock:
            start, end = self.window()
            key = start.date().isoformat()
            old = self.store.day(key)
            if old and old.get("status") == "ok" and not force and not self._needs_profile_backfill(old):
                return old
            has_original = bool(old and old.get("plan"))
            repairing_window = has_original and not self._day_covers_window(old)
            backfilling_profile = self._needs_profile_backfill(old)
            state_field = "window_generation" if repairing_window else "profile_backfill_generation" if backfilling_profile else None
            previous_state = self._window_generation_state(old or {})
            auto_before = previous_state.get("auto_attempts", previous_state.get("fail_count", 0))
            automatic = getattr(self, "_generation_automatic", not force)
            auto_attempts = auto_before + (1 if automatic else 0)
            total_attempts = previous_state.get("total_attempts", previous_state.get("fail_count", 0)) + 1
            window_state = {"business_start": start.isoformat(), "business_end": end.isoformat(),
                            "status": "generating", "auto_attempts": auto_attempts, "total_attempts": total_attempts,
                            "auto_attempts_before": auto_before, "automatic_attempt": automatic,
                            "retry_at": None, "started_at": self.now().isoformat()}
            if state_field:
                record = copy.deepcopy(old)
                record[state_field] = window_state
                self.store.save_day(record)
            if not has_original:
                record = copy.deepcopy(old or {"date": key, "fail_count": 0})
                for field, default in {"last_error": "", "plan": [], "actual": [], "changes": [], "outfits": {},
                                       "weather": {}, "surprises": [], "recap": "", "day_summary": "", "mood_overall": ""}.items():
                    record.setdefault(field, default)
                record.update(status="generating", generation_started_at=self.now().isoformat(), auto_attempts=auto_attempts, total_attempts=total_attempts,
                              auto_attempts_before=auto_before, automatic_attempt=automatic)
                self.store.save_day(record)
            try:
                persona = await self.check_persona()
                weather = await self.weather.refresh(self.now())
                calendar = get_calendar_info(start.date())
                yesterday = await self.recap_yesterday(start.date())
                history = [self.store.day((start.date() - timedelta(days=i)).isoformat()) for i in range(1, max(
                    int(self.config.get("reference_history_days", 3)), int(self.config.get("outfit_no_repeat_days", 3))) + 1)]
                history = [d for d in history if d and d.get("status") == "ok"]
                outfits = copy.deepcopy(old.get("outfits", {})) if has_original else self.wardrobe.select_outfits(
                    start.date(), weather, history, int(self.config.get("outfit_no_repeat_days", 3)))
                outfits = {kind: normalize_outfit(outfits.get(kind), kind) for kind in OUTFIT_CATEGORIES}
                sport_outfits = copy.deepcopy(old.get("sport_outfits", [])) if has_original and old.get("sport_outfits") else self.wardrobe.select_sports(
                    start.date(), weather, history, int(self.config.get("outfit_no_repeat_days", 3)))
                sport_outfits = [normalize_outfit(value, "sports") for value in sport_outfits]
                if outfit_override:
                    outfits["outdoor"] = normalize_outfit({**outfit_override, "category": "外出"}, "outdoor")
                # A changed card can change occupation or sleep hours. Do not
                # turn the historical profile into hard wake/sleep constraints.
                routine = ({"mode": "latest_persona", "jitter_minutes": int(self.config.get("routine_jitter_minutes", 20))}
                           if self.persona_changed else compute_routine(self.config.get("profile", {}), start.date(), self.tz,
                               workday=calendar["is_workday"], jitter_minutes=int(self.config.get("routine_jitter_minutes", 20)), yesterday=yesterday))
                relevant_events = self.events.relevant(start.date())
                allow_new = not has_original and random.random() < float(self.config.get("auto_event_probability", 0)) and not any(
                    e.get("source") == "ai" and e.get("status") in {"active", "upcoming"} for e in relevant_events)
                profile_was_empty = self.profile_empty()
                recent_worn = recent_actual_outfits(history, start.date(), int(self.config.get("outfit_no_repeat_days", 3)))
                recent_keys = {description_key(value["description"]) for value in recent_worn if value["description"]}
                inputs = {"timezone": str(self.tz), "business_start": start.isoformat(), "business_end": end.isoformat(),
                          "calendar": calendar, "weather": weather, "persona": persona, "profile": copy.deepcopy(self.config.get("profile", {})),
                          "profile_is_historical": self.persona_changed,
                          "allow_ai_weather": not self.weather.settings.get("enabled", True) or not str(self.weather.settings.get("city", "") or "").strip(),
                          "routine": routine, "history": [{"date": d["date"], "recap": d.get("recap", "")} for d in history[:int(self.config.get("reference_history_days", 3))]],
                          "yesterday_last_slot": (yesterday or {}).get("actual", [None])[-1:] or [],
                          "events": relevant_events, "today_promises": self.promises.due_for_day(start, end), "outfits": outfits,
                          "sport_outfits": sport_outfits,
                          "recent_worn_outfits": recent_worn,
                          "outfit_style_context": {"identity": self.config.get("profile", {}).get("identity", ""),
                              "hobbies_places": self.config.get("profile", {}).get("hobbies_places", ""), "persona": persona},
                          "recent_chats": await self.llm.chats(), "forbidden_interactions": effective_forbidden_interactions(self.config.get("forbidden_interactions", [])),
                          "user_aliases": list(user_aliases_from_config(self.config)),
                          "extra_generation_prompt": self.config.get("extra_generation_prompt", ""), "extra": extra,
                          "allow_new_event": allow_new}
                if backfilling_profile:
                    inputs["extra"] = "档案生成后补齐：按完整生活档案和新城市天气补齐未来安排，保留已有约定。" + extra
                if force and has_original:
                    inputs.update(now=self.now().isoformat(), current_actual=old.get("actual", []),
                                  rewrite_rule="重写全天输出供校验，但代码将只采用提交时刻之后的部分，过去记录冻结")
                prompt = schedule_prompt(inputs)
                reason = ""
                for attempt in range(3):
                    try:
                        payload = await self.llm.json(prompt + ("\n上次错误，请整体重写：" + reason if reason else ""), purpose="schedule")
                        aliases = user_aliases_from_config(self.config)
                        slots = repair_slots(payload.get("slots", []), start, end, self.config.get("forbidden_interactions", []), user_aliases=aliases)
                        chat_fields = _day_chat_fields(payload)
                        _validate_day_chat_fields(chat_fields)
                        raw_temporary = payload.get("temporary_outfits", {})
                        if not isinstance(raw_temporary, dict):
                            raise ValueError("临时搭配应按外出、居家、睡衣分别填写")
                        cleaned, removed = sanitize_forbidden_fields({
                            "day_summary": payload.get("day_summary", ""), "mood_overall": payload.get("mood_overall", ""),
                            "outfit_notes": payload.get("outfit_notes", ""), "temporary_outfits": raw_temporary},
                            self.config.get("forbidden_interactions", []), aliases)
                        if removed:
                            log.info("日程概况或穿搭说明已移除 %s 句与用户的受限互动，其余安排照常保留。", removed)
                        payload.update(cleaned)
                        temporary = payload["temporary_outfits"]
                        for kind, outfit in outfits.items():
                            if not outfit.get("description"):
                                description = format_outfit_description(temporary.get(kind, ""))
                                if not description and format_outfit_description(raw_temporary.get(kind, "")):
                                    temporary[kind] = {"outdoor": "适合当季天气的日常外出服", "home": "舒适的居家服", "sleepwear": "舒适的睡衣"}.get(kind, "舒适的日常穿搭")
                                    description = format_outfit_description(temporary[kind])
                                if not description:
                                    raise ValueError("缺少" + kind + "的临时搭配描述")
                                if description_key(description) in recent_keys:
                                    raise ValueError("临时搭配和近期实际穿过的衣服重复，请换一套")
                        break
                    except ValueError as exc:
                        reason = str(exc)
                        if attempt == 2:
                            raise
                for kind, outfit in outfits.items():
                    if not outfit.get("description"):
                        temporary = payload["temporary_outfits"][kind]
                        outfit["description"] = format_outfit_description(temporary)
                        if isinstance(temporary, dict):
                            outfit["parts"] = {key: temporary[key].strip() for key, _ in OUTFIT_PARTS
                                               if isinstance(temporary.get(key), str) and temporary[key].strip()}
                if not self.weather.matches_location(weather):
                    weather = self.weather.cached(self.now())
                allow_ai_weather = not self.weather.settings.get("enabled", True) or not str(self.weather.settings.get("city", "") or "").strip()
                if weather.get("source") == "ai" and allow_ai_weather:
                    ai = payload.get("ai_weather", {})
                    if isinstance(ai, dict):
                        weather = {**weather, **{k: ai[k] for k in ("summary", "temp_min", "temp_max", "precip_probability", "periods") if k in ai},
                                   "source": "ai", "weather_source": "ai"}
                    if not str(weather.get("summary", "")).strip() or "待按季节" in str(weather.get("summary", "")):
                        try:
                            ai = await self.llm.json("按城市、季节给出合理的模拟天气，只输出JSON："
                                '{"summary":"","temp_min":15,"temp_max":22,"precip_probability":10,"periods":{}}'
                                + "\n城市：" + str(weather.get("city", "")) + "，日期：" + key + "，季节：" + str(calendar.get("season", "")), purpose="weather")
                            weather.update({k: ai[k] for k in ("summary", "temp_min", "temp_max", "precip_probability", "periods") if k in ai})
                        except Exception as exc:
                            log.warning("模拟天气补齐失败，使用季节概况：%s", describe_error(exc))
                    if not str(weather.get("summary", "")).strip() or "待按季节" in str(weather.get("summary", "")):
                        weather["summary"] = str(calendar.get("season", "")) + "季模拟天气，实况暂缺"
                    weather.update(source="ai", weather_source="ai")
                    remember = getattr(self.weather, "remember_ai", None)
                    if callable(remember):
                        remember(weather)
                for slot in slots:
                    outfit_key = slot.get("outfit_type", "home")
                    chosen = outfits.get(outfit_key, {})
                    sports_index = next((index for index, item in enumerate(sport_outfits) if item.get("id") and item.get("id") == slot.get("outfit_id")), None)
                    sports = sport_outfits[sports_index] if sports_index is not None else None
                    if sports and any(word in str(slot.get("activity", "")) for word in ("运动", "健身", "跑步", "瑜伽", "锻炼", "篮球", "足球", "网球", "羽毛球")):
                        chosen = sports
                        outfit_key = "sports:" + str(sports_index)
                    slot["outfit"] = chosen.get("description", "")
                    slot["outfit_id"] = chosen.get("id", "")
                    slot["outfit_key"] = outfit_key
                async with self.mutation_lock:
                    await self.llm.selected_persona(self.setup.selected_id)
                    self.setup.assert_owner(token)
                    commit_now = self.now()
                    if commit_now >= end or self.window()[0] != start:
                        raise ValueError("生成期间已跨过业务日分界，等待新一天生成")
                    live = self.store.day(key)
                    live_references = copy.deepcopy(live or {})
                    if not self.weather.matches_location(weather):
                        weather = self.weather.cached(commit_now)
                    if has_original and live and live.get("plan"):
                        baseline = self._rewrite_base(live, start, end, commit_now) or live["actual"]
                        changed = merge_future(baseline, slots, commit_now, end, self.config.get("forbidden_interactions", []), user_aliases=user_aliases_from_config(self.config))
                        if backfilling_profile:
                            changed = self._preserve_promised_slots(changed, live, commit_now, end)
                        live["actual"] = changed
                        description = ("档案生成后补齐" + ("：" + extra if extra else "")) if backfilling_profile else extra or "管理员重写未来日程"
                        self.append_change(live, "admin", description, slots, commit_now)
                        record = live
                    else:
                        record = {"date": key, "plan": copy.deepcopy(slots), "actual": copy.deepcopy(slots), "changes": [], "surprises": [], "recap": "", "fail_count": (old or {}).get("fail_count", 0)}
                        record["generation_started_at"] = window_state["started_at"]
                        baseline = self._rewrite_base(None, start, end, commit_now)
                        if baseline:
                            record["actual"] = merge_future(baseline, slots, commit_now, end, self.config.get("forbidden_interactions", []), user_aliases=user_aliases_from_config(self.config))
                    record.update(status="ok", last_error="", retry_at=None, outfits=outfits, weather=weather,
                                  setup_token=token,
                                  business_start=start.isoformat(), business_end=end.isoformat(),
                                  auto_attempts=auto_attempts, total_attempts=total_attempts,
                                  profile_missing_at_generation=profile_was_empty,
                                  sport_outfits=sport_outfits,
                                  day_summary=str(payload.get("day_summary", "")), mood_overall=str(payload.get("mood_overall", "")),
                                  outfit_notes=str(payload.get("outfit_notes", "")))
                    record.update(chat_fields)
                    record.pop("window_generation", None)
                    record.pop("profile_backfill_generation", None)
                    record.pop("auto_attempts_before", None)
                    record.pop("automatic_attempt", None)
                    record = rebase_collected_outfits(record, live_references)
                    self.events.apply_generation(payload, start.date(), allow_new)
                    if not has_original:
                        record["surprises"] = self.choose_surprises(slots)
                    self.store.save_day(record)
                if not str(self.config.get("default_reference_umo") or "").strip():
                    log.info("尚未设置参考会话。请在目标私聊发送「参考会话 set」，用于识别约定并延续聊天话题。")
                self.restore_surprises()
                self.store.cleanup(start.date(), int(self.config.get("history_keep_days", 30)))
                self.promises.cleanup(self.now())
                return record
            except asyncio.CancelledError:
                if not self.setup.owns(token):
                    raise
                record = self.store.day(key) or {"date": key}
                if state_field:
                    self._cancel_attempt(window_state, "日程生成因插件重载中断，可以立即重试")
                    record[state_field] = window_state
                    self.store.save_day(record)
                elif not has_original:
                    self._cancel_attempt(record, "日程生成因插件重载中断，可以立即重试")
                    self.store.save_day(record)
                raise
            except Exception as exc:
                if not self.setup.owns(token):
                    raise
                record = self.store.day(key) or {"date": key}
                record.update(last_error=describe_error(exc), last_failure_at=self.now().isoformat())
                delay = retry_delay_minutes(exc, auto_attempts, content_minutes=10)
                if state_field:
                    window_state.update(status="failed", last_error=describe_error(exc), retry_at=(self.now() + timedelta(minutes=delay)).isoformat())
                    window_state.pop("auto_attempts_before", None)
                    window_state.pop("automatic_attempt", None)
                    record[state_field] = window_state
                elif not has_original:
                    record.update(status="failed", fail_count=record.get("fail_count", 0) + 1,
                                  auto_attempts=auto_attempts, total_attempts=total_attempts,
                                  retry_at=(self.now() + timedelta(minutes=delay)).isoformat())
                    record.pop("auto_attempts_before", None)
                    record.pop("automatic_attempt", None)
                self.store.save_day(record)
                if state_field or not has_original:
                    self.schedule_retry(window_state if state_field else record)
                log.error("生成 %s 日程失败：%s", key, describe_error(exc))
                if force:
                    raise
                return record

    @staticmethod
    def append_change(day, source, description, slots, now):
        day.setdefault("changes", []).append({"id": "change-" + uuid4().hex, "time": now.isoformat(), "source": source,
            "description": str(description), "slots": copy.deepcopy(slots)})

    def _preserve_promised_slots(self, changed, live, now, end):
        """Profile enrichment may fill ordinary life, never erase chat plans."""
        intervals = []
        for record in live.get("changes", []):
            if record.get("source") == "chat":
                for slot in record.get("slots", []):
                    try:
                        intervals.append((parse_local(slot["start"], self.tz), parse_local(slot["end"], self.tz)))
                    except (KeyError, TypeError, ValueError):
                        continue
        due = []
        for promise in self.promises.all():
            if promise.get("status") == "pending" and promise.get("due"):
                try:
                    due.append(parse_local(promise["due"], self.tz))
                except (TypeError, ValueError):
                    continue
        protected = []
        for slot in live.get("actual", []):
            left, right = parse_local(slot["start"], self.tz), parse_local(slot["end"], self.tz)
            if right > now and (any(left <= point < right for point in due) or any(left < b and right > a for a, b in intervals)):
                protected.append(slot)
        return merge_future(changed, protected, now, end, self.config.get("forbidden_interactions", []),
                            user_aliases=user_aliases_from_config(self.config))

    def choose_surprises(self, slots):
        if random.random() >= float(self.config.get("surprise_probability", 0.5)):
            return []
        now = self.now()
        candidates = []
        for slot in slots:
            if project_slot_rest(slot).get("phone") == "offline":
                continue
            start = max(parse_local(slot["start"], self.tz), now + timedelta(minutes=5))
            end = parse_local(slot["end"], self.tz) - timedelta(minutes=5)
            while start < end:
                candidates.append(start)
                start += timedelta(minutes=1)
        count = 2 if random.random() < 0.3 else 1
        chosen = sorted(random.sample(candidates, min(count, len(candidates))))
        return [{"id": "surprise_" + str(index), "at": point.isoformat(), "triggered": False} for index, point in enumerate(chosen)]

    def restore_surprises(self):
        if not self.life_ready() or not self.scheduler.running:
            return
        day = self.current_day()
        if not day or day.get("status") != "ok":
            return
        changed = False
        for surprise in day.get("surprises", []):
            if surprise.get("triggered") or surprise.get("skipped"):
                continue
            point = parse_local(surprise["at"], self.tz)
            if point <= self.now():
                surprise["skipped"] = True
                changed = True
                continue
            self.scheduler.add_job(self._surprise_callback, "date", run_date=point,
                args=[day["date"], surprise["id"]], id=day["date"] + "_" + surprise["id"], replace_existing=True)
        if changed:
            self.store.save_day(day)

    async def _surprise_callback(self, day_key, surprise_id):
        self.spawn(self.surprise(day_key, surprise_id))

    async def surprise(self, day_key, surprise_id):
        await self.setup.refresh()
        if not self.life_ready():
            return
        async with self.mutation_lock:
            day = self.store.day(day_key)
            if not day or day.get("status") != "ok" or day_key != self.window()[0].date().isoformat():
                return
            record = next((item for item in day.get("surprises", []) if item["id"] == surprise_id), None)
            if not record or record.get("triggered") or record.get("skipped"):
                return
            record.update(triggered=True, triggered_at=self.now().isoformat())
            self.store.save_day(day)
        try:
            await self.apply_change("surprise", {"reason": "日常小意外"})
        except Exception as exc:
            log.warning("小意外未改动日程：%s", describe_error(exc))

    async def refresh_weather(self, *, update_day=True):
        await self.setup.refresh()
        if not self.life_ready():
            return
        token = self._role_token()
        from .weather import significant_change
        previous = self.weather.cached(self.now())
        current = await self.weather.refresh(self.now())
        self.setup.assert_owner(token)
        if not update_day:
            return
        day = self.current_day()
        if day and day.get("status") == "ok":
            async with self.mutation_lock:
                live = self.store.day(day["date"])
                if live:
                    live["weather"] = current
                    self.store.save_day(live)
            if current.get("source") == "open-meteo" and significant_change(previous, current):
                try:
                    await self.apply_change("surprise", {"reason": "天气变化", "weather_change": {"previous": previous, "current": current}})
                except Exception as exc:
                    log.warning("天气变化调整失败：%s", describe_error(exc))

    def enqueue_chat(self, umo, user, reply, missed_ids=None):
        if self.closed or not self.life_ready() or not self.config.get("chat_change_enabled", True) or not reply:
            return
        if not missed_ids and not has_change_trigger(user, reply, self.config.get("change_trigger_keywords", DEFAULT_KEYWORDS)):
            return
        self.judge_queue.append({"umo": umo, "user": user, "reply": reply, "at": self.now().isoformat(), "missed_ids": list(missed_ids or [])})
        self.last_enqueue = asyncio.get_running_loop().time()
        if self.judge_task is None or self.judge_task.done():
            self.judge_task = self.spawn(self._judge_worker())

    async def _judge_worker(self):
        token = self._role_token()
        while self.judge_queue and not self.closed and self.life_ready() and self.setup.owns(token):
            while True:
                remaining = 5 - (asyncio.get_running_loop().time() - self.last_enqueue)
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            umo = self.judge_queue[0]["umo"]
            batch = [item for item in self.judge_queue if item["umo"] == umo]
            self.judge_queue = [item for item in self.judge_queue if item["umo"] != umo]
            recent = await self.llm.chats(umo=umo, count=6)
            if not self.life_ready() or not self.setup.owns(token):
                return
            for item in batch:
                for role, text in (("user", item["user"]), ("assistant", item["reply"])):
                    message = {"role": role, "content": text}
                    if message not in recent[-6:]:
                        recent.append(message)
            try:
                await self.apply_change("chat", {"recent_chats": recent[-6:], "batch": batch})
            except Exception as exc:
                log.warning("后台判断聊天失败：%s", describe_error(exc))

    def _apply_mood(self, day, mood, now, mutation_id=None):
        if not day or day.get("status") != "ok" or not isinstance(mood, str):
            return False
        mood = " ".join(mood.split())[:10]
        if not mood:
            return False
        actual = day["actual"]
        current, _ = find_slots(actual, now)
        if current is None:
            return False
        index = actual.index(current)
        changed = False
        for slot in actual[index:index + 3]:
            if slot.get("mood") != mood:
                slot["mood"] = mood
                changed = True
        if mutation_id is not None:
            day.setdefault("mood_mutation_ids", []).append(mutation_id)
            return True
        return changed

    async def update_mood(self, mood, *, expected_persona_id, expected_day,
                          still_current=None, mutation_id=None):
        token = self.setup.state.get("token")
        async with self.mutation_lock:
            if (self.closed or not self.life_ready() or not self.setup.owns(token)
                    or self.setup.state["persona_id"] != expected_persona_id):
                return False
            now = self.now()
            key = self.window()[0].date().isoformat()
            if key != expected_day or (still_current is not None and not still_current()):
                return False
            live = self.store.day(key)
            if live and live.get("status") == "ok" and mutation_id in live.get("mood_mutation_ids", []):
                return True
            if not self._apply_mood(live, mood, now, mutation_id):
                return False
            self.store.save_day(live)
            return True

    async def apply_change(self, source, context):
        await self.setup.refresh()
        if not self.life_ready():
            return
        token = self._role_token()
        now = self.now()
        start, end = self.window()
        day = self.store.day(start.date().isoformat())
        current = day.get("actual", []) if day and day.get("status") == "ok" else []
        inputs = {"now": now.isoformat(), "business_end": end.isoformat(),
                  "actual_future": [slot for slot in current if parse_local(slot["end"], self.tz) > now],
                  "pending_promises": self.promises.pending(now), "persona": await self.check_persona(),
                  "profile": self.config.get("profile", {}), "profile_is_historical": self.persona_changed,
                  "weather": self.weather.cached(now), "forbidden_interactions": effective_forbidden_interactions(self.config.get("forbidden_interactions", [])), **context}
        inputs["user_aliases"] = list(user_aliases_from_config(self.config))
        inputs["outfits"] = project_day_outfits(day or {}, copy.deepcopy(self.config.get("wardrobe", [])))
        allowed_missed = {item_id for batch in context.get("batch", []) for item_id in batch.get("missed_ids", [])}
        inputs["missed_promises_to_acknowledge"] = [item for item in self.promises.all() if item.get("id") in allowed_missed and item.get("status") == "missed"]
        result = await self.llm.json(change_prompt(inputs, source),
                                     provider_id=self.config.get("judge_provider") if source == "chat" else None, purpose=source)
        async with self.mutation_lock:
            await self.llm.selected_persona(self.setup.selected_id)
            self.setup.assert_owner(token)
            commit_now = self.now()
            if self.window()[0] != start:
                return
            live = self.store.day(start.date().isoformat())
            raw_changes = result.get("schedule_changes", [])
            if not isinstance(raw_changes, list):
                raise ValueError("schedule_changes应为数组")
            aliases = user_aliases_from_config(self.config)
            valid = [slot for slot in raw_changes if isinstance(slot, dict) and not contains_forbidden(
                slot, self.config.get("forbidden_interactions", []), aliases)]
            rows = {row["key"]: row for row in project_day_outfits(live or {}, copy.deepcopy(self.config.get("wardrobe", [])))}
            for slot in valid:
                key = slot_outfit_key(slot, live)
                selected = rows.get(key, {})
                slot["outfit"] = selected.get("description", "")
                slot["outfit_id"] = selected.get("id", "")
                slot["outfit_key"] = key
            affected = []
            if live and live.get("status") == "ok" and valid:
                previous = live["actual"]
                updated = merge_future(previous, valid, commit_now, end, self.config.get("forbidden_interactions", []), user_aliases=aliases)
                if updated != previous:
                    live["actual"] = updated
                    affected = valid
            promises_changed = []
            mood_changed = False
            if source == "chat":
                self.promises.forbidden = self.config.get("forbidden_interactions", [])
                self.promises.user_aliases = aliases
                promises_changed = self.promises.apply(result, commit_now, (live or {}).get("changes", []))
                notified = result.get("notified_missed_promise_ids", [])
                if isinstance(notified, list):
                    self.promises.mark_notified([item_id for item_id in notified if item_id in allowed_missed], commit_now)
                mood_changed = self._apply_mood(live, result.get("mood_update"), commit_now)
            if live and (affected or promises_changed):
                affected_reasons = "；".join(str(slot["reason"]) for slot in affected if slot.get("reason"))
                reason = result.get("reason") or affected_reasons
                if source == "chat":
                    reason = context.get("reason") or reason
                description = str(reason or context.get("reason") or "更新聊天约定")
                description, _ = sanitize_forbidden_text(description, self.config.get("forbidden_interactions", []), aliases)
                description = description or "更新后续生活安排"
                conflicts = result.get("promise_conflicts", [])
                for conflict in conflicts if isinstance(conflicts, list) else []:
                    if isinstance(conflict, dict):
                        description += "；将失约：" + str(conflict.get("id", "")) + "，原因及解释：" + str(conflict.get("note", ""))
                self.append_change(live, source, description, affected, commit_now)
                if source == "surprise" and isinstance(conflicts, list):
                    pending_ids = {item["id"] for item in self.promises.pending(commit_now)}
                    live["changes"][-1]["promise_conflicts"] = [item for item in conflicts if isinstance(item, dict) and item.get("id") in pending_ids]
            if live and (affected or promises_changed or mood_changed):
                self.store.save_day(live)

    def _recent_changes(self, day, start, now, *, surprise_only=False):
        if day.get("status") != "ok" or day.get("date") != start.date().isoformat():
            return []
        cutoff = start if surprise_only else max(start, now - timedelta(minutes=30))
        recent = []
        for index, change in enumerate(day.get("changes", [])):
            if surprise_only and change.get("source") != "surprise":
                continue
            if not change.get("time"):
                continue
            changed = parse_local(change["time"], self.tz)
            if not cutoff <= changed <= now:
                continue
            source = change.get("source")
            description = change.get("description")
            if not source or not description:
                continue
            identifier = change.get("id")
            if not identifier:
                legacy_key = dump([day["date"], index, change["time"], source, description])
                identifier = "change-" + hashlib.sha256(legacy_key.encode()).hexdigest()[:32]
            slots = []
            if source in {"surprise", "chat"}:
                for slot in day.get("actual", []):
                    activity, location = slot.get("activity", ""), slot.get("location", "")
                    for candidate in change.get("slots", []):
                        label = str(candidate.get("activity", "")).strip()
                        place = str(candidate.get("location", "") or "").strip()
                        if (label, place) != (activity, location):
                            continue
                        left = max(changed, parse_local(slot["start"], self.tz), parse_local(candidate["start"], self.tz))
                        right = min(parse_local(slot["end"], self.tz), parse_local(candidate["end"], self.tz))
                        if left < right:
                            slots.append({"start": left.isoformat(), "end": right.isoformat(),
                                          "activity": activity, "location": location})
                            break
            recent.append((changed, {"id": identifier, "time": changed.isoformat(), "source": source,
                                     "description": description, "slots": slots[:3]}))
        recent.sort(key=lambda item: item[0])
        return [record for _, record in (recent if surprise_only else recent[-3:])]

    def _past_slots(self, day, start, now):
        recorded_at = parse_local(day["generation_started_at"], self.tz) if day.get("generation_started_at") else start
        fields = ("start", "end", "activity", "location", "phone", "rest_type",
                  "outfit_key", "outfit_type", "outfit_id", "outfit")
        return [{key: slot[key] for key in fields if key in slot} for slot in day["actual"]
                if slot.get("activity") not in ("", None, UNRECORDED_ACTIVITY)
                and recorded_at < parse_local(slot["end"], self.tz) <= now]

    def get_current_state(self):
        if not self.life_ready():
            return {}
        now = self.now()
        day = self.current_day() or {}
        actual = day.get("actual", [])
        current, next_slot = find_slots(actual, now)
        start, end = self.window()
        day_key = start.date().isoformat()
        day_fields = {}
        if day.get("status") == "ok" and day.get("date") == day_key:
            day_fields = {**_day_chat_fields(day), "day_key": day_key,
                          "surprises": day.get("surprises", []),
                          "surprise_changes": self._recent_changes(day, start, now, surprise_only=True)}
        previous_slot = None
        next_night_sleep_end = None
        if current and day.get("status") == "ok":
            index = actual.index(current)
            for slot in actual[index:]:
                sleeping = project_slot_rest(slot)["rest_type"] == "night_sleep"
                if next_night_sleep_end and (not sleeping or parse_local(slot["start"], self.tz) != next_night_sleep_end):
                    break
                if sleeping:
                    next_night_sleep_end = parse_local(slot["end"], self.tz)
        if current and day.get("status") == "ok" and not self.persona_changed:
            current_start = parse_local(current["start"], self.tz)
            previous = actual[index - 1] if index else None
            if not index and current_start == start and day.get("date") == start.date().isoformat():
                previous_day = self.store.day(start.date() - timedelta(days=1)) or {}
                previous_actual = previous_day.get("actual", []) if previous_day.get("status") == "ok" else []
                if previous_actual:
                    previous = previous_actual[-1]
            if (previous and previous.get("activity") not in (None, "", UNRECORDED_ACTIVITY)
                    and parse_local(previous["end"], self.tz) == current_start):
                previous_slot = project_slot_rest({
                    "start": previous["start"], "end": previous["end"], "activity": previous["activity"],
                    "location": previous.get("location", ""), "phone": previous.get("phone", ""),
                    "rest_type": previous.get("rest_type"),
                })
        rows = project_day_outfits(day, copy.deepcopy(self.config.get("wardrobe", [])), self.wardrobe_uploads.images)
        outfits = {row["key"]: row for row in rows}
        def attach(slot):
            if not slot:
                return None
            value = project_slot_rest(slot)
            key = slot_outfit_key(slot, day)
            selected = outfits.get(key, {})
            value["outfit"] = selected.get("description") or format_outfit_description(slot.get("outfit", ""))
            value["outfit_id"] = selected.get("id", "")
            value["outfit_key"] = key
            value["outfit_label"] = slot_outfit_label(value, day)
            value["outfit_source"] = selected.get("source", "temporary")
            return value
        if "day_key" in day_fields:
            day_fields["past_slots"] = [attach(slot) for slot in self._past_slots(day, start, now)]
        profile = self.config.get("profile", {})
        # The weather service validates the configured city (including fallback_from). Falling
        # back to a day's old location would bypass that isolation.
        weather = self.weather.cached(now)
        return {**day_fields, "persona_id": self.setup.state["persona_id"],
                "now": now.isoformat(), "current_slot": attach(current), "next_slot": attach(next_slot),
                "previous_slot": previous_slot,
                "next_night_sleep_end": next_night_sleep_end.isoformat() if next_night_sleep_end else None,
                "persona_changed": self.persona_changed, "changes": self._recent_changes(day, start, now),
                "outfit": {**{key: copy.deepcopy(outfits[key]) for key in OUTFIT_CATEGORIES},
                           "sports": [copy.deepcopy(row) for row in rows if row["kind"] == "sports"]},
                "weather": copy.deepcopy(weather), "events": self.events.active(now.date()),
                "promises_due": self.promises.due_for_day(start, end), "day_summary": str(day.get("day_summary", "")),
                "profile_summary": "" if self.persona_changed else "；".join(str(profile.get(key, "")) for key in ("identity", "phone_habits") if profile.get(key))}

    async def confirm_tell_delivered(self, expected_persona_id, expected_promise):
        async with self.mutation_lock:
            if self.closed or not self.life_ready() or self.setup.state["persona_id"] != expected_persona_id:
                return False
            return self.promises.confirm_tell_delivered(expected_promise, self.now())

    async def get_life_context(self, allow_generate=True):
        await self.setup.refresh()
        if not self.life_ready():
            return {}
        start, _ = self.window()
        day = self.store.day(start.date().isoformat())
        if allow_generate and (not day or day.get("status") != "ok"):
            task = self.ensure_background()
            if task:
                await asyncio.shield(task)
            day = self.store.day(start.date().isoformat())
        if not self.life_ready():
            return {}
        state = self.get_current_state()
        usable = day if day and day.get("status") == "ok" else self.current_day()
        if not usable or usable.get("status") != "ok":
            return {}
        slot = state["current_slot"] or {}
        kind = slot.get("outfit_type", "home")
        selected = state["outfit"].get(kind, {})
        if str(slot.get("outfit_key", "")).startswith("sports:"):
            selected = next((value for value in state["outfit"]["sports"] if value["key"] == slot["outfit_key"]), selected)
        return {"outfit": slot.get("outfit") or selected.get("description", ""),
                "schedule": format_timeline(usable.get("actual", []), outfit_label=lambda value: slot_outfit_label(value, usable)),
                "meta": {"style": selected.get("category", "")},
                "timeline": copy.deepcopy(usable.get("actual", [])), **{key: state[key] for key in ("current_slot", "next_slot", "weather", "events", "now")}}

    def injection(self, owner=False):
        if not self.life_ready():
            return "", []
        from html import escape
        now = self.now()
        state = self.get_current_state()
        day = self.current_day() or {}
        phone_status = (state.get("current_slot") or {}).get("phone", "未确定")
        rules = life_rules(self.config.get("forbidden_interactions", []), phone_status=phone_status)
        budget = max(200, 1220 - len(rules) - 45)
        calendar = get_calendar_info(now.date())
        lines = ["现在：" + now.strftime("%Y-%m-%d %H:%M") + " " + str(calendar.get("weekday", "")) +
                 ("，工作日" if calendar["is_workday"] else "，休息日") + " " + str(calendar.get("solar_term", ""))]
        weather = state["weather"]
        weather_city = str(weather.get("fallback_from") or weather.get("city", ""))
        weather_text = weather_city + " " + str(weather.get("summary", "未知"))
        current_weather = weather.get("current") or {}
        if current_weather.get("temperature") is not None:
            weather_text = weather_city + " " + str(current_weather.get("summary", weather.get("summary", ""))) + " " + str(current_weather["temperature"]) + "℃"
        elif weather.get("temp_min") is not None:
            weather_text += " " + str(weather["temp_min"]) + "~" + str(weather.get("temp_max", "")) + "℃"
        evening = weather.get("periods", {}).get("evening", {})
        if isinstance(evening, dict) and evening.get("summary"):
            weather_text += "，晚间" + str(evening["summary"])[:10]
        if weather.get("source") == "ai":
            weather_text += "（按季节模拟）"
        details = ["天气：" + weather_text]
        current = state["current_slot"]
        if current:
            phone = {"free": "能及时看手机", "glance": "偶尔看一眼手机", "offline": "本来看不了手机"}
            span = parse_local(current["start"], self.tz).strftime("%H:%M") + "—" + parse_local(current["end"], self.tz).strftime("%H:%M")
            lines.append("正在做：" + span + " " + str(current.get("location", ""))[:20] + " " + str(current.get("activity", ""))[:45] + "；" + phone.get(current.get("phone"), "") + "；心情：" + str(current.get("mood", ""))[:15])
            details.append("穿着：" + current.get("outfit", "")[:90])
            following = [slot for slot in day.get("actual", []) if parse_local(slot["start"], self.tz) > now][:2]
            details.append("接下来：" + "；".join(parse_local(slot["start"], self.tz).strftime("%H:%M") + " " + str(slot.get("activity", ""))[:25] for slot in following))
            details.append("今天概况：" + state["day_summary"][:40])
            mood_overall = day.get("mood_overall")
            if mood_overall:
                details.append("今天整体心情：" + mood_overall[:20])
            if day.get("generation_started_at") and day.get("business_start"):
                generation_started = parse_local(day["generation_started_at"], self.tz)
                business_start = parse_local(day["business_start"], self.tz)
                if generation_started - business_start > timedelta(hours=3):
                    details.append("注：今天日程是在 " + generation_started.strftime("%H:%M") + " 以后才开始记录，之前的安排是推测。")
            if day.get("outfit_notes"):
                details.append("穿搭补充：" + str(day["outfit_notes"])[:35])
        else:
            profile = self.config.get("profile", {})
            if self.persona_changed:
                lines.append("人设正文已更新，历史档案不作为当前身份或作息依据；请按最新人设和真实当前时间推断状态。")
            else:
                details.append("档案：" + "；".join(str(profile.get(key, ""))[:70] for key in ("identity", "routine_workday", "routine_restday", "phone_habits")))
                lines.append("日程正在准备，请按档案、作息和真实当前时间推断合理状态。")
        # Preserve the current state and missed promises before optional details.
        # Return IDs only for missed promises that the model can actually see.
        missed_ids = []
        pending = []
        self.promises.refresh(now, day.get("changes", []))
        promise_label = "和你的约定："
        missed_label = "；已失约，请自然解释："
        for item in self.promises.for_injection(now, owner=owner):
            content = str(item.get("content", ""))[:80]
            text = promise_label + content + "；" + str(item.get("due") or "无具体时间")
            if item.get("status") != "missed":
                pending.append(text)
                continue
            note = str(item.get("note", ""))[:60]
            text += missed_label + note
            remaining = budget - len("\n".join(lines)) - 1
            if len(text) > remaining:
                if missed_ids:
                    continue
                # Compact the first missed promise without losing its known cause.
                room = remaining - len(promise_label) - len(missed_label)
                if room < 2:
                    continue
                content_size = min(len(content), room - min(len(note), room // 2))
                text = promise_label + content[:content_size] + missed_label + note[:room - content_size]
            lines.append(text)
            missed_ids.append(item["id"])
        lines.extend(details)
        for text in pending:
            if len("\n".join(lines + [text])) > budget:
                break
            lines.append(text)
        for item in state["events"][:2]:
            text = "最近在发生：" + str(item.get("title", ""))[:20] + "（至" + str(item.get("end_date", "")) + "）" + str(item.get("description", ""))[:30]
            if len("\n".join(lines + [text])) <= budget:
                lines.append(text)
        changes = day.get("changes", []) if day.get("date") == self.window()[0].date().isoformat() else []
        for item in changes[-3:]:
            text = "今天的变化：" + str(item.get("description", ""))[:65]
            if len("\n".join(lines + [text])) <= budget:
                lines.append(text)
        return "<character_life>\n" + escape("\n".join(lines)[:budget]) + "\n</character_life>\n" + rules, missed_ids
