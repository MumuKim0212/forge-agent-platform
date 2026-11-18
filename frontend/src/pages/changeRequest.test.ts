/**
 * A modify request needs a confirmed ref; the page disables the change-request form until
 * the project has one, and words the backend's `409 NO_CONFIRMED_REF` as the same reason.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import {
  NO_CONFIRMED_REF_CODE,
  NO_CONFIRMED_REF_MESSAGE,
  canSubmitChangeRequest,
  changeRequestBlockedMessage,
  hasConfirmedDeployment,
  isNoConfirmedRefRefusal,
  noConfirmedRefMessage,
} from "./changeRequest";
import { projectDetailFailureMessage } from "./ProjectDetailPage";

const REFUSED_DETAIL =
  "NO_CONFIRMED_REF: project p1 has no confirmed deployment yet, so there is nothing to modify; " +
  "confirm a first deployment (or submit a new create request) before sending a change request";

describe("the contract strings (pinned as literals)", () => {
  it("the refusal code is the backend's NO_CONFIRMED_REF_CODE", () => {
    expect(NO_CONFIRMED_REF_CODE).toBe("NO_CONFIRMED_REF");
  });

  it("the reason is one Korean line that names the next step", () => {
    expect(NO_CONFIRMED_REF_MESSAGE).toBe(
      "첫 배포를 확정한 뒤에 변경 요청을 보낼 수 있습니다. 배포 전 서비스는 새 요청으로 다시 만들어 주세요.",
    );
    expect(NO_CONFIRMED_REF_MESSAGE).not.toContain("\n");
  });
});

describe("hasConfirmedDeployment / canSubmitChangeRequest -- offered only with a confirmed ref", () => {
  it("is offered when the project has a confirmed ref", () => {
    expect(hasConfirmedDeployment({ confirmed_ref: "9fceb02" })).toBe(true);
    expect(canSubmitChangeRequest({ confirmed_ref: "9fceb02" })).toBe(true);
    expect(changeRequestBlockedMessage({ confirmed_ref: "9fceb02" })).toBeNull();
  });

  it("is not offered when it has none: null, empty, or absent", () => {
    for (const project of [{ confirmed_ref: null }, { confirmed_ref: "" }, {}]) {
      expect(hasConfirmedDeployment(project)).toBe(false);
      expect(canSubmitChangeRequest(project)).toBe(false);
      expect(changeRequestBlockedMessage(project)).toBe(NO_CONFIRMED_REF_MESSAGE);
    }
  });

  it("fails closed with no project", () => {
    expect(canSubmitChangeRequest(null)).toBe(false);
    expect(changeRequestBlockedMessage(null)).toBe(NO_CONFIRMED_REF_MESSAGE);
  });
});

describe("the 409 refusal is the same reason, and only that refusal", () => {
  it("a 409 whose detail starts with the code is worded as the reason", () => {
    const err = new ApiError(409, REFUSED_DETAIL, REFUSED_DETAIL);

    expect(isNoConfirmedRefRefusal(err)).toBe(true);
    expect(noConfirmedRefMessage(err)).toBe(NO_CONFIRMED_REF_MESSAGE);
    expect(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.")).toBe(NO_CONFIRMED_REF_MESSAGE);
  });

  it("the English detail never reaches the screen", () => {
    const err = new ApiError(409, REFUSED_DETAIL, REFUSED_DETAIL);

    expect(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.")).not.toContain("nothing to modify");
  });

  it("another 409 keeps the caller's fallback", () => {
    const err = new ApiError(409, "another deployment is in flight", "another deployment is in flight");

    expect(isNoConfirmedRefRefusal(err)).toBe(false);
    expect(noConfirmedRefMessage(err)).toBeNull();
    expect(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다.")).toBe("변경 요청 제출에 실패했습니다.");
  });

  it("the code under another status, a transport failure, or a plain Error is not this refusal", () => {
    expect(isNoConfirmedRefRefusal(new ApiError(400, REFUSED_DETAIL, REFUSED_DETAIL))).toBe(false);
    expect(isNoConfirmedRefRefusal(new ApiError(0, "network", undefined, "network"))).toBe(false);
    expect(isNoConfirmedRefRefusal(new Error(REFUSED_DETAIL))).toBe(false);
    expect(noConfirmedRefMessage("NO_CONFIRMED_REF: x")).toBeNull();
  });
});
