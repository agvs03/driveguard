"""Fault injection: turns a clean event list into a hostile delivery plan.

Faults injected:
  * duplicates      - the same message delivered more than once
  * reordering      - messages displaced up to `max_displacement` positions
  * late delivery   - a fraction of events held back and delivered at the very end
                      (after the trip has been swept as incomplete)
  * corruption      - a copy made schema-invalid (truncated JSON / bad field)
  * transient fails - consumer raises TransientError for the first n attempts
  * exhausting fails- transient failures that outlast the retry budget
  * permanent fails - consumer raises PermanentError (straight to the DLQ)

`FaultPlan.expected_event_ids()` is the set of events that *should* end up in
the store: every event with at least one intact copy that the consumer can
eventually process.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from driveguard.events import Event
from driveguard.pipeline.errors import PermanentError, TransientError


@dataclass(frozen=True)
class FaultProfile:
    seed: int = 0
    duplicate_p: float = 0.1
    reorder_p: float = 0.1
    max_displacement: int = 200
    late_p: float = 0.02
    corrupt_p: float = 0.01
    transient_p: float = 0.05
    exhausting_p: float = 0.0
    permanent_p: float = 0.0


@dataclass
class FaultPlan:
    """`batches` are delivered in order; the late batch comes last."""

    batches: list[list[str]]
    all_event_ids: set[str]
    corrupted_only: set[str] = field(default_factory=set)  # every copy corrupted
    transient: dict[str, int] = field(default_factory=dict)  # event_id -> failures before success
    permanent: set[str] = field(default_factory=set)

    def expected_event_ids(self, max_attempts: int) -> set[str]:
        exhausted = {e for e, n in self.transient.items() if n >= max_attempts}
        return self.all_event_ids - self.corrupted_only - self.permanent - exhausted

    def hook(self) -> "FaultHook":
        return FaultHook(self.transient, self.permanent)


class FaultHook:
    """Called by the consumer before applying an event. Raises injected failures."""

    def __init__(self, transient: dict[str, int], permanent: set[str]) -> None:
        self._transient = dict(transient)
        self._permanent = set(permanent)
        self._calls: dict[str, int] = {}
        self.enabled = True

    async def __call__(self, event: Event) -> None:
        if not self.enabled:
            return
        eid = str(event.event_id)
        if eid in self._permanent:
            raise PermanentError(f"injected permanent failure for {eid}")
        n = self._calls.get(eid, 0)
        self._calls[eid] = n + 1
        if n < self._transient.get(eid, 0):
            raise TransientError(f"injected transient failure #{n + 1} for {eid}")


def _corrupt(body: str, rng: random.Random) -> str:
    mode = rng.randrange(3)
    if mode == 0:
        return body[: max(1, len(body) // 2)]  # truncated JSON
    data = json.loads(body)
    if mode == 1:
        data["seq"] = -1  # schema-invalid value
    else:
        data.pop("event_time_ms", None)  # missing required field
    return json.dumps(data)


def plan_faults(events: list[Event], profile: FaultProfile, max_attempts: int = 5) -> FaultPlan:
    rng = random.Random(profile.seed)
    copies: list[tuple[str, str]] = []  # (event_id, body)
    intact: dict[str, bool] = {}
    late: list[tuple[str, str]] = []
    for ev in events:
        eid = str(ev.event_id)
        n = 2 if rng.random() < profile.duplicate_p else 1
        for _ in range(n):
            body = ev.to_json()
            if rng.random() < profile.corrupt_p:
                body = _corrupt(body, rng)
                intact.setdefault(eid, False)
            else:
                intact[eid] = True
            if rng.random() < profile.late_p:
                late.append((eid, body))
            else:
                copies.append((eid, body))

    # Bounded reordering: displace selected messages forward/backward.
    for i in range(len(copies)):
        if rng.random() < profile.reorder_p:
            j = min(len(copies) - 1, max(0, i + rng.randint(-profile.max_displacement, profile.max_displacement)))
            copies[i], copies[j] = copies[j], copies[i]

    transient: dict[str, int] = {}
    permanent: set[str] = set()
    for ev in events:
        eid = str(ev.event_id)
        r = rng.random()
        if r < profile.permanent_p:
            permanent.add(eid)
        elif r < profile.permanent_p + profile.exhausting_p:
            transient[eid] = max_attempts + 5
        elif r < profile.permanent_p + profile.exhausting_p + profile.transient_p:
            transient[eid] = rng.randint(1, max_attempts - 1)

    batches = [[b for _, b in copies]]
    if late:
        batches.append([b for _, b in late])
    return FaultPlan(
        batches=batches,
        all_event_ids={str(e.event_id) for e in events},
        corrupted_only={e for e, ok in intact.items() if not ok},
        transient=transient,
        permanent=permanent,
    )
