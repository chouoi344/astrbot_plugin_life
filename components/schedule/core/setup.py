"""Restart-safe preparation following AstrBot's global default persona."""

import asyncio
import copy
import inspect
import uuid

from astrbot.api import logger

from .errors import describe_error, user_error
from .llm import PersonaUnavailableError


class LifeSetup:
    ROLE_CONFIG = (
        "profile",
        "profile_persona_hash",
        "profile_generated_at",
        "profile_persona_id",
        "wardrobe",
        "weather",
    )

    def __init__(self, engine):
        self.engine = engine
        self.state = engine.store.read("setup_state.json", {})
        self.lock = asyncio.Lock()
        self.task = None
        self.observers = set()
        self.requests = set()
        self.selected = None
        self.available = False
        if self.state.get("status") == "preparing":
            self.state.update(
                status="failed", error="上次准备因插件重启中断，请在 AstrBot 的「拟人生活」设置中保存并关闭后继续。"
            )
            self.save()

    @property
    def selected_id(self):
        return self.engine.llm.global_persona_id()

    def save(self):
        self.engine.store.write("setup_state.json", self.state)

    def _save_configuration(self, changes):
        config = self.engine.config
        previous = {key: copy.deepcopy(config.get(key)) for key in changes}
        absent = set(changes) - set(config)
        try:
            config.update(changes)
            self.engine.save_config()
        except BaseException:
            for key, value in previous.items():
                if key in absent:
                    config.pop(key, None)
                else:
                    config[key] = value
            try:
                self.engine.save_config()
            except Exception as restore_error:
                raise OSError(
                    "配置保存和恢复都失败，准备已停止；旧生活备份仍保留，请检查配置文件写入权限。"
                ) from restore_error
            raise

    def owns(self, token):
        return bool(token and token == self.state.get("token") and self.selected_id == self.state.get("persona_id"))

    def assert_owner(self, token):
        if not self.owns(token):
            raise ValueError("准备期间 AstrBot 全局默认人设已改变，本次旧角色结果已停止保存。")

    def ready(self):
        if not (
            self.available
            and self.selected_id
            and self.state.get("status") == "ready"
            and self.state.get("persona_id") == self.selected_id
            and self.engine.config.get("profile_persona_id") == self.selected_id
            and not self.engine.profile_empty()
        ):
            return False
        # v3 keeps a synchronous current-card lookup, so deleted cards stop
        # synchronous external reads and prompt injection immediately as well.
        manager = getattr(self.engine.context, "persona_manager", None)
        lookup = getattr(manager, "get_persona_v3_by_id", None)
        if callable(lookup) and not inspect.iscoroutinefunction(lookup):
            persona = lookup(self.selected_id)
            if inspect.isawaitable(persona):
                persona.close()
            elif not persona:
                self.available = False
                return False
            else:
                prompt = self.engine.llm._persona_value(persona, "system_prompt") or self.engine.llm._persona_value(
                    persona, "prompt"
                )
                if not isinstance(prompt, str) or not prompt.strip():
                    self.available = False
                    return False
                old_hash = self.engine.config.get("profile_persona_hash", "")
                self.engine.persona_changed = bool(old_hash and old_hash != self.engine.llm.persona_hash(prompt))
        return True

    def has_data(self):
        engine = self.engine
        return bool(
            not engine.profile_empty()
            or engine.config.get("wardrobe")
            or any((engine.store.root / "days").glob("*.json"))
            or engine.store.read("promises.json", [])
            or engine.store.read("events.json", [])
        )

    def needs_reset(self):
        bound = self.engine.config.get("profile_persona_id")
        return self.has_data() and (bound != self.selected_id or self.state.get("force_reset", False))

    async def refresh(self, *, automatic=False, migrate=False):
        selected_id = self.selected_id
        self.available = False
        self.selected = None
        if not selected_id:
            return self.status()
        try:
            persona = await self.engine.llm.selected_persona(selected_id)
        except PersonaUnavailableError as exc:
            return self.status(unavailable=str(exc))
        except Exception as exc:
            return self.status(unavailable=user_error(exc))
        if selected_id != self.selected_id:
            return self.status()
        self.selected, self.available = persona, True
        engine = self.engine
        # v1 has no binding. A matching saved prompt hash is the only evidence
        # that its existing profile belongs to the explicitly selected card.
        if (
            (automatic or migrate)
            and not self.state
            and not engine.profile_empty()
            and engine.config.get("profile_persona_hash") == engine.llm.persona_hash(persona["prompt"])
        ):
            async with engine.config_lock:
                if self.state or self.selected_id != selected_id:
                    return self.status()
                self._save_configuration({"profile_persona_id": selected_id})
            usable = any(
                engine.store.read(str(path.relative_to(engine.store.root)), {}).get("status") == "ok"
                and engine.store.read(str(path.relative_to(engine.store.root)), {}).get("actual")
                for path in (engine.store.root / "days").glob("*.json")
            )
            self.state = {
                "status": "ready" if usable else "waiting_settings",
                "stage": "ready" if usable else "schedule",
                "persona_id": selected_id,
                "persona_name": persona["name"],
                "token": uuid.uuid4().hex,
            }
            self.save()
        if self.state.get("persona_id") == selected_id:
            self.state["persona_name"] = persona["name"]
        old_hash = engine.config.get("profile_persona_hash", "")
        engine.persona_changed = bool(
            engine.config.get("profile_persona_id") == selected_id
            and old_hash
            and old_hash != engine.llm.persona_hash(persona["prompt"])
        )
        if (
            automatic
            and self.state.get("settings_saved")
            and (not self.state.get("persona_id") or self.state.get("persona_id") != selected_id)
        ):
            return await self._begin(selected_id)
        return self.status()

    def status(self, unavailable=""):
        selected_id = self.selected_id
        selected_name = self.selected["name"] if self.selected and self.selected["id"] == selected_id else ""
        phase = self.state.get("status")
        stage = self.state.get("stage", "") if self.state.get("persona_id") == selected_id else ""
        error = ""
        if not selected_id or unavailable or not self.available:
            phase, message = (
                "unavailable",
                unavailable
                or (
                    "AstrBot 全局默认人设尚未设置，请在 AstrBot 人格设定里选择默认人设。"
                    if not selected_id
                    else "暂时无法读取 AstrBot 全局默认人设，请检查人格设定。"
                ),
            )
        elif self.ready():
            phase, message = "ready", "生活档案、日程和穿搭已准备好，聊天会使用当前全局角色的生活。"
            if self.engine.persona_changed:
                message = "人设正文已更新，现有生活资料保留；下次生成或手动重写会读取最新设定。"
        elif phase == "preparing" and self.state.get("persona_id") == selected_id:
            message = {
                "backup": "正在备份旧角色的生活资料。",
                "profile": "正在生成生活档案，完成后继续准备日程和穿搭。",
                "schedule": "生活档案已完成，正在准备日程和穿搭。",
            }.get(stage, "正在准备角色生活。")
        elif phase == "failed" and self.state.get("persona_id") == selected_id:
            error = self.state.get("error", "准备未完成。")
            message = (
                (
                    "设置"
                    if stage == "settings"
                    else "备份"
                    if stage == "backup"
                    else "生活档案"
                    if stage == "profile"
                    else "日程和穿搭"
                )
                + "准备失败："
                + error
            )
            message += " 请检查设置，在 AstrBot 的「拟人生活」设置中保存并关闭后继续。"
        else:
            phase, message = (
                "waiting_settings",
                "请在 AstrBot 插件管理中打开「拟人生活」设置，填写日程分区后点击“保存并关闭”，后台会自动准备生活档案、日程和穿搭。",
            )
        active_id = self.state.get("persona_id", "") if phase == "ready" else ""
        return {
            "status": phase,
            "selected_persona_id": selected_id,
            "selected_persona_name": selected_name,
            "active_persona_id": active_id,
            "active_persona_name": selected_name if active_id else "",
            "message": message,
            "stage": stage,
            "error": error,
            "persona_changed": self.engine.persona_changed,
        }

    async def on_native_config_saved(self):
        """Only AstrBot's successful native settings-save integration calls this."""
        await self.refresh(automatic=False, migrate=True)
        self.state["settings_saved"] = True
        self.save()
        try:
            self.engine.consume_native_settings_save()
        except Exception as exc:
            self.state.update(status="failed", stage="settings", persona_id=self.selected_id, error=user_error(exc))
            self.save()
            raise
        if not self.available:
            return self.status()
        return await self._begin(self.selected_id)

    async def _begin(self, persona_id):
        async with self.lock:
            if self.engine.closed:
                raise ValueError("插件正在关闭，请重新打开插件后再保存设置。")
            if persona_id != self.selected_id:
                return self.status()
            if not self.available:
                return self.status()
            if self.task and not self.task.done():
                if self.state.get("persona_id") == persona_id:
                    return self.status()
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if self.ready():
                return self.status()
            requires_reset = self.needs_reset()
            continuation = {
                key: self.state[key]
                for key in ("profile_required", "schedule_required")
                if self.state.get("persona_id") == persona_id and key in self.state
            }
            token = (
                self.state.get("token") if self.state.get("persona_id") == persona_id and not requires_reset else None
            )
            self.state = {
                "status": "preparing",
                "stage": "backup" if requires_reset else "profile",
                "persona_id": persona_id,
                "persona_name": self.selected["name"],
                "token": token or uuid.uuid4().hex,
                "settings_saved": True,
                "force_reset": requires_reset,
                "error": "",
                **continuation,
            }
            self.save()
            self.task = self.engine.spawn(self._prepare(requires_reset))
            return self.status()

    def begin_rebuild(self):
        if self.task and not self.task.done():
            return self.task
        self.state = {
            "status": "preparing",
            "stage": "profile",
            "persona_id": self.selected_id,
            "persona_name": self.selected["name"],
            "token": uuid.uuid4().hex,
            "settings_saved": True,
            "force_reset": False,
            "profile_required": True,
            "schedule_required": True,
            "error": "",
        }
        self.save()
        self.task = self.engine.spawn(self._prepare(False))
        self.engine.profile_task = self.task
        return self.task

    async def _reset(self, token):
        engine = self.engine
        # Stop all previous-role workers before taking the recoverable snapshot.
        workers = [
            task
            for task in engine.tasks | self.requests
            if task is not asyncio.current_task() and task not in self.observers and not task.done()
        ]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.assert_owner(token)
        async with engine.config_lock:
            async with engine.mutation_lock:
                values = {key: copy.deepcopy(engine.config.get(key)) for key in self.ROLE_CONFIG}
                backup = engine.store.backup_role(values, engine.now())
                self.assert_owner(token)
                moved = engine.store.retire_role_files(backup)
                try:
                    self._save_configuration(
                        {
                            "profile": {},
                            "profile_persona_hash": "",
                            "profile_generated_at": "",
                            "profile_persona_id": "",
                            "wardrobe": [],
                        }
                    )
                except BaseException:
                    for source, target in reversed(moved):
                        target.replace(source)
                    engine.store._cache.clear()
                    raise
                for directory in ("days", "wardrobe_images"):
                    (engine.store.root / directory).mkdir(exist_ok=True)
                engine.judge_queue.clear()
                engine.wardrobe_uploads.statuses = {}
                engine.wardrobe_uploads.store._cache.clear()
                engine.weather.invalidate_location()
                self.state.update(force_reset=False, backup=str(backup.relative_to(engine.store.root)))
                self.save()

    async def _prepare(self, reset):
        engine = self.engine
        token = self.state["token"]
        try:
            if reset:
                await self._reset(token)
            self.assert_owner(token)
            self.state.update(stage="profile")
            self.save()
            async with engine.config_lock:
                self.assert_owner(token)
                self._save_configuration({"profile_persona_id": self.selected_id})
            completed_profile = engine.store.read("profile_generation.json", {})
            profile_done = completed_profile.get("status") == "ready" and completed_profile.get("setup_token") == token
            if engine.profile_empty() or (self.state.get("profile_required") and not profile_done):
                if not await engine._generate_profile(force=True):
                    raise ValueError(engine.profile_generation_state()["message"])
                self.state["profile_required"] = False
                self.save()
            self.assert_owner(token)
            self.state.update(stage="schedule")
            self.save()
            start, _ = engine.window()
            day = engine.store.day(start.date().isoformat())
            schedule_done = bool(
                day and day.get("status") == "ok" and day.get("setup_token") == token and engine._day_covers_window(day)
            )
            if (
                (self.state.get("schedule_required") and not schedule_done)
                or not day
                or day.get("status") != "ok"
                or not engine._day_covers_window(day)
            ):
                engine._generation_automatic = False
                day = await engine._generate(force=True)
            self.assert_owner(token)
            await engine.llm.selected_persona(self.selected_id)
            self.assert_owner(token)
            if not day or day.get("status") != "ok":
                raise ValueError("今天的日程还没有准备完成，请在 AstrBot 的「拟人生活」设置中保存并关闭后继续。")
            self.state.update(status="ready", stage="ready", error="", schedule_required=False)
            self.save()
            engine.restore_surprises()
            engine.spawn(engine.wardrobe_uploads.process())
            return True
        except asyncio.CancelledError:
            if token == self.state.get("token"):
                self.state.update(
                    status="failed", error="准备已中断，请在 AstrBot 的「拟人生活」设置中保存并关闭后继续。"
                )
                self.save()
            raise
        except Exception as exc:
            if token == self.state.get("token"):
                self.state.update(status="failed", error=user_error(exc))
                self.save()
            logger.warning("[生活日程] 角色生活准备失败：%s", describe_error(exc))
            return False
