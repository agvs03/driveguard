"""Hard limits on steps, tool calls, money and wall time, enforced in code.

Limits are checked BEFORE each model call using a worst-case estimate (the
input so far plus `max_tokens` of output), so a single call cannot overshoot
the dollar cap. Exceeding any limit raises BudgetExceeded, which the
orchestrator turns into an ESCALATED case. It is never silently truncated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Price:
    input: float  # $ per 1M tokens
    output: float
    cache_read: float
    cache_write: float


# Anthropic first-party prices from the claude-api skill's cached model table
# (2026-10-06). Cache writes assumed 1.25x input (5-minute TTL). "scripted" is free.
DEFAULT_PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
    "scripted": Price(0.0, 0.0, 0.0, 0.0),
}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def cost_of(price: Price, u: Usage) -> float:
    return (u.input_tokens * price.input + u.output_tokens * price.output
            + u.cache_read_input_tokens * price.cache_read
            + u.cache_creation_input_tokens * price.cache_write) / 1_000_000


@dataclass(frozen=True)
class Limits:
    max_model_calls_per_agent: int = 8
    max_tool_calls_per_agent: int = 12
    max_model_calls_total: int = 24
    max_tool_calls_total: int = 30
    max_cost_usd: float = 0.50
    max_wall_s: float = 120.0


@dataclass
class Budget:
    limits: Limits = field(default_factory=Limits)
    prices: dict[str, Price] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    clock: Callable[[], float] = time.monotonic
    spent_usd: float = 0.0
    model_calls: dict[str, int] = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._t0 = self.clock()

    def price(self, model: str) -> Price:
        key = "scripted" if model.startswith("scripted") else model
        if key not in self.prices:
            raise BudgetExceeded(f"no price known for model {model!r}; refusing to run unpriced")
        return self.prices[key]

    @property
    def total_model_calls(self) -> int:
        return sum(self.model_calls.values())

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_calls.values())

    def _check_time(self) -> None:
        if self.clock() - self._t0 > self.limits.max_wall_s:
            raise BudgetExceeded(f"wall-clock limit of {self.limits.max_wall_s:g}s exceeded")

    def before_model_call(self, agent: str, model: str, est_input_tokens: int, max_tokens: int) -> None:
        self._check_time()
        if self.model_calls.get(agent, 0) >= self.limits.max_model_calls_per_agent:
            raise BudgetExceeded(f"{agent}: model-call limit of {self.limits.max_model_calls_per_agent} reached")
        if self.total_model_calls >= self.limits.max_model_calls_total:
            raise BudgetExceeded(f"case model-call limit of {self.limits.max_model_calls_total} reached")
        worst = cost_of(self.price(model), Usage(input_tokens=est_input_tokens, output_tokens=max_tokens))
        if self.spent_usd + worst > self.limits.max_cost_usd:
            raise BudgetExceeded(f"cost cap ${self.limits.max_cost_usd:.2f}: spent ${self.spent_usd:.4f}, next call "
                                 f"could cost up to ${worst:.4f}")
        self.model_calls[agent] = self.model_calls.get(agent, 0) + 1

    def after_model_call(self, model: str, usage: Usage) -> float:
        c = cost_of(self.price(model), usage)
        self.spent_usd += c
        return c

    def before_tool_call(self, agent: str) -> None:
        self._check_time()
        if self.tool_calls.get(agent, 0) >= self.limits.max_tool_calls_per_agent:
            raise BudgetExceeded(f"{agent}: tool-call limit of {self.limits.max_tool_calls_per_agent} reached")
        if self.total_tool_calls >= self.limits.max_tool_calls_total:
            raise BudgetExceeded(f"case tool-call limit of {self.limits.max_tool_calls_total} reached")
        self.tool_calls[agent] = self.tool_calls.get(agent, 0) + 1
