"""Credential redaction shared by the audit trace and the log formatter."""

from __future__ import annotations

import re
from typing import Any

JWT = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")
API_KEY = re.compile(r"sk-ant-[\w-]{8,}")
BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}")


def redact_str(s: str) -> str:
    return API_KEY.sub("[REDACTED_KEY]", JWT.sub("[REDACTED_TOKEN]", s))


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_str(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj
