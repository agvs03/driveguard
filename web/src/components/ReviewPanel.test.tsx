import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ReviewPanel } from "./ReviewPanel";
import { setToken } from "../api";
import type { CaseDetail } from "../types";

afterEach(() => vi.unstubAllGlobals());

const base = { case_id: "c1", status: "AWAITING_APPROVAL", version: 7 } as CaseDetail;

function stubFetch(status: number, body: object) {
  const f = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", f);
  return f;
}

test("buttons stay disabled until a comment is written", () => {
  render(<ReviewPanel c={base} onReviewed={() => {}} />);
  expect((screen.getByText("Approve and close") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/Comment/), { target: { value: "Looks right." } });
  expect((screen.getByText("Approve and close") as HTMLButtonElement).disabled).toBe(false);
});

test("approve posts the decision with the version the reviewer saw", async () => {
  setToken("tok");
  const f = stubFetch(200, { case_id: "c1", status: "CLOSED", version: 8 });
  const done = vi.fn();
  render(<ReviewPanel c={base} onReviewed={done} />);
  fireEvent.change(screen.getByLabelText(/Comment/), { target: { value: "  GPS confirms the stop.  " } });
  fireEvent.click(screen.getByText("Approve and close"));
  await waitFor(() => expect(done).toHaveBeenCalled());
  const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/cases/c1/review");
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body as string)).toEqual({ decision: "approve", comment: "GPS confirms the stop.", expected_version: 7 });
});

test("a 409 tells the reviewer the case changed", async () => {
  setToken("tok");
  stubFetch(409, { code: "CONFLICT", message: "version" });
  const done = vi.fn();
  render(<ReviewPanel c={base} onReviewed={done} />);
  fireEvent.change(screen.getByLabelText(/Comment/), { target: { value: "Reject: cite GPS." } });
  fireEvent.click(screen.getByText("Reject, re-investigate"));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("changed"));
  expect(done).not.toHaveBeenCalled();
});

test("closed cases show no review controls", () => {
  const { container } = render(<ReviewPanel c={{ ...base, status: "CLOSED" }} onReviewed={() => {}} />);
  expect(container.innerHTML).toBe("");
});
