"""Follow AstrBot's own proxy setting for outgoing HTTP."""

from __future__ import annotations

import fnmatch
import os
from urllib.parse import urlsplit


def astrbot_proxy(context=None, url: str = "") -> str | None:
    """Return the proxy AstrBot is configured with, or ``None`` for a direct connection.

    AstrBot copies ``http_proxy`` from its settings into the process
    environment at start-up; the setting itself is read first, at call time.
    Photo and voice call this once when their client is built, so a proxy
    changed later applies after the plugin reloads (saving this plugin's
    settings reloads it). ``no_proxy`` patterns are respected.
    """
    proxy = ""
    no_proxy: list[str] = []
    if context is not None:
        try:
            config = context.get_config()
            proxy = str(config.get("http_proxy") or "").strip()
            no_proxy = [str(item) for item in config.get("no_proxy") or []]
        except Exception:
            proxy = ""
    if not proxy:
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip()
        no_proxy = no_proxy or [
            item.strip() for item in (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").split(",")
        ]
    if not proxy:
        return None
    host = (urlsplit(url).hostname or "") if url else ""
    if host and any(pattern and (fnmatch.fnmatch(host, pattern) or host == pattern) for pattern in no_proxy):
        return None
    return proxy


def aiohttp_proxy(context=None, url: str = "") -> str | None:
    """Like :func:`astrbot_proxy`, but only HTTP(S) proxies, which is all aiohttp supports.

    A SOCKS proxy is skipped (direct connection) and logged once.
    """
    proxy = astrbot_proxy(context, url)
    if proxy and urlsplit(proxy).scheme.lower() not in ("http", "https"):
        from .compat import disabled_once

        disabled_once("语音走代理", "AstrBot 配置的是 SOCKS 代理，语音请求不支持，改为直连")
        return None
    return proxy
