"""Phase 5 guardrails. Each is switchable so red-team runs can compare off vs on.

The Phase 2/3 controls (case-bound tokens, per-agent tool allowlists, the
writer having no tools, the citation validator, human-only review) are NOT
switchable: they are the architecture, and stay on in both red-team modes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Guardrails:
    sanitize_untrusted_text: bool = True   # trips-server: spotlight + flag injection in driver notes
    redact_pii: bool = True                # trips-server: redact PII in driver notes at the source
    argument_limits: bool = True           # gateway: agents may not pull unbounded event ranges
    abort_on_repeated_denials: bool = True  # gateway: 2 denied calls -> stop the agent (manipulation signal)
    output_guard: bool = True              # orchestrator: policy-consistency, PII/secret scan, echo detection

    @property
    def any(self) -> bool:
        return any(self.__dict__.values())


ON = Guardrails()
OFF = Guardrails(False, False, False, False, False)
