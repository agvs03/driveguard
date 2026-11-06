"""Event store: idempotent event application, trip completion and detection.

The core rule: every effect of an event (raw row, trip row update, incident
re-detection) happens in ONE transaction, guarded by the `event_id` primary
key. A redelivered event therefore either finds its row (no-op) or finds
nothing at all, never a half-applied state.

Trip lifecycle:
  open       - events arriving; trip_end not seen or seqs missing
  complete   - trip_end seen and every seq 0..end_seq present -> detection run
  incomplete - swept after a quiet period with gaps -> detection run on what
               exists; any later (late) event for the trip re-runs detection
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from driveguard.events import Event
from driveguard.pipeline.detector import DETECTOR_VERSION, detect, stored_event_from_row
from driveguard.store.db import Database, transaction
from driveguard.store.migrations import migrate


class Outcome(str, Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"  # same event_id, same content: harmless redelivery
    CONFLICT = "conflict"  # same event_id (or trip/seq) with different content


@dataclass(frozen=True)
class ApplyResult:
    outcome: Outcome
    late: bool = False
    detection_ran: bool = False
    reason: str = ""


def _run_detection(conn: sqlite3.Connection, trip_id: str, now_ms: int) -> None:
    """Re-detect a trip. Incidents that disappear are *superseded*, never deleted,
    because cases may reference them (Phase 1 critique). An incident that comes
    back has the same uuid5 id and is un-superseded."""
    rows = conn.execute(
        "SELECT event_id, seq, event_time_ms, type, payload_json FROM raw_events WHERE trip_id = ? ORDER BY seq",
        (trip_id,),
    ).fetchall()
    incidents = detect(trip_id, [stored_event_from_row(r) for r in rows])
    keep = [i.incident_id for i in incidents]
    conn.execute(
        f"UPDATE detected_incidents SET superseded_at_ms = ? WHERE trip_id = ? AND detector_version = ? "
        f"AND superseded_at_ms IS NULL AND incident_id NOT IN ({','.join('?' * len(keep))})",
        (now_ms, trip_id, DETECTOR_VERSION, *keep),
    )
    conn.executemany(
        "INSERT INTO detected_incidents (incident_id, trip_id, kind, t_start_ms, t_end_ms, detector_version, "
        "trigger_event_ids_json, details_json, superseded_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT (incident_id) DO UPDATE SET t_end_ms = excluded.t_end_ms, "
        "trigger_event_ids_json = excluded.trigger_event_ids_json, details_json = excluded.details_json, "
        "superseded_at_ms = NULL",
        [
            (i.incident_id, i.trip_id, i.kind, i.t_start_ms, i.t_end_ms, i.detector_version,
             json.dumps(i.trigger_event_ids), json.dumps(i.details, sort_keys=True))
            for i in incidents
        ],
    )
    conn.execute("UPDATE trips SET detection_runs = detection_runs + 1 WHERE trip_id = ?", (trip_id,))


def _apply(conn: sqlite3.Connection, ev: Event, now_ms: int) -> ApplyResult:
    eid, tid = str(ev.event_id), str(ev.trip_id)
    chash = ev.content_hash()
    with transaction(conn):
        existing = conn.execute("SELECT content_hash FROM raw_events WHERE event_id = ?", (eid,)).fetchone()
        if existing:
            if existing["content_hash"] == chash:
                return ApplyResult(Outcome.DUPLICATE)
            return ApplyResult(Outcome.CONFLICT, reason="event_id reused with different content")
        clash = conn.execute(
            "SELECT event_id FROM raw_events WHERE trip_id = ? AND seq = ?", (tid, ev.seq)
        ).fetchone()
        if clash:
            return ApplyResult(Outcome.CONFLICT, reason=f"seq {ev.seq} already used by another event")

        conn.execute(
            "INSERT INTO trips (trip_id, max_event_time_ms, last_ingest_ms, status) VALUES (?, ?, ?, 'open') "
            "ON CONFLICT (trip_id) DO NOTHING",
            (tid, ev.event_time_ms, now_ms),
        )
        trip = conn.execute("SELECT * FROM trips WHERE trip_id = ?", (tid,)).fetchone()
        end_seq = trip["end_seq"]
        if end_seq is not None and ev.seq > end_seq:
            return ApplyResult(Outcome.CONFLICT, reason=f"seq {ev.seq} beyond trip_end seq {end_seq}")
        if ev.type == "trip_end":
            beyond = conn.execute(
                "SELECT 1 FROM raw_events WHERE trip_id = ? AND seq > ? LIMIT 1", (tid, ev.seq)
            ).fetchone()
            if beyond or (end_seq is not None and end_seq != ev.seq):
                return ApplyResult(Outcome.CONFLICT, reason="conflicting trip_end")

        # "Late" = arrived after the trip was already finalized by the sweeper.
        # (A max-event-time watermark was tried first and was wrong: one early
        # out-of-order event made everything behind it "late". See DECISIONS M-1.)
        late = trip["status"] == "incomplete"
        conn.execute(
            "INSERT INTO raw_events (event_id, trip_id, seq, event_time_ms, ingest_time_ms, type, payload_json, "
            "content_hash, late, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, tid, ev.seq, ev.event_time_ms, now_ms, ev.type,
             json.dumps(ev.payload, sort_keys=True), chash, int(late), ev.schema_version),
        )

        # Every update below is commutative, so arrival order cannot change the result.
        conn.execute(
            "UPDATE trips SET max_event_time_ms = MAX(max_event_time_ms, ?), last_ingest_ms = ?, "
            "started_at_ms = MIN(COALESCE(started_at_ms, ?), ?) WHERE trip_id = ?",
            (ev.event_time_ms, now_ms, ev.event_time_ms, ev.event_time_ms, tid),
        )
        if ev.type == "trip_start":
            conn.execute("UPDATE trips SET driver_id = ?, vehicle_id = ? WHERE trip_id = ?",
                         (ev.payload["driver_id"], ev.payload["vehicle_id"], tid))
        elif ev.type == "trip_end":
            conn.execute("UPDATE trips SET end_seq = ?, ended_at_ms = ?, distance_km = ? WHERE trip_id = ?",
                         (ev.seq, ev.event_time_ms, ev.payload["distance_km"], tid))
            end_seq = ev.seq

        detection_ran = False
        status = trip["status"]
        if end_seq is not None:
            n = conn.execute("SELECT COUNT(*) AS n FROM raw_events WHERE trip_id = ? AND seq <= ?",
                             (tid, end_seq)).fetchone()["n"]
            if n == end_seq + 1:
                _run_detection(conn, tid, now_ms)
                conn.execute("UPDATE trips SET status = 'complete' WHERE trip_id = ?", (tid,))
                detection_ran = True
        if not detection_ran and status == "incomplete":
            _run_detection(conn, tid, now_ms)  # late event for an already-swept trip
            detection_ran = True
        return ApplyResult(Outcome.INSERTED, late=late, detection_ran=detection_ran)


class Store:
    def __init__(self, path: str | Path) -> None:
        self._db = Database(path)

    async def connect(self) -> "Store":
        await self._db.connect(init=migrate)
        return self

    async def close(self) -> None:
        await self._db.close()

    async def apply_event(self, ev: Event, now_ms: int) -> ApplyResult:
        return await self._db.run(_apply, ev, now_ms)

    async def sweep(self, now_ms: int, quiet_ms: int) -> list[str]:
        """Finalize open trips that have been quiet for `quiet_ms` (status -> incomplete)."""

        def _do(conn: sqlite3.Connection) -> list[str]:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT trip_id FROM trips WHERE status = 'open' AND last_ingest_ms <= ?", (now_ms - quiet_ms,)
                ).fetchall()
                for r in rows:
                    _run_detection(conn, r["trip_id"], now_ms)
                    conn.execute("UPDATE trips SET status = 'incomplete' WHERE trip_id = ?", (r["trip_id"],))
                return [r["trip_id"] for r in rows]

        return await self._db.run(_do)

    async def seed_reference(self, drivers: list[Any], vehicles: list[Any]) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO drivers (driver_id, display_name, phone, personality, synthetic) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (driver_id) DO NOTHING",
                    [(d.driver_id, d.display_name, d.phone, d.personality, int(d.synthetic)) for d in drivers],
                )
                conn.executemany(
                    "INSERT INTO vehicles (vehicle_id, driver_id, make_model) VALUES (?, ?, ?) "
                    "ON CONFLICT (vehicle_id) DO NOTHING",
                    [(v.vehicle_id, v.driver_id, v.make_model) for v in vehicles],
                )

        await self._db.run(_do)

    async def list_incidents(self) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM detected_incidents WHERE superseded_at_ms IS NULL "
                                "ORDER BY trip_id, t_start_ms").fetchall()
            return [
                {**dict(r), "trigger_event_ids": json.loads(r["trigger_event_ids_json"]),
                 "details": json.loads(r["details_json"])}
                for r in rows
            ]

        return await self._db.run(_do)

    async def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        """Canonical business state, excluding operational metadata (ingest time,
        `late` flag, detection run counts), which legitimately depend on arrival order."""

        def _do(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
            return {
                "raw_events": [tuple(r) for r in conn.execute(
                    "SELECT event_id, trip_id, seq, event_time_ms, type, payload_json, content_hash "
                    "FROM raw_events ORDER BY event_id")],
                "trips": [tuple(r) for r in conn.execute(
                    "SELECT trip_id, driver_id, vehicle_id, started_at_ms, ended_at_ms, end_seq, distance_km, status "
                    "FROM trips ORDER BY trip_id")],
                "incidents": [tuple(r) for r in conn.execute(
                    "SELECT incident_id, trip_id, kind, t_start_ms, t_end_ms, detector_version, "
                    "trigger_event_ids_json, details_json FROM detected_incidents WHERE superseded_at_ms IS NULL "
                    "ORDER BY incident_id")],
            }

        return await self._db.run(_do)

    async def stats(self) -> dict[str, Any]:
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "events": q("SELECT COUNT(*) FROM raw_events"),
                "late_events": q("SELECT COUNT(*) FROM raw_events WHERE late = 1"),
                "trips": dict(conn.execute("SELECT status, COUNT(*) FROM trips GROUP BY status").fetchall()),
                "incidents": dict(conn.execute(
                    "SELECT kind, COUNT(*) FROM detected_incidents WHERE superseded_at_ms IS NULL GROUP BY kind").fetchall()),
            }

        return await self._db.run(_do)
