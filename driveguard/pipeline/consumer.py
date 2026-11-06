"""Asyncio consumer: claim -> validate -> apply (idempotent) -> ack | retry | DLQ.

Failure policy
  * unparseable / schema-invalid body  -> DLQ "schema"          (never retried)
  * same id, different content         -> DLQ "conflict"        (never retried)
  * PermanentError                     -> DLQ "permanent"
  * any other Exception                -> retry with exponential backoff + full
                                          jitter; after max_attempts -> DLQ
                                          "retries_exhausted"
  * worker dies mid-message            -> lease expires, message redelivered;
                                          idempotent apply makes that safe
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from driveguard.clock import Clock
from driveguard.events import Event, EventParseError, parse_event
from driveguard.obs.logging import log_event
from driveguard.obs.metrics import INGEST_DEAD, INGEST_LATENCY, INGEST_MESSAGES
from driveguard.obs.tracing import span
from driveguard.pipeline.broker import Broker, Message
from driveguard.pipeline.errors import PermanentError
from driveguard.store.repository import Outcome, Store

log = logging.getLogger("driveguard.consumer")

TOPIC = "telematics.events"
FaultHook = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True)
class ConsumerConfig:
    topic: str = TOPIC
    max_attempts: int = 5
    base_backoff_s: float = 0.5
    max_backoff_s: float = 30.0
    batch_size: int = 64
    concurrency: int = 8
    visibility_timeout_s: float = 30.0
    jitter_seed: int | None = None  # set in tests for reproducible backoff


@dataclass
class ConsumerStats:
    processed: int = 0
    inserted: int = 0
    duplicates: int = 0
    late: int = 0
    retries: int = 0
    dead: dict[str, int] = field(default_factory=dict)
    stale_leases: int = 0

    def as_dict(self) -> dict[str, object]:
        return {**self.__dict__, "dead": dict(self.dead)}


class Consumer:
    def __init__(self, broker: Broker, store: Store, clock: Clock,
                 config: ConsumerConfig = ConsumerConfig(), fault_hook: FaultHook | None = None) -> None:
        self.broker, self.store, self.clock, self.cfg = broker, store, clock, config
        self.fault_hook = fault_hook
        self.stats = ConsumerStats()
        self._rng = random.Random(config.jitter_seed)
        self._sem = asyncio.Semaphore(config.concurrency)

    def backoff_s(self, attempts: int) -> float:
        """Full jitter: uniform(0, min(cap, base * 2^(attempt-1)))."""
        ceiling = min(self.cfg.max_backoff_s, self.cfg.base_backoff_s * (2 ** (attempts - 1)))
        return self._rng.uniform(0, ceiling)

    async def _dead(self, msg: Message, reason: str, error: str) -> None:
        if await self.broker.dead_letter(msg, reason, error):
            self.stats.dead[reason] = self.stats.dead.get(reason, 0) + 1
            INGEST_MESSAGES.inc(outcome="dead")
            INGEST_DEAD.inc(reason=reason)
            # The error text never echoes event values (events.parse_event guarantees it for schema errors).
            log_event(log, "dead_lettered", logging.WARNING, msg_id=msg.msg_id, reason=reason,
                      attempts=msg.attempts, error=error[:200])
        else:
            self.stats.stale_leases += 1
            INGEST_MESSAGES.inc(outcome="stale_lease")

    async def handle(self, msg: Message) -> None:
        async with self._sem:
            self.stats.processed += 1
            try:
                event = parse_event(msg.body)
            except EventParseError as e:
                await self._dead(msg, "schema", str(e))
                return
            try:
                if self.fault_hook is not None:
                    await self.fault_hook(event)
                result = await self.store.apply_event(event, self.clock.now_ms())
            except PermanentError as e:
                await self._dead(msg, "permanent", f"{type(e).__name__}: {e}")
                return
            except Exception as e:  # noqa: BLE001 - every other failure is treated as retryable
                err = f"{type(e).__name__}: {e}"
                if msg.attempts >= self.cfg.max_attempts:
                    await self._dead(msg, "retries_exhausted", err)
                else:
                    self.stats.retries += 1
                    INGEST_MESSAGES.inc(outcome="retry")
                    delay = self.backoff_s(msg.attempts)
                    log_event(log, "retry_scheduled", logging.INFO, msg_id=msg.msg_id, attempts=msg.attempts,
                              delay_s=round(delay, 3), error=err[:200])
                    if not await self.broker.nack(msg, delay, err):
                        self.stats.stale_leases += 1
                return

            if result.outcome is Outcome.CONFLICT:
                await self._dead(msg, "conflict", result.reason)
                return
            if result.outcome is Outcome.DUPLICATE:
                self.stats.duplicates += 1
                INGEST_MESSAGES.inc(outcome="duplicate")
            else:
                self.stats.inserted += 1
                self.stats.late += int(result.late)
                INGEST_MESSAGES.inc(outcome="late" if result.late else "inserted")
            # Durable now (the apply committed), so this is the publish-to-durable latency, retries included.
            INGEST_LATENCY.observe(max(0, self.clock.now_ms() - msg.enqueued_at_ms) / 1000)
            if not await self.broker.ack(msg):
                # Lease expired and someone else re-claimed it; their apply will be a no-op.
                self.stats.stale_leases += 1

    async def process_batch(self) -> int:
        msgs = await self.broker.claim(self.cfg.topic, self.cfg.batch_size, self.cfg.visibility_timeout_s)
        if msgs:
            with span("ingest_batch", component="consumer", messages=len(msgs)):
                await asyncio.gather(*(self.handle(m) for m in msgs))
        return len(msgs)

    async def run_until_idle(self, max_batches: int = 1_000_000) -> None:
        """Drain the topic, sleeping (on the injected clock) until retries become visible."""
        for _ in range(max_batches):
            if await self.process_batch():
                continue
            nxt = await self.broker.next_visible_at_ms(self.cfg.topic)
            if nxt is None:
                return
            await self.clock.sleep(max(0, nxt - self.clock.now_ms()) / 1000 + 0.001)
        raise RuntimeError("run_until_idle exceeded max_batches")

    async def run_forever(self, stop: asyncio.Event, idle_poll_s: float = 0.5) -> None:
        while not stop.is_set():
            if not await self.process_batch():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_poll_s)
                except asyncio.TimeoutError:
                    pass
