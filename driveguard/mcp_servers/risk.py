"""risk-server: transparent trip risk scores with per-factor explanations.

Scores are deterministic: the same events and weights version always give the
same score_id and numbers. Scores are cached in `risk_scores` (idempotent
insert), so explain_score can return exactly what score_trip returned.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from driveguard.errors import Code, DriveGuardError, not_found
from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier, Principal, current_principal
from driveguard.mcp_servers.common import UUID_RE, RateLimiter, ToolKit, make_server
from driveguard.risk.scoring import RiskScore, compute_factors, explain, load_weights, score
from driveguard.store import queries as q
from driveguard.store.db import transaction

Uuid = Annotated[str, Field(pattern=UUID_RE)]
WeightsVersion = Literal["weights-v1"]


class Explanation(BaseModel):
    score: RiskScore
    explanation: list[str] = Field(description="One line per factor, largest contribution first")


def _deny(p: Principal, what: str) -> DriveGuardError:
    return DriveGuardError(Code.FORBIDDEN, f"{what} not accessible with this token") if p.bound \
        else not_found(f"{what} not found")


def _save(conn: Any, rs: RiskScore, now_ms: int) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO risk_scores (score_id, trip_id, weights_version, input_hash, score, result_json, "
            "computed_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (score_id) DO NOTHING",
            (rs.score_id, rs.trip_id, rs.weights_version, rs.input_hash, rs.score, rs.model_dump_json(), now_ms))


def _load(conn: Any, score_id: str) -> RiskScore | None:
    row = conn.execute("SELECT result_json FROM risk_scores WHERE score_id = ?", (score_id,)).fetchone()
    return RiskScore.model_validate_json(row["result_json"]) if row else None


def build_risk_server(db_path: str, *, verifier: DriveGuardTokenVerifier | None = None,
                      resource_url: str | None = None, limiter: RateLimiter | None = None) -> Any:
    kit = ToolKit("risk", db_path, read_only=False, limiter=limiter or RateLimiter(120, 60))
    mcp = make_server("driveguard-risk",
                      "Transparent, deterministic trip risk scores (0-100). Each factor reports its raw value, "
                      "normalisation, weight, contribution in points, and the event ids it came from.",
                      kit, token_verifier=verifier, resource_url=resource_url)
    mcp.kit = kit  # type: ignore[attr-defined]

    @mcp.tool(description="Compute (or fetch the cached) risk score for a trip. Contributions sum to the score.")
    @kit.guard("risk:read")
    async def score_trip(trip_id: Uuid, weights_version: WeightsVersion = "weights-v1") -> RiskScore:
        p = current_principal()
        if not p.can_see_trip(trip_id):
            raise _deny(p, "trip")
        trip = await kit.run(q.get_trip, trip_id)
        if trip is None:
            raise _deny(p, "trip")
        events = await kit.run(q.trip_stored_events, trip_id)
        incidents = await kit.run(q.list_incidents, trip_id)
        rs = score(trip_id, compute_factors(events, incidents, trip["distance_km"], weights_version), weights_version)
        await kit.run(_save, rs, kit.now_ms())
        return rs

    @mcp.tool(description="Explain a previously computed score: per-factor arithmetic in plain text.")
    @kit.guard("risk:read")
    async def explain_score(score_id: Uuid) -> Explanation:
        p = current_principal()
        rs = await kit.run(_load, score_id)
        if rs is None or not p.can_see_trip(rs.trip_id):
            raise _deny(p, "score")
        return Explanation(score=rs, explanation=explain(rs))

    @mcp.tool(description="The weights, floors and caps used for scoring. Public; contains no trip data.")
    @kit.guard("risk:read")
    async def get_weights(weights_version: WeightsVersion = "weights-v1") -> dict[str, Any]:
        return json.loads(json.dumps(load_weights(weights_version)))

    return mcp


def default_verifier(secret: str) -> DriveGuardTokenVerifier:
    return DriveGuardTokenVerifier(secret, AUD["risk"])
