"""Independent life photography; scheduler state is strictly read-only."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import time
import uuid
from collections import OrderedDict
from contextlib import aclosing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import mcp.types

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageEventResult,
    ResultContentType,
)
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, StarTools
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.agent.tool import ToolSet
from astrbot.core.exceptions import ProviderNotFoundError
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from ...runtime import component_enabled
from ..voice.core.text import strip_voice_tags
from ..voice.core.transport import send_native_private
from .core.config import Settings, ordered_references_model, validate_request
from .core.dashboard import PhotoDashboard
from .core.delivery import (
    CAPTION_GUIDANCE,
    SEND_TIMEOUT,
    DeliveryCancelled,
    DeliveryRejected,
    DeliveryUnknown,
    can_send_photo,
    can_send_proactive_photo,
    capture_target,
    dispatch_completion,
    rebuild_event,
    send_photo,
)
from .core.face_lock import relock
from .core.life import SCHEDULER_ID, global_persona_id, read_life
from .core.photo_context import PhotoContextStore, PhotoRecord, PhotoScope
from .core.planner import (
    BODY_REFERENCE_ROLE,
    FOOD_GUIDANCE,
    ShotSpec,
    body_reference_framing,
    continuation_role,
    group_camera_changed,
    has_portrait_scene_text,
    keeps_group_camera,
    make_plan,
    parse_shot_plan,
    reference_roles_note,
    scene_reference_note,
    selected_outfit,
)
from .core.pose_examples import pose_examples_prompt
from .core.poses import REACTION_GUIDANCE, SMILE_GUIDANCE, keeps_expression
from .core.provider import PhotoClient, PhotoError, validate_image
from .core.reference_library import ReferenceLibrary
from .core.references import (
    event_references,
    read_image,
    unique_references,
)
from .core.vision_notices import VisionNoticeFilter

PLUGIN_ID = "astrbot_plugin_life_photo"
TOOL_NAME = "photo_generate"
TURN_KEY = "_life_photo_request"
LIFE_SNAPSHOT_KEY = "_life_photo_planning_life"
BUSY_MEDIA_GUARD = "life_busy_media_guard_v1"
NATIVE_CONFIRMED_SEND = "life_private_native_confirmed_send_v1"
LAST_ATTEMPTED_TEXT = "_life_photo_last_attempted_text"
LEGACY_WAITING_JOB = "_life_photo_legacy_waiting_job"
LEGACY_VOICE_RECEIPT_COUNT = "_life_photo_legacy_voice_receipt_count"
_STATUS_CHAINS = {"tool_call", "reasoning", "agent_stats", "break"}
_CJK_TEXT = r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff60]"
_CJK_SPACES = re.compile(rf"(?<={_CJK_TEXT}) | (?={_CJK_TEXT})")
BUSY_MEDIA_STOPPED = "本次忙碌补答的拍照许可已失效，已停止拍照。"
_PHOTO_REPLY_FILTER = "_life_photo_reply_filter"
_PHOTO_REPLY_ANCHOR = "_life_photo_reply_anchor"
GROUP_MATERIAL_TTL = 120
GROUP_MATERIAL_MAX_SCOPES = 16
GROUP_MATERIAL_MAX_BYTES = 8 * 1024 * 1024
GROUP_MATERIAL_CAPTURE_TIMEOUT = 2
_GROUP_MATERIAL_SEEN = "_life_photo_group_material_seen"
_GROUP_MATERIAL_CURRENT = "_life_photo_group_material_current"
_GROUP_MATERIAL_PENDING = "_life_photo_group_material_pending"
_GROUP_MATERIAL_PREVIOUS = "_life_photo_group_material_previous"
_PREVIOUS_GROUP_MATERIAL = re.compile(
    r"(?:^|[，,。！!？?；;\n])\s*(?:请|就|我想|可以)?(?:"
    r"(?:用|让|拿|把)\s*(?:她|他|这(?:个|位)(?:人物|角色)|"
    r"(?:刚才(?:发的)?[这那]张|上(?:一)?张)(?:照片|图)?(?:里的人(?:物)?)?)\s*"
    r"(?:(?:当(?:成|作)?|作为|做)\s*我的?替身|(?:来)?代替我)"
    r"|(?:用|拿)(?:我)?(?:"
    r"(?:(?:上一条|上一轮|刚才)(?:发(?:给你)?|上传)的[这那]?张|上(?:一)?张)(?:人物)?(?:照片|图)"
    r"|(?:刚才|上面)[这那]张(?:(?:人物)?(?:照片|图))?)"
    r"(?:里的人(?:物)?)?[^，,。！!？?；;\n]{0,12}(?:合(?:一?张|个)?影|(?:跟|和)你一起拍))"
)
_REPLACE_GROUP_PERSON = re.compile(
    r"(?:^|[，,。！!？?；;\n])\s*(?:请|这次|这回)?(?:"
    r"(?:把|将)(?:合影(?:对象|的人)|另一个人|我(?:的替身)?)(?:换|改)(?:成|为)"
    r"|(?:换|改用)(?:另一?|一个|一位|个)?(?:合影对象|人物素材|替身)"
    r"|(?:用|拿)(?:本条|本轮|刚(?:发|上传)的|引用的|这(?:一)?张|那(?:一)?张)"
    r"[^，,。！!？?；;\n]{0,16}(?:当(?:成|作)?|作为|做)我的?替身)"
)
_RECENT_EDIT_SOURCE = r"(?:刚才|刚刚)(?:拍的|的)?(?:这|那)(?:一)?张|上(?:一)?张"
_GROUP_EDIT_SOURCE = (
    r"(?:刚才|刚刚|上一组|前一组)[^，。]{0,12}第(?P<index>[一二三四1234])张"
)
_EXCLUDED_EDIT_SOURCE = re.compile(
    r"(?:不要|别|不用|不想(?:要)?)(?:再)?(?:用|使用|选|参考)?\s*"
    rf"(?:(?P<recent>{_RECENT_EDIT_SOURCE})|(?P<group>{_GROUP_EDIT_SOURCE}))(?:照片|图片|图)?(?!的|里|图|照片)"
)
_CURRENT_EDIT_IMAGES_ONLY = re.compile(
    r"(?:^|[，,。！!？?；;\n])\s*(?:请)?(?:这次|本轮)?(?:只|仅)(?:用|使用|合成|合并|参考)"
    r"[^，,。！!？?；;\n]{0,12}(?:本轮|这次|刚发|刚上传)"
    r"[^，,。！!？?；;\n]{0,10}(?:图|照片|附件|[一二三四两1234]张)"
)
CHAT_GUIDANCE = (
    "照片交流：结合本轮原话、当前聊天与人设理解是否要照片，不要求用户写摄影提示词。"
    "想见对方、问穿搭并让拍给自己看，可按语境理解为要图；单纯表达情绪或谈论照片不自动拍照。"
    "话题和眼下生活自然接得上时，可以顺手拍一张，例如聊吃什么时正在吃饭、聊在哪时眼前有值得看的景色，或聊到今天穿搭。"
    "话题无关、眼下没什么值得拍的内容时只聊天，不突然插入照片。"
    "用户明确不要照片时不调用。user_prompt写简短拍摄意图，不能把你补写的拍法当作用户硬要求。"
    "所有新拍、生成和改图默认用3:4。只有用户明确要求其他比例或横图、正方形等构图时填写aspect_ratio。"
    "生活照在同一次photo_generate调用的shot_plan中，为每张安排capture/framing/angle/action/expression；"
    "capture为角色自行拍摄的方式，近景可手持、全身可对镜或固定手机定时，不默认旁人摄影或棚拍。"
    "人像新拍或续拍时，以本次日程心情为表情底色，按当前聊天调整，用符合人设的方式表达，用户明确指定优先。"
    "参考当前情绪强度：强烈时神态可以更生动，中性时自然放松，低落或疲惫时保留对应状态，不强行笑。"
    "区分用户自己的心情与角色对聊天的反应，按后者安排神态。"
    f"{REACTION_GUIDANCE}{SMILE_GUIDANCE}"
    "保留原身份参考中可见的人物外貌和质感。新拍按当前日程；续拍以来源成片实际可见的衣服和场景为准，"
    "别把旧日程衣服或场景写进续拍方案。用户要求保持表情时沿用成片实际表情，否则按本次心情和对话安排。"
    "自拍习惯自动跟随本次已有的人设性格、说话方式和互动氛围，不套统一动作模板；"
    "人设明确的性别最多作弱参考，不按姓名或参考图猜性别，不把男角色都拍冷脸、女角色都拍微笑。"
    "食物默认是就餐者在餐桌前用手机后置、普通吃饭距离和轻俯角随手拍，使用现场光和自然景深，"
    "保留适量已有容器与桌面，不擅添餐具、手、菜品、污渍或故意模糊，不默认广告摆盘与精修油光；"
    f"{FOOD_GUIDANCE}"
    "用户原话明确的特写、容器、食物和风格优先。食物或场景照用use_current_scene说明意图："
    "问角色眼下吃什么、窗外什么样等当前生活时填true，另点食物或地点时填false。"
    "人物身份、表情及原画风规则不套在食物或场景上。"
    "同组保持人物、衣服和环境；只锁用户明确要求的维度，主要靠角度、动作、距离和视线体现变化。"
    "续拍或换角度用action=continue并选择source_photo_id；只重发原图用action=resend，绝不重新生成。"
    "按下方的分组标签和组内序号选精确photo_id。‘刚才那组’指最近实际发了多张的那批。"
    "用户明确说哪组第几张时由插件核对；缺目标时请用户说明，不猜编号，不把内部编号说给用户。"
    "修改当前或引用的图片用mode=edit；修改刚才某张成功照片时选source_photo_id。"
    "角色与指定人物合影用with_user_image=true，优先本条新发的人物素材，没有再读本条引用。素材也可以是虚构人物。"
    "用户明确指定紧邻上一条上传的人物合影或充当替身时，也用with_user_image=true，由插件确认素材是否可用。"
    "用户同时提供新素材时，两者共同参考，按原话分工。用户明确只用本轮素材时不选旧照；"
    "用户只排除某张旧照时，仍选其明确要求的其他成片。"
    "只改背景时保留原图表情。"
    "下方是本会话、本角色最近两批成功成片的数据。描述和拍摄方案是拍前计划。"
    "已有合影续拍默认沿用两个人，明确更换合影对象时才用新人物素材。"
    "本轮附有旧成片时，可以根据它实际可见的内容承接；没有记录时不声称已找到旧图。"
)


def tool_result(message):
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)]
    )


def _clean_waiting_text(text: str) -> str:
    text = " ".join(strip_voice_tags(text).split())
    if not re.search(rf"(?!{_CJK_TEXT})\w", text):
        return text.replace(" ", "")
    return _CJK_SPACES.sub("", text)


class TaskStopped(RuntimeError):
    pass


@dataclass
class _GroupMaterial:
    expires: float
    scope: PhotoScope | None = None
    data: bytes | None = None


async def _read_gallery_faces(
    library: ReferenceLibrary, paths: tuple[str, ...]
) -> list[bytes]:
    if not paths:
        return []
    try:
        return await asyncio.to_thread(
            lambda: [library.read_image(path)[0] for path in paths]
        )
    except (OSError, ValueError, PhotoError):
        raise PhotoError("人物面部参考图无法读取，请在参考图库重新上传。") from None


class LifePhotoPlugin:
    life_busy_media_guard_v1 = True

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.config_error = ""
        try:
            self.settings = Settings.from_config(config)
            self.client = (
                PhotoClient(
                    self.settings.base_url, self.settings.api_keys, self.settings.model
                )
                if self.settings.configured
                else None
            )
        except (ValueError, PhotoError) as exc:
            self.settings = None
            self.client = None
            self.config_error = str(exc)
            logger.warning("[自然照片] %s", self.config_error)
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self.reference_library = ReferenceLibrary(self.data_dir, config)
        self.cache = self.data_dir / "photos"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.records_path = self.data_dir / "tasks.json"
        self.records = self._load_records()
        self.photo_context = PhotoContextStore(
            self.data_dir / "photo_context.json", self.cache
        )
        self.last_error = ""
        self.closing = False
        self.tasks = set()
        self.users = {}
        self.slots = asyncio.Semaphore(4)
        self.delivery_locks = {}
        self.recent_poses = list(self.records.get("recent_poses", []))
        self.seen = OrderedDict()
        self._group_material = OrderedDict()
        self._vision_notices = VisionNoticeFilter(Path(get_astrbot_temp_path()))
        self.dashboard = PhotoDashboard(self)

    def _load_records(self):
        if not self.records_path.exists():
            return {"recent_poses": [], "tasks": []}
        value = json.loads(self.records_path.read_text(encoding="utf-8-sig"))
        for row in value["tasks"]:
            if row["status"] in {"queued", "generating", "sending"}:
                row["status"] = "interrupted"
        return value

    def _record(self, job, status, sent=0):
        rows = self.records["tasks"]
        row = next((r for r in rows if r["id"] == job["id"]), None)
        if row is None:
            row = {
                "id": job["id"],
                "status": status,
                "sent_count": sent,
                "action": job.get("action", "generate"),
            }
            if scope := job.get("scope"):
                row["dashboard_scope"] = asdict(scope)
                row["dashboard_created_at"] = datetime.now(timezone.utc).isoformat()
                row["dashboard_mode"] = job.get("mode")
            rows.append(row)
        row.update(status=status, sent_count=sent)
        if outputs := job.get("dashboard_outputs"):
            row["dashboard_outputs"] = [path.name for path in outputs]
        self.records["tasks"] = rows[-50:]
        self.records["recent_poses"] = self.recent_poses[-3:]
        temp = self.records_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.records, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.records_path)

    async def initialize(self):
        # Loading or saving settings never starts a paid request.
        if self.records_path.exists():
            temp = self.records_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self.records, ensure_ascii=False), encoding="utf-8"
            )
            temp.replace(self.records_path)
        self.dashboard.register()

    async def terminate(self):
        self.closing = True
        self.dashboard.unregister()
        for material in self._group_material.values():
            material.data = None
        self._group_material.clear()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.reference_library.close)
        if self.client:
            await self.client.close()

    def _unavailable(self, event, *, require_service=True):
        if not event.is_private_chat():
            return "照片工具当前只用于私聊。"
        if not can_send_photo(event):
            return "当前私聊渠道没有可确认的照片发送方式。"
        if self.closing:
            return "照片插件正在关闭，请稍后重试。"
        if self.config_error:
            return self.config_error
        if not self.settings.enabled:
            return "照片功能已关闭。"
        if require_service and not self.settings.configured:
            return "请先在自然照片设置填写服务地址、密钥和模型名称。"
        return ""

    async def _group_material_scope(
        self, event, *, cid=None, conversation=None
    ) -> PhotoScope | None:
        umo = event.unified_msg_origin
        manager = self.context.conversation_manager
        if cid is None:
            cid = await manager.get_curr_conversation_id(umo)
        if not cid:
            return None
        if conversation is None:
            conversation = await manager.get_conversation(umo, cid)
        if (
            conversation is None
            or conversation.cid != cid
            or conversation.user_id != umo
        ):
            return None
        persona_id, *_ = await self.context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conversation.persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(umo).get("provider_settings"),
        )
        if persona_id and await manager.get_curr_conversation_id(umo) == cid:
            return PhotoScope(umo, cid, persona_id)
        return None

    def _expire_group_material(self, key, material):
        material.data = None
        if self._group_material.get(key) is material:
            self._group_material.pop(key)

    async def observe_group_material(self, event: AstrMessageEvent):
        """Only the immediately previous genuine private user turn may supply a stand-in."""
        sender = event.get_sender_id()
        if (
            not event.is_private_chat()
            or not sender
            or sender == event.get_self_id()
            or event.get_extra("life_photo_completion")
            or event.get_extra(_GROUP_MATERIAL_SEEN)
        ):
            return
        event.set_extra(_GROUP_MATERIAL_SEEN, True)
        key = (event.unified_msg_origin, sender)
        previous = self._group_material.pop(key, None)
        if self._unavailable(event):
            if previous is not None:
                previous.data = None
            return
        now = time.monotonic()
        material = _GroupMaterial(now + GROUP_MATERIAL_TTL)
        self._group_material[key] = material
        if len(self._group_material) > GROUP_MATERIAL_MAX_SCOPES:
            _, evicted = self._group_material.popitem(last=False)
            evicted.data = None
        if previous is not None:
            if (
                previous.data is not None
                and previous.expires > now
                and _PREVIOUS_GROUP_MATERIAL.search(event.get_message_str())
            ):
                event.set_extra(_GROUP_MATERIAL_PREVIOUS, previous)
            else:
                previous.data = None
        try:
            direct = await asyncio.wait_for(
                event_references(event, current_only=True, only_single=True),
                GROUP_MATERIAL_CAPTURE_TIMEOUT,
            )
        except (OSError, PhotoError, asyncio.TimeoutError):
            direct = []
        if direct:
            data = direct[0]
            event.set_extra(_GROUP_MATERIAL_CURRENT, data)
            if len(data) <= GROUP_MATERIAL_MAX_BYTES:
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
                scope = (
                    await self._group_material_scope(event, cid=cid) if cid else None
                )
                remaining = material.expires - time.monotonic()
                if (
                    (scope is not None or not cid)
                    and remaining > 0
                    and self._group_material.get(key) is material
                ):
                    if scope is None:
                        # The first request will receive its conversation after this adapter hook.
                        event.set_extra(_GROUP_MATERIAL_PENDING, material)
                    else:
                        material.scope, material.data = scope, data
                    asyncio.get_running_loop().call_later(
                        remaining, self._expire_group_material, key, material
                    )
                    return
        if self._group_material.get(key) is material:
            self._group_material.pop(key)

    async def _bind_pending_group_material(self, event, conversation) -> None:
        material = event.get_extra(_GROUP_MATERIAL_PENDING)
        if material is None or material.scope is not None or conversation is None:
            return
        key = (event.unified_msg_origin, event.get_sender_id())
        if (
            self._group_material.get(key) is not material
            or material.expires <= time.monotonic()
        ):
            return
        scope = await self._group_material_scope(event, conversation=conversation)
        if (
            scope is not None
            and self._group_material.get(key) is material
            and material.scope is None
            and material.expires > time.monotonic()
        ):
            material.scope = scope
            material.data = event.get_extra(_GROUP_MATERIAL_CURRENT)

    async def _group_references(self, event, action):
        current = event.get_extra(_GROUP_MATERIAL_CURRENT)
        if current is not None:
            return [current]
        supplied = await event_references(event, prefer_current=True)
        previous = event.get_extra(_GROUP_MATERIAL_PREVIOUS)
        if (
            supplied
            or action != "generate"
            or previous is None
            or previous.data is None
            or any(isinstance(component, Reply) for component in event.get_messages())
        ):
            return supplied
        scope = await self._group_material_scope(event)
        if (
            previous.scope == scope
            and previous.expires > time.monotonic()
            and previous.data is not None
        ):
            data = previous.data
            previous.data = None
            return [data]
        return supplied

    def _arm_photo_reply_filter(self, event: AstrMessageEvent) -> None:
        if event.get_extra(_PHOTO_REPLY_FILTER):
            return
        original_set_result = event.set_result
        had_override = "set_result" in vars(event)

        def restore():
            if event.set_result is set_photo_result:
                if had_override:
                    event.set_result = original_set_result
                else:
                    del event.set_result

        async def clean_stream(source):
            async with (
                aclosing(source),
                aclosing(self._vision_notices.clean_stream(source)) as cleaned,
            ):
                async for chain in cleaned:
                    yield chain

        def set_photo_result(result):
            # Native streaming replies bypass on_decorating_result.
            if (
                isinstance(result, MessageEventResult)
                and result.result_content_type == ResultContentType.STREAMING_RESULT
                and result.async_stream is not None
            ):
                restore()
                result.set_async_stream(clean_stream(result.async_stream))
            original_set_result(result)

        event.set_result = set_photo_result
        event.set_extra(_PHOTO_REPLY_FILTER, restore)

    async def clean_photo_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        if event.get_extra(_PHOTO_REPLY_FILTER):
            self._vision_notices.clean_chain(result.chain)
        job = event.get_extra(LEGACY_WAITING_JOB)
        if job is not None and result.type not in _STATUS_CHAINS and any(
            isinstance(part, Plain) and part.text.strip() for part in result.chain
        ):
            event.set_extra(LEGACY_WAITING_JOB, None)
            if not job["ack"].is_set() and not job.get("ack_finished"):
                self._attach_legacy_waiting_sender(event, job)

    async def capture_photo_turn(self, event: AstrMessageEvent, run_context):
        if event.get_extra(_PHOTO_REPLY_FILTER) and run_context.messages:
            event.set_extra(_PHOTO_REPLY_ANCHOR, run_context.messages[-1])

    async def clean_photo_agent_done(
        self, event: AstrMessageEvent, run_context, response
    ):
        restore = event.get_extra(_PHOTO_REPLY_FILTER)
        if not restore:
            return
        restore()
        if response is not None:
            if response.result_chain is not None:
                self._vision_notices.clean_chain(response.result_chain.chain)
            elif response.completion_text:
                response.completion_text = self._vision_notices.clean_text(
                    response.completion_text
                )
        # AstrBot appends assistant TextParts before this hook and persists them after it.
        messages = run_context.messages
        anchor = event.get_extra(_PHOTO_REPLY_ANCHOR)
        start = next(
            (index + 1 for index, message in enumerate(messages) if message is anchor),
            len(messages),
        )
        for message in messages[start:]:
            if message.role != "assistant":
                continue
            if isinstance(message.content, str):
                message.content = self._vision_notices.clean_text(message.content)
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, TextPart):
                        part.text = self._vision_notices.clean_text(part.text)

    async def on_llm_request(self, event: AstrMessageEvent, request):
        unavailable = self._unavailable(event, require_service=False)
        completion = event.get_extra("life_photo_completion", False)
        if completion:
            if not unavailable:
                self._arm_photo_reply_filter(event)
            # The host can populate the initially empty set from the persona
            # before calling this hook. A completion only needs one text reply.
            request.func_tool = ToolSet(tools=[])
            return
        if unavailable:
            if request.func_tool is not None:
                request.func_tool = copy.copy(request.func_tool)
                request.func_tool.remove_tool(TOOL_NAME)
            return
        await self._bind_pending_group_material(event, request.conversation)
        scope = await self._photo_scope(event)
        available = self.photo_context.describe(scope)
        if available and (
            request.conversation is None
            or request.conversation.cid == scope.conversation_id
        ):
            self._arm_photo_reply_filter(event)
        if request.func_tool is None or request.func_tool.get_tool(TOOL_NAME) is None:
            return
        state, life_status = await read_life(self.context, event)
        if scope.persona_id != global_persona_id(self.context):
            return
        event.set_extra(LIFE_SNAPSHOT_KEY, (scope, state, life_status))
        slot = state.get("current_slot", {})
        mood = str(slot.get("mood") or "").strip()
        intensity = slot.get("mood_intensity")
        mood_context = f"本次日程心情（表情底色）：{mood}\n" if mood else ""
        if intensity:
            mood_context += f"本次日程情绪强度：{intensity}\n"
        available = available or "本会话暂无可复用的成功照片。"
        request.system_prompt += (
            "\n\n"
            + CHAT_GUIDANCE
            + "\n"
            + mood_context
            + available
            + pose_examples_prompt(state, event.get_message_str())
        )
        await self._attach_recent_photo(event, request, scope)

    async def _attach_recent_photo(self, event, request, scope: PhotoScope) -> None:
        if (
            not scope.conversation_id
            or request.conversation is None
            or request.conversation.cid != scope.conversation_id
        ):
            return
        user_text = event.get_message_str()
        if _CURRENT_EDIT_IMAGES_ONLY.search(user_text):
            return
        photo = self.photo_context.planning_reference(
            scope, user_text, has_new_images=bool(request.image_urls)
        )
        if photo is None:
            return

        label = (
            f"临时看图参考：角色在本会话已成功发送的第{photo.index}张成片（photo_id={photo.photo_id}）。"
            "根据画面承接本轮话题；这张图和本轮用户附件分别对应各自用途。"
        )
        if any(
            isinstance(part, TextPart) and part.text == label
            for part in request.extra_user_content_parts
        ):
            return
        provider_id = event.get_extra("selected_provider")
        if not isinstance(provider_id, str) or not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(scope.umo)
            except ProviderNotFoundError:
                return
        provider = self.context.get_provider_by_id(provider_id)
        modalities = provider.provider_config.get("modalities") if provider else None
        if not isinstance(modalities, list) or "image" not in modalities:
            return
        selected_model = request.model or event.get_extra("selected_model")
        if selected_model and selected_model != provider.get_model():
            return

        try:
            data, mime = await asyncio.to_thread(
                lambda: validate_image(photo.path.read_bytes())
            )
        except (OSError, PhotoError) as exc:
            logger.warning("[自然照片] 本轮旧成片预览不可读（%s）", type(exc).__name__)
            return

        persona_id, *_ = await self.context.persona_manager.resolve_selected_persona(
            umo=scope.umo,
            conversation_persona_id=request.conversation.persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(umo=scope.umo).get(
                "provider_settings", {}
            ),
        )
        if (
            (persona_id or "") != scope.persona_id
            or await self._photo_scope(event) != scope
            or scope.persona_id != global_persona_id(self.context)
        ):
            return
        encoded = base64.b64encode(data).decode("ascii")
        image_part = ImageURLPart(
            image_url=ImageURLPart.ImageURL(url=f"data:{mime};base64,{encoded}")
        )
        request.extra_user_content_parts = [
            *request.extra_user_content_parts,
            TextPart(text=label).mark_as_temp(),
            image_part.mark_as_temp(),
        ]

    async def _photo_scope(self, event):
        persona_id = global_persona_id(self.context)
        conversation_id = (
            await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
        )
        return PhotoScope(
            event.unified_msg_origin, str(conversation_id or ""), persona_id
        )

    async def _check_job(self, job):
        if self.closing or global_persona_id(self.context) != job["persona_id"]:
            raise TaskStopped("角色已切换，已停止这次旧角色照片")
        if not await component_enabled(self.context, PLUGIN_ID, job["umo"]):
            raise TaskStopped("照片插件或当前会话已停用，已停止这次照片")
        if job["life_status"] == "ready" and not await component_enabled(
            self.context, SCHEDULER_ID, job["umo"]
        ):
            raise TaskStopped("日程或当前会话已停用，已停止这次生活照")
        if job.get("conversation_id"):
            current_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    job["umo"]
                )
            )
            if current_id != job["conversation_id"]:
                raise TaskStopped("聊天已切换，已停止原会话照片")
        if self.closing or global_persona_id(self.context) != job["persona_id"]:
            raise TaskStopped("角色已切换，已停止这次旧角色照片")

    def _background_supported(self, event):
        adapter = self.context.get_platform_inst(event.get_platform_id())
        return (
            event.get_platform_name() == "aiocqhttp"
            and callable(getattr(StarTools, "create_message", None))
            and callable(getattr(adapter, "create_event", None))
            and not self.context.get_config(event.unified_msg_origin)
            .get("provider_settings", {})
            .get("streaming_response", False)
        )

    async def _prepare(
        self,
        event,
        user_prompt,
        photo_type,
        count,
        with_user_image,
        aspect_ratio,
        mode,
        action="generate",
        source_photo_id="",
        shot_plan=(),
        use_current_scene=None,
        *,
        proactive_intent=None,
        life_activity=None,
    ):
        settings = self.settings
        scope = await self._photo_scope(event)
        user_text = (
            event.get_message_str() if proactive_intent is None else proactive_intent
        )
        excluded_source = _EXCLUDED_EDIT_SOURCE.search(user_text)
        current_images_only = bool(_CURRENT_EDIT_IMAGES_ONLY.search(user_text))
        if mode == "edit" and action == "generate" and current_images_only:
            source_photo_id = ""
        uses_existing = action in {"continue", "resend"}
        named_group, source = (
            self.photo_context.explicit_group_reference(scope, user_text)
            if uses_existing or (mode == "edit" and not current_images_only)
            else (False, None)
        )
        if named_group and source is None:
            raise ValueError(
                "没找到你指定的那组照片，请说清是哪一组，或引用要用的原图。"
            )
        if not named_group and (source_photo_id or uses_existing):
            source = self.photo_context.get(scope, source_photo_id)
        if (source_photo_id or uses_existing) and source is None:
            raise ValueError(
                "当前聊天和角色没有这张可用的成功照片，请先拍照或提供原图。"
            )
        if action == "resend":
            return {
                "id": uuid.uuid4().hex,
                "umo": scope.umo,
                "user_id": event.get_sender_id(),
                "persona_id": scope.persona_id,
                "conversation_id": scope.conversation_id,
                "scope": scope,
                "plans": [],
                "images": (),
                "roles": (),
                "user_prompt": user_prompt,
                "life_status": "not_used",
                "action": action,
                "reuse_record": source,
            }
        if source is not None and (
            mode == "edit" or (action == "continue" and source.mode == "edit")
        ):
            selected_is_excluded = current_images_only
            if excluded_source and not selected_is_excluded:
                if excluded_source["recent"]:
                    recent = self.photo_context.get(scope)
                    selected_is_excluded = (
                        recent is not None and source.photo_id == recent.photo_id
                    )
                else:
                    excluded_index = excluded_source["index"].translate(
                        str.maketrans("一二三四", "1234")
                    )
                    named_excluded_group, excluded_photo = (
                        self.photo_context.explicit_group_reference(
                            scope, excluded_source["group"]
                        )
                    )
                    selected_is_excluded = (
                        excluded_photo is not None
                        and source.photo_id == excluded_photo.photo_id
                        if named_excluded_group
                        else source.index == int(excluded_index)
                    )
            if selected_is_excluded:
                if action == "continue":
                    raise ValueError(
                        "用户排除了选中的成片，请按本轮附件或指定的照片改图。"
                    )
                source = None
        if action == "continue":
            mode, photo_type = source.mode, source.photo_type
        if mode == "text" and not settings.generate_enabled:
            raise ValueError("文生图功能已关闭。")
        if mode == "edit" and not settings.edit_enabled:
            raise ValueError("改图功能已关闭。")
        state, life_status = {}, "not_used"
        if mode == "photo":
            planned_life = (
                event.get_extra(LIFE_SNAPSHOT_KEY) if proactive_intent is None else None
            )
            if planned_life and planned_life[0] == scope:
                state, life_status = planned_life[1:]
            else:
                state, life_status = await read_life(self.context, event)
        live_slot = state.get("current_slot", {})
        current_mood = str(live_slot.get("mood") or "").strip()
        current_mood_intensity = str(live_slot.get("mood_intensity") or "").strip()
        if life_activity is not None:
            fields = ("start", "end", "activity", "location")
            scene_slot = next(
                (slot for slot in state.get("past_slots", ())
                 if all(slot.get(key) == life_activity.get(key) for key in fields)),
                None,
            )
            if scene_slot is None:
                raise TaskStopped("这段活动的日程已变化，本次空间照片已取消。")
            state = {**state, "current_slot": scene_slot, "now": scene_slot["end"]}
        if action == "continue":
            state = copy.deepcopy(source.state)
            life_status = "ready" if state.get("current_slot") else "not_used"
        people = photo_type in {"selfie", "outfit"}
        continuing = action == "continue"
        ordered = ordered_references_model(settings.model)
        use_gallery = mode == "photo" and people and settings.selfie_enabled
        gallery = (
            await asyncio.to_thread(self.reference_library.snapshot)
            if use_gallery
            else None
        )
        images, roles = [], []
        if continuing and any(
            role.startswith("用户身份") for role in source.reference_roles
        ):
            with_user_image = not (
                mode == "photo" and settings.selfie_enabled and source.reference_images
            ) or bool(_REPLACE_GROUP_PERSON.search(user_text))
        continuing_references = (
            continuing
            and mode == "photo"
            and settings.selfie_enabled
            and not with_user_image
            and source.reference_images
        )
        if continuing_references:
            for image, role in zip(source.reference_images, source.reference_roles):
                if role.startswith(("角色身份", "用户身份")):
                    images.append(image)
                    roles.append(role)
        elif gallery is not None:
            if self.reference_library.error:
                raise PhotoError(self.reference_library.error)
            images = await _read_gallery_faces(self.reference_library, gallery["face"])
            roles = ["角色身份：仅角色人脸与身份"] * len(images)
        if with_user_image:
            supplied = await self._group_references(event, action)
        elif mode == "edit":
            supplied = await event_references(event)
        else:
            supplied = []
        auto_edit_source = (
            mode == "edit"
            and source is None
            and not excluded_source
            and not current_images_only
        )
        if auto_edit_source and not any(
            isinstance(component, Reply) for component in event.get_messages()
        ):
            if re.search(_GROUP_EDIT_SOURCE, user_text):
                raise ValueError("请从本会话的成功照片中选中用户指定的那一张。")
            if re.search(_RECENT_EDIT_SOURCE, user_text):
                source = self.photo_context.get(scope)
                if source is None:
                    raise ValueError(
                        "当前聊天没有找到刚才的成片，请重新发送要修改的原图。"
                    )
        if with_user_image:
            if not supplied:
                raise ValueError("没读到合影人物素材，请重新发或引用人物图片。")
            if not images:
                raise ValueError("合影需要先在自然照片设置上传角色参考人像。")
            images.insert(1, supplied[0])
            roles.insert(1, "用户身份：合影另一位人物，只参考所提供素材的外貌与身份")
        if mode == "edit":
            if not supplied and source is None and auto_edit_source:
                source = self.photo_context.get(scope)
            images = supplied
            roles = ["本轮提供的素材：按用户说明用于编辑"] * len(images)
            if source is not None:
                source_role = (
                    continuation_role(photo_type, keep_expression=False, generic=True)
                    if continuing
                    else "选中的旧成片：按用户要求修改，保留未要求改变的细节"
                )
                images = [await read_image(str(source.path)), *images]
                roles = [source_role, *roles]
                images, roles = unique_references(images, roles)
            if not images:
                raise ValueError("这次没有改图，请让对方发送或引用要修改的图片。")
        elif mode == "photo" and images and life_status == "ready" and not continuing:
            outfit = selected_outfit(state)
            path = outfit.get("image_path")
            if path:
                clothing = await read_image(str(path))
                index = 2 if with_user_image else 1
                images.insert(index, clothing)
                roles.insert(index, "衣服：仅服装款式，不取脸")
        if len(images) > 1 and not ordered:
            if (
                with_user_image
                or mode == "edit"
                or any(role.startswith("用户身份") for role in roles)
            ):
                raise ValueError(
                    "当前模型不能分别参考多张图片，请换用支持多参考图的模型后再合影或改图。"
                )
            if not continuing:
                logger.warning(
                    "[自然照片] 当前模型不支持有序多图，本次仅使用第一张角色参考图"
                )
                images, roles = images[:1], roles[:1]
        if (
            mode == "photo"
            and people
            and not any(role.startswith("用户身份") for role in roles)
        ):
            identity_role = "角色身份：主参考，决定脸型、五官相对比例和原有质感"
            for index, role in enumerate(roles):
                if role.startswith("角色身份"):
                    roles[index] = identity_role
                    identity_role = "角色身份：只补主参考未显示的角度或被遮挡细节"
        identity_references, identity_roles = tuple(images), tuple(roles)
        if continuing and mode != "edit":
            source_image = await read_image(str(source.path))
            source_role = continuation_role(
                photo_type,
                keep_expression=keeps_expression(user_text),
                generic=mode != "photo",
                has_identity_references=ordered
                and any(role.startswith(("角色身份", "用户身份")) for role in roles),
            )
            if not ordered:
                images, roles = [source_image], [source_role]
            elif source_image not in images:
                images.append(source_image)
                roles.append(source_role)
        if images and not settings.edit_enabled:
            raise ValueError("改图功能已关闭，使用参考图片的拍摄也已停用。")
        if not images and not settings.generate_enabled:
            raise ValueError("文生图功能已关闭，这次没有可用的参考图片。")
        if mode in {"edit", "text"}:
            plans = make_plan(
                {},
                user_prompt,
                photo_type,
                count,
                aspect_ratio,
                roles,
                [],
                "not_used",
                user_text=user_text,
            )
            plans = [
                replace(
                    plan,
                    prompt=user_prompt
                    + "\n用户本轮原话（优先于补充方案）："
                    + user_text
                    + ("\n\n" + reference_roles_note(roles) if roles else "")
                    + (
                        "\n本组各张保持相同主体与要求，各张有可见构图差异。"
                        if count > 1
                        else ""
                    )
                    + (
                        "\n本张具体方案："
                        + json.dumps(asdict(shot_plan[index]), ensure_ascii=False)
                        if shot_plan
                        else ""
                    ),
                    pose_id="",
                    spec=shot_plan[index] if shot_plan else None,
                )
                for index, plan in enumerate(plans)
            ]
        else:
            plans = make_plan(
                state,
                user_prompt,
                photo_type,
                count,
                aspect_ratio,
                roles,
                self.recent_poses,
                life_status,
                shot_plan=shot_plan,
                user_text=user_text,
                current_mood=current_mood,
                current_mood_intensity=current_mood_intensity,
                source_continuity=continuing,
                use_current_scene=use_current_scene,
                previous_shot=(
                    ShotSpec(**source.shot)
                    if action == "continue" and source.shot
                    else None
                ),
            )
        shot_references = {}
        if (
            gallery is not None
            and gallery["body"]
            and ordered
            and settings.edit_enabled
            and any(role.startswith("角色身份") for role in roles)
            and not any(role.startswith("用户身份") for role in roles)
        ):
            full_body_shots = [
                index
                for index, plan in enumerate(plans)
                if body_reference_framing(
                    plan.spec.framing,
                    user_text,
                    single_outfit=count == 1 and photo_type == "outfit",
                )
            ]
            body_images = []
            if full_body_shots:
                for path in gallery["body"]:
                    try:
                        image, _ = await asyncio.to_thread(
                            self.reference_library.read_image, path
                        )
                    except (OSError, ValueError, PhotoError):
                        logger.warning("[自然照片] 一张身材视图无法读取，已跳过")
                        continue
                    if image not in images and image not in body_images:
                        body_images.append(image)
            if body_images:
                # Keep face/clothing numbers fixed and shift only source continuity.
                index = len(identity_references)
                with_body = (*images[:index], *body_images, *images[index:])
                body_roles = (
                    roles[:index]
                    + [BODY_REFERENCE_ROLE] * len(body_images)
                    + roles[index:]
                )
                old_note = reference_roles_note(roles)
                new_note = reference_roles_note(body_roles)
                for shot_index in full_body_shots:
                    plan = plans[shot_index]
                    plans[shot_index] = replace(
                        plan, prompt=plan.prompt.replace(old_note, new_note, 1)
                    )
                    shot_references[shot_index] = with_body
        return {
            "id": uuid.uuid4().hex,
            "umo": event.unified_msg_origin,
            "user_id": event.get_sender_id(),
            "persona_id": scope.persona_id,
            "conversation_id": scope.conversation_id,
            "scope": scope,
            "plans": plans,
            "images": tuple(images),
            "shot_references": shot_references,
            "roles": tuple(roles),
            "user_prompt": user_prompt,
            "life_status": life_status,
            "state": state,
            "live_slot": live_slot,
            "photo_type": photo_type,
            "mode": mode,
            "action": action,
            "independent_group_camera": (
                count > 1
                and action == "generate"
                and mode == "photo"
                and people
                and any(role.startswith("角色身份") for role in roles)
                and not any(role.startswith("用户身份") for role in roles)
                and has_portrait_scene_text(state, life_status)
                and not keeps_group_camera(user_text)
            ),
            "identity_references": identity_references,
            "identity_roles": identity_roles,
        }

    async def _run_job(
        self, job, event=None, target=None, *, before_send=None, before_generate=None,
        proactive=False,
    ):
        sent, outputs, failed = 0, [], False
        job["dashboard_outputs"] = outputs
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if target and (ack := job.get("ack")) is not None:
                # A fast service must not overtake the original waiting reply.
                # If the original turn never sends, no paid request is started.
                try:
                    await asyncio.wait_for(ack.wait(), 60)
                except asyncio.TimeoutError:
                    raise TaskStopped("候场回复没有送出，本次照片没有开始") from None
                if job.get("ack_failed"):
                    raise TaskStopped("候场回复的发送结果未确认，本次照片没有开始")
            await self._check_job(job)
            if job["action"] == "resend":
                outputs = [job["reuse_record"].path]
            else:
                async with self.slots:
                    await self._check_job(job)
                    if before_generate is not None:
                        await before_generate()
                    self._record(job, "generating")
                    references = list(job["images"])
                    has_identity_references = any(
                        role.startswith(("角色身份", "用户身份"))
                        for role in job["roles"]
                    )
                    supports_group_reference = ordered_references_model(
                        self.settings.model
                    )
                    first_plan = job["plans"][0]
                    first_image = None
                    for index, plan in enumerate(job["plans"]):
                        await self._check_job(job)
                        add_scene = (
                            first_image is not None
                            and supports_group_reference
                            and (
                                not job["independent_group_camera"]
                                or not group_camera_changed(first_plan.spec, plan.spec)
                            )
                        )
                        refs = list(job["shot_references"].get(index, references))
                        if add_scene:
                            refs.append(first_image)
                        prompt = plan.prompt
                        if add_scene:
                            prompt = relock(
                                prompt
                                + "\n\n"
                                + scene_reference_note(
                                    len(refs),
                                    job["photo_type"],
                                    generic=job["mode"] != "photo",
                                    has_identity_references=has_identity_references,
                                )
                            )
                        elif first_image is not None and not supports_group_reference:
                            logger.warning(
                                "[自然照片] 当前模型不能追加首张场景参考，后续照片仅沿用固定文字条件"
                            )
                        if before_send is not None:
                            await before_send()
                        try:
                            data = await self.client.generate(prompt, refs, plan.size)
                        except PhotoError as exc:
                            self.last_error = str(exc)
                            logger.warning("[自然照片] %s", self.last_error)
                            failed = True
                            break
                        await self._check_job(job)
                        extension = (
                            ".png"
                            if data.startswith(b"\x89PNG")
                            else ".jpg"
                            if data.startswith(b"\xff\xd8")
                            else ".webp"
                        )
                        path = self.cache / (
                            job["id"] + f"-{len(outputs) + 1}" + extension
                        )
                        path.write_bytes(data)
                        outputs.append(path)
                        if first_image is None:
                            first_image = data
                        if plan.pose_id:
                            self.recent_poses = (self.recent_poses + [plan.pose_id])[
                                -3:
                            ]
            delivery_event = (
                await rebuild_event(self.context, target) if target else event
            )
            lock = self.delivery_locks.setdefault(job["scope"], asyncio.Lock())
            async with lock:
                confirmed = []
                try:
                    for index, path in enumerate(outputs, 1):
                        await self._check_job(job)
                        self._record(job, "sending", sent)
                        if before_send is None:
                            await send_photo(delivery_event, path)
                        else:
                            await send_photo(
                                delivery_event,
                                path,
                                before_send=before_send,
                                proactive=proactive,
                            )
                        if proactive:
                            job["proactive_photo_id"] = path.stem
                        sent += 1
                        if job["action"] == "resend":
                            self.photo_context.touch(job["reuse_record"])
                        elif job["scope"].conversation_id:
                            spec = job["plans"][index - 1].spec or ShotSpec()
                            confirmed.append(
                                PhotoRecord(
                                    scope=job["scope"],
                                    photo_id=path.stem,
                                    path=path,
                                    group_id=job["id"],
                                    index=index,
                                    photo_type=job["photo_type"],
                                    mode=job["mode"],
                                    description=job["user_prompt"][:600],
                                    state=job["state"],
                                    shot=asdict(spec),
                                    reference_images=job["identity_references"],
                                    reference_roles=job["identity_roles"],
                                )
                            )
                finally:
                    # Commit each successfully delivered group without an await
                    # between its rows, including confirmed rows on cancellation.
                    for record in confirmed:
                        self.photo_context.remember(record)
            self._record(
                job,
                "partial" if failed and sent else "sent" if sent else "failed",
                sent,
            )
            facts = {
                "sent_count": sent,
                "partial": failed and bool(sent),
                "status": "sent" if sent else "failed",
                "user_prompt": job["user_prompt"],
                "action": job["action"],
                "mode": job.get("mode"),
                "reused": job["action"] == "resend",
                "photo_ids": [path.stem for path in outputs[:sent]],
            }
            if target:
                await self._check_job(job)
                await dispatch_completion(self.context, target, facts)
            if not failed:
                self.last_error = ""
            return facts
        except asyncio.CancelledError:
            self._record(job, "interrupted", sent)
            raise
        except (TaskStopped, DeliveryCancelled) as exc:
            self.last_error = str(exc)
            self._record(job, "stopped", sent)
            logger.info("[自然照片] %s", self.last_error)
            return {
                "sent_count": sent,
                "status": "stopped",
                "partial": bool(sent),
                "photo_ids": [path.stem for path in outputs[:sent]],
            }
        except DeliveryUnknown as exc:
            if proactive:
                job["proactive_delivery_unknown"] = True
            self.last_error = str(exc)
            self._record(job, "unknown", sent)
            logger.warning("[自然照片] %s", self.last_error)
            if target:
                await self._check_job(job)
                await dispatch_completion(
                    self.context,
                    target,
                    {
                        "sent_count": sent,
                        "partial": bool(sent),
                        "status": "unknown",
                        "user_prompt": job["user_prompt"],
                    },
                )
            return {
                "sent_count": sent,
                "status": "unknown",
                "partial": bool(sent),
                "photo_ids": [path.stem for path in outputs[:sent]],
            }
        except Exception as exc:
            self.last_error = "照片发送或保存未完成，请查看日志后重新发起"
            logger.error("[自然照片] 照片任务未完成（%s）", type(exc).__name__)
            status = (
                "partial" if sent and isinstance(exc, DeliveryRejected) else "failed"
            )
            self._record(job, status, sent)
            facts = {
                "sent_count": sent,
                "status": status,
                "partial": bool(sent),
                "photo_ids": [path.stem for path in outputs[:sent]],
            }
            if target:
                await self._check_job(job)
                await dispatch_completion(
                    self.context, target, {**facts, "user_prompt": job["user_prompt"]}
                )
            return facts
        finally:
            if "ack" in job:
                job["ack_finished"] = True
                restore = job.pop("restore_waiting_sender", None)
                if restore is not None:
                    restore()
            self.tasks.discard(task)
            self._release(job)
            # Only generated photos are cached. Original references stay in
            # memory for this scope's current successful group, never on disk.
            photos = sorted(self.cache.iterdir(), key=lambda p: p.stat().st_mtime)
            for path in photos[:-50]:
                path.unlink()

    async def generate_qzone_selfie(
        self, event, prompt: str, *, destination: Path, before_generate
    ) -> dict:
        return await self.generate_qzone_photo(
            event, prompt, photo_type="selfie", destination=destination,
            before_generate=before_generate,
        )

    async def generate_qzone_photo(
        self, event, prompt: str, *, photo_type: str, destination: Path,
        before_generate, activity=None,
    ) -> dict:
        """Generate one image for QQ space without delivering it in chat."""
        return await asyncio.create_task(
            self._generate_qzone_photo(event, prompt, photo_type, destination, before_generate, activity)
        )

    async def _generate_qzone_photo(
        self, event, prompt, photo_type, destination, before_generate, activity
    ):
        unavailable = self._unavailable(event)
        if unavailable:
            raise PhotoError(unavailable)
        if photo_type not in ("selfie", "food", "scene"):
            raise ValueError("空间照片类型应为自拍、食物照或场景照。")
        if activity is not None and not isinstance(activity, dict):
            raise ValueError("过去活动应使用日程的时间段记录。")
        label = {"selfie": "QQ空间自拍", "food": "QQ空间食物照", "scene": "QQ空间场景照"}[photo_type]
        if event.get_platform_name() != "aiocqhttp":
            raise PhotoError(label + "只用于已绑定的QQ个人号。")
        if not callable(before_generate):
            raise ValueError(label + "缺少发布条件检查。")
        if photo_type == "selfie" and not self.settings.selfie_enabled:
            raise PhotoError("自拍功能已关闭。")
        validate_request(prompt, photo_type, 1, False, "3:4", "photo")
        destination = Path(destination).resolve()
        space_data = Path(StarTools.get_data_dir("astrbot_plugin_qzone_life")).resolve()
        if not destination.is_relative_to(space_data):
            raise ValueError(label + "必须保存在空间的数据目录。")
        if await before_generate() is not True:
            raise TaskStopped(label + "的发布条件已变化。")
        user = event.get_sender_id()
        if self.users.get(user) or sum(self.users.values()) >= 16:
            raise TaskStopped("当前用户还有照片任务，请稍后再试。")
        self.users[user] = 1
        task = asyncio.current_task()
        self.tasks.add(task)
        job = {"user_id": user}
        try:
            job = await self._prepare(
                event, prompt, photo_type, 1, False, "3:4", "photo",
                use_current_scene=True, proactive_intent=prompt, life_activity=activity,
            )
            if job["life_status"] != "ready":
                raise TaskStopped("当前日程尚未就绪，" + label + "暂不生成。")
            slot = job["live_slot"]
            activity_slot = job["state"]["current_slot"] if activity is not None else None

            async def authorize():
                await self._check_job(job)
                if await before_generate() is not True:
                    raise TaskStopped(label + "的发布条件已变化。")
                await self._check_job(job)
                live, status = await read_life(self.context, event)
                if status != "ready" or live.get("current_slot") != slot:
                    raise TaskStopped("角色日程已变化，本次" + label + "已取消。")
                if activity_slot is not None and activity_slot not in live.get("past_slots", ()):
                    raise TaskStopped("这段活动的日程已变化，本次" + label + "已取消。")

            async with self.slots:
                await authorize()
                plan = job["plans"][0]
                references = job["shot_references"].get(0, job["images"])
                data = await self.client.generate(plan.prompt, list(references), plan.size)
                await authorize()
            extension = (
                ".png" if data.startswith(b"\x89PNG")
                else ".jpg" if data.startswith(b"\xff\xd8") else ".webp"
            )
            path = destination.with_suffix(extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(data)
            if plan.pose_id:
                self.recent_poses = (self.recent_poses + [plan.pose_id])[-3:]
            return {
                "path": str(path), "persona_id": job["persona_id"],
                "state": job["state"], "aspect_ratio": "3:4", "photo_type": photo_type,
            }
        finally:
            self.tasks.discard(task)
            self._release(job)

    async def send_proactive_photo(
        self,
        event,
        user_prompt: str,
        photo_type="selfie",
        shot_plan=None,
        *,
        before_send,
    ) -> dict:
        """Internal entry for a caller-owned private delivery, without chat hooks."""
        return await asyncio.create_task(
            self._send_proactive_photo(
                event, user_prompt, photo_type, shot_plan, before_send
            )
        )

    async def _send_proactive_photo(
        self, event, user_prompt, photo_type, shot_plan, before_send
    ) -> dict:
        def no_delivery(status, error):
            return {"status": status, "deliveries": [], "error": error}

        unavailable = self._unavailable(event)
        if unavailable:
            disabled = self.closing or (
                self.settings is not None and not self.settings.enabled
            )
            return no_delivery("cancelled" if disabled else "failed", unavailable)
        if not can_send_proactive_photo(event):
            return no_delivery("failed", "当前私聊没有可确认的照片发送目标。")
        if not callable(before_send):
            return no_delivery("failed", "缺少主动发送许可检查。")
        try:
            validate_request(user_prompt, photo_type, 1, False, "3:4", "photo")
            specs = parse_shot_plan(
                [shot_plan] if isinstance(shot_plan, dict) else shot_plan, 1
            )
        except ValueError as exc:
            return no_delivery("failed", str(exc))

        user = event.get_sender_id()
        umo = event.unified_msg_origin
        if self.users.get(user) or sum(self.users.values()) >= 16:
            return no_delivery("cancelled", "当前私聊还有照片任务。")
        self.users[user] = 1
        task = asyncio.current_task()
        self.tasks.add(task)
        job = {"user_id": user}
        facts = {}
        try:
            intent = user_prompt.strip()
            prepared = await self._prepare(
                event,
                intent,
                photo_type,
                1,
                False,
                "3:4",
                "photo",
                shot_plan=specs,
                use_current_scene=True,
                proactive_intent=intent,
            )
            if prepared["user_id"] != user or prepared["umo"] != umo:
                return no_delivery("cancelled", "照片的私聊目标已变化。")
            job = prepared
            if not job["conversation_id"] or job["life_status"] != "ready":
                return no_delivery("cancelled", "当前私聊或日程尚未就绪。")
            current_slot = job["state"]["current_slot"]

            async def authorize():
                try:
                    await self._check_job(job)
                    live, status = await read_life(self.context, event)
                    if status != "ready" or live.get("current_slot") != current_slot:
                        raise TaskStopped("角色日程已切换，本次主动照片已取消。")
                    await self._check_job(job)
                    if (
                        not event.is_private_chat()
                        or event.unified_msg_origin != umo
                        or event.get_sender_id() != user
                        or not await component_enabled(self.context, PLUGIN_ID, umo, event)
                        or not can_send_proactive_photo(event)
                    ):
                        raise TaskStopped("照片的私聊目标已变化。")
                    if await before_send() is not True:
                        raise TaskStopped("本次主动照片已取消。")
                except TaskStopped as exc:
                    raise DeliveryCancelled(str(exc)) from None
                except Exception as exc:
                    logger.warning(
                        "[自然照片] 主动发送检查失败（%s）", type(exc).__name__
                    )
                    raise DeliveryCancelled("主动发送检查失败，本次已停止。") from None

            await self._check_job(job)
            self._record(job, "queued")
            facts = await self._run_job(
                job, event=event, before_send=authorize, proactive=True
            )
        except TaskStopped:
            return no_delivery("cancelled", "照片的私聊或角色已变化。")
        except asyncio.CancelledError:
            if self.closing:
                return no_delivery("cancelled", "照片插件正在关闭。")
            raise
        except Exception as exc:
            logger.error("[自然照片] 主动照片处理失败（%s）", type(exc).__name__)
        finally:
            self.tasks.discard(task)
            self._release(job)

        if photo_id := job.get("proactive_photo_id"):
            return {
                "status": "sent",
                "deliveries": [
                    {"medium": "photo", "photo_id": photo_id, "text": intent[:160]}
                ],
            }
        if job.get("proactive_delivery_unknown") or facts.get("status") == "unknown":
            return no_delivery("unknown", "平台未确认照片是否送达。")
        if facts.get("status") == "stopped":
            return no_delivery("cancelled", "发送条件已变化，本次照片已取消。")
        return no_delivery("failed", "本次主动照片未能完成。")

    def _release(self, job):
        if job.get("released"):
            return
        job["released"] = True
        user = job["user_id"]
        self.users[user] -= 1
        if not self.users[user]:
            del self.users[user]

    def _worker_done(self, job, task):
        self.tasks.discard(task)
        already_released = job.get("released", False)
        self._release(job)
        if task.cancelled():
            if not already_released:
                self._record(job, "interrupted")
        elif task.exception() is not None:
            logger.error(
                "[自然照片] 后台任务退出（%s）", type(task.exception()).__name__
            )

    async def _send_waiting_reply(self, event, job, message, check_busy=None, *, original_send=None):
        if check_busy is not None:
            await check_busy()
        await self._check_job(job)

        def before_submit():
            if self.closing or global_persona_id(self.context) != job["persona_id"]:
                raise TaskStopped("角色已切换，已停止这次旧角色照片")

        native_send = event.get_extra(NATIVE_CONFIRMED_SEND)
        if native_send is not None:
            async def confirm(original_send, message, before_native_submit):
                return await send_native_private(
                    event, message, before_submit=before_submit, original_send=original_send,
                    before_native_submit=before_native_submit,
                )

            delivery = native_send(message, confirm)
        elif check_busy is not None or event.get_extra("life_delivery_v1") is True:
            before_submit()
            send = event.send if original_send is None else original_send
            delivery = send(message)
        else:
            delivery = send_native_private(
                event, message, before_submit=before_submit, original_send=original_send,
            )
        try:
            accepted = await asyncio.wait_for(delivery, SEND_TIMEOUT)
        except TaskStopped:
            raise
        except Exception as exc:
            logger.warning("[自然照片] 拍照前文字的发送结果未确认（%s）", type(exc).__name__)
            raise PhotoError("等待提示的发送结果未确认，本次拍照已暂停。") from None
        if accepted is False and getattr(event, "delivery_status", None) != "unknown":
            raise PhotoError("等待提示未送达，本次拍照已暂停。")
        if accepted is not True:
            raise PhotoError("等待提示的发送结果未确认，本次拍照已暂停。")
        await self._check_job(job)
        if check_busy is not None:
            await check_busy()

    def _attach_legacy_waiting_sender(self, event, job):
        ack = job["ack"]
        original_send = event.send
        had_override = "send" in vars(event)

        def restore():
            if event.send is send_waiting:
                if had_override:
                    event.send = original_send
                else:
                    del event.send

        async def send_waiting(message):
            if ack.is_set() or job.get("ack_finished"):
                restore()
                return await original_send(message)
            if message is None or message.type in _STATUS_CHAINS or not any(
                isinstance(part, Plain) and part.text.strip() for part in message.chain
            ):
                return await original_send(message)
            restore()
            confirmed = False
            try:
                await self._send_waiting_reply(event, job, message, original_send=original_send)
                confirmed = True
                return True
            except (PhotoError, TaskStopped):
                return False
            finally:
                job["ack_failed"] = not confirmed
                ack.set()

        event.send = send_waiting
        job["restore_waiting_sender"] = restore

    async def photo_generate(
        self,
        event: AstrMessageEvent,
        user_prompt: str,
        photo_type: str = "selfie",
        count: int = 1,
        with_user_image: bool = False,
        aspect_ratio: str = "",
        mode: str = "photo",
        action: str = "generate",
        source_photo_id: str = "",
        shot_plan: list[dict] | None = None,
        use_current_scene: bool | None = None,
        waiting_text: str = "",
    ):
        """应用户要求拍照、画图或修改图片。一次完整说明，一次调用。

        结合当前聊天理解自然要图、穿搭询问和连续拍照，不要求用户写完整摄影提示词。
        用户要照片或改图时可用；话题自然接上眼下的食物、景色或穿搭时可顺手拍一张。
        话题无关、眼下没什么值得拍、用户明确不要照片时只聊天。
        生活照读当前日程；没有可用日程时只按用户要求，不编造当前生活。
        一次一到四张；在同次调用shot_plan逐张安排具体拍法、构图、动作和表情。
        人像新拍或续拍用本次日程心情作底色，结合当前对话和人设，用户指定优先；保留身份和原画风。
        同组保持人物、衣服、环境连续，每张有可见差异；用户只锁一维时其他维度仍可变化。
        再拍、换角度用action=continue承接来源；原图再发用action=resend，免费复用指定成功图。
        按本会话成片的分组标签选精确source_photo_id；明确说哪组第几张时由插件核对，避免把不同组的序号混用。
        原合影续拍沿用两个人，明确更换合影对象时才读取新人物素材。
        修改当前/引用原图用mode=edit；修改刚才的成功图也可指定source_photo_id。
        角色与指定人物合影时 with_user_image=true，优先使用本条新发的人物素材，没有再用本条引用。
        素材可以是真人或虚构人物。用户明确指定紧邻上一条上传的人物素材时也用此选项。
        缺素材时请对方重新发或引用人物图片。
        新拍、续拍或改图时在waiting_text写一条符合人设的单行短话，例如“等着。这就拍给你看。”
        插件会先发这句再开始生成；不要另发同一句，不写旁白、声音标签或拍摄细节。
        原图重发时留空。完成后自然接话，不重复说已经发了，不重复调用或自行重试。

        Args:
            user_prompt(string): 基于聊天理解的简短拍摄意图；补写细节放shot_plan，不能冒充用户原话。
            photo_type(string): selfie自拍、scene场景、food食物、outfit穿搭。
            count(number): 一到四张，默认一张。
            with_user_image(boolean): 是否让角色与本条上传或引用的人物素材合影，支持虚构人物。
            aspect_ratio(string): 仅在用户明确指定比例或横图、正方形等构图时填写；所有新图留空默认3:4。
            mode(string): photo生活照、text普通文字生图、edit修改消息图片。
            action(string): generate新拍，continue承接来源再拍，resend复用一张已成功照片且不调用生图服务。
            source_photo_id(string): 本会话本角色的来源照片ID。指定组内某张时按组标签与序号选对应ID，即使本轮也有新附件。
            use_current_scene(boolean): 仅食物或场景的新拍使用。拍角色当前生活填true，用户另点题材填false。
            shot_plan(list[object]): 与count等长的逐张方案，每项可写capture拍摄方式、framing取景距离、angle角度、action动作、expression具体神态；值为文字，人设与当前聊天优先。
            waiting_text(string): 新生成前由插件先发的一条简短口语，最多60字；原图重发留空。
        """
        unavailable = self._unavailable(event, require_service=action != "resend")
        if unavailable:
            return tool_result(unavailable)
        if event.get_extra(TURN_KEY, False):
            return tool_result("本轮照片要求已经受理，请勿重复调用或自动重试。")
        media_guard = event.get_extra(BUSY_MEDIA_GUARD)
        check_busy = None
        if media_guard is not None:

            async def check_busy():
                try:
                    allowed = callable(media_guard) and await media_guard() is True
                except Exception as exc:
                    logger.warning(
                        "[自然照片] 忙碌补答许可检查失败（%s）", type(exc).__name__
                    )
                    allowed = False
                if not allowed:
                    raise DeliveryCancelled(BUSY_MEDIA_STOPPED)

            try:
                await check_busy()
            except DeliveryCancelled:
                return tool_result(BUSY_MEDIA_STOPPED)
        reserved = False
        handed_over = False
        current_task = asyncio.current_task()
        user = event.get_sender_id()
        try:
            validate_request(
                user_prompt, photo_type, count, with_user_image, aspect_ratio, mode
            )
            if not isinstance(action, str) or action not in {
                "generate",
                "continue",
                "resend",
            }:
                raise ValueError("照片操作应为新拍、继续拍摄或重发原图。")
            if not isinstance(source_photo_id, str) or len(source_photo_id) > 100:
                raise ValueError("请选择当前聊天中可用的照片编号。")
            if use_current_scene is not None and type(use_current_scene) is not bool:
                raise ValueError("是否按当前生活取景应为是或否。")
            if action == "resend":
                waiting_text = ""
            elif not isinstance(waiting_text, str) or len(waiting_text) > 60:
                raise ValueError("拍照前的等待提示请使用不超过60字的一条短话。")
            cleaned_waiting_text = _clean_waiting_text(waiting_text)
            if waiting_text and not cleaned_waiting_text:
                raise ValueError("拍照前的等待提示需要实际文字，不能只有空白或语音标签。")
            waiting_text = cleaned_waiting_text
            specs = parse_shot_plan(shot_plan, count)
            if sum(self.users.values()) >= 16 or self.users.get(user, 0) >= 2:
                raise ValueError("照片正在排队，请等这次完成后再拍。")
            now = time.monotonic()
            key = (event.unified_msg_origin, event.message_obj.message_id)
            self.seen = OrderedDict(
                (k, t) for k, t in self.seen.items() if now - t < 10
            )
            if key[1] and key in self.seen:
                return tool_result("这条消息的照片已经受理，请勿重复调用。")
            event.set_extra(TURN_KEY, True)
            self.users[user] = self.users.get(user, 0) + 1
            self.tasks.add(current_task)
            reserved = True
            job = await self._prepare(
                event,
                user_prompt.strip(),
                photo_type,
                count,
                with_user_image,
                aspect_ratio,
                mode,
                action,
                source_photo_id,
                specs,
                use_current_scene=use_current_scene,
            )
            await self._check_job(job)
            background = (
                check_busy is None
                and action != "resend"
                and self._background_supported(event)
            )
            target = await capture_target(self.context, event) if background else None
            if target is not None and not target.conversation_id:
                target = None
            if target is not None and target.conversation_id != job["scope"].conversation_id:
                raise TaskStopped("聊天已切换，已停止原会话照片")
            if key[1]:
                self.seen[key] = time.monotonic()
            if waiting_text:
                # The SDK runs the post-send hook even when its send raised.
                # Do not resend the same text or start a paid request without a receipt.
                if event.get_extra(LAST_ATTEMPTED_TEXT) == waiting_text:
                    raise PhotoError("等待提示的发送结果未确认，本次拍照已暂停。")
                await self._send_waiting_reply(event, job, event.plain_result(waiting_text), check_busy)
            self._record(job, "queued")
            if target is not None:
                if not waiting_text:
                    job["ack"] = asyncio.Event()
                    event.set_extra("_life_photo_ack", job["ack"])
                    event.set_extra(LEGACY_WAITING_JOB, job)
                    event.set_extra(
                        LEGACY_VOICE_RECEIPT_COUNT, len(event.get_extra("life_voice_delivery_v1", [])),
                    )
                task = asyncio.create_task(self._run_job(job, target=target))
                task.add_done_callback(lambda done: self._worker_done(job, done))
                self.tasks.add(task)
                self.tasks.discard(current_task)
                handed_over = True
                if waiting_text:
                    return tool_result(
                        "已确认发送等待提示，照片正在准备。请自然接话，不重复提示；照片完成后会自动发送。"
                    )
                return tool_result(
                    "照片已开始准备。请用当前人设自然说一句等待的话，可以继续聊天；"
                    "不要说已拍好，不要重复调用。照片完成后会自动发送并请你回应。"
                )
            handed_over = True
            if check_busy is None:
                facts = await self._run_job(job, event=event)
            else:
                facts = await self._run_job(
                    job, event=event, before_send=check_busy,
                    before_generate=check_busy if waiting_text else None,
                )
                if facts["status"] == "stopped" and not facts["sent_count"]:
                    return tool_result(BUSY_MEDIA_STOPPED)
            return tool_result(
                f"本次照片已结束，已确认发送{facts['sent_count']}张，状态{facts['status']}。"
                + (
                    "本次只复用已有文件，没有请求生图服务。"
                    if action == "resend"
                    else ""
                )
                + f"实际操作为{action}。"
                + (f"照片模式为{job['mode']}。" if action != "resend" else "")
                + "成功照片内部编号："
                + json.dumps(facts.get("photo_ids", []), ensure_ascii=False)
                + "。"
                + "unknown表示发送结果不确定，不能说发送失败或让用户重试。"
                "请按实际结果自然回应一次，不要重发照片或自动重试。"
                f"{CAPTION_GUIDANCE}"
                "不要向用户展示内部编号或技术过程。"
            )
        except DeliveryCancelled:
            return tool_result(BUSY_MEDIA_STOPPED)
        except (ValueError, PhotoError, OSError, TaskStopped) as exc:
            message = (
                str(exc)
                if not isinstance(exc, OSError)
                else "参考图片读取失败，请重新上传或发送。"
            )
            self.last_error = message
            return tool_result(message)
        finally:
            if reserved and not handed_over:
                self.users[user] -= 1
                if not self.users[user]:
                    del self.users[user]
                self.tasks.discard(current_task)

    async def acknowledge_waiting_reply(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is not None and result.type not in _STATUS_CHAINS and result.chain:
            text = "".join(part.text for part in result.chain if isinstance(part, Plain))
            if text:
                event.set_extra(LAST_ATTEMPTED_TEXT, _clean_waiting_text(text))

    async def acknowledge_voice_reply(
        self, event: AstrMessageEvent, tool, arguments, result
    ):
        ack = event.get_extra("_life_photo_ack")
        if (ack is not None and len(event.get_extra("life_voice_delivery_v1", []))
                > event.get_extra(LEGACY_VOICE_RECEIPT_COUNT, 0)):
            ack.set()

    async def life_photo_status(self, event: AstrMessageEvent):
        if event.is_private_chat():
            text = "自然照片 v0.1.18\n" + (self._unavailable(event) or "照片服务已配置")
            if self.last_error:
                text += "\n最近问题：" + self.last_error
            yield event.plain_result(text)


def configure_photo_tool_schema(tool):
    """Complete the registered photo tool schema before provider serialization."""
    tool.parameters["required"] = ["user_prompt", "waiting_text"]
    tool.parameters["properties"]["waiting_text"]["description"] = (
        "开始生成或处理图片前先发给用户的简短自然过渡话，必填，最多60字。"
        "必须符合角色人设口吻，例如“等着。这就拍给你看。”这类短话。严格单行、零换行，中文零空格，"
        "英文保留必要词间空格。严禁包含[轻笑]等语音标签，严禁使用“请稍等”或“正在生成图片”等AI客服套话。"
        "原图重发时填空字符串。"
    )
    tool.parameters["properties"]["shot_plan"]["items"] = {
        "type": "object",
        "properties": {
            "capture": {
                "type": "string",
                "description": "角色自行拍摄的合理方式，如手持、对镜、固定手机定时；用户指定方式优先。",
            },
            "framing": {
                "type": "string",
                "description": "本张明确取景距离及范围，如近景、半身、全身；只锁用户要求的范围。",
            },
            "angle": {
                "type": "string",
                "description": "本张具体机位方向和角度，与未被用户固定的其它张有可见区别。",
            },
            "action": {
                "type": "string",
                "description": "符合当前环境、角色和聊天的具体拍照小动作，不改变日程或套固定男女动作。",
            },
            "expression": {
                "type": "string",
                "description": f"结合人设、日程心情和当前聊天写具体神态。{REACTION_GUIDANCE}{SMILE_GUIDANCE}",
            },
        },
        "additionalProperties": False,
    }
