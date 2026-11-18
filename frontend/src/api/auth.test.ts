/**
 * `authApi.localAccountsEnabled` — the second half of mode
 * detection, on its own. `context/IdentityContext.test.ts` exercises it
 * indirectly through `detectAuthMode`; this pins the schema-reading logic
 * directly, since that logic (parse `/openapi.json`, check `/api/auth/signup`
 * is a key of its `paths` object) is the one place a typo could silently
 * invert the result.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { authApi } from "./auth";

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
 * one field this module reads. */
function openApiSchema(paths: Record<string, unknown>): unknown {
  return { openapi: "3.1.0", info: { title: "Forge Platform API" }, paths };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("localAccountsEnabled — only a schema naming /api/auth/signup asserts local", () => {
  it("200 with /api/auth/signup in paths -> true", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, openApiSchema({ "/api/auth/signup": { post: {} } })) as unknown as Response,
      ),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(true);
  });

  it("200 with a paths object that does not name the route -> false", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, openApiSchema({ "/api/health": { get: {} } })) as unknown as Response,
      ),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });

  it("200 with an empty paths object -> false", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, openApiSchema({})) as unknown as Response),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });

  it("a non-200 response -> false, the schema endpoint itself unreachable/broken", () => {
    return Promise.all(
      [404, 401, 403, 500, 502].map(async (status) => {
        vi.stubGlobal(
          "fetch",
          vi.fn(async () => jsonResponse(status, { detail: "x" }) as unknown as Response),
        );
        await expect(authApi.localAccountsEnabled()).resolves.toBe(false);
      }),
    );
  });

  it("a non-2xx response whose body happens to be schema-shaped and names the route -> still false — the status is checked, not just the body shape", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(500, openApiSchema({ "/api/auth/signup": { post: {} } })) as unknown as Response,
      ),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });

  it("a 200 whose body is not JSON -> false", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => {
          throw new SyntaxError("Unexpected token < in JSON at position 0");
        },
      }) as unknown as Response),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });

  it("a 200 whose body has no paths object -> false", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { openapi: "3.1.0", info: {} }) as unknown as Response),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });

  it("a 200 whose paths field is not an object (e.g. null or a string) -> false", () => {
    return Promise.all(
      [null, "not-an-object", 42, ["/api/auth/signup"]].map(async (paths) => {
        vi.stubGlobal(
          "fetch",
          vi.fn(async () => jsonResponse(200, { paths }) as unknown as Response),
        );
        await expect(authApi.localAccountsEnabled()).resolves.toBe(false);
      }),
    );
  });

  it("a 200 whose body is not an object at all (e.g. a bare array or null) -> false", () => {
    return Promise.all(
      [null, [], "schema"].map(async (body) => {
        vi.stubGlobal(
          "fetch",
          vi.fn(async () => jsonResponse(200, body) as unknown as Response),
        );
        await expect(authApi.localAccountsEnabled()).resolves.toBe(false);
      }),
    );
  });

  it("a network failure -> false", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))),
    );

    return expect(authApi.localAccountsEnabled()).resolves.toBe(false);
  });
});

describe("localAccountsEnabled — probes the schema endpoint, not the /api base", () => {
  it("requests GET /openapi.json, unprefixed by the /api base apiUrl builds", () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse(200, openApiSchema({})) as unknown as Response,
    );
    vi.stubGlobal("fetch", fetchMock);

    return authApi.localAccountsEnabled().then(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe("/openapi.json");
      expect(init?.method ?? "GET").toBe("GET");
    });
  });
});
