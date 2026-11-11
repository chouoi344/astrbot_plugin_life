"""QQ空间 — character-native dialogue over durable own-account actions."""

import asyncio
import inspect
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

from ...runtime import QZONE_ID
from ..common.contracts import PHOTO_COMPLETION, PROACTIVE_DELIVERY
from .core.dialogue import CHAT_RULE, Outcome, RequestProblem, exception_outcome
from .core.integrations import space_session_enabled
from .core.service import QzoneService
from .core.settings import admin_ids


class QzoneLifePlugin:
    def __init__(self, context: Context, config):
        self.context = context
        self.config = config
        self.service = None
        self._closing = False
        self._operations = set()

    def _enabled(self):
        return self.config["enabled"] and not self._closing

    async def initialize(self):
        if self._enabled():
            self.service = QzoneService(
                self.context,
                self.config,
                Path(StarTools.get_data_dir(QZONE_ID)),
                runtime_enabled=self._enabled,
            )
            self.service.start()

    async def terminate(self):
        self._closing = True
        operations = tuple(self._operations)
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        if self.service is not None:
            service, self.service = self.service, None
            await service.close()

    async def available_for(self, event):
        return bool(
            self._enabled()
            and self.service is not None
            and event.get_platform_name() == "aiocqhttp"
            and getattr(event, "bot", None)
            and str(event.get_sender_id()) in admin_ids(self.context)
            and not event.get_extra(PHOTO_COMPLETION)
            and not event.get_extra(PROACTIVE_DELIVERY)
            and await space_session_enabled(self.context, event.unified_msg_origin, event)
            and self._enabled()
        )

    async def _run(self, event, operation, *, action="request"):
        # Return evidence to the existing conversation's tool loop. The original
        # character model keeps its history, memory and control of the reply.
        task = asyncio.current_task()
        try:
            if not await self.available_for(event):
                raise RequestProblem(
                    "space_unavailable",
                    {"result": "当前QQ会话无法使用空间功能，此次操作未执行"},
                    status="blocked",
                )
            self._operations.add(task)
            if action != "control":
                await self.service.ensure_ready(event, required=action != "status")
            result = operation()
            result = await result if inspect.isawaitable(result) else result
            if not isinstance(result, Outcome):
                raise RuntimeError("空间操作缺少结构化结果")
        except Exception as exc:
            logger.warning("QQ空间操作未完成：%s", exc)
            result = exception_outcome(exc, action=action)
        finally:
            self._operations.discard(task)
        return result.tool_text()

    async def space_dialogue_request(self, event: AstrMessageEvent, req):
        if await self.available_for(event):
            req.system_prompt = (req.system_prompt or "") + "\n\n" + CHAT_RULE

    async def publish_photo_tool(self, event: AstrMessageEvent, caption: str = "") -> str:
        """用户明确要求把引用或当前消息的照片发到你的QQ空间时使用。仅看图聊天不调用。未明确指定发布文案时caption留空，插件按照片和当前人设自动配文。结果按facts用角色口吻回复，勿显示内部引用。结果不明先查询，勿再次调用发布。

        Args:
            caption(string): 用户明确指定的文案；未指定留空。
        """
        return await self._run(event, lambda: self.service.publish_from_chat(event, caption), action="publish")

    async def delete_post_tool(self, event: AstrMessageEvent, target: str) -> str:
        """用户明确要删除你自己空间的说说时使用。用文案片段、刚才那条、最新一条或刚查询候选中的第一条等定位。候选不唯一时询问用户，不猜。内部标识仅用于调用，不展示。

        Args:
            target(string): 用户指定的内容片段、先后顺序或内部引用。
        """
        return await self._run(event, lambda: self.service.delete_post(event, target), action="delete")

    async def list_posts_tool(self, event: AstrMessageEvent) -> str:
        """用户询问自己空间最近内容，或需要定位要删的说说时查看。用时间和内容描述候选，勿展示内部标识。"""
        return await self._run(event, lambda: self.service.list_posts(event), action="list")

    async def status_tool(self, event: AstrMessageEvent) -> str:
        """用户询问今天自主发了几条、是否暂停、日常分享与回评状态时查看。次数与开关信息用此工具就够了；只有用户明确询问具体说说内容时再读列表。用当前角色口吻解释。"""
        return await self._run(event, lambda: self.service.user_status(event), action="status")

    async def control_tool(self, event: AstrMessageEvent, action: str) -> str:
        """管理员要求暂停或恢复日常自主发说说、自动回评与补赞时调用；这个开关一起控制三项。手动要求发说说仍可用。

        Args:
            action(string): pause表示暂停，resume表示恢复。
        """
        return await self._run(event, lambda: self.service.control(event, action), action="control")

    async def retry_tool(self, event: AstrMessageEvent) -> str:
        """用户要求重试刚才未成功的照片发布时使用。复用当前聊天里确定失败的最近一次原图原文，结果不明会先阻止重复发送。"""
        return await self._run(event, lambda: self.service.retry_last_publish(event), action="publish")

    async def check_tool(
        self, event: AstrMessageEvent, target: str = "", outcome: str = "", confirmation: str = ""
    ) -> str:
        """用户询问刚才发/删/回评是否成功，或明确核实后告知结果时调用。查询时outcome与confirmation留空。只有用户亲自看过空间、明确陈述结果时填写outcome，confirmation必须完整复制当前用户消息原文，不截取片段，问题或猜测不算确认。发布成功还会实际查询匹配说说。内部引用勿展示。

        Args:
            target(string): 刚才那次、内容片段、第一条或之前工具给的内部引用；只有一次时留空。
            outcome(string): 查询留空；用户明确核实已发生填已成功，明确未发生填未成功。
            confirmation(string): 完整复制对应确认结论的当前用户消息原文，不截取片段；查询留空。
        """
        return await self._run(
            event,
            lambda: self.service.check_pending(event, target, outcome, confirmation),
            action="check",
        )
