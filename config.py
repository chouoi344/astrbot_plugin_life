"""A component config remains a live view over one native AstrBot section."""

import json
from collections.abc import MutableMapping
from pathlib import Path


class ComponentConfig(MutableMapping):
    def __init__(self, parent, section: str, write_lock):
        self.parent = parent
        self.section = section
        self._write_lock = write_lock

    @property
    def schema(self):
        return self.parent.schema[self.section]["items"]

    @property
    def config_path(self):
        return self.parent.config_path

    @property
    def _values(self):
        return self.parent[self.section]

    def __getitem__(self, key):
        return self._values[key]

    def __setitem__(self, key, value):
        self._values[key] = value

    def __delitem__(self, key):
        del self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def copy(self):
        return self._values.copy()

    def _read_saved_parent(self):
        return json.loads(Path(self.config_path).read_text(encoding="utf-8-sig"))

    def read_saved_config(self):
        return self._read_saved_parent()[self.section]

    def save_config(self, replace_config=None, *, indent=2):
        with self._write_lock:
            if replace_config is not None:
                self.update(replace_config)
            try:
                persisted = self._read_saved_parent()
            except FileNotFoundError:
                persisted = {}
            previous_schedule_marker = self.parent["schedule"]["settings_saved"]
            for name, value in persisted.items():
                if name != self.section:
                    self.parent[name] = value
            if self.section != "schedule" and "schedule" not in persisted:
                self.parent["schedule"]["settings_saved"] = False
            try:
                self.parent.save_config(indent=indent)
            finally:
                if self.section != "schedule":
                    # This live marker is offered to the native settings form.
                    # An unrelated component must preserve the last disk value.
                    self.parent["schedule"]["settings_saved"] = previous_schedule_marker
