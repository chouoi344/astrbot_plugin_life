"""Read-only presentation of a character's recorded photo work."""

import asyncio
import base64
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageOps

from .components.photo.core.photo_context import PhotoScope

_JOB = re.compile(r"[0-9a-f]{32}")
_EXTENSIONS = (".png", ".jpg", ".webp")
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024
_MAX_PIXELS = 25_000_000
_LIST_LIMIT = 16


def _scope_id(scope):
    key = json.dumps([scope.umo, scope.conversation_id, scope.persona_id])
    return hashlib.sha256(key.encode()).hexdigest()


def _image_path(photo, group_id, filename):
    if not _JOB.fullmatch(group_id) or not isinstance(filename, str):
        return None
    if not re.fullmatch(rf"{group_id}-[1-4]\.(?:png|jpg|webp)", filename):
        return None
    cache = photo.cache
    path = cache / filename
    # Both the cache itself and the named entry must stay under the data root.
    if (
        cache.resolve() != photo.data_dir.resolve() / "photos"
        or path.resolve().parent != cache.resolve()
        or path.is_symlink()
        or path.suffix not in _EXTENSIONS
        or not path.is_file()
    ):
        return None
    return path


def _photo_rows(photo, persona_id):
    """Keep scope in memory; expose only a generated ID and display fields."""
    recorded = photo.records["tasks"]
    by_id = {row["id"]: row for row in recorded}
    groups = {}

    for task in reversed(recorded):
        owner = task.get("dashboard_scope")
        if not owner or owner.get("persona_id") != persona_id or not owner.get("conversation_id"):
            continue
        scope = PhotoScope(**owner)
        groups.setdefault(scope, {})[task["id"]] = (task, task.get("dashboard_outputs", []))

    # Older task records lack ownership. A confirmed photo context can prove
    # the exact group's owner and the files that were actually sent.
    for scope, batches in photo.photo_context._groups.items():
        if scope.persona_id != persona_id or not scope.conversation_id:
            continue
        for batch in batches:
            first = batch[0]
            tasks = groups.setdefault(scope, {})
            if first.group_id not in tasks:
                saved = by_id.get(first.group_id, {})
                owner = saved.get("dashboard_scope")
                if owner and PhotoScope(**owner) != scope:
                    continue
                task = {
                    "id": first.group_id,
                    "action": saved.get("action", "generate"),
                    "dashboard_mode": first.mode,
                    "status": saved.get("status", "sent"),
                    "sent_count": max(saved.get("sent_count", 0), len(batch)),
                }
                tasks[first.group_id] = (task, [record.path.name for record in batch])

    order = {task["id"]: index for index, task in enumerate(recorded)}
    return {
        scope: sorted(tasks.values(), key=lambda item: order.get(item[0]["id"], -1), reverse=True)
        for scope, tasks in groups.items()
    }


async def _available_scopes(plugin, photo, persona_id):
    groups = _photo_rows(photo, persona_id)
    if not groups:
        return {}
    manager = plugin.context.conversation_manager
    selected = {}
    for umo in dict.fromkeys(scope.umo for scope in groups):
        cid = await manager.get_curr_conversation_id(umo)
        scope = PhotoScope(umo, str(cid or ""), persona_id)
        if scope in groups:
            selected[_scope_id(scope)] = (scope, groups[scope])
    priorities = (plugin.config["chat"].get("target_session"), plugin.config["schedule"].get("default_reference_umo"))
    return dict(
        sorted(
            selected.items(),
            key=lambda item: (
                priorities.index(item[1][0].umo) if item[1][0].umo in priorities else len(priorities),
                item[0],
            ),
        )
    )


def _session_label(plugin, scope):
    platform_id = scope.umo.split(":", 1)[0]
    platform = plugin.context.get_platform_inst(platform_id)
    kind = platform.meta().name if platform else platform_id
    return {
        "aiocqhttp": "QQ 私聊",
        "qq_official": "QQ 官方私聊",
        "qq_official_webhook": "QQ 官方私聊",
        "webchat": "网页聊天",
        "weixin_oc": "微信私聊",
    }.get(kind, "私聊")


def _photo_items(photo, scope_id, tasks):
    items = []
    paths = {}
    for task, filenames in tasks:
        job_id = task["id"]
        status = task["status"]
        for index, filename in enumerate(filenames or [None], 1):
            item_id = hashlib.sha256(f"{scope_id}/{job_id}/{index}".encode()).hexdigest()
            path = _image_path(photo, job_id, filename)
            if path:
                paths[item_id] = path
            sent = task["sent_count"]
            delivery = status
            if filename:
                if index <= sent:
                    delivery = "sent"
                elif status == "partial" or (status == "unknown" and index > sent + 1):
                    delivery = "not_sent"
            items.append(
                {
                    "id": item_id,
                    "action": task["action"],
                    "mode": task.get("dashboard_mode"),
                    "delivery_status": delivery,
                    "task_delivery_status": status,
                    "generation_status": (
                        "generated" if filename else "pending" if status in {"queued", "generating"} else "unknown"
                    ),
                    "sent_count": sent,
                    "created_at": task.get("dashboard_created_at"),
                    "image_id": item_id if path else None,
                }
            )
            if len(items) >= _LIST_LIMIT:
                return items, paths
    return items, paths


def _preview(path: Path):
    with path.open("rb") as source_file:
        raw = source_file.read(_MAX_PREVIEW_BYTES + 1)
    if len(raw) > _MAX_PREVIEW_BYTES:
        raise ValueError("Image exceeds the preview size limit")
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > _MAX_PIXELS:
            raise ValueError("Invalid preview image")
        image = ImageOps.exif_transpose(source)
        image.thumbnail((480, 480))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=84)
    return {"data_url": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")}


class PhotoHistory:
    def __init__(self, plugin):
        self.plugin = plugin
        self._preview_slots = asyncio.Semaphore(2)

    async def read(self, photo, persona_id, requested_scope):
        scopes = await _available_scopes(self.plugin, photo, persona_id)
        manager = self.plugin.context.conversation_manager
        lookup = getattr(manager, "get_conversation", None)
        labels = {}
        numbered = {}
        for identifier, (scope, _) in scopes.items():
            label = _session_label(self.plugin, scope)
            conversation = await lookup(scope.umo, scope.conversation_id) if callable(lookup) else None
            title = getattr(conversation, "title", None)
            if isinstance(title, str) and title.strip():
                labels[identifier] = f"{label} · {title.strip()[:80]}"
            else:
                numbered[label] = numbered.get(label, 0) + 1
                labels[identifier] = f"{label} {numbered[label]}"

        scope_id = requested_scope if requested_scope in scopes else next(iter(scopes), None)
        if scope_id:
            selected_scope = scopes[scope_id][0]
            current_id = await manager.get_curr_conversation_id(selected_scope.umo)
            if current_id != selected_scope.conversation_id:
                scope_id = None
        items = _photo_items(photo, scope_id, scopes[scope_id][1])[0] if scope_id else []
        timezone = str(self.plugin.context.get_config().get("timezone") or "Asia/Shanghai").strip()
        try:
            zone = ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            zone = ZoneInfo("Asia/Shanghai")
        for item in items:
            if created_at := item["created_at"]:
                item["created_at"] = datetime.fromisoformat(created_at).astimezone(zone).isoformat()
        return {
            "scope_id": scope_id,
            "scopes": [{"id": identifier, "name": label} for identifier, label in labels.items()],
            "items": items,
        }

    async def image(self, photo, persona_id, scope_id, image_id):
        scopes = await _available_scopes(self.plugin, photo, persona_id)
        if scope_id not in scopes:
            return None
        path = _photo_items(photo, scope_id, scopes[scope_id][1])[1].get(image_id)
        if not path:
            return None
        try:
            async with self._preview_slots:
                result = await asyncio.to_thread(_preview, path)
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
        scope = scopes[scope_id][0]
        current = await self.plugin.context.conversation_manager.get_curr_conversation_id(scope.umo)
        return result if current == scope.conversation_id else None
