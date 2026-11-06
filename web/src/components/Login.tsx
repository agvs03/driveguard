import { useState } from "react";
import { api, setToken } from "../api";
import type { Me } from "../types";

export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [token, setTokenText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await api.me(token.trim());
      if (!me.scopes.includes("cases:read")) throw new Error("this token cannot read cases (needs cases:read)");
      setToken(token.trim());
      onSignedIn(me);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>DriveGuard case review</h1>
        <p className="muted">
          Paste a reviewer token for the API. Create one with:
        </p>
        <pre className="cmd">driveguard token --sub human:you --role human --scopes cases:read,trace:read,cases:review --aud api</pre>
        <p className="muted small">Leave out <code>cases:review</code> for a read-only reviewer.</p>
        <label htmlFor="token">Bearer token</label>
        <textarea id="token" rows={4} value={token} onChange={(e) => setTokenText(e.target.value)}
          spellCheck={false} autoComplete="off" />
        {error && <div className="error-box" role="alert">{error}</div>}
        <button type="submit" disabled={busy || !token.trim()}>{busy ? "Checking…" : "Sign in"}</button>
        <p className="muted small">The token stays in this tab's session storage and is sent only to this site's /api.</p>
      </form>
    </div>
  );
}
