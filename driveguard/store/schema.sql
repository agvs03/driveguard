-- DriveGuard store schema (SQLite dialect; kept close to portable SQL).
-- All rows are synthetic. drivers.synthetic is enforced by a CHECK.

CREATE TABLE IF NOT EXISTS drivers (
    driver_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    phone        TEXT,
    personality  TEXT,
    synthetic    INTEGER NOT NULL CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    driver_id  TEXT NOT NULL REFERENCES drivers (driver_id),
    make_model TEXT NOT NULL
);

-- No FK to drivers: ingestion must not fail because reference data is missing.
CREATE TABLE IF NOT EXISTS trips (
    trip_id           TEXT PRIMARY KEY,
    driver_id         TEXT,
    vehicle_id        TEXT,
    started_at_ms     INTEGER,
    ended_at_ms       INTEGER,
    end_seq           INTEGER,
    distance_km       REAL,
    max_event_time_ms INTEGER NOT NULL,
    last_ingest_ms    INTEGER NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN ('open', 'complete', 'incomplete')),
    detection_runs    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_trips_status ON trips (status, last_ingest_ms);

CREATE TABLE IF NOT EXISTS raw_events (
    event_id       TEXT    PRIMARY KEY,           -- idempotency key
    trip_id        TEXT    NOT NULL REFERENCES trips (trip_id),
    seq            INTEGER NOT NULL,
    event_time_ms  INTEGER NOT NULL,
    ingest_time_ms INTEGER NOT NULL,              -- operational metadata
    type           TEXT    NOT NULL,
    payload_json   TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    late           INTEGER NOT NULL,              -- operational metadata
    schema_version INTEGER NOT NULL,
    UNIQUE (trip_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_raw_events_trip_time ON raw_events (trip_id, event_time_ms);
CREATE INDEX IF NOT EXISTS ix_raw_events_trip_type ON raw_events (trip_id, type);

CREATE TABLE IF NOT EXISTS detected_incidents (
    incident_id            TEXT PRIMARY KEY,      -- uuid5(trip, kind, t_start, version): stable across re-runs
    trip_id                TEXT    NOT NULL REFERENCES trips (trip_id),
    kind                   TEXT    NOT NULL,
    t_start_ms             INTEGER NOT NULL,
    t_end_ms               INTEGER NOT NULL,
    detector_version       TEXT    NOT NULL,
    trigger_event_ids_json TEXT    NOT NULL,
    details_json           TEXT    NOT NULL,
    UNIQUE (trip_id, kind, t_start_ms, detector_version)
);
