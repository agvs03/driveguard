"""Fault-injection tests: the pipeline's stored state must not depend on
duplicates, ordering, lateness, transient failures or crashes.

Invariant under test:
    snapshot(faulty run) == snapshot(clean run over the events that *can* survive)
where "can survive" excludes events whose every copy was corrupted or whose
processing fails permanently / beyond the retry budget (those must be in the DLQ).
"""

import asyncio
import json
import random
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from driveguard.clock import FakeClock
from driveguard.pipeline.consumer import ConsumerConfig
from driveguard.sim.faults import FaultProfile, plan_faults
from driveguard.sim.generator import SimConfig, simulate
from tests.conftest import clean_snapshot, open_pipeline

import pytest

pytestmark = pytest.mark.slow

NO_FAULTS = FaultProfile(duplicate_p=0, reorder_p=0, late_p=0, corrupt_p=0, transient_p=0)


async def test_clean_ingest_completes_all_trips(tmp_path, small_sim):
    p = await open_pipeline(tmp_path, "clean")
    await p.ingest([[e.to_json() for e in small_sim.events]])
    stats = await p.store.stats()
    assert stats["events"] == len(small_sim.events)
    assert stats["trips"] == {"complete": 4}
    assert p.consumer.stats.dead == {} and p.consumer.stats.retries == 0
    await p.close()


async def test_duplicates_are_idempotent(tmp_path, small_sim):
    expected = await clean_snapshot(tmp_path, "clean", small_sim.events)
    plan = plan_faults(small_sim.events, FaultProfile(seed=1, duplicate_p=0.5, reorder_p=0, late_p=0,
                                                      corrupt_p=0, transient_p=0))
    p = await open_pipeline(tmp_path, "dups")
    await p.ingest(plan.batches)
    assert await p.store.snapshot() == expected
    n_copies = sum(len(b) for b in plan.batches)
    assert p.consumer.stats.duplicates == n_copies - len(small_sim.events) > 0
    await p.close()


async def test_full_shuffle_gives_same_state(tmp_path, small_sim):
    expected = await clean_snapshot(tmp_path, "clean", small_sim.events)
    bodies = [e.to_json() for e in small_sim.events]
    random.Random(4).shuffle(bodies)  # far worse than any real reordering
    p = await open_pipeline(tmp_path, "shuffled")
    await p.ingest([bodies])
    assert await p.store.snapshot() == expected
    await p.close()


async def test_late_events_after_sweep_trigger_redetection(tmp_path, small_sim):
    """Withhold the evidence of real incidents, let the sweeper finalize the trips
    as incomplete (incidents missed), then deliver the withheld events late."""
    expected = await clean_snapshot(tmp_path, "clean", small_sim.events)
    withheld = {eid for lab in small_sim.labels if lab.is_incident for eid in lab.evidence_event_ids}
    assert withheld
    on_time = [e.to_json() for e in small_sim.events if str(e.event_id) not in withheld]
    late = [e.to_json() for e in small_sim.events if str(e.event_id) in withheld]

    p = await open_pipeline(tmp_path, "late")
    await p.ingest([on_time])
    mid = await p.store.stats()
    assert set(mid["trips"]) == {"incomplete"}
    expected_incidents = {(r[1], r[2], r[3]) for r in expected["incidents"]}
    mid_incidents = {(r[1], r[2], r[3]) for r in (await p.store.snapshot())["incidents"]}
    assert expected_incidents - mid_incidents, "withholding evidence should hide some incidents"

    await p.ingest([late])
    assert p.consumer.stats.late == len(late)
    assert await p.store.snapshot() == expected
    await p.close()


async def test_late_events_redetect_even_if_trip_never_completes(tmp_path, small_sim):
    """Same as above, but every trip also permanently loses one unrelated GPS
    event, so trips stay 'incomplete' after the late data arrives. Late evidence
    must still be picked up (regression: mutation M1 in DECISIONS.md survived
    the previous test because the late batch completed the trips)."""
    withheld = {eid for lab in small_sim.labels if lab.is_incident for eid in lab.evidence_event_ids}
    lost: set[str] = set()
    for trip in {e.trip_id for e in small_sim.events}:
        lost.add(next(str(e.event_id) for e in small_sim.events if e.trip_id == trip and e.type == "gps"
                      and str(e.event_id) not in withheld))
    survivors = [e for e in small_sim.events if str(e.event_id) not in lost]
    expected = await clean_snapshot(tmp_path, "clean", survivors)
    assert {r[7] for r in expected["trips"]} == {"incomplete"}

    p = await open_pipeline(tmp_path, "late2")
    await p.ingest([[e.to_json() for e in survivors if str(e.event_id) not in withheld],
                    [e.to_json() for e in survivors if str(e.event_id) in withheld]])
    assert await p.store.snapshot() == expected
    await p.close()


async def test_transient_failures_are_retried_with_backoff(tmp_path, small_sim):
    expected = await clean_snapshot(tmp_path, "clean", small_sim.events)
    plan = plan_faults(small_sim.events, FaultProfile(seed=2, duplicate_p=0, reorder_p=0, late_p=0,
                                                      corrupt_p=0, transient_p=0.2))
    p = await open_pipeline(tmp_path, "transient", hook=plan.hook())
    t0 = p.clock.now_ms()
    await p.ingest(plan.batches)
    assert await p.store.snapshot() == expected
    assert p.consumer.stats.retries == sum(plan.transient.values()) > 0
    assert p.consumer.stats.dead == {}
    assert p.clock.now_ms() > t0  # the consumer actually waited for backoff on the (fake) clock
    await p.close()


async def test_poison_and_exhausted_messages_go_to_dlq_then_replay(tmp_path, small_sim):
    plan = plan_faults(small_sim.events, FaultProfile(seed=3, duplicate_p=0, reorder_p=0, late_p=0, corrupt_p=0,
                                                      transient_p=0, exhausting_p=0.01, permanent_p=0.01))
    assert plan.permanent and any(n >= 5 for n in plan.transient.values())
    hook = plan.hook()
    p = await open_pipeline(tmp_path, "dlq", hook=hook)
    await p.ingest(plan.batches)

    survivors = [e for e in small_sim.events if str(e.event_id) in plan.expected_event_ids(5)]
    assert await p.store.snapshot() == await clean_snapshot(tmp_path, "subset", survivors)
    dead = await p.broker.list_dead()
    assert {d.reason for d in dead} == {"permanent", "retries_exhausted"}
    assert {json.loads(d.body)["event_id"] for d in dead} == plan.permanent | {
        e for e, n in plan.transient.items() if n >= 5}
    assert all(d.attempts == 5 for d in dead if d.reason == "retries_exhausted")
    assert all(d.attempts == 1 for d in dead if d.reason == "permanent")

    hook.enabled = False  # the "outage" is fixed; operator replays the DLQ
    assert await p.broker.replay_dead() == len(dead)
    await p.drain()
    assert await p.store.snapshot() == await clean_snapshot(tmp_path, "full", small_sim.events)
    await p.close()


async def test_corrupted_messages_dead_letter_without_retry(tmp_path, small_sim):
    plan = plan_faults(small_sim.events, FaultProfile(seed=4, duplicate_p=0, reorder_p=0, late_p=0,
                                                      corrupt_p=0.05, transient_p=0))
    p = await open_pipeline(tmp_path, "corrupt")
    await p.ingest(plan.batches)
    assert p.consumer.stats.dead == {"schema": len(plan.corrupted_only)} and plan.corrupted_only
    assert p.consumer.stats.retries == 0
    survivors = [e for e in small_sim.events if str(e.event_id) in plan.expected_event_ids(5)]
    assert await p.store.snapshot() == await clean_snapshot(tmp_path, "subset", survivors)
    await p.close()


async def test_conflicting_duplicates_are_rejected(tmp_path, small_sim):
    ev = small_sim.events[5]
    tampered_payload = dict(ev.payload)
    key = next(iter(tampered_payload))
    tampered_payload[key] = tampered_payload[key] + 1 if isinstance(tampered_payload[key], (int, float)) else "x"
    same_id = ev.model_copy(update={"payload": tampered_payload}).to_json()
    same_seq = json.loads(ev.to_json())
    same_seq["event_id"] = "00000000-0000-4000-8000-000000000001"

    p = await open_pipeline(tmp_path, "conflict")
    await p.ingest([[e.to_json() for e in small_sim.events] + [same_id, json.dumps(same_seq)]])
    assert p.consumer.stats.dead == {"conflict": 2}
    assert await p.store.snapshot() == await clean_snapshot(tmp_path, "clean", small_sim.events)
    await p.close()


async def test_crash_mid_batch_is_recovered_by_redelivery(tmp_path, small_sim):
    """A worker claims a batch, applies half of it, and the process dies before acking."""
    expected = await clean_snapshot(tmp_path, "clean", small_sim.events)
    clock = FakeClock()
    p = await open_pipeline(tmp_path, "crash", clock=clock)
    await p.broker.publish_many(p.consumer.cfg.topic, [(None, e.to_json()) for e in small_sim.events])
    claimed = await p.broker.claim(p.consumer.cfg.topic, 100, visibility_timeout_s=30)
    from driveguard.events import parse_event
    for m in claimed[:50]:
        await p.store.apply_event(parse_event(m.body), clock.now_ms())  # applied, never acked
    await p.close()  # crash

    clock.advance(31)
    p2 = await open_pipeline(tmp_path, "crash", clock=clock)
    await p2.drain()
    assert await p2.store.snapshot() == expected
    assert p2.consumer.stats.duplicates == 50
    await p2.close()


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    sim_seed=st.integers(0, 10_000),
    fault_seed=st.integers(0, 10_000),
    dup=st.floats(0, 0.4), reorder=st.floats(0, 0.5), late=st.floats(0, 0.1),
    corrupt=st.floats(0, 0.05), transient=st.floats(0, 0.2),
    exhausting=st.floats(0, 0.01), permanent=st.floats(0, 0.01),
)
def test_property_faulty_equals_clean(sim_seed, fault_seed, dup, reorder, late, corrupt, transient,
                                      exhausting, permanent):
    sim = simulate(SimConfig(seed=sim_seed, n_trips=2, n_drivers=2, trip_seconds=(120, 200),
                             incident_rate=1.0, negative_rate=0.5))
    profile = FaultProfile(seed=fault_seed, duplicate_p=dup, reorder_p=reorder, late_p=late, corrupt_p=corrupt,
                           transient_p=transient, exhausting_p=exhausting, permanent_p=permanent)
    plan = plan_faults(sim.events, profile)
    survivors = [e for e in sim.events if str(e.event_id) in plan.expected_event_ids(5)]

    async def run():
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            p = await open_pipeline(dp, "faulty", hook=plan.hook(), cfg=ConsumerConfig(jitter_seed=fault_seed))
            await p.ingest(plan.batches)
            got = await p.store.snapshot()
            await p.close()
            return got, await clean_snapshot(dp, "clean", survivors)

    got, want = asyncio.run(run())
    assert got == want
