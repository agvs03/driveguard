import { useState } from "react";
import type { Json } from "../types";

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status.toLowerCase()}`}>{status.replace(/_/g, " ").toLowerCase()}</span>;
}

/** JSON is rendered as text inside <pre>: React escapes it, so attacker-controlled strings stay inert. */
export function JsonView({ value, max = 6000 }: { value: Json; max?: number }) {
  const [full, setFull] = useState(false);
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const clipped = !full && text.length > max;
  return (
    <div className="json">
      <pre>{clipped ? text.slice(0, max) + "\n…" : text}</pre>
      {clipped && (
        <button className="link" onClick={() => setFull(true)}>
          show all {text.length.toLocaleString()} characters
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return <div className="error-box" role="alert">{msg}</div>;
}
