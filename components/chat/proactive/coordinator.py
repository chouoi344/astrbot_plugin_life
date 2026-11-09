import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from .delivery_error import ConversationGone
from .triggers import choose_trigger, contact_hold, rest_type, slot_time

logger = logging.getLogger(__name__)
_PHOTO_TYPES = {"selfie", "scene", "food", "outfit"}
_SHOT_FIELDS = {"capture", "framing", "angle", "action", "expression"}
_STATUSES = {"sent", "failed", "cancelled", "stopped", "unknown"}
_WAIT_REACTIONS = {"轻微在意", "有点在意", "会失落"}
_WAIT_RECOVERY = {
    "自动恢复": "先前等回复时的那点情绪已经淡了，按你平时的性格与脾气自然回应即可。",
    "对方回复后恢复": "终于看到对方的消息，先前的在意缓了下来，可以按你的人设性格（如自然接话、散漫调侃一句或傲娇哼一声）回应。",
    "需要对方主动一点": "心里还留着一点等久了的微妙在意，按你的人设脾气（如略带拿乔、嘴硬或简短冷淡一点）回应，给对方留点主动哄你或接话的空间。",
}
# Exact visible controls from the natural voice plugin's core/text.py.
_VISIBLE_VOICE_TAGS = re.compile(
    r"\A(?:\s*【语音】)+[ \t]*|"
    r"\[(?:笑|轻笑|咯咯笑|哭声|抽泣|呜咽|呻吟|哀号|叹气|惊呼|吸气|呼气|喘息|"
    r"耳语|喊叫|惨叫|唱歌|哼歌|结巴|停顿|清嗓|咳嗽|吸鼻子|咂嘴|啧嘴|打哈欠|"
    r"打喷嚏|打嗝|打饱嗝|吞咽|干呕|闷哼|冷笑|哼鼻子)\]"
)


def parse_plan(raw, allowed):
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("主动计划必须是 JSON 对象")
    medium = plan.get("medium")
    if medium == "skip":
        return None
    text = plan.get("text")
    if medium not in ("text", "voice") or not isinstance(text, str) or not text.strip():
        raise ValueError("主动计划缺少有效的发送方式或正文")
    text = text.strip()
    medium = medium if allowed["voice"] else "text"
    photo = plan.get("photo") if allowed["photo"] and medium == "text" else None
    if photo is not None:
        if not isinstance(photo, dict):
            raise ValueError("主动照片计划必须是 JSON 对象")
        brief = photo.get("brief")
        photo_type = photo.get("type")
        shot = photo.get("shot_plan")
        if (
            not isinstance(brief, str) or not brief.strip()
            or not isinstance(photo_type, str) or photo_type not in _PHOTO_TYPES
            or not isinstance(shot, dict) or not shot
            or set(shot) - _SHOT_FIELDS
            or any(not isinstance(value, str) or not value.strip() for value in shot.values())
        ):
            raise ValueError("主动照片计划缺少拍摄意图、类型或拍摄细节")
        photo = {"brief": brief.strip(), "type": photo_type, "shot_plan": [shot]}
    evidence = plan.get("promise_evidence")
    return {
        "text": text,
        "medium": medium,
        "photo": photo,
        "promise_evidence": evidence.strip()
        if isinstance(evidence, str) and evidence.strip() in text else "",
    }


class Coordinator:
    def __init__(self, settings, ledger, linked, bridge):
        self.settings = settings
        self.ledger = ledger
        self.linked = linked
        self.bridge = bridge
        self.closed = False

    def message_seen(self, bot_self_id, platform_name, text):
        state = self.linked.state()
        hold = contact_hold(text)
        data = self.ledger.data
        if hold == "night":
            night_end = state.get("next_night_sleep_end") or ""
        elif not hold and data["hold"] == "night":
            hold, night_end = "night", data["hold_until"]
        else:
            night_end = ""
        self.ledger.message_seen(
            datetime.now(timezone.utc), bot_self_id, platform_name,
            hold if hold != "open" else "", night_end,
        )

    def reply_seen(self, recovery_id=None):
        self.ledger.reply_seen(datetime.now(timezone.utc), recovery_id)

    def reply_recovery(self, conversation_id):
        data = self.ledger.data
        waiting = data["waiting_reply"]
        if (
            not waiting or not waiting["applied"]
            or waiting["conversation_id"] != conversation_id
            or data["message_version"] <= waiting["message_version"]
            or self.linked.state().get("persona_id") != waiting["persona_id"]
        ):
            return None
        tone = _WAIT_RECOVERY[waiting["recover"]]
        return waiting["id"], tone + "结合当前聊天内容与你的人设性格自然发挥，不必做客服式解释。"

    async def _apply_wait_reaction(self, scope):
        data = self.ledger.data
        waiting = data["waiting_reply"]
        if not waiting or waiting["applied"]:
            return

        def still_current():
            return (
                not self.closed and data["waiting_reply"] is waiting
                and data["message_version"] == waiting["message_version"]
                and self.linked.proactive_scope(self.settings.target_umo, data["bot_self_id"]) == scope
            )

        state = self.linked.state()
        if not still_current() or not state.get("day_key"):
            return
        if (
            state["day_key"] != waiting["day"] or state["persona_id"] != waiting["persona_id"]
            or await self.bridge.current_conversation_id() != waiting["conversation_id"]
        ):
            if still_current():
                data["waiting_reply"] = None
                self.ledger.save()
            return
        if await self.linked.update_mood(
            waiting["mood"], expected_persona_id=waiting["persona_id"],
            expected_day=waiting["day"], mutation_id=waiting["id"], still_current=still_current,
        ):
            waiting["applied"] = True
            self.ledger.save()

    def close(self):
        self.closed = True

    def can_start(self, state, now):
        data = self.ledger.data
        current = state["current_slot"]
        if (
            self.closed
            or not data["last_seen"]
            or data["unanswered"] >= state.get("max_unanswered", self.settings.max_unanswered)
            or current["phone"] != "free"
            or rest_type(current) != "none"
            or self.ledger.day(now.date().isoformat())["rounds"] >= self.settings.daily_max
        ):
            return False
        if data["hold"] == "user":
            return False
        if data["hold"] == "night":
            if data["hold_until"]:
                if now < slot_time(data["hold_until"], now):
                    return False
            else:
                previous = state.get("previous_slot")
                hold_started = slot_time(data["hold_started"] or data["last_seen"], now)
                if (
                    not previous
                    or rest_type(previous) != "night_sleep"
                    or slot_time(previous["end"], now) <= hold_started
                ):
                    # Once the schedule catches up, remember its actual next sleep end.
                    if night_end := state.get("next_night_sleep_end"):
                        self.ledger.data["hold_until"] = night_end
                        self.ledger.save()
                    return False
                self.ledger.data["hold_until"] = previous["end"]
                self.ledger.save()
        return True

    def owner_event(self, before_send=None):
        bot_self_id = self.ledger.data["bot_self_id"]
        platform_name = self.ledger.data["platform_name"]
        if not bot_self_id or not self.bridge.target_available(bot_self_id, platform_name):
            return None
        event = self.bridge.build_media_event(
            bot_self_id=bot_self_id, platform_name=platform_name, before_send=before_send,
        )
        return event if event and self.linked.is_owner(event) else None

    async def tick(self):
        data = self.ledger.data
        bot_self_id = data["bot_self_id"]
        target = self.settings.target_umo
        scope = self.linked.proactive_scope(target, bot_self_id)
        if scope is None:
            return
        await self.finish_pending()
        if data["pending"] is not None:
            return
        await self._apply_wait_reaction(scope)
        state = self.linked.state()
        owner = self.owner_event()
        if not state.get("current_slot") or not state.get("persona_id") or owner is None:
            return
        now = datetime.fromisoformat(state["now"])
        if not self.can_start(state, now) or await self.linked.blocked(target, bot_self_id):
            return
        message_version = data["message_version"]
        last_reply = data["last_reply"]
        last_seen = data["last_seen"]
        considered = data["completed"].keys() | data["considered"].keys()
        after = max(
            slot_time(value, now)
            for value in (last_seen, last_reply, data["last_proactive"])
            if value
        )
        trigger = choose_trigger(
            state, considered, after, last_seen=last_seen, silence_hours=self.settings.silence_hours,
        )
        if trigger is None:
            return
        conversation_id = await self.bridge.current_conversation_id()
        persona_id = state["persona_id"]
        if not conversation_id or not await self.bridge.persona_matches(conversation_id, persona_id):
            return

        day = now.date().isoformat()
        promise = next(
            (row for row in state.get("promises_due", [])
             if f"promise:{row['id']}" == trigger.key and row["type"] == "tell"
             and row["status"] == "pending"),
            None,
        )

        def still_current():
            if (
                self.closed
                or data["message_version"] != message_version
                or data["last_reply"] != last_reply
            ):
                return False
            if self.linked.proactive_scope(target, bot_self_id) != scope:
                return False
            latest = self.linked.state()
            slot = latest.get("current_slot")
            if (
                slot != state["current_slot"]
                or latest.get("persona_id") != persona_id
            ):
                return False
            latest_now = datetime.fromisoformat(latest["now"])
            return (
                latest_now < trigger.valid_until
                and slot["phone"] == "free"
                and rest_type(slot) == "none"
                and (promise is None or promise in latest.get("promises_due", []))
                and (not trigger.key.startswith("change:") or trigger.fact in latest.get("changes", []))
                and (not trigger.key.startswith("silence:") or trigger == choose_trigger(
                    latest, considered, after, last_seen=last_seen,
                    silence_hours=self.settings.silence_hours,
                ))
                and self.owner_event() is not None
            )

        async def before_send():
            return (
                still_current()
                and await self.bridge.current_conversation_id() == conversation_id
                and await self.bridge.persona_matches(conversation_id, persona_id)
                and not await self.linked.blocked(target, bot_self_id)
                and still_current()
            )

        recall = await self.linked.recall_for(target)
        memory_epoch = await self.linked.memory_epoch(target)
        if not await before_send():
            return
        memory_event = owner if recall is not None else None

        turn = {
            "id": uuid4().hex, "key": trigger.key, "day": day,
            "started_at": now.isoformat(), "message_version": message_version,
            "conversation_id": conversation_id, "persona_id": persona_id,
            "promise": promise, "promise_evidence": "",
            "deliveries": [], "recorded": 0, "send_in_flight": False,
            "promise_done": False, "memory_done": False, "memory_epoch": memory_epoch,
        }
        ticket = None

        async def acquire_send():
            nonlocal ticket
            if not await before_send():
                return False
            chat, epoch = scope
            ticket = await chat.claim_proactive_send(target, bot_self_id, epoch)
            if ticket is None or not still_current():
                return False
            self.ledger.start_send(turn)
            return True

        def release_send():
            nonlocal ticket
            if ticket is not None:
                scope[0].release_proactive_send(ticket)
                ticket = None

        self.ledger.reserve(turn)
        allowed = {
            "voice": owner.supports_voice and self.settings.voice_enabled
            and await self.linked.voice(self.settings.target_umo) is not None,
            "photo": owner.supports_photo and self.settings.photo_enabled
            and await self.linked.photo(self.settings.target_umo) is not None
            and self.ledger.day(day)["photos"] < self.settings.daily_photo_max,
        }
        if not await before_send():
            await self.finish_pending()
            return
        raw = await self.bridge.generate(
            state, trigger, self.settings.proactive_prompt, allowed,
            conversation_id=conversation_id, event=memory_event, recall=recall,
        )
        plan = parse_plan(raw, allowed)
        if plan is not None:
            turn["promise_evidence"] = plan["promise_evidence"]
            await self.deliver(turn, plan, before_send, acquire_send, release_send)
        await self.finish_pending()
        await self._apply_wait_reaction(scope)

    async def deliver(self, turn, plan, before_send, acquire_send, release_send):
        if not await before_send():
            return
        status = "failed"
        voice = await self.linked.voice(self.settings.target_umo) if plan["medium"] == "voice" else None
        if voice is not None:
            event = self.owner_event(acquire_send)
            if event is None:
                return
            try:
                async def before_voice():
                    return (
                        await before_send()
                        and await self.linked.voice(self.settings.target_umo) is not None
                    )

                response = await voice(event, plan["text"], before_send=before_voice)
                status = await self.accept_media(turn, response, event, {"voice", "text"})
            finally:
                release_send()
        if status == "failed" and not turn["deliveries"]:
            status = await self.deliver_text(turn, plan["text"], acquire_send, release_send)
        if status != "sent" or not plan["photo"] or not await before_send():
            return
        photo = await self.linked.photo(self.settings.target_umo)
        if photo is None:
            return
        shot = plan["photo"]

        async def before_photo():
            if not await before_send() or await self.linked.photo(self.settings.target_umo) is None:
                return False
            now = datetime.fromisoformat(self.linked.state()["now"])
            return self.ledger.day(now.date().isoformat())["photos"] < self.settings.daily_photo_max

        if not await before_photo():
            return

        async def acquire_photo():
            return await before_photo() and await acquire_send()

        event = self.owner_event(acquire_photo)
        if event is None:
            return
        try:
            response = await photo(
                event, shot["brief"], photo_type=shot["type"],
                shot_plan=shot["shot_plan"], before_send=before_photo,
            )
            await self.accept_media(turn, response, event, {"photo"})
        finally:
            release_send()

    async def deliver_text(self, turn, text, acquire_send, release_send):
        visible = _VISIBLE_VOICE_TAGS.sub("", text)
        parts = [part.strip() for part in visible.splitlines() if part.strip()]
        if not parts:
            return "cancelled"
        bubble_limit = 3
        if len(parts) == 1:
            line = parts[0]
            comma_free = "，" not in line and "," not in line
            if comma_free or len(line) <= 80:
                parts = [segment.strip() for segment in re.findall(r".*?[~。？！…]+|.+$", line)]
                if comma_free:
                    bubble_limit = 6
        merged_source = None
        if len(parts) > bubble_limit:
            last = bubble_limit - 1
            merged_source = "\n".join(parts[last:])
            parts = parts[:last] + ["".join(parts[last:])]
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(1)
            try:
                response = await self.bridge.send_text(
                    part, bot_self_id=self.ledger.data["bot_self_id"],
                    platform_name=self.ledger.data["platform_name"], before_send=acquire_send,
                )
                status = response["status"]
                if status == "sent":
                    if merged_source is not None and index == last:
                        response["promise_source_text"] = merged_source
                    await self.accept(turn, [response])
                elif status == "unknown":
                    self.note_unknown(turn)
                    return status
                else:
                    self.ledger.finish_send(turn)
                    return status
            finally:
                release_send()
        return "sent"

    async def accept_media(self, turn, response, event, media):
        status = response["status"]
        if status not in _STATUSES:
            raise ValueError("主动媒体返回未知发送状态")
        event_status = getattr(event, "delivery_status", None)
        if event_status in {"stopped", "cancelled"}:
            self.ledger.finish_send(turn)
            return "cancelled"
        if event_status == "unknown" or status == "unknown":
            self.note_unknown(turn)
            return "unknown"
        deliveries = response["deliveries"]
        if not isinstance(deliveries, list) or len(deliveries) > 1:
            raise ValueError("主动媒体必须返回单张照片或一条语音的回执")
        for item in deliveries:
            if item["medium"] not in media or not isinstance(item["text"], str) or not item["text"].strip():
                raise ValueError("主动媒体回执缺少真实内容")
        if status == "sent":
            if not deliveries or event_status != "sent" or not event.platform_message_id:
                self.note_unknown(turn)
                return "unknown"
            item = deliveries[0]
            text = f"拍摄主题：{item['text']}" if item["medium"] == "photo" else item["text"]
            await self.accept(turn, [{
                **item, "text": text, "status": "sent",
                "platform_message_id": event.platform_message_id,
            }])
        else:
            self.ledger.finish_send(turn)
        return status

    def clock(self, turn):
        state = self.linked.state()
        if state:
            return datetime.fromisoformat(state["now"])
        return datetime.now(datetime.fromisoformat(turn["started_at"]).tzinfo)

    def note_unknown(self, turn):
        self.ledger.unknown_send(turn, self.clock(turn))

    async def accept(self, turn, deliveries):
        now = self.clock(turn)
        turn["delivered_at"] = now.isoformat()
        waiting = None
        if not turn["deliveries"]:
            state = self.linked.state()
            data = self.ledger.data
            limit = state.get("max_unanswered", self.settings.max_unanswered)
            reaction = state.get("wait_reply_reaction")
            if (
                data["unanswered"] + 1 == limit and reaction in _WAIT_REACTIONS
                and state.get("persona_id") == turn["persona_id"] and state.get("day_key")
            ):
                waiting = {
                    "id": turn["id"], "day": state["day_key"],
                    "persona_id": turn["persona_id"], "conversation_id": turn["conversation_id"],
                    "message_version": turn["message_version"], "mood": reaction,
                    "recover": state.get("wait_reply_recover", "自动恢复"), "applied": False,
                }
        self.ledger.accept(turn, deliveries, now.date().isoformat(), waiting)
        await self.record_pending(turn)

    async def record_pending(self, turn):
        for index in range(turn["recorded"], len(turn["deliveries"])):
            try:
                await self.bridge.record(
                    f"{turn['id']}-{index}", [turn["deliveries"][index]], turn["conversation_id"],
                )
            except ConversationGone:
                self.ledger.archive_unmatched(turn)
                return False
            turn["recorded"] = index + 1
            self.ledger.save()
        return True

    async def finish_pending(self):
        turn = self.ledger.data["pending"]
        if not turn:
            return
        if not await self.record_pending(turn):
            return
        if turn["send_in_flight"]:
            self.note_unknown(turn)
        evidence = turn["promise_evidence"]
        delivered_text = "\n".join(
            item.get("promise_source_text", item["text"])
            for item in turn["deliveries"] if item["medium"] in {"text", "voice"}
        )
        # Match only confirmed text, with original line boundaries preserved when
        # overflow was merged. Auto-split punctuation may trim adjacent spaces.
        delivered_text = re.sub(r"(?<=[~。？！…])\s+", "", delivered_text)
        delivered_evidence = re.sub(r"(?<=[~。？！…])\s+", "", evidence)
        if not turn["promise_done"] and turn["promise"] and evidence and delivered_evidence in delivered_text:
            if not self.linked.state():
                return
            await self.linked.confirm_tell(
                expected_persona_id=turn["persona_id"], expected_promise=turn["promise"],
            )
            turn["promise_done"] = True
            self.ledger.save()
        if turn["deliveries"] and not turn["memory_done"] and turn.get("memory_epoch") is None:
            logger.info("[生活主动消息] 本轮没有记忆清空代数，只保留已送达的聊天历史")
            turn["memory_done"] = True
        if turn["deliveries"] and not turn["memory_done"]:
            event = self.owner_event()
            if event is None:
                return
            deliveries = turn["deliveries"]
            text = "\n".join(
                ("[照片] " if item["medium"] == "photo" else "") + item["text"]
                for item in deliveries
            )
            await self.linked.remember(
                event,
                transaction_id=turn["id"],
                expected_persona_id=turn["persona_id"],
                expected_conversation_id=turn["conversation_id"],
                expected_delivery_epoch=turn["memory_epoch"],
                user_messages=[],
                assistant_text=text,
                medium=deliveries[0]["medium"],
            )
            turn["memory_done"] = True
            self.ledger.save()
        self.ledger.data["pending"] = None
        self.ledger.save()
