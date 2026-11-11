"""One AstrBot entry point for the Natural Life components."""

import asyncio
from contextlib import aclosing
from copy import copy
from functools import partial
from threading import RLock

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core import sp
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.custom_filter import CustomFilter

from .components.chat.main import AlreadyAwakePrivate, LifeChat
from .components.photo.main import LifePhotoPlugin, configure_photo_tool_schema
from .components.qzone.main import QzoneLifePlugin
from .components.schedule.main import LifeSchedulerPlus
from .components.voice.main import LifeVoicePlugin
from .config import ComponentConfig
from .dashboard import NaturalLifeDashboard
from .migrate import migrate_config
from .runtime import (
    CHAT_ID,
    LEGACY_IDS,
    PHOTO_ID,
    QQ_OFFICIAL_PLATFORMS,
    QQ_PLATFORM,
    QZONE_ID,
    ROOT_PLUGIN_ID,
    SCHEDULE_ID,
    SECTIONS,
    VOICE_ID,
    component_enabled,
    is_qq_event,
    legacy_conflicts,
)

COMPONENT_TYPES = {
    SCHEDULE_ID: LifeSchedulerPlus,
    PHOTO_ID: LifePhotoPlugin,
    VOICE_ID: LifeVoicePlugin,
    CHAT_ID: LifeChat,
    QZONE_ID: QzoneLifePlugin,
}
SCHEDULE_TOOLS = {
    "life_get_schedule",
    "life_wardrobe_list",
    "life_wardrobe_remove",
    "life_wardrobe_edit",
}
PHOTO_TOOL = "photo_generate"
VOICE_TOOL = "tts_speak"
QQ_ADAPTERS = (
    filter.PlatformAdapterType.AIOCQHTTP
    | filter.PlatformAdapterType.QQOFFICIAL
    | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
)
VOICE_PLATFORMS = QQ_ADAPTERS | filter.PlatformAdapterType.WEBCHAT
QZONE_TOOLS = {
    "qzone_publish_photo",
    "qzone_delete_post",
    "qzone_list_own_posts",
    "qzone_space_status",
    "qzone_pause_or_resume",
    "qzone_retry_last_photo",
    "qzone_check_pending",
}
TOOL_TEMPLATES = {}


class OfficialPrivateOnly(CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() not in QQ_OFFICIAL_PLATFORMS or is_qq_event(event)


def qq_command(name):
    def decorate(handler):
        handler = filter.platform_adapter_type(QQ_ADAPTERS)(handler)
        handler = filter.custom_filter(OfficialPrivateOnly, False)(handler)
        return filter.command(name)(handler)

    return decorate


def life_tool(name):
    def stage(handler):
        # Use AstrBot's own docstring-to-tool schema, but defer publishing the
        # tool name until the legacy plugins are confirmed inactive.
        previous = llm_tools.get_func(name)
        registered = filter.llm_tool(name=name)(handler)
        tool = llm_tools.get_func(name)
        llm_tools.remove_func(name)
        if previous is not None:
            llm_tools.func_list.append(previous)
        if name == PHOTO_TOOL:
            configure_photo_tool_schema(tool)
        TOOL_TEMPLATES[name] = tool
        return registered

    return stage


@register(
    ROOT_PLUGIN_ID,
    "臭屁",
    "日程驱动的 QQ 拟人陪伴：主动聊天、语音、自拍和 QQ 空间。网页聊天支持语音。",
    "v1.4.4",
)
class NaturalLife(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.components = {}
        self.failed_components = {}
        self._lifecycle = asyncio.Lock()
        self._config_write_lock = RLock()
        self._running = False
        self._closed = False
        self._blocked_by = ()
        self._published_tools = {}
        self._displaced_tools = {}
        self._dashboard = NaturalLifeDashboard(self)

    async def initialize(self):
        try:
            migrate_config(self.config, self._config_write_lock)
        except Exception as error:
            logger.error("[拟人生活] 旧配置迁移失败，本次按现有配置运行（%s: %s）。", type(error).__name__, error)
        await self._sync_components()
        self._dashboard.register()

    async def terminate(self):
        self._closed = True
        self._dashboard.unregister()
        async with self._lifecycle:
            await self._stop_components()

    async def _sync_components(self):
        async with self._lifecycle:
            if self._closed:
                return
            conflicts = legacy_conflicts(self.context, check_unloaded=True)
            if conflicts:
                if self._blocked_by != conflicts:
                    logger.warning(
                        "[拟人生活] 发现旧插件正在运行或尚未载入（%s），请停用或移出旧插件后重载拟人生活。",
                        "、".join(conflicts),
                    )
                self._blocked_by = conflicts
                await self._stop_components()
                return
            self._blocked_by = ()
            if not self._running:
                self.failed_components.clear()
                for plugin_id, component_type in COMPONENT_TYPES.items():
                    if legacy_conflicts(self.context):
                        await self._stop_components()
                        return
                    try:
                        component = component_type(
                            self.context,
                            ComponentConfig(
                                self.config,
                                SECTIONS[plugin_id],
                                self._config_write_lock,
                            ),
                        )
                        if plugin_id == SCHEDULE_ID:
                            component.html_render = self.html_render
                        self.components[plugin_id] = component
                        await component.initialize()
                    except Exception as error:
                        self.failed_components[plugin_id] = type(error).__name__
                        logger.error(
                            "[拟人生活] %s 初始化失败（%s：%s）。",
                            SECTIONS[plugin_id],
                            type(error).__name__,
                            error,
                        )
                        await self._stop_component(plugin_id)
                self._running = True
            if legacy_conflicts(self.context):
                await self._stop_components()
                return
            await self._publish_tools()

    async def _stop_component(self, plugin_id):
        component = self.components.pop(plugin_id, None)
        if component is not None:
            try:
                await component.terminate()
            except Exception as error:
                logger.error(
                    "[拟人生活] %s 停止失败（%s）。",
                    SECTIONS[plugin_id],
                    type(error).__name__,
                )

    async def _stop_components(self):
        self._withdraw_tools()
        for plugin_id in reversed(tuple(self.components)):
            await self._stop_component(plugin_id)
        self._running = False

    async def _publish_tools(self):
        disabled = await sp.global_get("inactivated_llm_tools", [])
        if legacy_conflicts(self.context):
            return
        for name, template in TOOL_TEMPLATES.items():
            current = llm_tools.get_func(name)
            if name in self._published_tools and current is self._published_tools[name]:
                continue
            tool = copy(template)
            tool.handler = partial(template.handler, self)
            tool.handler_module_path = __name__
            tool.active = name not in disabled
            llm_tools.remove_func(name)
            llm_tools.func_list.append(tool)
            self._published_tools[name] = tool
            self._displaced_tools[name] = current

    def _withdraw_tools(self):
        for name, tool in self._published_tools.items():
            if llm_tools.get_func(name) is tool:
                llm_tools.remove_func(name)
                if previous := self._displaced_tools[name]:
                    llm_tools.func_list.append(previous)
        self._published_tools.clear()
        self._displaced_tools.clear()

    @filter.on_plugin_loaded()
    async def on_legacy_plugin_loaded(self, metadata):
        if metadata.name in LEGACY_IDS:
            await self._sync_components()

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        await self._sync_components()
        if chat := self.components.get(CHAT_ID):
            await chat.on_platform_loaded()

    async def _component_for(self, plugin_id, event, *, allow_disabled=False):
        if plugin_id == QZONE_ID and event.get_platform_name() != QQ_PLATFORM:
            return None
        if self._closed or not (
            is_qq_event(event) or (plugin_id == VOICE_ID and event.get_platform_name() == "webchat")
        ):
            return None
        if legacy_conflicts(self.context):
            await self._sync_components()
            return None
        if await component_enabled(
            self.context,
            plugin_id,
            event.unified_msg_origin,
            event,
            allow_disabled=allow_disabled,
        ):
            return self.components.get(plugin_id)
        return None

    async def _qzone_for(self, event):
        component = await self._component_for(QZONE_ID, event)
        if component is not None and await component.available_for(event):
            return component
        return None

    @filter.on_llm_request()
    async def component_requests(self, event: AstrMessageEvent, request: ProviderRequest):
        if not is_qq_event(event):
            unavailable_tools = set(self._published_tools)
            if VOICE_TOOL in unavailable_tools and (voice := await self._component_for(VOICE_ID, event)):
                await voice.on_llm_request(event, request)
                if event.is_stopped():
                    return
                unavailable_tools.remove(VOICE_TOOL)
        else:
            if legacy_conflicts(self.context):
                await self._sync_components()
                return
            unavailable_tools = set()
            for plugin_id, tool_names in (
                (PHOTO_ID, {PHOTO_TOOL}),
                (SCHEDULE_ID, SCHEDULE_TOOLS),
                (VOICE_ID, {VOICE_TOOL}),
            ):
                if component := await self._component_for(plugin_id, event):
                    await component.on_llm_request(event, request)
                    if event.is_stopped():
                        return
                else:
                    unavailable_tools.update(tool_names)
            if component := await self._qzone_for(event):
                await component.space_dialogue_request(event, request)
            else:
                unavailable_tools.update(QZONE_TOOLS)
        if unavailable_tools and request.func_tool is not None:
            request.func_tool = copy(request.func_tool)
            for name in unavailable_tools:
                request.func_tool.remove_tool(name)

    @filter.on_llm_response()
    async def schedule_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if component := await self._component_for(SCHEDULE_ID, event):
            await component.on_llm_response(event, resp)

    @filter.after_message_sent()
    async def schedule_after_message_sent(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            await component.after_message_sent(event)

    @filter.on_llm_tool_respond()
    async def schedule_on_llm_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """Consume event-local success receipts; tool arguments prove no delivery."""
        if component := await self._component_for(SCHEDULE_ID, event):
            await component.on_llm_tool_respond(event, tool, tool_args, tool_result)

    @qq_command("life")
    async def life(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        """Life English command router, shared by both AstrBot command parsers."""
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life(event, extra)) as results:
                async for result in results:
                    yield result

    @qq_command("查看日程")
    async def life_show(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_show(event)) as results:
                async for result in results:
                    yield result

    @qq_command("日程原计划")
    async def life_plan(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_plan(event)) as results:
                async for result in results:
                    yield result

    @qq_command("重写日程")
    async def life_renew(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_renew(event, extra)) as results:
                async for result in results:
                    yield result

    @qq_command("昨天")
    async def life_yesterday(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_yesterday(event)) as results:
                async for result in results:
                    yield result

    @qq_command("查看档案")
    async def life_profile(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_profile(event)) as results:
                async for result in results:
                    yield result

    @qq_command("重新生成档案")
    async def life_profile_regen(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_profile_regen(event)) as results:
                async for result in results:
                    yield result

    @qq_command("档案已确认")
    async def life_profile_ok(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_profile_ok(event)) as results:
                async for result in results:
                    yield result

    @qq_command("查看约定")
    async def life_promise(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_promise(event)) as results:
                async for result in results:
                    yield result

    @qq_command("取消约定")
    async def life_promise_cancel(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_promise_cancel(event, extra)) as results:
                async for result in results:
                    yield result

    @qq_command("查看衣柜")
    async def wardrobe_show(self, event: AstrMessageEvent):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.wardrobe_show(event)) as results:
                async for result in results:
                    yield result

    @qq_command("展示")
    async def wardrobe_show_image(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.wardrobe_show_image(event, extra)) as results:
                async for result in results:
                    yield result

    @qq_command("日程时间")
    async def life_time(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_time(event, extra)) as results:
                async for result in results:
                    yield result

    @qq_command("参考会话")
    async def life_reference_umo(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        if component := await self._component_for(SCHEDULE_ID, event):
            async with aclosing(component.life_reference_umo(event, extra)) as results:
                async for result in results:
                    yield result

    @life_tool("life_get_schedule")
    async def llm_get_schedule(self, event: AstrMessageEvent) -> str:
        """查询角色今日全天各时段完整日程、待办约定（仅主人可查）及正在进行的近期跨天大事件，补充 <character_life> 未展示的今日时段和近期大事。工具只返回当前已有记录。更远日期没有相应记录时，按角色的平常作息自然回答，明确只是平常安排，不当作当天已确定的行程，无需重复调用。"""
        if component := await self._component_for(SCHEDULE_ID, event):
            return await component.llm_get_schedule(event)
        return "当前会话未启用这项功能。"

    @life_tool("life_wardrobe_list")
    async def llm_wardrobe_list(self, event: AstrMessageEvent) -> str:
        """列出衣柜最新编号、稳定ID、描述、分类和季节；只供主人使用。"""
        if component := await self._component_for(SCHEDULE_ID, event):
            return await component.llm_wardrobe_list(event)
        return "当前会话未启用这项功能。"

    @life_tool("life_wardrobe_remove")
    async def llm_wardrobe_remove(self, event: AstrMessageEvent, query: str) -> str:
        """按主人要求删除一套穿搭，先列出衣柜核对编号。

        Args:
            query(string): 稳定ID、穿搭N或唯一描述关键词。"""
        if component := await self._component_for(SCHEDULE_ID, event):
            return await component.llm_wardrobe_remove(event, query)
        return "当前会话未启用这项功能。"

    @life_tool("life_wardrobe_edit")
    async def llm_wardrobe_edit(self, event: AstrMessageEvent, query: str, instruction: str) -> str:
        """按主人明确要求修改穿搭，保留其余造型细节。

        Args:
            query(string): 稳定ID、穿搭N或唯一描述关键词。
            instruction(string): 用户的穿戴修改要求，如换鞋或赤足。"""
        if component := await self._component_for(SCHEDULE_ID, event):
            return await component.llm_wardrobe_edit(event, query, instruction)
        return "当前会话未启用这项功能。"

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    @filter.custom_filter(OfficialPrivateOnly, False, priority=100)
    @filter.platform_adapter_type(QQ_ADAPTERS, priority=100)
    async def photo_observe_group_material(self, event: AstrMessageEvent):
        """Only the immediately previous genuine private user turn may supply a stand-in."""
        if component := await self._component_for(PHOTO_ID, event):
            await component.observe_group_material(event)

    @filter.on_decorating_result()
    async def photo_clean_photo_result(self, event: AstrMessageEvent):
        if component := await self._component_for(PHOTO_ID, event):
            await component.clean_photo_result(event)

    @filter.on_agent_begin()
    async def photo_capture_photo_turn(self, event: AstrMessageEvent, run_context):
        if component := await self._component_for(PHOTO_ID, event):
            await component.capture_photo_turn(event, run_context)

    @filter.on_agent_done()
    async def photo_clean_photo_agent_done(self, event: AstrMessageEvent, run_context, response):
        if component := await self._component_for(PHOTO_ID, event):
            await component.clean_photo_agent_done(event, run_context, response)

    @life_tool("photo_generate")
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
        新拍、续拍或改图时在waiting_text写一条符合人设的简短过渡话。
        插件会先发这句再开始生成；不要另发同一句，不写旁白、声音标签或拍摄细节。
        原图重发时留空。完成后自然接话，不重复说已经发了，不重复调用或自行重试。

        Args:
            user_prompt(string): 基于聊天理解的简短拍摄意图；补写细节放shot_plan，不能冒充用户原话。
            photo_type(string): selfie自拍、scene场景、food食物、outfit穿搭。
            count(number): 一到四张，默认一张。
            with_user_image(boolean): 是否让角色与本条上传或引用的人物素材合影，支持虚构人物。
            aspect_ratio(string): 仅在用户明确指定比例或横图、正方形等构图时填写；其余留空，按插件的照片尺寸设置。
            mode(string): photo生活照、text普通文字生图、edit修改消息图片。
            action(string): generate新拍，continue承接来源再拍，resend复用一张已成功照片且不调用生图服务。
            source_photo_id(string): 本会话本角色的来源照片ID。指定组内某张时按组标签与序号选对应ID，即使本轮也有新附件。
            use_current_scene(boolean): 仅食物或场景的新拍使用。拍角色当前生活填true，用户另点题材填false。
            shot_plan(list[object]): 与count等长的逐张方案，每项可写capture拍摄方式、framing取景距离、angle角度、action动作、expression具体神态；值为文字，人设与当前聊天优先。
            waiting_text(string): 新生成前由插件先发的一条简短口语，最多60字；原图重发留空。"""
        if component := await self._component_for(PHOTO_ID, event):
            return await component.photo_generate(
                event,
                user_prompt,
                photo_type,
                count,
                with_user_image,
                aspect_ratio,
                mode,
                action,
                source_photo_id,
                shot_plan,
                use_current_scene,
                waiting_text=waiting_text,
            )

    @filter.after_message_sent()
    async def photo_acknowledge_waiting_reply(self, event: AstrMessageEvent):
        if component := await self._component_for(PHOTO_ID, event):
            await component.acknowledge_waiting_reply(event)

    @filter.on_llm_tool_respond()
    async def photo_acknowledge_voice_reply(self, event: AstrMessageEvent, tool, arguments, result):
        if component := await self._component_for(PHOTO_ID, event):
            await component.acknowledge_voice_reply(event, tool, arguments, result)

    @qq_command("life_photo_status")
    async def life_photo_status(self, event: AstrMessageEvent):
        if component := await self._component_for(PHOTO_ID, event, allow_disabled=True):
            async with aclosing(component.life_photo_status(event)) as results:
                async for result in results:
                    yield result

    @life_tool("tts_speak")
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
        if component := await self._component_for(VOICE_ID, event):
            async with aclosing(component.tts_speak(event, text)) as results:
                async for result in results:
                    yield result

    @filter.command("life_voice_test")
    @filter.custom_filter(OfficialPrivateOnly, False)
    @filter.platform_adapter_type(VOICE_PLATFORMS)
    async def life_voice_test(self, event: AstrMessageEvent, *, text: str = ""):
        if component := await self._component_for(VOICE_ID, event, allow_disabled=True):
            async with aclosing(component.life_voice_test(event, text=text)) as results:
                async for result in results:
                    yield result

    @filter.command("life_voice_status")
    @filter.custom_filter(OfficialPrivateOnly, False)
    @filter.platform_adapter_type(VOICE_PLATFORMS)
    async def life_voice_status(self, event: AstrMessageEvent):
        if component := await self._component_for(VOICE_ID, event, allow_disabled=True):
            async with aclosing(component.life_voice_status(event)) as results:
                async for result in results:
                    yield result

    @filter.on_decorating_result()
    async def voice_clean_visible_tags(self, event: AstrMessageEvent):
        if component := await self._component_for(VOICE_ID, event):
            await component.clean_visible_tags(event)

    @filter.custom_filter(AlreadyAwakePrivate, False, priority=1000)
    async def chat_on_private(self, event: AstrMessageEvent):
        if component := await self._component_for(CHAT_ID, event):
            await component.on_private(event)

    @filter.on_llm_request(priority=-1000)
    async def chat_guide_reply(self, event: AstrMessageEvent, request):
        if component := await self._component_for(CHAT_ID, event):
            await component.guide_reply(event, request)

    @filter.on_decorating_result()
    async def chat_clean_reply_markers(self, event: AstrMessageEvent):
        if component := await self._component_for(CHAT_ID, event):
            await component.clean_reply_markers(event)

    @filter.after_message_sent(priority=1000)
    async def chat_observe_normal_reply(self, event: AstrMessageEvent):
        if component := await self._component_for(CHAT_ID, event):
            await component.observe_normal_reply(event)

    @life_tool("qzone_publish_photo")
    async def qzone_publish_photo(self, event: AstrMessageEvent, caption: str = "") -> str:
        """用户明确要求把引用或当前消息的照片发到你的QQ空间时使用。仅看图聊天不调用。未明确指定发布文案时caption留空，插件按照片和当前人设自动配文。结果按facts用角色口吻回复，勿显示内部引用。结果不明先查询，勿再次调用发布。

        Args:
            caption(string): 用户明确指定的文案；未指定留空。"""
        if component := await self._qzone_for(event):
            return await component.publish_photo_tool(event, caption)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_delete_post")
    async def qzone_delete_post(self, event: AstrMessageEvent, target: str) -> str:
        """用户明确要删除你自己空间的说说时使用。用文案片段、刚才那条、最新一条或刚查询候选中的第一条等定位。候选不唯一时询问用户，不猜。内部标识仅用于调用，不展示。

        Args:
            target(string): 用户指定的内容片段、先后顺序或内部引用。"""
        if component := await self._qzone_for(event):
            return await component.delete_post_tool(event, target)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_list_own_posts")
    async def qzone_list_own_posts(self, event: AstrMessageEvent) -> str:
        """用户询问自己空间最近内容，或需要定位要删的说说时查看。用时间和内容描述候选，勿展示内部标识。"""
        if component := await self._qzone_for(event):
            return await component.list_posts_tool(event)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_space_status")
    async def qzone_space_status(self, event: AstrMessageEvent) -> str:
        """用户询问今天自主发了几条、是否暂停、日常分享与回评状态时查看。次数与开关信息用此工具就够了；只有用户明确询问具体说说内容时再读列表。用当前角色口吻解释。"""
        if component := await self._qzone_for(event):
            return await component.status_tool(event)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_pause_or_resume")
    async def qzone_pause_or_resume(self, event: AstrMessageEvent, action: str) -> str:
        """管理员要求暂停或恢复日常自主发说说、自动回评与补赞时调用；这个开关一起控制三项。手动要求发说说仍可用。

        Args:
            action(string): pause表示暂停，resume表示恢复。"""
        if component := await self._qzone_for(event):
            return await component.control_tool(event, action)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_retry_last_photo")
    async def qzone_retry_last_photo(self, event: AstrMessageEvent) -> str:
        """用户要求重试刚才未成功的照片发布时使用。复用当前聊天里确定失败的最近一次原图原文，结果不明会先阻止重复发送。"""
        if component := await self._qzone_for(event):
            return await component.retry_tool(event)
        return "当前会话无法使用 QQ 空间。"

    @life_tool("qzone_check_pending")
    async def qzone_check_pending(
        self,
        event: AstrMessageEvent,
        target: str = "",
        outcome: str = "",
        confirmation: str = "",
    ) -> str:
        """用户询问刚才发/删/回评是否成功，或明确核实后告知结果时调用。查询时outcome与confirmation留空。只有用户亲自看过空间、明确陈述结果时填写outcome，confirmation必须完整复制当前用户消息原文，不截取片段，问题或猜测不算确认。发布成功还会实际查询匹配说说。内部引用勿展示。

        Args:
            target(string): 刚才那次、内容片段、第一条或之前工具给的内部引用；只有一次时留空。
            outcome(string): 查询留空；用户明确核实已发生填已成功，明确未发生填未成功。
            confirmation(string): 完整复制对应确认结论的当前用户消息原文，不截取片段；查询留空。"""
        if component := await self._qzone_for(event):
            return await component.check_tool(event, target, outcome, confirmation)
        return "当前会话无法使用 QQ 空间。"
