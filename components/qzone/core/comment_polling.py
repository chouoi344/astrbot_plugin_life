"""Check today's own posts during at most two rounds per free schedule slot."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import timedelta

from .dialogue import RequestProblem
from .request_policy import QzoneDeferred
from .settings import admin_ids
from .transport import AccountIdentityError, QzonePostDeletedError, QzoneReadCancelled


READ_BUDGET_SECONDS = 60
REPLIES_PER_ROUND = 20


def _timestamp(value, fallback=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) and number >= 0 else fallback


def _post_key(post):
    return f"comment_poll:{post['binding']['bot_id']}:{post['tid']}"


def post_reported_deleted(store, post):
    """Keep explicit upstream deletion observations from earlier versions."""
    return store.get_value(_post_key(post), {}).get("remote_deleted_observed_at") is not None


def _round_times(account, start, end):
    digest = hashlib.sha256(f"{account}:{start}".encode()).digest()
    length = end - start
    return (
        start + length * (0.25 + 0.15 * digest[0] / 255),
        start + length * (0.65 + 0.20 * digest[1] / 255),
    )


def _claim_round(store, account, slot, now):
    start, end = (point.timestamp() for point in slot)
    early, late = _round_times(account, start, end)
    timestamp = now.timestamp()
    if not early <= timestamp < end:
        return False
    round_number = 2 if timestamp >= late else 1
    key = f"comment_rounds:v1:{account}"
    rounds = store.get_value(key, {})
    slot_key = str(start)
    previous = rounds.get(slot_key)
    if previous and (
        round_number <= previous[0] or timestamp < previous[1] + min(300, (end - start) * 0.2)
    ):
        return False
    rounds = {key: value for key, value in rounds.items() if float(key) >= timestamp - 172800}
    rounds[slot_key] = [round_number, timestamp]
    store.set_value(key, rounds)
    return True


def _observe(service, post, detail, binding, day_start, day_end, now):
    store = service.store
    baseline_key = "comments_seen:" + post["tid"]
    seen = set(store.get_value(baseline_key, []))
    state = store.get_value(_post_key(post), {})
    first_detail = not state.get("detail_initialized") and not seen
    activated = _timestamp(store.get_value("activated_at", now), now)
    admins = admin_ids(service.context)
    observed = []
    for comment in detail.get("comments", []):
        if not isinstance(comment, dict):
            continue
        uid, cid = str(comment.get("uin", "")), str(comment.get("id", ""))
        if not uid or uid == "0" or not cid or cid == "0":
            continue
        key = f"{post['tid']}:{comment.get('parent_id') or '-'}:{cid}:{uid}"
        if uid == str(binding["bot_id"]) or uid not in admins:
            seen.add(key)
            continue
        created_at = _timestamp(comment.get("created_at"))
        if (created_at and (created_at < activated or not day_start <= created_at < day_end)
                or (first_detail and post["source"] == "external" and not created_at)):
            seen.add(key)
            continue
        if key not in seen:
            store.record_comment({
                "key": key,
                "tid": post["tid"],
                "comment_id": cid,
                "parent_id": comment.get("parent_id"),
                "author_qq": uid,
                "author_name": str(comment.get("nickname", uid)),
                "content": str(comment.get("content", "")),
                "created_at": created_at or now,
                "binding": post["binding"],
                "raw": comment,
            })
            seen.add(key)
        existing = store.get_comment(key)
        if (existing and existing["status"] in ("pending", "failed")
                and day_start <= existing["created_at"] < day_end):
            observed.append(existing)
    store.set_value(baseline_key, sorted(seen))
    store.set_value(_post_key(post), {**state, "detail_initialized": True, "last_detail_at": now})
    return observed


async def _read_before_deadline(awaitable, deadline):
    return await asyncio.wait_for(awaitable, timeout=max(0.001, deadline - time.monotonic()))


async def poll_comments(service, event, binding):
    store = service.store
    if not service.settings.auto_reply or store.get_value("paused", False):
        return
    now = service.now()
    slot = await service.integrations.comment_slot(binding, now)
    if slot is None or not _claim_round(store, binding["bot_id"], slot, now):
        return

    day = now.date()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start, day_end = midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()
    post_before = min(day_end, slot[0].timestamp())

    def eligible(timestamp):
        return day_start <= _timestamp(timestamp) < post_before

    async def check_window():
        check_at = service.now()
        try:
            await service.require_runtime(event)
            service.authorize(event)
        except (PermissionError, RequestProblem):
            raise QzoneReadCancelled() from None
        if (check_at.date() != day
                or await service.integrations.comment_slot(binding, check_at) != slot
                or service.now().date() != day or service.now().timestamp() >= slot[1].timestamp()
                or not service.settings.auto_reply or store.get_value("paused", False)):
            raise QzoneReadCancelled()

    deadline = time.monotonic() + READ_BUDGET_SECONDS
    try:
        await check_window()
        await _read_before_deadline(service.refresh_posts(
            event, binding, eligible=eligible, before_read=check_window,
        ), deadline)
        await check_window()
    except QzoneReadCancelled:
        return
    except (AccountIdentityError, QzoneDeferred):
        raise
    except Exception:
        store.set_value("last_comment_error", "空间列表暂时无法读取，等下一轮空闲再检查")
        return

    owners = {binding["umo"]: binding}
    posts = store.list_posts(
        limit=service.settings.watch_posts,
        bot_id=binding["bot_id"], persona_id=binding["persona_id"],
        published_from=day_start, published_before=post_before,
    )
    posts.sort(key=lambda post: store.get_value(_post_key(post), {}).get("last_detail_at", 0))
    candidates = {}
    error = ""
    for post in posts:
        if time.monotonic() >= deadline:
            break
        if post_reported_deleted(store, post) or not await service.comment_owner_is_current(
            post["binding"], binding, owners
        ):
            continue
        try:
            await check_window()
            transport = await service.transport(event)
            detail = await _read_before_deadline(
                transport.get_post(post["tid"], before_read=check_window), deadline,
            )
            await check_window()
        except QzoneReadCancelled:
            return
        except QzonePostDeletedError:
            key = _post_key(post)
            state = store.get_value(key, {})
            store.set_value(key, {**state, "remote_deleted_observed_at": service.now().timestamp()})
            continue
        except (AccountIdentityError, QzoneDeferred):
            raise
        except asyncio.TimeoutError:
            error = "本轮评论读取已到时间上限，等下一轮空闲再检查"
            break
        except Exception:
            error = "部分说说暂时无法读取，等下一轮空闲再检查"
            continue
        for comment in _observe(service, post, detail, binding, day_start, day_end, service.now().timestamp()):
            candidates[comment["key"]] = (post, comment)

    ordered = sorted(candidates.values(), key=lambda item: (item[1]["created_at"], item[1]["key"]))
    attempted = 0
    for post, comment in ordered:
        if attempted >= REPLIES_PER_ROUND:
            break
        key = comment["key"]
        if time.time() < _timestamp(store.get_value("reply_retry:" + key, 0)):
            continue
        try:
            await check_window()
            attempted += 1
            await service.reply_to_comment(post, comment, check_window=check_window)
        except QzoneReadCancelled:
            return
        except (AccountIdentityError, QzoneDeferred):
            raise
        except Exception:
            retry_at = max(time.time() + 180, _timestamp(store.get_value("reply_retry:" + key, 0)))
            store.set_value("reply_retry:" + key, retry_at)
            error = "部分评论暂时未能回复，已保留处理记录"
        else:
            if store.get_comment(key)["status"] in ("failed", "failed_exhausted", "uncertain"):
                error = "部分评论暂时未能回复，已保留处理记录"
    store.set_value("last_comment_error", error)
