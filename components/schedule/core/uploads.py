"""Import saved native AstrBot file fields without blocking normal chat."""

import asyncio
import copy
import hashlib
import uuid
from pathlib import Path

from PIL import Image

from astrbot.api import logger as log

from .errors import describe_error, run_with_timeout, user_error
from .storage import JsonStore


class WardrobeUploadProcessor:
    MAX_BYTES = 20 * 1024 * 1024
    MAX_PIXELS = 60_000_000

    def __init__(self, config, wardrobe, llm, images, data_dir):
        self.config, self.wardrobe, self.llm, self.images = config, wardrobe, llm, images
        self.data_dir = Path(data_dir).resolve()
        self.upload_dir = self.data_dir / "files"
        self.store = JsonStore(self.data_dir)
        self.statuses = self.store.read("wardrobe_uploads.json", {})
        self.lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()

    def _unchanged(self, original, current, references):
        return bool(current) and self._uploads(current) == references and all(
            current.get(key) == original.get(key) for key in ("description", "category", "seasons", "image_processed_hash"))

    async def _restore_image(self, entry_id, payload):
        worker = asyncio.create_task(asyncio.to_thread(self.images.restore, entry_id, payload))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # to_thread cannot stop a running atomic file replacement. Finish
            # it before rollback so a late worker cannot overwrite old data.
            try:
                await worker
            except Exception as exc:
                log.warning("衣柜展示图提交中断：%s", describe_error(exc))
            raise

    async def _commit(self, original, references, normalized, fingerprint, thumbnail):
        # Image decoding and the model run outside this shared lock. Only a
        # short final file/config transaction excludes simultaneous API edits.
        async with self.config_lock:
            current = next((item for item in self.wardrobe.all() if item["id"] == original["id"]), None)
            if not self._unchanged(original, current, references):
                self._status(original["id"], "failed", "已保留刚刚修改的穿搭，请重新上传后保存配置再识别。")
                return False
            previous = copy.deepcopy(current)
            previous_image = await asyncio.to_thread(self.images.snapshot, original["id"])
            try:
                await self._restore_image(original["id"], thumbnail)
                self.wardrobe.update(original["id"], **normalized)
                for configured in self.config.get("wardrobe", []):
                    if configured.get("id") == original["id"]:
                        configured["image_processed_hash"] = fingerprint
                        break
                self.wardrobe._save()
            except BaseException:
                for index, configured in enumerate(self.config.get("wardrobe", [])):
                    if configured.get("id") == original["id"]:
                        self.config["wardrobe"][index] = previous
                        break
                await self._restore_image(original["id"], previous_image)
                try:
                    self.wardrobe._save()
                except Exception as save_exc:
                    log.warning("衣柜配置保存失败，内存中的旧方案已保留：%s", describe_error(save_exc))
                raise
            return True

    @staticmethod
    def _uploads(entry):
        value = entry.get("image_upload", [])
        return [path for path in value if isinstance(path, str) and path.strip()][:4] if isinstance(value, list) else []

    def _inspect(self, references):
        digest = hashlib.sha256()
        paths = []
        for reference in references:
            source = Path(reference)
            path = (source if source.is_absolute() else self.data_dir / source).resolve()
            if self.upload_dir not in path.parents:
                raise ValueError("只处理本插件配置面板上传的图片")
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("图片格式请使用 PNG、JPG 或 WebP")
            if not path.is_file():
                raise ValueError("上传的图片文件已不存在，请重新上传后保存配置")
            if path.stat().st_size > self.MAX_BYTES:
                raise ValueError("单张图片请控制在 20 MB 以内")
            with Image.open(path) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("文件内容与支持的图片格式不符")
                if image.width * image.height > self.MAX_PIXELS:
                    raise ValueError("图片分辨率过大，请缩小后重新上传")
                image.verify()
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            paths.append(str(path))
        return paths, digest.hexdigest()

    def _status(self, entry_id, status, message=""):
        self.statuses[entry_id] = {"status": status, "message": message}
        self.store.write("wardrobe_uploads.json", self.statuses)

    async def process(self):
        async with self.lock:
            entries = self.wardrobe.all()
            valid_ids = {entry["id"] for entry in entries}
            self.statuses = {key: value for key, value in self.statuses.items() if key in valid_ids}
            for entry in entries:
                references = self._uploads(entry)
                if not references:
                    self.statuses.pop(entry["id"], None)
                    continue
                staging_id = None
                try:
                    paths, fingerprint = await asyncio.to_thread(self._inspect, references)
                    if fingerprint == entry.get("image_processed_hash"):
                        self._status(entry["id"], "ok", "图片穿搭已提取")
                        continue
                    self._status(entry["id"], "processing", "正在提取穿搭…")
                    normalized = await self.llm.describe_outfit(paths, "仅整理这套图片中的穿戴信息，自动判断分类和适用季节。")
                    current = next((item for item in self.wardrobe.all() if item["id"] == entry["id"]), None)
                    if not self._unchanged(entry, current, references):
                        self._status(entry["id"], "failed", "已保留刚刚修改的穿搭，请重新上传后保存配置再识别。")
                        continue
                    # Recheck content after inference: replacing the same file
                    # while a model is running must not acknowledge stale data.
                    _, current_hash = await asyncio.to_thread(self._inspect, references)
                    if current_hash != fingerprint:
                        self._status(entry["id"], "failed", "图片在识别期间变化，请再次保存配置重试")
                        continue
                    staging_id = "wardrobe-upload-stage-" + uuid.uuid4().hex
                    await run_with_timeout(self.images.save(staging_id, paths[0]), 30, "准备衣柜展示图")
                    thumbnail = await asyncio.to_thread(self.images.snapshot, staging_id)
                    _, current_hash = await asyncio.to_thread(self._inspect, references)
                    if current_hash != fingerprint:
                        self._status(entry["id"], "failed", "图片在识别期间变化，请再次保存配置重试")
                        continue
                    if await self._commit(entry, references, normalized, fingerprint, thumbnail):
                        self._status(entry["id"], "ok", "图片穿搭已提取")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("衣柜图片提取失败（%s）：%s", entry["id"], describe_error(exc))
                    self._status(entry["id"], "failed", user_error(exc, action="识别穿搭图片"))
                finally:
                    if staging_id:
                        self.images.remove(staging_id)
            self.store.write("wardrobe_uploads.json", self.statuses)
