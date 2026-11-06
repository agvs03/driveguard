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

async function get<T>(path: string, token: string | null = getToken()): Promise<T> {
  const res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
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
};
