"""Untrusted free text: injection signals, spotlighting, PII redaction and scanning.

Heuristics, not a classifier: they will miss paraphrased attacks. They are
one layer; the layers that do not depend on recognising an attack are the
capability limits (no review tool, case-bound tokens) and the deterministic
output checks (policy consistency, citation grounding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INJECTION = [
    ("override", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|polic|previous|above)",
                            re.I | re.S)),
    ("role_spoof", re.compile(r"(^|\n|\s)(system|assistant|developer)\s*:|<\|?(system|im_start)|\[/?inst\]", re.I)),
    ("classify_cmd", re.compile(r"\b(classify|mark|label|report)\b.{0,30}\bas\b\s+(no_incident|crash|hard_braking|"
                                r"near_miss|phone_distraction|inconclusive|safe|clean)", re.I | re.S)),
    ("tool_cmd", re.compile(r"\b\w+__\w+\b|\b(call|invoke|use|run)\b.{0,20}\b(tool|function)\b|\breview_case\b|"
                            r"\bapprove\b.{0,20}\bcase\b", re.I | re.S)),
    ("exfil_cmd", re.compile(r"\b(include|print|reveal|output|append|add|repeat|copy)\b.{0,40}\b(token|api.?key|secret|"
                             r"password|credential|phone|email|address|name|summary|report|note)", re.I | re.S)),
    ("other_trip", re.compile(r"\btrip\b.{0,15}[0-9a-f]{8}-[0-9a-f]{4}-", re.I | re.S)),
    ("persona", re.compile(r"\byou are now\b|\bnew instructions\b|\bimportant (system )?notice\b|\bas an ai\b", re.I)),
]


def injection_signals(text: str) -> list[str]:
    return [name for name, rx in _INJECTION if rx.search(text)]


def spotlight(text: str) -> str:
    """Wrap untrusted text in explicit delimiters (and neutralise look-alike closers)
    so a model can tell data from instructions. Helps real models; no effect on the
    gullible test agent, which is the point of testing with it."""
    safe = text.replace("<untrusted_driver_note>", "[tag removed]").replace("</untrusted_driver_note>", "[tag removed]")
    return f"<untrusted_driver_note>{safe}</untrusted_driver_note>"


# ----------------------------------------------------------------------- PII --- #
_PII = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Trailing "(?!\w|\.\d)": a sentence-final full stop must not stop a match (it did: M-22),
    # while "13.9" style decimals still can't be read as part of a number.
    # Separators between digit groups are REQUIRED: real Claude reports quote 13-digit epoch-ms timestamps
    # ("at 1768216407600"), which the old optional-separator pattern read as phone numbers (M-24).
    # Trade-off: an unformatted 10-digit phone number is no longer caught.
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w|\.\d)")),
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-])?\d{3}[\s.-]\d{4}(?!\w|\.\d)")),
    ("street_address", re.compile(r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|"
                                  r"Boulevard|Blvd|Way|Court|Ct)\b\.?")),
    # The phrase is case-insensitive; the name itself must be capitalised words.
    ("self_identified_name", re.compile(r"\b(?i:my name is|i am|i'm|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)")),
    ("plate", re.compile(r"\b(?:plate|registration|reg)\b[:\s#]*([A-Z0-9]{2,4}[-\s]?[A-Z0-9]{2,4})\b", re.I)),
]


@dataclass
class Redaction:
    text: str
    kinds: list[str] = field(default_factory=list)


def redact_pii(text: str, known_names: list[str] | None = None) -> Redaction:
    kinds: list[str] = []
    out = text
    for name in sorted(known_names or [], key=len, reverse=True):
        if name and re.search(re.escape(name), out, re.I):
            out = re.sub(re.escape(name), "[REDACTED:name]", out, flags=re.I)
            kinds.append("known_name")
    for kind, rx in _PII:
        if kind == "self_identified_name":
            new = rx.sub(lambda m: m.group(0).replace(m.group(1), "[REDACTED:name]"), out)
        else:
            new = rx.sub(f"[REDACTED:{kind}]", out)
        if new != out:
            kinds.append(kind)
            out = new
    return Redaction(out, sorted(set(kinds)))


_SECRETS = [("jwt", re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")),
            ("api_key", re.compile(r"sk-ant-[\w-]{8,}"))]


def scan_sensitive(text: str, known_names: list[str] | None = None,
                   known_phones: list[str] | None = None) -> list[str]:
    """Kinds of PII/secrets present in text (for the output guard). Redaction markers don't count."""
    found = [k for k, rx in _SECRETS if rx.search(text)]
    found += redact_pii(text, known_names).kinds
    for ph in known_phones or []:
        if ph and ph in text:
            found.append("known_phone")
    return sorted(set(found))


_WORD = r"[a-z0-9]+(?:'[a-z0-9]+)*"


def shared_run(a: str, b: str) -> tuple[int, list[str], list[str]]:
    """(length, the shared words, a's words with that run removed) for the longest run of
    consecutive words shared by a and b."""
    wa, wb = re.findall(_WORD, a.lower()), re.findall(_WORD, b.lower())
    best, at = 0, 0
    prev = [0] * (len(wb) + 1)
    for i in range(1, len(wa) + 1):
        cur = [0] * (len(wb) + 1)
        for j in range(1, len(wb) + 1):
            if wa[i - 1] == wb[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, at = cur[j], i - cur[j]
        prev = cur
    return best, wa[at:at + best], wa[:at] + wa[at + best:]


DISCLOSURE = re.compile(r"\b(request|requested|requests|asked|asks|instruct\w*|attempt\w*|not followed|not acted|"
                        r"ignored|declined|disregarded|flagged|injection|untrusted|refused)\b")


def longest_shared_words(a: str, b: str) -> int:
    """Length of the longest run of consecutive words shared by a and b (case-insensitive)."""
    # Apostrophes only inside words ("driver's"): quote marks around a phrase must not
    # change its tokens, or quoting the injected text evades the check (found by red team).
    word = r"[a-z0-9]+(?:'[a-z0-9]+)*"
    wa = re.findall(word, a.lower())
    wb = re.findall(word, b.lower())
    if not wa or not wb:
        return 0
    best = 0
    prev = [0] * (len(wb) + 1)
    for i in range(1, len(wa) + 1):
        cur = [0] * (len(wb) + 1)
        for j in range(1, len(wb) + 1):
            if wa[i - 1] == wb[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best
