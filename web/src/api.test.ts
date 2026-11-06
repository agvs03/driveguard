import { api, getToken, setToken, SIGNED_OUT } from "./api";

afterEach(() => vi.unstubAllGlobals());

function stubFetch(status: number, body: object) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

test("requests carry the bearer token only to the same-origin /api", async () => {
  setToken("tok-123");
  const f = stubFetch(200, { cases: [] });
  await api.cases("ESCALATED");
  const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/cases?status=ESCALATED");
  expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-123");
});

test("a 401 clears the stale token and signals sign-out", async () => {
  setToken("expired");
  stubFetch(401, { code: "UNAUTHENTICATED", message: "expired" });
  const seen = vi.fn();
  window.addEventListener(SIGNED_OUT, seen);
  await expect(api.cases()).rejects.toMatchObject({ status: 401, code: "UNAUTHENTICATED" });
  expect(getToken()).toBeNull();
  expect(seen).toHaveBeenCalledOnce();
  window.removeEventListener(SIGNED_OUT, seen);
});

test("a 403 keeps the session (the token is valid, just not allowed)", async () => {
  setToken("valid");
  stubFetch(403, { code: "FORBIDDEN", message: "no" });
  await expect(api.trace("r")).rejects.toMatchObject({ status: 403 });
  expect(getToken()).toBe("valid");
});
