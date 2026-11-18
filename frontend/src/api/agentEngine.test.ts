/**
 * The contract strings of the engine selection call: its path, its verb and the body
 * key. All three cross the API boundary, so a one-sided rename is invisible to `tsc` and
 * degrades quietly (a 404, a 405 or a 422 that reads as a console bug). Literals on the
 * expected side; the module is imported fresh so `vi.stubGlobal("fetch")` is what it calls.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

// Every test imports the module fresh, so it pays a fresh module compile inside its own time
// limit; on a busy machine that has gone past Vitest's 5 s default (two timeouts in one full
// run, the file green alone). 20 s is headroom for that and still bounded, so a test that really
// hangs still fails. Per file, not global.
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

describe("agentEngineApi", () => {
  it("reads GET /api/platform/agent-engine", async () => {
    const fetchMock = stubFetch(200, { engines: [] });
    const { agentEngineApi } = await import("./agentEngine");

    await agentEngineApi.get();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/platform/agent-engine");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBeUndefined();
  });

  it("selects with PATCH /api/platform/agent-engine and the body {engine}", async () => {
    const fetchMock = stubFetch(200, { engines: [] });
    const { agentEngineApi } = await import("./agentEngine");

    await agentEngineApi.select({ engine: "openai" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(fetchMock.mock.calls[0][0]).toBe("/api/platform/agent-engine");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ engine: "openai" });
  });

  it("is not a console path: that router is read-only", async () => {
    const { AGENT_ENGINE_PATH } = await import("./agentEngine");

    expect(AGENT_ENGINE_PATH.startsWith("/console")).toBe(false);
  });
});
