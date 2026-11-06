"""Durable message broker.

`Broker` is the contract; `SqliteBroker` is the default implementation (Docker
is not available, so no Redpanda; see docs/DECISIONS.md D-1).

Semantics: at-least-once delivery. A claimed message is leased until
`visible_at_ms`; if it is not acked in time it becomes claimable again (crash
recovery). `attempts` counts claims, so a message that keeps crashing its
consumer still reaches the DLQ. Each lease is identified by its attempt
number; ack/nack/dead_letter with a stale lease are ignored, so a slow worker
cannot clobber a message another worker has since re-claimed.

No per-key ordering guarantee: consumers must not depend on order.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from driveguard.clock import Clock
from driveguard.store.db import Database, transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_messages (
    msg_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic          TEXT    NOT NULL,
    key            TEXT,
    body           TEXT    NOT NULL,
    state          TEXT    NOT NULL CHECK (state IN ('ready', 'inflight', 'done', 'dead')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    visible_at_ms  INTEGER NOT NULL,
    enqueued_at_ms INTEGER NOT NULL,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_claim ON queue_messages (topic, state, visible_at_ms, msg_id);
CREATE TABLE IF NOT EXISTS dead_letters (
    msg_id         INTEGER PRIMARY KEY,
    topic          TEXT    NOT NULL,
    key            TEXT,
    body           TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    last_error     TEXT,
    attempts       INTEGER NOT NULL,
    dead_at_ms     INTEGER NOT NULL,
    replayed_at_ms INTEGER
);
"""


@dataclass(frozen=True)
class Message:
    msg_id: int
    topic: str
    key: str | None
    body: str
    attempts: int  # includes the current delivery; also the lease id
    enqueued_at_ms: int = 0  # publish time, for end-to-end latency metrics


@dataclass(frozen=True)
class DeadLetter:
    msg_id: int
    topic: str
    key: str | None
    body: str
    reason: str
    last_error: str | None
    attempts: int
    dead_at_ms: int
    replayed_at_ms: int | None


class Broker(Protocol):
    async def publish_many(self, topic: str, items: list[tuple[str | None, str]]) -> None: ...
    async def claim(self, topic: str, limit: int, visibility_timeout_s: float) -> list[Message]: ...
    async def ack(self, msg: Message) -> bool: ...
    async def nack(self, msg: Message, delay_s: float, error: str) -> bool: ...
    async def dead_letter(self, msg: Message, reason: str, error: str) -> bool: ...
    async def next_visible_at_ms(self, topic: str) -> int | None: ...
    async def list_dead(self, topic: str | None = None, include_replayed: bool = False) -> list[DeadLetter]: ...
    async def replay_dead(self, msg_ids: list[int] | None = None, reason: str | None = None) -> int: ...
    async def counts(self, topic: str) -> dict[str, int]: ...


class SqliteBroker:
    def __init__(self, path: str | Path, clock: Clock) -> None:
        self._db = Database(path)
        self._clock = clock

    async def connect(self) -> "SqliteBroker":
        await self._db.connect(_SCHEMA)
        return self

    async def close(self) -> None:
        await self._db.close()

    async def publish(self, topic: str, key: str | None, body: str) -> None:
        await self.publish_many(topic, [(key, body)])

    async def publish_many(self, topic: str, items: list[tuple[str | None, str]]) -> None:
        now = self._clock.now_ms()

        def _do(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO queue_messages (topic, key, body, state, visible_at_ms, enqueued_at_ms) "
                    "VALUES (?, ?, ?, 'ready', ?, ?)",
                    [(topic, k, b, now, now) for k, b in items],
                )

        await self._db.run(_do)

    async def claim(self, topic: str, limit: int, visibility_timeout_s: float) -> list[Message]:
        now = self._clock.now_ms()
        lease_until = now + int(visibility_timeout_s * 1000)

        def _do(conn: sqlite3.Connection) -> list[Message]:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT msg_id, topic, key, body, attempts, enqueued_at_ms FROM queue_messages "
                    "WHERE topic = ? AND state IN ('ready', 'inflight') AND visible_at_ms <= ? "
                    "ORDER BY msg_id LIMIT ?",
                    (topic, now, limit),
                ).fetchall()
                conn.executemany(
                    "UPDATE queue_messages SET state = 'inflight', attempts = attempts + 1, visible_at_ms = ? "
                    "WHERE msg_id = ?",
                    [(lease_until, r["msg_id"]) for r in rows],
                )
            return [Message(r["msg_id"], r["topic"], r["key"], r["body"], r["attempts"] + 1, r["enqueued_at_ms"])
                    for r in rows]

        return await self._db.run(_do)

    async def ack(self, msg: Message) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE queue_messages SET state = 'done' WHERE msg_id = ? AND state = 'inflight' AND attempts = ?",
                (msg.msg_id, msg.attempts),
            )
            return cur.rowcount == 1

        return await self._db.run(_do)

    async def nack(self, msg: Message, delay_s: float, error: str) -> bool:
        visible = self._clock.now_ms() + int(delay_s * 1000)

        def _do(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE queue_messages SET state = 'ready', visible_at_ms = ?, last_error = ? "
                "WHERE msg_id = ? AND state = 'inflight' AND attempts = ?",
                (visible, error[:1000], msg.msg_id, msg.attempts),
            )
            return cur.rowcount == 1

        return await self._db.run(_do)

    async def dead_letter(self, msg: Message, reason: str, error: str) -> bool:
        now = self._clock.now_ms()

        def _do(conn: sqlite3.Connection) -> bool:
            with transaction(conn):
                cur = conn.execute(
                    "UPDATE queue_messages SET state = 'dead', last_error = ? "
                    "WHERE msg_id = ? AND state = 'inflight' AND attempts = ?",
                    (error[:1000], msg.msg_id, msg.attempts),
                )
                if cur.rowcount != 1:
                    return False
                conn.execute(
                    "INSERT INTO dead_letters (msg_id, topic, key, body, reason, last_error, attempts, dead_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (msg.msg_id, msg.topic, msg.key, msg.body, reason, error[:1000], msg.attempts, now),
                )
            return True

        return await self._db.run(_do)

    async def next_visible_at_ms(self, topic: str) -> int | None:
        def _do(conn: sqlite3.Connection) -> int | None:
            row = conn.execute(
                "SELECT MIN(visible_at_ms) AS v FROM queue_messages WHERE topic = ? AND state IN ('ready', 'inflight')",
                (topic,),
            ).fetchone()
            return row["v"]

        return await self._db.run(_do)

    async def list_dead(self, topic: str | None = None, include_replayed: bool = False) -> list[DeadLetter]:
        def _do(conn: sqlite3.Connection) -> list[DeadLetter]:
            sql = "SELECT * FROM dead_letters WHERE 1=1"
            args: list[object] = []
            if topic:
                sql += " AND topic = ?"
                args.append(topic)
            if not include_replayed:
                sql += " AND replayed_at_ms IS NULL"
            rows = conn.execute(sql + " ORDER BY msg_id", args).fetchall()
            return [DeadLetter(**dict(r)) for r in rows]

        return await self._db.run(_do)

    async def replay_dead(self, msg_ids: list[int] | None = None, reason: str | None = None) -> int:
        """Re-enqueue dead letters as fresh messages (attempts reset). Returns count."""
        now = self._clock.now_ms()

        def _do(conn: sqlite3.Connection) -> int:
            with transaction(conn):
                sql = "SELECT * FROM dead_letters WHERE replayed_at_ms IS NULL"
                args: list[object] = []
                if reason:
                    sql += " AND reason = ?"
                    args.append(reason)
                rows = conn.execute(sql, args).fetchall()
                if msg_ids is not None:
                    wanted = set(msg_ids)
                    rows = [r for r in rows if r["msg_id"] in wanted]
                for r in rows:
                    conn.execute(
                        "INSERT INTO queue_messages (topic, key, body, state, visible_at_ms, enqueued_at_ms) "
                        "VALUES (?, ?, ?, 'ready', ?, ?)",
                        (r["topic"], r["key"], r["body"], now, now),
                    )
                    conn.execute("UPDATE dead_letters SET replayed_at_ms = ? WHERE msg_id = ?", (now, r["msg_id"]))
                return len(rows)

        return await self._db.run(_do)

    async def counts(self, topic: str) -> dict[str, int]:
        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM queue_messages WHERE topic = ? GROUP BY state", (topic,)
            ).fetchall()
            out = {"ready": 0, "inflight": 0, "done": 0, "dead": 0}
            out.update({r["state"]: r["n"] for r in rows})
            return out

        return await self._db.run(_do)
