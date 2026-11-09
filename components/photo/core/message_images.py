"""Current/replied image extraction adapted from astrbot_plugin_gitee_aiimg.

Upstream authors: 木有知、Zhalslar
Source: https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg
The platform compatibility and extraction helpers below retain core/utils.py's
implementation. Avatar collection and image downloading are deliberately omitted.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from astrbot.api import logger
from astrbot.core.message.components import Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .provider import PhotoError

try:
    from astrbot.core.utils.quoted_message_parser import (
        extract_quoted_message_images as _astrbot_extract_quoted_message_images,
    )
except Exception:
    _astrbot_extract_quoted_message_images = None


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(obj, attr)
    except Exception as e:
        logger.debug(
            "[get_images] 读取属性失败: %s.%s err=%s",
            type(obj).__name__,
            attr,
            e,
        )
        return default


def _get_event_chain(event: AstrMessageEvent) -> list[Any]:
    try:
        chain = event.get_messages()
        if isinstance(chain, list):
            return chain
    except Exception:
        pass

    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None)
    return chain if isinstance(chain, list) else []


def _append_unique_string(target: list[str], value: Any) -> None:
    s = str(value or "").strip()
    if s and s not in target:
        target.append(s)


def _normalize_image_ref(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("data:image/"):
        _, _, payload = s.partition(",")
        return f"base64://{payload}" if payload else ""
    if s.startswith(("http://", "https://", "file:///", "base64://")):
        return s
    if os.path.exists(s):
        return os.path.abspath(s)
    return ""


def _file_uri_path(value: str) -> str:
    return url2pathname(urlsplit(value).path)


def _collect_image_refs_from_mapping(mapping: dict[str, Any], refs: list[str]) -> None:
    for key in ("url", "file", "path", "image", "file_id", "id"):
        _append_unique_string(refs, mapping.get(key))


def _extract_images_from_structure(
    value: Any,
    image_segs: list[Image],
    image_refs: list[str],
    *,
    seen: set[int] | None = None,
    skip_replies: bool = False,
) -> None:
    if value is None or (skip_replies and isinstance(value, Reply)):
        return

    if seen is None:
        seen = set()

    try:
        marker = id(value)
    except Exception:
        marker = None
    if marker is not None:
        if marker in seen:
            return
        seen.add(marker)

    if isinstance(value, str):
        # OneBot may expose raw_message as CQ text or a JSON-encoded segment list.
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if decoded is not None and decoded is not value:
                _extract_images_from_structure(
                    decoded,
                    image_segs,
                    image_refs,
                    seen=seen,
                    skip_replies=skip_replies,
                )
                return
        for match in re.finditer(r"\[CQ:image,([^\]]+)\]", text, re.IGNORECASE):
            params: dict[str, str] = {}
            for part in match.group(1).split(","):
                key, separator, raw_value = part.partition("=")
                if separator and key.strip():
                    params[key.strip().lower()] = unquote(raw_value.strip())
            _collect_image_refs_from_mapping(params, image_refs)
        return

    if isinstance(value, Image):
        image_segs.append(value)
        return

    if isinstance(value, dict):
        seg_type = str(value.get("type") or "").strip().lower()
        if skip_replies and seg_type == "reply":
            return
        data = value.get("data")
        if seg_type == "image":
            if isinstance(data, dict):
                _collect_image_refs_from_mapping(data, image_refs)
            else:
                _collect_image_refs_from_mapping(value, image_refs)
            return

        for key in ("message", "messages", "chain", "content", "elements", "segments"):
            if key in value:
                _extract_images_from_structure(
                    value.get(key),
                    image_segs,
                    image_refs,
                    seen=seen,
                    skip_replies=skip_replies,
                )
        return

    if isinstance(value, (list, tuple, set)):
        for item in value:
            _extract_images_from_structure(
                item, image_segs, image_refs, seen=seen, skip_replies=skip_replies
            )
        return

    for attr in ("chain", "message", "messages", "content", "elements", "segments"):
        nested = _safe_getattr(value, attr, None)
        if nested is not None and nested is not value:
            _extract_images_from_structure(
                nested, image_segs, image_refs, seen=seen, skip_replies=skip_replies
            )


def _resolve_call_action_candidates(
    event: AstrMessageEvent,
) -> list[Callable[..., Awaitable[Any]]]:
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    funcs: list[Callable[..., Awaitable[Any]]] = []
    seen: set[tuple[int, int]] = set()

    for owner in (api, bot):
        func = getattr(owner, "call_action", None)
        if not callable(func):
            continue
        marker = (
            id(getattr(func, "__self__", None)),
            id(getattr(func, "__func__", func)),
        )
        if marker in seen:
            continue
        seen.add(marker)
        funcs.append(func)

    return funcs


def _looks_like_call_action_signature_error(exc: Exception) -> bool:
    if not isinstance(exc, TypeError):
        return False
    msg = str(exc)
    markers = (
        "positional argument",
        "unexpected keyword argument",
        "missing 1 required positional argument",
        "got multiple values for argument",
    )
    return any(marker in msg for marker in markers)


async def _invoke_call_action(
    call_action: Callable[..., Awaitable[Any]],
    action: str,
    params: dict[str, Any],
) -> Any:
    try:
        return await call_action(action, **params)
    except Exception as e:
        if not _looks_like_call_action_signature_error(e):
            raise
    return await call_action(action=action, **params)


async def _call_action_compat(
    event: AstrMessageEvent,
    action: str,
    params_list: list[dict[str, Any]],
) -> Any:
    funcs = _resolve_call_action_candidates(event)
    if not funcs:
        return None

    for params in params_list:
        for func in funcs:
            try:
                result = await _invoke_call_action(func, action, params)
                if isinstance(result, dict):
                    return result
            except Exception as e:
                logger.debug(
                    "[get_images] call_action failed: action=%s params=%s err=%s",
                    action,
                    {k: str(v)[:80] for k, v in params.items()},
                    e,
                )
    return None


def _unwrap_action_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _build_reply_lookup_params(message_id: Any) -> list[dict[str, Any]]:
    message_id_str = str(message_id or "").strip()
    if not message_id_str:
        return []
    params_list: list[dict[str, Any]] = [
        {"message_id": message_id_str},
        {"id": message_id_str},
    ]
    if message_id_str.isdigit():
        numeric_id = int(message_id_str)
        params_list.extend([{"message_id": numeric_id}, {"id": numeric_id}])
    return params_list


def _build_image_resolve_actions(
    event: AstrMessageEvent,
    image_ref: str,
) -> list[tuple[str, dict[str, Any]]]:
    candidates: list[str] = []
    cleaned = str(image_ref or "").strip()
    if cleaned:
        candidates.append(cleaned)
        stem, ext = os.path.splitext(cleaned)
        if ext and stem and stem not in candidates:
            candidates.append(stem)

    actions: list[tuple[str, dict[str, Any]]] = []
    for candidate in candidates:
        actions.extend(
            [
                ("get_image", {"file": candidate}),
                ("get_image", {"file_id": candidate}),
                ("get_image", {"id": candidate}),
                ("get_image", {"image": candidate}),
                ("get_file", {"file_id": candidate}),
                ("get_file", {"file": candidate}),
            ]
        )

    try:
        group_id = event.get_group_id()
    except Exception:
        group_id = None
    if isinstance(group_id, str) and group_id.isdigit():
        group_id = int(group_id)
    if group_id:
        for candidate in candidates:
            actions.append(
                ("get_group_file_url", {"group_id": group_id, "file_id": candidate})
            )
    for candidate in candidates:
        actions.append(("get_private_file_url", {"file_id": candidate}))
    return actions


async def _resolve_image_ref(event: AstrMessageEvent, image_ref: str) -> str:
    normalized = _normalize_image_ref(image_ref)
    if normalized:
        return normalized

    for action, params in _build_image_resolve_actions(event, image_ref):
        payload = await _call_action_compat(event, action, [params])
        data = _unwrap_action_data(payload)
        for key in ("url", "file", "path"):
            normalized = _normalize_image_ref(data.get(key))
            if normalized:
                return normalized
    return ""


def _image_identity(seg: Image) -> str:
    direct = _normalize_image_ref(
        getattr(seg, "url", None)
        or getattr(seg, "file", None)
        or getattr(seg, "path", None)
    )
    if direct:
        return direct
    return f"obj:{id(seg)}"


def _append_unique_images(target: list[Image], extras: list[Image]) -> None:
    seen = {_image_identity(seg) for seg in target}
    for seg in extras:
        ident = _image_identity(seg)
        if ident in seen:
            continue
        seen.add(ident)
        target.append(seg)


async def _build_images_from_refs(
    event: AstrMessageEvent,
    refs: list[str],
) -> list[Image]:
    images: list[Image] = []
    for ref in refs:
        resolved = await _resolve_image_ref(event, ref)
        if not resolved:
            continue
        if resolved.startswith(("http://", "https://")):
            images.append(Image.fromURL(resolved))
        elif resolved.startswith("base64://"):
            images.append(Image.fromBase64(resolved.removeprefix("base64://")))
        elif resolved.startswith("file:///"):
            images.append(Image(file=resolved, path=_file_uri_path(resolved)))
        else:
            images.append(Image.fromFileSystem(resolved))
    return images


async def get_raw_images_from_event(
    event: AstrMessageEvent, *, skip_replies: bool = False
) -> list[Image]:
    """Recover image references from the untouched platform payload.

    Some plugins replace an incoming OneBot image component with a temporary
    local path.  That path can be gone by the time a later plugin handles the
    same event, while ``message_obj.raw_message`` still contains the original
    URL or file id that OneBot can resolve.
    """
    raw_message = _safe_getattr(
        _safe_getattr(event, "message_obj", None),
        "raw_message",
        None,
    )
    if raw_message is None:
        raw_message = _safe_getattr(event, "raw_message", None)
    image_segs: list[Image] = []
    image_refs: list[str] = []
    if raw_message is not None:
        _extract_images_from_structure(
            raw_message, image_segs, image_refs, skip_replies=skip_replies
        )
    if image_refs:
        _append_unique_images(
            image_segs,
            await _build_images_from_refs(event, image_refs),
        )

    # Context-aware may replace the current segment with a temporary path and
    # leave no raw CQ payload on the event.  Fetching the current OneBot message
    # gives us the original file id/URL before the temp file is cleaned up.
    def _segment_has_usable_image(seg: Image) -> bool:
        ref = _normalize_image_ref(
            getattr(seg, "url", None)
            or getattr(seg, "file", None)
            or getattr(seg, "path", None)
        )
        if not ref:
            return False
        if ref.startswith("file:///"):
            return os.path.exists(_file_uri_path(ref))
        return True

    has_usable_image = any(_segment_has_usable_image(seg) for seg in image_segs)
    if not has_usable_image:
        message_ids: list[str] = []
        message_obj = _safe_getattr(event, "message_obj", None)
        for owner in (message_obj, event):
            getter = _safe_getattr(owner, "get_message_id", None)
            if callable(getter):
                try:
                    _append_unique_string(message_ids, getter())
                except Exception:
                    pass
            for attr in ("message_id", "messageId", "id"):
                _append_unique_string(message_ids, _safe_getattr(owner, attr, None))

        for message_id in message_ids:
            params_list = _build_reply_lookup_params(message_id)
            if not params_list:
                continue
            payload = await _call_action_compat(event, "get_msg", params_list)
            data = _unwrap_action_data(payload)
            fetched_refs: list[str] = []
            _extract_images_from_structure(
                data, image_segs, fetched_refs, skip_replies=skip_replies
            )
            if fetched_refs:
                _append_unique_images(
                    image_segs,
                    await _build_images_from_refs(event, fetched_refs),
                )
            if fetched_refs or image_segs:
                break
    return image_segs


async def _extract_reply_images(
    event: AstrMessageEvent,
    reply_seg: Reply,
) -> list[Image]:
    image_segs: list[Image] = []
    image_refs: list[str] = []

    embedded_chain = _safe_getattr(reply_seg, "chain", None)
    _extract_images_from_structure(embedded_chain, image_segs, image_refs)
    if image_refs:
        _append_unique_images(
            image_segs,
            await _build_images_from_refs(event, image_refs),
        )
    if image_segs:
        return image_segs

    if _astrbot_extract_quoted_message_images is not None:
        try:
            refs = await _astrbot_extract_quoted_message_images(
                event, reply_component=reply_seg
            )
            _append_unique_images(
                image_segs, await _build_images_from_refs(event, refs)
            )
        except Exception as e:
            logger.debug(
                "[get_images] astrbot quoted_message_parser failed: reply_id=%s err=%s",
                _safe_getattr(reply_seg, "id", ""),
                e,
            )
    if image_segs:
        return image_segs

    reply_id = _safe_getattr(reply_seg, "id", "")
    params_list = _build_reply_lookup_params(reply_id)
    if not params_list:
        return image_segs

    try:
        payload = await _call_action_compat(event, "get_msg", params_list)
    except Exception as e:
        logger.warning(
            "[get_images] failed to fetch replied message id=%s: %s",
            reply_id,
            e,
        )
        return image_segs

    data = _unwrap_action_data(payload)
    _extract_images_from_structure(data, image_segs, image_refs)
    if image_refs:
        _append_unique_images(
            image_segs,
            await _build_images_from_refs(event, image_refs),
        )
    return image_segs


async def collect_images(
    event: AstrMessageEvent,
    *,
    prefer_current: bool = False,
    current_only: bool = False,
) -> list[Image]:
    """Collect images in source order, optionally restricting them to direct uploads."""
    chain = _get_event_chain(event)
    skip_replies = prefer_current or current_only
    current = [segment for segment in chain if isinstance(segment, Image)]
    has_raw_images = False
    if not current:
        raw = _safe_getattr(
            _safe_getattr(event, "message_obj", None), "raw_message", None
        )
        if raw is None:
            raw = _safe_getattr(event, "raw_message", None)
        raw_images: list[Image] = []
        raw_refs: list[str] = []
        _extract_images_from_structure(
            raw, raw_images, raw_refs, skip_replies=skip_replies
        )
        has_raw_images = bool(raw_images or raw_refs)

    has_current = bool(current or has_raw_images)
    if current_only and not has_current:
        return []
    only_current = skip_replies and has_current
    images: list[Image] = []
    if not only_current:
        for segment in chain:
            if isinstance(segment, Reply):
                _append_unique_images(
                    images, await _extract_reply_images(event, segment)
                )
    _append_unique_images(images, current)
    needs_raw = not images or has_raw_images
    for segment in current:
        source = _normalize_image_ref(segment.url or segment.file or segment.path)
        if not source or (
            source.startswith("file:///") and not os.path.exists(_file_uri_path(source))
        ):
            needs_raw = True
            break
    if needs_raw:
        _append_unique_images(
            images, await get_raw_images_from_event(event, skip_replies=only_current)
        )

    resolved_images: list[Image] = []
    for segment in images:
        source = segment.url or segment.file or segment.path
        if not source:
            continue
        if source.startswith("file:///") and not os.path.exists(_file_uri_path(source)):
            continue
        if os.path.isabs(source) and not os.path.exists(source):
            continue
        _append_unique_images(
            resolved_images, await _build_images_from_refs(event, [source])
        )
    if only_current and not resolved_images:
        raise PhotoError("参考图片读取失败，请重新上传或发送。")
    return resolved_images
