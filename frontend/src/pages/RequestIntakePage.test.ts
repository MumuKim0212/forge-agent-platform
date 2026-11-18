/**
 * `RequestIntakePage`'s submit handler used to render `err.message`
 * — supposedly a network-failure fallback, but `api/client.ts` always
 * throws `ApiError extends Error`, so the Korean fallback never actually
 * rendered. `requestIntakeFailureMessage` is the decision layer that
 * replaces it (ADR 0021: decision logic, not rendering).
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import {
  intakeLoadFailureMessage,
  intakeViewFor,
  isIntakeSubmitBlocked,
  requestIntakeFailureMessage,
} from "./RequestIntakePage";

describe("isIntakeSubmitBlocked — over the limit blocks submit, at the limit does not", () => {
  it("lets a request and name at exactly their limits through", () => {
    expect(isIntakeSubmitBlocked("이".repeat(100), "가".repeat(8000))).toBe(false);
    expect(isIntakeSubmitBlocked("이름", "짧은 요청")).toBe(false);
  });

  it("blocks when the request is one over, with a fine name", () => {
    expect(isIntakeSubmitBlocked("이름", "가".repeat(8001))).toBe(true);
  });

  it("blocks when the name is one over, with a fine request", () => {
    expect(isIntakeSubmitBlocked("이".repeat(101), "짧은 요청")).toBe(true);
  });

  it("does not block 8000 emoji, which the server accepts", () => {
    expect(isIntakeSubmitBlocked("이름", "😀".repeat(8000))).toBe(false);
  });
});

describe("requestIntakeFailureMessage — an over-long request or name gets its own message", () => {
  const overLong = (field: string) =>
    new ApiError(422, "[object Object]", [{ type: "string_too_long", loc: ["body", field] }]);

  it("says the request is too long, not the generic fallback", () => {
    const message = requestIntakeFailureMessage(overLong("nl_request"), "요청 제출에 실패했습니다.");

    expect(message).toContain("요청 내용이 너무 깁니다");
    expect(message).not.toBe("요청 제출에 실패했습니다.");
  });

  it("says the name is too long", () => {
    expect(requestIntakeFailureMessage(overLong("name"), "요청 제출에 실패했습니다.")).toContain(
      "프로젝트 이름이 너무 깁니다",
    );
  });

  it("keeps the fallback for any other 422", () => {
    const other = new ApiError(422, "x", [{ type: "missing", loc: ["body", "name"] }]);

    expect(requestIntakeFailureMessage(other, "요청 제출에 실패했습니다.")).toBe("요청 제출에 실패했습니다.");
  });
});

describe("requestIntakeFailureMessage — the backend's detail never reaches the screen", () => {
  it("returns the caller's Korean fallback for an ApiError, not its detail", () => {
    const err = new ApiError(404, "Template not found");
    const message = requestIntakeFailureMessage(err, "요청 제출에 실패했습니다.");

    expect(message).toBe("요청 제출에 실패했습니다.");
    expect(message).not.toContain("Template not found");
  });

  it("ignores a non-ApiError failure the same way", () => {
    expect(requestIntakeFailureMessage(new Error("network down"), "요청 제출에 실패했습니다.")).toBe(
      "요청 제출에 실패했습니다.",
    );
    expect(requestIntakeFailureMessage("not even an Error", "요청 제출에 실패했습니다.")).toBe(
      "요청 제출에 실패했습니다.",
    );
  });

  it("never leaks the server's own words, whatever the detail says", () => {
    const err = new ApiError(500, "distinctive-server-detail-xyz");

    expect(requestIntakeFailureMessage(err, "요청 제출에 실패했습니다.")).not.toContain(
      "distinctive-server-detail-xyz",
    );
  });
});

describe("intakeViewFor — the form never sits over empty selects", () => {
  const lists = (templates: number, teams: number) => ({
    templates: Array.from({ length: templates }, (_, i) => ({ id: `t${i}` })),
    teams: Array.from({ length: teams }, (_, i) => ({ id: `g${i}` })),
  });

  it("shows the form only when both lists have something to pick", () => {
    expect(intakeViewFor({ loading: false, error: null, lists: lists(2, 1) })).toBe("form");
  });

  it("says there are no templates when the template list is empty", () => {
    expect(intakeViewFor({ loading: false, error: null, lists: lists(0, 2) })).toBe("no-templates");
  });

  it("says there are no teams when only the team list is empty", () => {
    expect(intakeViewFor({ loading: false, error: null, lists: lists(3, 0) })).toBe("no-teams");
  });

  it("names the template gap first when both are empty", () => {
    expect(intakeViewFor({ loading: false, error: null, lists: lists(0, 0) })).toBe("no-templates");
  });

  it("is the error view when the load failed, even though there is no list to count", () => {
    expect(intakeViewFor({ loading: false, error: "x", lists: null })).toBe("error");
  });

  it("is loading while a load runs — including a retry after an error", () => {
    expect(intakeViewFor({ loading: true, error: null, lists: null })).toBe("loading");
    expect(intakeViewFor({ loading: true, error: null, lists: lists(2, 2) })).toBe("loading");
  });

  it("waits rather than showing the form when it has neither a result nor an error", () => {
    expect(intakeViewFor({ loading: false, error: null, lists: null })).toBe("loading");
  });
});

describe("intakeLoadFailureMessage", () => {
  it("tells a dead server in Korean, and otherwise gives the one plain sentence", () => {
    expect(intakeLoadFailureMessage(new ApiError(0, "x", undefined, "network"))).toBe(
      "서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.",
    );
    expect(intakeLoadFailureMessage(new ApiError(500, "internal server error"))).toBe(
      "템플릿과 팀 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
    expect(intakeLoadFailureMessage(new TypeError("Failed to fetch"))).toBe(
      "템플릿과 팀 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
  });
});
