/**
 * The signup-approval panel's transition table (ADR 0021: decision logic,
 * not rendering). Mirrors backend `services/signup.py`'s transition rules
 * so the panel never offers a
 * button whose transition the backend would reject as illegal — though the
 * backend stays the enforcement point regardless (every one of these still
 * has a 400/409 behind it).
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { LAST_ADMIN_EXPLANATION } from "./roleChange";
import {
  AccountStatus,
  accountStatusLabel,
  canApprove,
  canDeactivate,
  canReject,
  canReopen,
  signupActionFailureMessage,
} from "./signupRequests";

describe("AccountStatus is a cross-service contract", () => {
  it("matches the strings backend services/account_status.py sends", () => {
    // Pinned as a literal, the same discipline `auth/roles.test.ts` uses for
    // `Role` — every other test in this file feeds `AccountStatus.*` in as
    // an *input*, so this is the one assertion that would catch a rename.
    // Unlike `Role`, these are UPPER_SNAKE (`role` alone stayed lowercase).
    expect(AccountStatus).toEqual({
      PENDING: "PENDING",
      ACTIVE: "ACTIVE",
      REJECTED: "REJECTED",
    });
  });
});

const ALL_STATUSES = [AccountStatus.PENDING, AccountStatus.ACTIVE, AccountStatus.REJECTED];
const UNRECOGNISED = ["pending", "Active", "CANCELLED", "", "REJECTED "];

describe("canApprove — PENDING or REJECTED only", () => {
  it("matches services/signup.approve_signup's guard", () => {
    expect(canApprove(AccountStatus.PENDING)).toBe(true);
    expect(canApprove(AccountStatus.REJECTED)).toBe(true);
    expect(canApprove(AccountStatus.ACTIVE)).toBe(false);
  });

  it("fail-closed: an unrecognised status is never approvable", () => {
    for (const status of UNRECOGNISED) {
      expect(canApprove(status)).toBe(false);
    }
  });
});

describe("canReject — PENDING only", () => {
  it("matches services/signup.reject_signup's guard", () => {
    expect(canReject(AccountStatus.PENDING)).toBe(true);
    expect(canReject(AccountStatus.REJECTED)).toBe(false);
    expect(canReject(AccountStatus.ACTIVE)).toBe(false);
  });

  it("fail-closed: an unrecognised status is never rejectable", () => {
    for (const status of UNRECOGNISED) {
      expect(canReject(status)).toBe(false);
    }
  });
});

describe("canReopen — REJECTED only", () => {
  it("matches services/signup.reopen_signup's guard", () => {
    expect(canReopen(AccountStatus.REJECTED)).toBe(true);
    expect(canReopen(AccountStatus.PENDING)).toBe(false);
    expect(canReopen(AccountStatus.ACTIVE)).toBe(false);
  });

  it("fail-closed: an unrecognised status is never reopenable", () => {
    for (const status of UNRECOGNISED) {
      expect(canReopen(status)).toBe(false);
    }
  });
});

describe("canDeactivate — ACTIVE only", () => {
  it("matches services/signup.deactivate_user's guard", () => {
    expect(canDeactivate(AccountStatus.ACTIVE)).toBe(true);
    expect(canDeactivate(AccountStatus.PENDING)).toBe(false);
    expect(canDeactivate(AccountStatus.REJECTED)).toBe(false);
  });

  it("fail-closed: an unrecognised status is never deactivatable", () => {
    for (const status of UNRECOGNISED) {
      expect(canDeactivate(status)).toBe(false);
    }
  });
});

describe("exactly one action is legal per status — no overlap, no gap", () => {
  it("each real status has exactly one of approve/reject/reopen/deactivate available on its own axis", () => {
    // Not literally mutually exclusive as a group (approve and reject both
    // hold for PENDING, by design), but every status has *some* legal
    // transition and REJECTED/ACTIVE each have exactly the ones the
    // transition table names.
    expect([canApprove(AccountStatus.PENDING), canReject(AccountStatus.PENDING)]).toEqual([
      true,
      true,
    ]);
    expect([
      canApprove(AccountStatus.REJECTED),
      canReject(AccountStatus.REJECTED),
      canReopen(AccountStatus.REJECTED),
      canDeactivate(AccountStatus.REJECTED),
    ]).toEqual([true, false, true, false]);
    expect([
      canApprove(AccountStatus.ACTIVE),
      canReject(AccountStatus.ACTIVE),
      canReopen(AccountStatus.ACTIVE),
      canDeactivate(AccountStatus.ACTIVE),
    ]).toEqual([false, false, false, true]);
  });

  it("every real status is covered by ALL_STATUSES in this test — guards against a silent 4th value", () => {
    expect(ALL_STATUSES).toEqual(["PENDING", "ACTIVE", "REJECTED"]);
  });
});

describe("accountStatusLabel", () => {
  it("labels every known status", () => {
    expect(accountStatusLabel(AccountStatus.PENDING)).toBe("대기중");
    expect(accountStatusLabel(AccountStatus.ACTIVE)).toBe("활성");
    expect(accountStatusLabel(AccountStatus.REJECTED)).toBe("거부됨");
  });

  it("fail-closed: an unrecognised status names itself rather than borrowing another label", () => {
    for (const status of UNRECOGNISED) {
      const label = accountStatusLabel(status);
      expect(label).not.toBe("대기중");
      expect(label).not.toBe("활성");
      expect(label).not.toBe("거부됨");
      expect(label).toContain(status);
    }
  });
});

describe("signupActionFailureMessage", () => {
  it("409 reuses the role-change panel's exact last-admin explanation, not a second voice", () => {
    const message = signupActionFailureMessage(new ApiError(409, "cannot deactivate the last admin"), "fallback");

    expect(message).toContain(LAST_ADMIN_EXPLANATION);
    expect(message).toContain("cannot deactivate the last admin");
  });

  it("400/403/404 each get their own message, none collapsing into a generic one", () => {
    const messages = [400, 403, 404].map(
      (status) => signupActionFailureMessage(new ApiError(status, "server detail"), "fallback"),
    );

    expect(new Set(messages).size).toBe(3);
    for (const message of messages) {
      expect(message).toContain("server detail");
    }
  });

  it("falls back to the caller's message for any other status, keeping the server detail", () => {
    expect(signupActionFailureMessage(new ApiError(500, "boom"), "요청을 처리하지 못했습니다.")).toBe(
      "요청을 처리하지 못했습니다. (서버 응답: boom)",
    );
  });

  it("still produces a message when the server sent no detail", () => {
    expect(signupActionFailureMessage(new ApiError(500, ""), "fallback")).toBe("fallback");
    expect(signupActionFailureMessage(new ApiError(409, ""), "fallback")).toBe(LAST_ADMIN_EXPLANATION);
  });

  it("never appends a library's English to the fallback: a non-ApiError gets the fallback only", () => {
    expect(signupActionFailureMessage(new Error("network down"), "fallback")).toBe("fallback");
    expect(signupActionFailureMessage(new TypeError("Failed to fetch"), "fallback")).toBe("fallback");
    expect(signupActionFailureMessage("not even an Error", "fallback")).toBe("fallback");
  });

  it("words a transport failure in Korean and quotes no server text", () => {
    // The expected strings are literals; see `panelError.test.ts` for why.
    expect(signupActionFailureMessage(new ApiError(0, "x", undefined, "network"), "fallback")).toBe(
      "서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.",
    );
    expect(signupActionFailureMessage(new ApiError(200, "x", undefined, "parse"), "fallback")).toBe(
      "서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
  });
});
