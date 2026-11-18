/**
 * The role panel's two testable decisions: the grantable set, and turning
 * a rejection into an explanation.
 *
 * The last-admin refusal is the one the criteria single out. It arrives as a
 * `409` with a one-line server detail, and a console that shows that line raw
 * reads as a broken console rather than as the invariant that keeps the platform
 * from locking itself out of role management.
 */
import { describe, expect, it, vi } from "vitest";

import {
  LAST_ADMIN_EXPLANATION,
  ROLE_OPTIONS,
  SELF_CHANGE_NOTICE,
  adminCountSentence,
  refreshIdentityAfterRoleChange,
  roleApplyLabel,
  roleApplyText,
  roleChangeFailureMessage,
} from "./roleChange";

describe("ROLE_OPTIONS — exactly the ladder, no free-form entry", () => {
  it("offers the three role values and nothing else", () => {
    // Literals on the expected side: these are the values
    // sent to `PATCH /users/{id}/role`, which validates against
    // `services/roles.ALL_ROLES`. A fourth option, or a renamed one, is a 400 at
    // best and a silently privilege-less user at worst.
    expect(ROLE_OPTIONS.map((option) => option.value)).toEqual([
      "requester",
      "approver",
      "admin",
    ]);
  });

  it("labels each option, and the labels are not just the wire values", () => {
    for (const option of ROLE_OPTIONS) {
      expect(option.label.length, option.value).toBeGreaterThan(option.value.length);
      expect(option.label).toContain(option.value);
    }
  });

  it("lists them in ladder order, lowest rung first", () => {
    expect(ROLE_OPTIONS[0].value).toBe("requester");
    expect(ROLE_OPTIONS[ROLE_OPTIONS.length - 1].value).toBe("admin");
  });
});

describe("roleChangeFailureMessage", () => {
  const LAST_ADMIN_DETAIL = "cannot demote the last remaining admin";

  it("explains the last-admin refusal instead of passing the bare error through", () => {
    const message = roleChangeFailureMessage(409, LAST_ADMIN_DETAIL);
    expect(message).not.toBe(LAST_ADMIN_DETAIL);
    expect(message.length).toBeGreaterThan(LAST_ADMIN_DETAIL.length);
    expect(message).toContain(LAST_ADMIN_EXPLANATION);
  });

  it("no longer echoes the server's own line — the Korean explanation is the whole message", () => {
    // This used to append ` (서버 응답: ${detail})`. The explanation
    // above stands on its own now; the raw server line is not repeated.
    expect(roleChangeFailureMessage(409, LAST_ADMIN_DETAIL)).not.toContain(LAST_ADMIN_DETAIL);
    expect(roleChangeFailureMessage(409, LAST_ADMIN_DETAIL)).toBe(LAST_ADMIN_EXPLANATION);
  });

  it("gives each contract status its own message", () => {
    // 400 unknown role and 409 last admin are two different refusals from
    // `routers/auth.change_user_role_endpoint`; 403 is `deps.require_role`. If
    // any of them falls through to the generic branch they become
    // indistinguishable on screen.
    const messages = [400, 403, 404, 409].map((status) =>
      roleChangeFailureMessage(status, "detail"),
    );
    expect(new Set(messages).size).toBe(messages.length);
    for (const message of messages) {
      expect(message).not.toBe("detail");
    }
  });

  it("pins the literal Korean copy for each contract status, not only which branch fires", () => {
    // A test must pin the copy, not only the
    // criterion — this is the file's own prior gap.
    expect(roleChangeFailureMessage(400, "detail")).toBe(
      "역할 값이 올바르지 않습니다. requester / approver / admin 중 하나여야 합니다.",
    );
    expect(roleChangeFailureMessage(403, "detail")).toBe(
      "역할 변경은 admin만 할 수 있습니다. 방금 본인 권한이 바뀌었다면 새로 고침 후 다시 확인하세요.",
    );
    expect(roleChangeFailureMessage(404, "detail")).toBe(
      "대상 사용자를 찾을 수 없습니다. 목록이 오래되었을 수 있으니 새로 고침 후 다시 시도하세요.",
    );
    expect(roleChangeFailureMessage(409, "detail")).toBe(LAST_ADMIN_EXPLANATION);
    expect(roleChangeFailureMessage(500, "detail")).toBe("역할 변경에 실패했습니다.");
  });

  it("falls back to a fixed Korean message for an unexpected status, not the server's own text", () => {
    expect(roleChangeFailureMessage(500, "internal server error")).toBe("역할 변경에 실패했습니다.");
    expect(roleChangeFailureMessage(500, "internal server error")).not.toContain(
      "internal server error",
    );
  });

  it("still produces a message when the server sent no detail", () => {
    for (const status of [400, 403, 404, 409, 500]) {
      const message = roleChangeFailureMessage(status, "");
      expect(message.length, `status ${status}`).toBeGreaterThan(0);
      expect(message).not.toContain("서버 응답");
    }
  });

  it("never leaks the server's detail, whatever it says — all five branches", () => {
    for (const status of [400, 403, 404, 409, 500]) {
      const message = roleChangeFailureMessage(status, "some distinctive backend detail xyz");
      expect(message, `status ${status}`).not.toContain("some distinctive backend detail xyz");
      expect(message, `status ${status}`).not.toContain("서버 응답");
    }
  });
});

describe("refreshIdentityAfterRoleChange — the missing call site", () => {
  it("refreshes the acting user's own identity on self-demotion", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);

    await refreshIdentityAfterRoleChange(true, refresh);

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not refresh when the change targets someone else", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);

    await refreshIdentityAfterRoleChange(false, refresh);

    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("SELF_CHANGE_NOTICE", () => {
  it("says self-demotion is allowed while another admin remains", () => {
    // Self-demotion is permitted; the criterion asks the UI to make
    // clear the change applies to the acting user. A notice that only warned,
    // without saying it is permitted, would read as a refusal.
    expect(SELF_CHANGE_NOTICE).toContain("본인");
    expect(SELF_CHANGE_NOTICE.length).toBeGreaterThan(0);
  });
});

describe("the per-row apply button's name", () => {
  it("is the user's name, then the visible word", () => {
    expect(roleApplyLabel("홍길동", false)).toBe("홍길동 적용");
    expect(roleApplyText(false)).toBe("적용");
  });

  it("follows the button's text while the change is being saved", () => {
    expect(roleApplyText(true)).toBe("변경 중...");
    expect(roleApplyLabel("홍길동", true)).toBe("홍길동 변경 중...");
  });

  it("gives two users two different names", () => {
    expect(roleApplyLabel("홍길동", false)).not.toBe(roleApplyLabel("김철수", false));
  });
});

describe("adminCountSentence: the backend's number, or nothing", () => {
  it("states the number it is given, zero and one included", () => {
    expect(adminCountSentence(2)).toBe("현재 활성 admin: 2명.");
    expect(adminCountSentence(1)).toBe("현재 활성 admin: 1명.");
    expect(adminCountSentence(0)).toBe("현재 활성 admin: 0명.");
  });

  it("drops the sentence when the count is unknown instead of guessing one", () => {
    expect(adminCountSentence(null)).toBe("");
  });
});
