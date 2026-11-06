import sqlite3

import pytest

from driveguard.cases import service as svc
from driveguard.errors import DriveGuardError
from driveguard.store import queries as q

AGENT = svc.Actor("agent:x", "agent")
HUMAN = svc.Actor("human:r", "human")
SVC = svc.Actor("service:o", "service")


@pytest.fixture
def conn(db):
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def _incident(conn, kind=None):
    sql = "SELECT incident_id, trip_id FROM detected_incidents WHERE superseded_at_ms IS NULL"
    args = ()
    if kind:
        sql += " AND kind = ?"
        args = (kind,)
    return conn.execute(sql + " ORDER BY incident_id LIMIT 1", args).fetchone()


def _code(excinfo):
    return excinfo.value.code.value


def test_create_is_idempotent(conn):
    inc = _incident(conn)
    a, created_a = svc.create_case(conn, inc["incident_id"], SVC, 1)
    b, created_b = svc.create_case(conn, inc["incident_id"], SVC, 2)
    assert created_a and not created_b and a["case_id"] == b["case_id"]
    assert a["status"] == "OPEN" and a["version"] == 1 and a["trip_id"] == inc["trip_id"]


def test_happy_path_to_closed_with_full_history(conn):
    inc = _incident(conn)
    case, _ = svc.create_case(conn, inc["incident_id"], SVC, 1)
    cid = case["case_id"]
    c = svc.start_investigation(conn, cid, AGENT, 2)
    ev = case["incident"]["trigger_event_ids"][0]
    svc.attach_evidence(conn, cid, AGENT, 3, [ev], "trigger window")
    c = svc.get_case(conn, cid)
    c = svc.set_classification(conn, cid, AGENT, 4, "hard_braking", 0.8, c["version"])
    c = svc.submit_for_approval(conn, cid, AGENT, 5, c["version"])
    assert c["status"] == "AWAITING_APPROVAL"
    c = svc.review(conn, cid, HUMAN, 6, "approve", "looks right", c["version"])
    assert c["status"] == "CLOSED"
    assert [h["action"] for h in c["history"]] == ["create", "start", "evidence", "classify", "submit", "approve"]
    assert [h["actor_role"] for h in c["history"]] == ["service", "agent", "agent", "agent", "agent", "human"]


def test_reject_returns_to_investigating(conn):
    inc = _incident(conn)
    cid = svc.create_case(conn, inc["incident_id"], SVC, 1)[0]["case_id"]
    svc.start_investigation(conn, cid, AGENT, 2)
    c = svc.set_classification(conn, cid, AGENT, 3, "inconclusive", 0.3, svc.get_case(conn, cid)["version"])
    c = svc.submit_for_approval(conn, cid, AGENT, 4, c["version"])
    c = svc.review(conn, cid, HUMAN, 5, "reject", "need more evidence", c["version"])
    assert c["status"] == "INVESTIGATING"


@pytest.mark.parametrize("action", ["submit", "approve", "reject"])
def test_illegal_transitions_from_open(conn, action):
    cid = svc.create_case(conn, _incident(conn)["incident_id"], SVC, 1)[0]["case_id"]
    with pytest.raises(DriveGuardError) as e:
        if action == "submit":
            svc.submit_for_approval(conn, cid, AGENT, 2, 1)
        else:
            svc.review(conn, cid, HUMAN, 2, action, "x", 1)
    assert _code(e) == "FAILED_PRECONDITION"


def test_agent_cannot_review_even_with_direct_service_call(conn):
    cid = svc.create_case(conn, _incident(conn)["incident_id"], SVC, 1)[0]["case_id"]
    with pytest.raises(DriveGuardError) as e:
        svc.review(conn, cid, AGENT, 2, "approve", "self-approve", 1)
    assert _code(e) == "FORBIDDEN"


def test_stale_version_conflicts(conn):
    cid = svc.create_case(conn, _incident(conn)["incident_id"], SVC, 1)[0]["case_id"]
    svc.start_investigation(conn, cid, AGENT, 2)
    with pytest.raises(DriveGuardError) as e:
        svc.set_classification(conn, cid, AGENT, 3, "crash", 0.9, expected_version=1)
    assert _code(e) == "CONFLICT" and e.value.retryable


def test_submit_requires_classification_and_evidence(conn):
    cid = svc.create_case(conn, _incident(conn)["incident_id"], SVC, 1)[0]["case_id"]
    c = svc.start_investigation(conn, cid, AGENT, 2)
    with pytest.raises(DriveGuardError, match="classification"):
        svc.submit_for_approval(conn, cid, AGENT, 3, c["version"])
    c = svc.set_classification(conn, cid, AGENT, 3, "crash", 0.9, c["version"])
    with pytest.raises(DriveGuardError, match="evidence"):
        svc.submit_for_approval(conn, cid, AGENT, 4, c["version"])


def test_evidence_must_come_from_the_case_trip(conn):
    inc = _incident(conn)
    cid = svc.create_case(conn, inc["incident_id"], SVC, 1)[0]["case_id"]
    svc.start_investigation(conn, cid, AGENT, 2)
    other = conn.execute("SELECT event_id FROM raw_events WHERE trip_id != ? LIMIT 1", (inc["trip_id"],)).fetchone()[0]
    with pytest.raises(DriveGuardError) as e:
        svc.attach_evidence(conn, cid, AGENT, 3, [other], "sneaky")
    assert _code(e) == "INVALID_ARGUMENT"
    with pytest.raises(DriveGuardError) as e2:
        svc.attach_evidence(conn, cid, AGENT, 3, ["00000000-0000-4000-8000-000000000000"], "made up")
    assert str(e.value).split(":")[0] == str(e2.value).split(":")[0], "no existence oracle"


def test_evidence_is_idempotent(conn):
    inc = _incident(conn)
    case = svc.create_case(conn, inc["incident_id"], SVC, 1)[0]
    ev = case["incident"]["trigger_event_ids"][:2]
    assert svc.attach_evidence(conn, case["case_id"], AGENT, 2, ev, "r")["added"] == len(ev)
    again = svc.attach_evidence(conn, case["case_id"], AGENT, 3, ev, "r")
    assert again == {"case_id": case["case_id"], "added": 0, "already_attached": len(ev)}


def test_closed_case_is_frozen(conn):
    inc = _incident(conn)
    case = svc.create_case(conn, inc["incident_id"], SVC, 1)[0]
    cid = case["case_id"]
    c = svc.escalate(conn, cid, AGENT, 2, "budget exceeded")
    c = svc.review(conn, cid, HUMAN, 3, "approve", "closing", c["version"])
    for attempt in (
        lambda: svc.add_note(conn, cid, AGENT, 4, "late note"),
        lambda: svc.attach_evidence(conn, cid, AGENT, 4, case["incident"]["trigger_event_ids"][:1], "r"),
        lambda: svc.set_classification(conn, cid, AGENT, 4, "crash", 1.0, c["version"]),
        lambda: svc.start_investigation(conn, cid, AGENT, 4),
    ):
        with pytest.raises(DriveGuardError) as e:
            attempt()
        assert _code(e) == "FAILED_PRECONDITION"


def test_superseded_incident_cannot_open_a_case(conn):
    inc = _incident(conn)
    conn.execute("UPDATE detected_incidents SET superseded_at_ms = 1 WHERE incident_id = ?", (inc["incident_id"],))
    with pytest.raises(DriveGuardError) as e:
        svc.create_case(conn, inc["incident_id"], SVC, 1)
    assert _code(e) == "FAILED_PRECONDITION"


def test_queries_never_select_identity_columns():
    import inspect
    src = inspect.getsource(q)
    body = src.split('"""', 2)[2]  # skip the module docstring, which names them on purpose
    for col in ("display_name", "phone", "personality"):
        assert col not in body
