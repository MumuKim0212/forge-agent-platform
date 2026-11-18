/**
 * `api/client.ts` decision logic (ADR 0021): what the request layer decides to
 * *send*, and what it decides to do on a `401`. No rendering, no real network.
 *
 * The headline case is the credential-leak one: in `oidc` mode the app must not
 * send `X-Forge-User-Id` even when a stale `forge.devUserId` is left in
 * `localStorage` from a previous dev session. A code review could only
 * establish that by reading the code path; this pins it as a test.
 *
 * `devUserId` and the 401 handler are module-level state, so every test imports
 * a **fresh module instance** (`vi.resetModules()`), which is also the only
 * honest way to represent "the process started in oidc mode".
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

// Every test imports `client.ts` fresh, so it pays a fresh module compile inside its own time
// limit; on a busy machine that can pass Vitest's 5 s default (seen in the sibling
// `agentEngine.test.ts`). 20 s is headroom for that and still bounded, so a test that really hangs
// still fails. Per file, not global.
vi.setConfig({ testTimeout: 20_000 });

const STORAGE_KEY = "forge.devUserId";

type FakeResponse = {
  ok: boolean;
  status: number;
  statusText: string;
  json: () => Promise<unknown>;
};

function jsonResponse(status: number, body: unknown): FakeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `status ${status}`,
    json: async () => body,
  };
}

/** Installs a `fetch` stub that answers every call with `responses` in order
 * (the last one repeats), and returns the mock for call inspection. */
function stubFetch(...responses: FakeResponse[]) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => {
    const next = responses.length > 1 ? responses.shift()! : responses[0];
    return next as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentHeaders(fetchMock: ReturnType<typeof stubFetch>, call = 0): Headers {
  const init = fetchMock.mock.calls[call][1] as RequestInit;
  return init.headers as Headers;
}

function sentUrl(fetchMock: ReturnType<typeof stubFetch>, call = 0): string {
  return fetchMock.mock.calls[call][0];
}

/** A fresh `client.ts`: `devUserId === null`, no unauthorized handler. */
async function freshClient() {
  vi.resetModules();
  return await import("./client");
}

beforeEach(() => {
  vi.resetModules();
});

describe("the identity header is gated on the mode, not on storage", () => {
  it("sends no X-Forge-User-Id in oidc mode even with a stale stored key", async () => {
    // The stale key is written directly, as a previous dev session left it —
    // `setDevUserId` is never called, which is exactly what oidc mode does.
    localStorage.setItem(STORAGE_KEY, "stale-dev-user-id");
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, { id: "u1" }));

    await client.api.get("/users/me");

    expect(sentHeaders(fetchMock).has("X-Forge-User-Id")).toBe(false);
    // Guard against a vacuous pass: the key really is present, so the absence
    // of the header is the gate working and not an empty store.
    expect(client.readStoredDevUserId()).toBe("stale-dev-user-id");
  });

  it("sends the header once dev mode has armed it — the contrast case", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, { id: "u1" }));

    client.setDevUserId("dev-user-id");
    await client.api.get("/users/me");

    expect(sentHeaders(fetchMock).get("X-Forge-User-Id")).toBe("dev-user-id");
  });

  it("stops sending the header and clears storage on setDevUserId(null)", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    client.setDevUserId("dev-user-id");
    client.setDevUserId(null);
    await client.api.get("/users/me");

    expect(sentHeaders(fetchMock).has("X-Forge-User-Id")).toBe(false);
    expect(client.readStoredDevUserId()).toBeNull();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("credentials every request, whatever its verb", async () => {
    // Without `credentials: "include"` the session cookie is not sent and oidc
    // mode is anonymous (ADR 0018 §2) — the failure is a silent 401 storm.
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    await client.api.post("/auth/logout");
    await client.api.get("/projects");

    for (const call of [0, 1]) {
      expect((fetchMock.mock.calls[call][1] as RequestInit).credentials).toBe("include");
    }
    expect(sentUrl(fetchMock)).toBe("/api/auth/logout");
  });
});

describe("Content-Type is sent only with a body", () => {
  // A `Content-Type: application/json` header on a GET is not a CORS-safelisted request header,
  // so under the cross-origin `VITE_API_BASE_URL` setup (ADR 0028) each GET becomes a preflight.
  it("sends none on a GET", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, []));

    await client.api.get("/projects");

    expect(sentHeaders(fetchMock).has("Content-Type")).toBe(false);
  });

  it("sends none on a POST with no body", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    await client.api.post("/auth/logout");

    expect(sentHeaders(fetchMock).has("Content-Type")).toBe(false);
    expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBeUndefined();
  });

  it("sends application/json with the body on a POST that has one", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(201, {}));

    await client.api.post("/projects", { name: "x" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(sentHeaders(fetchMock).get("Content-Type")).toBe("application/json");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"name":"x"}');
  });

  it("sends application/json with the body on a PATCH", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    await client.api.patch("/users/u1/role", { role: "approver" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(sentHeaders(fetchMock).get("Content-Type")).toBe("application/json");
    expect(init.method).toBe("PATCH");
    expect(init.body).toBe('{"role":"approver"}');
  });

  it("still sends an empty object body as JSON: a body is what decides, not its size", async () => {
    // `deploymentsApi.rollback` posts `{}` on purpose.
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    await client.api.post("/projects/p1/deployments/rollback", {});

    expect(sentHeaders(fetchMock).get("Content-Type")).toBe("application/json");
    expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBe("{}");
  });

  it("leaves the identity header to its own rule: it still rides on a GET once armed", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, []));
    client.setDevUserId("dev-user-id");

    await client.api.get("/projects");

    expect(sentHeaders(fetchMock).get("X-Forge-User-Id")).toBe("dev-user-id");
    expect(sentHeaders(fetchMock).has("Content-Type")).toBe(false);
  });
});

describe("every failure is an ApiError", () => {
  function stubNetworkFailure() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
  }

  it("turns a network failure (fetch rejects with a TypeError) into ApiError status 0, kind network", async () => {
    const client = await freshClient();
    stubNetworkFailure();

    const err = await client.api.get("/projects").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(client.ApiError);
    expect(err).toMatchObject({ status: 0, kind: "network" });
    // The browser's English is not carried anywhere on the error.
    expect((err as Error).message).not.toContain("Failed to fetch");
    expect((err as Error).message).toBe(client.NETWORK_ERROR_COPY);
  });

  it("does not fire the 401 hook for a network failure", async () => {
    const client = await freshClient();
    stubNetworkFailure();
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);

    await expect(client.api.get("/projects")).rejects.toBeInstanceOf(client.ApiError);

    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("turns a 2xx whose body is not JSON into ApiError kind parse, carrying the 2xx status", async () => {
    const client = await freshClient();
    stubFetch({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON at position 0");
      },
    });

    const err = await client.api.get("/projects").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(client.ApiError);
    expect(err).toMatchObject({ status: 200, kind: "parse" });
    expect((err as Error).message).toBe(client.PARSE_ERROR_COPY);
    expect((err as Error).message).not.toContain("Unexpected token");
  });

  it("marks an ordinary non-2xx response as kind http", async () => {
    const client = await freshClient();
    stubFetch(jsonResponse(404, { detail: "Project not found" }));

    await expect(client.api.get("/projects/x")).rejects.toMatchObject({ status: 404, kind: "http" });
  });

  it("reads a 4xx's detail when there is one: message is the string, detail is as received", async () => {
    const client = await freshClient();
    stubFetch(jsonResponse(409, { detail: "Cannot demote the last admin" }));

    await expect(client.api.get("/x")).rejects.toMatchObject({
      status: 409,
      message: "Cannot demote the last admin",
      detail: "Cannot demote the last admin",
    });
  });

  it("falls back to the status text when a 4xx body has no detail", async () => {
    const client = await freshClient();
    stubFetch(jsonResponse(400, {}));

    const err = await client.api.get("/x").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(client.ApiError);
    expect(err).toMatchObject({ status: 400, message: "status 400", detail: undefined });
  });

  it("survives an error body that is valid JSON but not an object (null, a string, a number)", async () => {
    // `null.detail` used to be a raw TypeError out of the error path itself.
    const client = await freshClient();
    stubFetch(jsonResponse(500, null), jsonResponse(502, "upstream down"), jsonResponse(503, 7));

    await expect(client.api.get("/a")).rejects.toMatchObject({ status: 500, kind: "http", message: "status 500" });
    await expect(client.api.get("/b")).rejects.toMatchObject({ status: 502, kind: "http", message: "status 502" });
    await expect(client.api.get("/c")).rejects.toMatchObject({ status: 503, kind: "http", message: "status 503" });
  });

  it("keeps a structured detail but words the message as the status text, not [object Object]", async () => {
    const client = await freshClient();
    const detail = [{ type: "string_too_long", loc: ["body", "name"] }];
    stubFetch(jsonResponse(422, { detail }));

    await expect(client.api.post("/projects", {})).rejects.toMatchObject({
      status: 422,
      message: "status 422",
      detail,
    });
  });

  it("returns undefined for a 204 and the parsed body for a 2xx", async () => {
    const client = await freshClient();
    stubFetch(
      { ok: true, status: 204, statusText: "No Content", json: async () => Promise.reject(new SyntaxError("empty")) },
      jsonResponse(200, { id: "p1" }),
    );

    await expect(client.api.post("/auth/logout")).resolves.toBeUndefined();
    await expect(client.api.get("/projects/p1")).resolves.toEqual({ id: "p1" });
  });
});

describe("transportErrorMessage", () => {
  it("words a network and a parse failure in Korean, and nothing else", async () => {
    const client = await freshClient();

    expect(client.transportErrorMessage(new client.ApiError(0, "x", undefined, "network"))).toBe(
      "서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.",
    );
    expect(client.transportErrorMessage(new client.ApiError(200, "x", undefined, "parse"))).toBe(
      "서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
    // An http failure is worded by the screen that knows what it was doing.
    expect(client.transportErrorMessage(new client.ApiError(500, "boom"))).toBeNull();
    // A non-ApiError has no wording at all, least of all its own English message.
    expect(client.transportErrorMessage(new TypeError("Failed to fetch"))).toBeNull();
    expect(client.transportErrorMessage(null)).toBeNull();
  });
});

describe("aborting a request", () => {
  it("hands the signal to fetch", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, []));
    const controller = new AbortController();

    await client.api.get("/projects", { signal: controller.signal });

    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBe(controller.signal);
  });

  it("sends no signal when none was given", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, []));

    await client.api.get("/projects");

    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeUndefined();
  });

  it("rethrows fetch's AbortError as it is: not an ApiError, and recognised by isAbortError", async () => {
    const client = await freshClient();
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        // What a real fetch does: reject with the signal's own AbortError once it fires.
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        });
      }),
    );

    const pending = client.api.get("/projects", { signal: controller.signal }).catch((e: unknown) => e);
    controller.abort();
    const err = await pending;

    expect(err).not.toBeInstanceOf(client.ApiError);
    expect(client.isAbortError(err)).toBe(true);
  });

  it("rethrows an abort that happens while the body is being read, not a parse failure", async () => {
    const client = await freshClient();
    const abortError = new DOMException("The operation was aborted.", "AbortError");
    stubFetch({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => {
        throw abortError;
      },
    });

    const err = await client.api.get("/projects").catch((e: unknown) => e);

    expect(err).toBe(abortError);
  });

  it("isAbortError matches by name only", async () => {
    const client = await freshClient();

    expect(client.isAbortError(new DOMException("x", "AbortError"))).toBe(true);
    expect(client.isAbortError({ name: "AbortError" })).toBe(true);
    expect(client.isAbortError(new TypeError("Failed to fetch"))).toBe(false);
    expect(client.isAbortError(new client.ApiError(0, "x", undefined, "network"))).toBe(false);
    expect(client.isAbortError(null)).toBe(false);
    expect(client.isAbortError("AbortError")).toBe(false);
  });
});

describe("the 401 hook", () => {
  it("fires exactly once per 401 and still throws the ApiError", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(401, { detail: "Invalid session" }));
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);

    await expect(client.api.get("/users/me")).rejects.toBeInstanceOf(client.ApiError);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    // No retry: one request in, one request out. A retry on 401 is the shape
    // that turns an expired session into a request loop.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not loop on a 401 from the login page's own session probe", async () => {
    // `GET /users/me` is what the bootstrap calls, including while sitting on
    // `/login`. This is the structural reason that cannot loop: the hook does
    // nothing but call the handler — no navigation, no retry — so nothing
    // re-enters the flow. (The other half, `/login` being declared outside
    // `RequireIdentity`, is routing rather than decision logic and stays a
    // read/manual check per ADR 0021.)
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(401, { detail: "Invalid session" }));
    const calls: unknown[][] = [];
    client.setUnauthorizedHandler((...args: unknown[]) => {
      calls.push(args);
    });

    await expect(client.api.get("/users/me")).rejects.toThrow("Invalid session");

    expect(calls).toEqual([[]]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("fires once per 401 response, not once per process", async () => {
    const client = await freshClient();
    stubFetch(jsonResponse(401, { detail: "gone" }));
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);

    await expect(client.api.get("/users/me")).rejects.toThrow();
    await expect(client.api.get("/projects")).rejects.toThrow();

    expect(onUnauthorized).toHaveBeenCalledTimes(2);
  });

  it("is not fired by any other failure status", async () => {
    const client = await freshClient();
    stubFetch(
      jsonResponse(403, { detail: "Project owner or admin role required" }),
      jsonResponse(404, { detail: "not found" }),
      jsonResponse(500, { detail: "boom" }),
    );
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);

    await expect(client.api.get("/deployments")).rejects.toMatchObject({ status: 403 });
    await expect(client.api.get("/projects/x")).rejects.toMatchObject({ status: 404 });
    await expect(client.api.get("/projects")).rejects.toMatchObject({ status: 500 });

    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("survives a 401 with no handler registered", async () => {
    // The provider unregisters on unmount, so an in-flight request can land
    // with the hook already `null`. It must still be an ApiError, not a
    // TypeError from calling `null`.
    const client = await freshClient();
    stubFetch(jsonResponse(401, { detail: "Invalid session" }));
    client.setUnauthorizedHandler(null);

    await expect(client.api.get("/users/me")).rejects.toMatchObject({
      status: 401,
      message: "Invalid session",
    });
  });

  it("keeps a structured detail (pydantic's 422 array) on the ApiError, not only its string form", async () => {
    const client = await freshClient();
    const detail = [{ type: "string_too_long", loc: ["body", "nl_request"], msg: "too long" }];
    stubFetch(jsonResponse(422, { detail }));

    await expect(client.api.post("/projects", {})).rejects.toMatchObject({ status: 422, detail });
  });

  it("falls back to the status text when the error body is not JSON", async () => {
    const client = await freshClient();
    stubFetch({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    });

    await expect(client.api.get("/projects")).rejects.toMatchObject({
      status: 502,
      message: "Bad Gateway",
    });
  });
});

describe("apiUrl", () => {
  it("prefixes the same base that fetch uses, so a browser navigation agrees with it", async () => {
    const client = await freshClient();
    const fetchMock = stubFetch(jsonResponse(200, {}));

    await client.api.get("/auth/login");

    // Two assertions on purpose. The first pins the base as a literal, so the
    // value cannot drift unnoticed; the second pins that `fetch` and a browser
    // navigation agree. Neither alone is enough — comparing only the two
    // production-derived values would stay green if `API_BASE_URL` changed,
    // because both sides move together.
    expect(client.apiUrl("/auth/login")).toBe("/api/auth/login");
    expect(client.apiUrl("/auth/login")).toBe(sentUrl(fetchMock));
  });
});
