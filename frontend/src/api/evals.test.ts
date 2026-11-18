/**
 * The contract strings of the eval calls: paths and verbs cross the API boundary, so a
 * one-sided rename is invisible to `tsc` and degrades quietly (a 404 or 405 that reads as a console
 * bug). Literals on the expected side; the module is imported fresh so `vi.stubGlobal("fetch")` is
 * what it calls.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

// Every test imports the module fresh, so it pays a fresh module compile inside its own time
// limit; on a busy machine that can pass Vitest's 5 s default (seen in the sibling
// `agentEngine.test.ts`). 20 s is headroom for that and still bounded, so a test that really hangs
// still fails. Per file, not global.
vi.setConfig({ testTimeout: 20_000 });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(
    async (_url: string, _init?: RequestInit) =>
      ({
        ok: status >= 200 && status < 300,
        status,
        statusText: `status ${status}`,
        json: async () => body,
      }) as unknown as Response,
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("evalsApi", () => {
  it("lists with GET /api/evals/runs", async () => {
    const fetchMock = stubFetch(200, []);
    const { evalsApi } = await import("./evals");

    await evalsApi.listRuns();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evals/runs");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBeUndefined();
  });

  it("reads the pre-run status with GET /api/evals/status", async () => {
    const fetchMock = stubFetch(200, {});
    const { evalsApi } = await import("./evals");

    await evalsApi.status();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evals/status");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBeUndefined();
  });

  it("reads one run with GET /api/evals/runs/{id}", async () => {
    const fetchMock = stubFetch(200, {});
    const { evalsApi } = await import("./evals");

    await evalsApi.getRun("run-1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evals/runs/run-1");
  });

  it("encodes the id it puts in the path", async () => {
    const fetchMock = stubFetch(200, {});
    const { evalsApi } = await import("./evals");

    await evalsApi.getRun("a/b?c");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evals/runs/a%2Fb%3Fc");
  });

  it("starts with POST /api/evals/runs", async () => {
    const fetchMock = stubFetch(201, {});
    const { evalsApi } = await import("./evals");

    await evalsApi.startRun();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evals/runs");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
  });

  it("is not a console path: that router is read-only", async () => {
    const { EVAL_RUNS_PATH, EVAL_STATUS_PATH } = await import("./evals");

    expect(EVAL_RUNS_PATH.startsWith("/console")).toBe(false);
    expect(EVAL_STATUS_PATH.startsWith("/console")).toBe(false);
  });
});
