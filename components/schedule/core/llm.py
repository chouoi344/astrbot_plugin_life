"""Small, bounded AstrBot 4.26 provider adapter; never uses a user's session."""

import asyncio
import hashlib
import inspect
import json
import re
import uuid

from astrbot.api import logger as log

from ...common.text import extract_json
from .domain import PROFILE_FIELDS
from .errors import describe_error, run_with_timeout
from .model_queue import ModelQueue

MODEL_TIMEOUTS = {
    "profile": 180,
    "schedule": 180,
    "generation": 180,
    "surprise": 120,
    "event": 120,
    "recap": 120,
    "weather": 120,
    "chat": 60,
    "outfit": 195,
}

# Garment fit (e.g. 修身) and makeup remain valid. Body/face identity and
# composition clauses are deliberately excluded even if a model puts them in
# an otherwise allowed clothing field.
NON_WEARABLE_RE = re.compile(
    r"人物外貌|脸部|脸型|五官|眼睛|双眼皮|鼻梁|鼻子|嘴唇|肤色|皮肤|白皙|身材|体型|胸部|胸围|腰围|三围|长腿|大长腿|"
    r"镜头|构图|背景|场景|姿势|动作|站在|坐在|抬手|俯拍|仰拍|半身照|全身照|"
    r"\b(?:face|facial|eyes?|nose|skin|complexion|body\s*shape|bust|waist|background|camera|composition|pose)\b",
    re.IGNORECASE,
)


def wearable_only(value):
    if not isinstance(value, str):
        return ""
    clauses = re.split(r"[，,；;。\n]+", value)
    return "，".join(clause.strip() for clause in clauses if clause.strip() and not NON_WEARABLE_RE.search(clause))


class NoProviderError(RuntimeError):
    def __init__(self):
        super().__init__("没有可用的模型，请在 AstrBot 里配置")


class PersonaUnavailableError(ValueError):
    pass


class LLMClient:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.timeout = None  # Optional local override, useful for short tests.
        self.timeouts = dict(MODEL_TIMEOUTS)
        self.queue = ModelQueue(self._profile_empty)
        self._jobs = set()
        self._logged = set()
        self.persona_warning = ""

    def _profile_empty(self):
        profile = self.config.get("profile", {})
        return not isinstance(profile, dict) or not any(
            str(profile.get(key, "") or "").strip() for key in PROFILE_FIELDS
        )

    def timeout_for(self, purpose):
        return self.timeout if self.timeout is not None else self.timeouts.get(purpose, 180)

    async def _serial(self, purpose, operation):
        async def run():
            async with self.queue.slot(purpose):
                # Start the clock only after this invocation owns the slot.
                label = "识别穿搭图片" if purpose == "outfit" else "模型调用"
                return await run_with_timeout(operation(), self.timeout_for(purpose), label)

        task = asyncio.create_task(run())
        self._jobs.add(task)
        try:
            return await task
        finally:
            self._jobs.discard(task)

    async def close(self):
        self.queue.close()
        jobs = list(self._jobs)
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def once(self, key, message):
        if key not in self._logged:
            log.info(message)
            self._logged.add(key)

    @staticmethod
    def _persona_value(persona, key, default=""):
        return persona.get(key, default) if isinstance(persona, dict) else getattr(persona, key, default)

    def global_persona_id(self):
        """Read AstrBot's default configuration, never a chat or plugin override."""
        config = self.context.get_config()
        runner = config.get("agent_runner")
        if isinstance(runner, dict):
            values = runner.get("config", {})
            if runner.get("runner_type") == "local":
                persona_id = values.get("persona", {}).get("persona_id", "")
            else:
                persona_id = values.get("persona_id", "")
        else:
            persona_id = config.get("provider_settings", {}).get("default_personality", "")
        return str(persona_id or "").strip()

    async def selected_persona(self, persona_id=None):
        """Resolve the configured global card by its real ID, without a fallback."""
        current_id = self.global_persona_id()
        if persona_id is not None and str(persona_id).strip() != current_id:
            raise PersonaUnavailableError("AstrBot 全局默认人设已改变，本次旧角色结果已停止保存。")
        persona_id = current_id
        if not persona_id:
            raise PersonaUnavailableError("AstrBot 全局默认人设尚未设置，请在 AstrBot 人格设定里选择默认人设。")
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            raise PersonaUnavailableError("暂时读不到 AstrBot 人设列表，请重新加载插件后再试。")
        lookup = getattr(manager, "get_persona_v3_by_id", None) or getattr(manager, "get_persona", None)
        if not callable(lookup):
            raise PersonaUnavailableError("当前 AstrBot 无法读取全局默认人设，请检查人格设定。")
        persona = lookup(persona_id)
        if inspect.isawaitable(persona):
            persona = await run_with_timeout(persona, 10, "读取人设")
        if not persona:
            raise PersonaUnavailableError("AstrBot 全局默认人设已不存在，请在 AstrBot 人格设定里重新选择默认人设。")
        prompt = self._persona_value(persona, "system_prompt") or self._persona_value(persona, "prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PersonaUnavailableError("AstrBot 全局默认人设正文为空，请先在 AstrBot 人格设定里填写。")
        name = self._persona_value(persona, "name") or self._persona_value(persona, "persona_id") or persona_id
        return {"id": persona_id, "name": str(name), "prompt": prompt}

    async def persona(self):
        self.persona_warning = ""
        try:
            return (await self.selected_persona())["prompt"]
        except Exception as exc:
            self.persona_warning = str(exc)
            raise

    @staticmethod
    def persona_hash(persona):
        return hashlib.sha256(persona.encode("utf-8")).hexdigest()

    def reference_umo(self, umo=None):
        umo = str(umo or self.config.get("default_reference_umo", "") or "").strip()
        lookup = getattr(self.context, "get_platform_inst", None)
        if not umo or not callable(lookup):
            return None
        adapter = lookup(umo.split(":", 1)[0])
        if adapter is None:
            return None
        platform = adapter.meta().name
        if platform == "aiocqhttp":
            return umo
        parts = umo.split(":", 2)
        if (
            platform in {"qq_official", "qq_official_webhook"}
            and len(parts) == 3
            and parts[1] == "FriendMessage"
            and parts[2]
        ):
            return umo
        return None

    async def chats(self, umo=None, count=None):
        if count is None:
            count = int(self.config.get("reference_recent_count", 10))
        umo = self.reference_umo(umo)
        if not umo or count <= 0:
            return []
        try:
            manager = self.context.conversation_manager
            cid = await run_with_timeout(manager.get_curr_conversation_id(umo), 10, "读取参考会话")
            if not cid:
                return []
            conversation = await run_with_timeout(manager.get_conversation(umo, cid), 10, "读取参考聊天")
            history = getattr(conversation, "history", None)
            history = json.loads(history) if isinstance(history, str) else history
            return [
                {"role": item.get("role", ""), "content": item.get("content", "")}
                for item in (history or [])[-count:]
                if isinstance(item, dict)
            ]
        except Exception as exc:
            log.warning("读取参考聊天失败：%s", describe_error(exc))
            return []

    async def call(self, prompt, *, provider_id=None, images=None, purpose="generation"):
        return await self._serial(
            purpose, lambda: self._call_provider(prompt, provider_id=provider_id, images=images, purpose=purpose)
        )

    async def _call_provider(self, prompt, *, provider_id=None, images=None, purpose="generation"):
        provider_id = provider_id or self.config.get("llm_provider", "")
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if provider is None:
            provider = self.context.get_using_provider(umo=self.reference_umo())
        if provider is None:
            raise NoProviderError()
        sid = "life_plus_" + purpose + "_" + uuid.uuid4().hex
        try:
            prepared = None
            if images:
                from .image_input import prepare_provider_images

                prepared = await run_with_timeout(prepare_provider_images(images[:4]), 30, "准备模型图片")
            for attempt in range(2):
                response = await provider.text_chat(prompt, session_id=sid, contexts=[], image_urls=prepared)
                for field in ("completion_text", "completion", "text", "content"):
                    value = getattr(response, field, None)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                if attempt == 0:
                    log.warning("模型回复为空，重试一次。")
            raise ValueError("模型连续返回空内容")
        finally:
            # Provider.text_chat has no conversation-persistence side effects in
            # 4.26. Also clean up providers that choose to create a session.
            async def cleanup():
                manager = getattr(self.context, "conversation_manager", None)
                if manager is not None:
                    cid = await manager.get_curr_conversation_id(sid)
                    if cid:
                        await manager.delete_conversation(sid, cid)
                delete = getattr(provider, "delete_session", None)
                if callable(delete):
                    value = delete(sid)
                    if inspect.isawaitable(value):
                        await value

            try:
                await run_with_timeout(cleanup(), 5, "清理临时模型会话")
            except Exception as exc:
                log.debug("清理临时模型会话失败：%s", describe_error(exc))

    async def json(self, prompt, **kwargs):
        return extract_json(await self.call(prompt, **kwargs))

    async def describe_outfit(self, images, text=""):
        return await self._serial("outfit", lambda: self._describe_outfit(images, text))

    async def _describe_outfit(self, images, text):
        prompt = (
            "整理可复用的穿搭。只提取服装、鞋袜、配饰、发型、妆容、颜色材质版型、赤足等穿戴约束；"
            "明确排除人物外貌、脸部特征、身材、肤色、场景、动作、镜头构图，也忽略地点、天气、时间、人物身份、光线、氛围和道具。"
            "看不清的细节留空。suggest category仅外出/居家/睡衣/运动，seasons为春夏秋冬的数组。"
            '只输出JSON：{"overall_style":"","clothing":"","footwear":"","accessories":"",'
            '"hair":"","makeup":"","details":"","constraints":"","category":"外出","seasons":["春","夏","秋","冬"]}。'
            "\n用户文字：" + str(text)
        )
        provider_id = (self.config.get("image_provider") if images else "") or self.config.get("llm_provider")
        for attempt in range(2):
            try:
                # This entire operation already owns one slot and one 195s
                # budget, including a possible JSON repair; never enqueue it
                # recursively or restart its deadline on the second attempt.
                payload = extract_json(
                    await self._call_provider(prompt, images=images, provider_id=provider_id, purpose="outfit")
                )
                fields = [
                    ("overall_style", "整体风格"),
                    ("clothing", "服装"),
                    ("footwear", "鞋袜"),
                    ("accessories", "配饰"),
                    ("hair", "发型"),
                    ("makeup", "妆容"),
                    ("details", "颜色材质与版型"),
                    ("constraints", "穿戴约束"),
                ]
                parts = [
                    label + "：" + cleaned for key, label in fields if (cleaned := wearable_only(payload.get(key)))
                ]
                if not parts:
                    raise ValueError("没有提取到穿戴信息")
                category = payload.get("category", "外出")
                seasons = payload.get("seasons")
                return {
                    "description": "；".join(parts) + "。",
                    "category": category if category in {"外出", "居家", "睡衣", "运动"} else "外出",
                    "seasons": [
                        season for season in ("春", "夏", "秋", "冬") if isinstance(seasons, list) and season in seasons
                    ]
                    or ["春", "夏", "秋", "冬"],
                }
            except ValueError:
                if attempt:
                    raise
                prompt += "\n上次格式或穿搭字段无效，请严格按格式重新输出。"
