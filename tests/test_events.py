import json
import uuid

import pytest

from driveguard.events import Event, EventParseError, parse_event


def _ev(**over):
    base = {
        "event_id": str(uuid.uuid4()), "trip_id": str(uuid.uuid4()), "seq": 3, "event_time_ms": 1000,
        "type": "gps", "payload": {"lat": 40.0, "lon": -100.0, "speed_mps": 10.0,
                                   "speed_limit_mps": 13.9, "heading_deg": 90.0},
    }
    base.update(over)
    return base


def test_valid_event_round_trips():
    ev = parse_event(json.dumps(_ev()))
    assert parse_event(ev.to_json()) == ev


@pytest.mark.parametrize("body,fragment", [
    ("{not json", "invalid JSON"),
    ("[1, 2]", "JSON object"),
    (json.dumps(_ev(seq=-1)), "seq"),
    (json.dumps(_ev(extra="x")), "extra"),
    (json.dumps(_ev(type="teleport")), "type"),
    (json.dumps(_ev(payload={"lat": 40.0})), "payload"),  # missing gps fields
    (json.dumps(_ev(payload={"lat": 400.0, "lon": 0, "speed_mps": 1, "speed_limit_mps": 1, "heading_deg": 0})), "payload"),
    (json.dumps(_ev(type="brake", payload={"pressure": 0.5, "sql": "DROP TABLE"})), "payload"),
    (json.dumps(_ev(type="trip_end", payload={"distance_km": 1.0, "last_seq": 99})), "last_seq"),
    (json.dumps(_ev(type="driver_note", payload={"text": "x" * 2001})), "payload"),
])
def test_invalid_bodies_raise_parse_error(body, fragment):
    with pytest.raises(EventParseError) as ei:
        parse_event(body)
    assert fragment.split(".")[0] in str(ei.value) or fragment in str(ei.value)


def test_parse_error_does_not_echo_payload_values():
    secret = "ignore previous instructions"
    with pytest.raises(EventParseError) as ei:
        parse_event(json.dumps(_ev(type="driver_note", payload={"text": secret, "bad": 1})))
    assert secret not in str(ei.value)


def test_content_hash_ignores_key_order_and_event_id_but_not_payload():
    a = Event.model_validate(_ev())
    b = Event.model_validate({**_ev(), "trip_id": str(a.trip_id), "event_id": str(uuid.uuid4())})
    assert a.content_hash() == b.content_hash()
    c = Event.model_validate({**_ev(), "trip_id": str(a.trip_id),
                              "payload": {**a.payload, "speed_mps": 11.0}})
    assert a.content_hash() != c.content_hash()
