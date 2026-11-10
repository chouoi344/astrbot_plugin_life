"""Authenticated, bounded APIs for the four-section AstrBot plugin view."""

import asyncio
import base64
import binascii
import copy
import hashlib
import io
import json
import math
import re
import tempfile
import uuid
from datetime import date
from pathlib import Path

from PIL import Image

from astrbot.api import logger as log

from .calendar_info import get_calendar_info
from .domain import DEFAULT_KEYWORDS, PROFILE_FIELDS
from .errors import describe_error, run_with_timeout, user_error
from .llm import NoProviderError
from .outfits import bind_outfit_reference, description_key, project_day_outfits, slot_outfit_label
from .timeline import parse_local, project_slot_rest
from .uploads import WardrobeUploadProcessor
from .view import build_wardrobe_payload
from .wardrobe import CATEGORIES, SEASONS, season_for_day

from ....runtime import ROOT_PLUGIN_ID


class ViewError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class LifeViewAPI:
    MAX_ACTIVE_IMAGE_TASKS = 8

    def __init__(self, plugin):
        self.plugin = plugin
        self.write_lock = self.engine.config_lock
        stored_tasks = self.engine.store.read("view_tasks.json", {})
        self.tasks = (
            {key: value for key, value in stored_tasks.items() if isinstance(value, dict)}
            if isinstance(stored_tasks, dict)
            else {}
        )
        recovered = False
        for item in self.tasks.values():
            item.setdefault("type", "profile")
            if item.get("status") in {"queued", "running"}:
                self._remove_task_image(item)
                item.update(status="failed", message="插件已重新加载，请重试", updated_at=self.engine.now().isoformat())
                recovered = True
        if recovered:
            self._save_tasks()

    @property
    def engine(self):
        return self.plugin.engine

    @property
    def config(self):
        return self.plugin.config

    @property
    def routes(self):
        return [
            ("wardrobe/list", "GET", self.wardrobe_list),
            ("wardrobe/add", "POST", self.wardrobe_add),
            ("wardrobe/edit", "POST", self.wardrobe_edit),
            ("wardrobe/delete", "POST", self.wardrobe_delete),
            ("wardrobe/transcribe", "POST", self.wardrobe_transcribe),
            ("wardrobe/collect", "POST", self.wardrobe_collect),
            ("schedule/today", "GET", self.schedule_today),
            ("schedule/retry", "POST", self.schedule_retry),
            ("profile/get", "GET", self.profile_get),
            ("profile/save", "POST", self.profile_save),
            ("profile/regenerate", "POST", self.profile_regenerate),
            ("settings/get", "GET", self.settings_get),
            ("settings/save", "POST", self.settings_save),
            ("setup/status", "GET", self.setup_status),
            ("tasks/get", "GET", self.task_get),
        ]

    def register(self):
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        for suffix, method, handler in self.routes:
            register(f"/{ROOT_PLUGIN_ID}/{suffix}", handler, [method], "生活日程视图：" + suffix)
        return True

    def unregister(self):
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [item for item in routes if getattr(item[1], "__self__", None) is not self]

    def _require_admin(self, request):
        """The host validates JWT signatures; inspect identity/type additionally.

        Chat owner IDs are unrelated to Dashboard administrator identities.
        A page-asset token or plugin-scoped API key must never grant writes.
        """
        username = getattr(request, "username", None)
        dashboard = self.plugin.context.get_config().get("dashboard", {})
        configured = dashboard.get("username") if isinstance(dashboard, dict) else None
        if (
            not isinstance(username, str)
            or not username
            or username.startswith("api_key:")
            or not configured
            or username != configured
        ):
            raise ViewError("只有 AstrBot 面板管理员可以修改这些内容，请重新登录。", 403)
        authorization = getattr(request, "headers", {}).get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token:
            token = getattr(request, "cookies", {}).get("astrbot_dashboard_jwt", "")
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, IndexError, TypeError, binascii.Error):
            raise ViewError("请使用 AstrBot 面板管理员登录后再保存。", 403)
        if (
            not isinstance(claims, dict)
            or claims.get("username") != username
            or claims.get("token_type") not in (None, "dashboard", "access")
        ):
            raise ViewError("当前凭据仅用于页面资源，无法执行修改，请重新登录面板。", 403)

    async def _respond(self, action, *, write=False):
        from astrbot.api.web import json_response, request
        role_request = action.__name__ not in {"_settings_get", "_settings_save", "_setup_status",
                                               "_profile_regenerate", "_task_get"}
        current = asyncio.current_task()
        try:
            if write:
                self._require_admin(request)
                if not self.config.get("enabled", True) and action.__name__ in {
                    "_wardrobe_transcribe", "_schedule_retry", "_profile_regenerate",
                }:
                    raise ViewError("日程功能已关闭，请在「拟人生活」设置中开启日程后再试。", 409)
            await self.engine.setup.refresh()
            if role_request:
                if not self.engine.life_ready():
                    raise ViewError(self.engine.setup.status()["message"], 409)
                token = self.engine.setup.state["token"]
                self.engine.setup.requests.add(current)
            result = await action(request)
            if role_request and action.__name__ != "_profile_regenerate":
                self.engine.setup.assert_owner(token)
            return json_response(result)
        except ViewError as exc:
            return json_response({"status": "error", "message": str(exc)}, status_code=exc.status_code)
        except NoProviderError as exc:
            return json_response({"status": "error", "message": str(exc)}, status_code=503)
        except asyncio.TimeoutError as exc:
            log.warning("生活视图处理超时：%s", describe_error(exc))
            return json_response({"status": "error", "message": user_error(exc)}, status_code=504)
        except (ValueError, KeyError) as exc:
            log.warning("生活视图内容处理失败：%s", describe_error(exc))
            return json_response({"status": "error", "message": user_error(exc)}, status_code=400)
        except Exception as exc:
            log.exception("生活视图 API 失败：%s", describe_error(exc))
            return json_response({"status": "error", "message": user_error(exc)}, status_code=500)
        finally:
            self.engine.setup.requests.discard(current)

    async def setup_status(self):
        return await self._respond(self._setup_status)

    async def _setup_status(self, request):
        return await self.engine.setup.refresh()


    @staticmethod
    async def _body(request):
        value = await run_with_timeout(request.json(), 10, "读取视图请求")
        if not isinstance(value, dict):
            raise ViewError("没有读到有效的填写内容，请刷新页面后重试")
        return value

    @staticmethod
    def _text(value, label, limit=10000, required=False):
        if not isinstance(value, str) or len(value) > limit:
            raise ViewError(label + "格式不正确或内容过长")
        value = value.strip()
        if required and not value:
            raise ViewError("请填写" + label)
        return value

    @staticmethod
    def _outfit(body):
        description = LifeViewAPI._text(body.get("description", ""), "穿搭描述", required=True)
        category = body.get("category", "外出")
        seasons = body.get("seasons", list(SEASONS))
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ViewError("请选择有效的穿搭分类")
        if not isinstance(seasons, list) or not seasons or any(item not in SEASONS for item in seasons):
            raise ViewError("至少选择一个有效季节")
        return {"description": description, "category": category, "seasons": [s for s in SEASONS if s in seasons]}

    def _save_config(self, changes):
        before = {key: copy.deepcopy(self.config.get(key)) for key in changes}
        missing = {key for key in changes if key not in self.config}
        try:
            self.config.update(changes)
            self.engine.save_config()
        except Exception:
            for key, value in before.items():
                if key in missing:
                    self.config.pop(key, None)
                else:
                    self.config[key] = value
            raise

    def _image_file(self, value):
        if not isinstance(value, str) or not value:
            raise ViewError("请上传 JPG、PNG 或 WebP 图片")
        if len(value) > WardrobeUploadProcessor.MAX_BYTES * 4 // 3 + 1024:
            raise ViewError("单张图片请控制在 20 MB 以内")
        encoded = value
        if value.startswith("data:"):
            match = re.fullmatch(r"data:image/(?:png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\r\n]+)", value)
            if not match:
                raise ViewError("图片格式无效")
            encoded = match.group(1)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ViewError("没有读到完整的图片，请重新选择图片后重试")
        if not raw or len(raw) > WardrobeUploadProcessor.MAX_BYTES:
            raise ViewError("单张图片请控制在 20 MB 以内")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
                if not extension or image.width * image.height > WardrobeUploadProcessor.MAX_PIXELS:
                    raise ViewError("图片格式或分辨率不符合要求")
                image.verify()
        except ViewError:
            raise
        except Exception:
            raise ViewError("文件内容不是有效图片")
        directory = self.engine.store.root / "view_uploads"
        directory.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, suffix=extension, delete=False) as stream:
            stream.write(raw)
            return Path(stream.name)

    async def _prepare_image(self, value):
        # Cancelling to_thread does not stop its filesystem work. Wait for the
        # worker before removing its result, so a late write cannot leak a file.
        worker = asyncio.create_task(asyncio.to_thread(self._image_file, value))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                source = await worker
                source.unlink(missing_ok=True)
            except Exception as exc:
                log.warning("清理中断的视图上传失败：%s", describe_error(exc))
            raise

    async def wardrobe_list(self):
        return await self._respond(self._wardrobe_list)

    async def _wardrobe_list(self, request):
        async with self.write_lock:
            entries = copy.deepcopy(self.engine.wardrobe.display_entries())
        payload = await asyncio.to_thread(build_wardrobe_payload, None, self.plugin.wardrobe_images, entries=entries)
        for entry in payload["entries"]:
            if entry["id"] in self.engine.wardrobe_uploads.statuses:
                entry["image_status"] = dict(self.engine.wardrobe_uploads.statuses[entry["id"]])
        return payload

    async def wardrobe_add(self):
        return await self._respond(self._wardrobe_add, write=True)

    async def wardrobe_edit(self):
        return await self._respond(self._wardrobe_edit, write=True)

    async def _wardrobe_add(self, request):
        return await self._write_outfit(await self._body(request))

    async def _wardrobe_edit(self, request):
        body = await self._body(request)
        entry_id = self._text(body.get("id", ""), "穿搭 ID", 128, required=True)
        return await self._write_outfit(body, entry_id)

    async def _write_outfit(self, body, entry_id=None):
        normalized = self._outfit(body)
        source = await self._prepare_image(body["image_b64"]) if body.get("image_b64") else None
        try:
            async with self.write_lock:
                entries = self.engine.wardrobe.all()
                if entry_id:
                    entry = next((item for item in entries if item["id"] == entry_id), None)
                    if not entry:
                        raise ViewError("这套穿搭已删除，请刷新列表", 404)
                else:
                    if any(item["description"].casefold() == normalized["description"].casefold() for item in entries):
                        raise ViewError("已有相同描述的穿搭，请编辑原方案", 409)
                    entry = {
                        "__template_key": "outfit",
                        "id": "wardrobe-" + uuid.uuid4().hex,
                        "image_upload": [],
                        "image_processed_hash": "",
                    }
                    entries.append(entry)
                entry.update(normalized)
                if source:
                    entry.update(image_upload=[], image_processed_hash="")
                previous_image = await asyncio.to_thread(self.plugin.wardrobe_images.snapshot, entry["id"])
                try:
                    if source:
                        await run_with_timeout(
                            self.plugin.wardrobe_images.save(entry["id"], str(source)), 30, "保存衣柜展示图"
                        )
                    self._save_config({"wardrobe": entries})
                except BaseException:
                    if source:
                        await asyncio.to_thread(self.plugin.wardrobe_images.restore, entry["id"], previous_image)
                    raise
                return {
                    "entry": copy.deepcopy({key: entry[key] for key in ("id", "description", "category", "seasons")})
                }
        finally:
            if source:
                source.unlink(missing_ok=True)

    async def wardrobe_delete(self):
        return await self._respond(self._wardrobe_delete, write=True)

    async def _wardrobe_delete(self, request):
        body = await self._body(request)
        entry_id = self._text(body.get("id", ""), "穿搭 ID", 128, required=True)
        async with self.write_lock:
            entries = self.engine.wardrobe.all()
            if not any(entry["id"] == entry_id for entry in entries):
                raise ViewError("这套穿搭已删除，请刷新列表", 404)
            remaining = [entry for entry in entries if entry["id"] != entry_id]
            image = await asyncio.to_thread(self.plugin.wardrobe_images.snapshot, entry_id)
            previous = copy.deepcopy(self.config.get("wardrobe", []))
            try:
                self._save_config({"wardrobe": remaining})
                self.plugin.wardrobe_images.remove(entry_id)
            except Exception:
                self.config["wardrobe"] = previous
                self.engine.save_config()
                await asyncio.to_thread(self.plugin.wardrobe_images.restore, entry_id, image)
                raise
            self.engine.wardrobe_uploads.statuses.pop(entry_id, None)
            return {"deleted": entry_id}

    async def wardrobe_transcribe(self):
        return await self._respond(self._wardrobe_transcribe, write=True)

    async def _wardrobe_transcribe(self, request):
        body = await self._body(request)
        source = await self._prepare_image(body.get("image_b64"))
        try:
            async with self.write_lock:
                active = sum(
                    task.get("type") == "wardrobe_transcribe" and task.get("status") in {"queued", "running"}
                    for task in self.tasks.values()
                )
                if active >= self.MAX_ACTIVE_IMAGE_TASKS:
                    raise ViewError("等待识别的图片有些多，请等当前图片完成后再上传。", 429)
                task_id = uuid.uuid4().hex
                self.tasks[task_id] = {
                    "task_id": task_id,
                    "type": "wardrobe_transcribe",
                    "status": "queued",
                    "message": "图片已收到，正在等待识别穿搭…",
                    "created_at": self.engine.now().isoformat(),
                    "temp_image": str(source),
                    "role_token": self.engine.setup.state["token"],
                }
                try:
                    self._save_tasks()
                    worker = self.engine.spawn(self._run_outfit_task(task_id, source))
                    if worker is None:
                        self.tasks[task_id].update(status="failed", message="插件正在关闭，请重新打开后再上传。")
                        self.tasks[task_id].pop("temp_image", None)
                        self._save_tasks()
                        raise ViewError("插件正在关闭，请重新打开后再上传。", 409)
                    worker.add_done_callback(
                        lambda finished, identifier=task_id, image=source: self._outfit_task_done(
                            identifier, image, finished
                        )
                    )
                except BaseException:
                    if self.tasks.get(task_id, {}).get("status") == "queued":
                        self.tasks.pop(task_id, None)
                    raise
                source = None  # The managed worker owns cleanup after acceptance.
                return {
                    "task_id": task_id,
                    "type": "wardrobe_transcribe",
                    "status": "queued",
                    "message": "图片已收到，正在等待识别穿搭…",
                }
        finally:
            if source:
                source.unlink(missing_ok=True)

    @staticmethod
    def _outfit_fingerprint(day_key, row):
        value = json.dumps([day_key, row["key"], description_key(row.get("description", ""))], ensure_ascii=False)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _outfit_rows(self, day, entries):
        rows = project_day_outfits(day, entries, self.plugin.wardrobe_images)
        for row in rows:
            # Local paths are an internal get_current_state contract only.
            row.pop("image_path", None)
            row["fingerprint"] = self._outfit_fingerprint(day.get("date", ""), row)
            row["image"] = ""
        return rows

    def _outfit_images(self, rows):
        # Called off the event loop; only read thumbnails, never normalize or
        # write shared configuration from this worker.
        for row in rows:
            if row.get("source") == "wardrobe" and row.get("id"):
                try:
                    row["image"] = self.plugin.wardrobe_images.data_uri(row["id"])
                except (OSError, ValueError):
                    row["image"] = ""
        return rows

    async def wardrobe_collect(self):
        return await self._respond(self._wardrobe_collect, write=True)

    async def _wardrobe_collect(self, request):
        body = await self._body(request)
        if set(body) != {"date", "outfit_key", "fingerprint"}:
            raise ViewError("没有读到完整的穿搭信息，请刷新今日日程后再试")
        day_key = self._text(body["date"], "日程日期", 10, required=True)
        outfit_key = self._text(body["outfit_key"], "穿搭位置", 32, required=True)
        fingerprint = self._text(body["fingerprint"], "穿搭信息", 64, required=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_key) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ViewError("穿搭信息已过期，请刷新今日日程后再试")
        try:
            calendar_day = date.fromisoformat(day_key)
        except ValueError:
            raise ViewError("日程日期不正确，请刷新今日日程后再试")
        async with self.write_lock:
            async with self.engine.mutation_lock:
                if day_key != self.engine.window()[0].date().isoformat():
                    raise ViewError("今天的日程已经更新，请刷新后再收进衣柜", 409)
                day = self.engine.store.day(day_key)
                if not day:
                    raise ViewError("今天的穿搭还没准备好，请稍后刷新", 409)
                entries = self.engine.wardrobe.all()
                rows = self._outfit_rows(day, entries)
                row = next((item for item in rows if item["key"] == outfit_key), None)
                if row is None or not row.get("description"):
                    raise ViewError("这套穿搭还没准备好，请刷新今日日程", 409)
                if row["fingerprint"] != fingerprint:
                    raise ViewError("这套穿搭已经更新，请刷新后再收进衣柜", 409)
                entry = next((item for item in entries if item["id"] == row.get("id")), None)
                already_exists = bool(entry and row.get("source") == "wardrobe")
                if not already_exists:
                    if not row.get("can_collect") or row.get("source") != "temporary":
                        raise ViewError("这套衣服已从衣柜删除，如需保留，请在衣柜里重新添加", 409)
                    normalized = self._outfit({"description": row["description"], "category": row["category"],
                                              "seasons": [season_for_day(calendar_day)]})
                    entry = next((item for item in entries if description_key(item["description"]) == description_key(normalized["description"])), None)
                    already_exists = entry is not None
                    if entry is None:
                        entry = {"__template_key": "outfit", "id": "wardrobe-" + uuid.uuid4().hex,
                                 "image_upload": [], "image_processed_hash": "", **normalized}
                updated = bind_outfit_reference(day, outfit_key, entry)
                adding = not any(item["id"] == entry["id"] for item in entries)
                config_saved = day_attempted = False
                try:
                    if adding:
                        self._save_config({"wardrobe": entries + [entry]})
                        config_saved = True
                    day_attempted = True
                    self.engine.store.save_day(updated)
                except BaseException:
                    if config_saved:
                        try:
                            self._save_config({"wardrobe": entries})
                        except Exception as rollback_error:
                            log.error("收进衣柜失败后的配置还原未完成：%s", describe_error(rollback_error))
                    if day_attempted:
                        try:
                            self.engine.store.save_day(day)
                        except Exception as rollback_error:
                            log.error("收进衣柜失败后的日程还原未完成：%s", describe_error(rollback_error))
                    raise
                final_entries = entries + [entry] if adding else entries
                collected = next(item for item in self._outfit_rows(updated, final_entries) if item["key"] == outfit_key)
                number = collected["wardrobe_number"]
        collected = (await asyncio.to_thread(self._outfit_images, [collected]))[0]
        return {"saved": True, "already_exists": already_exists,
                "message": ("已在衣柜里·穿搭" if already_exists else "已收进衣柜·穿搭") + str(number),
                "outfit": collected}

    async def _run_outfit_task(self, task_id, source):
        task = self.tasks[task_id]
        try:
            task.update(status="running", message="正在等待模型并识别穿搭…")
            self._save_tasks()
            # LLMClient starts its 195-second outfit budget after acquiring
            # the shared model slot; waiting in that queue has no API timeout.
            normalized = await self.engine.llm.describe_outfit([str(source)])
            self.engine.setup.assert_owner(task["role_token"])
            task.update(status="done", result=self._outfit(normalized), message="穿搭识别好了。")
        except asyncio.CancelledError:
            task.update(status="failed", message="识别因插件重新加载中断，请重新上传图片。")
            raise
        except Exception as exc:
            log.warning("视图后台识别穿搭失败：%s", describe_error(exc))
            task.update(status="failed", message=user_error(exc))
        finally:
            source.unlink(missing_ok=True)
            task.pop("temp_image", None)
            task["updated_at"] = self.engine.now().isoformat()
            self._save_tasks()

    def _outfit_task_done(self, task_id, source, worker):
        # A task cancelled before its first bytecode never enters its finally.
        # This callback also covers that immediate-unload window.
        task = self.tasks.get(task_id)
        if task is None:
            source.unlink(missing_ok=True)
            return
        if task.get("status") in {"queued", "running"}:
            task.update(
                status="failed", message="识别已中断，请重新上传图片。", updated_at=self.engine.now().isoformat()
            )
        source.unlink(missing_ok=True)
        if task.pop("temp_image", None):
            self._save_tasks()

    def _remove_task_image(self, task):
        reference = task.pop("temp_image", None)
        if not reference:
            return
        try:
            source = Path(reference).resolve()
            directory = (self.engine.store.root / "view_uploads").resolve()
            if source.parent == directory and source.suffix.lower() in {".jpg", ".png", ".webp"}:
                source.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("清理中断的图片识别文件失败：%s", describe_error(exc))

    @staticmethod
    def _active_worker(worker):
        return worker is not None and not worker.done()

    def _profile_working(self):
        return (
            self.engine.profile_lock.locked()
            or self._active_worker(self.engine.profile_task)
            or any(
                task.get("type", "profile") == "profile" and task.get("status") in {"queued", "running"}
                and task.get("role_token") == self.engine.setup.state.get("token")
                for task in self.tasks.values()
            )
        )

    def _active_generation(self, generation, message):
        return {**generation, "status": "generating", "message": message, "attempts": 0, "retry_at": None}

    async def schedule_today(self):
        return await self._respond(self._schedule_today)

    async def _schedule_today(self, request):
        async with self.write_lock:
            async with self.engine.mutation_lock:
                start, end = self.engine.window()
                day = self.engine.store.day(start.date().isoformat()) or {}
                state = self.engine.get_current_state()
                generation = self.engine.schedule_generation_state(day)
                generating = self._active_worker(self.engine.generation_task) or self.engine.generation_lock.locked()
                if generating:
                    generation = self._active_generation(generation, "正在生成今天的日程…")
                ready = day.get("status") == "ok" and self.engine._day_covers_window(day)
                status = "generating" if generating else day.get("status", "preparing")
                ready = ready and not generating
                if status == "ok" and not ready:
                    status = (
                        generation["status"] if generation["status"] in {"generating", "failed", "exhausted"} else "preparing"
                    )
                visible = []
                if ready:
                    for slot in day.get("actual", []):
                        left, right = parse_local(slot["start"], self.engine.tz), parse_local(slot["end"], self.engine.tz)
                        if right <= start or left >= end:
                            continue
                        item = project_slot_rest(slot)
                        item.update(start=max(start, left).isoformat(), end=min(end, right).isoformat())
                        item["outfit_label"] = slot_outfit_label(item, day)
                        visible.append(item)
                rows = self._outfit_rows(day, self.engine.wardrobe.display_entries())
        rows = await asyncio.to_thread(self._outfit_images, rows)
        return {
            "date": start.date().isoformat(),
            "weekday": get_calendar_info(start.date())["weekday"],
            "now": state["now"],
            "status": status,
            "actual": visible,
            "weather": copy.deepcopy(state["weather"]),
            "day_summary": str(day.get("day_summary", "")),
            "outfits": rows,
            "outfit_notes": str(day.get("outfit_notes", "") or ""),
            "business_start": start.isoformat(),
            "business_end": end.isoformat(),
            "generation": self._generation(generation),
            **self._clock(),
        }

    def _clock(self):
        return {"timezone": str(self.engine.tz), "server_time": self.engine.now().isoformat()}

    @staticmethod
    def _generation(state):
        # Persisted diagnostic strings stay in logs, away from user-facing data.
        return {key: copy.deepcopy(state.get(key)) for key in ("status", "message", "attempts", "retry_at")}

    async def schedule_retry(self):
        return await self._respond(self._schedule_retry, write=True)

    async def _schedule_retry(self, request):
        worker = self.engine.ensure_background(force=True)
        if worker is None:
            raise ViewError("插件正在关闭，请重新打开插件后再试", 409)
        return {
            "status": "ok",
            "task_status": "running",
            "message": "正在生成今天的日程…",
            "generation": self._generation(
                self._active_generation(self.engine.schedule_generation_state(), "正在生成今天的日程…")
            ),
            **self._clock(),
        }

    async def profile_get(self):
        return await self._respond(self._profile_get)

    async def _profile_get(self, request):
        await self.engine.check_persona()
        profile = self.config.get("profile", {})
        stored = self.config.get("profile_persona_hash", "")
        working = self._profile_working()
        generation = self.engine.profile_generation_state()
        if working:
            generation = self._active_generation(generation, "正在生成生活档案…")
        return {
            "profile": {key: str(profile.get(key, "") or "") for key in PROFILE_FIELDS},
            "profile_generated_at": self.config.get("profile_generated_at", ""),
            "persona_changed": self.engine.persona_changed,
            "persona_warning": getattr(self.engine.llm, "persona_warning", ""),
            "persona_hash_status": "unconfirmed"
            if not stored
            else "changed"
            if self.engine.persona_changed
            else "matched",
            "regenerating": working,
            "generation": self._generation(generation),
            **self._clock(),
        }

    async def profile_save(self):
        return await self._respond(self._profile_save, write=True)

    async def _profile_save(self, request):
        body = await self._body(request)
        if not body or any(key not in PROFILE_FIELDS for key in body):
            raise ViewError("档案包含未知字段")
        values = {key: self._text(value, "档案字段", 10000) for key, value in body.items()}
        persona = await self.engine.llm.persona()
        async with self.write_lock:
            if self._profile_working():
                raise ViewError("档案正在生成，请完成后再保存", 409)
            async with self.engine.profile_lock:
                profile = {key: str(self.config.get("profile", {}).get(key, "") or "") for key in PROFILE_FIELDS}
                profile.update(values)
                self._save_config({"profile": profile, "profile_persona_hash": self.engine.llm.persona_hash(persona)})
                self.engine.persona_changed = False
                state = self.engine.store.read("profile_generation.json", {})
                today = self.engine.now().date().isoformat()
                if state.get("date") != today:
                    state = {"date": today, "attempts": 0, "auto_attempts": 0}
                state.update(
                    status="ready" if any(profile.values()) else "idle",
                    last_error="",
                    retry_at=None,
                    message="生活档案已保存。",
                )
                self.engine.store.write("profile_generation.json", state)
        return {"saved": True, "profile": profile}

    def _save_tasks(self):
        # Completed history is bounded; never discard an accepted live task.
        token = self.engine.setup.state.get("token")
        current = {key: value for key, value in self.tasks.items() if token and value.get("role_token") == token}
        ordered = sorted(current.items(), key=lambda pair: pair[1].get("created_at", ""), reverse=True)
        active = [(key, item) for key, item in ordered if item.get("status") in {"queued", "running"}]
        completed = [(key, item) for key, item in ordered if item.get("status") not in {"queued", "running"}]
        self.tasks = dict(active + completed[: max(0, 30 - len(active))])
        self.engine.store.write("view_tasks.json", self.tasks)

    async def profile_regenerate(self):
        return await self._respond(self._profile_regenerate, write=True)

    async def _profile_regenerate(self, request):
        async with self.write_lock:
            active = next(
                (
                    task
                    for task in self.tasks.values()
                    if task.get("type", "profile") == "profile" and task.get("status") in {"queued", "running"}
                    and task.get("role_token") == self.engine.setup.state.get("token")
                ),
                None,
            )
            if active:
                if active.get("persona_id") != self.engine.setup.selected_id:
                    raise ViewError("所选人设已改变，请到基础设置确认后开始。", 409)
                return {
                    "task_id": active["task_id"],
                    "type": "profile",
                    "status": active["status"],
                    "generation": self._generation(self._active_generation({}, "正在生成生活档案…")),
                }
            if not self.engine.life_ready():
                raise ViewError(self.engine.setup.status()["message"], 409)
            worker = self.engine.ensure_profile_background(force=True)
            if worker is None:
                raise ViewError("插件正在关闭，请重新打开插件后再试", 409)
            task_id = uuid.uuid4().hex
            self.tasks[task_id] = {
                "task_id": task_id,
                "type": "profile",
                "status": "queued",
                "message": "等待生成档案",
                "created_at": self.engine.now().isoformat(),
                "persona_id": self.engine.setup.selected_id,
                "role_token": self.engine.setup.state["token"],
            }
            self._save_tasks()
            wrapper = self.engine.spawn(self._run_profile_task(task_id, worker))
            if wrapper is None:
                self.tasks[task_id].update(status="failed", message="插件正在卸载，请重载后再试")
                self._save_tasks()
                raise ViewError("插件正在卸载，请重载后再试", 409)
            self.engine.setup.observers.add(wrapper)
            wrapper.add_done_callback(self.engine.setup.observers.discard)
            wrapper.add_done_callback(lambda done, identifier=task_id: self._profile_task_done(identifier, done))
            return {
                "task_id": task_id,
                "type": "profile",
                "status": "queued",
                "generation": self._generation(self._active_generation({}, "正在生成生活档案…")),
            }

    async def _run_profile_task(self, task_id, worker):
        task = self.tasks[task_id]
        task.update(status="running", message="正在备份并生成档案")
        self._save_tasks()
        try:
            success = await worker
            state = self.engine.profile_generation_state()
            task.update(
                status="done" if success else "failed",
                message="档案已更新" if success else state.get("message") or "这次生成没有完成，请重新生成",
            )
        except asyncio.CancelledError:
            task.update(status="failed", message="生成因插件卸载中断，请重试")
            raise
        except Exception as exc:
            log.warning("视图后台生成档案失败：%s", describe_error(exc))
            task.update(status="failed", message=user_error(exc))
        finally:
            task["updated_at"] = self.engine.now().isoformat()
            self._save_tasks()

    def _profile_task_done(self, task_id, worker):
        # Cancellation before the wrapper starts skips its try/finally.
        task = self.tasks.get(task_id)
        if task and task.get("status") in {"queued", "running"}:
            task.update(status="failed", message="生成因插件卸载中断，请重试", updated_at=self.engine.now().isoformat())
            self._save_tasks()

    async def task_get(self):
        return await self._respond(self._task_get)

    async def _task_get(self, request):
        task_id = request.query.get("id", "")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id) or task_id not in self.tasks:
            raise ViewError("没有找到这项后台任务", 404)
        task = self.tasks[task_id]
        if task.get("role_token") != self.engine.setup.state.get("token") or self.engine.setup.state.get("persona_id") != self.engine.setup.selected_id:
            raise ViewError("这项任务属于之前的生活记录，请刷新页面。", 404)
        return copy.deepcopy(
            {
                key: task[key]
                for key in ("task_id", "type", "status", "message", "created_at", "updated_at", "result")
                if key in task
            }
        )

    def _providers(self):
        get_all = getattr(self.plugin.context, "get_all_providers", None)
        if not callable(get_all):
            return []
        result = []
        for provider in get_all() or []:
            try:
                meta = provider.meta()
                provider_id = str(meta.id)
                model = getattr(meta, "model", None) or provider.get_model()
                result.append({"id": provider_id, "name": provider_id + (" · " + str(model) if model else "")})
            except Exception as exc:
                log.debug("跳过无法读取的模型信息：%s", describe_error(exc))
        return result

    def _settings(self):
        return {
            "city": str(self.config.get("weather", {}).get("city", "") or ""),
            "schedule_time": self.config.get("schedule_time", "07:00"),
            "owner_ids": copy.deepcopy(self.config.get("owner_ids", [])),
            "llm_provider": str(self.config.get("llm_provider", "") or ""),
            "routine_jitter_minutes": self.config.get("routine_jitter_minutes", 20),
            "surprise_probability": self.config.get("surprise_probability", 0.5),
            "change_trigger_keywords": copy.deepcopy(self.config.get("change_trigger_keywords", DEFAULT_KEYWORDS)),
            "providers": self._providers(),
        }

    async def settings_get(self):
        return await self._respond(self._settings_get)

    async def _settings_get(self, request):
        return self._settings()

    @staticmethod
    def _list(value, label, limit=100, split_space=False):
        if isinstance(value, str):
            value = re.split(r"[,，\s]+" if split_space else r"[,，\n]+", value)
        if (
            not isinstance(value, list)
            or len(value) > limit
            or any(not isinstance(item, str) or len(item) > 128 for item in value)
        ):
            raise ViewError(label + "格式不正确或内容过多")
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    async def settings_save(self):
        return await self._respond(self._settings_save, write=True)

    async def _settings_save(self, request):
        body = await self._body(request)
        allowed = {
            "city",
            "schedule_time",
            "owner_ids",
            "llm_provider",
            "routine_jitter_minutes",
            "surprise_probability",
            "change_trigger_keywords",
        }
        if not body or set(body) - allowed:
            raise ViewError("设置包含未知字段")
        async with self.write_lock:
            current = self._settings()
            values = {**current, **body}
            city = self._text(values["city"], "城市", 80)
            schedule_time = self._text(values["schedule_time"], "每日生成时间", 5)
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
                raise ViewError("每日生成时间请写成 07:00 这样的格式，小时为 00–23，分钟为 00–59")
            provider_id = self._text(values["llm_provider"], "模型", 256)
            if provider_id and self.plugin.context.get_provider_by_id(provider_id) is None:
                raise ViewError("所选模型已不可用，请刷新后重新选择")
            jitter = values["routine_jitter_minutes"]
            if type(jitter) is not int or not 0 <= jitter <= 60:
                raise ViewError("作息浮动请填写 0–60 的整数")
            probability = values["surprise_probability"]
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise ViewError("小意外概率需要在 0–1 之间")
            owner_ids = self._list(values["owner_ids"], "主人用户 ID", split_space=True)
            keywords = self._list(values["change_trigger_keywords"], "关键词", limit=256)
            changes = {
                "weather": {**self.config.get("weather", {}), "city": city},
                "schedule_time": schedule_time,
                "owner_ids": owner_ids,
                "llm_provider": provider_id,
                "routine_jitter_minutes": jitter,
                "surprise_probability": float(probability),
                "change_trigger_keywords": keywords,
            }
            previous = {key: copy.deepcopy(self.config.get(key)) for key in changes}
            self._save_config(changes)
            try:
                if schedule_time != current["schedule_time"]:
                    self.engine.reschedule()
            except Exception:
                self._save_config(previous)
                self.engine.reschedule()
                raise
            if city != current["city"]:
                self.engine.weather.invalidate_location()
        if city != current["city"] and self.config.get("enabled", True):
            self.engine.spawn(self.engine.refresh_weather(update_day=False))
        message = "设置已保存并立即生效。" + ("未填写城市时，将按季节模拟天气。" if not city else "")
        await self.engine.setup.refresh()
        return {"saved": True, "message": message, **self._settings()}
