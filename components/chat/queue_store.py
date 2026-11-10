from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

QQ_SEEN_LIMIT = 256


@dataclass(frozen=True)
class Scope:
    session: str
    persona: str
    conversation: str = ""


@dataclass(frozen=True)
class QueuedMessage:
    message_id: str
    payload: str
    received_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Batch:
    scope: Scope
    messages: tuple[QueuedMessage, ...]
    revision: tuple[int, ...]


class QueueStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._prepare_schema()

    def _prepare_schema(self) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    conversation TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL,
                    payload TEXT,
                    received_at REAL NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'uncertain', 'confirmed', 'recalled')),
                    UNIQUE (session, message_id)
                )
            """)
            message_columns = self._db.execute("PRAGMA table_info(messages)")
            if not any(column[1] == "conversation" for column in message_columns):
                self._db.execute(
                    "ALTER TABLE messages ADD COLUMN conversation TEXT NOT NULL DEFAULT ''"
                )
                self._db.execute("DROP INDEX IF EXISTS pending_by_scope")
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS pending_by_scope ON messages
                    (session, persona, conversation, received_at, sequence) WHERE status = 'pending'
            """)
            attention_columns = list(
                self._db.execute("PRAGMA table_info(attention_episodes)")
            )
            legacy_attention = attention_columns and not any(
                column[1] == "conversation" for column in attention_columns
            )
            if legacy_attention:
                self._db.execute(
                    "ALTER TABLE attention_episodes RENAME TO legacy_attention_episodes"
                )
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS attention_episodes (
                    session TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    conversation TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    cutover INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session, persona, conversation)
                )
            """)
            if legacy_attention:
                self._db.execute("""
                    INSERT INTO attention_episodes (session, persona, note, cutover)
                    SELECT session, persona, note, cutover FROM legacy_attention_episodes
                """)
                self._db.execute("DROP TABLE legacy_attention_episodes")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS bot_bindings (
                    umo TEXT NOT NULL,
                    platform_type TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    PRIMARY KEY (umo, platform_type)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS qq_reply_wait (
                    session TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    conversation TEXT NOT NULL,
                    through_sequence INTEGER NOT NULL,
                    PRIMARY KEY (session, persona, conversation)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS qq_inbound_seen (
                    sequence INTEGER PRIMARY KEY,
                    session TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    UNIQUE (session, message_id)
                )
            """)

    def seen_qq_inbound(self, session: str, message_id: str) -> bool:
        return self._db.execute(
            """
            SELECT 1 FROM qq_inbound_seen WHERE session = ? AND message_id = ?
            UNION ALL
            SELECT 1 FROM messages WHERE session = ? AND message_id = ?
            LIMIT 1
            """,
            (session, message_id, session, message_id),
        ).fetchone() is not None

    def remember_qq_inbound(self, session: str, message_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO qq_inbound_seen (session, message_id) VALUES (?, ?)",
                (session, message_id),
            )
            self._db.execute(
                """
                DELETE FROM qq_inbound_seen
                WHERE session = ? AND sequence < (
                    SELECT sequence FROM qq_inbound_seen WHERE session = ?
                    ORDER BY sequence DESC LIMIT 1 OFFSET ?
                )
                """,
                (session, session, QQ_SEEN_LIMIT - 1),
            )

    def bound_bot(self, umo: str, platform_type: str) -> str:
        row = self._db.execute(
            "SELECT bot_id FROM bot_bindings WHERE umo = ? AND platform_type = ?",
            (umo, platform_type),
        ).fetchone()
        return row[0] if row else ""

    def bind_bot(self, umo: str, platform_type: str, bot_id: str) -> str:
        with self._db:
            self._db.execute(
                """
                INSERT INTO bot_bindings (umo, platform_type, bot_id) VALUES (?, ?, ?)
                ON CONFLICT (umo, platform_type) DO NOTHING
                """,
                (umo, platform_type, bot_id),
            )
            return self.bound_bot(umo, platform_type)

    def add(self, scope: Scope, message: QueuedMessage) -> bool:
        with self._db:
            result = self._db.execute(
                """
                INSERT INTO messages (session, persona, conversation, message_id, payload, received_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT (session, message_id) DO NOTHING
                """,
                (
                    scope.session,
                    scope.persona,
                    scope.conversation,
                    message.message_id,
                    message.payload,
                    message.received_at,
                ),
            )
            return result.rowcount == 1

    def recall(
        self, session: str, message_id: str, *, remember_missing: bool = False
    ) -> bool:
        with self._db:
            result = self._db.execute(
                """
                UPDATE messages SET payload = NULL, status = 'recalled'
                WHERE session = ? AND message_id = ? AND status != 'recalled'
                """,
                (session, message_id),
            )
            if result.rowcount:
                self._clear_idle_attention(session)
                return True
            if remember_missing:
                result = self._db.execute(
                    """
                    INSERT INTO messages (session, persona, message_id, received_at, status)
                    VALUES (?, '', ?, ?, 'recalled')
                    ON CONFLICT (session, message_id) DO NOTHING
                    """,
                    (session, message_id, time.time()),
                )
                return result.rowcount == 1
            return False

    def retained_payloads(self) -> tuple[str, ...]:
        return tuple(
            payload
            for (payload,) in self._db.execute(
                "SELECT payload FROM messages WHERE status IN ('pending', 'uncertain')"
            )
        )

    def payload_for_recall(self, session: str, message_id: str) -> str | None:
        row = self._db.execute(
            "SELECT payload FROM messages WHERE session = ? AND message_id = ?",
            (session, message_id),
        ).fetchone()
        return row[0] if row else None

    def uncertain_count(self, session: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE session = ? AND status = 'uncertain'",
            (session,),
        ).fetchone()[0]

    def recalled_ids(self, session: str) -> set[str]:
        return {
            message_id
            for (message_id,) in self._db.execute(
                "SELECT message_id FROM messages WHERE session = ? AND status = 'recalled'",
                (session,),
            )
        }

    def has_pending(self, scope: Scope) -> bool:
        return (
            self._db.execute(
                """
            SELECT 1 FROM messages
            WHERE session = ? AND persona = ? AND conversation = ? AND status = 'pending' LIMIT 1
            """,
                (scope.session, scope.persona, scope.conversation),
            ).fetchone()
            is not None
        )

    def has_pending_for_session(self, session: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM messages WHERE session = ? AND status = 'pending' LIMIT 1",
                (session,),
            ).fetchone()
            is not None
        )

    def pending_scopes(self) -> tuple[Scope, ...]:
        rows = self._db.execute("""
            SELECT session, persona, conversation FROM messages
            WHERE status = 'pending'
            GROUP BY session, persona, conversation ORDER BY MIN(received_at), MIN(sequence)
        """)
        return tuple(
            Scope(session, persona, conversation)
            for session, persona, conversation in rows
        )

    def claim(self, scope: Scope) -> Batch | None:
        rows = self._db.execute(
            """
            SELECT sequence, message_id, payload, received_at FROM messages
            WHERE session = ? AND persona = ? AND conversation = ? AND status = 'pending'
            ORDER BY received_at, sequence
            """,
            (scope.session, scope.persona, scope.conversation),
        ).fetchall()
        if not rows:
            return None
        return Batch(
            scope,
            tuple(
                QueuedMessage(message_id, payload, received_at)
                for _, message_id, payload, received_at in rows
            ),
            tuple(sequence for sequence, *_ in rows),
        )

    def begin_delivery(self, batch: Batch, *, allow_newer: bool = False) -> bool:
        scope = batch.scope
        with self._db:
            # Hold the write lock while checking and moving the batch out of the queue.
            self._db.execute("BEGIN IMMEDIATE")
            current = self._db.execute(
                """
                SELECT sequence FROM messages
                WHERE session = ? AND persona = ? AND conversation = ? AND status = 'pending'
                ORDER BY received_at, sequence
                """,
                (scope.session, scope.persona, scope.conversation),
            )
            revision = tuple(sequence for (sequence,) in current)
            if allow_newer:
                revision = revision[: len(batch.revision)]
            if revision != batch.revision:
                return False
            self._db.executemany(
                "UPDATE messages SET status = 'uncertain' WHERE sequence = ?",
                ((sequence,) for sequence in revision),
            )
            self._db.execute(
                """
                UPDATE attention_episodes SET cutover = 1
                WHERE session = ? AND persona = ? AND conversation = ?
                """,
                (scope.session, scope.persona, scope.conversation),
            )
            return True

    def waiting_for_qq_reply(self, batch: Batch) -> bool:
        scope = batch.scope
        return self._db.execute(
            """
            SELECT 1 FROM qq_reply_wait
            WHERE session = ? AND persona = ? AND conversation = ?
                AND through_sequence >= ?
            """,
            (scope.session, scope.persona, scope.conversation, max(batch.revision)),
        ).fetchone() is not None

    def wait_for_qq_reply(self, batch: Batch, *, restore: bool = False) -> None:
        scope = batch.scope
        with self._db:
            self._db.execute(
                """
                INSERT INTO qq_reply_wait VALUES (?, ?, ?, ?)
                ON CONFLICT (session, persona, conversation)
                DO UPDATE SET through_sequence = MAX(through_sequence, excluded.through_sequence)
                """,
                (scope.session, scope.persona, scope.conversation, max(batch.revision)),
            )
            if restore:
                self._restore_rejected_delivery(batch)

    def restore_rejected_delivery(self, batch: Batch) -> None:
        """Restore an original batch only after its first send was explicitly rejected."""
        with self._db:
            self._restore_rejected_delivery(batch)

    def _restore_rejected_delivery(self, batch: Batch) -> None:
        scope = batch.scope
        self._db.executemany(
            "UPDATE messages SET status = 'pending' WHERE sequence = ? AND status = 'uncertain'",
            ((sequence,) for sequence in batch.revision),
        )
        self._db.execute(
            """
            UPDATE attention_episodes SET cutover = 0
            WHERE session = ? AND persona = ? AND conversation = ?
            """,
            (scope.session, scope.persona, scope.conversation),
        )
        self._clear_idle_attention(scope.session)

    def finish_delivery(self, batch: Batch) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE messages SET payload = NULL, status = 'confirmed' WHERE sequence = ? AND status = 'uncertain'",
                ((sequence,) for sequence in batch.revision),
            )

    def try_attention(
        self,
        scope: Scope,
        since_timestamp: float,
        min_messages: int = 3,
    ) -> bool:
        with self._db:
            result = self._db.execute(
                """
                INSERT INTO attention_episodes (session, persona, conversation)
                SELECT ?, ?, ?
                WHERE (
                    SELECT COUNT(*) FROM messages
                    WHERE session = ? AND persona = ? AND conversation = ?
                      AND status = 'pending' AND received_at >= ?
                ) >= ?
                ON CONFLICT (session, persona, conversation) DO NOTHING
                """,
                (
                    scope.session,
                    scope.persona,
                    scope.conversation,
                    scope.session,
                    scope.persona,
                    scope.conversation,
                    since_timestamp,
                    min_messages,
                ),
            )
            return result.rowcount == 1

    def record_attention(self, scope: Scope, text: str) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE attention_episodes SET note = ?
                WHERE session = ? AND persona = ? AND conversation = ?
                """,
                (text, scope.session, scope.persona, scope.conversation),
            )

    def attention_text(self, scope: Scope) -> str:
        row = self._db.execute(
            """
            SELECT note FROM attention_episodes
            WHERE session = ? AND persona = ? AND conversation = ?
            """,
            (scope.session, scope.persona, scope.conversation),
        ).fetchone()
        return row[0] if row else ""

    def close_attention(self, scope: Scope) -> None:
        with self._db:
            self._db.execute(
                """
                DELETE FROM attention_episodes
                WHERE session = ? AND persona = ? AND conversation = ?
                """,
                (scope.session, scope.persona, scope.conversation),
            )

    def recover_interrupted_attention(self) -> None:
        """Close episodes at startup whose delivery cutover survived without settlement."""
        with self._db:
            self._db.execute("DELETE FROM attention_episodes WHERE cutover = 1")

    def _clear_idle_attention(self, session: str) -> None:
        self._db.execute(
            """
            DELETE FROM attention_episodes
            WHERE session = ? AND cutover = 0 AND NOT EXISTS (
                SELECT 1 FROM messages
                WHERE session = attention_episodes.session
                  AND persona = attention_episodes.persona
                  AND conversation = attention_episodes.conversation
                  AND status = 'pending'
            )
            """,
            (session,),
        )

    def close(self) -> None:
        self._db.close()
