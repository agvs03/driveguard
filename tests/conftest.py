from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from driveguard.clock import FakeClock
from driveguard.events import Event
from driveguard.pipeline.broker import SqliteBroker
from driveguard.pipeline.consumer import Consumer, ConsumerConfig, FaultHook
from driveguard.pipeline.runner import ingest_batches
from driveguard.sim.generator import SimConfig, SimOutput, simulate
from driveguard.store.repository import Store

SMALL = SimConfig(seed=11, n_trips=4, n_drivers=3, trip_seconds=(200, 300), incident_rate=1.0, negative_rate=0.6)


@dataclass
class Pipeline:
    store: Store
    broker: SqliteBroker
    consumer: Consumer
    clock: FakeClock

    async def ingest(self, batches: list[list[str]]) -> None:
        assert batches, "pass [[...]]; an empty list of batches does nothing (use drain())"
        await ingest_batches(batches, broker=self.broker, consumer=self.consumer, store=self.store, clock=self.clock)

    async def drain(self) -> None:
        """Process whatever is already queued (e.g. replayed DLQ or redelivered messages), then sweep."""
        await self.ingest([[]])

    async def close(self) -> None:
        await self.broker.close()
        await self.store.close()


async def open_pipeline(dirpath: Path, name: str, hook: FaultHook | None = None,
                        clock: FakeClock | None = None, cfg: ConsumerConfig | None = None) -> Pipeline:
    clock = clock or FakeClock()
    store = await Store(dirpath / f"{name}.db").connect()
    broker = await SqliteBroker(dirpath / f"{name}.queue.db", clock).connect()
    consumer = Consumer(broker, store, clock, cfg or ConsumerConfig(jitter_seed=0), fault_hook=hook)
    return Pipeline(store, broker, consumer, clock)


async def clean_snapshot(dirpath: Path, name: str, events: list[Event]) -> dict:
    p = await open_pipeline(dirpath, name)
    try:
        await p.ingest([[e.to_json() for e in events]])
        return await p.store.snapshot()
    finally:
        await p.close()


@pytest.fixture(scope="session")
def small_sim() -> SimOutput:
    return simulate(SMALL)


@pytest.fixture(scope="session")
def golden(tmp_path_factory):
    """(db_path, sim): a fully ingested database shared read-only across MCP tests."""
    from tests.mcp_util import build_golden_db

    path = tmp_path_factory.mktemp("golden") / "golden.db"
    sim = build_golden_db(path)
    return path, sim


@pytest.fixture
def db(golden, tmp_path) -> Path:
    """A private, writable copy of the golden database for one test."""
    from tests.mcp_util import copy_db

    return copy_db(golden[0], tmp_path / "case.db")


@pytest.fixture
def sim(golden):
    return golden[1]
