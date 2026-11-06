"""Telematics event envelope and per-type payload schemas.

Every event on the wire is JSON matching `Event`. `event_id` is the idempotency
key; `(trip_id, seq)` is a second uniqueness constraint (seq is per-trip,
contiguous from 0, and the `trip_end` event carries `last_seq == seq`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1
G = 9.81  # m/s^2 per g

EventType = Literal["trip_start", "gps", "accel", "brake", "phone", "driver_note", "trip_end"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripStart(_Payload):
    driver_id: str = Field(pattern=r"^drv_[0-9a-f]{8}$")
    vehicle_id: str = Field(pattern=r"^veh_[0-9a-f]{8}$")


class Gps(_Payload):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_mps: float = Field(ge=0, le=90)
    speed_limit_mps: float = Field(gt=0, le=60)
    heading_deg: float = Field(ge=0, lt=360)


class Accel(_Payload):
    """One-second accelerometer summary in g. ax: longitudinal (+ forward),
    ay: lateral, az: vertical including gravity (~1.0 at rest)."""

    ax_mean: float = Field(ge=-20, le=20)
    ax_peak: float = Field(ge=-20, le=20)
    ay_peak: float = Field(ge=-20, le=20)
    az_peak: float = Field(ge=-20, le=20)


class Brake(_Payload):
    pressure: float = Field(ge=0, le=1)


class Phone(_Payload):
    action: Literal["unlock", "screen_on", "screen_off", "call_start", "call_end"]


class DriverNote(_Payload):
    # Untrusted free text. Bounded here; treated as data downstream (Phase 5).
    text: str = Field(min_length=1, max_length=2000)


class TripEnd(_Payload):
    distance_km: float = Field(ge=0, le=5000)
    last_seq: int = Field(ge=0)


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "trip_start": TripStart,
    "gps": Gps,
    "accel": Accel,
    "brake": Brake,
    "phone": Phone,
    "driver_note": DriverNote,
    "trip_end": TripEnd,
}


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    trip_id: UUID
    seq: int = Field(ge=0, le=1_000_000)
    event_time_ms: int = Field(ge=0)
    type: EventType
    payload: dict[str, Any]
    schema_version: Literal[1] = SCHEMA_VERSION

    @model_validator(mode="after")
    def _validate_payload(self) -> "Event":
        model = PAYLOAD_MODELS[self.type]
        try:
            self.payload = model.model_validate(self.payload).model_dump()
        except ValidationError as e:
            # Report field names and error types only, never input values (they may be untrusted text).
            where = sorted({f"{'.'.join(str(p) for p in err['loc']) or '<root>'}({err['type']})" for err in e.errors()})
            raise ValueError(f"payload invalid for type {self.type}: {', '.join(where)}") from None
        if self.type == "trip_end" and self.payload["last_seq"] != self.seq:
            raise ValueError("trip_end.last_seq must equal the event's seq")
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def content_hash(self) -> str:
        """Hash of everything except event_id: detects 'same id, different content'."""
        body = {
            "trip_id": str(self.trip_id),
            "seq": self.seq,
            "event_time_ms": self.event_time_ms,
            "type": self.type,
            "payload": self.payload,
            "schema_version": self.schema_version,
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class EventParseError(ValueError):
    """Raised for bodies that can never become a valid Event (non-retryable)."""


def parse_event(body: str | bytes) -> Event:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise EventParseError(f"invalid JSON: {e.msg if hasattr(e, 'msg') else e}") from e
    if not isinstance(data, dict):
        raise EventParseError("event must be a JSON object")
    try:
        return Event.model_validate(data)
    except ValidationError as e:
        # Keep the message short and free of input values: field paths, plus our own
        # validator messages (which are built from field names only).
        parts = sorted({
            ".".join(str(p) for p in err["loc"]) if err["loc"] else str(err["msg"])
            for err in e.errors()
        })
        raise EventParseError(f"schema violation: {'; '.join(parts)}"[:400]) from e
