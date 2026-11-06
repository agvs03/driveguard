"""Forward-only schema migrations tracked with SQLite's `PRAGMA user_version`.

Migration 1 is the Phase 1 schema (idempotent `IF NOT EXISTS` DDL, so Phase 1
databases at user_version 0 upgrade cleanly). Never edit a shipped migration;
add a new one.
"""

from __future__ import annotations

import sqlite3
from importlib import resources

from driveguard.store.db import transaction

_V2 = """
ALTER TABLE detected_incidents ADD COLUMN superseded_at_ms INTEGER;

CREATE TABLE IF NOT EXISTS risk_scores (
    score_id        TEXT PRIMARY KEY,           -- uuid5(trip, weights_version, input_hash)
    trip_id         TEXT    NOT NULL REFERENCES trips (trip_id),
    weights_version TEXT    NOT NULL,
    input_hash      TEXT    NOT NULL,
    score           REAL    NOT NULL CHECK (score BETWEEN 0 AND 100),
    result_json     TEXT    NOT NULL,
    computed_at_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    incident_id    TEXT    NOT NULL UNIQUE REFERENCES detected_incidents (incident_id),
    trip_id        TEXT    NOT NULL REFERENCES trips (trip_id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('OPEN', 'INVESTIGATING', 'AWAITING_APPROVAL', 'ESCALATED', 'CLOSED')),
    classification TEXT CHECK (classification IS NULL OR classification IN
                       ('hard_braking', 'crash', 'near_miss', 'phone_distraction', 'no_incident', 'inconclusive')),
    confidence     REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    created_by     TEXT    NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_cases_status ON cases (status, updated_at_ms);

CREATE TABLE IF NOT EXISTS case_notes (
    note_id       TEXT PRIMARY KEY,
    case_id       TEXT    NOT NULL REFERENCES cases (case_id),
    author_sub    TEXT    NOT NULL,
    author_role   TEXT    NOT NULL,
    text          TEXT    NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS case_evidence (
    case_id     TEXT    NOT NULL REFERENCES cases (case_id),
    event_id    TEXT    NOT NULL REFERENCES raw_events (event_id),
    reason      TEXT    NOT NULL,
    added_by    TEXT    NOT NULL,
    added_at_ms INTEGER NOT NULL,
    PRIMARY KEY (case_id, event_id)
);

CREATE TABLE IF NOT EXISTS case_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT    NOT NULL REFERENCES cases (case_id),
    actor_sub   TEXT    NOT NULL,
    actor_role  TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    detail_json TEXT    NOT NULL,
    at_ms       INTEGER NOT NULL
);
"""


def _v1() -> str:
    return resources.files("driveguard.store").joinpath("schema.sql").read_text(encoding="utf-8")


_V3 = """
CREATE TABLE IF NOT EXISTS investigation_runs (
    run_id         TEXT PRIMARY KEY,
    case_id        TEXT    REFERENCES cases (case_id),
    incident_id    TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('running', 'awaiting_approval', 'escalated', 'skipped', 'failed')),
    llm            TEXT    NOT NULL,
    started_at_ms  INTEGER NOT NULL,
    ended_at_ms    INTEGER,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    model_calls    INTEGER NOT NULL DEFAULT 0,
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    outcome_json   TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_case ON investigation_runs (case_id, started_at_ms);

-- Append-only and hash-chained per run. Triggers refuse UPDATE and DELETE.
CREATE TABLE IF NOT EXISTS audit_trace (
    run_id         TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    case_id        TEXT,
    span_id        TEXT    NOT NULL UNIQUE,
    parent_span_id TEXT,
    kind           TEXT    NOT NULL CHECK (kind IN ('run_start', 'state', 'model_call', 'tool_call', 'validation',
                                                    'guard', 'human_decision', 'run_end')),
    actor          TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('ok', 'error')),
    input_json     TEXT    NOT NULL,
    output_json    TEXT    NOT NULL,
    model          TEXT,
    usage_json     TEXT,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    latency_ms     REAL    NOT NULL DEFAULT 0,
    at_ms          INTEGER NOT NULL,
    prev_hash      TEXT    NOT NULL,
    hash           TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_trace_case ON audit_trace (case_id, run_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_trace_no_update BEFORE UPDATE ON audit_trace
BEGIN
    SELECT RAISE(ABORT, 'audit_trace is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_trace_no_delete BEFORE DELETE ON audit_trace
BEGIN
    SELECT RAISE(ABORT, 'audit_trace is append-only');
END;

CREATE TABLE IF NOT EXISTS reports (
    report_id       TEXT PRIMARY KEY,
    case_id         TEXT    NOT NULL REFERENCES cases (case_id),
    run_id          TEXT    NOT NULL REFERENCES investigation_runs (run_id),
    valid           INTEGER NOT NULL,
    report_json     TEXT    NOT NULL,
    validation_json TEXT    NOT NULL,
    markdown        TEXT    NOT NULL,
    created_at_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reports_case ON reports (case_id, created_at_ms);
"""

MIGRATIONS: list[tuple[int, object]] = [(1, _v1), (2, lambda: _V2), (3, lambda: _V3)]
LATEST = MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        with transaction(conn):
            for stmt in _split(sql()):  # executescript would COMMIT our transaction
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(version)}")
        current = version
    return current


def _split(script: str) -> list[str]:
    """Split a script into statements. sqlite3.complete_statement keeps the
    semicolons inside trigger bodies (BEGIN ... END;) from splitting a statement."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            if buf.strip().rstrip(";").strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf[:80]!r}")
    return out
