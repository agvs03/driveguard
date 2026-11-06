"""Injectable clocks so retry/backoff/visibility logic is testable without real sleeps."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock: sleep() advances time instantly."""

    def __init__(self, start_ms: int = 1_767_600_000_000) -> None:
        self._t = start_ms

    def now_ms(self) -> int:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += int(seconds * 1000)

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0.0))
        await asyncio.sleep(0)
