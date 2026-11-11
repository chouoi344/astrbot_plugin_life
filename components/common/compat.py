"""AstrBot version checks for features that rely on AstrBot internals."""

from __future__ import annotations

from astrbot.api import logger

_logged: set[str] = set()


def astrbot_version():
    try:
        from astrbot import __version__
        from packaging.version import Version

        return Version(__version__)
    except Exception:
        return None


def version_between(low: str, high: str) -> bool:
    """True when AstrBot's version is in ``[low, high)``; unknown counts as False."""
    from packaging.version import Version

    version = astrbot_version()
    return version is not None and Version(low) <= version < Version(high)


def disabled_once(feature: str, reason: str) -> None:
    """Log once that one optional feature is switched off, without stopping the plugin."""
    if feature not in _logged:
        _logged.add(feature)
        logger.warning("[拟人生活] %s已关闭：%s", feature, reason)
