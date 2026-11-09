"""Local-only QQ Space activity for the currently bound character."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot.core import sp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .components.qzone.core.auto_setup import connected_qq
from .components.qzone.core.dialogue import RequestProblem
from .components.qzone.core.integrations import IntegrationError, Integrations
from .components.qzone.core.settings import Settings, admin_ids
from .runtime import QZONE_ID


def _read_database(path, reader):
    if not path.is_file():
        return None
    # A stopped database without a WAL can be read without creating SQLite
    # sidecar files. An active WAL is read only when both sidecars already exist.
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and not path.with_name(path.name + "-shm").exists():
        return None
    uri = path.resolve().as_uri() + ("?mode=ro" if wal.exists() else "?mode=ro&immutable=1")
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db, db:
        db.execute("BEGIN")
        return reader(db)


def _value(db, key, default=None):
    row = db.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _snapshot(db, binding, zone):
    if _value(db, "binding") != binding:
        return None
    now = datetime.now(zone)
    today = now.date()
    start = datetime.combine(today, time.min, zone).timestamp()
    end = datetime.combine(today + timedelta(days=1), time.min, zone).timestamp()
    where = " AND ".join(
        f"CAST(json_extract(record_json, '$.binding.{field}') AS TEXT)=?"
        for field in ("bot_id", "persona_id", "umo")
    )
    owner = tuple(str(binding[field]) for field in ("bot_id", "persona_id", "umo"))
    posts = [
        json.loads(row[0]) for row in db.execute(
            f"SELECT record_json FROM posts WHERE {where} AND source IN ('manual','autonomous') "
            "ORDER BY published_at DESC,tid LIMIT 8",
            owner,
        )
    ]
    published_today = db.execute(
        f"SELECT COUNT(*) FROM posts WHERE {where} AND source IN ('manual','autonomous') "
        "AND published_at>=? AND published_at<?",
        (*owner, start, end),
    ).fetchone()[0]
    autonomous_today = db.execute(
        "SELECT COUNT(*) FROM posts WHERE source='autonomous' AND local_day=? "
        "AND CAST(json_extract(record_json, '$.binding.bot_id') AS TEXT)=?",
        (today.isoformat(), owner[0]),
    ).fetchone()[0]
    comment_states = dict(db.execute(
        f"SELECT status,COUNT(*) FROM comments WHERE {where} "
        "AND tid IN (SELECT tid FROM posts WHERE status='published' AND published_at>=? AND published_at<?) "
        "GROUP BY status",
        (*owner, start, end),
    ))
    replies_today = db.execute(
        f"SELECT COUNT(*) FROM comments WHERE {where} AND status='replied' "
        "AND CAST(json_extract(record_json, '$.reply.created_at') AS REAL)>=? "
        "AND CAST(json_extract(record_json, '$.reply.created_at') AS REAL)<?",
        (*owner, start, end),
    ).fetchone()[0]

    return {
        "persona_id": binding["persona_id"],
        "published_today": published_today,
        "autonomous_today": autonomous_today,
        "paused": bool(_value(db, "paused", False)),
        "reply_pending": sum(comment_states.get(state, 0) for state in ("pending", "processing", "failed")),
        "reply_unknown": comment_states.get("uncertain", 0),
        "replies_today": replies_today,
        "posts": [
            {
                "caption": post.get("content", ""),
                "created_at": datetime.fromtimestamp(post["published_at"], zone).isoformat(),
                "status": (
                    "remote_deleted"
                    if post["status"] == "published" and _value(
                        db, f"comment_poll:{owner[0]}:{post['tid']}", {}
                    ).get("remote_deleted_observed_at") is not None
                    else post["status"]
                ),
                "source": post["source"],
            }
            for post in posts
        ],
    }


async def qzone_status(plugin, component):
    config = plugin.config["qzone"]
    settings = Settings.from_config(config, plugin.context.get_config())
    enabled = config["enabled"]
    result = {
        "status": "unbound" if enabled else "disabled",
        "persona_id": None,
        "posts": [],
        "published_today": None,
        "autonomous_today": None,
        "daily_limit": settings.daily_max,
        "replies_enabled": settings.auto_reply,
        "reply_pending": None,
        "reply_unknown": None,
        "replies_today": None,
        "paused": None,
    }
    if component is None:
        return {**result, "status": "unavailable", "message": "空间组件暂未就绪。"}
    service = component.service
    path = (
        service.data_dir if service
        else Path(get_astrbot_data_path()) / "plugin_data" / QZONE_ID
    ) / "state.sqlite3"
    binding = await asyncio.to_thread(_read_database, path, lambda db: _value(db, "binding"))
    if (
        not binding
        or not binding.get("bot_id")
        or not binding.get("persona_id")
        or not binding.get("umo")
        or str(binding.get("admin_id")) not in admin_ids(plugin.context)
    ):
        return {**result, "message": "还没有可核对的本地空间绑定。"}

    integrations = service.integrations if service else Integrations(plugin.context)
    platform = plugin.context.get_platform_inst(binding["umo"].split(":", 1)[0])
    if platform and platform.meta().name == "aiocqhttp":
        try:
            account = connected_qq(platform.get_client())
        except RequestProblem:
            return {**result, "message": "当前 QQ 连接身份暂时无法核对。"}
        if account and account != str(binding["bot_id"]):
            return {**result, "message": "当前 QQ 账号与本地绑定不同，等待重新核对。"}
    event = SimpleNamespace(unified_msg_origin=binding["umo"])
    try:
        persona_id = await integrations._memory_persona_id(event, sp)
    except IntegrationError:
        return {**result, "message": "当前空间会话的人设暂时无法核对。"}
    if persona_id != binding["persona_id"]:
        return {**result, "message": "当前会话人设已改变，等待空间更新本地绑定。"}
    snapshot = await asyncio.to_thread(
        _read_database, path,
        lambda db: _snapshot(db, binding, ZoneInfo(settings.timezone)),
    )
    if not snapshot or await integrations._memory_persona_id(event, sp) != persona_id:
        return {**result, "message": "空间本地记录正在更新，请稍后刷新。"}
    return {
        **result,
        **snapshot,
        "status": "ready" if enabled else "disabled",
        "message": "这里只显示本地已记录的空间活动。",
    }

