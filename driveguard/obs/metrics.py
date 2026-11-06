"""In-process metrics: counters and histograms with Prometheus text exposition.

Histograms keep cumulative buckets (what Prometheus scrapes, for
`histogram_quantile`) AND a bounded window of recent raw samples, so the SLO
report can give exact percentiles for a local run instead of a bucket
interpolation.

Label values must come from small closed sets (tool names, agent names,
status codes). Never ids or free text: unbounded label values blow up
cardinality, and ids would leak case structure to anyone who can scrape.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Iterable

LATENCY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 90.0, 120.0, 300.0)
COST_BUCKETS_USD = (0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0)
RECENT_SAMPLES = 10_000

LabelKey = tuple[str, ...]


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(names: Iterable[str], values: Iterable[str], extra: str = "") -> str:
    parts = [f'{n}="{_esc(v)}"' for n, v in zip(names, values)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _num(x: float) -> str:
    f = float(x)
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    return str(int(f)) if f.is_integer() and abs(f) < 1e15 else repr(f)


class _Metric:
    kind = ""

    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.labelnames = name, help, tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> LabelKey:
        if set(labels) != set(self.labelnames):
            raise ValueError(f"{self.name}: expected labels {self.labelnames}, got {sorted(labels)}")
        return tuple(str(labels[n]) for n in self.labelnames)


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._values: dict[LabelKey, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters only go up")
        k = self._key(labels)
        with self._lock:
            self._values[k] = self._values.get(k, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def total(self) -> float:
        return sum(self._values.values())

    def items(self) -> list[tuple[dict[str, str], float]]:
        return [(dict(zip(self.labelnames, k)), v) for k, v in sorted(self._values.items())]

    def render(self) -> list[str]:
        return [f"{self.name}{_fmt_labels(self.labelnames, k)} {_num(v)}" for k, v in sorted(self._values.items())]

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


class _HistState:
    __slots__ = ("buckets", "count", "sum", "recent")

    def __init__(self, n: int) -> None:
        self.buckets = [0] * n
        self.count = 0
        self.sum = 0.0
        self.recent: deque[float] = deque(maxlen=RECENT_SAMPLES)


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = LATENCY_BUCKETS_S) -> None:
        super().__init__(name, help, labelnames)
        self.bounds = tuple(sorted(buckets))
        self._states: dict[LabelKey, _HistState] = {}

    def observe(self, value: float, **labels: str) -> None:
        k = self._key(labels)
        with self._lock:
            st = self._states.get(k)
            if st is None:
                st = self._states[k] = _HistState(len(self.bounds))
            for i, b in enumerate(self.bounds):
                if value <= b:
                    st.buckets[i] += 1
            st.count += 1
            st.sum += value
            st.recent.append(value)

    def samples(self, **match: str) -> list[float]:
        """Recent raw samples across every label set that matches `match` (a subset of labels)."""
        out: list[float] = []
        with self._lock:
            for k, st in self._states.items():
                lab = dict(zip(self.labelnames, k))
                if all(lab.get(m) == v for m, v in match.items()):
                    out.extend(st.recent)
        return out

    def count(self, **match: str) -> int:
        with self._lock:
            return sum(st.count for k, st in self._states.items()
                       if all(dict(zip(self.labelnames, k)).get(m) == v for m, v in match.items()))

    def render(self) -> list[str]:
        lines: list[str] = []
        for k, st in sorted(self._states.items()):
            for b, c in zip(self.bounds, st.buckets):
                le = 'le="' + _num(b) + '"'
                lines.append(f"{self.name}_bucket{_fmt_labels(self.labelnames, k, le)} {c}")
            le_inf = 'le="+Inf"'
            lines.append(f"{self.name}_bucket{_fmt_labels(self.labelnames, k, le_inf)} {st.count}")
            lines.append(f"{self.name}_sum{_fmt_labels(self.labelnames, k)} {_num(round(st.sum, 9))}")
            lines.append(f"{self.name}_count{_fmt_labels(self.labelnames, k)} {st.count}")
        return lines

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _get(self, cls: type, name: str, help: str, labelnames: tuple[str, ...], **kw: object) -> _Metric:
        with self._lock:
            m = self._metrics.get(name)
            if m is None:
                m = self._metrics[name] = cls(name, help, labelnames, **kw)
            elif not isinstance(m, cls) or m.labelnames != tuple(labelnames):
                raise ValueError(f"metric {name} already registered with a different type or labels")
            return m

    def counter(self, name: str, help: str, labelnames: tuple[str, ...] = ()) -> Counter:
        return self._get(Counter, name, help, labelnames)  # type: ignore[return-value]

    def histogram(self, name: str, help: str, labelnames: tuple[str, ...] = (),
                  buckets: tuple[float, ...] = LATENCY_BUCKETS_S) -> Histogram:
        return self._get(Histogram, name, help, labelnames, buckets=buckets)  # type: ignore[return-value]

    def get(self, name: str) -> _Metric | None:
        return self._metrics.get(name)

    def render(self) -> str:
        out: list[str] = []
        for name in sorted(self._metrics):
            m = self._metrics[name]
            out += [f"# HELP {name} {m.help}", f"# TYPE {name} {m.kind}", *m.render()]
        return "\n".join(out) + "\n"

    def reset(self) -> None:
        """Clear values, keep registrations (tests and benchmark runs)."""
        for m in self._metrics.values():
            m.reset()  # type: ignore[attr-defined]


REGISTRY = Registry()

# ---------------------------------------------------------------- catalogue --- #
# Every metric DriveGuard emits is declared here, so the full list is greppable in one place.

INGEST_MESSAGES = REGISTRY.counter(
    "driveguard_ingest_messages_total", "Messages handled by the consumer, by outcome", ("outcome",))
INGEST_LATENCY = REGISTRY.histogram(
    "driveguard_ingest_latency_seconds", "Publish-to-durable latency for stored or duplicate events")
INGEST_DEAD = REGISTRY.counter("driveguard_ingest_dead_letters_total", "Messages dead-lettered, by reason", ("reason",))

MCP_CALLS = REGISTRY.counter("driveguard_mcp_tool_calls_total", "Server-side MCP tool calls",
                             ("server", "tool", "code"))
MCP_LATENCY = REGISTRY.histogram("driveguard_mcp_tool_latency_seconds", "Server-side MCP tool latency",
                                 ("server", "tool"))

GATEWAY_LATENCY = REGISTRY.histogram("driveguard_gateway_tool_latency_seconds",
                                     "Client-observed tool latency at the agent gateway", ("tool",))

LLM_CALLS = REGISTRY.counter("driveguard_llm_calls_total", "Model calls by agent and outcome",
                             ("agent", "model", "outcome"))
LLM_LATENCY = REGISTRY.histogram("driveguard_llm_latency_seconds", "Model call latency", ("agent",))
LLM_TOKENS = REGISTRY.counter("driveguard_llm_tokens_total", "Tokens by agent and kind", ("agent", "kind"))
LLM_COST = REGISTRY.counter("driveguard_llm_cost_usd_total", "Model spend in USD by agent", ("agent",))

INVESTIGATIONS = REGISTRY.counter("driveguard_investigations_total", "Investigation runs by final status",
                                  ("status",))
INVESTIGATION_LATENCY = REGISTRY.histogram("driveguard_investigation_latency_seconds",
                                           "Investigation wall time (excludes human review)")
INVESTIGATION_COST = REGISTRY.histogram("driveguard_investigation_cost_usd", "Investigation cost in USD",
                                        buckets=COST_BUCKETS_USD)
ESCALATIONS = REGISTRY.counter("driveguard_escalations_total", "Escalations by cause", ("cause",))
GUARD_BLOCKS = REGISTRY.counter("driveguard_guardrail_events_total", "Guardrail interventions", ("guard",))

API_REQUESTS = REGISTRY.counter("driveguard_api_requests_total", "API requests", ("route", "status"))
API_LATENCY = REGISTRY.histogram("driveguard_api_latency_seconds", "API request latency", ("route",))
