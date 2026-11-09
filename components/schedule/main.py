"""Schedule component callbacks; slow work lives in core.engine's managed tasks."""

import asyncio
import copy
import logging
import re
import unicodedata
from datetime import timedelta

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, StarTools
from astrbot.core.star.filter.command import GreedyStr
from ...runtime import component_enabled

from .core.domain import PROFILE_FIELDS
from .core.engine import LifeEngine, PROFILE_WARNING
from .core.errors import describe_error, run_with_timeout, user_error
from .core.image_input import collect_image_paths
from .core.images import WardrobeImageStore
from .core.outfits import project_day_outfits, slot_outfit_label
from .core.prompts import dump
from .core.timeline import format_timeline, parse_local
from .core.wardrobe import wardrobe_html
from .core.web_api import LifeViewAPI

log = logging.getLogger("astrbot")
PLUGIN_ID = "astrbot_plugin_life_scheduler_plus"


def _short_outfit_text(value, limit=60):
    """Keep joined emoji, modifiers and combining accents intact when clipping."""
    text = " ".join(str(value or "").split())
    clusters = []
    for character in text:
        codepoint = ord(character)
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        continuation = (unicodedata.category(character).startswith("M") or character == "\u200d"
                        or 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF
                        or 0x1F3FB <= codepoint <= 0x1F3FF or 0xE0020 <= codepoint <= 0xE007F)
        paired_flag = (regional and clusters and len(clusters[-1]) == 1
                       and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF)
        if clusters and (continuation or clusters[-1].endswith("\u200d") or paired_flag):
            clusters[-1] += character
        else:
            clusters.append(character)
    return "".join(clusters[:limit]) + ("…" if len(clusters) > limit else "")


class LifeSchedulerPlus:
    def __init__(self, context: Context, config: AstrBotConfig):
        self._native_settings_saved = config.get("settings_saved", False) is True
        config["settings_saved"] = True
        self.context, self.config = context, config
        self.engine = None
        self.life_api = None
        self._temp_warning_logged = False
        self._view_warning_logged = False

    async def initialize(self):
        self.data_dir = StarTools.get_data_dir(PLUGIN_ID)
        self.engine = LifeEngine(self.context, self.config, self.data_dir, native_settings_saved=self._native_settings_saved)
        self.wardrobe_images = WardrobeImageStore(self.data_dir / "wardrobe_images")
        self._register_life_view()
        if self.config.get("enabled", True):
            self.wardrobe_images.prune({entry["id"] for entry in self.engine.wardrobe.all()})
            self.engine.start()

    def _register_life_view(self):
        try:
            if self.life_api is None:
                self.life_api = LifeViewAPI(self)
            if self.life_api.register():
                return
        except Exception as exc:
            log.debug("注册生活视图失败：%s", describe_error(exc))
            if self.life_api:
                self.life_api.unregister()
        if not self._view_warning_logged:
            log.info("当前环境缺少插件视图 API，仍可使用日程和衣柜指令。")
            self._view_warning_logged = True

    async def terminate(self):
        try:
            if self.engine:
                await self.engine.stop()
        finally:
            if self.life_api:
                self.life_api.unregister()

    def is_owner(self, event):
        owners = [str(value) for value in self.config.get("owner_ids", []) if str(value).strip()]
        if owners:
            return str(event.get_sender_id()) in owners
        return bool(event.is_admin())

    def get_current_state(self):
        """Stable, independent snapshot for future photo/proactive/QZone modules."""
        if not self.config.get("enabled", True) or not self.engine or self.engine.closed:
            return {}
        return copy.deepcopy(self.engine.get_current_state())

    async def confirm_tell_delivered(self, *, expected_persona_id: str, expected_promise: dict) -> bool:
        """Complete one due tell from a snapshot after its content was sent."""
        if not self.config.get("enabled", True) or not self.engine or not isinstance(expected_promise, dict):
            return False
        return await self.engine.confirm_tell_delivered(expected_persona_id, copy.deepcopy(expected_promise))

    async def get_life_context(self, *, allow_generate: bool = True) -> dict:
        if not self.config.get("enabled", True) or not self.engine or self.engine.closed:
            return {}
        return await self.engine.get_life_context(allow_generate=allow_generate)

    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not self.engine or self.engine.closed or event.get_extra("life_plus_command", False):
                return
            if not await component_enabled(self.context, PLUGIN_ID, str(event.unified_msg_origin), event):
                return
            await self.engine.setup.refresh(automatic=True)
            if self.engine.closed or not self.engine.life_ready():
                return
            event.set_extra("life_plus_token", self.engine.setup.state["token"])
            event.set_extra("life_plus_user", str(event.message_str or ""))
            text, missed = self.engine.injection(owner=self.is_owner(event))
            if not text:
                return
            event.set_extra("life_plus_missed", missed)
            if self.config.get("inject_as_temp", True):
                try:
                    from astrbot.core.agent.message import TextPart
                    if not callable(getattr(TextPart, "mark_as_temp", None)) or not hasattr(req, "extra_user_content_parts"):
                        raise ImportError("temporary content unavailable")
                    req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
                    return
                except (ImportError, AttributeError, TypeError):
                    if not self._temp_warning_logged:
                        log.info("当前环境缺少临时注入接口，生活状态改用系统提示词注入。")
                        self._temp_warning_logged = True
            req.system_prompt = (req.system_prompt or "") + "\n" + text
        except Exception as exc:
            log.warning("生活状态注入失败，聊天继续：%s", describe_error(exc))

    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            if (self.engine and self.engine.life_ready() and self.engine.setup.owns(event.get_extra("life_plus_token", ""))
                    and not event.get_extra("life_plus_command", False)):
                text = getattr(resp, "completion_text", "")
                if isinstance(text, str) and text.strip():
                    event.set_extra("life_plus_reply", text)
        except Exception as exc:
            log.warning("保存本轮回复供日程判断失败：%s", describe_error(exc))

    async def after_message_sent(self, event: AstrMessageEvent):
        try:
            if not event.get_extra("life_plus_reply", ""):
                return
            # Event results still exist here in 4.26/4.29; prefer the actual
            # sent text after other plugins decorate the provider completion.
            result = event.get_result()
            reply = "".join(part.text for part in result.chain if isinstance(part, Plain)).strip() if result else ""
            if reply:
                await self._enqueue_delivered(event, reply)
            event.set_extra("life_plus_reply", "")
        except Exception as exc:
            log.warning("回复后日程判断入队失败，聊天继续：%s", describe_error(exc))

    async def on_llm_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """Consume event-local success receipts; tool arguments prove no delivery."""
        try:
            receipts = event.get_extra("life_voice_delivery_v1", [])
            if not isinstance(receipts, list):
                raise ValueError("语音交付收据必须是数组")
            for receipt in receipts:
                if (not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str) or not receipt["id"]
                        or not isinstance(receipt.get("text"), str) or not receipt["text"].strip()
                        or receipt.get("medium") not in {"voice", "text"}):
                    raise ValueError("语音交付收据字段无效")
                await self._enqueue_delivered(event, receipt["text"].strip(), receipt_id=receipt["id"])
        except Exception as exc:
            log.warning("语音交付后日程判断入队失败，聊天继续：%s", describe_error(exc))

    async def _enqueue_delivered(self, event, reply, receipt_id=None):
        if not await component_enabled(self.context, PLUGIN_ID, str(event.unified_msg_origin), event):
            return
        if (not self.engine or self.engine.closed or event.get_extra("life_plus_command", False)
                or not self.is_owner(event) or not self.config.get("chat_change_enabled", True)
                or not self.engine.life_ready() or not self.engine.setup.owns(event.get_extra("life_plus_token", ""))):
            return
        receipt_ids = event.get_extra("life_plus_voice_receipts", [])
        if receipt_id and receipt_id in receipt_ids:
            return
        texts = event.get_extra("life_plus_delivered_texts", [])
        if receipt_id:
            event.set_extra("life_plus_voice_receipts", [*receipt_ids, receipt_id])
        if reply not in texts:
            # A failed background registration must not retry an already-sent
            # line on the next tool hook, including failures after queue append.
            event.set_extra("life_plus_delivered_texts", [*texts, reply])
            self.engine.enqueue_chat(str(event.unified_msg_origin), event.get_extra("life_plus_user", event.message_str),
                                     reply, missed_ids=event.get_extra("life_plus_missed", []))
            event.set_extra("life_plus_missed", [])

    async def life(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        """Life English command router, shared by both AstrBot command parsers."""
        text = self._argument(extra)
        exact = {"": "show", "show": "show", "plan": "plan", "yesterday": "yesterday", "profile": "profile",
                 "profile regen": "profile_regen", "profile ok": "profile_ok", "promise": "promise",
                 "wardrobe show": "wardrobe_show", "wardrobe": "wardrobe_show"}
        if text in exact:
            action, argument = exact[text], ""
        else:
            prefixes = {"promise cancel": "promise_cancel", "wardrobe image": "wardrobe_image", "renew": "renew", "time": "time", "umo": "umo"}
            found = next(((prefix, action) for prefix, action in prefixes.items() if text == prefix or text.startswith(prefix + " ")), None)
            if not found:
                yield event.plain_result("用法：life show / plan / renew / yesterday / profile [regen|ok] / promise [cancel] / wardrobe [show|image] / time / umo")
                return
            prefix, action = found
            argument = text[len(prefix):].strip()
        async for result in self._dispatch(event, action, argument):
            yield result

    @staticmethod
    def _argument(value):
        return "" if value is GreedyStr or value is None else str(value).strip()

    async def life_show(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "show"):
            yield result

    async def life_plan(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "plan"):
            yield result

    async def life_renew(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        async for result in self._dispatch(event, "renew", self._argument(extra)):
            yield result

    async def life_yesterday(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "yesterday"):
            yield result

    async def life_profile(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "profile"):
            yield result

    async def life_profile_regen(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "profile_regen"):
            yield result

    async def life_profile_ok(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "profile_ok"):
            yield result




    async def life_promise(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "promise"):
            yield result

    async def life_promise_cancel(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        async for result in self._dispatch(event, "promise_cancel", self._argument(extra)):
            yield result


    async def wardrobe_show(self, event: AstrMessageEvent):
        async for result in self._dispatch(event, "wardrobe_show"):
            yield result

    async def wardrobe_show_image(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        async for result in self._dispatch(event, "wardrobe_image", self._argument(extra)):
            yield result

    async def life_time(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        async for result in self._dispatch(event, "time", self._argument(extra)):
            yield result

    async def life_reference_umo(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        async for result in self._dispatch(event, "umo", self._argument(extra)):
            yield result

    def _day_text(self, day, plan=False, yesterday=False):
        text = day.get("date", "") + (" 原计划" if plan else " 实际日程") + "\n"
        weather = day.get("weather", {})
        fallback_from = str(weather.get("fallback_from") or "").strip()
        city = str(weather.get("city") or "").strip()
        text += "天气：" + (fallback_from or city) + " " + str(weather.get("summary", "未知"))
        if weather.get("temp_min") is not None:
            text += " " + str(weather["temp_min"]) + "~" + str(weather.get("temp_max", "")) + "℃"
        if weather.get("source") == "ai":
            text += "（AI 按季节模拟）"
        elif fallback_from and city:
            text += "（参考" + city + "天气）"
        entries = self.engine.wardrobe.display_entries()
        outfits = project_day_outfits(day, entries)
        text += "\n👗 今日穿搭"
        for outfit in outfits:
            description = _short_outfit_text(outfit.get("description")) or "未记录"
            text += "\n" + outfit["label"] + "：" + description + "（" + outfit["source_label"] + "）"
        if day.get("outfit_notes"):
            text += "\n补充：" + _short_outfit_text(day["outfit_notes"])
        text += "\n" + format_timeline(day.get("plan" if plan else "actual", []), now=None if yesterday else self.engine.now(),
                                       outfit_label=lambda slot: slot_outfit_label(slot, day))
        if day.get("day_summary"):
            text += "\n概况：" + day["day_summary"]
        events = self.engine.events.active(parse_local(day["date"] + "T12:00", self.engine.tz).date())
        if events:
            text += "\n进行中的事件：" + "；".join(str(item.get("title", "")) + "（" + str(item.get("end_date", "")) + "结束）" for item in events)
        if day.get("changes") and not plan:
            text += "\n今天的变化：\n" + "\n".join(str(item.get("time", ""))[11:16] + " " + str(item.get("description", "")) for item in day["changes"])
        if yesterday and day.get("recap"):
            text += "\n昨日回顾：" + day["recap"]
        return text

    async def _dispatch(self, event, action, argument=""):
        event.set_extra("life_plus_command", True)
        if not self.engine:
            yield event.plain_result("拟人生活的日程正在初始化")
            return
        public = {"show", "yesterday", "wardrobe_show"}
        if action not in public and not self.is_owner(event):
            yield event.plain_result("只有主人可以使用这条指令。")
            return
        try:
            await self.engine.setup.refresh(automatic=True)
            if not self.engine.life_ready():
                yield event.plain_result(self.engine.setup.status()["message"])
                return
            token = self.engine.setup.state["token"]
            await self.engine.check_persona()
            prefix = PROFILE_WARNING + "\n" if event.is_admin() and self.engine.persona_changed else ""
            if action in {"show", "plan", "yesterday"}:
                start, _ = self.engine.window()
                key = (start.date() - timedelta(days=1) if action == "yesterday" else start.date()).isoformat()
                day = self.engine.store.day(key)
                if not day or day.get("status") != "ok":
                    if action != "yesterday":
                        self.engine.ensure_background()
                    if action == "yesterday":
                        message = "没有昨天的实际记录。"
                    else:
                        message = self.engine.schedule_generation_state(day)["message"]
                    yield event.plain_result(prefix + message)
                    return
                yield event.plain_result(prefix + self._day_text(day, plan=action == "plan", yesterday=action == "yesterday"))
            elif action == "renew":
                yield event.plain_result(prefix + "正在重写当前时间之后的日程……")
                images = await run_with_timeout(collect_image_paths(event), 30, "收集图片")
                self.engine.setup.assert_owner(token)
                override = None
                if images:
                    override = {**await self.engine.llm.describe_outfit(images, argument), "id": "", "temporary": True}
                    self.engine.setup.assert_owner(token)
                else:
                    match = re.match(r"^穿搭\s*(\d+)(?:\s*[，,。]?\s*(.*))?$", argument)
                    if match:
                        override = self.engine.wardrobe.resolve("穿搭" + match.group(1))
                        if not override:
                            raise ValueError("没有找到这套穿搭，请先查看衣柜")
                        argument = "严格采用指定的外出穿搭：" + override["description"] + "；" + str(match.group(2) or "")
                day = await self.engine.generate(force=True, extra=argument, outfit_override=override)
                yield event.plain_result(self._day_text(day))
            elif action == "profile":
                labels = {"identity": "身份", "living": "居住", "routine_workday": "工作日作息", "routine_restday": "休息日作息", "weekly_fixed": "每周固定安排", "periodic": "周期事项", "vacations": "假期", "social_circle": "社交圈", "hobbies_places": "爱好与常去地点", "phone_habits": "手机习惯"}
                profile = self.config.get("profile", {})
                yield event.plain_result(prefix + "生活档案\n" + "\n".join(labels[key] + "：" + str(profile.get(key, "") or "（未填写）") for key in PROFILE_FIELDS) +
                                         "\n人设状态：" + ("正文已更新，生活资料保留" if self.engine.persona_changed else "已确认" if self.config.get("profile_persona_hash") else "等待生成档案") +
                                         "\n" + self.engine.profile_generation_state()["message"] +
                                         ("\n" + self.engine.llm.persona_warning if self.engine.llm.persona_warning else ""))
            elif action == "profile_regen":
                task = self.engine.ensure_profile_background(force=True)
                if task is None:
                    yield event.plain_result("插件正在重新加载，请稍后再点重新生成档案。")
                    return
                yield event.plain_result(prefix + "生活档案正在生成中，完成后会显示结果。")
                success = await asyncio.shield(task)
                yield event.plain_result("生活档案已更新并保存。" if success else self.engine.profile_generation_state()["message"])
            elif action == "profile_ok":
                await self.engine.confirm_profile()
                yield event.plain_result("已确认当前档案，人设变化提醒已清除。")
            elif action == "promise":
                now = self.engine.now()
                self.engine.promises.refresh(now, (self.engine.current_day() or {}).get("changes", []))
                lines = []
                labels = {"pending": "未完成", "fulfilled": "已完成", "missed": "已失约", "cancelled": "已取消"}
                for index, item in enumerate(self.engine.promises.all(), 1):
                    ended = item.get("ended_at") or item.get("created_at")
                    if item["status"] == "pending" or (item["status"] in {"fulfilled", "missed"} and parse_local(ended, self.engine.tz) >= now - timedelta(days=7)):
                        lines.append(str(index) + ". " + item["content"] + "｜" + str(item.get("due") or "无具体时间") + "｜" + labels.get(item["status"], item["status"]) + ("｜" + item["note"] if item.get("note") else ""))
                yield event.plain_result(prefix + ("\n".join(lines) or "没有未完成或最近七天完成、失约的约定。"))
            elif action == "promise_cancel":
                item = self.engine.promises.cancel(argument, self.engine.now())
                day = self.engine.current_day()
                if day:
                    self.engine.append_change(day, "promise", "主人取消约定：" + item["content"], [], self.engine.now())
                    self.engine.store.save_day(day)
                yield event.plain_result(prefix + "已取消约定：" + item["content"])
            elif action == "wardrobe_show":
                entries = self.engine.wardrobe.display_entries()
                if not entries:
                    yield event.plain_result(prefix + self.engine.wardrobe.format_text())
                    return
                try:
                    html = wardrobe_html(entries, self.wardrobe_images)
                    rendered = await run_with_timeout(self.html_render(html, {}), 30, "渲染衣柜卡片")
                    self.engine.setup.assert_owner(token)
                    if prefix:
                        yield event.plain_result(prefix.strip())
                    yield event.image_result(rendered)
                except Exception as exc:
                    log.info("衣柜卡片渲染失败，使用文字：%s", describe_error(exc))
                    yield event.plain_result(prefix + self.engine.wardrobe.format_text())
            elif action == "wardrobe_image":
                if not re.fullmatch(r"穿搭\s*\d+", argument):
                    raise ValueError("用法：展示 穿搭N，并附带或引用图片")
                entry = self.engine.wardrobe.resolve(argument)
                if not entry:
                    raise ValueError("没有找到这套穿搭，请先查看衣柜")
                images = await run_with_timeout(collect_image_paths(event), 30, "收集图片")
                self.engine.setup.assert_owner(token)
                if not images:
                    raise ValueError("请在指令里附上图片，或引用一条带图片的消息")
                async with self.engine.config_lock:
                    self.engine.setup.assert_owner(token)
                    await run_with_timeout(self.wardrobe_images.save(entry["id"], images[0]), 30, "保存衣柜展示图")
                yield event.plain_result(prefix + "已保存" + argument + "的展示图。")
            elif action == "time":
                if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", argument):
                    raise ValueError("用法：日程时间 07:00（小时0~23，分钟0~59）")
                hour, minute = map(int, argument.split(":"))
                async with self.engine.config_lock:
                    self.config["schedule_time"] = f"{hour:02d}:{minute:02d}"
                    self.engine.save_config()
                self.engine.reschedule()
                self.engine.ensure_background()
                yield event.plain_result(prefix + "每天生成时间已改为 " + self.config["schedule_time"] + "。")
            elif action == "umo":
                argument = argument or "set"
                if argument == "set":
                    async with self.engine.config_lock:
                        self.config["default_reference_umo"] = str(event.unified_msg_origin)
                        self.engine.save_config()
                    message = "已将当前会话设为日程参考来源。"
                elif argument == "show":
                    message = "默认参考会话：" + str(self.config.get("default_reference_umo") or "未配置")
                elif argument == "clear":
                    async with self.engine.config_lock:
                        self.config["default_reference_umo"] = ""
                        self.engine.save_config()
                    message = "已清除默认参考会话。"
                else:
                    raise ValueError("用法：参考会话 [set/show/clear]")
                yield event.plain_result(prefix + message)
        except Exception as exc:
            log.warning("生活日程指令 %s 失败：%s", action, describe_error(exc))
            yield event.plain_result(user_error(exc, action="更新生活安排"))

    async def llm_get_schedule(self, event: AstrMessageEvent) -> str:
        """查询角色今日全天各时段完整日程、待办约定（仅主人可查）及正在进行的近期跨天大事件，补充 <character_life> 未展示的今日时段和近期大事。工具只返回当前已有记录。更远日期没有相应记录时，按角色的平常作息自然回答，明确只是平常安排，不当作当天已确定的行程，无需重复调用。"""
        try:
            await self._require_current_life(event)
            start, _ = self.engine.window()
            day = self.engine.store.day(start.date().isoformat())
            text = self._day_text(day) if day and day.get("status") == "ok" else "日程正在准备中。"
            if self.is_owner(event):
                pending = [
                    f"{item['content']}（{item.get('due') or '无具体时间'}）"
                    for item in self.engine.promises.all() if item.get("status") == "pending"
                ]
                if pending:
                    text += "\n待办约定：" + "；".join(pending)
            return text
        except Exception as exc:
            log.warning("日程只读工具失败：%s", describe_error(exc))
            return "日程暂时未能读取。"

    async def llm_wardrobe_list(self, event: AstrMessageEvent) -> str:
        """列出衣柜最新编号、稳定ID、描述、分类和季节；只供主人使用。"""
        try:
            if not self.is_owner(event):
                return "只有主人可以使用衣柜工具。"
            await self._require_current_life(event)
            return dump(self.engine.wardrobe.display_entries())
        except Exception as exc:
            log.warning("衣柜列表工具失败：%s", describe_error(exc))
            return "衣柜读取失败。"


    async def llm_wardrobe_remove(self, event: AstrMessageEvent, query: str) -> str:
        """按主人要求删除一套穿搭，先列出衣柜核对编号。

        Args:
            query(string): 稳定ID、穿搭N或唯一描述关键词。
        """
        try:
            if not self.is_owner(event):
                return "只有主人可以使用衣柜工具。"
            await self._require_current_life(event)
            entry = self.engine.wardrobe.resolve(query)
            if not entry:
                return "没有找到唯一匹配的穿搭。"
            async with self.engine.config_lock:
                self.engine.setup.assert_owner(event.get_extra("life_plus_token", ""))
                self.engine.wardrobe.remove(entry["id"])
                self.wardrobe_images.remove(entry["id"])
            return "已从衣柜删除：" + entry["description"]
        except Exception as exc:
            log.warning("衣柜删除工具失败：%s", describe_error(exc))
            return "删除失败：" + user_error(exc, action="删除穿搭")

    async def llm_wardrobe_edit(self, event: AstrMessageEvent, query: str, instruction: str) -> str:
        """按主人明确要求修改穿搭，保留其余造型细节。

        Args:
            query(string): 稳定ID、穿搭N或唯一描述关键词。
            instruction(string): 用户的穿戴修改要求，如换鞋或赤足。
        """
        try:
            if not self.is_owner(event):
                return "只有主人可以使用衣柜工具。"
            await self._require_current_life(event)
            entry = self.engine.wardrobe.resolve(query)
            if not entry:
                return "没有找到唯一匹配的穿搭。"
            normalized = await self.engine.llm.describe_outfit([], "按要求修改，保留其他穿戴细节，明确赤足等约束。原方案：" + entry["description"] + "\n修改：" + instruction)
            async with self.engine.config_lock:
                self.engine.setup.assert_owner(event.get_extra("life_plus_token", ""))
                current = self.engine.wardrobe.resolve(entry["id"])
                if current.get("description") != entry.get("description"):
                    return "这套穿搭刚刚被修改，请重新查看衣柜后再试。"
                updated = self.engine.wardrobe.update(entry["id"], **normalized)
            return "已修改衣柜方案：" + dump(updated)
        except Exception as exc:
            log.warning("衣柜修改工具失败：%s", describe_error(exc))
            return "修改失败：" + user_error(exc, action="修改穿搭")

    async def _require_current_life(self, event):
        await self.engine.setup.refresh(automatic=True)
        if not self.engine.life_ready():
            raise ValueError(self.engine.setup.status()["message"])
        self.engine.setup.assert_owner(event.get_extra("life_plus_token", ""))
