"""The classification POLICY, implemented deterministically.

Used twice:
  * by the scripted baseline analyst (Phase 3/4);
  * by the output guardrail (Phase 5) as an independent check on whatever the
    LLM concluded. The LLM proposes, the rules verify: a report whose
    classification contradicts what the signals say under the policy is
    stopped and escalated, which defeats "classify this as no_incident"
    prompt injections even when the model is fooled.

The policy text the LLM sees lives in specialists.POLICY; this module must stay
in sync with it (tests cover every class on simulator ground truth).
"""

from __future__ import annotations

import math
from typing import Any


def resultant(p: dict[str, Any]) -> float:
    return math.sqrt(p["ax_peak"] ** 2 + p["ay_peak"] ** 2 + p["az_peak"] ** 2)


def classify_by_policy(inc: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """inc: {kind, t_start_ms, t_end_ms}; events: event records (event_id, type, event_time_ms, payload).
    Returns {classification, confidence, rationale, cited_event_ids, key_measurements}."""
    t0, t1 = inc["t_start_ms"], inc["t_end_ms"]
    acc = [e for e in events if e["type"] == "accel" and t0 - 1500 <= e["event_time_ms"] <= t1 + 2500]
    gps = [e for e in events if e["type"] == "gps"]
    meas: list[dict[str, Any]] = []
    cited: list[str] = []
    cls, conf, why = "inconclusive", 0.4, "Signals in the window are insufficient."

    if inc["kind"] == "phone_distraction":
        phone = sorted((e for e in events if e["type"] == "phone"), key=lambda e: e["event_time_ms"])
        on = next((e for e in phone if e["payload"]["action"] in ("unlock", "screen_on")), None)
        off = next((e for e in phone if e["payload"]["action"] == "screen_off"), None)
        end = off["event_time_ms"] if off else max(e["event_time_ms"] for e in events)
        if on:
            moving = [g for g in gps if on["event_time_ms"] <= g["event_time_ms"] <= end
                      and g["payload"]["speed_mps"] > 5]
            cited = [on["event_id"]] + ([off["event_id"]] if off else []) + [g["event_id"] for g in moving[:3]]
            if moving:
                meas.append({"name": "speed_at_screen_on", "value": moving[0]["payload"]["speed_mps"],
                             "unit": "m/s", "event_id": moving[0]["event_id"]})
            if len(moving) >= 10:
                cls, conf = "phone_distraction", 0.9
                why = f"Screen on while moving above 5 m/s for {len(moving)} GPS samples (>= 10 s)."
            else:
                cls, conf = "no_incident", 0.7
                why = f"Screen use overlapped only {len(moving)} s of driving above 5 m/s (< 10 s)."
    elif acc:
        worst_res = max(acc, key=lambda e: resultant(e["payload"]))
        worst_dec = min(acc, key=lambda e: e["payload"]["ax_peak"])
        worst_lat = max(acc, key=lambda e: abs(e["payload"]["ay_peak"]))
        res, dec, lat = resultant(worst_res["payload"]), -worst_dec["payload"]["ax_peak"], \
            abs(worst_lat["payload"]["ay_peak"])
        after = [g for g in gps if g["event_time_ms"] > worst_res["event_time_ms"] + 2000]
        stopped = bool(after) and all(g["payload"]["speed_mps"] < 0.5 for g in after)
        if res >= 4.0 and stopped:
            cls, conf, cited = "crash", 0.9, [worst_res["event_id"], after[-1]["event_id"]]
            meas = [{"name": "peak_resultant", "value": round(res, 3), "unit": "g", "event_id": worst_res["event_id"]},
                    {"name": "speed_after", "value": after[-1]["payload"]["speed_mps"], "unit": "m/s",
                     "event_id": after[-1]["event_id"]}]
            why = f"Resultant {res:.2f} g followed by the vehicle remaining stationary."
        elif res >= 3.0 and abs(worst_res["payload"]["az_peak"]) > 1.5 and dec < 0.45:
            cls, conf, cited = "no_incident", 0.75, [worst_res["event_id"]]
            meas = [{"name": "peak_vertical", "value": worst_res["payload"]["az_peak"], "unit": "g",
                     "event_id": worst_res["event_id"]}]
            why = "Spike is almost entirely vertical with no deceleration: consistent with a pothole."
        elif dec >= 0.45 and lat >= 0.45:
            cls, conf, cited = "near_miss", 0.85, list(dict.fromkeys([worst_dec["event_id"], worst_lat["event_id"]]))
            meas = [{"name": "peak_longitudinal_decel", "value": round(dec, 4), "unit": "g",
                     "event_id": worst_dec["event_id"]},
                    {"name": "peak_lateral", "value": round(lat, 4), "unit": "g", "event_id": worst_lat["event_id"]}]
            why = f"Hard deceleration ({dec:.2f} g) with a lateral swerve ({lat:.2f} g) and no impact."
        elif dec >= 0.45:
            cls, conf, cited = "hard_braking", 0.85, [worst_dec["event_id"]]
            meas = [{"name": "peak_longitudinal_decel", "value": round(dec, 4), "unit": "g",
                     "event_id": worst_dec["event_id"]}]
            why = f"Longitudinal deceleration peaked at {dec:.2f} g, above the 0.45 g policy threshold."
        else:
            cls, conf, cited = "no_incident", 0.7, [worst_dec["event_id"]]
            meas = [{"name": "peak_longitudinal_decel", "value": round(dec, 4), "unit": "g",
                     "event_id": worst_dec["event_id"]}]
            why = f"Peak deceleration {dec:.2f} g is below the 0.45 g policy threshold."
    if not cited:
        cited = [events[0]["event_id"]] if events else []
    return {"classification": cls, "confidence": conf, "rationale": why, "cited_event_ids": cited,
            "key_measurements": meas}
