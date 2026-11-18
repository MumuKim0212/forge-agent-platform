/**
 * A 403 has to render as an explanation, not as an empty panel
 * (the nav gate is presentation, the
 * backend is the enforcement point, so this state is reachable).
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { panelErrorMessage } from "./panelError";

const FALLBACK = "불러오지 못했습니다.";

describe("panelErrorMessage", () => {
  it("explains a 403 without echoing the server's own line (suffix dropped)", () => {
    const detail = "Admin role required";
    const message = panelErrorMessage(new ApiError(403, detail), FALLBACK);
    expect(message).not.toBe(detail);
    expect(message).not.toContain(detail);
    expect(message).toBe(
      "이 정보는 admin 역할만 조회할 수 있습니다. 방금 역할이 바뀌었다면 새로 고침 후 다시 확인하세요.",
    );
  });

  it("distinguishes a 403 from any other failure", () => {
    const forbidden = panelErrorMessage(new ApiError(403, "nope"), FALLBACK);
    const serverError = panelErrorMessage(new ApiError(500, "nope"), FALLBACK);
    expect(forbidden).not.toBe(serverError);
  });

  it("drops the server detail for other statuses — only the caller's fallback remains", () => {
    const message = panelErrorMessage(new ApiError(400, "month must be YYYY-MM"), FALLBACK);
    expect(message).toBe(FALLBACK);
    expect(message).not.toContain("month must be YYYY-MM");
  });

  it("never leaks the server's own words, whatever the status", () => {
    for (const status of [400, 403, 404, 409, 500]) {
      const message = panelErrorMessage(new ApiError(status, "distinctive-server-detail-xyz"), FALLBACK);
      expect(message).not.toContain("distinctive-server-detail-xyz");
      expect(message).not.toContain("서버 응답");
    }
  });

  it("never returns an empty string, whatever it was handed", () => {
    // The whole point: something must appear on screen. A blank panel is
    // indistinguishable from "there is no data".
    for (const err of [
      new ApiError(403, ""),
      new ApiError(500, "   "),
      new Error(""),
      new TypeError("Failed to fetch"),
      "a string",
      null,
      undefined,
      { status: 403 },
    ]) {
      expect(panelErrorMessage(err, FALLBACK).length, String(err)).toBeGreaterThan(0);
    }
  });

  it("uses the fallback for a non-Error value", () => {
    expect(panelErrorMessage(null, FALLBACK)).toBe(FALLBACK);
    expect(panelErrorMessage({ status: 403 }, FALLBACK)).toBe(FALLBACK);
  });

  it("words a network failure in Korean, from the one shared sentence", () => {
    // `api/client.ts` now throws `ApiError` kind network for a dead server, so this is what a
    // panel meets. The expected text is a literal, not `NETWORK_ERROR_COPY`: a one-sided edit of
    // the production string must not move this test with it.
    const message = panelErrorMessage(new ApiError(0, "x", undefined, "network"), FALLBACK);

    expect(message).toBe("서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.");
    expect(message).not.toContain(FALLBACK);
  });

  it("words an unreadable 2xx body in Korean too", () => {
    expect(panelErrorMessage(new ApiError(200, "x", undefined, "parse"), FALLBACK)).toBe(
      "서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
  });

  it("never puts a library's English on screen: a raw TypeError gets the fallback only", () => {
    // This used to append the TypeError's message to the Korean copy.
    const message = panelErrorMessage(new TypeError("Failed to fetch"), FALLBACK);

    expect(message).toBe(FALLBACK);
    expect(message).not.toContain("Failed to fetch");
    expect(panelErrorMessage(new Error("Unexpected token < in JSON"), FALLBACK)).toBe(FALLBACK);
  });
});
