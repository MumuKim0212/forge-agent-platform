/**
 * `ProjectDetailPage`'s deploy controls (confirm / rollback / stop)
 * and change-request form used a helper (`deployErrorMessage`) and an
 * inline ternary that both read `err.message` as though it were an
 * unreachable network-failure fallback — but `api/client.ts` always throws
 * `ApiError extends Error`, so the backend's `detail` reached the screen on
 * every rejection. `projectDetailFailureMessage` (hoisted to module scope,
 * ADR 0021) replaces both.
 *
 * the three deploy controls moved on to `deployActionFailureMessage`
 * (`deployControls.test.ts`, which tells the coded refusals apart); this function still words the
 * page's own load failure and the change-request form, which is what these tests now cover.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { isChangeRequestSubmitBlocked, projectDetailFailureMessage } from "./ProjectDetailPage";

describe("isChangeRequestSubmitBlocked — over the limit blocks submit, at the limit does not", () => {
  it("lets exactly the limit through and blocks one over", () => {
    expect(isChangeRequestSubmitBlocked("가".repeat(8000))).toBe(false);
    expect(isChangeRequestSubmitBlocked("가".repeat(8001))).toBe(true);
  });

  it("counts code points: 8000 emoji pass, 8001 block", () => {
    expect(isChangeRequestSubmitBlocked("😀".repeat(8000))).toBe(false);
    expect(isChangeRequestSubmitBlocked("😀".repeat(8001))).toBe(true);
  });

  it("does not block an empty box (the required attribute covers that)", () => {
    expect(isChangeRequestSubmitBlocked("")).toBe(false);
  });
});

describe("projectDetailFailureMessage — an over-long change request gets its own message", () => {
  it("says the request is too long, not the change-request fallback", () => {
    const err = new ApiError(422, "[object Object]", [
      { type: "string_too_long", loc: ["body", "nl_request"] },
    ]);
    const message = projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.");

    expect(message).toContain("요청 내용이 너무 깁니다");
    expect(message).not.toBe("변경 요청 제출에 실패했습니다.");
  });

  it("keeps the fallback for any other 422", () => {
    const err = new ApiError(422, "x", [{ type: "missing", loc: ["body", "nl_request"] }]);

    expect(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.")).toBe(
      "변경 요청 제출에 실패했습니다.",
    );
  });
});

describe("projectDetailFailureMessage — the backend's detail never reaches the screen", () => {
  it("returns the caller's Korean fallback for an ApiError, not err.message", () => {
    const err = new ApiError(409, "cannot roll back: no previous deployment");
    const message = projectDetailFailureMessage(err, "롤백에 실패했습니다.");

    expect(message).toBe("롤백에 실패했습니다.");
    expect(message).not.toContain("cannot roll back: no previous deployment");
  });

  it("keeps each deploy-control call site's fallback distinguishable", () => {
    const err = new ApiError(403, "Project owner or admin role required");

    expect(projectDetailFailureMessage(err, "배포 확정에 실패했습니다.")).toBe("배포 확정에 실패했습니다.");
    expect(projectDetailFailureMessage(err, "롤백에 실패했습니다.")).toBe("롤백에 실패했습니다.");
    expect(projectDetailFailureMessage(err, "중지에 실패했습니다.")).toBe("중지에 실패했습니다.");
  });

  it("covers the change-request form's fallback the same way", () => {
    const err = new ApiError(400, "nl_request must not be empty");

    expect(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.")).toBe(
      "변경 요청 제출에 실패했습니다.",
    );
  });

  it("ignores a non-ApiError failure the same way", () => {
    expect(projectDetailFailureMessage(new Error("network down"), "배포 확정에 실패했습니다.")).toBe(
      "배포 확정에 실패했습니다.",
    );
  });

  it("never leaks the server's own words, whatever the detail says", () => {
    const err = new ApiError(500, "distinctive-server-detail-xyz");

    expect(projectDetailFailureMessage(err, "중지에 실패했습니다.")).not.toContain(
      "distinctive-server-detail-xyz",
    );
  });
});
