"""Bearer tokens for the MCP servers.

Format: standard JWT, HS256 only (PyJWT), so no hand-rolled crypto. Claims:

    iss       "driveguard"                    sub       who (e.g. "agent:triage:<run>")
    aud       ["driveguard:trips", ...]       role      "agent" | "human" | "service"
    scope     space-separated scopes          exp/iat/nbf/jti
    case_id   case the token is bound to      trip_ids  trips the token may read

Rules enforced at BOTH issue and verify time:
  * agent tokens MUST be bound (case_id + at least one trip_id);
  * agent tokens MUST NOT carry cases:review or cases:create;
  * only human tokens may carry cases:review or trace:read;
  * the verifier only accepts its own audience, HS256, and unexpired tokens.

Binding is what stops cross-case data leakage: a valid agent token for case A
cannot read case B's trips even if it guesses B's ids (design §7 T3).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, Field, ValidationError, field_validator

from driveguard.errors import Code, DriveGuardError

ISSUER = "driveguard"
ALGORITHM = "HS256"
AUD = {"trips": "driveguard:trips", "risk": "driveguard:risk", "cases": "driveguard:cases", "api": "driveguard:api"}
ALL_SCOPES = {"trips:read", "risk:read", "cases:read", "cases:write", "cases:create", "cases:review", "trace:read"}
HUMAN_ONLY_SCOPES = {"cases:review", "trace:read"}  # trace:read: a trace holds every tool result of a run
AGENT_FORBIDDEN_SCOPES = {"cases:review", "cases:create"}
MIN_SECRET_BYTES = 32

Role = Literal["agent", "human", "service"]


class TokenClaims(BaseModel):
    iss: Literal["driveguard"]
    sub: str = Field(min_length=1, max_length=200)
    aud: list[str] = Field(min_length=1)
    role: Role
    scope: str
    exp: int
    iat: int
    nbf: int
    jti: str
    case_id: str | None = None
    trip_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("aud", mode="before")
    @classmethod
    def _aud_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @property
    def scopes(self) -> list[str]:
        return [s for s in self.scope.split(" ") if s]


def _check_policy(role: str, scopes: set[str], case_id: str | None, trip_ids: list[str]) -> None:
    unknown = scopes - ALL_SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    if role == "agent":
        if not case_id or not trip_ids:
            raise ValueError("agent tokens must be bound to a case and at least one trip")
        if scopes & AGENT_FORBIDDEN_SCOPES:
            raise ValueError(f"agent tokens may not carry {sorted(scopes & AGENT_FORBIDDEN_SCOPES)}")
    if scopes & HUMAN_ONLY_SCOPES and role != "human":
        raise ValueError(f"only human tokens may carry {sorted(scopes & HUMAN_ONLY_SCOPES)}")


def _secret(secret: str | bytes) -> bytes:
    b = secret.encode() if isinstance(secret, str) else secret
    if len(b) < MIN_SECRET_BYTES:
        raise ValueError(f"token secret must be at least {MIN_SECRET_BYTES} bytes")
    return b


class TokenIssuer:
    def __init__(self, secret: str | bytes, now: Any = time.time) -> None:
        self._secret = _secret(secret)
        self._now = now

    def issue(self, *, sub: str, role: Role, scopes: set[str] | list[str], audiences: list[str],
              case_id: str | None = None, trip_ids: list[str] | None = None, ttl_s: int = 900) -> str:
        scopes = set(scopes)
        trip_ids = list(trip_ids or [])
        _check_policy(role, scopes, case_id, trip_ids)
        now = int(self._now())
        claims = {
            "iss": ISSUER, "sub": sub, "aud": audiences, "role": role, "scope": " ".join(sorted(scopes)),
            "iat": now, "nbf": now, "exp": now + ttl_s, "jti": str(uuid.uuid4()),
            "case_id": case_id, "trip_ids": trip_ids,
        }
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM)


class DriveGuardTokenVerifier:
    """Implements the MCP SDK's TokenVerifier protocol."""

    def __init__(self, secret: str | bytes, audience: str, now: Any = time.time, leeway_s: int = 5) -> None:
        self._secret = _secret(secret)
        self.audience = audience
        self._now = now
        self._leeway = leeway_s

    def decode(self, token: str) -> TokenClaims | None:
        try:
            raw = jwt.decode(
                token, self._secret, algorithms=[ALGORITHM], audience=self.audience, issuer=ISSUER,
                leeway=self._leeway, options={"require": ["exp", "iat", "nbf", "aud", "iss", "sub", "jti"],
                                               "verify_iat": False},
            )
            # PyJWT checks exp against the wall clock; re-check with the injectable clock for tests.
            if raw["exp"] + self._leeway < self._now() or raw["nbf"] - self._leeway > self._now():
                return None
            claims = TokenClaims.model_validate(raw)
            _check_policy(claims.role, set(claims.scopes), claims.case_id, claims.trip_ids)
            return claims
        except (jwt.PyJWTError, ValidationError, ValueError, KeyError):
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        c = self.decode(token)
        if c is None:
            return None
        return AccessToken(
            token=token, client_id=c.sub, scopes=c.scopes, expires_at=c.exp, resource=self.audience,
            subject=c.sub, claims={"iss": c.iss, "role": c.role, "case_id": c.case_id,
                                   "trip_ids": c.trip_ids, "jti": c.jti},
        )


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    scopes: frozenset[str]
    case_id: str | None
    trip_ids: frozenset[str]
    jti: str

    @property
    def bound(self) -> bool:
        return self.case_id is not None

    @classmethod
    def from_access_token(cls, tok: AccessToken) -> "Principal":
        c = tok.claims or {}
        return cls(sub=tok.subject or tok.client_id, role=str(c.get("role", "")), scopes=frozenset(tok.scopes),
                   case_id=c.get("case_id"), trip_ids=frozenset(c.get("trip_ids") or []), jti=str(c.get("jti", "")))

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise DriveGuardError(Code.FORBIDDEN, f"token lacks scope {scope}")

    def can_see_trip(self, trip_id: str) -> bool:
        return not self.bound or trip_id in self.trip_ids

    def require_case(self, case_id: str) -> None:
        if self.bound and case_id != self.case_id:
            raise DriveGuardError(Code.FORBIDDEN, "case not accessible with this token")


def current_principal() -> Principal:
    tok = get_access_token()
    if tok is None:
        # Defence in depth: only reachable if a server is mounted without the auth middleware.
        raise DriveGuardError(Code.UNAUTHENTICATED, "missing or invalid bearer token")
    return Principal.from_access_token(tok)
