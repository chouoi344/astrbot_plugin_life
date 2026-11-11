"""Silent, bounded autonomous publishing with durable media reuse."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .caption import valid_caption
from .dialogue import RequestProblem
from .events import SilentGenerationEvent
from .planning import daily_target, schedule_hash
from .request_policy import QzoneDeferred
from .transport import AccountUnavailableError


class _StopOpportunity(RuntimeError):
    """The opportunity changed or expired; keep a fixed, safe reason only."""


def _unresolved(service, binding, own_key: str = "") -> bool:
    for state in ("uncertain", "inflight"):
        for operation in service.store.list_operations(status=state, limit=100000):
            post = operation["payload"].get("post", {})
            bot = str(post.get("binding", {}).get("bot_id", ""))
            if (
                operation["key"] != own_key
                and operation["kind"] == "publish"
                and (not bot or bot == str(binding["bot_id"]))
            ):
                return True
    return False


def _quota_full(service, binding) -> bool:
    day = str(service.now().date())
    return service.store.count_autonomous(day, binding["bot_id"]) >= daily_target(
        day, binding["bot_id"], service.settings.daily_min, service.settings.daily_max
    )


def _too_close(service, binding) -> bool:
    previous = [
        post
        for post in service.store.list_posts(limit=200, include_deleted=True)
        if post["source"] == "autonomous" and str(post["binding"].get("bot_id")) == str(binding["bot_id"])
    ]
    return bool(
        previous and service.now().timestamp() - previous[0]["published_at"] < service.settings.min_gap_minutes * 60
    )


async def _check(service, event, binding, plan, *, verify_identity=True):
    if not service.settings.auto_publish or service.store.get_value("paused", False):
        raise _StopOpportunity("paused")
    if not service.plan_is_current(plan):
        raise _StopOpportunity("opportunity_expired")
    try:
        await service.verify_binding(event, binding, verify_identity=verify_identity)
        await service.integrations.check_life_persona(binding)
    except asyncio.CancelledError:
        raise
    except (QzoneDeferred, AccountUnavailableError):
        raise
    except Exception:
        raise _StopOpportunity("binding_changed") from None
    life = await service.current_life(binding, force=True)
    try:
        await service.require_runtime(event)
        service.authorize(event)
    except (RequestProblem, PermissionError):
        raise _StopOpportunity("binding_changed") from None
    if not service.settings.auto_publish or service.store.get_value("paused", False):
        raise _StopOpportunity("paused")
    if schedule_hash(life) != plan["schedule_hash"]:
        raise _StopOpportunity("schedule_changed")
    if not service.plan_matches_life(plan, life):
        raise _StopOpportunity("outside_schedule_activity")
    if _quota_full(service, binding):
        raise _StopOpportunity("daily_quota_reached")
    if _too_close(service, binding):
        raise _StopOpportunity("minimum_gap")
    if _unresolved(service, binding, "auto:" + plan["plan_id"]):
        raise _StopOpportunity("other_publish_unknown")


def _terminal(service, plan, state, reason):
    service.store.update_plan(plan["plan_id"], stage=state, last_error_kind=reason)
    service.store.finish_plan(plan["plan_id"], state)
    service.store.set_value("last_autonomous_result", "本次自主分享已静默跳过")


async def run_autonomous(service, event, binding, *, binding_verified=False):
    """Execute at most one due attempt; never send a chat message.

    The runtime owns its service lock. SQLite claims prevent two workers from
    taking the same opportunity, while cached stages survive process restarts.
    """
    if not service.settings.auto_publish or service.store.get_value("paused", False):
        return
    service.store.expire_previous_plans(str(service.now().date()))
    if not binding_verified:
        await service.verify_binding(event, binding)
    await service.integrations.check_life_persona(binding)
    if _unresolved(service, binding):
        service.store.set_value("schedule_status", "自主分享结果待核对，后续自主分享已暂停")
        return
    life = await service.ensure_plan(event, binding)
    if not life:
        return
    day = str(service.now().date())
    if _quota_full(service, binding):
        return
    delays = [
        service.settings.auto_retry_first_minutes * 60,
        service.settings.auto_retry_second_minutes * 60,
    ][: service.settings.auto_retry_count]
    for plan in service.store.list_plans(day):
        now = service.now().timestamp()
        if plan["status"] != "pending" or plan["slot_at"] > now:
            continue
        if not service.plan_is_current(plan):
            _terminal(service, plan, "skipped", "opportunity_expired")
            continue
        if plan["schedule_hash"] != schedule_hash(life):
            _terminal(service, plan, "cancelled", "schedule_changed")
            continue
        if plan.get("trigger") == "mood" and service.store.mood_opportunity(day, binding["bot_id"]) not in (
            None,
            plan["plan_id"],
        ):
            _terminal(service, plan, "skipped", "mood_already_triggered")
            continue
        # A newly selected skip policy also retires retries already waiting in
        # SQLite. Never claim one extra attempt using its old retry budget.
        if int(plan.get("attempt_count", 0)) > len(delays):
            _terminal(service, plan, "skipped", "retry_disabled" if not delays else "retry_exhausted")
            continue
        if float(plan.get("next_attempt_at") or plan["slot_at"]) > now or _too_close(service, binding):
            continue
        if not service.store.claim_plan(plan["plan_id"], now=now, retry_delays=delays):
            continue
        plan = service.store.get_plan(plan["plan_id"])
        key = "auto:" + plan["plan_id"]
        stage = "selfie"
        stopped = None

        async def final_check(*, verify_identity=True):
            nonlocal stopped
            try:
                await _check(service, event, binding, plan, verify_identity=verify_identity)
            except _StopOpportunity as exc:
                stopped = str(exc)
                raise
            return True

        try:
            await final_check()
            operation = service.store.get_operation(key)
            # Once an external write was attempted, its immutable payload is the
            # sole retry source. Never replace a missing original with a new image.
            if operation:
                if operation["status"] in ("uncertain", "inflight"):
                    service.store.finish_plan(plan["plan_id"], "uncertain")
                    break
                if operation["status"] == "done":
                    service.store.finish_plan(plan["plan_id"], "done", (operation.get("result") or {}).get("tid"))
                    break
                draft = operation["payload"]["post"]
                if draft["content"] != "" and not valid_caption(draft["content"]):
                    # A write attempt fixes the payload. Do not rewrite an
                    # unusable legacy draft and replay it as the same operation.
                    _terminal(service, plan, "skipped", "legacy_caption_format")
                    break
                original_images = draft.get("images") or []
                if len(original_images) != 1 or not Path(original_images[0]).is_file():
                    _terminal(service, plan, "skipped", "original_image_missing")
                    break
                plan = service.store.update_plan(
                    plan["plan_id"],
                    cached_image_path=str(original_images[0]),
                    cached_summary=str(draft.get("image_summary") or ""),
                    cached_caption=str(draft.get("content") or ""),
                    caption_decided=True,
                )
            path = plan.get("cached_image_path")
            if not path or not Path(path).is_file():
                if not plan.get("share_decided"):
                    stage = "sharing_intent"
                    context, decision = await service.prepare_share(
                        event,
                        binding,
                        plan["activity"],
                        "" if plan.get("activity_slot") else life.get("outfit", ""),
                        trigger=plan.get("trigger", "activity"),
                        mood=plan.get("mood", ""),
                    )
                    await final_check()
                    if not decision["share"]:
                        _terminal(service, plan, "skipped", "nothing_to_share")
                        break
                    plan = service.store.update_plan(
                        plan["plan_id"],
                        cached_sharing_context=context,
                        cached_share_focus=decision["focus"],
                        share_decided=True,
                        cached_photo_type=decision.get("photo_type", "selfie"),
                    )
                photo_type = plan.get("cached_photo_type", "selfie")
                stage = photo_type
                service.store.update_plan(
                    plan["plan_id"],
                    stage=stage,
                    cached_summary="",
                    cached_caption="",
                    caption_decided=False,
                )
                ended_activity = plan.get("activity_slot")
                if ended_activity:
                    scene = f"记录刚结束的活动：{plan['activity']}。用当时的场景。"
                elif plan.get("trigger") == "mood":
                    scene = f"当前心情：{plan.get('mood', '')}。拍下现在的状态，活动作为背景。"
                else:
                    scene = f"当前活动：{plan['activity']}。"
                subjects = {
                    "selfie": "拍一张你自己的单人日常自拍。保持已设置的角色参考形象，动作和构图自然。",
                    "food": "拍一张普通手机食物照，以这段活动里的食物为主。画面里没有人物。",
                    "scene": "拍一张普通手机场景照，以这段活动的地点、景色或环境为主。画面里没有人物。",
                }
                prompt = subjects[photo_type] + scene + "不混入今天其他时间的场景。"
                if photo_type == "selfie":
                    prompt += "拍法跟着角色自己的性格和审美走，不按性别套模板：像本人随手拍下的一张，不像棚拍，但角度、光线和神情是他愿意发出去被看见的样子，随手，但好看。"
                if photo_type == "selfie" and not ended_activity and life.get("outfit"):
                    prompt += f"当前穿搭：{life['outfit']}。"
                if plan.get("cached_share_focus"):
                    prompt += (
                        f"\n这次想记录的生活片刻：{plan['cached_share_focus']}。"
                        "不添加用户或其他人物，"
                        "不要把聊天内容写成画面中的文字，不添加日程外的地点、物品或经历。"
                    )
                silent_event = SilentGenerationEvent(event, final_check)
                if photo_type == "selfie" and not ended_activity:
                    generation = service.integrations.selfie(silent_event, prompt)
                else:
                    generation = service.integrations.photo(
                        silent_event,
                        prompt,
                        photo_type=photo_type,
                        activity=ended_activity,
                    )
                path, _ = await asyncio.wait_for(
                    generation,
                    service.settings.selfie_timeout,
                )
                if not Path(path).is_file():
                    raise ValueError("照片文件缺失")
                plan = service.store.update_plan(plan["plan_id"], cached_image_path=str(path))
            await final_check()
            images = [str(path)]
            stage = "vision"
            summary = plan.get("cached_summary")
            if not summary:
                service.store.update_plan(plan["plan_id"], stage=stage)
                summary = await service.photo_summary(event, images)
                if not isinstance(summary, str) or not summary.strip():
                    raise ValueError("识图内容为空")
                plan = service.store.update_plan(plan["plan_id"], cached_summary=summary)
            await final_check()
            stage = "caption"
            text = plan.get("cached_caption")
            if text and not valid_caption(text):
                plan = service.store.update_plan(plan["plan_id"], cached_caption="", caption_decided=False)
                text = ""
            if not text and not plan.get("caption_decided"):
                service.store.update_plan(plan["plan_id"], stage=stage)
                text = await service.generate_caption(
                    event,
                    summary,
                    activity=plan["activity"],
                    sharing_context=plan.get("cached_sharing_context"),
                    focus=plan.get("cached_share_focus", ""),
                    trigger=plan.get("trigger", "activity"),
                    mood=plan.get("mood", ""),
                    mood_intensity=plan.get("mood_intensity", ""),
                )
                if not isinstance(text, str) or (text and not valid_caption(text)):
                    raise ValueError("配文内容无效")
                plan = service.store.update_plan(plan["plan_id"], cached_caption=text, caption_decided=True)
            await final_check()
            stage = "publish"
            service.store.update_plan(plan["plan_id"], stage=stage)
            await service._publish(event, binding, text, images, summary, "autonomous", key, final_check=final_check)
            operation = service.store.get_operation(key)
            if operation and operation["status"] == "done":
                service.store.finish_plan(plan["plan_id"], "done", (operation.get("result") or {}).get("tid"))
                service.store.set_value("last_autonomous_result", "自主分享已完成")
                service.store.set_value("last_autonomous_error", "")
            elif operation and operation["status"] in ("inflight", "uncertain"):
                service.store.finish_plan(plan["plan_id"], "uncertain")
                service.store.set_value("last_autonomous_result", "自主分享结果待核对")
            elif stopped:
                _terminal(service, plan, "cancelled", stopped)
            else:
                service.store.schedule_plan_retry(plan["plan_id"], "publish_rejected", now=service.now().timestamp())
        except _StopOpportunity as exc:
            _terminal(service, plan, "cancelled", str(exc))
        except (QzoneDeferred, AccountUnavailableError) as exc:
            now = service.now().timestamp()
            retry_at = max(now + 60, exc.retry_at if isinstance(exc, QzoneDeferred) else 0)
            service.store.defer_plan(plan["plan_id"], retry_at=retry_at, now=now)
            service.store.set_value("last_autonomous_result", "本次自主分享等待空间访问恢复")
            raise
        except asyncio.CancelledError:
            operation = service.store.get_operation(key)
            if operation and operation["status"] in ("inflight", "uncertain"):
                if operation["status"] == "inflight":
                    service.store.mark_operation_uncertain(key, "发布任务中断，等待核对")
                service.store.finish_plan(plan["plan_id"], "uncertain")
            else:
                service.store.schedule_plan_retry(
                    plan["plan_id"], "interrupted_before_publish", now=service.now().timestamp()
                )
            raise
        except Exception:
            operation = service.store.get_operation(key)
            if operation and operation["status"] in ("inflight", "uncertain"):
                if operation["status"] == "inflight":
                    service.store.mark_operation_uncertain(key, "发布结果未取得，等待核对")
                service.store.finish_plan(plan["plan_id"], "uncertain")
            elif operation and operation["status"] == "done":
                service.store.finish_plan(plan["plan_id"], "done", (operation.get("result") or {}).get("tid"))
            else:
                if not service.plan_is_current(plan):
                    _terminal(service, plan, "cancelled", "opportunity_expired")
                else:
                    service.store.schedule_plan_retry(plan["plan_id"], stage + "_failed", now=service.now().timestamp())
                service.store.set_value("last_autonomous_error", stage + "_failed")
        break
