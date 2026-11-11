"""Run the tests without AstrBot: ``astrbot.*`` and ``mcp.*`` imports get stub modules.

The stubs only supply names; tests cover the plugin's own pure logic.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class _StubMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Stub()


class _Stub(metaclass=_StubMeta):
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

    def __call__(self, *args, **kwargs):
        # Decorators such as @register(...) or @filter.command(...) keep the function.
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return _Stub()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Stub()

    def __or__(self, other):
        return self

    __ror__ = __or__

    def __getitem__(self, key):
        return _Stub()

    def __setitem__(self, key, value):
        pass

    def __iter__(self):
        return iter(())


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = type(name, (_Stub,), {})
        setattr(self, name, value)
        return value


# Supplied by AstrBot at runtime, not by this plugin's requirements.
_STUBBED = {"astrbot", "mcp"}


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _STUBBED:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        module = _StubModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        if module.__name__ == "astrbot":
            module.__version__ = "4.29.0"
        if module.__name__ == "astrbot.api":
            module.logger = logging.getLogger("astrbot-test")
            module.AstrBotConfig = dict


sys.meta_path.insert(0, _Finder())
