import json
import sqlite3
from importlib import resources

from driveguard.cases import service as svc
from driveguard.store.migrations import LATEST, migrate
from driveguard.store.repository import _run_detection


def _conn(path):
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def test_phase1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "p1.db"
    c = _conn(path)
    c.executescript(resources.files("driveguard.store").joinpath("schema.sql").read_text())  # Phase 1 layout
    c.execute("INSERT INTO trips (trip_id, max_event_time_ms, last_ingest_ms, status) VALUES ('t', 1, 1, 'open')")
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert migrate(c) == LATEST
    assert c.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 1
    cols = {r[1] for r in c.execute("PRAGMA table_info(detected_incidents)")}
    assert "superseded_at_ms" in cols
    assert migrate(c) == LATEST  # idempotent


def test_redetection_supersedes_instead_of_deleting(db):
    c = _conn(db)
    inc = c.execute("SELECT * FROM detected_incidents WHERE kind = 'hard_braking' AND superseded_at_ms IS NULL "
                    "LIMIT 1").fetchone()
    case, _ = svc.create_case(c, inc["incident_id"], svc.Actor("s", "service"), 1)
    triggers = json.loads(inc["trigger_event_ids_json"])
    saved = [dict(r) for r in c.execute(
        f"SELECT * FROM raw_events WHERE event_id IN ({','.join('?' * len(triggers))})", triggers)]

    # Simulate the evidence changing under re-detection (e.g. a corrected upload).
    c.execute(f"UPDATE raw_events SET payload_json = json_set(payload_json, '$.ax_peak', 0.0) "
              f"WHERE event_id IN ({','.join('?' * len(triggers))})", triggers)
    _run_detection(c, inc["trip_id"], now_ms=99)
    row = c.execute("SELECT superseded_at_ms FROM detected_incidents WHERE incident_id = ?",
                    (inc["incident_id"],)).fetchone()
    assert row["superseded_at_ms"] == 99, "incident kept, marked superseded"
    assert svc.get_case(c, case["case_id"])["incident"]["superseded"] is True

    for r in saved:
        c.execute("UPDATE raw_events SET payload_json = ? WHERE event_id = ?", (r["payload_json"], r["event_id"]))
    _run_detection(c, inc["trip_id"], now_ms=100)
    row = c.execute("SELECT superseded_at_ms FROM detected_incidents WHERE incident_id = ?",
                    (inc["incident_id"],)).fetchone()
    assert row["superseded_at_ms"] is None, "same incident id comes back"
