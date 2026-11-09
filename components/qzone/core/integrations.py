"""Version-aware bridges to the user's existing character plugins.

Only the live LivingMemory engine writes memories. Its SQLite connection is
used for parameterized, read-only reconciliation of our own external keys.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....runtime import PHOTO_ID, QZONE_ID, SCHEDULE_ID, component_enabled, get_component
from ...schedule.core.engine import UNRECORDED_ACTIVITY
from ...schedule.core.timeline import format_timeline, parse_local, project_slot_rest


class IntegrationError(RuntimeError):
    """An integration could not finish; keep the durable job for inspection/retry."""


class DependencyUnavailable(IntegrationError):
    pass


class IntegrationIncompatible(IntegrationError):
    pass


class AccessDenied(IntegrationError):
    pass


class BindingMismatch(IntegrationError):
    pass


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def space_session_enabled(context, umo, event=None) -> bool:
    from astrbot.api import sp

    umo = str(umo)
    services = await sp.get_async(
        scope="umo", scope_id=umo, key="session_service_config", default={}
    )
    if not isinstance(services, dict):
        raise IntegrationIncompatible("AstrBot 会话设置格式不兼容")
    if services.get("session_enabled") is not None and not services["session_enabled"]:
        return False
    return await component_enabled(context, QZONE_ID, umo, event)


def schedule_peer(context):
    peer = get_component(context, SCHEDULE_ID)
    if peer is None or peer.engine is None or peer.engine.closed or not peer.config["enabled"]:
        raise DependencyUnavailable("拟人生活日程尚未就绪或已停用")
    return peer


def schedule_generating(engine):
    return engine.generation_lock.locked() or (
        engine.generation_task is not None and not engine.generation_task.done()
    )


def find_plugin(context: Any, plugin_id: str) -> Any:
    """Resolve the current instance each time, including after plugin reloads."""
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        raise IntegrationIncompatible("当前 AstrBot 缺少插件实例查询接口")
    metadata = getter(plugin_id)
    if metadata is None or not getattr(metadata, "activated", True):
        raise DependencyUnavailable(f"{plugin_id} 尚未加载或已停用")
    peer = getattr(metadata, "star_cls", None)
    if peer is None or getattr(peer, "_terminating", False):
        raise DependencyUnavailable(f"{plugin_id} 尚未就绪或正在卸载")
    return peer


def _peer_module(peer: Any, suffix: str) -> Any:
    module = type(peer).__module__
    package, separator, _ = module.rpartition(".")
    if not separator:
        raise IntegrationIncompatible("无法确定依赖插件的实际加载包")
    try:
        return importlib.import_module(f"{package}.{suffix}")
    except ImportError as exc:
        raise IntegrationIncompatible(f"依赖插件缺少兼容接口：{suffix}") from exc


def _value(event: Any, name: str) -> str:
    method = getattr(event, name, None)
    return str(method() or "").strip() if callable(method) else ""


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("metadata", {})
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise IntegrationError("LivingMemory 返回的记忆元数据不是对象")
    return value


def _time_label(value: Any) -> str:
    if isinstance(value, (int, float)):
        # Explicit timezone rather than silently interpreting a server timezone.
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value or "时间未提供")


def _prompt(persona: Any) -> str:
    if isinstance(persona, dict):
        return str(persona.get("prompt") or persona.get("system_prompt") or "").strip()
    return str(getattr(persona, "system_prompt", "") or getattr(persona, "prompt", "")).strip()


class Integrations:
    def __init__(self, context: Any, data_dir: Path | None = None):
        self.context = context
        self.data_dir = data_dir
        self._memory_lock = asyncio.Lock()

    async def _memory_persona_id(self, event: Any, storage: Any) -> str | None:
        """Read LivingMemory's selection order without its per-call INFO log.

        Use the same public AstrBot session/conversation/default APIs as the
        supported LivingMemory bridge. Do not suppress its logger globally or
        cache the choice: switching personas must affect the very next check.
        Missing/unreadable APIs stop the caller rather than inventing a default.
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            raise BindingMismatch("缺少角色绑定会话")

        def selected(value):
            if value is not None and not isinstance(value, str):
                raise IntegrationIncompatible("AstrBot 人格标识格式不兼容")
            return None if value in (None, "", "[%None]") else value

        getter = getattr(storage, "get_async", None)
        if not callable(getter):
            raise IntegrationIncompatible("AstrBot 缺少会话人格读取接口")
        services = await _await(getter(
            scope="umo", scope_id=umo, key="session_service_config", default={}
        ))
        if not isinstance(services, dict):
            raise IntegrationIncompatible("AstrBot 会话设置格式不兼容")
        session_persona = services.get("persona_id")
        if session_persona:
            return selected(session_persona)

        conversations = getattr(self.context, "conversation_manager", None)
        current = getattr(conversations, "get_curr_conversation_id", None)
        read = getattr(conversations, "get_conversation", None)
        if not callable(current) or not callable(read):
            raise IntegrationIncompatible("AstrBot 缺少当前会话人格读取接口")
        cid = await _await(current(umo))
        if cid is not None:
            conversation = await _await(read(umo, cid))
            if conversation is not None and not hasattr(conversation, "persona_id"):
                raise IntegrationIncompatible("AstrBot 会话人格格式不兼容")
            conversation_persona = getattr(conversation, "persona_id", None)
            if conversation_persona:
                return selected(conversation_persona)

        manager = getattr(self.context, "persona_manager", None)
        default = getattr(manager, "get_default_persona_v3", None)
        if not callable(default):
            raise IntegrationIncompatible("AstrBot 缺少默认人格读取接口")
        persona = await _await(default(umo=umo))
        if persona is None or persona == {}:
            return None
        if not isinstance(persona, dict) or "name" not in persona:
            raise IntegrationIncompatible("AstrBot 默认人格格式不兼容")
        return selected(persona["name"])

    async def persona(self, event: Any) -> tuple[str | None, str]:
        """Resolve the real selected character, never a made-up fallback prompt."""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            raise BindingMismatch("缺少角色绑定会话")
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            raise DependencyUnavailable("AstrBot 人设管理器尚未就绪")
        resolver = getattr(manager, "resolve_selected_persona", None)
        if callable(resolver):
            conversation_persona_id = None
            conversations = self.context.conversation_manager
            cid = await conversations.get_curr_conversation_id(umo)
            if cid:
                conversation = await conversations.get_conversation(umo, cid)
                conversation_persona_id = getattr(conversation, "persona_id", None)
            config = self.context.get_config(umo)
            selected = await _await(
                resolver(
                    umo=umo,
                    conversation_persona_id=conversation_persona_id,
                    platform_name=_value(event, "get_platform_name"),
                    provider_settings=config.get("provider_settings", {}),
                )
            )
            if not isinstance(selected, (tuple, list)) or len(selected) < 2:
                raise IntegrationIncompatible("AstrBot 人设解析结果不兼容")
            persona_id, persona = selected[:2]
        else:
            peer = self._memory_peer()
            utils = _peer_module(peer, "core.utils")
            persona_id = await self._memory_persona_id(event, utils.sp)
            persona = None
        if persona_id == "[%None]":
            persona_id = None
        if persona_id and not persona:
            getter = getattr(manager, "get_persona", None)
            if not callable(getter):
                getter = getattr(manager, "get_persona_v3_by_id", None)
            if not callable(getter):
                raise IntegrationIncompatible("AstrBot 缺少人设读取接口")
            persona = await _await(getter(persona_id))
        return (str(persona_id) if persona_id else None), _prompt(persona)

    async def life_context(self) -> dict[str, Any]:
        """Project only the current, persisted Natural Life business day."""
        peer = schedule_peer(self.context)
        engine = peer.engine
        start, end = engine.window()
        day = engine.store.day(start.date().isoformat())
        if not day or day["date"] != start.date().isoformat() or day["status"] != "ok":
            return {}
        state = peer.get_current_state()
        if not state or state["persona_id"] != engine.setup.selected_id:
            return {}
        current = state["current_slot"]
        if not current or not current["outfit"]:
            return {}
        timeline = []
        recorded_at = parse_local(day["generation_started_at"], engine.tz) if day.get("generation_started_at") else start
        for slot in day["actual"]:
            a, b = parse_local(slot["start"], engine.tz), parse_local(slot["end"], engine.tz)
            if a < end and b > start:
                item = project_slot_rest(slot)
                timeline.append({
                    "start": max(a, start).isoformat(),
                    "end": min(b, end).isoformat(),
                    "activity": (
                        item["activity"] if item["activity"] not in
                        {UNRECORDED_ACTIVITY, "等待更新后续安排"} else ""
                    ),
                    "location": item.get("location", ""),
                    "rest_type": item["rest_type"],
                    "phone": item.get("phone", ""),
                    "mood": item.get("mood", ""),
                    "mood_intensity": item.get("mood_intensity", ""),
                    "actual_start": slot["start"],
                    "actual_end": slot["end"],
                    "after_recorded": recorded_at < b,
                })
        if not timeline:
            return {}
        return {
            "schedule": format_timeline(timeline, outfit_label=lambda _: ""),
            "outfit": current["outfit"],
            "timeline": timeline,
            "meta": {
                "qzone_schedule_day": start.date().isoformat(),
                "qzone_valid_until": end.timestamp(),
                "qzone_generation_in_progress": schedule_generating(engine),
                "qzone_schedule_persona": state["persona_id"],
            },
        }

    async def check_life_persona(self, binding: dict[str, Any]) -> None:
        if not await component_enabled(self.context, SCHEDULE_ID, binding["umo"]):
            raise DependencyUnavailable("拟人生活日程在绑定会话中尚未就绪或已停用")
        engine = schedule_peer(self.context).engine
        if (
            not engine.life_ready()
            or engine.setup.selected_id != binding["persona_id"]
            or engine.setup.state["persona_id"] != binding["persona_id"]
        ):
            raise BindingMismatch("拟人生活日程与空间绑定角色尚未一致，自主发布等待恢复")

    async def comment_slot(self, binding, now):
        """Read the present, saved free slot without preparing a schedule."""
        try:
            await self.check_life_persona(binding)
            engine = schedule_peer(self.context).engine
        except (DependencyUnavailable, BindingMismatch):
            return None
        business_start, business_end = engine.window()
        day = engine.store.day(business_start.date().isoformat())
        if not day or day["date"] != business_start.date().isoformat() or day["status"] != "ok":
            return None
        for raw in day["actual"]:
            start = parse_local(raw["start"], engine.tz)
            end = min(parse_local(raw["end"], engine.tz), business_end)
            if max(start, business_start).timestamp() <= now.timestamp() < end.timestamp():
                slot = project_slot_rest(raw)
                if (slot.get("phone") == "free" and slot["rest_type"] == "none"
                        and slot["activity"] not in ("", UNRECORDED_ACTIVITY, "等待更新后续安排")):
                    return start, end
                return None
        return None

    async def selfie(self, event: Any, prompt: str) -> tuple[Path, dict[str, Any]]:
        return await self.photo(event, prompt, photo_type="selfie")

    async def photo(self, event: Any, prompt: str, *, photo_type, activity=None) -> tuple[Path, dict[str, Any]]:
        umo = str(event.unified_msg_origin)
        peer = get_component(self.context, PHOTO_ID)
        enabled_key = "selfie_enabled" if photo_type == "selfie" else "generate_enabled"
        if (
            peer is None
            or not peer.config[enabled_key]
            or not await component_enabled(self.context, PHOTO_ID, umo, event)
        ):
            raise DependencyUnavailable("拟人生活对应照片功能尚未就绪或已停用")
        destination = self.data_dir / "images" / f"{photo_type}-{uuid.uuid4().hex}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if photo_type == "selfie" and activity is None:
            metadata = await peer.generate_qzone_selfie(
                event, prompt, destination=destination,
                before_generate=event.authorize_generation,
            )
        else:
            metadata = await peer.generate_qzone_photo(
                event, prompt, photo_type=photo_type, activity=activity,
                destination=destination, before_generate=event.authorize_generation,
            )
        path = Path(metadata["path"])
        if not path.is_file():
            raise IntegrationError("照片生成未返回可读取的图片文件")
        return path, metadata

    def _memory_peer(self) -> Any:
        peer = find_plugin(self.context, "astrbot_plugin_livingmemory")
        initializer = getattr(peer, "initializer", None)
        if (
            initializer is None
            or not getattr(initializer, "is_initialized", False)
            or getattr(initializer, "memory_engine", None) is None
            or getattr(initializer, "memory_processor", None) is None
        ):
            raise DependencyUnavailable("LivingMemory 正在初始化，记忆等待同步")
        return peer

    async def _check_session(self, event: Any, plugin_id: str, *, storage: Any = None) -> None:
        """Mirror AstrBot's controls; unreadable settings do not mean allowed."""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            raise BindingMismatch("缺少绑定会话，无法核对插件开关")
        if storage is None:
            try:
                storage = importlib.import_module("astrbot.api").sp
            except (ImportError, AttributeError) as exc:
                raise IntegrationIncompatible("无法读取 AstrBot 会话设置") from exc
        services = await storage.get_async(
            scope="umo", scope_id=umo, key="session_service_config", default={}
        )
        plugins = await storage.get_async(
            scope="umo", scope_id=umo, key="session_plugin_config", default={}
        )
        if not isinstance(services, dict) or not isinstance(plugins, dict):
            raise IntegrationIncompatible("AstrBot 会话设置格式不兼容")
        if services.get("session_enabled") is not None and not bool(services["session_enabled"]):
            raise AccessDenied("绑定会话已停用，后台操作等待恢复")
        entry = plugins.get(umo, {})
        if not isinstance(entry, dict):
            raise IntegrationIncompatible("AstrBot 会话插件设置格式不兼容")
        disabled = entry.get("disabled_plugins", [])
        if not isinstance(disabled, list):
            raise IntegrationIncompatible("AstrBot 禁用插件列表格式不兼容")
        aliases = {plugin_id}
        if plugin_id == "astrbot_plugin_livingmemory":
            aliases.add("LivingMemory")
        if aliases.intersection(str(item) for item in disabled):
            raise AccessDenied(f"{plugin_id} 在绑定会话中已停用")

    async def resolve_binding(
        self, event: Any, expected_persona_id: str | None = None
    ) -> dict[str, Any]:
        peer = self._memory_peer()
        return await self._resolve_binding(peer, event, expected_persona_id)

    async def _resolve_binding(
        self, peer: Any, event: Any, expected_persona_id: str | None = None
    ) -> dict[str, Any]:
        scope_module = _peer_module(peer, "core.memory_scope")
        utils = _peer_module(peer, "core.utils")
        await self._check_session(event, QZONE_ID, storage=utils.sp)
        await self._check_session(event, "astrbot_plugin_livingmemory", storage=utils.sp)
        if not scope_module.is_event_memory_allowed(peer.config_manager, event):
            raise AccessDenied("当前绑定身份不在 LivingMemory 允许范围内")
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        bot_id = _value(event, "get_self_id")
        admin_id = _value(event, "get_sender_id")
        if not all((umo, bot_id, admin_id)):
            raise BindingMismatch("记忆绑定缺少会话、机器人账号或管理员身份")
        persona_id = await self._memory_persona_id(event, utils.sp)
        if persona_id == "[%None]":
            persona_id = None
        if expected_persona_id is not None and str(persona_id or "") != str(expected_persona_id):
            raise BindingMismatch("绑定会话的人设已改变，记忆等待核对后同步")
        # Same write semantics as LivingMemory's own memorize tool. None in
        # legacy unfiltered mode does not mean inventing a global shared scope.
        scope = scope_module.resolve_memory_scope(peer.config_manager, event) or umo
        return {
            "umo": umo,
            "bot_id": bot_id,
            "admin_id": admin_id,
            "persona_id": persona_id,
            "scope": scope,
        }

    @staticmethod
    def _check_binding(actual: dict[str, Any], expected: dict[str, Any]) -> None:
        for key in ("umo", "bot_id", "admin_id", "persona_id", "scope"):
            if key in expected and str(expected[key] or "") != str(actual.get(key) or ""):
                raise BindingMismatch(f"记忆绑定的 {key} 已改变，保留待同步记录")

    @staticmethod
    async def _records(engine: Any, *, tid: str, binding: dict[str, Any]) -> list[dict[str, Any]]:
        connection = getattr(engine, "db_connection", None)
        if connection is None:
            raise DependencyUnavailable("LivingMemory 数据库连接尚未就绪")
        # No writes, migrations, or extra indexes are performed on its database.
        cursor = await connection.execute(
            "SELECT id, text, metadata FROM documents "
            "WHERE json_valid(metadata) "
            "AND json_extract(metadata, '$.memory_origin') = ? "
            "AND json_extract(metadata, '$.qzone_tid') = ? "
            "AND json_extract(metadata, '$.qzone_bot_id') = ? "
            "AND COALESCE(json_extract(metadata, '$.session_id'), '') = ? "
            "AND COALESCE(json_extract(metadata, '$.persona_id'), '') = ? "
            "ORDER BY id",
            (
                "qzone_life",
                tid,
                binding["bot_id"],
                str(binding["scope"] or ""),
                str(binding["persona_id"] or ""),
            ),
        )
        try:
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                await _await(close())

    @staticmethod
    def _event_record(records: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        matches = [row for row in records if _metadata(row).get("qzone_event_key") == key]
        if len(matches) > 1:
            raise IntegrationError("同一空间事件存在多条记忆，需先核对重复记录")
        return matches[0] if matches else None

    @staticmethod
    def _describe(payload: dict[str, Any], binding: dict[str, Any]) -> tuple[str, list[str]]:
        post = payload.get("post") or {}
        role = str(binding.get("persona_id") or "当前角色")
        tid = str(payload["tid"])
        when = _time_label(payload.get("occurred_at"))
        content = str(post.get("content") or "（无文字文案）")
        image_summary = str(post.get("image_summary") or "").strip()
        post_label = "既有 QQ空间说说" if post.get("source") == "external" else "QQ空间说说"
        kind = payload["kind"]
        if kind == "publish":
            source = str(post.get("source") or "")
            user_photo = source in {
                "quoted",
                "quote",
                "chat",
                "manual",
                "user",
                "user_photo",
                "quoted_photo",
            }
            if user_photo:
                origin = "使用管理员提供的照片"
            elif source in {"selfie", "auto", "autonomous", "life", "scheduled_selfie"}:
                origin = "按照自己的日程和生活场景制作了自拍"
            else:
                origin = "使用了本次发布所选的图片"
            facts = [
                f"{role}于{when}在自己的 QQ空间发布说说（ID：{tid}）。",
                f"实际发布的文案：{content}",
            ]
            if post.get("images") or image_summary:
                facts.append(origin + (f"；图片内容：{image_summary}" if image_summary else "。"))
            return "\n".join(facts), facts
        if kind == "delete":
            facts = [
                f"{role}此前发布的{post_label}（ID：{tid}）已于{when}从该QQ账号的空间删除，现已不存在。",
                f"这是删除前的历史文案：{content}",
            ]
            return "\n".join(facts), facts
        comment = payload.get("comment") or {}
        author = str(comment.get("author_name") or comment.get("author_qq") or "管理员")
        author_qq = str(comment.get("author_qq") or "")
        comment_text = str(comment.get("content") or "")
        comment_when = _time_label(comment.get("created_at") or payload.get("occurred_at"))
        facts = [
            f"{comment_when}，{author}（QQ：{author_qq}）在{role}自己的{post_label}（ID：{tid}）下留言：{comment_text}",
            f"当时说说文案：{content}",
        ]
        if kind == "reply":
            reply = payload.get("reply") or {}
            reply_when = _time_label(reply.get("created_at") or payload.get("occurred_at"))
            facts.append(f"{role}于{reply_when}实际回复这条评论：{str(reply.get('content') or '')}")
        return "\n".join(facts), facts

    @staticmethod
    def _deleted_description(summary: str, facts: list[str], when: Any) -> tuple[str, list[str]]:
        marker = f"该说说已于{_time_label(when)}删除；以下内容属于删除前的历史记录。"
        return marker + "\n" + summary, [marker, *["历史记录：" + fact for fact in facts]][:5]

    async def sync_memory(self, event: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Idempotently persist a confirmed external action; never publish anything."""
        kind = payload.get("kind")
        key = str(payload.get("event_key") or "").strip()
        tid = str(payload.get("tid") or "").strip()
        expected = payload.get("binding") or (payload.get("post") or {}).get("binding")
        if kind not in {"publish", "delete", "comment", "reply"} or not key or not tid:
            raise ValueError("空间记忆事件缺少有效类型、事件键或说说 ID")
        if not isinstance(expected, dict) or not expected.get("umo"):
            raise BindingMismatch("空间事件没有原始绑定信息")
        async with self._memory_lock:
            peer = self._memory_peer()
            binding = await self._resolve_binding(peer, event, expected.get("persona_id"))
            self._check_binding(binding, expected)
            engine = peer.initializer.memory_engine
            records = await self._records(engine, tid=tid, binding=binding)
            existing = self._event_record(records, key)
            replacements: dict[str, int] = {}
            if kind == "delete":
                for row in records:
                    metadata = _metadata(row)
                    if (
                        metadata.get("qzone_updated_by_delete_key") == key
                        and metadata.get("previous_id") is not None
                    ):
                        replacements[str(metadata["previous_id"])] = int(row["id"])
                    if (
                        metadata.get("qzone_kind") == "delete"
                        or metadata.get("qzone_post_status") == "deleted"
                    ):
                        continue
                    summary, facts = self._deleted_description(
                        str(metadata.get("persona_summary") or row["text"]),
                        list(metadata.get("key_facts") or []),
                        payload.get("occurred_at"),
                    )
                    metadata.update(
                        {
                            "persona_summary": summary,
                            "canonical_summary": summary,
                            "key_facts": facts,
                            "qzone_post_status": "deleted",
                            "qzone_deleted_at": payload.get("occurred_at"),
                            "qzone_updated_by_delete_key": key,
                        }
                    )
                    replacement = getattr(engine, "replace_memory", None)
                    if not callable(replacement):
                        raise IntegrationIncompatible("LivingMemory 缺少完整记忆修订接口")
                    new_id = await replacement(
                        int(row["id"]),
                        content=summary,
                        metadata=metadata,
                        importance=float(metadata.get("importance", 0.7)),
                    )
                    replacements[str(row["id"])] = int(new_id)
            if existing is not None:
                memory_id = int(existing["id"])
                return {
                    "id": memory_id,
                    "memory_id": memory_id,
                    "deduplicated": True,
                    "updated_memory_ids": replacements,
                }
            summary, facts = self._describe(payload, binding)
            deletion = next(
                (_metadata(row) for row in records if _metadata(row).get("qzone_kind") == "delete"),
                None,
            )
            deleted = kind == "delete" or deletion is not None
            if deletion is not None and kind != "delete":
                summary, facts = self._deleted_description(
                    summary, facts, deletion.get("qzone_occurred_at")
                )
            processor = peer.initializer.memory_processor
            content, metadata, importance = processor.build_memory_from_structured_data(
                structured_data={
                    "summary": summary,
                    "topics": ["QQ空间", str(binding["persona_id"] or "当前角色")],
                    "key_facts": facts[:5],
                    "sentiment": "neutral",
                    "importance": 0.7,
                },
                is_group_chat=bool(_value(event, "get_group_id")),
                fallback_excerpt=summary,
            )
            metadata.update(
                {
                    "memory_origin": "qzone_life",
                    "qzone_event_key": key,
                    "qzone_kind": kind,
                    "qzone_tid": tid,
                    "qzone_bot_id": binding["bot_id"],
                    "qzone_post_status": "deleted" if deleted else "published",
                    "qzone_occurred_at": payload.get("occurred_at"),
                    "qzone_source": (payload.get("post") or {}).get("source", ""),
                    "qzone_comment": payload.get("comment") or {},
                    "qzone_reply": payload.get("reply") or {},
                    "source_session_id": binding["umo"],
                    "time_tags": [_time_label(payload.get("occurred_at"))],
                    "source_window": {"session_id": binding["umo"], "triggered_by": "qzone_life"},
                }
            )
            atoms = None
            classifier = getattr(processor, "classify_atoms_from_metadata", None)
            if callable(classifier):
                atoms = classifier(
                    metadata=metadata,
                    parent_importance=importance,
                    session_id=binding["scope"],
                    persona_id=binding["persona_id"],
                )
            memory_id = int(
                await engine.add_memory(
                    content=content,
                    session_id=binding["scope"],
                    persona_id=binding["persona_id"],
                    importance=importance,
                    metadata=metadata,
                    atoms=atoms,
                )
            )
            return {
                "id": memory_id,
                "memory_id": memory_id,
                "deduplicated": False,
                "updated_memory_ids": replacements,
            }
