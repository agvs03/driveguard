import { useState } from "react";
import { api, ApiError } from "../api";
import type { CaseDetail } from "../types";

const REVIEWABLE = new Set(["AWAITING_APPROVAL", "ESCALATED"]);

/** Approve or reject: the human decision the system is built around. The server enforces every rule
 *  (human-only scope, case version, legal transition); this panel only collects the decision. */
export function ReviewPanel({ c, onReviewed }: { c: CaseDetail; onReviewed: () => void }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!REVIEWABLE.has(c.status)) return null;
  const tooShort = comment.trim().length < 3;

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      await api.review(c.case_id, decision, comment.trim(), c.version);
      setComment("");
      onReviewed();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError("This case changed since you opened it (or was already reviewed). Reload it and check again.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="card review-panel" aria-label="Review this case">
      <h2>Your decision</h2>
      <p className="muted small">
        {c.status === "ESCALATED"
          ? "The agents could not produce a report that passed every check. Decide from the evidence and the timeline."
          : "Check the findings against the cited evidence before approving. Rejecting sends the case back for a new investigation with your comment."}
      </p>
      <label htmlFor="review-comment">Comment (required, recorded in the audit trail)</label>
      <textarea id="review-comment" rows={3} maxLength={2000} value={comment}
        onChange={(e) => setComment(e.target.value)} placeholder="e.g. GPS confirms the stop; approved." />
      {error && <div className="error-box" role="alert">{error}</div>}
      <div className="review-actions">
        <button className="btn approve" disabled={tooShort || busy !== null} onClick={() => decide("approve")}>
          {busy === "approve" ? "Approving…" : "Approve and close"}
        </button>
        <button className="btn reject" disabled={tooShort || busy !== null} onClick={() => decide("reject")}>
          {busy === "reject" ? "Rejecting…" : "Reject, re-investigate"}
        </button>
      </div>
    </section>
  );
}
