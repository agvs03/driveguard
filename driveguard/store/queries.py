"""Read-side queries used by the MCP servers (synchronous; run via Database.run).

These return plain dicts. Authorization is NOT done here: callers (the MCP
tool layer) must check the principal's binding before or after calling.
Columns that could identify a person (drivers.display_name, drivers.phone)
and the simulator's ground-truth persona (drivers.personality) are never
selected by any query in this module.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from driveguard.pipeline.detector import StoredEvent

_EVENT_COLS = "event_id, trip_id, seq, event_time_ms, type, payload_json, late"


def _event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"], "trip_id": row["trip_id"], "seq": row["seq"],
        "event_time_ms": row["event_time_ms"], "type": row["type"],
        "payload": json.loads(row["payload_json"]), "late": bool(row["late"]),
    }


def _incident(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "incident_id": row["incident_id"], "trip_id": row["trip_id"], "kind": row["kind"],
        "t_start_ms": row["t_start_ms"], "t_end_ms": row["t_end_ms"],
        "detector_version": row["detector_version"],
        "trigger_event_ids": json.loads(row["trigger_event_ids_json"]),
        "details": json.loads(row["details_json"]),
        "superseded": row["superseded_at_ms"] is not None,
    }


def get_trip(conn: sqlite3.Connection, trip_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT trip_id, driver_id, vehicle_id, started_at_ms, ended_at_ms, end_seq, distance_km, status "
        "FROM trips WHERE trip_id = ?", (trip_id,)
    ).fetchone()
    if row is None:
        return None
    counts = dict(conn.execute(
        "SELECT type, COUNT(*) FROM raw_events WHERE trip_id = ? GROUP BY type", (trip_id,)).fetchall())
    incident_ids = [r[0] for r in conn.execute(
        "SELECT incident_id FROM detected_incidents WHERE trip_id = ? AND superseded_at_ms IS NULL "
        "ORDER BY t_start_ms", (trip_id,))]
    out = dict(row)
    out["event_counts"] = counts
    out["incident_ids"] = incident_ids
    return out


def trip_driver(conn: sqlite3.Connection, trip_id: str) -> str | None:
    row = conn.execute("SELECT driver_id FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
    return row["driver_id"] if row else None


def list_events(
    conn: sqlite3.Connection, trip_id: str, *, t_from_ms: int | None, t_to_ms: int | None,
    types: list[str] | None, after_seq: int | None, limit: int,
) -> list[dict[str, Any]]:
    sql = f"SELECT {_EVENT_COLS} FROM raw_events WHERE trip_id = ?"
    args: list[Any] = [trip_id]
    if t_from_ms is not None:
        sql += " AND event_time_ms >= ?"
        args.append(t_from_ms)
    if t_to_ms is not None:
        sql += " AND event_time_ms <= ?"
        args.append(t_to_ms)
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        args += types
    if after_seq is not None:
        sql += " AND seq > ?"
        args.append(after_seq)
    sql += " ORDER BY seq LIMIT ?"
    args.append(limit)
    return [_event(r) for r in conn.execute(sql, args)]


def get_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_EVENT_COLS} FROM raw_events WHERE event_id = ?", (event_id,)).fetchone()
    return _event(row) if row else None


def get_events(conn: sqlite3.Connection, event_ids: list[str]) -> list[dict[str, Any]]:
    if not event_ids:
        return []
    rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM raw_events WHERE event_id IN ({','.join('?' * len(event_ids))})", event_ids)
    return [_event(r) for r in rows]


def list_incidents(conn: sqlite3.Connection, trip_id: str, include_superseded: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM detected_incidents WHERE trip_id = ?"
    if not include_superseded:
        sql += " AND superseded_at_ms IS NULL"
    return [_incident(r) for r in conn.execute(sql + " ORDER BY t_start_ms", (trip_id,))]


def get_incident(conn: sqlite3.Connection, incident_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM detected_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
    return _incident(row) if row else None


def driver_aggregates(conn: sqlite3.Connection, driver_id: str) -> dict[str, Any] | None:
    """Aggregates only (no other trips' events or ids): the one deliberate cross-trip read."""
    if conn.execute("SELECT 1 FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone() is None and \
            conn.execute("SELECT 1 FROM trips WHERE driver_id = ?", (driver_id,)).fetchone() is None:
        return None
    t = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(distance_km), 0) AS km FROM trips WHERE driver_id = ?", (driver_id,)
    ).fetchone()
    by_kind = dict(conn.execute(
        "SELECT i.kind, COUNT(*) FROM detected_incidents i JOIN trips t USING (trip_id) "
        "WHERE t.driver_id = ? AND i.superseded_at_ms IS NULL GROUP BY i.kind", (driver_id,)).fetchall())
    return {"driver_id": driver_id, "trips_total": t["n"], "distance_km_total": round(t["km"], 3),
            "incidents_by_kind_total": by_kind}


def trip_stored_events(conn: sqlite3.Connection, trip_id: str) -> list[StoredEvent]:
    rows = conn.execute(
        "SELECT event_id, seq, event_time_ms, type, payload_json FROM raw_events WHERE trip_id = ? ORDER BY seq",
        (trip_id,))
    return [StoredEvent(r["event_id"], r["seq"], r["event_time_ms"], r["type"], json.loads(r["payload_json"]))
            for r in rows]
