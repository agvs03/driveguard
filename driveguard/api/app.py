"""HTTP API for the reviewer UI (Starlette).

    GET  /healthz                       liveness (no auth)
    GET  /metrics                       Prometheus text (no auth; aggregate labels only, bind to localhost)
    GET  /api/me                        who the token says you are
    GET  /api/cases?status=&limit=      cases visible to the token                      cases:read
    GET  /api/cases/{case_id}           case, notes, history, runs, latest report       cases:read
    GET  /api/runs/{run_id}/trace       a run's audit trace, chain check, citation map  trace:read
    GET  /api/slo                       SLO report for this database                    cases:read
    POST /api/cases/{case_id}/review    approve or reject (the only write)              cases:review (human-only)
    /                                   the built UI (web/dist), if present

Access control (closes the Phase 5 residual risk "the trace has no access control"):
  * bearer JWT for audience `driveguard:api`, same verifier as the MCP servers;
  * `trace:read` is human-only (enforced when a token is issued AND verified):
    a trace contains every tool result an agent saw, so agents never get it;
  * a case-bound token (claim case_id) sees only that case and its runs; a
    request for anything else gets the same 403 whether it exists or not, so
    the API is not an existence oracle;
  * reads go through a mode=ro connection; the one write is a review, which
    needs `cases:review` (human-only at token issue AND verify), the case
    version the reviewer saw (optimistic locking), a JSON body and a comment.
    Auth is a bearer header the browser never attaches on its own, so
    cross-site requests can't ride on a session (no CSRF surface).

The trace view is redacted for DISPLAY on top of the redaction at source:
runs made with guardrails off can hold raw driver-note PII, and long strings
are clipped. The stored chain is untouched, so `integrity` is computed on the
real rows.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from driveguard.agents.trace import Trace, events_seen, load_spans, verify_run
from driveguard.cases.service import Actor, get_case, list_cases
from driveguard.cases.service import review as service_review
from driveguard.errors import DriveGuardError
from driveguard.guardrails.text import redact_pii
from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier, TokenClaims
from driveguard.obs.context import bind
from driveguard.obs.logging import log_event
from driveguard.obs.metrics import API_LATENCY, API_REQUESTS, REGISTRY
from driveguard.obs.tracing import span
from driveguard.store.db import Database

log = logging.getLogger("driveguard.api")
MAX_STR = 4000
CASE_STATUSES = {"OPEN", "INVESTIGATING", "AWAITING_APPROVAL", "ESCALATED", "CLOSED"}
MAX_COMMENT = 2000
# Domain error code -> HTTP status. CONFLICT (stale version) and FAILED_PRECONDITION (illegal transition)
# are both 409: the reviewer's view of the case is out of date.
HTTP_STATUS = {"INVALID_ARGUMENT": 400, "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
               "CONFLICT": 409, "FAILED_PRECONDITION": 409, "RATE_LIMITED": 429, "INTERNAL": 500}
SECURITY_HEADERS = {
    # Trace content includes attacker-controlled driver notes: scripts only from our own origin, nothing inline.
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


FORBIDDEN_CASE = ApiError(403, "FORBIDDEN", "case not accessible with this token")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError(400, "INVALID_ARGUMENT", "id must be a UUID") from None


# ---------------------------------------------------------------- display --- #

def display_redact(obj: Any, names: list[str]) -> Any:
    """Clip long strings and redact PII in prose-like strings (ids and numbers are left alone)."""
    if isinstance(obj, str):
        s = obj
        if " " in s and len(s) >= 12:
            s = redact_pii(s, names).text
        return s if len(s) <= MAX_STR else s[:MAX_STR] + f"... [{len(s) - MAX_STR} more chars clipped for display]"
    if isinstance(obj, dict):
        return {k: display_redact(v, names) for k, v in obj.items()}
    if isinstance(obj, list):
        return [display_redact(v, names) for v in obj]
    return obj


def _driver_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT display_name FROM drivers") if r[0]]


# ---------------------------------------------------------------- queries --- #

def q_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
        return None
    case = get_case(conn, case_id)
    case["runs"] = [dict(r) | {"outcome": json.loads(r["outcome_json"] or "{}")} for r in conn.execute(
        "SELECT run_id, status, llm, started_at_ms, ended_at_ms, cost_usd, model_calls, tool_calls, outcome_json "
        "FROM investigation_runs WHERE case_id = ? ORDER BY started_at_ms DESC, rowid DESC", (case_id,))]
    for r in case["runs"]:
        r.pop("outcome_json", None)
    rep = conn.execute("SELECT report_id, run_id, valid, report_json, validation_json, created_at_ms FROM reports "
                       "WHERE case_id = ? ORDER BY created_at_ms DESC, rowid DESC LIMIT 1", (case_id,)).fetchone()
    case["report"] = None if rep is None else {
        "report_id": rep["report_id"], "run_id": rep["run_id"], "valid": bool(rep["valid"]),
        "created_at_ms": rep["created_at_ms"], "body": json.loads(rep["report_json"]),
        "validation": json.loads(rep["validation_json"])}
    trip = conn.execute("SELECT trip_id, driver_id, vehicle_id, started_at_ms, ended_at_ms, distance_km, status "
                        "FROM trips WHERE trip_id = ?", (case["trip_id"],)).fetchone()
    case["trip"] = dict(trip) if trip else None  # pseudonymous ids only: no name or phone ever leaves the API
    return display_redact(case, _driver_names(conn))


def q_case_exists(conn: sqlite3.Connection, case_id: str) -> bool:
    return conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is not None


def q_latest_run(conn: sqlite3.Connection, case_id: str) -> str | None:
    row = conn.execute("SELECT run_id FROM investigation_runs WHERE case_id = ? ORDER BY started_at_ms DESC, "
                       "rowid DESC LIMIT 1", (case_id,)).fetchone()
    return row[0] if row else None


def q_run_case(conn: sqlite3.Connection, run_id: str) -> tuple[bool, str | None]:
    row = conn.execute("SELECT case_id FROM investigation_runs WHERE run_id = ?", (run_id,)).fetchone()
    return (row is not None), (row[0] if row else None)


def q_trace(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    ok, bad_seq = verify_run(conn, run_id)
    spans = load_spans(conn, run_id)
    citations = {eid: sid for eid, (_, sid) in events_seen(spans).items()}
    seq_of = {s["span_id"]: s["seq"] for s in spans}
    run = dict(conn.execute("SELECT run_id, case_id, incident_id, status, llm, started_at_ms, ended_at_ms, cost_usd, "
                            "model_calls, tool_calls FROM investigation_runs WHERE run_id = ?", (run_id,)).fetchone())
    keep = ("seq", "span_id", "parent_span_id", "kind", "actor", "status", "model", "usage", "cost_usd", "latency_ms",
            "at_ms", "input", "output")
    view = [{k: s[k] for k in keep} for s in spans]
    return {
        "run": run,
        "integrity": {"ok": ok, "first_bad_seq": bad_seq, "spans": len(spans)},
        "citations": {eid: {"span_id": sid, "seq": seq_of.get(sid)} for eid, sid in citations.items()},
        "spans": display_redact(view, _driver_names(conn)),
    }


# --------------------------------------------------------------------- app --- #

Handler = Callable[[Request, TokenClaims | None], Awaitable[Any]]


def create_app(db_path: str, secret: str | bytes, *, static_dir: str | Path | None = None,
               now: Callable[[], float] = time.time) -> Starlette:
    db = Database(db_path, read_only=True)
    rw = Database(db_path)
    verifier = DriveGuardTokenVerifier(secret, AUD["api"], now=now)

    def authenticate(request: Request) -> TokenClaims:
        h = request.headers.get("authorization", "")
        claims = verifier.decode(h[7:].strip()) if h[:7].lower() == "bearer " else None
        if claims is None:
            raise ApiError(401, "UNAUTHENTICATED", "missing, invalid or expired bearer token")
        return claims

    def require(claims: TokenClaims, scope: str) -> None:
        if scope not in claims.scopes:
            raise ApiError(403, "FORBIDDEN", f"token lacks scope {scope}")

    def require_case(claims: TokenClaims, case_id: str | None) -> None:
        if claims.case_id is not None and case_id != claims.case_id:
            raise FORBIDDEN_CASE

    def endpoint(template: str, fn: Handler, *, auth: bool = True, methods: list[str] | None = None) -> Route:
        methods = methods or ["GET"]
        async def handle(request: Request) -> Response:
            t0 = time.perf_counter()
            status, code = 200, "OK"
            with bind(component="api", request_id=request.headers.get("x-request-id") or uuid.uuid4().hex[:16]), \
                    span("api_request", route=template) as sp:
                try:
                    claims = authenticate(request) if auth else None
                    result = await fn(request, claims)
                    resp: Response = result if isinstance(result, Response) else JSONResponse(result)
                except ApiError as e:
                    status, code = e.status, e.code
                    resp = JSONResponse({"code": e.code, "message": e.message}, status_code=e.status)
                except DriveGuardError as e:
                    status, code = HTTP_STATUS.get(e.code.value, 400), e.code.value
                    resp = JSONResponse(e.envelope(), status_code=status)
                except Exception:  # noqa: BLE001
                    status, code = 500, "INTERNAL"
                    log.exception("api handler crashed route=%s", template)
                    resp = JSONResponse({"code": "INTERNAL", "message": "internal error"}, status_code=500)
                latency = time.perf_counter() - t0
                API_REQUESTS.inc(route=template, status=str(status))
                API_LATENCY.observe(latency, route=template)
                sp.set(status=status)
                if status >= 500:
                    sp.error(code)
                log_event(log, "api_request", logging.WARNING if status >= 500 else logging.INFO, route=template,
                          status=status, code=code, latency_ms=round(latency * 1000, 2))
            return resp

        return Route(template, handle, methods=methods)

    async def healthz(_: Request, __: Any) -> Any:
        return {"ok": True}

    async def metrics(_: Request, __: Any) -> Any:
        return PlainTextResponse(REGISTRY.render(), media_type="text/plain; version=0.0.4")

    async def me(_: Request, c: TokenClaims) -> Any:
        return {"sub": c.sub, "role": c.role, "scopes": c.scopes, "case_id": c.case_id, "expires_at": c.exp}

    async def cases(request: Request, c: TokenClaims) -> Any:
        require(c, "cases:read")
        status = request.query_params.get("status") or None
        if status is not None and status not in CASE_STATUSES:
            raise ApiError(400, "INVALID_ARGUMENT", f"status must be one of {sorted(CASE_STATUSES)}")
        try:
            limit = max(1, min(200, int(request.query_params.get("limit", "100"))))
        except ValueError:
            raise ApiError(400, "INVALID_ARGUMENT", "limit must be an integer") from None
        return {"cases": await db.run(list_cases, status, limit, c.case_id)}

    async def case(request: Request, c: TokenClaims) -> Any:
        require(c, "cases:read")
        case_id = _uuid(request.path_params["case_id"])
        require_case(c, case_id)  # before the lookup: no existence oracle
        found = await db.run(q_case, case_id)
        if found is None:
            raise ApiError(404, "NOT_FOUND", "case not found")
        return found

    async def trace(request: Request, c: TokenClaims) -> Any:
        require(c, "trace:read")
        run_id = _uuid(request.path_params["run_id"])
        exists, case_id = await db.run(q_run_case, run_id)
        if c.case_id is not None and (not exists or case_id != c.case_id):
            raise FORBIDDEN_CASE  # same answer for "no such run" and "someone else's run"
        if not exists:
            raise ApiError(404, "NOT_FOUND", "run not found")
        return await db.run(q_trace, run_id)

    async def review(request: Request, c: TokenClaims) -> Any:
        """Human approval from the UI. Same rules as `driveguard review`: scope cases:review (human-only by
        token policy), optimistic locking via expected_version, and a human_decision span in the run's trace."""
        require(c, "cases:review")
        if c.role != "human":  # unreachable with a valid token (policy), kept as a second line of defence
            raise ApiError(403, "FORBIDDEN", "only a human reviewer can review a case")
        case_id = _uuid(request.path_params["case_id"])
        require_case(c, case_id)
        if "application/json" not in request.headers.get("content-type", ""):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "send a JSON body")
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "INVALID_ARGUMENT", "body is not valid JSON") from None
        if not isinstance(body, dict) or set(body) - {"decision", "comment", "expected_version"}:
            raise ApiError(400, "INVALID_ARGUMENT", "body must be {decision, comment, expected_version}")
        decision, comment, version = body.get("decision"), body.get("comment"), body.get("expected_version")
        if decision not in ("approve", "reject"):
            raise ApiError(400, "INVALID_ARGUMENT", "decision must be approve or reject")
        if not isinstance(comment, str) or not 3 <= len(comment.strip()) <= MAX_COMMENT:
            raise ApiError(400, "INVALID_ARGUMENT", f"comment is required (3 to {MAX_COMMENT} characters)")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ApiError(400, "INVALID_ARGUMENT", "expected_version must be the case version you reviewed")
        if await db.run(q_case_exists, case_id) is False:
            raise ApiError(404, "NOT_FOUND", "case not found")
        now_ms = int(now() * 1000)
        updated = await rw.run(service_review, case_id, Actor(c.sub, "human"), now_ms, decision, comment.strip(),
                               version)
        run_id = await rw.run(q_latest_run, case_id)
        if run_id:  # the decision becomes part of the run's tamper-evident record, as with the CLI
            await Trace(rw, run_id, case_id=case_id, now_ms=lambda: now_ms).record(
                "human_decision", c.sub, {"decision": decision, "comment": comment.strip()},
                {"status": updated["status"], "version": updated["version"], "via": "api"})
        log_event(log, "case_reviewed", case_id=case_id, decision=decision, status=updated["status"])
        return {"case_id": case_id, "status": updated["status"], "version": updated["version"]}

    async def slo(_: Request, c: TokenClaims) -> Any:
        require(c, "cases:read")
        from driveguard.obs.slo import evaluate
        results = await db.run(lambda conn: evaluate([conn]))
        return {"slos": [r.as_dict() for r in results]}

    routes: list[Any] = [
        endpoint("/healthz", healthz, auth=False),
        endpoint("/metrics", metrics, auth=False),
        endpoint("/api/me", me),
        endpoint("/api/cases", cases),
        endpoint("/api/cases/{case_id}", case),
        endpoint("/api/runs/{run_id}/trace", trace),
        endpoint("/api/slo", slo),
        endpoint("/api/cases/{case_id}/review", review, methods=["POST"]),
    ]
    if static_dir and Path(static_dir, "index.html").is_file():
        routes.append(Mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui"))

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await db.connect()
        await rw.connect()  # the one write path: reviews (separate connection; reads stay mode=ro)
        try:
            yield
        finally:
            await rw.close()
            await db.close()

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders)])


class SecurityHeaders:
    """Pure ASGI middleware: add SECURITY_HEADERS to every response (API, static files, 404s)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        cacheable = scope.get("path", "").startswith("/assets/")  # content-hashed UI bundles

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()
                         if k.encode() not in present and not (cacheable and k == "cache-control")]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8100, static_dir: str | None = None) -> None:
    import uvicorn

    from driveguard.mcp_servers.serve import require_secret
    uvicorn.run(create_app(db_path, require_secret(), static_dir=static_dir), host=host, port=port,
                log_level="warning")
