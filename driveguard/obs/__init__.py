"""Operational telemetry: structured logs, metrics, tracing and SLOs.

Two kinds of record, kept apart on purpose:

  audit trace (agents/trace.py)   the business record of an investigation:
                                  durable, hash-chained, complete, per run
  telemetry (this package)        operational signals: JSON log lines,
                                  counters/histograms, timing spans; lossy is
                                  fine, never contains payloads

They join on one key: an investigation's trace_id IS its run_id (a uuid4's
32 hex digits), so a slow span in the logs leads straight to the audit trace.

Dependency-free by choice (DECISIONS D-45). The wire formats are the standard
ones (W3C `traceparent`, Prometheus text exposition, one JSON object per log
line), so swapping in OpenTelemetry later changes the exporter, not the call
sites.
"""

from driveguard.obs.context import bind, current
from driveguard.obs.logging import configure_logging, log_event
from driveguard.obs.metrics import REGISTRY
from driveguard.obs.tracing import span

__all__ = ["REGISTRY", "bind", "configure_logging", "current", "log_event", "span"]
