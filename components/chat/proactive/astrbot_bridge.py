"""AstrBot 对话接入、私聊身份与平台发送回执。"""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    bind_checkpoint_messages,
    get_checkpoint_id,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.utils.session_lock import session_lock_manager

from .delivery_error import ConversationGone
from .delivery_error import DeliveryError as ProactiveSendError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .triggers import Trigger


QQ_OFFICIAL = {"qq_official", "qq_official_webhook"}
SUPPORTED_PLATFORMS = {"aiocqhttp", "weixin_oc", *QQ_OFFICIAL}
_TARGET = re.compile(r"([^:,\s]+):FriendMessage:([^:,\s]+)")
_ACCOUNT_ID = re.compile(r"[1-9][0-9]*")
_MESSAGE_ID = re.compile(r"-?[1-9][0-9]*")
_SLOT_FIELDS = ("start", "end", "activity", "location", "mood", "phone", "outfit")
_PROTOCOL = """请仅输出一个JSON对象，不要加代码块，不执行任何工具：
{"medium":"text","text":"实际要说的话","photo":null,"promise_evidence":null}
硬性约束：
1. 若最近已说晚安、睡了、去忙、稍后聊，且当前状态与那时相比没有新的自然分享点，必须输出 "medium":"skip"。若已经说过、没有具体新内容、最近还在同一话题，或只有日程切换、早晚时刻、沉默可当理由，也选择 skip。
2. 若上一条消息就是你发的主动消息且对方尚未回复，除非跨越了全新时段且有完全不同的新生活事件，否则优先输出 "medium":"skip"，严禁催促或连续刷屏。沉默本身没有情绪，不问怎么不回。不把一个“嗯”当作结束。对方明确要自己回来找你、正在睡、或不想接话时尊重边界。
3. "text" 字段只写角色实际要发的内容，并按媒介区分规范：当 "medium" 为 "text" 时，严格遵循人设卡的普通文字输出规范（如人设规定的零空格、零换行、日常零逗号、用句号切分短气泡等），不含任何声音标签；当 "medium" 为 "voice" 时，切换为真人口头语音规范，使用句内正常以逗号（，）控制呼吸停顿的连贯口语长句，不套用普通文字的短气泡标点规则，可带1~2个如[轻笑]、[叹气]、[耳语]、[打哈欠]的声音标签。
4. "medium" 只能为 "text"、"voice" 或 "skip"。本轮允许语音时，情绪明显、适合用声音传达，或者有些话用嘴说比打字自然，才用 voice。"medium" 为 "text" 时可按需附带 "photo"（须本轮允许照片且符合当前真实状态）；"medium" 为 "voice" 或 "skip" 时 "photo" 必须为 null。"medium" 为 "skip" 时 "text" 填空字符串。
本轮允许照片且正在做的事有具体的视觉内容可以分享时，photo 使用现有对象格式：
{"type":"selfie","brief":"可以真实拍摄的内容与简短主题","shot_plan":{"capture":"拍摄方式","framing":"景别与构图","angle":"机位与角度","action":"动作","expression":"表情"}}
photo.type 只可用 selfie、scene、food、outfit。带照片时只配一句简短文字，别重复画面里的内容。不能把未生成、未送达的照片说成已发出。
只有本轮针对到期告知，且现有事实能确认要告知的结果，才在 promise_evidence 填写与结果对应、逐字出现在 text 中的短句。约定原文仅说明答应了什么；如果约定本身是此时联系对方（例如答应明早找对方），发出的开场就是履约，填写开场中逐字出现的短句。其他计划、预期、缺失或无法核实的结果填 null。生活事实中的全天摘要和后续安排包含未来计划，不能据此判断活动已经发生或约定已经完成。描述未来活动时，用“计划”或“准备”的语气。
输出无代码围栏、无额外说明。"""


def _life_facts(state: dict) -> dict:
    facts = {
        key: state[key]
        for key in ("now", "profile_summary", "day_summary")
        if state.get(key)
    }
    for key in ("current_slot", "next_slot"):
        if slot := state.get(key):
            facts[key] = {name: slot[name] for name in _SLOT_FIELDS if name in slot}
    return facts


def _recall_query(history: list[dict], state: dict, trigger: Trigger) -> str:
    for item in reversed(history):
        if item.get("role") != "user" or item.get("_no_save"):
            continue
        content = item.get("content")
        if isinstance(content, list):
            content = " ".join(
                part["text"]
                for part in content
                if part.get("type") == "text" and not part.get("_no_save")
            )
        if isinstance(content, str) and content.strip():
            return content.strip()

    activity = (state.get("current_slot") or {}).get("activity")
    return " ".join(filter(None, (trigger.reason, trigger.promise or activity)))


class _MediaEvent(AstrMessageEvent):
    """为媒体插件提供私聊上下文，无入站消息和聊天管线派发。"""

    def __init__(
        self,
        bridge: AstrBotBridge,
        platform,
        recipient: str,
        bot_self_id: str,
        before_send,
        *,
        supports_voice: bool,
        supports_photo: bool,
    ):
        meta = platform.meta()
        self._platform_name = meta.name
        self._account_id = bot_self_id
        self.supports_voice = supports_voice
        self.supports_photo = supports_photo
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = (
            bot_self_id if meta.name == "aiocqhttp"
            else meta.id if meta.name == "weixin_oc" else "unknown_selfid"
        )
        message.session_id = recipient
        message.message_id = f"life-proactive-{uuid4().hex}"
        message.timestamp = int(time.time())
        message.message_str = ""
        message.message = []
        message.raw_message = None
        message.sender = MessageMember(user_id=recipient, nickname=recipient)
        super().__init__("", message, meta, recipient)
        if meta.name == "aiocqhttp":
            self.bot = (
                SimpleNamespace(send_private_msg=self._send_direct)
                if before_send is not None else platform.get_client()
            )
        self.set_extra("life_delivery_v1", True)
        if meta.name != "aiocqhttp" and supports_photo and before_send is not None:
            self.set_extra("life_photo_ack_v1", True)
        self._before_send = before_send
        self._bridge = bridge
        self.delivery_status: str | None = None
        self.platform_message_id: str | None = None
        if recipient in bridge.context.get_config(bridge.target_umo).get(
            "admins_id", []
        ):
            self.role = "admin"

    async def _send_direct(self, *, user_id, self_id, message):
        if str(user_id) != self.get_sender_id() or str(self_id) != self.get_self_id():
            raise ProactiveSendError("failed", "照片请求与已绑定私聊不一致")
        try:
            result = await self._bridge._send_payload(
                message,
                bot_self_id=self.get_self_id(),
                before_send=self._before_send,
                on_attempt=self._mark_attempt,
            )
        except ProactiveSendError as exc:
            self.delivery_status = exc.status
            raise
        self.platform_message_id = str(result["message_id"])
        self.delivery_status = "sent"
        return result

    def _mark_attempt(self):
        self.delivery_status = "unknown"

    async def send(self, message: MessageChain) -> bool:
        self.delivery_status = None
        self.platform_message_id = None
        try:
            self.platform_message_id = await self._bridge._send_chain(
                message,
                bot_self_id=self._account_id,
                platform_name=self._platform_name,
                before_send=self._before_send,
                on_attempt=self._mark_attempt,
            )
        except ProactiveSendError as exc:
            self.delivery_status = (
                "cancelled" if exc.status == "stopped" and self._platform_name != "aiocqhttp"
                else exc.status
            )
            if exc.status in {"failed", "stopped"}:
                return False
            raise
        self.delivery_status = "sent"
        self._has_send_oper = True
        return True


class AstrBotBridge:
    def __init__(self, context, target_umo: str):
        self.context = context
        self.target_umo = target_umo
        self._target = _TARGET.fullmatch(target_umo)

    def _platform(self):
        if self._target is None:
            return None
        platform = self.context.get_platform_inst(self._target[1])
        if (
            platform is not None
            and platform.meta().name in SUPPORTED_PLATFORMS
            and platform.meta().id == self._target[1]
            and platform.status == PlatformStatus.RUNNING
        ):
            return platform
        return None

    def _transport(self, platform, bot_self_id):
        recipient = self._target[2]
        if platform.meta().name in QQ_OFFICIAL:
            from .qq_official_transport import QQOfficialTransport

            return QQOfficialTransport(platform, recipient, bot_self_id)
        if platform.meta().name == "weixin_oc":
            from .weixin_transport import WeixinTransport

            return WeixinTransport(platform, recipient, bot_self_id)
        return None

    def target_available(self, bot_self_id=None, platform_name=None) -> bool:
        platform = self._platform()
        if platform is None or (platform_name and platform.meta().name != platform_name):
            return False
        if platform.meta().name == "aiocqhttp":
            return bool(
                _ACCOUNT_ID.fullmatch(self._target[2])
                and (bot_self_id is None or _ACCOUNT_ID.fullmatch(bot_self_id))
            )
        return bool(bot_self_id and self._transport(platform, bot_self_id).available())

    async def current_conversation_id(self) -> str | None:
        return await self.context.conversation_manager.get_curr_conversation_id(
            self.target_umo
        )

    async def _conversation(self, conversation_id: str):
        conversation = await self.context.conversation_manager.get_conversation(
            self.target_umo, conversation_id
        )
        # The SDK looks up the ID globally, even when given a session.
        if conversation is not None and conversation.user_id == self.target_umo:
            return conversation
        return None

    @staticmethod
    def _history(conversation) -> list[dict]:
        history = conversation.history
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            raise ValueError("主动消息无法读取当前对话历史")
        return history

    async def _persona(self, conversation):
        config = self.context.get_config(self.target_umo)
        platform = self._platform()
        (
            persona_id,
            persona,
            _,
            _,
        ) = await self.context.persona_manager.resolve_selected_persona(
            umo=self.target_umo,
            conversation_persona_id=conversation.persona_id,
            platform_name=platform.meta().name if platform else "",
            provider_settings=config.get("provider_settings", {}),
        )
        return persona_id, persona

    async def persona_matches(self, conversation_id: str, persona_id: str) -> bool:
        if not persona_id:
            return False
        conversation = await self._conversation(conversation_id)
        if conversation is None:
            return False
        active_id, persona = await self._persona(conversation)
        return persona is not None and active_id == persona_id

    async def generate(
        self,
        state: dict,
        trigger: Trigger,
        prompt: str,
        allowed_media: dict,
        *,
        conversation_id: str | None = None,
        event: AstrMessageEvent | None = None,
        recall: Callable[[AstrMessageEvent, ProviderRequest], Awaitable[None]]
        | None = None,
    ) -> str:
        conversation_id = conversation_id or await self.current_conversation_id()
        if not conversation_id:
            raise ValueError("主动消息需要已有的私聊对话")
        conversation = await self._conversation(conversation_id)
        if conversation is None:
            raise ValueError("主动消息找不到目标私聊对话")
        persona_id, persona = await self._persona(conversation)
        if persona is None or not persona_id or persona_id != state.get("persona_id"):
            raise ValueError("私聊当前人设与日程人设不一致")

        history = self._history(conversation)
        system_prompt = "\n\n".join((persona["prompt"], prompt, _PROTOCOL))
        extra = {}
        if recall is not None:
            if (
                event is None
                or event.unified_msg_origin != self.target_umo
                or event.get_extra("life_delivery_v1") is not True
            ):
                raise ValueError("主动召回需要目标私聊的专用事件")
            if await self.current_conversation_id() != conversation_id:
                raise ValueError("主动召回前目标私聊已切换对话")
            request = ProviderRequest(
                prompt=_recall_query(history, state, trigger),
                system_prompt=system_prompt,
                contexts=deepcopy(history),
                conversation=conversation,
                session_id=self.target_umo,
            )
            original_text = event.message_str
            original_message_text = event.message_obj.message_str
            try:
                # LivingMemory queries the event text. The delivery marker blocks a user write.
                event.message_str = event.message_obj.message_str = request.prompt
                await recall(event, request)
            finally:
                event.message_str = original_text
                event.message_obj.message_str = original_message_text
            if (
                not await self.persona_matches(conversation_id, persona_id)
                or await self.current_conversation_id() != conversation_id
            ):
                raise ValueError("主动召回期间目标私聊对话或人设已变化")
            history = request.contexts
            system_prompt = request.system_prompt
            if request.extra_user_content_parts:
                extra["extra_user_content_parts"] = request.extra_user_content_parts

        contexts = bind_checkpoint_messages(history)
        if examples := persona.get("_begin_dialogs_processed"):
            contexts[:0] = deepcopy(examples)
        task = {
            "触发原因": trigger.reason,
            "生活事实": _life_facts(state),
            "本轮允许语音": allowed_media["voice"],
            "本轮允许照片": allowed_media["photo"],
        }
        if trigger.promise:
            task["到期告知约定原文"] = trigger.promise
        if trigger.fact:
            task["本轮具体新事实"] = trigger.fact
        if trigger.key.startswith("mood:"):
            task["心情表达"] = (
                "参考本轮心情背景，让它从语气、想起的事或自然想说的话里流露出来。"
                "别直接宣布‘我心情很好’或‘我心情不好’，也别为了开口编造具体事件。"
            )
        provider_id = await self.context.get_current_chat_provider_id(self.target_umo)
        result = await self.context.llm_generate(
            chat_provider_id=provider_id,
            tools=None,
            contexts=contexts,
            system_prompt=system_prompt,
            prompt=json.dumps(task, ensure_ascii=False),
            **extra,
        )
        if (
            result.role != "assistant"
            or result.tools_call_name
            or not result.completion_text
        ):
            raise ValueError("主动消息模型没有返回可用的文本方案")
        return result.completion_text

    async def _send_chain(
        self,
        message: MessageChain,
        *,
        bot_self_id: str,
        platform_name: str | None = None,
        before_send=None,
        on_attempt=None,
    ) -> str:
        platform = self._platform()
        if platform is None or not self.target_available(bot_self_id, platform_name):
            raise ProactiveSendError("failed", "目标私聊平台或绑定账号不可用")
        transport = self._transport(platform, bot_self_id)
        if transport is not None:
            return await transport.send(message, before_send=before_send, on_attempt=on_attempt)

        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        try:
            segments = await AiocqhttpMessageEvent._parse_onebot_json(message)
        except Exception as exc:
            raise ProactiveSendError("failed", "无法准备 QQ 消息内容") from exc
        if not segments:
            raise ProactiveSendError("failed", "QQ 消息内容为空")

        result = await self._send_payload(
            segments,
            bot_self_id=bot_self_id,
            before_send=before_send,
            on_attempt=on_attempt,
        )
        return str(result["message_id"])

    async def _send_payload(self, segments, *, bot_self_id, before_send, on_attempt):
        from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable

        if before_send is not None and not await before_send():
            raise ProactiveSendError("stopped", "当前条件已变化，停止发送")
        platform = self._platform()
        if platform is None or platform.meta().name != "aiocqhttp":
            raise ProactiveSendError("failed", "目标 OneBot 私聊平台不可用")
        client = platform.get_client()
        if on_attempt is not None:
            on_attempt()
        try:
            result = await client.send_private_msg(
                user_id=int(self._target[2]), self_id=int(bot_self_id), message=segments
            )
        except (ApiNotAvailable, ActionFailed) as exc:
            raise ProactiveSendError("failed", "QQ 接口未能发送消息") from exc
        except Exception as exc:
            # Once handed to OneBot, a lost response cannot rule out delivery.
            raise ProactiveSendError("unknown", "QQ 发送结果未知") from exc
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if _MESSAGE_ID.fullmatch(str(message_id)) is None:
            raise ProactiveSendError("unknown", "QQ 接口未返回有效消息编号")
        return result

    async def send_text(
        self, text: str, *, bot_self_id: str, platform_name: str | None = None, before_send=None
    ) -> dict:
        try:
            message_id = await self._send_chain(
                MessageChain([Plain(text=text)]),
                bot_self_id=bot_self_id,
                platform_name=platform_name,
                before_send=before_send,
            )
        except ProactiveSendError as exc:
            return {"status": exc.status, "reason": str(exc)}
        return {
            "status": "sent",
            "medium": "text",
            "text": text,
            "platform_message_id": message_id,
        }

    def build_media_event(
        self, bot_self_id: str, *, platform_name: str | None = None, before_send=None
    ) -> AstrMessageEvent:
        platform = self._platform()
        if platform is None or not self.target_available(bot_self_id, platform_name):
            raise ProactiveSendError("failed", "目标私聊平台或绑定账号不可用")
        transport = self._transport(platform, bot_self_id)
        text_only = platform.meta().name == "weixin_oc"
        return _MediaEvent(
            self, platform, self._target[2], bot_self_id, before_send,
            supports_voice=not text_only and (transport is None or transport.supports_voice),
            supports_photo=not text_only and (transport is None or transport.supports_photo),
        )

    async def record(
        self, turn_id: str, deliveries: list[dict], conversation_id: str
    ) -> None:
        delivered = []
        for item in deliveries:
            if item["status"] != "sent":
                continue
            text = item.get("text", "")
            if item["medium"] == "photo":
                text = f"[照片] {text}".rstrip()
            elif item["medium"] == "voice":
                text = f"[语音] {text}"
            delivered.append(AssistantMessageSegment(content=text).model_dump())
        if not delivered:
            return

        checkpoint = f"life-proactive:{turn_id}"
        async with session_lock_manager.acquire_lock(self.target_umo):
            conversation = await self._conversation(conversation_id)
            if conversation is None:
                raise ConversationGone("已送达消息的原私聊记录已删除或不可用")
            history = self._history(conversation)
            if any(get_checkpoint_id(entry) == checkpoint for entry in history):
                return
            # Native checkpoints stay in history through ordinary chat saves.
            await self.context.conversation_manager.update_conversation(
                self.target_umo,
                conversation_id,
                history=[
                    *history,
                    *delivered,
                    {"role": "_checkpoint", "content": {"id": checkpoint}},
                ],
            )
            updated = await self._conversation(conversation_id)
            if updated is None:
                raise ConversationGone("保存已送达消息时原对话已删除")
            if not any(
                get_checkpoint_id(entry) == checkpoint
                for entry in self._history(updated)
            ):
                raise RuntimeError("主动消息历史未保存，等待重试")
