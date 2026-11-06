import { useCallback, useEffect, useState } from "react";
import { api, setToken, getToken, SIGNED_OUT } from "./api";
import { CaseList } from "./components/CaseList";
import { CaseView } from "./components/CaseView";
import { Login } from "./components/Login";
import { Empty } from "./components/common";
import type { Me } from "./types";

// Hash routing (#/cases/<id>?run=<run_id>): the API serves one static index.html, no server-side routes.
function parseHash(): { caseId: string | null; runId: string | null } {
  const m = window.location.hash.match(/^#\/cases\/([0-9a-f-]{36})(?:\?run=([0-9a-f-]{36}))?/);
  return { caseId: m?.[1] ?? null, runId: m?.[2] ?? null };
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [route, setRoute] = useState(parseHash);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    const onSignedOut = () => setMe(null); // any 401 (expired token) returns to the sign-in screen
    window.addEventListener("hashchange", onHash);
    window.addEventListener(SIGNED_OUT, onSignedOut);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener(SIGNED_OUT, onSignedOut);
    };
  }, []);

  useEffect(() => {
    if (!getToken()) {
      setChecking(false);
      return;
    }
    api.me().then(setMe).catch(() => setToken(null)).finally(() => setChecking(false));
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    setMe(null);
  }, []);

  if (checking) return <div className="pad muted">Loading…</div>;
  if (!me) return <Login onSignedIn={setMe} />;

  // A case-bound token can only ever open its own case.
  const caseId = route.caseId ?? me.case_id;
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">DriveGuard <span className="muted">case review</span></span>
        <span className="who">
          <code>{me.sub}</code>
          {me.case_id && <span className="muted small"> · limited to one case</span>}
          <button className="link" onClick={signOut}>sign out</button>
        </span>
      </header>
      <div className="layout">
        <CaseList selected={caseId} onSelect={(id) => { window.location.hash = `#/cases/${id}`; }} />
        <main>
          {caseId ? (
            <CaseView key={caseId} caseId={caseId} runId={route.runId} me={me}
              onSelectRun={(r) => { window.location.hash = `#/cases/${caseId}?run=${r}`; }} />
          ) : (
            <Empty>Select a case. Cases awaiting approval or escalated need a reviewer's decision
              {me.scopes.includes("cases:review") ? "." : <> (your token is read-only: it lacks <code>cases:review</code>).</>}</Empty>
          )}
        </main>
      </div>
    </div>
  );
}
