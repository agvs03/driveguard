import type { CaseDetail, CaseSummary, Me, TraceResponse } from "./types";

// The token lives in sessionStorage: gone when the tab closes, never sent anywhere but our own /api.
const KEY = "driveguard.token";
export const SIGNED_OUT = "driveguard:signed-out";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: the user signs in again next load */
  }
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export const CASE_UPDATED = "driveguard:case-updated";

/** GET, or POST a JSON body when `payload` is given. */
async function get<T>(path: string, token: string | null = getToken(), payload?: object): Promise<T> {
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  if (payload) headers["Content-Type"] = "application/json";
  const res = await fetch(path, payload ? { method: "POST", headers, body: JSON.stringify(payload) } : { headers });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && token) {
    // Expired or revoked: drop the stale token and send the user back to sign-in instead of
    // leaving them "signed in" with every request failing.
    setToken(null);
    window.dispatchEvent(new Event(SIGNED_OUT));
  }
  if (!res.ok) throw new ApiError(res.status, body.code ?? "ERROR", body.message ?? res.statusText);
  return body as T;
}

export const api = {
  me: (token?: string) => get<Me>("/api/me", token ?? getToken()),
  cases: (status?: string) =>
    get<{ cases: CaseSummary[] }>(`/api/cases${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  case: (id: string) => get<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`),
  trace: (runId: string) => get<TraceResponse>(`/api/runs/${encodeURIComponent(runId)}/trace`),
  review: (caseId: string, decision: "approve" | "reject", comment: string, expectedVersion: number) =>
    get<{ case_id: string; status: string; version: number }>(
      `/api/cases/${encodeURIComponent(caseId)}/review`, getToken(),
      { decision, comment, expected_version: expectedVersion }),
};
