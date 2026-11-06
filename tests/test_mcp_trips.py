import json
import sqlite3

import pytest

from driveguard.mcp_servers.common import RateLimiter
from driveguard.mcp_servers.trips import build_trips_server
from tests.mcp_util import HUMAN, agent_token, call, client, token

FAKE = "00000000-0000-4000-8000-000000000000"


def _trips(sim):
    return list(dict.fromkeys(str(e.trip_id) for e in sim.events))


def _trip_with(sim, kind):
    return next(l.trip_id for l in sim.labels if l.kind == kind)


def _trip_with_note(sim):
    return next(str(e.trip_id) for e in sim.events if e.type == "driver_note")


@pytest.fixture
def srv(db):
    return build_trips_server(str(db))


async def test_every_tool_has_strict_schema_and_description(srv):
    async with client(srv, token("human", HUMAN), "trips") as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == {"get_trip", "list_events", "get_events", "list_incidents", "get_incident",
                                       "get_risk_factors", "get_driver_profile"}
    for t in tools:
        assert t.input_schema.get("additionalProperties") is False, t.name
        assert t.description and len(t.description) > 20, t.name
        assert t.output_schema, t.name


async def test_no_token_is_unauthenticated(srv, sim):
    async with client(srv, None, "trips") as c:
        _, err = await call(c, "get_trip", trip_id=_trips(sim)[0])
    assert err == {"code": "UNAUTHENTICATED", "message": "missing or invalid bearer token", "retryable": False}


async def test_missing_scope_is_forbidden(srv, sim):
    async with client(srv, token("human", {"cases:read"}, aud=("trips",)), "trips") as c:
        _, err = await call(c, "get_trip", trip_id=_trips(sim)[0])
    assert err["code"] == "FORBIDDEN" and "trips:read" in err["message"]


async def test_bound_token_cannot_reach_other_trips_and_cannot_probe(srv, sim):
    mine, other = _trips(sim)[:2]
    async with client(srv, agent_token("case-1", mine), "trips") as c:
        ok, err = await call(c, "get_trip", trip_id=mine)
        assert err is None and ok["trip_id"] == mine
        _, e_other = await call(c, "get_trip", trip_id=other)
        _, e_fake = await call(c, "get_trip", trip_id=FAKE)
        assert e_other == e_fake and e_other["code"] == "FORBIDDEN", "existing and missing trips look identical"
        for tool, args in [("list_events", {"trip_id": other}), ("list_incidents", {"trip_id": other}),
                           ("get_risk_factors", {"trip_id": other})]:
            _, e = await call(c, tool, **args)
            assert e["code"] == "FORBIDDEN", tool
        other_event = next(str(e.event_id) for e in sim.events if str(e.trip_id) == other)
        my_event = next(str(e.event_id) for e in sim.events if str(e.trip_id) == mine)
        _, e = await call(c, "get_events", event_ids=[my_event, other_event])
        assert e["code"] == "FORBIDDEN", "one foreign id poisons the whole request"


async def test_unbound_human_gets_not_found_for_missing(srv):
    async with client(srv, token("human", HUMAN), "trips") as c:
        _, err = await call(c, "get_trip", trip_id=FAKE)
    assert err["code"] == "NOT_FOUND"


async def test_pagination_returns_each_event_exactly_once(srv, sim):
    trip = _trips(sim)[0]
    expected = [str(e.event_id) for e in sim.events if str(e.trip_id) == trip]
    got, after = [], None
    async with client(srv, agent_token("c", trip), "trips") as c:
        while True:
            args = {"trip_id": trip, "limit": 137}
            if after is not None:
                args["after_seq"] = after
            page, err = await call(c, "list_events", **args)
            assert err is None
            got += [e["event_id"] for e in page["events"]]
            after = page["next_after_seq"]
            if after is None:
                break
    assert got == expected


async def test_filters_and_argument_validation(srv, sim):
    trip = _trips(sim)[0]
    t0 = min(e.event_time_ms for e in sim.events if str(e.trip_id) == trip)
    async with client(srv, agent_token("c", trip), "trips") as c:
        page, _ = await call(c, "list_events", trip_id=trip, types=["phone", "brake"], limit=500)
        assert page["events"] and {e["type"] for e in page["events"]} <= {"phone", "brake"}
        page, _ = await call(c, "list_events", trip_id=trip, t_from_ms=t0, t_to_ms=t0 + 10_000, limit=500)
        assert all(t0 <= e["event_time_ms"] <= t0 + 10_000 for e in page["events"])
        for bad in ({"limit": 0}, {"limit": 501}, {"types": ["teleport"]}, {"type": ["gps"]},
                    {"t_from_ms": 10, "t_to_ms": 5}, {"trip_id": "not-a-uuid"}):
            _, err = await call(c, "list_events", **{"trip_id": trip, **bad})
            assert err["code"] == "INVALID_ARGUMENT", bad
            assert "input_value" not in err["message"], "validation errors must not echo inputs"


async def test_gps_coarse_outside_incident_windows_full_inside(srv, sim):
    trip = _trip_with(sim, "hard_braking")
    async with client(srv, agent_token("c", trip), "trips") as c:
        incs, _ = await call(c, "list_incidents", trip_id=trip)
        page, _ = await call(c, "list_events", trip_id=trip, types=["gps"], limit=500)
    windows = [(i["t_start_ms"] - 60_000, i["t_end_ms"] + 60_000) for i in incs]
    seen = set()
    for e in page["events"]:
        inside = any(a <= e["event_time_ms"] <= b for a, b in windows)
        assert e["location_precision"] == ("full" if inside else "coarse")
        if not inside:
            assert e["payload"]["lat"] == round(e["payload"]["lat"], 3)
        seen.add(e["location_precision"])
    assert seen == {"full", "coarse"}


async def test_driver_notes_flagged_untrusted(srv, sim):
    trip = _trip_with_note(sim)
    async with client(srv, agent_token("c", trip), "trips") as c:
        page, _ = await call(c, "list_events", trip_id=trip, types=["driver_note"])
    assert page["events"] and all(e["untrusted_text"] for e in page["events"])


async def test_no_identity_data_in_any_output(srv, sim, db):
    conn = sqlite3.connect(db)
    secrets = [x for row in conn.execute("SELECT display_name, phone, personality FROM drivers") for x in row]
    assert secrets
    trip = _trips(sim)[0]
    driver = next(e.payload["driver_id"] for e in sim.events if str(e.trip_id) == trip and e.type == "trip_start")
    dumps = []
    async with client(srv, agent_token("c", trip), "trips") as c:
        for tool, args in [("get_trip", {"trip_id": trip}), ("list_events", {"trip_id": trip, "limit": 500}),
                           ("list_incidents", {"trip_id": trip}), ("get_risk_factors", {"trip_id": trip}),
                           ("get_driver_profile", {"driver_id": driver})]:
            data, err = await call(c, tool, **args)
            assert err is None, (tool, err)
            dumps.append(json.dumps(data))
    blob = "\n".join(dumps)
    for s in secrets:
        assert s not in blob


async def test_driver_profile_only_for_own_trip_driver(srv, sim):
    trips = _trips(sim)
    drivers = {str(e.trip_id): e.payload["driver_id"] for e in sim.events if e.type == "trip_start"}
    mine = trips[0]
    stranger = next(d for t, d in drivers.items() if d != drivers[mine])
    async with client(srv, agent_token("c", mine), "trips") as c:
        prof, _ = await call(c, "get_driver_profile", driver_id=drivers[mine])
        assert prof["trips_total"] >= 1 and set(prof) == {"driver_id", "trips_total", "distance_km_total",
                                                          "incidents_by_kind_total"}
        _, err = await call(c, "get_driver_profile", driver_id=stranger)
    assert err["code"] == "FORBIDDEN"


async def test_rate_limit(db, sim):
    srv = build_trips_server(str(db), limiter=RateLimiter(3, 60))
    trip = _trips(sim)[0]
    async with client(srv, agent_token("c", trip), "trips") as c:
        for _ in range(3):
            assert (await call(c, "get_trip", trip_id=trip))[1] is None
        _, err = await call(c, "get_trip", trip_id=trip)
    assert err["code"] == "RATE_LIMITED" and err["retryable"] is True


async def test_crash_is_reported_as_generic_internal(db, sim, monkeypatch):
    from driveguard.store import queries

    def boom(*a, **k):
        raise RuntimeError("SELECT * FROM secret_table at C:\\data\\driveguard.db")

    monkeypatch.setattr(queries, "get_trip", boom)
    srv = build_trips_server(str(db))
    async with client(srv, agent_token("c", _trips(sim)[0]), "trips") as c:
        _, err = await call(c, "get_trip", trip_id=_trips(sim)[0])
    assert err == {"code": "INTERNAL", "message": "internal error", "retryable": True}


async def test_trips_server_database_is_read_only(srv, sim):
    async with client(srv, token("human", HUMAN), "trips") as c:
        await call(c, "get_trip", trip_id=_trips(sim)[0])  # lifespan opened the DB
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await srv.kit.run(lambda conn: conn.execute("DELETE FROM raw_events"))
