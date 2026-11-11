"""Proactive messages for one target private chat (item 10 extension, 8, 9, 14, 17).

Each target has its own ledger, next due time and counters. A tick runs about
every PROACTIVE_POLL_SECONDS (20 s, chat/main.py) and goes through, in order:

1. fixed-time reasons with their own clock (due promise, care follow-up,
   expired "wait for me", a natural point in the day to miss the user);
2. the random next time, checked against quiet hours, the unanswered limit,
   the daily count and whether the character is free.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from astrbot.api import logger

from ...common.text import (
    SegmentSettings,
    extract_json,
    media_side_note,
    split_bubbles,
    strip_media_markers,
    strip_voice_tags,
)
from . import timing
from .delivery_error import ConversationGone
from .triggers import (
    HOLD_ASK_ATTEMPTS,
    Trigger,
    contact_hold,
    due_care,
    due_promise,
    expired_hold,
    hold_active,
    hold_attempts,
    hold_pending,
    hold_retry_delay,
    is_free,
    miss_you_moment,
    slot_time,
    topic_candidates,
)

_PHOTO_TYPES = {"selfie", "scene", "food", "outfit"}
_SHOT_FIELDS = {"capture", "framing", "angle", "action", "expression"}
_STATUSES = {"sent", "failed", "cancelled", "stopped", "unknown"}


def parse_plan(raw, allowed):
    # C1: tolerate a preface, a code fence or trailing words around the JSON.
    plan = extract_json(raw)
    if not isinstance(plan, dict):
        raise ValueError("主动计划必须是 JSON 对象")
    medium = plan.get("medium")
    if medium == "skip":
        return None
    text = plan.get("text")
    if medium not in ("text", "voice") or not isinstance(text, str) or not text.strip():
        raise ValueError("主动计划缺少有效的发送方式或正文")
    text = strip_media_markers(text.strip())
    if not text:
        raise ValueError("主动计划缺少有效的正文")
    medium = medium if allowed["voice"] else "text"
    photo = plan.get("photo") if allowed["photo"] and medium == "text" else None
    if photo is not None:
        if not isinstance(photo, dict):
            raise ValueError("主动照片计划必须是 JSON 对象")
        brief = photo.get("brief")
        photo_type = photo.get("type")
        shot = photo.get("shot_plan")
        if (
            not isinstance(brief, str)
            or not brief.strip()
            or not isinstance(photo_type, str)
            or photo_type not in _PHOTO_TYPES
            or not isinstance(shot, dict)
            or not shot
            or set(shot) - _SHOT_FIELDS
            or any(not isinstance(value, str) or not value.strip() for value in shot.values())
        ):
            raise ValueError("主动照片计划缺少拍摄意图、类型或拍摄细节")
        photo = {"brief": brief.strip(), "type": photo_type, "shot_plan": [shot]}
    evidence = plan.get("promise_evidence")
    topic = plan.get("topic")
    return {
        "text": text,
        "medium": medium,
        "photo": photo,
        "topic": topic.strip() if isinstance(topic, str) else "",
        "promise_evidence": evidence.strip() if isinstance(evidence, str) and evidence.strip() in text else "",
    }


def _ago(then: str, now: datetime) -> str:
    if not then:
        return ""
    minutes = max(0, int((now - slot_time(then, now)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时{f' {rest} 分钟' if rest else ''}前"


def _clock(value: str, now: datetime) -> str:
    if not value:
        return ""
    when = slot_time(value, now)
    return when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%m-%d %H:%M")


def wait_reply_facts(facts: dict, state: dict, now: datetime) -> str:
    """Item 3: facts for the reply after unanswered proactive messages."""
    lines = [
        "【回复参考事实】",
        f"在对方这条消息之前，你主动找了对方 {facts['count']} 轮（每轮可能含几条消息）对方都没回，"
        f"第一轮是 {_ago(facts['first_sent_at'], now)}（{_clock(facts['first_sent_at'], now)}）。",
    ]
    if facts.get("user_last_before"):
        lines.append(
            f"对方上一次说的话（{_clock(facts['user_last_before_at'], now)}）：{facts['user_last_before'][:120]}"
        )
    lines.append(f"现在是 {now.strftime('%H:%M')}。")
    reaction = state.get("wait_reply_reaction")
    recover = state.get("wait_reply_recover")
    if reaction or recover:
        lines.append(
            "人设参考（不是硬性要求）："
            + "；".join(
                filter(
                    None,
                    (
                        f"没等到回复时的在意程度：{reaction}" if reaction else "",
                        f"情绪恢复方式：{recover}" if recover else "",
                    ),
                )
            )
        )
    lines.append(
        "隔了一整晚没回、或者对方上一句是“晚安”这类收尾，都是正常的没回，不算冷落。"
        "要不要流露情绪、怎么接话，都按人设自己决定。"
    )
    return "\n".join(lines)


class Coordinator:
    def __init__(self, settings, target, ledger, linked, bridge, history):
        self.settings = settings
        self.target = target
        self.ledger = ledger
        self.linked = linked
        self.bridge = bridge
        self.history = history
        self.closed = False

    # -- incoming ------------------------------------------------------------
    def _now(self) -> datetime:
        state = self.linked.state()
        if state.get("now"):
            return datetime.fromisoformat(state["now"])
        return datetime.now(timezone.utc).astimezone()

    def message_seen(self, text: str) -> dict | None:
        """Record a user message. Returns facts about unanswered proactive messages."""
        now = self._now()
        due = timing.next_due(now, self.settings.min_interval_minutes, self.settings.max_interval_minutes)
        return self.ledger.message_seen(now, text, contact_hold(text), due)

    def reply_seen(self):
        self.ledger.reply_seen(self._now())

    def close(self):
        self.closed = True

    def owner_event(self, before_send=None):
        bot_self_id = self.ledger.data["bot_self_id"]
        platform_name = self.ledger.data["platform_name"]
        if not bot_self_id or not self.bridge.target_available(bot_self_id, platform_name):
            return None
        event = self.bridge.build_media_event(
            bot_self_id=bot_self_id,
            platform_name=platform_name,
            before_send=before_send,
        )
        return event if event and self.linked.is_owner(event) else None

    # -- choosing ------------------------------------------------------------
    def _reschedule(self, now: datetime) -> None:
        self.ledger.set_due(
            timing.next_due(now, self.settings.min_interval_minutes, self.settings.max_interval_minutes)
        )

    def _quiet(self, now: datetime) -> bool:
        return self.settings.quiet_hours_enabled and timing.in_quiet_hours(now, self.settings.quiet_hours)

    def _limits_reached(self, now: datetime) -> bool:
        data = self.ledger.data
        return (
            self.settings.unanswered_reached(data["unanswered"])
            or self.ledger.day(now.date().isoformat())["rounds"] >= self.settings.daily_max
        )

    def _chatting(self, now: datetime, window: timedelta | None = None) -> bool:
        """A message went either way within ``window`` (default: the shortest proactive interval)."""
        data = self.ledger.data
        recent = [
            slot_time(value, now) for value in (data["last_seen"], data["last_reply"], data["last_proactive"]) if value
        ]
        if window is None:
            window = timedelta(minutes=self.settings.min_interval_minutes)
        return bool(recent) and now - max(recent) < window

    def _miss_you_allowed(self, now: datetime) -> bool:
        """At most once a day, and never in the middle of a chat."""
        return self.ledger.day(now.date().isoformat())["miss_you"] < 1 and not self._chatting(now)

    def choose(self, state: dict, now: datetime) -> Trigger | None:
        """Pick this tick's reason, updating the next due time when needed."""
        data = self.ledger.data
        considered = data["completed"].keys() | data["considered"].keys()
        events = self.linked.user_events(self.target)
        free = is_free(state.get("current_slot"))

        promise = due_promise(state, considered, now)
        if promise and free:
            return promise
        for fixed in (due_care(events, considered, now), expired_hold(data, self.settings.hold_hours, now)):
            if fixed and free and not self._quiet(now) and not self._limits_reached(now):
                # G-1: the reminder also waits while a chat is going on; the gap is the
                # retry delay (shortest interval kept within 30–120 minutes) so a long
                # shortest interval cannot push it far past the hold time.
                if fixed.kind == "hold" and self._chatting(now, hold_retry_delay(self.settings.min_interval_minutes)):
                    continue
                return fixed
        # G-1: until the expired reminder is sent or dropped, no miss-you and no random message.
        holding = hold_active(data, self.settings.hold_hours, now) or hold_pending(data, self.settings.hold_hours, now)
        if self._miss_you_allowed(now) and not self._quiet(now) and not self._limits_reached(now):
            moment = miss_you_moment(state, considered, now)
            if moment and not holding:
                return moment

        due = self.ledger.due()
        if due is None:
            if not self.settings.unanswered_reached(data["unanswered"]):
                # First tick after upgrade, or the user replied after a stop.
                self._reschedule(now)
            return None
        if now < due:
            return None
        if self._quiet(now):
            self._reschedule(now)
            return None
        if self.settings.unanswered_reached(data["unanswered"]):
            self.ledger.set_due(None)  # Wait for the user to come back.
            return None
        day = self.ledger.day(now.date().isoformat())
        if day["rounds"] >= self.settings.daily_max:
            self.ledger.set_due(
                timing.next_day_due(now, self.settings.min_interval_minutes, self.settings.max_interval_minutes)
            )
            return None
        if holding:
            self._reschedule(now)
            return None
        if not free:
            if data["waiting_free_since"] != "busy":
                data["waiting_free_since"] = "busy"
                self.ledger.save()
            return None
        if data["waiting_free_since"] == "busy":
            # Just became free: send within a few minutes, not right at the edge.
            data["waiting_free_since"] = (now + timing.free_grace()).isoformat()
            self.ledger.save()
            return None
        if data["waiting_free_since"] and now < slot_time(data["waiting_free_since"], now):
            return None
        topics = topic_candidates(state, considered, events, now, miss_you_allowed=self._miss_you_allowed(now))
        return Trigger(
            f"timed:{due.isoformat()}",
            "",
            "timed",
            None,
            topics=tuple(json.dumps(topic, ensure_ascii=False) for topic in topics),
        )

    def _task(self, trigger: Trigger, now: datetime) -> dict:
        data = self.ledger.data
        task: dict = {}
        if trigger.kind == "timed":
            task["本轮情况"] = (
                "你忽然想起了对方。先想想现在想不想开口；想的话，从可选话题里挑一个你此刻最自然会说起的，都不合适就 skip。"
            )
            task["可选话题"] = [json.loads(topic) for topic in trigger.topics]
        else:
            task["触发原因"] = trigger.reason
            task["话题 id"] = trigger.key
            if trigger.promise:
                task["到期告知约定原文"] = trigger.promise
            if trigger.fact:
                task["本轮具体事实"] = trigger.fact
        task["当前时间"] = now.strftime("%Y-%m-%d %H:%M")
        last = max(
            (value for value in (data["last_seen"], data["last_reply"], data["last_proactive"]) if value),
            key=lambda value: slot_time(value, now),
            default="",
        )
        if last:
            task["最后一条消息时间"] = _clock(last, now)
        task["你连续主动找对方、对方还没回的轮数"] = data["unanswered"]
        return task

    # -- one tick ------------------------------------------------------------
    async def tick(self):
        data = self.ledger.data
        bot_self_id = data["bot_self_id"]
        target = self.target
        scope = self.linked.proactive_scope(target, bot_self_id)
        if scope is None:
            return
        await self.finish_pending()
        if data["pending"] is not None:
            return
        state = self.linked.state()
        if not state.get("current_slot") or not state.get("persona_id") or not data["last_seen"]:
            return
        if data["blocked_after_message"] == data["message_version"]:
            return
        owner = self.owner_event()
        if owner is None:
            return
        now = datetime.fromisoformat(state["now"])
        trigger = self.choose(state, now)
        if trigger is None or await self.linked.blocked(target, bot_self_id):
            return
        conversation_id = await self.bridge.current_conversation_id()
        persona_id = state["persona_id"]
        if not conversation_id or not await self.bridge.persona_matches(conversation_id, persona_id):
            return

        message_version = data["message_version"]
        last_reply = data["last_reply"]
        day = now.date().isoformat()
        promise = next(
            (
                row
                for row in state.get("promises_due", [])
                if f"promise:{row['id']}" == trigger.key and row["type"] == "tell" and row["status"] == "pending"
            ),
            None,
        )

        def still_current():
            if self.closed or data["message_version"] != message_version or data["last_reply"] != last_reply:
                return False
            if self.linked.proactive_scope(target, bot_self_id) != scope:
                return False
            latest = self.linked.state()
            if latest.get("persona_id") != persona_id or not latest.get("now"):
                return False
            latest_now = datetime.fromisoformat(latest["now"])
            if trigger.valid_until is not None and latest_now >= trigger.valid_until:
                return False
            if (latest_now - now).total_seconds() > 15 * 60:
                return False
            return (
                is_free(latest.get("current_slot"))
                and (promise is None or promise in latest.get("promises_due", []))
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
            "id": uuid4().hex,
            "key": trigger.key,
            "day": day,
            "kind": trigger.kind,
            "started_at": now.isoformat(),
            "message_version": message_version,
            "conversation_id": conversation_id,
            "persona_id": persona_id,
            "promise": promise,
            "promise_evidence": "",
            "counted": trigger.counted,
            "miss_you": trigger.kind == "miss_you",
            "topic": "",
            "deliveries": [],
            "recorded": 0,
            "send_in_flight": False,
            "promise_done": False,
            "memory_done": False,
            "memory_epoch": memory_epoch,
            "topics": [trigger.key] if trigger.kind != "timed" else [],
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
            "voice": owner.supports_voice
            and self.settings.voice_enabled
            and await self.linked.voice(target) is not None,
            "photo": owner.supports_photo
            and self.settings.photo_enabled
            and await self.linked.photo(target) is not None
            and self.ledger.day(day)["photos"] < self.settings.daily_photo_max,
        }
        if not await before_send():
            await self.finish_pending()
            return
        lines = await self.history.recent_lines(
            target,
            self.bridge.platform_id,
            self.settings.history_count,
            now,
        )
        if trigger.kind == "hold":
            # C2: count the try now, mark it done only after a real send.
            self.ledger.hold_attempt(now, hold_retry_delay(self.settings.min_interval_minutes))
        try:
            raw = await self.bridge.generate(
                state,
                self._task(trigger, now),
                self.settings.proactive_prompt,
                allowed,
                conversation_id=conversation_id,
                event=memory_event,
                recall=recall,
                history_lines=lines,
                history_limit=self.settings.history_count,
            )
            plan = parse_plan(raw, allowed)
        except Exception:
            if trigger.kind == "timed":
                self._reschedule(now)
            self._hold_unsent(trigger)
            raise
        if plan is None:
            logger.info("[自然私聊] %s 本轮模型选择不打扰。", target)
            if trigger.kind == "timed":
                self._reschedule(now)
            self._hold_unsent(trigger)
            await self.finish_pending()
            return
        self._note_topic(turn, plan["topic"], trigger)
        turn["promise_evidence"] = plan["promise_evidence"]
        await self.deliver(turn, plan, before_send, acquire_send, release_send)
        if turn["deliveries"]:
            self._reschedule(self.clock(turn))
            if trigger.kind == "care" or turn["topic"].startswith("care:"):
                await self.linked.care_followed(target, turn["topic"].removeprefix("care:"))
        elif trigger.kind == "timed":
            self._reschedule(now)
        else:
            self._hold_unsent(trigger)
        await self.finish_pending()

    def _hold_unsent(self, trigger: Trigger) -> None:
        data = self.ledger.data
        if trigger.kind != "hold" or data["hold"] != "user" or data["hold_notified"] or not hold_attempts(data):
            return
        if hold_attempts(data) >= HOLD_ASK_ATTEMPTS:
            logger.info(
                "[自然私聊] %s “忙完来找你”的到期提醒试了 %s 次都没发出，不再追问，等对方回来。",
                self.target,
                hold_attempts(data),
            )
        else:
            logger.info(
                "[自然私聊] %s “忙完来找你”的到期提醒这轮没发出，%s 后再试。", self.target, data["hold_retry_at"]
            )

    def _note_topic(self, turn: dict, topic: str, trigger: Trigger) -> None:
        if trigger.kind != "timed":
            turn["topic"] = trigger.key
            return
        known = {json.loads(item)["id"] for item in trigger.topics}
        topic = topic if topic in known else "life"
        turn["topic"] = topic
        turn["miss_you"] = topic.startswith("miss_you:")
        if topic != "life":
            self.ledger.data["considered"][topic] = turn["day"]
            self.ledger.save()

    async def deliver(self, turn, plan, before_send, acquire_send, release_send):
        if not await before_send():
            return
        status = "failed"
        voice = await self.linked.voice(self.target) if plan["medium"] == "voice" else None
        if voice is not None:
            event = self.owner_event(acquire_send)
            if event is None:
                return
            try:

                async def before_voice():
                    return await before_send() and await self.linked.voice(self.target) is not None

                response = await voice(event, plan["text"], before_send=before_voice)
                status = await self.accept_media(turn, response, event, {"voice", "text"})
            finally:
                release_send()
        if status == "failed" and not turn["deliveries"]:
            status = await self.deliver_text(turn, plan["text"], acquire_send, release_send)
        if status != "sent" or not plan["photo"] or not await before_send():
            return
        photo = await self.linked.photo(self.target)
        if photo is None:
            return
        shot = plan["photo"]

        async def before_photo():
            if not await before_send() or await self.linked.photo(self.target) is None:
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
                event,
                shot["brief"],
                photo_type=shot["type"],
                shot_plan=shot["shot_plan"],
                before_send=before_photo,
            )
            await self.accept_media(turn, response, event, {"photo"})
        finally:
            release_send()

    async def deliver_text(self, turn, text, acquire_send, release_send):
        """Plain text follows AstrBot's segmented reply setting (item 6)."""
        segments = SegmentSettings.from_context(self.bridge.context, self.target)
        parts = split_bubbles(strip_media_markers(strip_voice_tags(text)), segments)
        if not parts:
            return "cancelled"
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(segments.delay())
            try:
                response = await self.bridge.send_text(
                    part,
                    bot_self_id=self.ledger.data["bot_self_id"],
                    platform_name=self.ledger.data["platform_name"],
                    before_send=acquire_send,
                )
                status = response["status"]
                if status == "sent":
                    if index == len(parts) - 1 and len(parts) > 1:
                        response["promise_source_text"] = text
                    await self.accept(turn, [response])
                elif status == "unknown":
                    self.note_unknown(turn)
                    return status
                elif response.get("reply_required"):
                    self.wait_for_message(turn)
                    return "cancelled"
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
        if getattr(event, "reply_required", False) is True:
            self.wait_for_message(turn)
            return "cancelled"
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
            await self.accept(
                turn,
                [
                    {
                        **item,
                        "status": "sent",
                        "platform_message_id": event.platform_message_id,
                    }
                ],
            )
        else:
            self.ledger.finish_send(turn)
        return status

    def wait_for_message(self, turn):
        self.ledger.wait_for_message(turn)
        logger.info("[自然私聊] QQ 官方拒绝主动发送，收到对方新消息后再评估。")

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
        self.ledger.accept(turn, deliveries, now.date().isoformat())
        for item in deliveries:
            await self.history.record_outgoing(
                self.target,
                self.bridge.platform_id,
                media_side_note(item["medium"], item.get("text", "")),
                self.ledger.data["bot_self_id"],
            )
        await self.record_pending(turn)

    async def record_pending(self, turn):
        for index in range(turn["recorded"], len(turn["deliveries"])):
            try:
                await self.bridge.record(
                    f"{turn['id']}-{index}",
                    [turn["deliveries"][index]],
                    turn["conversation_id"],
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
            for item in turn["deliveries"]
            if item["medium"] in {"text", "voice"}
        )
        delivered_text = re.sub(r"\s+", "", delivered_text)
        delivered_evidence = re.sub(r"\s+", "", evidence)
        if not turn["promise_done"] and turn["promise"] and evidence and delivered_evidence in delivered_text:
            if not self.linked.state():
                return
            await self.linked.confirm_tell(
                expected_persona_id=turn["persona_id"],
                expected_promise=turn["promise"],
            )
            turn["promise_done"] = True
            self.ledger.save()
        if turn["deliveries"] and not turn["memory_done"]:
            event = self.owner_event()
            if event is None:
                return
            deliveries = turn["deliveries"]
            await self.linked.remember(
                event,
                transaction_id=turn["id"],
                expected_persona_id=turn["persona_id"],
                expected_conversation_id=turn["conversation_id"],
                expected_delivery_epoch=turn.get("memory_epoch"),
                user_messages=[],
                assistant_text="\n".join(media_side_note(item["medium"], item["text"]) for item in deliveries),
                medium=deliveries[0]["medium"],
            )
            turn["memory_done"] = True
            self.ledger.save()
        self.ledger.data["pending"] = None
        self.ledger.save()
