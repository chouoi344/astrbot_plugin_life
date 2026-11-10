"""Small atomic JSON store. Corrupt originals are preserved for inspection."""

import copy
import datetime as dt
import json
import os
import tempfile
import zipfile
from pathlib import Path

from astrbot.api import logger

from .errors import describe_error


class JsonStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ("days", "profile_backups", "wardrobe_images"):
            (self.root / directory).mkdir(exist_ok=True)
        self._cache = {}

    def _path(self, name):
        path = (self.root / str(name)).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("Data path must remain inside plugin data directory")
        return path

    def read(self, name, default=None):
        path = self._path(name)
        # Cached payloads remain isolated from callers' in-place mutations.
        if str(name) in self._cache:
            return copy.deepcopy(self._cache[str(name)])
        if not path.exists():
            return copy.deepcopy(default)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if default is not None and not isinstance(value, type(default)):
                raise ValueError("Unexpected JSON root type")
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            logger.error("日程数据文件损坏，保留原文件：%s (%s)", path, describe_error(exc))
            broken = path.with_name(path.name + ".broken")
            suffix = 1
            while broken.exists():
                broken = path.with_name(path.name + ".%d.broken" % suffix)
                suffix += 1
            path.replace(broken)
            return copy.deepcopy(default)
        self._cache[str(name)] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def write(self, name, value):
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._cache[str(name)] = copy.deepcopy(value)

    def day(self, date_str):
        if isinstance(date_str, (dt.date, dt.datetime)):
            date_str = date_str.date().isoformat() if isinstance(date_str, dt.datetime) else date_str.isoformat()
        day_key = dt.date.fromisoformat(str(date_str)).isoformat()
        value = self.read("days/%s.json" % day_key, {})
        return value or None

    def save_day(self, day):
        day_key = dt.date.fromisoformat(day["date"]).isoformat()
        self.write("days/%s.json" % day_key, day)

    def backup_role(self, config, now):
        """Verify a complete archive before any live role data is moved."""
        directory = self.root / "role_backups" / now.strftime("%Y%m%dT%H%M%S%f")
        directory.mkdir(parents=True)
        archive = directory / "life.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("role_config.json", json.dumps(config, ensure_ascii=False, allow_nan=False))
            for path in self.root.rglob("*"):
                if path.is_file() and "role_backups" not in path.relative_to(self.root).parts:
                    output.write(path, "data/" + path.relative_to(self.root).as_posix())
        with zipfile.ZipFile(archive) as saved:
            if saved.testzip() is not None:
                raise OSError("旧生活资料备份校验失败，原资料已保留。")
        return directory

    def retire_role_files(self, backup):
        """Move live files into the verified backup; failures restore all moves."""
        destination = backup / "retired"
        destination.mkdir()
        moved = []
        try:
            for path in self.root.iterdir():
                if path.name in {"role_backups", "setup_state.json", "runtime_config.json"}:
                    continue
                target = destination / path.name
                path.replace(target)
                moved.append((path, target))
        except BaseException:
            for path, target in reversed(moved):
                target.replace(path)
            raise
        self._cache.clear()
        return moved

    def cleanup(self, today, keep_days=60):
        if isinstance(today, dt.datetime):
            today = today.date()
        if isinstance(today, str):
            today = dt.date.fromisoformat(today)
        cutoff = today - dt.timedelta(days=max(1, int(keep_days)))
        for path in (self.root / "days").glob("*.json"):
            try:
                file_day = dt.date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_day < cutoff:
                path.unlink(missing_ok=True)
                self._cache.pop("days/%s" % path.name, None)
