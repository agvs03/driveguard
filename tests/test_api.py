"""Phase 6: the reviewer API. Access control on cases and traces, no PII, security headers."""

import json
import sqlite3

import httpx2
import pytest

from driveguard.api.app import create_app
from driveguard.mcp_servers.auth import AUD, TokenIssuer
from tests.mcp_util import SECRET

ISS = TokenIssuer(SECRET)
OTHER_UUID = "00000000-0000-4000-8000-000000000000"


def tok(role="human", scopes=("cases:read", "trace:read"), aud="api", case_id=None, trip_ids=None):
    return ISS.issue(sub=f"{role}:t", role=role, scopes=set(scopes), audiences=[AUD[aud]], case_id=case_id,
                     trip_ids=trip_ids)


@pytest.fixture
async def investigated(db):
    """A db with two investigated cases (one plain, one whose trip carries a PII driver note), guardrails OFF
    so raw PII reaches the trace: the API's display redaction must still keep it off the wire."""
    from driveguard.guardrails import OFF
    from driveguard.redteam.attacks import plant_note
    from tests.test_agents import _incident, _run

    with sqlite3.connect(db) as c:
        trip = c.execute("SELECT trip_id FROM detected_incidents WHERE incident_id = ?",
                         (_incident(db, "hard_braking"),)).fetchone()[0]
    plant_note(db, trip, "My name is Avery Quinlan, call me on +1-555-0142 or avery.q@example.com")
    a = await _run(db, "crash")
    b = await _run(db, "hard_braking", guardrails=OFF)
    return db, a, b


@pytest.fixture
async def client(investigated):
    db, a, b = investigated
    app = create_app(str(db), SECRET)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            yield c, a, b, db


def H(t):
    return {"Authorization": f"Bearer {t}"}


async def test_authentication_is_required_and_audience_checked(client):
    c, a, _, _ = client
    assert (await c.get("/api/cases")).status_code == 401
    assert (await c.get("/api/cases", headers={"Authorization": "Bearer nope"})).status_code == 401
    trips_tok = tok(aud="trips")  # a valid token for another service is not valid here
    assert (await c.get("/api/cases", headers=H(trips_tok))).status_code == 401
    assert (await c.get("/healthz")).status_code == 200


async def test_reviewer_sees_cases_detail_and_a_verified_trace(client):
    c, a, _, _ = client
    t = tok()
    cases = (await c.get("/api/cases", headers=H(t))).json()["cases"]
    assert {x["case_id"] for x in cases} >= {a.case_id}
    d = (await c.get(f"/api/cases/{a.case_id}", headers=H(t))).json()
    assert d["report"]["valid"] and d["runs"][0]["run_id"] == a.run_id
    tr = (await c.get(f"/api/runs/{a.run_id}/trace", headers=H(t))).json()
    assert tr["integrity"] == {"ok": True, "first_bad_seq": None, "spans": len(tr["spans"])}
    cited = {e for f in d["report"]["body"]["findings"] for e in f["cited_event_ids"]}
    assert cited <= set(tr["citations"]), "every citation resolves to the tool call that returned it"


async def test_trace_needs_trace_read_and_agents_can_never_hold_it(client):
    c, a, _, _ = client
    no_trace = tok(scopes=("cases:read",))
    assert (await c.get(f"/api/cases/{a.case_id}", headers=H(no_trace))).status_code == 200
    r = await c.get(f"/api/runs/{a.run_id}/trace", headers=H(no_trace))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    with pytest.raises(ValueError, match="only human tokens"):
        tok(role="agent", scopes=("trace:read",), case_id=a.case_id, trip_ids=["t"])
    with pytest.raises(ValueError, match="only human tokens"):
        tok(role="service", scopes=("trace:read",))


async def test_case_bound_token_sees_only_its_case_with_no_existence_oracle(client):
    c, a, b, _ = client
    t = tok(case_id=a.case_id)
    listed = (await c.get("/api/cases", headers=H(t))).json()["cases"]
    assert [x["case_id"] for x in listed] == [a.case_id]
    assert (await c.get(f"/api/cases/{a.case_id}", headers=H(t))).status_code == 200
    assert (await c.get(f"/api/runs/{a.run_id}/trace", headers=H(t))).status_code == 200
    other = await c.get(f"/api/cases/{b.case_id}", headers=H(t))
    missing = await c.get(f"/api/cases/{OTHER_UUID}", headers=H(t))
    assert other.status_code == missing.status_code == 403 and other.json() == missing.json()
    other_run = await c.get(f"/api/runs/{b.run_id}/trace", headers=H(t))
    missing_run = await c.get(f"/api/runs/{OTHER_UUID}/trace", headers=H(t))
    assert other_run.status_code == missing_run.status_code == 403 and other_run.json() == missing_run.json()


async def test_unbound_token_gets_404_and_bad_ids_400(client):
    c, *_ = client
    t = tok()
    assert (await c.get(f"/api/cases/{OTHER_UUID}", headers=H(t))).status_code == 404
    assert (await c.get(f"/api/runs/{OTHER_UUID}/trace", headers=H(t))).status_code == 404
    assert (await c.get("/api/cases/not-a-uuid", headers=H(t))).status_code == 400
    assert (await c.get("/api/cases?status=BOGUS", headers=H(t))).status_code == 400
    assert (await c.get("/api/cases?limit=x", headers=H(t))).status_code == 400


async def test_no_driver_identity_or_note_pii_reaches_the_wire(client):
    c, a, b, db = client
    t = tok()
    with sqlite3.connect(db) as conn:
        names, phones = zip(*conn.execute("SELECT display_name, phone FROM drivers"))
    bodies = []
    for case_id, run_id in ((a.case_id, a.run_id), (b.case_id, b.run_id)):
        bodies.append((await c.get(f"/api/cases/{case_id}", headers=H(t))).text)
        bodies.append((await c.get(f"/api/runs/{run_id}/trace", headers=H(t))).text)
    blob = "\n".join(bodies)
    for secret in [*names, *(p for p in phones if p), "Avery Quinlan", "555-0142", "avery.q@example.com"]:
        assert secret not in blob, secret
    # ...and the planted note really was in the stored trace (guardrails off), so redaction did the work:
    with sqlite3.connect(db) as conn:
        raw = "\n".join(r[0] for r in conn.execute("SELECT output_json FROM audit_trace WHERE run_id = ?", (b.run_id,)))
    assert "Avery Quinlan" in raw
    assert "[REDACTED:" in blob


async def test_security_headers_on_every_response(client):
    c, a, _, _ = client
    for r in (await c.get("/api/cases", headers=H(tok())), await c.get("/api/cases"), await c.get("/nope")):
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in r.headers, "no CORS: the UI is same-origin"


async def test_metrics_and_slo_endpoints(client):
    c, a, _, _ = client
    await c.get(f"/api/cases/{a.case_id}", headers=H(tok()))
    text = (await c.get("/metrics")).text
    assert 'driveguard_api_requests_total{route="/api/cases/{case_id}",status="200"}' in text
    assert a.case_id not in text, "ids never become metric labels"
    slos = {s["name"]: s for s in (await c.get("/api/slo", headers=H(tok()))).json()["slos"]}
    assert slos["investigation-completion"]["total"] >= 2 and slos["api-case-load"]["total"] >= 1


async def test_ui_is_served_when_built(investigated, tmp_path):
    db, *_ = investigated
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    app = create_app(str(db), SECRET, static_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/")
            assert r.status_code == 200 and "root" in r.text and "content-security-policy" in r.headers
            assert (await c.get("/api/cases")).status_code == 401, "API routes win over the static mount"


def test_display_redaction_leaves_ids_and_numbers_alone():
    from driveguard.api.app import display_redact
    obj = {"event_id": "0a1b2c3d-1111-4222-8333-444455556666", "t": 1768216407600,
           "text": "Driver Riley Novak said call 555-123-4567", "long": "x " * 5000}
    out = display_redact(obj, ["Riley Novak"])
    assert out["event_id"] == obj["event_id"] and out["t"] == obj["t"]
    assert "Riley" not in out["text"] and "555-123" not in out["text"]
    assert len(out["long"]) < 4200 and "clipped for display" in out["long"]
    assert json.dumps(out)
