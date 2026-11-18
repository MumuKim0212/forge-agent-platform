/**
 * The approval console's decisions used to fold every failure into
 * one generic sentence per action (the backend's English `detail` was kept off the screen, and
 * threw the distinction away with it). A self-approval 403 and an already-decided 400 are different
 * things for an approver to do something about, so they are told apart by status, the way
 * `roleChangeFailureMessage` does it; the server's own words still never reach the screen.
 *
 * The old test here pinned a 403 "self-approval is not permitted" to the generic string. That rule
 * moved: a 403 now has its own sentence (below).
 */
import { describe, expect, it } from "vitest";

import { ApiError, NETWORK_ERROR_COPY, NETWORK_ERROR_STATUS } from "../api/client";
import {
  ALREADY_DECIDED_MESSAGE,
  CONFLICTING_DECISION_MESSAGE,
  FORBIDDEN_DECISION_MESSAGE,
  NOT_FOUND_DECISION_MESSAGE,
  approvalDecisionFailureMessage,
  approvalLoadFailureMessage,
  shouldRereadAfterDecisionFailure,
} from "./approvalMessages";

describe("approvalDecisionFailureMessage — each refusal has its own sentence", () => {
  it("pins the four sentences (the rest of this file reads against the constants)", () => {
    expect(FORBIDDEN_DECISION_MESSAGE).toBe(
      "이 요청은 직접 결정할 수 없습니다. 본인이 요청했거나 본인이 소유한 프로젝트의 요청은 다른 승인자가 결정해야 하고, approver 이상의 역할이 필요합니다.",
    );
    expect(ALREADY_DECIDED_MESSAGE).toBe(
      "이미 승인·반려되었거나 더 새로운 빌드로 대체된 요청이거나, 이미 승인한 요청입니다. 화면을 최신 상태로 다시 불러옵니다.",
    );
    expect(NOT_FOUND_DECISION_MESSAGE).toBe("요청을 찾을 수 없습니다. 목록을 새로 고친 뒤 다시 확인하세요.");
    expect(CONFLICTING_DECISION_MESSAGE).toBe(
      "다른 결정과 동시에 처리되었습니다. 화면을 최신 상태로 다시 불러옵니다.",
    );
  });

  it("a self-approval 403 is not the generic failure", () => {
    const err = new ApiError(403, "requester/project owner cannot approve their own request");
    const message = approvalDecisionFailureMessage(err, "approve");

    expect(message).toBe(FORBIDDEN_DECISION_MESSAGE);
    expect(message).not.toBe("승인에 실패했습니다.");
    expect(message).toContain("본인이 요청했거나");
  });

  it("an already-decided or superseded 400 says so and that the view is being re-read", () => {
    const err = new ApiError(400, "approval request r1 is already SUPERSEDED");
    const message = approvalDecisionFailureMessage(err, "approve");

    expect(message).toBe(ALREADY_DECIDED_MESSAGE);
    expect(message).toContain("대체된");
    expect(message).toContain("다시 불러옵니다");
  });

  it("a 404 and a 409 are their own sentences", () => {
    expect(approvalDecisionFailureMessage(new ApiError(404, "Approval request not found"), "reject")).toBe(
      NOT_FOUND_DECISION_MESSAGE,
    );
    expect(approvalDecisionFailureMessage(new ApiError(409, "clash"), "approve")).toBe(
      CONFLICTING_DECISION_MESSAGE,
    );
  });

  it("the four are four different sentences, none of them the generic one", () => {
    const messages = [403, 400, 404, 409].map((status) =>
      approvalDecisionFailureMessage(new ApiError(status, "x"), "approve"),
    );

    expect(new Set(messages).size).toBe(4);
    expect(messages).not.toContain("승인에 실패했습니다.");
  });

  it("any other status keeps each action's own fallback", () => {
    const err = new ApiError(500, "comment is required");

    expect(approvalDecisionFailureMessage(err, "approve")).toBe("승인에 실패했습니다.");
    expect(approvalDecisionFailureMessage(err, "reject")).toBe("반려에 실패했습니다.");
  });

  it("a failure that never reached the server says so", () => {
    const offline = new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network");

    expect(approvalDecisionFailureMessage(offline, "approve")).toBe(NETWORK_ERROR_COPY);
  });

  it("a non-ApiError gets the fallback", () => {
    expect(approvalDecisionFailureMessage(new Error("network down"), "approve")).toBe("승인에 실패했습니다.");
    expect(approvalDecisionFailureMessage("not even an Error", "reject")).toBe("반려에 실패했습니다.");
  });

  it("never leaks the server's own words, whatever the status", () => {
    for (const status of [400, 403, 404, 409, 500]) {
      const err = new ApiError(status, "distinctive-server-detail-xyz", "distinctive-server-detail-xyz");

      expect(approvalDecisionFailureMessage(err, "approve"), String(status)).not.toContain(
        "distinctive-server-detail-xyz",
      );
    }
  });
});

describe("shouldRereadAfterDecisionFailure — a stale request must not keep its buttons", () => {
  it("re-reads after a 400 or 409", () => {
    expect(shouldRereadAfterDecisionFailure(new ApiError(400, "x"))).toBe(true);
    expect(shouldRereadAfterDecisionFailure(new ApiError(409, "x"))).toBe(true);
  });

  it("does not after a 403, a 404, a 500 or a transport failure", () => {
    expect(shouldRereadAfterDecisionFailure(new ApiError(403, "x"))).toBe(false);
    expect(shouldRereadAfterDecisionFailure(new ApiError(404, "x"))).toBe(false);
    expect(shouldRereadAfterDecisionFailure(new ApiError(500, "x"))).toBe(false);
    expect(
      shouldRereadAfterDecisionFailure(new ApiError(NETWORK_ERROR_STATUS, "x", undefined, "network")),
    ).toBe(false);
    expect(shouldRereadAfterDecisionFailure(new Error("x"))).toBe(false);
  });
});

describe("approvalLoadFailureMessage — a failed read", () => {
  it("is the caller's fallback for a server answer, whatever the status or detail", () => {
    const err = new ApiError(500, "distinctive-server-detail-xyz");
    const message = approvalLoadFailureMessage(err, "요청 상세 정보를 불러오지 못했습니다.");

    expect(message).toBe("요청 상세 정보를 불러오지 못했습니다.");
    expect(message).not.toContain("distinctive-server-detail-xyz");
  });

  it("says the server could not be reached when it could not", () => {
    const offline = new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network");

    expect(approvalLoadFailureMessage(offline, "fallback")).toBe(NETWORK_ERROR_COPY);
  });

  it("does not map a status to a decision sentence: a read has no 'already decided'", () => {
    expect(approvalLoadFailureMessage(new ApiError(403, "x"), "fallback")).toBe("fallback");
    expect(approvalLoadFailureMessage(new ApiError(400, "x"), "fallback")).toBe("fallback");
  });
});
