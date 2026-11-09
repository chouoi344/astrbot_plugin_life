"""The authoritative, ordered reference images for Natural Photo."""

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from threading import Lock
from uuid import uuid4

from .provider import MAX_IMAGE_BYTES, PhotoError, validate_image

MAX_REFERENCE_UPLOAD_BYTES = 20 * 1024 * 1024
_LEGACY_KEYS = {"face": "reference_images", "body": "body_reference_images"}
_MAX_NEW_REFERENCES = 3
_STORED_LIMIT = {"face": 8, "body": 3}
_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class ReferenceConflict(ValueError):
    """The group changed after the dashboard displayed its image indices."""


def reference_revision(paths: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(paths).encode("utf-8")).hexdigest()


def _reference_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if len(path.parts) < 2 or path.parts[0] != "files" or ".." in path.parts:
        raise ValueError("参考图路径无效")
    return path


class ReferenceLibrary:
    def __init__(self, data_dir, legacy_config):
        self.data_dir = data_dir.resolve()
        self.path = self.data_dir / "reference_library.json"
        self._lock = Lock()
        self._closed = False
        self._groups = {group: () for group in _LEGACY_KEYS}
        self.error = ""
        try:
            if self.path.is_symlink():
                raise ValueError("参考图清单路径无效")
            if self.path.exists():
                stored = json.loads(self.path.read_text(encoding="utf-8-sig"))
                self._groups = self._parse_groups(stored)
            else:
                groups = self._parse_groups(
                    {
                        group: legacy_config.get(key, [])
                        for group, key in _LEGACY_KEYS.items()
                    }
                )
                self._write_index(groups)
                self._groups = groups
        except (OSError, ValueError):
            self.error = "参考图资料无法读取或迁移，请检查插件数据后重新加载。"

    @staticmethod
    def _parse_groups(raw):
        if not isinstance(raw, dict) or raw.keys() != _LEGACY_KEYS.keys():
            raise ValueError("参考图清单格式无效")
        groups = {}
        for group, values in raw.items():
            if (
                not isinstance(values, list)
                or len(values) > _STORED_LIMIT[group]
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError("参考图列表无效")
            for value in values:
                _reference_path(value)
            groups[group] = tuple(values)
        return groups

    def _write_index(self, groups):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(
            dir=self.data_dir, prefix=".reference-library-", suffix=".tmp"
        )
        temp = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(groups, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._groups)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _files_root(self):
        root = self.data_dir / "files"
        if root.is_symlink():
            raise ValueError("参考图目录无效")
        return root

    def read_image(self, value: str) -> tuple[bytes, str]:
        root = self._files_root()
        relative = _reference_path(value)
        path = self.data_dir.joinpath(*relative.parts).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("参考图文件无效")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise PhotoError("参考图超过 50 MB")
        with path.open("rb") as stream:
            return validate_image(stream.read(MAX_IMAGE_BYTES + 1))

    def _editable_paths(self, group: str, revision: str | None):
        if self._closed:
            raise ValueError("插件正在重新加载，请稍后刷新页面")
        if self.error:
            raise ValueError(self.error)
        paths = self._groups[group]
        if revision is not None and revision != reference_revision(paths):
            raise ReferenceConflict("参考图已变化，请刷新页面后重试。")
        return paths

    def delete_image(self, group: str, index: int, revision: str):
        with self._lock:
            paths = self._editable_paths(group, revision)
            if not 0 <= index < len(paths):
                raise ValueError("找不到要删除的参考图")
            groups = {**self._groups, group: paths[:index] + paths[index + 1 :]}
            self._write_index(groups)
            self._groups = groups
            return dict(groups)

    def save_image(
        self, group: str, index: int | None, data: bytes, revision: str | None = None
    ):
        if len(data) > MAX_REFERENCE_UPLOAD_BYTES:
            raise PhotoError("参考图不能超过 20 MB")
        data, mime = validate_image(data)
        with self._lock:
            values = list(self._editable_paths(group, revision))
            if index is None:
                if len(values) >= _MAX_NEW_REFERENCES:
                    raise ValueError("这一组已达到三张，请替换现有图片")
                index = len(values)
                values.append("")
            elif not 0 <= index < len(values):
                raise ValueError("找不到要替换的参考图，请刷新页面")

            directory = self._files_root() / "reference_library"
            if directory.is_symlink():
                raise ValueError("参考图目录无效")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{group}-{uuid4().hex}{_SUFFIX[mime]}"
            values[index] = path.relative_to(self.data_dir).as_posix()
            groups = {**self._groups, group: tuple(values)}
            created = False
            try:
                with path.open("xb") as stream:
                    created = True
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._write_index(groups)
            except OSError:
                if created:
                    path.unlink(missing_ok=True)
                raise
            self._groups = groups
            return dict(groups)
