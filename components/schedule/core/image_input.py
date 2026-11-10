"""Bounded SDK image handling, adapted from 木有知's MIT life_scheduler."""

import asyncio
import base64
from collections import deque
from pathlib import Path

from astrbot.api import logger

from .errors import describe_error, run_with_timeout

_logged_legacy = False


async def _bounded(awaitable):
    return await run_with_timeout(awaitable, 10, "读取图片消息")


async def resolve_image_path(component) -> str:
    # SDK converters may decode base64 and write files before their first await.
    # Their HTTP sessions are created inside this worker's loop; no bot/client
    # objects from the main loop are passed into it.
    def convert():
        return asyncio.run(_bounded(component.convert_to_file_path()))

    return await run_with_timeout(asyncio.to_thread(convert), 15, "转换图片")


async def _iter_quoted_image_refs(extractor, reply):
    """Fetch the next forward only after the previous images were consumed."""
    for ref in extractor._reply_parser.extract_image_refs_from_reply_component(reply):
        yield ref
    reply_id = str(getattr(reply, "id", "") or "").strip()
    if not reply_id:
        return
    payload = await _bounded(extractor._client.get_msg(reply_id))
    if not payload:
        return
    parsed = await asyncio.to_thread(extractor._payload_parser.parse_get_msg_payload, payload)
    for ref in parsed["image_refs"]:
        yield ref

    pending = deque(parsed["forward_ids"])
    seen = set()
    while pending and len(seen) < extractor._settings.max_forward_fetch:
        forward_id = str(pending.popleft() or "").strip()
        if not forward_id or forward_id in seen:
            continue
        seen.add(forward_id)
        payload = await _bounded(extractor._client.get_forward_msg(forward_id))
        if not payload:
            continue
        parsed = await asyncio.to_thread(extractor._payload_parser.parse_get_forward_payload, payload)
        for ref in parsed["image_refs"]:
            yield ref
        pending.extend(parsed["forward_ids"])


def _legacy_message_parts(payload):
    """Extract OneBot image and forward references from old payload shapes."""
    image_refs = []
    forward_ids = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        segment_type = value.get("type")
        data = value.get("data")
        if segment_type == "image":
            image_data = data if isinstance(data, dict) else value
            for key in ("file", "url", "path"):
                ref = image_data.get(key)
                if isinstance(ref, str) and ref.strip():
                    image_refs.append(ref)
                    break
            return
        if segment_type == "forward":
            forward_data = data if isinstance(data, dict) else value
            forward_id = forward_data.get("id") or forward_data.get("res_id")
            if forward_id is not None:
                forward_ids.append(str(forward_id))
            return

        for key in ("data", "message", "messages", "content", "chain"):
            if key in value:
                visit(value[key])

    visit(payload)
    return image_refs, forward_ids


def _legacy_payload_data(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


async def _legacy_call_action(event, action, **params):
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        call_action = getattr(getattr(bot, "api", None), "call_action", None)
    if not callable(call_action):
        raise TypeError("legacy bot API does not expose call_action")
    try:
        return await _bounded(call_action(action=action, **params))
    except TypeError:
        return await _bounded(call_action(action, params))


async def _legacy_image_ref(event, ref):
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://", "base64://", "file://", "data:")):
        return ref
    if ref.startswith("/"):
        return ref

    payload = await _legacy_call_action(event, "get_image", file=ref)
    data = _legacy_payload_data(payload)
    if isinstance(data, dict):
        for key in ("url", "file", "path"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


async def _iter_legacy_quoted_image_refs(event, reply):
    reply_id = str(getattr(reply, "id", "") or "").strip()
    if not reply_id:
        return
    payload = await _legacy_call_action(event, "get_msg", message_id=reply_id)
    if not payload:
        return
    image_refs, forward_ids = _legacy_message_parts(_legacy_payload_data(payload))
    for ref in image_refs:
        yield ref

    pending = deque(forward_ids)
    seen = set()
    while pending and len(seen) < 32:
        forward_id = str(pending.popleft() or "").strip()
        if not forward_id or forward_id in seen:
            continue
        seen.add(forward_id)
        payload = await _legacy_call_action(event, "get_forward_msg", id=forward_id)
        if not payload:
            continue
        image_refs, nested_ids = _legacy_message_parts(_legacy_payload_data(payload))
        for ref in image_refs:
            yield ref
        pending.extend(nested_ids)


async def iter_quoted_image_paths(event, reply):
    """Fetch raw quoted references, resolving each only when requested."""
    from astrbot.api.message_components import Image

    try:
        from astrbot.core.utils.quoted_message.extractor import QuotedMessageExtractor
        from astrbot.core.utils.quoted_message.image_refs import (
            get_existing_local_path,
            normalize_image_ref,
        )
        from astrbot.core.utils.quoted_message.image_resolver import (
            _build_image_resolve_actions,
        )
    except ImportError:
        global _logged_legacy
        if not _logged_legacy:
            logger.info("当前 AstrBot 缺少新版引用图片解析器，使用 OneBot 兼容方式")
            _logged_legacy = True
        seen = set()
        async for ref in _iter_legacy_quoted_image_refs(event, reply):
            ref = str(ref or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            try:
                normalized = await _legacy_image_ref(event, ref)
                if normalized:
                    yield await resolve_image_path(Image(file=normalized))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to resolve legacy quoted image: %s", describe_error(exc))
        return

    extractor = QuotedMessageExtractor(event)
    # Both images() and _fetch_quoted_content() traverse every forward eagerly.
    # Keep traversal lazy so the caller's four-image limit stops network I/O too.
    seen = set()
    async for ref in _iter_quoted_image_refs(extractor, reply):
        ref = str(ref or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        try:
            normalized = await asyncio.to_thread(normalize_image_ref, ref)
            if not normalized:
                if await asyncio.to_thread(get_existing_local_path, ref):
                    continue
                for action, params in _build_image_resolve_actions(event, ref):
                    # OneBot calls stay on the bot's original event loop.
                    data = await _bounded(extractor._client.call(action, params))
                    if not isinstance(data, dict):
                        continue
                    for key in ("url", "file"):
                        value = data.get(key)
                        if isinstance(value, str):
                            normalized = await asyncio.to_thread(normalize_image_ref, value)
                            if normalized:
                                break
                    if normalized:
                        break
            if normalized:
                yield await resolve_image_path(Image(file=normalized))
        # SDK converters also raise generic Exception for unavailable attachments.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve quoted image: %s", describe_error(exc))


async def prepare_provider_images(image_refs: list[str]) -> list[str]:
    """Let modern SDKs read paths; preencode for legacy synchronous providers."""
    from importlib import import_module

    from astrbot.api.message_components import Image

    # Modern ProviderRequest uses MediaResolver, which reads files in a worker.
    # Passing base64:// here would force an extra decode/write/read/encode cycle.
    entities = import_module("astrbot.core.provider.entities")
    if getattr(entities, "MediaResolver", None) is not None:
        return image_refs[:4]

    def encode(path):
        return "base64://" + base64.b64encode(Path(path).read_bytes()).decode("ascii")

    prepared = []
    for ref in image_refs[:4]:
        if ref.startswith("base64://"):
            prepared.append(ref)
            continue
        path = await resolve_image_path(Image(file=ref))
        prepared.append(await asyncio.to_thread(encode, path))
    return prepared


async def collect_image_paths(event, max_images=4) -> list[str]:
    """Resolve direct, embedded, quoted and forwarded images lazily (at most four)."""
    from astrbot.api.message_components import Image, Reply

    limit = min(4, max(0, int(max_images)))
    paths, seen = [], set()
    seen_forwards = set()

    def add(path):
        value = str(path or "").strip()
        if value and value not in seen:
            seen.add(value)
            paths.append(value)

    async def forward(forward_id):
        pending = deque([forward_id])
        while pending and len(paths) < limit and len(seen_forwards) < 32:
            item = str(pending.popleft() or "").strip()
            if not item or item in seen_forwards:
                continue
            seen_forwards.add(item)
            payload = await _legacy_call_action(event, "get_forward_msg", id=item)
            refs, nested = _legacy_message_parts(_legacy_payload_data(payload))
            for ref in refs:
                if len(paths) >= limit:
                    break
                normalized = await _legacy_image_ref(event, ref)
                if normalized:
                    add(await resolve_image_path(Image(file=normalized)))
            pending.extend(nested)

    async def collect():
        components = deque(getattr(getattr(event, "message_obj", None), "message", []) or [])
        visited = 0
        while components and len(paths) < limit and visited < 512:
            component = components.popleft()
            visited += 1
            try:
                if isinstance(component, Image):
                    add(await resolve_image_path(component))
                elif isinstance(component, Reply):
                    embedded = False
                    for child in getattr(component, "chain", None) or []:
                        if len(paths) >= limit:
                            break
                        if isinstance(child, Image):
                            embedded = True
                            try:
                                add(await resolve_image_path(child))
                            except Exception as exc:
                                logger.warning("引用图片读取失败：%s", describe_error(exc))
                    if not embedded:
                        async for path in iter_quoted_image_paths(event, component):
                            add(path)
                            if len(paths) >= limit:
                                break
                elif type(component).__name__ in {"Forward", "Nodes", "Node"}:
                    nested = getattr(component, "content", None) or getattr(component, "nodes", None)
                    if isinstance(nested, list):
                        components.extendleft(reversed(nested))
                    else:
                        identifier = getattr(component, "id", None)
                        if identifier:
                            await forward(identifier)
            except Exception as exc:
                logger.warning("图片读取失败：%s", describe_error(exc))

    try:
        await run_with_timeout(collect(), 30, "收集图片")
    except asyncio.TimeoutError:
        logger.warning("图片收集超过 30 秒，使用已取得的图片")
    return paths
