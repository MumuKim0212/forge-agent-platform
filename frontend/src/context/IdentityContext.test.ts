/**
 * Mode detection (ADR 0021: decision logic, not rendering). The provider is
 * never mounted here — only `detectAuthMode`, which is the whole decision.
 *
 * `GET /dev/identities`' registration condition was widened from `not
 * oidc_enabled` to `not session_auth_enabled`, so that route is now absent
 * in **both** `local` and `oidc` mode, not just `oidc` — a 404 on it no
 * longer conclusively means `oidc`. A second probe
 * (`authApi.localAccountsEnabled`) that only fires once the first one has
 * already failed (or come back malformed), and only a
 * confirmed positive from it asserts `local`. Every other outcome — of
 * either probe — resolves to `oidc`, the same fail-safe direction this file
 * already established before `local` existed: being wrong that way shows an
 * unusable SSO button rather than a credential-free identity picker or a
 * signup form nobody can submit against a real backend.
 *
 * The second probe's mechanism was replaced (`GET /auth/signup`
 * 405-vs-404 status code -> `GET /openapi.json`'s `paths` object, tested on
 * its own in `api/auth.test.ts`), and the one remaining gap closed:
 * a `200` from the first probe carrying non-array JSON no longer resolves
 * `dev` (the *least* restrictive mode) — see the dedicated describe block
 * below, which replaces the old `KNOWN GAP` characterisation test now that
 * the gap is closed rather than merely documented.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { detectAuthMode } from "./IdentityContext";

// This file resets the module registry before each test, but imports `IdentityContext` statically,
// so its compile happens at collection, outside any test's time limit: unlike its siblings
// (`agentEngine`, `client`, `evals`) it does not pay a fresh compile per test. Same bounded 20 s
// headroom anyway, so a later `await import(...)` here is covered and a real hang still fails
//.
vi.setConfig({ testTimeout: 20_000 });

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

/** A schema shaped like FastAPI's `/openapi.json` output, narrowed to the
 * one field `authApi.localAccountsEnabled` reads. */
function openApiSchema(paths: Record<string, unknown>): unknown {
  return { openapi: "3.1.0", info: { title: "Forge Platform API" }, paths };
}

const LOCAL_MODE_SCHEMA = jsonResponse(200, openApiSchema({ "/api/auth/signup": { post: {} } }));
const NON_LOCAL_MODE_SCHEMA = jsonResponse(200, openApiSchema({ "/api/health": { get: {} } }));

/** Every call to `fetch`, regardless of path, gets the same answer — used
 * for the scenarios where both probes see the same outcome (e.g. a network
 * failure that affects every request, or a 404/401/500 a real backend would
 * never actually give both paths, kept here as-is because both probes fall
 * through to `oidc` identically either way). */
function respondWith(response: FakeResponse | (() => Promise<never>)) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      if (typeof response === "function") return response();
      return response as unknown as Response;
    }),
  );
}

/** Answers differently per path, so the dev probe (`/dev/identities`) and
 * the schema probe (`/openapi.json`) can be driven independently — the only
 * way to exercise the `local` branch, which needs the first probe to fail
 * and the second to name `/api/auth/signup` in its schema. */
function respondByPath(handlers: Record<string, FakeResponse | (() => Promise<never>)>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const handler = handlers[url];
      if (!handler) throw new Error(`test fetch stub: unexpected request to ${url}`);
      return typeof handler === "function" ? handler() : (handler as unknown as Response);
    }),
  );
}

const DEV_IDENTITIES_URL = "/api/dev/identities";
const OPENAPI_SCHEMA_URL = "/openapi.json";

beforeEach(() => {
  vi.resetModules();
});

describe("detectAuthMode — only a 200 with an array body asserts dev", () => {
  it("resolves dev on a 200 array, and the probe doubles as the switcher list", async () => {
    const identities = [
      { id: "u1", name: "김민지" },
      { id: "u2", name: "이도현" },
    ];
    respondWith(jsonResponse(200, identities));

    // Also the contrast case for every test below: if detection could not
    // resolve `dev` at all, the oidc/local assertions would be vacuous.
    await expect(detectAuthMode()).resolves.toEqual({ mode: "dev", devIdentities: identities });
  });

  it("resolves dev on a 200 empty array — a dev backend with no seeded users", async () => {
    respondWith(jsonResponse(200, []));

    await expect(detectAuthMode()).resolves.toEqual({ mode: "dev", devIdentities: [] });
  });
});

describe("detectAuthMode — the local probe fires only after the dev probe fails", () => {
  it("dev 404, schema names /api/auth/signup -> local", async () => {
    respondByPath({
      [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
      [OPENAPI_SCHEMA_URL]: LOCAL_MODE_SCHEMA,
    });

    await expect(detectAuthMode()).resolves.toEqual({ mode: "local", devIdentities: [] });
  });

  it("dev 401 (an odd shape, but still not a dev 200 array), schema names the route -> local", async () => {
    respondByPath({
      [DEV_IDENTITIES_URL]: jsonResponse(401, { detail: "Invalid session" }),
      [OPENAPI_SCHEMA_URL]: LOCAL_MODE_SCHEMA,
    });

    await expect(detectAuthMode()).resolves.toEqual({ mode: "local", devIdentities: [] });
  });

  it("does not probe /openapi.json at all once the dev probe already succeeded", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === DEV_IDENTITIES_URL) return jsonResponse(200, []) as unknown as Response;
      throw new Error(`unexpected request to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await detectAuthMode();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("detectAuthMode — every remaining outcome resolves to oidc", () => {
  const cases: Array<{ label: string; arrange: () => void }> = [
    {
      label: "both probes 404 — a real oidc-mode backend (openapi disabled too)",
      arrange: () =>
        respondByPath({
          [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
          [OPENAPI_SCHEMA_URL]: jsonResponse(404, { detail: "Not Found" }),
        }),
    },
    {
      label: "401 on both",
      arrange: () => respondWith(jsonResponse(401, { detail: "Invalid session" })),
    },
    {
      label: "500 — backend up but broken, we cannot tell the mode",
      arrange: () => respondWith(jsonResponse(500, { detail: "boom" })),
    },
    {
      label: "network rejection — backend down or unreachable",
      arrange: () => respondWith(() => Promise.reject(new TypeError("Failed to fetch"))),
    },
    {
      label: "200 whose body is not JSON — e.g. a proxy answering with HTML",
      arrange: () =>
        respondWith({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => {
            throw new SyntaxError("Unexpected token < in JSON at position 0");
          },
        }),
    },
    {
      label: "dev probe 404, schema reachable but does not name /api/auth/signup",
      arrange: () =>
        respondByPath({
          [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
          [OPENAPI_SCHEMA_URL]: NON_LOCAL_MODE_SCHEMA,
        }),
    },
    {
      label: "dev probe 404, schema body has no paths object",
      arrange: () =>
        respondByPath({
          [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
          [OPENAPI_SCHEMA_URL]: jsonResponse(200, { openapi: "3.1.0", info: {} }),
        }),
    },
    {
      label: "dev probe 404, schema probe itself 500s",
      arrange: () =>
        respondByPath({
          [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
          [OPENAPI_SCHEMA_URL]: jsonResponse(500, { detail: "boom" }),
        }),
    },
    {
      label: "dev probe 404, schema probe network failure",
      arrange: () =>
        respondByPath({
          [DEV_IDENTITIES_URL]: jsonResponse(404, { detail: "Not Found" }),
          [OPENAPI_SCHEMA_URL]: () => Promise.reject(new TypeError("Failed to fetch")),
        }),
    },
  ];

  for (const { label, arrange } of cases) {
    it(`${label} -> oidc, with no identities`, async () => {
      arrange();

      await expect(detectAuthMode()).resolves.toEqual({ mode: "oidc", devIdentities: [] });
    });
  }
});

describe("detectAuthMode — the Array.isArray() guard", () => {
  // A gap that used to be open:
  // a `200` from the dev probe carrying valid non-array JSON used
  // to resolve `dev` outright — the LEAST restrictive mode, and backwards
  // for an unproven body. Closed here. These two tests replace the old
  // `KNOWN GAP` characterisation test, which predicted exactly this: "the
  // day someone adds the Array.isArray() guard, this test fails and has to
  // be flipped on purpose, which is the point."

  it("a 200 with non-array JSON no longer asserts dev — falls through, and a local-shaped second probe now wins", async () => {
    respondByPath({
      [DEV_IDENTITIES_URL]: jsonResponse(200, { detail: "Not Found" }),
      [OPENAPI_SCHEMA_URL]: LOCAL_MODE_SCHEMA,
    });

    await expect(detectAuthMode()).resolves.toEqual({ mode: "local", devIdentities: [] });
  });

  it("a 200 with non-array JSON falls through to oidc when the second probe also fails to prove local", async () => {
    respondByPath({
      [DEV_IDENTITIES_URL]: jsonResponse(200, { detail: "Not Found" }),
      [OPENAPI_SCHEMA_URL]: jsonResponse(404, { detail: "Not Found" }),
    });

    await expect(detectAuthMode()).resolves.toEqual({ mode: "oidc", devIdentities: [] });
  });
});

describe("detectAuthMode — the probe timeout", () => {
  it("times out a hanging dev probe and still asks the local probe, which also hangs, before settling oidc", async () => {
    // Closed gap: the dev probe can hang.
    // Both probes hang for the whole test — nothing ever resolves the raw
    // `fetch` — so the only thing that can make `detectAuthMode` settle is
    // the timeout itself.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );

    vi.useFakeTimers();
    try {
      let settled = false;
      const detection = detectAuthMode().then((value) => {
        settled = true;
        return value;
      });

      // Comfortably before either probe's timeout: nothing has settled yet.
      await vi.advanceTimersByTimeAsync(4_000);
      expect(settled).toBe(false);

      // Past the first probe's timeout but not the second's: still pending,
      // because the local probe starts its own timer once the dev probe's
      // times out.
      await vi.advanceTimersByTimeAsync(2_000);
      expect(settled).toBe(false);

      // Past both timeouts.
      await vi.advanceTimersByTimeAsync(5_000);
      expect(settled).toBe(true);
      await expect(detection).resolves.toEqual({ mode: "oidc", devIdentities: [] });
    } finally {
      vi.useRealTimers();
    }
  });

  it("a hanging dev probe times out and a fast, unregistered schema probe still resolves oidc", async () => {
    // The more realistic shape: the backend answers *something* once it
    // eventually gets a request, it just never answers the first probe.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === DEV_IDENTITIES_URL) return new Promise<Response>(() => {});
        if (url === OPENAPI_SCHEMA_URL) return jsonResponse(404, { detail: "Not Found" }) as unknown as Response;
        throw new Error(`unexpected request to ${url}`);
      }),
    );

    vi.useFakeTimers();
    try {
      const detection = detectAuthMode();

      await vi.advanceTimersByTimeAsync(5_000);
      await expect(detection).resolves.toEqual({ mode: "oidc", devIdentities: [] });
    } finally {
      vi.useRealTimers();
    }
  });
});
