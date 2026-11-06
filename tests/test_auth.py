import base64
import json
import time

import jwt
import pytest

from driveguard.errors import DriveGuardError
from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier, Principal, TokenIssuer
from tests.mcp_util import SECRET, token

V = DriveGuardTokenVerifier(SECRET, AUD["trips"])


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


async def test_round_trip_claims():
    tok = token("agent", {"trips:read"}, case_id="c1", trip_ids=["t1", "t2"])
    at = await V.verify_token(tok)
    p = Principal.from_access_token(at)
    assert (p.role, p.case_id, p.trip_ids, p.scopes) == ("agent", "c1", frozenset({"t1", "t2"}),
                                                          frozenset({"trips:read"}))
    assert p.bound and p.can_see_trip("t1") and not p.can_see_trip("t3")


async def test_expired_token_rejected():
    tok = TokenIssuer(SECRET, now=lambda: time.time() - 3600).issue(
        sub="h", role="human", scopes={"trips:read"}, audiences=[AUD["trips"]], ttl_s=60)
    assert await V.verify_token(tok) is None


async def test_not_yet_valid_rejected_with_injected_clock():
    tok = token("human", {"trips:read"})
    early = DriveGuardTokenVerifier(SECRET, AUD["trips"], now=lambda: time.time() - 600)
    assert await early.verify_token(tok) is None


async def test_wrong_audience_rejected():
    assert await V.verify_token(token("human", {"cases:read"}, aud=("cases",))) is None


async def test_wrong_secret_and_tampering_rejected():
    other = TokenIssuer("another-secret-" + "z" * 40).issue(sub="h", role="human", scopes={"trips:read"},
                                                             audiences=[AUD["trips"]])
    assert await V.verify_token(other) is None
    good = token("agent", {"trips:read"}, case_id="c1", trip_ids=["t1"])
    h, payload, sig = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["trip_ids"] = ["t1", "victim-trip"]  # privilege escalation attempt
    assert await V.verify_token(f"{h}.{_b64(claims)}.{sig}") is None


async def test_alg_none_rejected():
    claims = jwt.decode(token("human", {"trips:read"}), options={"verify_signature": False})
    forged = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    assert await V.verify_token(forged) is None


@pytest.mark.parametrize("role,scopes,case_id,trips,why", [
    ("agent", {"trips:read"}, None, [], "unbound agent"),
    ("agent", {"cases:review"}, "c", ["t"], "agent with review"),
    ("agent", {"cases:create"}, "c", ["t"], "agent with create"),
    ("service", {"cases:review"}, None, [], "service with review"),
    ("human", {"admin:everything"}, None, [], "unknown scope"),
])
async def test_policy_enforced_at_issue_and_verify(role, scopes, case_id, trips, why):
    with pytest.raises(ValueError):
        TokenIssuer(SECRET).issue(sub="x", role=role, scopes=scopes, audiences=[AUD["trips"]],
                                  case_id=case_id, trip_ids=trips)
    # A correctly *signed* token that violates policy (e.g. a buggy issuer) is still refused.
    now = int(time.time())
    raw = jwt.encode({"iss": "driveguard", "sub": "x", "aud": [AUD["trips"]], "role": role,
                      "scope": " ".join(scopes), "iat": now, "nbf": now, "exp": now + 60, "jti": "j",
                      "case_id": case_id, "trip_ids": trips}, SECRET, algorithm="HS256")
    assert await V.verify_token(raw) is None, why


def test_short_secret_refused():
    with pytest.raises(ValueError):
        TokenIssuer("short")
    with pytest.raises(ValueError):
        DriveGuardTokenVerifier("short", AUD["trips"])


def test_principal_case_binding():
    p = Principal("a", "agent", frozenset(), "case-a", frozenset({"t"}), "j")
    p.require_case("case-a")
    with pytest.raises(DriveGuardError) as e:
        p.require_case("case-b")
    assert e.value.code.value == "FORBIDDEN"
    Principal("h", "human", frozenset(), None, frozenset(), "j").require_case("anything")
