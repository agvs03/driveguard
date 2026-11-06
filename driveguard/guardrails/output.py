"""Output guard: the last check before a report can reach a human as "valid".

  G_POLICY_MISMATCH   the report's classification contradicts the deterministic
                      policy check of the incident window, read straight from
                      the database (independent of what the agents fetched)
  G_SENSITIVE         PII or a secret appears in report text
  G_ECHO              the report reproduces a run of >= 6 words from a driver
                      note that carried injection signals
  G_INJECTION_TEXT    the report itself contains instruction-like text

Any violation stops the report and escalates the case with the reasons, so a
human sees the manipulation attempt instead of a clean-looking report.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from driveguard.agents.policy import classify_by_policy
from driveguard.guardrails.text import DISCLOSURE, injection_signals, scan_sensitive, shared_run

ECHO_MIN_WORDS = 6
WINDOW_BEFORE_MS, WINDOW_AFTER_MS = 5_000, 45_000


@dataclass
class GuardResult:
    ok: bool
    violations: list[dict[str, str]] = field(default_factory=list)
    policy_classification: str | None = None
    disclosures: list[dict[str, str]] = field(default_factory=list)  # injection attempts the report disclosed

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": self.violations, "policy_classification": self.policy_classification,
                "disclosures": self.disclosures}


def _untag(text: str) -> str:
    return text.replace("<untrusted_driver_note>", " ").replace("</untrusted_driver_note>", " ")


def _sentences(report: dict[str, Any]) -> list[str]:
    """Report text split into sentences, with spotlight tags removed: a pasted note carries its
    <untrusted_driver_note> tags, and the word 'untrusted' must not count as a disclosure."""
    import re
    out: list[str] = []
    for part in report_text(report).splitlines():
        out += [x for x in re.split(r"(?<=[.!?])\s+", _untag(part)) if x.strip()]
    return out


def report_text(report: dict[str, Any]) -> str:
    parts = [report.get("summary", "")] + [f["claim"] for f in report.get("findings", [])] + \
            [f["claim"] for f in report.get("contradicting_evidence", [])] + list(report.get("limitations", []))
    return "\n".join(parts)


def incident_window_events(conn: sqlite3.Connection, incident_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import json
    inc = conn.execute("SELECT trip_id, kind, t_start_ms, t_end_ms FROM detected_incidents WHERE incident_id = ?",
                       (incident_id,)).fetchone()
    inc = {"trip_id": inc[0], "kind": inc[1], "t_start_ms": inc[2], "t_end_ms": inc[3]}
    rows = conn.execute("SELECT event_id, type, event_time_ms, payload_json FROM raw_events WHERE trip_id = ? AND "
                        "event_time_ms BETWEEN ? AND ? ORDER BY seq",
                        (inc["trip_id"], inc["t_start_ms"] - WINDOW_BEFORE_MS, inc["t_end_ms"] + WINDOW_AFTER_MS))
    return inc, [{"event_id": r[0], "type": r[1], "event_time_ms": r[2], "payload": json.loads(r[3])} for r in rows]


def identity_values(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Driver names/phones, read ONLY to detect them in outputs (never returned to agents)."""
    rows = conn.execute("SELECT display_name, phone FROM drivers").fetchall()
    return [r[0] for r in rows if r[0]], [r[1] for r in rows if r[1]]


def check_report(conn: sqlite3.Connection, report: dict[str, Any], incident_id: str,
                 seen_notes: list[dict[str, Any]]) -> GuardResult:
    res = GuardResult(ok=True)
    inc, events = incident_window_events(conn, incident_id)
    policy = classify_by_policy(inc, events)
    res.policy_classification = policy["classification"]
    if policy["classification"] != "inconclusive" and report["classification"] != policy["classification"]:
        res.violations.append({"code": "G_POLICY_MISMATCH", "detail": (
            f"report says {report['classification']} but the policy check of the incident window says "
            f"{policy['classification']} ({policy['rationale']})")})

    text = report_text(report)
    names, phones = identity_values(conn)
    sensitive = scan_sensitive(text, names, phones)
    if sensitive:
        res.violations.append({"code": "G_SENSITIVE", "detail": f"report contains: {', '.join(sensitive)}"})

    for note in seen_notes:
        note_text = _untag(note["payload"].get("text", ""))
        if not (injection_signals(note_text) or note.get("injection_signals")):
            continue
        for sentence in _sentences(report):
            n, _, rest = shared_run(sentence, note_text)
            if n < ECHO_MIN_WORDS:
                continue
            # Real Claude quoted the attack while REPORTING it ("...a request to include the driver's details.
            # It was flagged ... and its request was not followed."): that's the behaviour we want, not an echo.
            # The disclosure must be in the sentence OUTSIDE the copied words, or an attacker could simply
            # put "request" inside the text they want copied (M-24).
            if DISCLOSURE.search(" ".join(rest)):
                res.disclosures.append({"note": note["event_id"], "sentence": sentence[:300]})
                continue
            res.violations.append({"code": "G_ECHO", "detail": f"report copies {n} consecutive words from a "
                                                               f"suspicious driver note ({note['event_id']})"})
            break
    sig = injection_signals(text)
    if set(sig) & {"override", "role_spoof", "persona"}:
        res.violations.append({"code": "G_INJECTION_TEXT", "detail": f"report contains instruction-like text: {sig}"})
    res.ok = not res.violations
    return res
