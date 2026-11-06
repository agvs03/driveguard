r"""Investigation cases: state machine, optimistic locking, append-only history.

State machine (enforced here, not just in the orchestrator):

    OPEN --start--> INVESTIGATING --submit--> AWAITING_APPROVAL --review(approve)--> CLOSED
      \                  |   ^                        |
       \--escalate-->  ESCALATED  \---review(reject)--+  (back to INVESTIGATING)
                         |
                         +--review(approve)--> CLOSED, review(reject) --> INVESTIGATING

Only a human may `review` (checked by the caller via scope AND role, and again
here via `actor.role`). Every mutation bumps `version` and writes a
`case_history` row in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from driveguard.errors import Code, DriveGuardError, invalid, not_found
from driveguard.store.db import transaction

_NS = uuid.UUID("9d2e1c4b-7a6f-4e3d-8c2b-1a0f9e8d7c6b")

Status = Literal["OPEN", "INVESTIGATING", "AWAITING_APPROVAL", "ESCALATED", "CLOSED"]
Classification = Literal["hard_braking", "crash", "near_miss", "phone_distraction", "no_incident", "inconclusive"]

TRANSITIONS: dict[str, dict[str, str]] = {
    "start": {"OPEN": "INVESTIGATING"},
    "submit": {"INVESTIGATING": "AWAITING_APPROVAL"},
    "escalate": {"OPEN": "ESCALATED", "INVESTIGATING": "ESCALATED"},
    "approve": {"AWAITING_APPROVAL": "CLOSED", "ESCALATED": "CLOSED"},
    "reject": {"AWAITING_APPROVAL": "INVESTIGATING", "ESCALATED": "INVESTIGATING"},
}
MUTABLE = {"OPEN", "INVESTIGATING"}  # notes/evidence/classification allowed only here (+ notes when escalated)


@dataclass(frozen=True)
class Actor:
    sub: str
    role: str  # "agent" | "human" | "service"


def case_id_for(incident_id: str) -> str:
    return str(uuid.uuid5(_NS, incident_id))


def _history(conn: sqlite3.Connection, case_id: str, actor: Actor, action: str, frm: str | None,
             to: str | None, detail: dict[str, Any], now_ms: int) -> None:
    conn.execute(
        "INSERT INTO case_history (case_id, actor_sub, actor_role, action, from_status, to_status, detail_json, at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (case_id, actor.sub, actor.role, action, frm, to, json.dumps(detail, sort_keys=True), now_ms),
    )


def _load(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        raise not_found("case not found")
    return row


def _check_version(row: sqlite3.Row, expected_version: int | None) -> None:
    if expected_version is not None and expected_version != row["version"]:
        raise DriveGuardError(Code.CONFLICT, f"case version is {row['version']}, expected {expected_version}; "
                                             "re-read the case and retry")


def _bump(conn: sqlite3.Connection, case_id: str, now_ms: int, **fields: Any) -> None:
    sets = ", ".join([f"{k} = ?" for k in fields] + ["version = version + 1", "updated_at_ms = ?"])
    conn.execute(f"UPDATE cases SET {sets} WHERE case_id = ?", (*fields.values(), now_ms, case_id))


def create_case(conn: sqlite3.Connection, incident_id: str, actor: Actor, now_ms: int) -> tuple[dict[str, Any], bool]:
    """Idempotent: one case per incident. Returns (case, created)."""
    with transaction(conn):
        inc = conn.execute("SELECT trip_id, superseded_at_ms FROM detected_incidents WHERE incident_id = ?",
                           (incident_id,)).fetchone()
        if inc is None:
            raise not_found("incident not found")
        cid = case_id_for(incident_id)
        if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (cid,)).fetchone():
            created = False
        else:
            if inc["superseded_at_ms"] is not None:
                raise DriveGuardError(Code.FAILED_PRECONDITION, "incident has been superseded by re-detection")
            conn.execute(
                "INSERT INTO cases (case_id, incident_id, trip_id, status, created_by, created_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, 'OPEN', ?, ?, ?)", (cid, incident_id, inc["trip_id"], actor.sub, now_ms, now_ms))
            _history(conn, cid, actor, "create", None, "OPEN", {"incident_id": incident_id}, now_ms)
            created = True
    return get_case(conn, cid), created


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    row = _load(conn, case_id)
    inc = conn.execute("SELECT kind, t_start_ms, t_end_ms, superseded_at_ms, trigger_event_ids_json "
                       "FROM detected_incidents WHERE incident_id = ?", (row["incident_id"],)).fetchone()
    return {
        **{k: row[k] for k in ("case_id", "incident_id", "trip_id", "status", "classification", "confidence",
                               "created_by", "created_at_ms", "updated_at_ms", "version")},
        "incident": {"kind": inc["kind"], "t_start_ms": inc["t_start_ms"], "t_end_ms": inc["t_end_ms"],
                     "superseded": inc["superseded_at_ms"] is not None,
                     "trigger_event_ids": json.loads(inc["trigger_event_ids_json"])},
        "notes": [dict(r) for r in conn.execute(
            "SELECT note_id, author_sub, author_role, text, created_at_ms FROM case_notes WHERE case_id = ? "
            "ORDER BY created_at_ms, note_id", (case_id,))],
        "evidence": [dict(r) for r in conn.execute(
            "SELECT event_id, reason, added_by, added_at_ms FROM case_evidence WHERE case_id = ? "
            "ORDER BY added_at_ms, event_id", (case_id,))],
        "history": [{**dict(r), "detail": json.loads(r["detail_json"])} for r in conn.execute(
            "SELECT actor_sub, actor_role, action, from_status, to_status, detail_json, at_ms FROM case_history "
            "WHERE case_id = ? ORDER BY id", (case_id,))],
    }


def list_cases(conn: sqlite3.Connection, status: str | None, limit: int,
               only_case_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT case_id, incident_id, trip_id, status, classification, confidence, version, updated_at_ms " \
          "FROM cases WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if only_case_id is not None:
        sql += " AND case_id = ?"
        args.append(only_case_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY updated_at_ms DESC, case_id LIMIT ?", (*args, limit))]


def _transition(conn: sqlite3.Connection, case_id: str, action: str, actor: Actor, now_ms: int,
                expected_version: int | None, detail: dict[str, Any], precheck=None) -> dict[str, Any]:
    with transaction(conn):
        row = _load(conn, case_id)
        _check_version(row, expected_version)
        to = TRANSITIONS[action].get(row["status"])
        if to is None:
            raise DriveGuardError(Code.FAILED_PRECONDITION,
                                  f"cannot {action} a case in status {row['status']}")
        if precheck:
            precheck(row)
        _bump(conn, case_id, now_ms, status=to)
        _history(conn, case_id, actor, action, row["status"], to, detail, now_ms)
    return get_case(conn, case_id)


def start_investigation(conn, case_id: str, actor: Actor, now_ms: int, expected_version: int | None = None):
    return _transition(conn, case_id, "start", actor, now_ms, expected_version, {})


def escalate(conn, case_id: str, actor: Actor, now_ms: int, reason: str, expected_version: int | None = None):
    return _transition(conn, case_id, "escalate", actor, now_ms, expected_version, {"reason": reason})


def submit_for_approval(conn, case_id: str, actor: Actor, now_ms: int, expected_version: int):
    def pre(row: sqlite3.Row) -> None:
        if row["classification"] is None:
            raise DriveGuardError(Code.FAILED_PRECONDITION, "set a classification before submitting")
        n = conn.execute("SELECT COUNT(*) FROM case_evidence WHERE case_id = ?", (case_id,)).fetchone()[0]
        if n == 0 and row["classification"] != "inconclusive":
            raise DriveGuardError(Code.FAILED_PRECONDITION, "attach at least one evidence event before submitting")
    return _transition(conn, case_id, "submit", actor, now_ms, expected_version, {}, precheck=pre)


def review(conn, case_id: str, actor: Actor, now_ms: int, decision: Literal["approve", "reject"],
           comment: str, expected_version: int):
    if actor.role != "human":  # second line of defence behind the scope check
        raise DriveGuardError(Code.FORBIDDEN, "only a human reviewer can review a case")
    if decision not in ("approve", "reject"):
        raise invalid("decision must be approve or reject")
    return _transition(conn, case_id, decision, actor, now_ms, expected_version, {"comment": comment})


def set_classification(conn, case_id: str, actor: Actor, now_ms: int, classification: str, confidence: float,
                       expected_version: int):
    with transaction(conn):
        row = _load(conn, case_id)
        _check_version(row, expected_version)
        if row["status"] not in MUTABLE:
            raise DriveGuardError(Code.FAILED_PRECONDITION, f"case is {row['status']}; classification is frozen")
        _bump(conn, case_id, now_ms, classification=classification, confidence=confidence)
        _history(conn, case_id, actor, "classify", row["status"], row["status"],
                 {"classification": classification, "confidence": confidence,
                  "previous": row["classification"]}, now_ms)
    return get_case(conn, case_id)


def add_note(conn, case_id: str, actor: Actor, now_ms: int, text: str) -> dict[str, Any]:
    with transaction(conn):
        row = _load(conn, case_id)
        if row["status"] == "CLOSED":
            raise DriveGuardError(Code.FAILED_PRECONDITION, "case is closed")
        note_id = str(uuid.uuid4())
        conn.execute("INSERT INTO case_notes (note_id, case_id, author_sub, author_role, text, created_at_ms) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (note_id, case_id, actor.sub, actor.role, text, now_ms))
        _bump(conn, case_id, now_ms)
        _history(conn, case_id, actor, "note", row["status"], row["status"], {"note_id": note_id}, now_ms)
    return {"note_id": note_id, "case_id": case_id}


def attach_evidence(conn, case_id: str, actor: Actor, now_ms: int, event_ids: list[str],
                    reason: str) -> dict[str, Any]:
    with transaction(conn):
        row = _load(conn, case_id)
        if row["status"] not in MUTABLE:
            raise DriveGuardError(Code.FAILED_PRECONDITION, f"case is {row['status']}; evidence is frozen")
        found = {r["event_id"]: r["trip_id"] for r in conn.execute(
            f"SELECT event_id, trip_id FROM raw_events WHERE event_id IN ({','.join('?' * len(event_ids))})",
            event_ids)}
        bad = [e for e in event_ids if found.get(e) != row["trip_id"]]
        if bad:
            # Same message whether the event is missing or belongs to another trip: no existence oracle.
            raise invalid(f"{len(bad)} event id(s) do not belong to this case's trip: {', '.join(bad[:5])}")
        added = 0
        for e in dict.fromkeys(event_ids):
            cur = conn.execute(
                "INSERT INTO case_evidence (case_id, event_id, reason, added_by, added_at_ms) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (case_id, event_id) DO NOTHING", (case_id, e, reason, actor.sub, now_ms))
            added += cur.rowcount
        if added:
            _bump(conn, case_id, now_ms)
            _history(conn, case_id, actor, "evidence", row["status"], row["status"],
                     {"event_ids": list(dict.fromkeys(event_ids)), "added": added}, now_ms)
    return {"case_id": case_id, "added": added, "already_attached": len(set(event_ids)) - added}
