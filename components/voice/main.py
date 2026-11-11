"""自然语音插件入口。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, StarTools

from ..common.contracts import VOICE_DELIVERY
from ..common.proxy import aiohttp_proxy
from .core.compat import finish_direct_tool_delivery
from .core.config import Settings
from .core.provider import BreezeClient, VoiceError, cleanup_cache
from .core.text import MAX_TEXT_LENGTH, prepare_text, split_voice_tag_chunk, strip_voice_tags
from .core.transport import (
    SendCancelled,
    send_native_private,
    supports_native_target,
    uses_confirmed_transport,
)
from .core.voice_options import populate_voice_options
from .core.voices import BreezeVoiceCatalog

PLUGIN_ID = "astrbot_plugin_life_voice"
TOOL_NAME = "tts_speak"
TURN_KEY = "_life_voice_turn"
DELIVERY_KEY = VOICE_DELIVERY
STREAM_FILTER_KEY = "_life_voice_stream_filtered"
_PLATFORM_CANCELLED_STATUSES = ("stopped", "cancelled")
_DUPLICATE_SPEECH = "本事件已发送相同台词"


@dataclass
class VoiceTurn:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delivered: set[str] = field(default_factory=set)
    proactive_unknown: set[str] = field(default_factory=set)


def _voice_turn(event: AstrMessageEvent) -> VoiceTurn:
    turn = event.get_extra(TURN_KEY)
    if turn is None:
        turn = VoiceTurn()
        event.set_extra(TURN_KEY, turn)
    return turn


def _record_delivery(event, turn, signature, text, medium) -> None:
    turn.delivered.add(signature)
    # 只公布平台发送接口已经确认交付的正文。
    deliveries = event.get_extra(DELIVERY_KEY)
    if deliveries is None:
        deliveries = []
    deliveries.append({"id": signature, "text": text, "medium": medium})
    event.set_extra(DELIVERY_KEY, deliveries)


def _undelivered(status: str, error: str = "") -> dict:
    result = {"status": status, "deliveries": []}
    if error:
        result["error"] = error
    return result


async def _clean_visible_stream(stream):
    pending = None
    async for chunk in stream:
        if chunk.type is not None:
            # 工具分段、推理和音频等有类型的消息保持原样。
            if pending:
                yield replace(pending[0], chain=[pending[1]])
                pending = None
            yield chunk
            continue
        visible = []
        for part in chunk.chain:
            if not isinstance(part, Plain):
                if pending:
                    visible.append(pending[1])
                    pending = None
                visible.append(part)
                continue
            prefix = pending[1].text if pending else ""
            text, tail = split_voice_tag_chunk(prefix + part.text)
            pending = None
            if tail:
                # Plain 的浅拷贝共享字段，缓冲和输出各自构造独立组件。
                pending = (chunk, Plain(text=tail))
            if text:
                visible.append(Plain(text=text))
        if visible or not chunk.chain:
            yield replace(chunk, chain=visible)
    if pending:
        yield replace(pending[0], chain=[pending[1]])


class LifeVoicePlugin:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.settings = Settings.from_config(config)
        proxy = aiohttp_proxy(context, self.settings.base_url)
        self.audio_dir = Path(StarTools.get_data_dir(PLUGIN_ID)) / "audio"
        self.voice_catalog = BreezeVoiceCatalog(base_url=self.settings.base_url, proxy=proxy)
        self.client = (
            BreezeClient(
                self.settings.api_key,
                self.settings.voice_id,
                self.audio_dir,
                base_url=self.settings.base_url,
                proxy=proxy,
            )
            if self.settings.configured
            else None
        )
        self.last_error = ""
        self._closing = False
        self._active: set[asyncio.Task] = set()

    async def initialize(self):
        removed = await asyncio.to_thread(cleanup_cache, self.audio_dir)
        if removed:
            logger.info("[自然语音] 已清理 %d 个超过 7 天的语音缓存。", removed)
        issue = await populate_voice_options(self.config.schema, self.settings, self.voice_catalog)
        if issue:
            logger.warning("[自然语音] 音色列表加载失败：%s", issue)

    async def terminate(self):
        self._closing = True
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.close()
        await self.voice_catalog.close()

    def _unavailable(self, event: AstrMessageEvent) -> str:
        if not event.is_private_chat():
            return "语音工具仅用于私聊，请用普通文字回复。"
        if self._closing or not self.settings.enabled:
            return "语音插件当前已关闭，请用普通文字回复。"
        if not supports_native_target(event):
            return "QQ 官方语音仅用于与机器人的私聊，请用普通文字回复。"
        if not self.settings.configured:
            return "请先在自然语音设置中填写 BreezeBlue 密钥并选择音色。"
        return ""

    def _require_open(self) -> None:
        if self._closing:
            raise SendCancelled

    async def on_llm_request(self, event: AstrMessageEvent, request):
        if (
            self.settings.enabled
            and not self._closing
            and event.is_private_chat()
            and not event.get_extra(STREAM_FILTER_KEY)
        ):
            # 原生流式绕过 on_decorating_result，在当前事件的适配器入口清理。
            send_streaming = event.send_streaming
            had_override = "send_streaming" in vars(event)

            async def send_clean_stream(generator, use_fallback=False):
                try:
                    async with (
                        aclosing(generator),
                        aclosing(_clean_visible_stream(generator)) as clean,
                    ):
                        await send_streaming(clean, use_fallback)
                finally:
                    if event.send_streaming is send_clean_stream:
                        if had_override:
                            event.send_streaming = send_streaming
                        else:
                            del event.send_streaming
                    event.set_extra(STREAM_FILTER_KEY, False)

            event.send_streaming = send_clean_stream
            event.set_extra(STREAM_FILTER_KEY, True)
        if self._unavailable(event) and request.func_tool is not None:
            # remove_tool 替换列表；复制容器，不能改其他会话共享的工具集合。
            request.func_tool = copy.copy(request.func_tool)
            request.func_tool.remove_tool(TOOL_NAME)

    async def _synthesize_speech(self, event: AstrMessageEvent, text: str) -> Path:
        if len(text) > MAX_TEXT_LENGTH:
            raise VoiceError("这段话超过语音服务的长度限制，已完整改发文字")
        config = self.context.get_config(event.unified_msg_origin)
        tool_timeout = config.get("agent_runner", {}).get("config", {}).get("misc", {}).get("tool_call_timeout", 120)
        # 在核心工具超时前留出发送正文的时间，不改用户的全局设置。
        timeout = min(90.0, max(0.1, float(tool_timeout) * 0.8))
        try:
            return await asyncio.wait_for(self.client.synthesize(text), timeout=timeout)
        except asyncio.TimeoutError:
            raise VoiceError("语音等待超时，已改发文字；可稍后查看生成记录") from None

    async def _send_speech(self, event: AstrMessageEvent, text: str) -> str | None:
        unavailable = self._unavailable(event)
        if unavailable:
            return unavailable
        speech = prepare_text(text)
        if not speech.display_text:
            return "没有可朗读的正文，请用完整的话回复，不要只写声音标签。"
        turn = _voice_turn(event)
        # 同一轮相同台词只发送一次；另一条用户消息有独立状态。
        signature = hashlib.sha256(speech.display_text.encode("utf-8")).hexdigest()
        task = asyncio.current_task()
        self._active.add(task)
        try:
            async with turn.lock:
                self._require_open()
                if signature in turn.delivered:
                    event.clear_result()
                    return None
                medium = "text"
                try:
                    path = await self._synthesize_speech(event, speech.tts_text)
                except (VoiceError, OSError) as exc:
                    self._require_open()
                    self.last_error = str(exc) if isinstance(exc, VoiceError) else "音频文件保存失败"
                    logger.warning("[自然语音] %s；本条改发文字", self.last_error)
                    await event.send(event.plain_result(speech.display_text))
                else:
                    self._require_open()
                    try:
                        await event.send(event.chain_result([Record(file=str(path))]))
                    except Exception as exc:
                        self._require_open()
                        # 平台发送失败时明确记录，并把原话改发文字；不重新生成音频。
                        self.last_error = f"语音发送失败（{type(exc).__name__}）"
                        logger.warning("[自然语音] %s；本条改发文字", self.last_error)
                        await event.send(event.plain_result(speech.display_text))
                    else:
                        self.last_error = ""
                        medium = "voice"
                _record_delivery(event, turn, signature, speech.display_text, medium)
                event.clear_result()
                return None
        except SendCancelled:
            event.clear_result()
            event.stop_event()
            return None
        finally:
            self._active.discard(task)

    async def send_proactive_voice(
        self, event: AstrMessageEvent, text: str, *, before_send: Callable[[], Awaitable[bool]]
    ) -> dict:
        """发送主动消息的一条完整台词，回报本次确认的交付。"""
        job = asyncio.create_task(self._send_guarded_voice(event, text, before_send))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            raise

    async def _send_confirmed_native(self, event, message):
        return await send_native_private(event, message, before_submit=self._require_open)

    async def _send_guarded_voice(self, event, text, before_send, *, native=False) -> dict:
        if self._closing:
            return _undelivered("cancelled")
        unavailable = self._unavailable(event)
        if unavailable:
            return _undelivered("failed", unavailable)
        try:
            speech = prepare_text(text)
        except ValueError as exc:
            return _undelivered("failed", str(exc))
        if not speech.display_text:
            return _undelivered("failed", "没有可朗读的正文")
        turn = _voice_turn(event)
        signature = hashlib.sha256(speech.display_text.encode("utf-8")).hexdigest()
        task = asyncio.current_task()
        self._active.add(task)
        try:
            async with turn.lock:
                if signature in turn.proactive_unknown:
                    return _undelivered("unknown", "本事件同一台词的发送结果未确认")
                if signature in turn.delivered:
                    return _undelivered("cancelled", _DUPLICATE_SPEECH)
                try:
                    if self._closing or (not native and (not await before_send() or self._closing)):
                        return _undelivered("cancelled")
                    medium = "text"
                    message = event.plain_result(speech.display_text)
                    try:
                        path = await self._synthesize_speech(event, speech.tts_text)
                    except (VoiceError, OSError):
                        pass  # 合成失败，发送已经清理好的完整正文。
                    else:
                        medium = "voice"
                        message = event.chain_result([Record(file=str(path))])
                    if self._closing or (not native and (not await before_send() or self._closing)):
                        return _undelivered("cancelled")
                except Exception as exc:
                    logger.warning("[自然语音] 语音准备失败（%s）", type(exc).__name__)
                    return _undelivered("failed", "语音准备失败")

                try:
                    if native:
                        accepted = await self._send_confirmed_native(event, message)
                        if (
                            accepted is False
                            and medium == "voice"
                            and getattr(event, "delivery_status", None) not in _PLATFORM_CANCELLED_STATUSES
                            and not self._closing
                        ):
                            # 只有明确未发出语音才回退。缺回执或出网异常不可重发。
                            medium = "text"
                            accepted = await self._send_confirmed_native(
                                event,
                                event.plain_result(speech.display_text),
                            )
                    else:
                        accepted = await event.send(message)
                except SendCancelled:
                    return _undelivered("cancelled")
                except asyncio.CancelledError:
                    turn.proactive_unknown.add(signature)
                    return _undelivered("unknown", "平台发送结果未确认")
                except Exception as exc:
                    if getattr(event, "delivery_status", None) not in (
                        *_PLATFORM_CANCELLED_STATUSES,
                        "failed",
                    ):
                        turn.proactive_unknown.add(signature)
                        logger.warning("[自然语音] 平台发送结果未确认（%s）", type(exc).__name__)
                        return _undelivered("unknown", "平台发送结果未确认")
                    accepted = False
                if accepted is False:
                    if self._closing or getattr(event, "delivery_status", None) in _PLATFORM_CANCELLED_STATUSES:
                        return _undelivered("cancelled")
                    return _undelivered("failed", "平台未发送消息")
                if accepted is not True:
                    turn.proactive_unknown.add(signature)
                    return _undelivered("unknown", "平台未返回发送确认")

                _record_delivery(event, turn, signature, speech.display_text, medium)
                return {
                    "status": "sent",
                    "deliveries": [{"medium": medium, "text": speech.display_text}],
                }
        except asyncio.CancelledError:
            return _undelivered("cancelled")
        finally:
            self._active.discard(task)

    async def tts_speak(self, event: AstrMessageEvent, text: str):
        """给当前私聊发送一条完整的语音。

        每轮只选一种：要么只发普通文字，要么只调用本工具发语音。调用时主回复的普通文字留空，不另写同样内容的文字或预告。
        看 <character_life> 里你正在做什么：手上占着、在路上、刚睡醒，或者这句话用嘴说更自然时适合发语音，台词里可以顺口带出手边的事；在需要安静的地方优先打字，对方坚持想听时，用 [耳语] 压低声音简短说一句。
        台词是你随口说出来的一段连贯口语，句内正常用逗号（，）换气停顿，不要用一串句号把话切碎。
        最多用两个声音标签，只写在 text 参数里。日常常用：[笑] [轻笑] [咯咯笑] [叹气] [耳语] [停顿] [清嗓] [吸气] [呼气] [吸鼻子] [打哈欠] [哼歌] [惊呼] [哼鼻子] [啧嘴]；情绪真的很重时才用 [抽泣] [哭声]。
        语音合成失败时会自动改发正文。

        Args:
            text(string): 一条语音要说的完整连贯口语台词（句内用逗号自然换气），可含声音标签，最多1000字。
        """
        if uses_confirmed_transport(event):
            receipt = await self._send_guarded_voice(event, text, None, native=True)
            self.last_error = receipt.get("error", "")
            event.clear_result()
            status = receipt["status"]
            if status == "sent":
                if receipt["deliveries"][0]["medium"] == "voice":
                    tool_receipt = "语音已发送。"
                else:
                    tool_receipt = "语音未能发送，已改发文字。"
                    logger.warning("[自然语音] %s", tool_receipt)
            elif status == "cancelled" and event.get_extra(DELIVERY_KEY):
                tool_receipt = (
                    "本轮已发送相同内容，已跳过重复发送。" if receipt.get("error") == _DUPLICATE_SPEECH else None
                )
            else:
                event.stop_event()
                yield None
                return
            yield finish_direct_tool_delivery(event, TOOL_NAME, tool_receipt)
            return

        message = await self._send_speech(event, text)
        if message is None and not event.is_stopped():
            # 兼容旧发送接口，它不保证返回交付确认。
            finish_direct_tool_delivery(event, TOOL_NAME, None)
        yield message

    async def life_voice_test(self, event: AstrMessageEvent, *, text: str = ""):
        text = text or "这是一条语音试听。"
        if uses_confirmed_transport(event):
            unavailable = self._unavailable(event)
            if unavailable:
                yield event.plain_result(unavailable)
                return
            receipt = await self._send_guarded_voice(event, text, None, native=True)
            self.last_error = receipt.get("error", "")
            event.clear_result()
            return
        message = await self._send_speech(event, text)
        if message:
            yield event.plain_result(message)

    async def life_voice_status(self, event: AstrMessageEvent):
        if not event.is_private_chat():
            return
        text = (
            "自然语音\n"
            f"启用：{'是' if self.settings.enabled else '否'}\n"
            f"语音设置：{'已填写' if self.settings.configured else '待填写密钥或选择音色'}"
        )
        if self.last_error:
            text += f"\n最近问题：{self.last_error}"
        yield event.plain_result(text)

    async def clean_visible_tags(self, event: AstrMessageEvent):
        if not self.settings.enabled or not event.is_private_chat():
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        turn = event.get_extra(TURN_KEY)
        if turn is not None and turn.delivered:
            # 核心若启用了中间文字缓冲，会在工具结束后释放；本轮已有交付则去重。
            result.chain = [item for item in result.chain if not isinstance(item, Plain)]
            return
        for component in result.chain:
            if isinstance(component, Plain):
                component.text = strip_voice_tags(component.text)
