/**
 * The contract strings of the test-message call: its path and its verb cross the API
 * boundary, so a one-sided rename is invisible to `tsc` and reads as a console bug (404/405).
 * Literals on the expected side; the module is imported fresh so the stubbed fetch is what it calls.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.setConfig({ testTimeout: 20_000 });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("notificationsApi", () => {
  it("sends the test with POST /api/platform/notifications/test", async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        ({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({ results: [] }),
        }) as unknown as Response,
    );
    vi.stubGlobal("fetch", fetchMock);
    const { notificationsApi } = await import("./notifications");

    await notificationsApi.sendTest();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/platform/notifications/test");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
  });
});
