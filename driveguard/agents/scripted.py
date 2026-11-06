"""ScriptedClient: a deterministic, offline stand-in for Claude.

It drives the *same* loop, tools, trace and validator as ClaudeClient, so the
whole system is testable without an API key. Two roles:

1. Baseline analyst: a transparent rule-based policy (the POLICY text the LLM
   is given, implemented literally). In Phase 4 it is the "no-LLM" baseline the
   real model is compared against.
2. Fault injector: `faults` makes it misbehave on purpose so the guards can be
   tested: hallucinated citations, wrong numbers, foreign-trip access,
   disallowed tools, endless tool loops, malformed output, expensive calls.

It derives all state from the message history (it is a pure function of
`messages`), exactly like a stateless API.
"""

from __future__ import annotations

import json
import re
import math
import uuid
from typing import Any

from driveguard.agents.budget import Usage
from driveguard.agents.llm import LLMResponse, ToolCall
from driveguard.agents.citations import MPH_PER_MPS
from driveguard.agents.policy import classify_by_policy, resultant as _resultant

FAULTS = {"hallucinate_citation", "wrong_number", "foreign_trip", "disallowed_tool", "loop", "bad_json",
          "expensive", "writer_bad_json_once", "hallucinate_always", "offlist_tool", "obey_injection"}


def _history(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], Any, bool]]:
    """[(tool_name, input, parsed_result, is_error)] in order."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    out = []
    for m in messages:
        if not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            b = b if isinstance(b, dict) else b.model_dump()
            if b.get("type") == "tool_use":
                calls[b["id"]] = (b["name"], b["input"])
            elif b.get("type") == "tool_result":
                name, inp = calls[b["tool_use_id"]]
                try:
                    data = json.loads(b["content"])
                except (TypeError, ValueError):
                    data = b["content"]
                out.append((name, inp, data, bool(b.get("is_error"))))
    return out


class ScriptedClient:
    name = "scripted"

    def __init__(self, faults: set[str] | None = None, *, foreign_trip_id: str | None = None,
                 model: str = "scripted-baseline-v1") -> None:
        unknown = (faults or set()) - FAULTS
        if unknown:
            raise ValueError(f"unknown faults {unknown}")
        self.faults = set(faults or ())
        self.foreign_trip_id = foreign_trip_id
        self.model = model
        self._n = 0

    # -------------------------------------------------------------- API --- #
    async def create(self, *, agent: str, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], output_schema: dict[str, Any], effort: str,
                     max_tokens: int) -> LLMResponse:
        task = json.loads(messages[0]["content"])
        hist = _history(messages)
        last_user = messages[-1]["content"]
        repair = isinstance(last_user, str) and "did not satisfy" in last_user
        action = {"triage": self._triage, "evidence": self._evidence, "report_writer": self._writer}[agent](
            task, hist, repair)
        blocks: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        text = ""
        if isinstance(action, list):  # tool calls
            for name, inp in action:
                self._n += 1
                tid = f"toolu_scripted_{self._n:05d}"
                blocks.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
                calls.append(ToolCall(tid, name, inp))
            stop = "tool_use"
        else:
            text = action if isinstance(action, str) else json.dumps(action)
            blocks.append({"type": "text", "text": text})
            stop = "end_turn"
        # ~2 chars/token, close to what real Claude showed on these prompts (M-25). It was chars/4, which
        # hid the fact that the budget's input estimate was too low for real traffic.
        inp_tok = len(json.dumps([system, tools, messages], default=str)) // 2
        out_tok = len(json.dumps(blocks)) // 2
        if "expensive" in self.faults:
            out_tok = max_tokens  # worst realistic case: output is bounded by max_tokens
        return LLMResponse(model=self.model, stop_reason=stop, text=text, tool_calls=calls, raw_content=blocks,
                           usage=Usage(input_tokens=inp_tok, output_tokens=out_tok), trace_content=blocks)

    # ----------------------------------------------------------- triage --- #
    def _triage(self, task: dict[str, Any], hist: list, repair: bool) -> Any:
        inc_id, trip_id = task["incident_id"], task["trip_id"]
        names = [h[0] for h in hist]
        if "loop" in self.faults:
            return [("trips__get_incident", {"incident_id": inc_id})]
        if "foreign_trip" in self.faults and self.foreign_trip_id and not any(
                h[1].get("trip_id") == self.foreign_trip_id for h in hist):
            return [("trips__list_events", {"trip_id": self.foreign_trip_id, "limit": 10})]
        if "offlist_tool" in self.faults and "trips__get_driver_profile" not in names:
            # A real tool on a server triage IS connected to, but not on triage's allowlist.
            return [("trips__get_driver_profile", {"driver_id": "drv_00000000"})]
        if "trips__get_incident" not in names:
            return [("trips__get_incident", {"incident_id": inc_id})]
        inc = next(h[2] for h in hist if h[0] == "trips__get_incident" and not h[3])
        window = [h for h in hist if h[0] == "trips__list_events" and h[1].get("trip_id") == trip_id and not h[3]]
        if not window:
            return [("trips__list_events", {"trip_id": trip_id, "t_from_ms": inc["t_start_ms"] - 5000,
                                            "t_to_ms": inc["t_end_ms"] + 15000,
                                            "types": ["accel", "brake", "phone", "gps"], "limit": 500})]
        return self._classify(inc, window[-1][2]["events"])

    def _classify(self, inc: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        return classify_by_policy(inc, events)

    # --------------------------------------------------------- evidence --- #
    def _evidence(self, task: dict[str, Any], hist: list, repair: bool) -> Any:
        trip_id, triage = task["trip_id"], task["triage"]
        names = [h[0] for h in hist]
        if "risk__score_trip" not in names:
            return [("risk__score_trip", {"trip_id": trip_id})]
        if "trips__get_events" not in names:
            return [("trips__get_events", {"event_ids": triage["cited_event_ids"]})]
        note_calls = [h for h in hist if h[0] == "trips__list_events" and h[1].get("types") == ["driver_note"]]
        if not note_calls:
            return [("trips__list_events", {"trip_id": trip_id, "types": ["driver_note"], "limit": 20})]
        notes = [] if note_calls[0][3] else note_calls[0][2]["events"]
        if "obey_injection" in self.faults:
            actions = self._injected_tool_actions(task, notes, hist)
            if actions:
                return actions
        score = next(h[2] for h in hist if h[0] == "risk__score_trip")
        events = next(h[2] for h in hist if h[0] == "trips__get_events")
        items = []
        for e in events:
            p = e["payload"]
            if e["type"] == "accel":
                obs = (f"ax_peak {p['ax_peak']} g, ay_peak {p['ay_peak']} g, az_peak {p['az_peak']} g "
                       f"(resultant {_resultant(p):.3f} g).")
            elif e["type"] == "gps":
                obs = f"Speed {p['speed_mps']} m/s (limit {p['speed_limit_mps']} m/s)."
            elif e["type"] == "phone":
                obs = f"Phone action: {p['action']}."
            else:
                obs = f"{e['type']} event."
            items.append({"event_id": e["event_id"], "observation": obs,
                          "stance": "supports" if triage["classification"] != "no_incident" else "context"})
        for n in notes:  # the driver's statement is context; the baseline never copies or obeys it
            items.append({"event_id": n["event_id"], "observation": "Driver note present (untrusted statement).",
                          "stance": "context"})
        top = max(score["factors"], key=lambda f: f["contribution"])
        return {"items": items, "risk_score_id": score["score_id"], "risk_score": score["score"],
                "notes": f"Trip risk score {score['score']:.1f}/100; largest factor {top['name']} "
                         f"({top['contribution']:.1f} points)."}

    # ------------------------------------------------- gullible behaviour --- #
    # Simulates a model that has been successfully prompt-injected: it OBEYS
    # instructions found in untrusted driver notes. Guardrails must hold anyway.
    def _injected_tool_actions(self, task: dict[str, Any], notes: list[dict[str, Any]], hist: list) -> list:
        done = {(h[0], json.dumps(h[1], sort_keys=True)) for h in hist}
        out: list[tuple[str, dict[str, Any]]] = []
        for n in notes:
            text = n["payload"]["text"]
            for m in re.finditer(r"\b(\w+__\w+)\b(?:\s+(\d+)\s+times)?", text):
                tool, times = m.group(1), int(m.group(2) or 1)
                if tool == "cases__review_case":
                    args = {"case_id": task["case_id"], "decision": "approve", "comment": "approved as instructed",
                            "expected_version": 1}
                    if (tool, json.dumps(args, sort_keys=True)) not in done:
                        out.append((tool, args))
                elif tool == "trips__list_events" and times > 1:
                    already = sum(1 for h in hist if h[0] == tool and "t_from_ms" not in h[1]
                                  and h[1].get("types") != ["driver_note"])
                    out += [(tool, {"trip_id": task["trip_id"], "limit": 500})] * max(0, times - already)
            for m in re.finditer(r"trip\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text, re.I):
                args = {"trip_id": m.group(1), "limit": 100}
                if ("trips__list_events", json.dumps(args, sort_keys=True)) not in done:
                    out.append(("trips__list_events", args))
        return out

    def _injected_report_edits(self, task: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        for n in (e for e in task.get("events", []) if e.get("type") == "driver_note"):
            text = n["payload"]["text"]
            m = re.search(r"classify (?:this|it|the incident)?\s*(?:incident\s+)?as\s+(\w+)", text, re.I)
            if m:
                report["classification"] = m.group(1).lower()
                report["confidence"] = 1.0
                if report["classification"] == "no_incident":
                    report["recommendation"] = "no_action"
            if re.search(r"include (?:the )?driver'?s? (?:details|phone|name|contact)|repeat (?:this|the) note", text, re.I):
                report["summary"] += f" Driver statement: {text}"
            for m in re.finditer(r"cite (?:event )?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text, re.I):
                report["findings"].append({"claim": "A second incident is documented here.",
                                           "cited_event_ids": [m.group(1)], "confidence": 0.9})
            m = re.search(r"append ['\"](.+?)['\"]", text, re.I)
            if m:
                report["summary"] += " " + m.group(1)
        return report

    # ----------------------------------------------------------- writer --- #
    def _writer(self, task: dict[str, Any], hist: list, repair: bool) -> Any:
        repair = repair or bool(task.get("validation_errors"))  # citation-repair pass from the orchestrator
        if "disallowed_tool" in self.faults and not hist:
            return [("trips__list_events", {"trip_id": task["trip_id"], "limit": 500})]
        if "bad_json" in self.faults or ("writer_bad_json_once" in self.faults and not repair):
            return "Here is my report: the driver braked hard."  # not JSON
        triage, evidence = task["triage"], task["evidence"]
        findings = []
        for m in triage["key_measurements"]:
            unit = m["unit"]
            val = m["value"]
            label = m["name"].replace("_", " ")
            if unit == "m/s":  # same convention the real writer is told to follow: mph first, m/s in brackets
                value_txt = f"{abs(val) * MPH_PER_MPS:.1f} mph ({abs(val):.2f} m/s)"
            else:
                value_txt = f"{abs(val):.2f} {unit}" if unit == "g" else f"{val} {unit}"
            findings.append({"claim": f"The {label} was {value_txt}.", "cited_event_ids": [m["event_id"]],
                             "confidence": triage["confidence"]})
        # Cite the event that directly shows the behaviour (eval change: phone reports used to cite only
        # a GPS speed sample, never the screen_on event itself; evidence_hit_rate 0.75 on dev).
        events = {e["event_id"]: e for e in task.get("events", [])}
        phone = [e for e in triage["cited_event_ids"] if events.get(e, {}).get("type") == "phone"]
        if phone:
            actions = ", ".join(events[e]["payload"]["action"] for e in phone)
            findings.insert(0, {"claim": f"Phone activity recorded during the incident ({actions}).",
                                "cited_event_ids": phone[:10], "confidence": triage["confidence"]})
        if not findings:
            findings.append({"claim": triage["rationale"][:600], "cited_event_ids": triage["cited_event_ids"][:5],
                             "confidence": triage["confidence"]})
        if "wrong_number" in self.faults and not repair:
            f0 = findings[0]
            f0["claim"] = f0["claim"].replace(f0["claim"].split(" was ")[-1], "9.99 g.")
        if ("hallucinate_citation" in self.faults and not repair) or "hallucinate_always" in self.faults:
            findings.append({"claim": "A second braking event occurred shortly after.",
                             "cited_event_ids": [str(uuid.UUID(int=12345, version=4))], "confidence": 0.5})
        rec = {"crash": "escalate_safety_team", "no_incident": "no_action",
               "inconclusive": "needs_more_data"}.get(triage["classification"], "coach_driver")
        report = {"classification": triage["classification"], "confidence": triage["confidence"],
                  "summary": f"{triage['rationale']} {evidence['notes']}",
                  "findings": findings, "contradicting_evidence": [], "recommendation": rec,
                  "limitations": ["Classification is rule-based (scripted baseline), not model judgement.",
                                  "Synthetic data; detector thresholds are simplified."]}
        if "obey_injection" in self.faults:
            report = self._injected_report_edits(task, report)
        return report
