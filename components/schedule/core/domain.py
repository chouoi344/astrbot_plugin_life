"""Life profile, interaction constraints, promises and multi-day events."""

import copy
import datetime as dt
import json
import random
import re
import uuid
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .errors import describe_error
from .timeline import parse_local, project_slot_rest

PROMISE_MISSED_AFTER = dt.timedelta(hours=2)

PROFILE_FIELDS = (
    "identity",
    "living",
    "routine_workday",
    "routine_restday",
    "weekly_fixed",
    "periodic",
    "vacations",
    "social_circle",
    "hobbies_places",
    "phone_habits",
)
DEFAULT_KEYWORDS = [
    "今晚",
    "今天",
    "明天",
    "后天",
    "等会",
    "待会",
    "一会儿",
    "下班",
    "放学",
    "下课",
    "睡前",
    "起床",
    "记得",
    "答应",
    "约好",
    "说好",
    "一言为定",
    "早点睡",
    "别熬夜",
    "陪我",
    "一起",
    "改天",
    "周末",
    "下周",
    "提醒",
    "语音约定",
]
TIME_TRIGGER_RE = re.compile(
    r"\d{1,2}\s*[点:：]|几点|半(?:个)?小时|(?:\d+|[一二两三四五六七八九十百]+)\s*(?:分钟|小时|天)\s*[后内前]"
)
DIRECT_SCHEDULE_CHANGE_RE = re.compile(
    r"(?:把|将)(?:今天|明天|后天|下午|上午|晚上|早上|中午|早晨|夜里|今晚|日程|安排|行程|计划)[^。！？\n]{0,30}(?:改成|改为|调整为|换成)"
    r"|(?:修改|调整|重排|更改|重写|改一下)[^。！？\n]{0,18}(?:日程|行程|时间表|作息|安排)"
    r"|(?:日程|行程|作息|安排)[^。！？\n]{0,12}(?:改成|改为|调整为|换成|提前|推迟)"
)
MOOD_TRIGGER_RE = re.compile(
    r"开心|高兴|兴奋|感动|惊喜|吓到|难过|伤心|失落|委屈|生气|着急|担心|害怕|心疼|沮丧|崩溃|心情|"
    r"吵架|分手|和好|失恋|通过了|考上了|没考上|被录取|升职|辞职|失业"
)
# Ordinary voice messages are available; live calls keep their configured limits.
RESTRICT_VOICE_MESSAGES = False
VOICE_MESSAGE_TERMS = ("语音消息", "语音留言", "语音条", "语音短信")
_VOICE_MESSAGE_QUANTITY = r"(?:一(?:条|段|个|则|下)?|条|段|个|则|几(?:条|段))?"
_VOICE_MESSAGE_ACTION = re.compile(
    r"(?:发(?!起)(?:送)?|回(?:复)?|留|录(?:制)?|听(?:听)?)"
    r"(?:(?!和|跟|与|通话|聊天|实时|发起|连线|连麦|打|接)[^。！？\n，,；;]){0,18}$"
)
_VOICE_MESSAGE_SEND = re.compile(r"(?:发(?:送)?|回(?:复)?|留)" + _VOICE_MESSAGE_QUANTITY + r"$")
_VOICE_MESSAGE_SUFFIX = re.compile(r"^(?:消息|留言|条|短信|给)")
_VOICE_MESSAGE_MANNER = re.compile(r"(?:用|通过)$")
VOICE_CALL_TERMS = ("语音通话", "语音聊天", "打电话", "通电话", "打语音", "语音电话")
_VOICE_CANCEL_LABEL = re.compile(
    r"^(?:"
    + "|".join(re.escape(word[2:]) for word in VOICE_MESSAGE_TERMS + VOICE_CALL_TERMS if word.startswith("语音"))
    + r")"
)
_VOICE_ONLY_KEYWORDS = {"语音", *VOICE_MESSAGE_TERMS, "发语音", "听语音", "录语音", "发条语音", "听一句语音"}
_VOICE_COMMITMENT = re.compile(
    r"明晚|以后|今后|往后|下次|每(?:天|晚|周|次)|约定|承诺|答应|说好|约好|一言为定|晚点|稍后|随后|回头|有空|空了"
    r"|(?:等|待)[^。！？\n]{0,12}(?:完|结束|回来)|(?:完|结束|回来)(?:再|就|后|给|发|录)"
)
_VOICE_CONTENT = re.compile(r"语音|录(?:制)?[^。！？\n]{0,8}(?:晚安|留言)")

FORBIDDEN_ALIASES = {
    "连麦": ("连麦", "开麦连线", "开麦", "语音连线"),
    "语音通话": VOICE_CALL_TERMS + ("语音",),
    "视频通话": ("视频通话", "视频聊天", "开视频", "打视频", "视频"),
    "一起打游戏": ("一起打游戏", "一起玩游戏", "打游戏", "玩游戏", "开黑", "联机游戏"),
    "线下见面": ("线下见面", "线下约会", "见面", "当面", "去找你", "来找你", "找你", "陪你逛街", "拜访你"),
}
_CLAUSE_PARTS = re.compile(r"([。！？!?；;，,\r\n]+)")
_SENTENCE_PARTS = re.compile(r"([。！？!?\r\n]+|(?<!\d)\.(?=\s|$))")
_OTHER_PERSON = r"(?:家人|父母|爸爸|妈妈|爸妈|母亲|父亲|朋友|闺蜜|室友|同学|同事|老师|邻居|哥哥|姐姐|弟弟|妹妹)"
_NEGATIVE_INTERACTION = re.compile(r"(?:不|没|别|无法|拒绝|推掉|取消|禁止|避免|谢绝|无需)[^，。；;\n]{0,7}$")
_AFFIRMATIVE_INTERACTION = re.compile(
    r"(?:别忘|不要忘|不能不|不得不|不拒绝|不会拒绝|不再拒绝|没有拒绝|不用拒绝|无需拒绝|不介意|不妨|不只是|不仅|不止|不久|没多久)[^，。；;\n]{0,7}$"
)
_POST_CANCELLED_INTERACTION = re.compile(
    r"^(?:这件事|的安排|的约定|计划|约定|请求|通话|聊天)?(?:已经|已|已被|决定|临时|暂时|最终|就|被|都|全部|还是)*(?:取消|作罢|推掉|拒绝|算了|暂停)(?:了|吧|掉)?\s*$"
)
_VIDEO_CONTENT_ACTION = re.compile(
    r"(?:观看|看|刷)(?:手机上的|手机上|手机里|屏幕上|网上|你发的|你分享的|这段|那段|一段|一个|几个|过|了|短|小|的)*$"
    r"|(?:分享|转发|录|拍|剪辑|推荐|(?<!出)发)(?:给你|给主人|给用户|一段|一个|这段|那段|短|小|的|了)*$"
)


def has_change_trigger(user, reply, keywords=None):
    text = str(user or "") + "\n" + str(reply or "")
    choices = DEFAULT_KEYWORDS if keywords is None else keywords
    if not any(str(word).strip() for word in choices):
        return True
    # New voice promises must work with already-saved keyword lists. Generic
    # voice words alone describe the delivery medium, not a changed schedule.
    voice_promise = bool(_VOICE_CONTENT.search(text) and _VOICE_COMMITMENT.search(text))
    return (
        any(
            str(word).strip() not in _VOICE_ONLY_KEYWORDS and str(word).strip() and str(word).strip() in text
            for word in choices
        )
        or bool(TIME_TRIGGER_RE.search(text))
        or bool(DIRECT_SCHEDULE_CHANGE_RE.search(text))
        or bool(MOOD_TRIGGER_RE.search(text))
        or voice_promise
    )


def effective_forbidden_interactions(forbidden):
    """Read legacy voice labels as live-call limits without rewriting settings."""
    choices = (forbidden,) if isinstance(forbidden, str) else forbidden or ()
    result = []
    for value in choices:
        term = str(value or "").strip()
        if not RESTRICT_VOICE_MESSAGES and term in {"语音", *VOICE_MESSAGE_TERMS}:
            term = "语音通话"
        if term and term not in result:
            result.append(term)
    return result


def user_aliases_from_config(config):
    """Optional explicit owner nicknames; host bot nicknames are unrelated."""
    aliases = []
    sources = [config]
    profile = config.get("profile", {}) if isinstance(config, Mapping) else {}
    if isinstance(profile, Mapping):
        sources.append(profile)
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("owner_nicknames", "owner_nickname", "user_nicknames", "user_nickname"):
            value = source.get(key, [])
            if isinstance(value, str):
                value = re.split(r"[,，\n]+", value)
            if isinstance(value, (list, tuple)):
                aliases.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    return tuple(dict.fromkeys(aliases))


def _user_references(user_aliases):
    if isinstance(user_aliases, str):
        user_aliases = (user_aliases,)
    aliases = [str(value).strip() for value in user_aliases or () if str(value).strip()]
    # 迷你 is a clothing/object adjective, rather than an address to the user.
    possessed_other = r"(?!的?" + _OTHER_PERSON + r")"
    choices = [r"(?<!迷)你" + possessed_other, r"主人(?!公|翁)" + possessed_other, "用户" + possessed_other]
    choices.extend(re.escape(value) + possessed_other for value in aliases)
    return re.compile("|".join(choices))


def _explicit_other_party(clause, match_start, references):
    """An explicit family/friend receiver wins over incidental user mentions."""
    prefix = clause[:match_start]
    if not references.search(clause) and re.search(_OTHER_PERSON, clause):
        return True
    # Inspect the nearest receiver link, not a 'you' in a different action.
    links = list(re.finditer(r"和|跟|与|陪|给|同|找", prefix))
    if not links:
        return False
    receiver = prefix[links[-1].end() :].strip()
    receiver = re.sub(r"(?:一起|一块|一同|进行|在线|连线|语音|线下|打|玩|开|去|的)+$", "", receiver).strip()
    if not receiver or len(receiver) > 18:
        return False
    # Conjoined user participants still belong to a restricted interaction.
    if re.search(r"(?:" + references.pattern + r")(?:一起)?\s*(?:[、和及跟与给]|以及|还有)", prefix):
        return False
    return not references.search(receiver)


def _clause_has_forbidden(clause, forbidden, user_aliases=(), inherited_user=False):
    if isinstance(user_aliases, str):
        user_aliases = (user_aliases,)
    references = _user_references(user_aliases)
    if not inherited_user and not references.search(clause):
        return False
    choices = effective_forbidden_interactions(forbidden)
    for configured in choices:
        term = str(configured or "").strip()
        if not term:
            continue
        aliases = FORBIDDEN_ALIASES.get(term, (term,))
        voice_related = term in {"语音通话", "语音", *VOICE_MESSAGE_TERMS}
        if voice_related and RESTRICT_VOICE_MESSAGES:
            aliases += VOICE_MESSAGE_TERMS
        if term == "线下见面":
            aliases += tuple("找" + name for name in ("主人", "用户", *(user_aliases or ())))
        for alias in aliases:
            for match in re.finditer(re.escape(alias), clause, re.IGNORECASE):
                prefix = clause[max(0, match.start() - 12) : match.start()]
                suffix = clause[match.end() :].strip()
                voice_message = False
                if voice_related:
                    if alias == "语音" and re.match(r"^(?:输入|识别|转文字|转写)", suffix):
                        continue
                    voice_message = alias not in VOICE_CALL_TERMS and bool(
                        alias in VOICE_MESSAGE_TERMS
                        or any(clause[match.start() :].startswith(word) for word in VOICE_MESSAGE_TERMS)
                        or _VOICE_MESSAGE_ACTION.search(clause[: match.start()])
                        or _VOICE_MESSAGE_SUFFIX.match(suffix)
                        or (_VOICE_MESSAGE_MANNER.search(prefix) and re.match(r"^(?:说|告诉|回复|回你|发给)", suffix))
                    )
                    if voice_message and not RESTRICT_VOICE_MESSAGES:
                        continue
                    # A receiver after the voice noun is common in messages:
                    # '发条语音给妈妈，你先休息' must keep its third-party target.
                    receiver = re.match(r"^(?:消息|留言|条|短信)?给([^，。！？；;\n]+)", suffix)
                    if (
                        receiver
                        and not references.search(receiver.group(1))
                        and not references.search(clause[: match.start()])
                    ):
                        continue
                cancelled_suffix = (
                    _VOICE_CANCEL_LABEL.sub("", suffix, count=1) if voice_related and alias == "语音" else suffix
                )
                if _POST_CANCELLED_INTERACTION.fullmatch(cancelled_suffix):
                    continue
                self_action = re.search(r"(?:自己|独自|一个人|单独)[^，。；;\n]{0,5}$", prefix)
                if self_action and not references.search(self_action.group()):
                    # Recording alone is ordinary life; sending the recording
                    # to the user is still a voice interaction.
                    user_receiver = re.search(r"给(?:" + references.pattern + r")", suffix)
                    if not (voice_message and (_VOICE_MESSAGE_SEND.search(prefix) or user_receiver)):
                        continue
                if term in {"视频通话", "视频"} and (
                    _VIDEO_CONTENT_ACTION.search(prefix)
                    or re.search(r"(?:聊|谈)(?:一聊|到|了|关于)?$", prefix)
                    or (alias == "视频" and prefix.endswith("短"))
                ):
                    continue
                if _explicit_other_party(clause, match.start(), references):
                    continue
                if _AFFIRMATIVE_INTERACTION.search(prefix) or not _NEGATIVE_INTERACTION.search(prefix):
                    return True
    return False


def _interaction_clauses(text, user_aliases=()):
    """Share user context inside a sentence, never across sentences or fields."""
    sentences = _SENTENCE_PARTS.split(text)
    references = _user_references(user_aliases)
    for sentence_index in range(0, len(sentences), 2):
        sentence = sentences[sentence_index]
        sentence_delimiter = sentences[sentence_index + 1] if sentence_index + 1 < len(sentences) else ""
        inherited = bool(references.search(sentence))
        pieces = _CLAUSE_PARTS.split(sentence)
        for index in range(0, len(pieces), 2):
            delimiter = pieces[index + 1] if index + 1 < len(pieces) else sentence_delimiter
            yield pieces[index], delimiter, inherited


def contains_forbidden(text, forbidden, user_aliases=()):
    """Block configured interactions only when their clause targets the user.

    Structured payloads are inspected field by field. A reference to the user
    in another profile field is never attached to a family or friends' call.
    """
    if isinstance(text, dict):
        return any(contains_forbidden(value, forbidden, user_aliases) for value in text.values())
    if isinstance(text, (list, tuple)):
        return any(contains_forbidden(value, forbidden, user_aliases) for value in text)
    if not isinstance(text, str):
        return False
    if text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
            if isinstance(payload, (dict, list)):
                return contains_forbidden(payload, forbidden, user_aliases)
        except (ValueError, TypeError):
            pass
    return any(
        _clause_has_forbidden(clause, forbidden, user_aliases, inherited)
        for clause, _, inherited in _interaction_clauses(text, user_aliases)
    )


def sanitize_forbidden_text(text, forbidden, user_aliases=()):
    """Remove violating clauses only; return the text and removed-clause count."""
    if not isinstance(text, str):
        return text, 0
    kept, removed = [], 0
    for clause, delimiter, inherited in _interaction_clauses(text, user_aliases):
        if _clause_has_forbidden(clause, forbidden, user_aliases, inherited):
            removed += 1
        else:
            kept.append(clause + delimiter)
    return ("".join(kept).strip(" ，,；;\r\n") if removed else text), removed


def sanitize_forbidden_fields(fields, forbidden, user_aliases=()):
    """Clean each textual field independently without mutating the LLM payload."""
    removed = 0

    def clean(value):
        nonlocal removed
        if isinstance(value, str):
            result, count = sanitize_forbidden_text(value, forbidden, user_aliases)
            removed += count
            return result
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return copy.deepcopy(value)

    return clean(fields), removed


def _zone(value):
    return ZoneInfo(value) if isinstance(value, str) else value


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


class PromiseManager:
    def __init__(self, store, tz):
        self.store, self.tz = store, _zone(tz)
        self.forbidden = ()
        self.user_aliases = ()

    def all(self):
        return self.store.read("promises.json", [])

    def _save(self, values):
        self.store.write("promises.json", values)

    @staticmethod
    def _reason(item, changes):
        reason = str(item.get("note", "") or "")
        for change in reversed(changes or []):
            conflicts = change.get("promise_conflicts", [])
            matched = (
                next(
                    (
                        conflict
                        for conflict in conflicts
                        if isinstance(conflict, dict) and str(conflict.get("id")) == str(item.get("id"))
                    ),
                    None,
                )
                if isinstance(conflicts, list)
                else None
            )
            if matched:
                return str(matched.get("note", "") or "")
            explanation = change.get("summary") or change.get("description") or change.get("reason") or ""
            if explanation and str(item.get("id", "")) in str(explanation):
                return str(explanation)
        if not reason:
            for change in reversed(changes or []):
                explanation = change.get("summary") or change.get("description") or change.get("reason") or ""
                if explanation:
                    return str(explanation)
        return reason

    def refresh(self, now, changes=None):
        now = parse_local(now, self.tz)
        values = self.all()
        changed = False
        for item in values:
            if item.get("status") == "missed" and not item.get("note") and changes:
                reason = self._reason(item, changes)
                if reason:
                    item["note"] = reason
                    changed = True
            if item.get("status") != "pending":
                continue
            try:
                due = parse_local(item["due"], self.tz) if item.get("due") else None
                created = parse_local(item["created_at"], self.tz)
            except (KeyError, ValueError, TypeError):
                logger.warning("[生活日程] 约定日期损坏，跳过：%s", item.get("id", ""))
                continue
            if due is not None and now >= due + PROMISE_MISSED_AFTER:
                item["status"] = "missed"
                item["ended_at"] = now.isoformat()
                # Structured conflicts bind the cause to this promise, so a
                # later unrelated edit cannot replace the actual explanation.
                item["note"] = self._reason(item, changes)
                changed = True
            elif due is None and now >= created + dt.timedelta(days=7):
                item["status"] = "cancelled"
                item["note"] = "七天未设到期时间，已过期"
                item["ended_at"] = now.isoformat()
                changed = True
        if changed:
            self._save(values)
        return copy.deepcopy(values)

    def pending(self, now):
        return [item for item in self.refresh(now) if item.get("status") == "pending"]

    def due_for_day(self, start, end):
        results = []
        for item in self.refresh(start):
            if item.get("status") != "pending":
                continue
            if not item.get("due"):
                if item.get("type") in {"do", "avoid"}:
                    results.append(item)
                continue
            try:
                due = parse_local(item["due"], self.tz)
            except ValueError:
                continue
            if start <= due < end:
                results.append(item)
        return results

    def confirm_tell_delivered(self, expected_promise, now):
        values = self.all()
        for item in values:
            if item != expected_promise or item.get("type") != "tell" or item.get("status") != "pending":
                continue
            if not item.get("id") or not item.get("due"):
                return False
            try:
                due = parse_local(item["due"], self.tz)
            except ValueError:
                return False
            if not due <= now < due + PROMISE_MISSED_AFTER:
                return False
            item.update(status="fulfilled", ended_at=now.isoformat())
            self._save(values)
            return True
        return False

    def apply(self, payload, now, changes=None):
        now = parse_local(now, self.tz)
        values = self.refresh(now, changes)
        changed = []
        additions = payload.get("new_promises", [])
        if not isinstance(additions, list):
            raise ValueError("new_promises 必须是数组")
        for raw in additions:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content", "") or "").strip()
            kind = raw.get("type", "tell")
            if (
                not content
                or kind not in {"tell", "do", "avoid"}
                or contains_forbidden(content, self.forbidden, self.user_aliases)
            ):
                continue
            due = None
            if raw.get("due"):
                try:
                    due = parse_local(raw["due"], self.tz).isoformat()
                except (ValueError, TypeError):
                    continue
            if any(
                item.get("status") == "pending" and item.get("content") == content and item.get("due") == due
                for item in values
            ):
                continue
            item = {
                "id": "promise-" + uuid.uuid4().hex,
                "content": content,
                "created_at": now.isoformat(),
                "due": due,
                "type": kind,
                "status": "pending",
                "note": "",
            }
            values.append(item)
            changed.append(copy.deepcopy(item))
        for field, status in (("fulfilled_promise_ids", "fulfilled"), ("cancelled_promise_ids", "cancelled")):
            identifiers = payload.get(field, [])
            if not isinstance(identifiers, list):
                raise ValueError(field + " 必须是数组")
            selected = {str(value) for value in identifiers}
            for item in values:
                if item.get("id") in selected and item.get("status") in {"pending", "missed"}:
                    item["status"] = status
                    item["ended_at"] = now.isoformat()
                    changed.append(copy.deepcopy(item))
        if changed:
            self._save(values)
        return changed

    def for_injection(self, now, owner=False):
        values = self.refresh(now)
        if not owner:
            return []
        results = []
        for item in values:
            if item.get("status") == "missed" and not item.get("notified_at"):
                results.append(item)
            elif item.get("status") == "pending" and item.get("due"):
                try:
                    due = parse_local(item["due"], self.tz)
                except ValueError:
                    continue
                if now - dt.timedelta(hours=24) <= due <= now + dt.timedelta(hours=24):
                    results.append(item)
        return results

    def mark_notified(self, ids, now):
        selected = set(ids)
        values = self.all()
        changed = False
        for item in values:
            if item.get("id") in selected and item.get("status") == "missed" and not item.get("notified_at"):
                item["notified_at"] = parse_local(now, self.tz).isoformat()
                changed = True
        if changed:
            self._save(values)

    def cancel(self, query, now):
        values = self.refresh(now)
        item = _find_number_or_id(values, query)
        if item is None:
            raise ValueError("没有找到这个约定编号")
        item["status"] = "cancelled"
        item["ended_at"] = parse_local(now, self.tz).isoformat()
        item["note"] = "主人手动取消"
        self._save(values)
        return copy.deepcopy(item)

    def cleanup(self, now):
        values = self.refresh(now)
        kept = []
        for item in values:
            if item.get("status") == "pending":
                kept.append(item)
                continue
            try:
                ended = parse_local(item.get("ended_at") or item.get("due") or item.get("created_at"), self.tz)
            except (ValueError, TypeError):
                kept.append(item)
                continue
            if ended >= now - dt.timedelta(days=30):
                kept.append(item)
        if kept != values:
            self._save(kept)


def _find_number_or_id(values, query):
    query = str(query).strip()
    if query.isdigit():
        index = int(query) - 1
        return values[index] if 0 <= index < len(values) else None
    return next((item for item in values if item.get("id") == query), None)


class EventManager:
    """AI-created events only; user configuration contains no event list."""

    def __init__(self, store, config, tz):
        self.store, self.config, self.tz = store, config, _zone(tz)

    @staticmethod
    def _status(item, today):
        if item.get("status") in {"cancelled", "done"}:
            return item.get("status")
        if today > _date(item["end_date"]):
            return "done"
        return "upcoming" if today < _date(item["start_date"]) else "active"

    def _save(self, ai):
        self.store.write("events.json", ai)

    def all(self, today):
        today = _date(today)
        ai = self.store.read("events.json", [])
        before_ai = copy.deepcopy(ai)
        results = []
        seen = set()
        for item in ai:
            if not isinstance(item, dict):
                continue
            try:
                if _date(item["end_date"]) < _date(item["start_date"]):
                    raise ValueError("事件结束日期早于开始日期")
                if not str(item.get("title", "")).strip():
                    continue
                identifier = str(item.get("id", "") or "")
                if not identifier or identifier in seen:
                    identifier = "event-" + uuid.uuid4().hex
                item["id"] = identifier
                seen.add(identifier)
                item["source"] = "ai"
                item["status"] = self._status(item, today)
                results.append(copy.deepcopy(item))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("[生活日程] 长期事件数据无效，跳过：%s", describe_error(exc))
        if before_ai != ai:
            self._save(ai)
        return results

    def relevant(self, today):
        today = _date(today)
        return [
            item
            for item in self.all(today)
            if item["status"] == "active"
            or (item["status"] == "upcoming" and _date(item["start_date"]) <= today + dt.timedelta(days=3))
        ]

    def active(self, today):
        return [item for item in self.all(today) if item["status"] == "active"]

    def _validate(self, payload, today):
        if not isinstance(payload, dict):
            raise ValueError("事件必须是对象")
        title = str(payload.get("title", "") or "").strip()
        description = str(payload.get("description", "") or "").strip()
        if not title:
            raise ValueError("事件缺少标题")
        if contains_forbidden(
            {"title": title, "description": description},
            self.config.get("forbidden_interactions", []),
            user_aliases_from_config(self.config),
        ):
            raise ValueError("事件包含受限互动")
        start, end = _date(payload.get("start_date", today)), _date(payload.get("end_date", today))
        if end < start:
            raise ValueError("事件结束日期早于开始日期")
        if not 1 <= (end - start).days + 1 <= 7:
            raise ValueError("AI事件应持续1至7天")
        return {
            "id": "event-" + uuid.uuid4().hex,
            "title": title,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "description": description,
            "source": "ai",
            "status": "upcoming" if start > today else "done" if end < today else "active",
        }

    def add(self, payload, today):
        today = _date(today)
        item = self._validate(payload, today)
        self.all(today)
        ai = self.store.read("events.json", [])
        for existing in ai:
            if existing.get("status") in {"cancelled", "done"}:
                continue
            if _date(existing["start_date"]) <= _date(item["end_date"]) and _date(existing["end_date"]) >= _date(
                item["start_date"]
            ):
                raise ValueError("同一时间最多有一个AI长期事件")
        ai.append(item)
        self._save(ai)
        return copy.deepcopy(item)

    def apply_generation(self, payload, today, allow_new):
        today = _date(today)
        self.all(today)
        ai = self.store.read("events.json", [])
        changed = []
        updates = payload.get("event_updates", [])
        if not isinstance(updates, list):
            updates = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            item = next(
                (value for value in ai if value.get("id") == update.get("id") and value.get("status") == "active"), None
            )
            if item is None:
                continue
            candidate = copy.deepcopy(item)
            if update.get("status") == "done":
                candidate["status"] = "done"
                candidate["end_date"] = min(today, _date(item["end_date"])).isoformat()
            elif update.get("end_date"):
                try:
                    end = _date(update["end_date"])
                    if end < _date(item["start_date"]):
                        continue
                    if (end - _date(item["start_date"])).days + 1 > 7:
                        continue
                    candidate["end_date"] = end.isoformat()
                    candidate["status"] = self._status(candidate, today)
                except (ValueError, TypeError):
                    continue
            if update.get("description"):
                description = str(update["description"])
                if contains_forbidden(
                    description, self.config.get("forbidden_interactions", []), user_aliases_from_config(self.config)
                ):
                    continue
                candidate["description"] = description
            if candidate.get("status") in {"upcoming", "active"}:
                overlap = any(
                    other.get("id") != candidate["id"]
                    and other.get("status") not in {"done", "cancelled"}
                    and _date(other["start_date"]) <= _date(candidate["end_date"])
                    and _date(other["end_date"]) >= _date(candidate["start_date"])
                    for other in ai
                )
                if overlap:
                    continue
            if candidate != item:
                item.clear()
                item.update(candidate)
                changed.append(copy.deepcopy(item))
        if changed:
            self._save(ai)
        additions = payload.get("new_events", [])
        if allow_new and isinstance(additions, list):
            for addition in additions:
                try:
                    changed.append(self.add(addition, today))
                    break
                except (ValueError, TypeError) as exc:
                    logger.info("[生活日程] 忽略无效AI长期事件，继续保留其余事件：%s", describe_error(exc))
        return changed


def compute_routine(profile, today, tz, workday=True, jitter_minutes=20, yesterday=None, rng=None):
    """Resolve explicit wake/sleep clocks, then apply jitter and actual sleep debt."""
    tz, today, rng = _zone(tz), _date(today), rng or random
    text = str(profile.get("routine_workday" if workday else "routine_restday", "") or "")
    clock = r"(?P<hour>[01]?\d|2[0-3])(?:[:：](?P<minute>[0-5]\d)|点(?P<half>半)?(?P<cnminute>[0-5]?\d)?分?)"

    def extract(verbs, default):
        for match in re.finditer(clock, text):
            tail = text[match.end() : match.end() + 8]
            if re.search(verbs, tail):
                minute = 30 if match.group("half") else int(match.group("minute") or match.group("cnminute") or 0)
                return int(match.group("hour")) * 60 + minute
        for match in re.finditer(r"(?:" + verbs + r")[^\d，。；]{0,6}" + clock, text):
            minute = 30 if match.group("half") else int(match.group("minute") or match.group("cnminute") or 0)
            return int(match.group("hour")) * 60 + minute
        return default

    wake_minute = extract(r"起床|起|醒", 7 * 60 + 30 if workday else 9 * 60 + 30)
    sleep_minute = extract(r"入睡|睡觉|睡|就寝", 23 * 60 + 30)
    midnight = dt.datetime.combine(today, dt.time(), tzinfo=tz)
    wake_base = midnight + dt.timedelta(minutes=wake_minute)
    sleep_base = midnight + dt.timedelta(minutes=sleep_minute)
    if sleep_base <= wake_base:
        sleep_base += dt.timedelta(days=1)
    jitter = max(0, min(60, int(jitter_minutes)))
    wake = wake_base + dt.timedelta(minutes=rng.randint(-jitter, jitter))
    sleep = sleep_base + dt.timedelta(minutes=rng.randint(-jitter, jitter))
    delay_minutes = 0
    if isinstance(yesterday, dict):
        previous_date = today - dt.timedelta(days=1)
        expected_sleep = dt.datetime.combine(previous_date, dt.time(), tzinfo=tz) + dt.timedelta(minutes=sleep_minute)
        if sleep_minute <= wake_minute:
            expected_sleep += dt.timedelta(days=1)
        segments = yesterday.get("actual", [])
        candidates = []
        for item in segments:
            if project_slot_rest(item)["rest_type"] != "night_sleep":
                continue
            try:
                a, b = parse_local(item["start"], tz), parse_local(item["end"], tz)
            except (KeyError, ValueError):
                continue
            if b > expected_sleep and b - a >= dt.timedelta(hours=2):
                candidates.append(a)
        if candidates:
            actual_sleep = min(candidates, key=lambda value: abs((value - expected_sleep).total_seconds()))
            delay_minutes = max(0, int((actual_sleep - expected_sleep).total_seconds() / 60))
            wake += dt.timedelta(minutes=delay_minutes)
    return {"wake": wake.isoformat(), "sleep": sleep.isoformat(), "late_sleep_delay_minutes": delay_minutes}
