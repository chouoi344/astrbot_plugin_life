"""Authenticated reference list, preview, and edit routes."""

import asyncio
import base64
import io
import json
import re

from astrbot.api.web import json_response, request
from PIL import Image, ImageOps

from ....runtime import ROOT_PLUGIN_ID
from .provider import PhotoError
from .reference_library import (
    MAX_REFERENCE_UPLOAD_BYTES,
    ReferenceConflict,
    reference_revision,
)

_ROUTE_ROOT = f"/{ROOT_PLUGIN_ID}/photo"
_GROUPS = ("face", "body")
_IMAGE_HEADERS = {
    "data:image/jpeg;base64",
    "data:image/png;base64",
    "data:image/webp;base64",
}
_MAX_ENCODED_BYTES = 4 * ((MAX_REFERENCE_UPLOAD_BYTES + 2) // 3)
_MAX_BODY_BYTES = _MAX_ENCODED_BYTES + 512
_MAX_DELETE_BODY_BYTES = 512
_TOO_LARGE = "单张图片请控制在 20 MB 以内。"
_NO_CACHE = {"Cache-Control": "no-store"}


def _error(message: str, status: int):
    return json_response(
        {"status": "error", "message": message},
        status_code=status,
        headers=_NO_CACHE,
    )


def _valid_revision(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class PhotoDashboard:
    """Use the host's authenticated routes and keep write permission local."""

    def __init__(self, plugin):
        self.plugin = plugin
        self._preview_slots = asyncio.Semaphore(2)

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        register(
            f"{_ROUTE_ROOT}/references",
            self.references,
            ["GET"],
            "自然照片：参考素材状态",
        )
        register(
            f"{_ROUTE_ROOT}/reference",
            self.reference,
            ["GET"],
            "自然照片：参考素材预览",
        )
        register(
            f"{_ROUTE_ROOT}/reference/upload",
            self.upload,
            ["POST"],
            "自然照片：上传参考素材",
        )
        register(
            f"{_ROUTE_ROOT}/reference/delete",
            self.delete,
            ["POST"],
            "自然照片：删除参考素材",
        )
        return True

    def unregister(self) -> None:
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [row for row in routes if getattr(row[1], "__self__", None) is not self]

    def _is_dashboard_admin(self) -> bool:
        # The host has already verified the credential. Inspect the same token
        # to distinguish dashboard login from an asset token with the same name.
        username = request.username
        dashboard = self.plugin.context.get_config().get("dashboard", {})
        if (
            not username
            or username.startswith("api_key:")
            or not isinstance(dashboard, dict)
            or username != dashboard.get("username")
        ):
            return False

        authorization = request.headers.get("authorization", "").strip()
        token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        if not token:
            token = request.cookies.get("astrbot_dashboard_jwt", "").strip()
        try:
            _, segment, _ = token.split(".")
            claims = json.loads(
                base64.b64decode(
                    segment + "=" * (-len(segment) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            )
        except ValueError:
            return False
        return (
            isinstance(claims, dict)
            and claims.get("username") == username
            and claims.get("token_type") in (None, "dashboard", "access")
            and not any(field in claims for field in ("scope", "scopes", "plugin_name", "page_name"))
        )

    def _manifest(self, snapshot, *, admin: bool):
        settings = self.plugin.settings
        library_error = self.plugin.reference_library.error
        return json_response(
            {
                "generate_enabled": bool(settings and settings.generate_enabled),
                "edit_enabled": bool(settings and settings.edit_enabled),
                "selfie_enabled": bool(settings and settings.selfie_enabled),
                "configured": bool(settings and settings.configured),
                "config_error": bool(self.plugin.config_error),
                "library_error": library_error,
                "can_write": admin and not library_error,
                "revisions": {group: reference_revision(snapshot[group]) for group in _GROUPS},
                **{group: [{"index": index} for index, _ in enumerate(snapshot[group])] for group in _GROUPS},
            },
            headers=_NO_CACHE,
        )

    async def references(self):
        snapshot = await asyncio.to_thread(self.plugin.reference_library.snapshot)
        return self._manifest(snapshot, admin=self._is_dashboard_admin())

    async def reference(self):
        query = request.query
        groups, indices = query.getlist("group"), query.getlist("index")
        revisions = query.getlist("revision")
        if (
            set(query.keys()) not in ({"group", "index"}, {"group", "index", "revision"})
            or ("revision" in query and (len(revisions) != 1 or not _valid_revision(revisions[0])))
            or len(groups) != 1
            or groups[0] not in _GROUPS
            or len(indices) != 1
            or not re.fullmatch(r"0|[1-9][0-9]{0,2}", indices[0])
        ):
            return _error("参考素材编号无效。", 400)

        library = self.plugin.reference_library
        snapshot = await asyncio.to_thread(library.snapshot)
        paths = snapshot[groups[0]]
        if revisions and revisions[0] != reference_revision(paths):
            return _error("参考图已变化，请刷新页面后重试。", 409)
        index = int(indices[0])
        if index >= len(paths):
            return _error("没有找到这张参考素材。", 404)
        try:
            async with self._preview_slots:
                preview = await asyncio.to_thread(self._read_thumbnail, paths[index])
        except (OSError, ValueError, RuntimeError):
            return _error("参考素材无法预览，请重新上传。", 404)
        return json_response(preview, headers=_NO_CACHE)

    def _read_thumbnail(self, path: str) -> dict[str, str]:
        data, _ = self.plugin.reference_library.read_image(path)
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((480, 480))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=84)
        return {
            "mime": "image/jpeg",
            "base64": base64.b64encode(output.getvalue()).decode("ascii"),
        }

    async def upload(self):
        if not self._is_dashboard_admin():
            return _error("请先用 AstrBot 面板管理员账号登录。", 403)
        library = self.plugin.reference_library
        if library.error:
            return _error(library.error, 503)

        content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return _error("请从自然照片面板上传一张图片。", 415)
        length = request.headers.get("content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > _MAX_BODY_BYTES):
            return _error(_TOO_LARGE, 413)
        raw = await request.body()
        if len(raw) > _MAX_BODY_BYTES:
            return _error(_TOO_LARGE, 413)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return _error("上传内容无效，请重新选择图片。", 400)
        required = {"group", "index", "image"}
        if not isinstance(payload, dict) or payload.keys() not in (
            required,
            required | {"revision"},
        ):
            return _error("上传内容无效，请重新选择图片。", 400)
        revision = payload.get("revision")
        if "revision" in payload and not _valid_revision(revision):
            return _error("参考图版本无效，请刷新页面后重试。", 400)

        group, index, image = payload["group"], payload["index"], payload["image"]
        if group not in _GROUPS or (index is not None and (type(index) is not int or index < 0)):
            return _error("参考素材分类或编号无效。", 400)
        if index is not None and revision is None:
            return _error("请刷新页面后再替换参考图。", 409)
        if not isinstance(image, str):
            return _error("请上传 JPG、PNG 或 WebP 图片。", 400)
        header, separator, encoded = image.partition(",")
        if not separator or header not in _IMAGE_HEADERS or not encoded:
            return _error("请上传 JPG、PNG 或 WebP 图片。", 400)
        if len(encoded) > _MAX_ENCODED_BYTES:
            return _error(_TOO_LARGE, 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return _error("图片内容不完整，请重新选择图片。", 400)
        if len(data) > MAX_REFERENCE_UPLOAD_BYTES:
            return _error(_TOO_LARGE, 413)

        try:
            snapshot = await asyncio.to_thread(library.save_image, group, index, data, revision)
        except ReferenceConflict as exc:
            return _error(str(exc), 409)
        except PhotoError:
            return _error("没有读到有效的 JPG、PNG 或 WebP 图片。", 400)
        except ValueError:
            return _error("参考素材编号无效或该分区已满，请刷新后再试。", 400)
        except OSError:
            return _error("参考素材暂时无法保存，请稍后再试。", 500)
        return self._manifest(snapshot, admin=True)

    async def delete(self):
        if not self._is_dashboard_admin():
            return _error("请先用 AstrBot 面板管理员账号登录。", 403)
        library = self.plugin.reference_library
        if library.error:
            return _error(library.error, 503)

        content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return _error("请从自然照片面板删除参考图。", 415)
        length = request.headers.get("content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > _MAX_DELETE_BODY_BYTES):
            return _error("删除内容无效。", 413)
        raw = await request.body()
        if len(raw) > _MAX_DELETE_BODY_BYTES:
            return _error("删除内容无效。", 413)
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return _error("删除内容无效。", 400)
        if not isinstance(payload, dict) or payload.keys() != {
            "group",
            "index",
            "revision",
        }:
            return _error("删除内容无效。", 400)
        group, index, revision = payload["group"], payload["index"], payload["revision"]
        if group not in _GROUPS or type(index) is not int or index < 0 or not _valid_revision(revision):
            return _error("参考素材分类、编号或版本无效。", 400)
        try:
            snapshot = await asyncio.to_thread(library.delete_image, group, index, revision)
        except ReferenceConflict as exc:
            return _error(str(exc), 409)
        except ValueError:
            return _error("找不到要删除的参考图，请刷新页面后重试。", 400)
        except OSError:
            return _error("暂时无法保存删除结果，请稍后再试。", 500)
        return self._manifest(snapshot, admin=True)
