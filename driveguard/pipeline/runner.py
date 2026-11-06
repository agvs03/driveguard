"""Glue for running the pipeline end to end (used by the CLI and tests)."""

from __future__ import annotations

from driveguard.clock import Clock
from driveguard.pipeline.broker import SqliteBroker
from driveguard.pipeline.consumer import Consumer
from driveguard.store.repository import Store


async def ingest_batches(
    batches: list[list[str]],
    *,
    broker: SqliteBroker,
    consumer: Consumer,
    store: Store,
    clock: Clock,
    quiet_s: float = 120.0,
) -> list[str]:
    """Publish each batch, drain it, let `quiet_s` pass and sweep quiet trips.

    The pause between batches is what makes the late batch genuinely late: by
    the time it arrives, the trips it belongs to have already been swept and
    finalized as incomplete. Returns the trip_ids swept (in order, with repeats).
    """
    swept: list[str] = []
    for batch in batches:
        await broker.publish_many(consumer.cfg.topic, [(None, body) for body in batch])
        await consumer.run_until_idle()
        await clock.sleep(quiet_s)
        swept += await store.sweep(clock.now_ms(), int(quiet_s * 1000))
    return swept
