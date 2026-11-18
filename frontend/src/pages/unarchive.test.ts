/**
 * The project page's un-archive control, as named decisions (ADR 0021): who is offered it
 * (a truth table over viewer x project status), what each failure says, when a refusal re-reads,
 * and the one thing the confirmation must not get wrong.
 *
 * "ARCHIVED" is a contract string with the backend (`ProjectStatus.ARCHIVED`), so it is a literal
 * here; the viewer's role names are literals too. The owner-or-admin rule itself is
 * `canOperateDeployment` (`auth/roles.test.ts` pins it); what is tested here is that the control
 * composes it, so a drop of the owner condition from the predicate goes red.
 */
import { describe, expect, it } from "vitest";

import { ApiError, NETWORK_ERROR_COPY, NETWORK_ERROR_STATUS, PARSE_ERROR_COPY } from "../api/client";
import type { User } from "../types/api";
import {
  ARCHIVED_PROJECT_STATUS,
  UNARCHIVED_NOTICE,
  UNARCHIVE_CONFIRMATION,
  canUnarchiveProject,
  failureAfterControlWithdrawn,
  isProjectArchived,
  shouldDismissConfirmation,
  shouldRefreshAfterUnarchiveFailure,
  unarchiveFailureMessage,
} from "./unarchive";

function user(id: string, role: string): User {
  return { id, name: id, email: `${id}@example.com`, role, team_id: null, created_at: "2026-10-17T00:00:00Z" };
}

const OWNER = user("user-owner", "requester");
const OTHER = user("user-other", "requester");
const APPROVER = user("user-approver", "approver");
const ADMIN = user("user-admin", "admin");

function project(status: string, ownerId = OWNER.id) {
  return { owner_id: ownerId, status };
}

/** Sentences as a reader meets them: split after `.`, `!` or `?` followed by whitespace. */
function sentences(text: string): string[] {
  return text.split(/(?<=[.!?])\s+/).filter((s) => s.length > 0);
}

describe("the status word is the backend's, spelled out", () => {
  it("pins ARCHIVED (a one-sided rename would make the control silently never show)", () => {
    expect(ARCHIVED_PROJECT_STATUS).toBe("ARCHIVED");
  });

  it("isProjectArchived: only that value, exactly", () => {
    expect(isProjectArchived({ status: "ARCHIVED" })).toBe(true);
    for (const status of ["ACTIVE", "DRAFT", "SUBMITTED", "BUILD_PENDING", "PREVIEW_READY", "AWAITING_APPROVAL", "archived", "ARCHIVED ", ""]) {
      expect(isProjectArchived({ status }), status).toBe(false);
    }
    expect(isProjectArchived(null)).toBe(false);
  });
});

describe("canUnarchiveProject: archived AND (owner or admin)", () => {
  // viewer x archived? -> offered. Every row is one cell of the table.
  const TABLE: Array<[string, User, string, boolean]> = [
    ["owner, archived", OWNER, "ARCHIVED", true],
    ["admin who is not the owner, archived", ADMIN, "ARCHIVED", true],
    ["another requester, archived", OTHER, "ARCHIVED", false],
    ["an approver who is not the owner, archived", APPROVER, "ARCHIVED", false],
    ["owner, active", OWNER, "ACTIVE", false],
    ["admin, active", ADMIN, "ACTIVE", false],
    ["another requester, active", OTHER, "ACTIVE", false],
    ["approver, active", APPROVER, "ACTIVE", false],
    ["owner, draft", OWNER, "DRAFT", false],
    ["admin, awaiting approval", ADMIN, "AWAITING_APPROVAL", false],
  ];

  it.each(TABLE)("%s", (_name, viewer, status, expected) => {
    expect(canUnarchiveProject(viewer, project(status))).toBe(expected);
  });

  it("an owner who is also an approver is offered it (ownership is its own axis)", () => {
    const ownerApprover = user(OWNER.id, "approver");
    expect(canUnarchiveProject(ownerApprover, project("ARCHIVED"))).toBe(true);
  });

  it("an approver does NOT override ownership: an approver who is not the owner is refused", () => {
    expect(canUnarchiveProject(APPROVER, project("ARCHIVED"))).toBe(false);
  });

  it("fails closed on a missing viewer or project, and on an unrecognised role", () => {
    expect(canUnarchiveProject(null, project("ARCHIVED"))).toBe(false);
    expect(canUnarchiveProject(OWNER, null)).toBe(false);
    expect(canUnarchiveProject(null, null)).toBe(false);
    expect(canUnarchiveProject(user("someone", "superuser"), project("ARCHIVED"))).toBe(false);
    expect(canUnarchiveProject(user("someone", ""), project("ARCHIVED"))).toBe(false);
  });

  it("the owner is matched by exact id, not by name, prefix or case", () => {
    expect(canUnarchiveProject(user("USER-OWNER", "requester"), project("ARCHIVED"))).toBe(false);
    expect(canUnarchiveProject(user("user-owne", "requester"), project("ARCHIVED"))).toBe(false);
  });
});

describe("unarchiveFailureMessage: one sentence per status, never the server's English", () => {
  const http = (status: number, detail = "Project is not archived") => new ApiError(status, detail, detail);

  it("words each status the route answers, each its own sentence", () => {
    const sentences = [400, 401, 403, 404].map((status) => unarchiveFailureMessage(http(status)));

    expect(new Set(sentences).size).toBe(4);
    expect(sentences[0]).toContain("이미 보관이 해제");
    expect(sentences[1]).toContain("로그인");
    expect(sentences[2]).toContain("소유자 또는 admin");
    expect(sentences[3]).toContain("찾을 수 없");
  });

  it("a 5xx is the server's fault and is told apart from the fallback", () => {
    const fallback = unarchiveFailureMessage(http(418));
    const server = unarchiveFailureMessage(http(500));

    expect(server).toBe(unarchiveFailureMessage(http(503)));
    expect(server).not.toBe(fallback);
    expect(server).toContain("서버");
  });

  it("any other status gets the action's own fallback", () => {
    expect(unarchiveFailureMessage(http(409))).toBe("보관 해제에 실패했습니다.");
    expect(unarchiveFailureMessage(http(422))).toBe("보관 해제에 실패했습니다.");
    expect(unarchiveFailureMessage(new Error("boom"))).toBe("보관 해제에 실패했습니다.");
    expect(unarchiveFailureMessage("boom")).toBe("보관 해제에 실패했습니다.");
    expect(unarchiveFailureMessage(undefined)).toBe("보관 해제에 실패했습니다.");
  });

  it("a failure that never reached the server says so, whatever status the error carries", () => {
    expect(unarchiveFailureMessage(new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network"))).toBe(
      NETWORK_ERROR_COPY,
    );
    expect(unarchiveFailureMessage(new ApiError(200, PARSE_ERROR_COPY, undefined, "parse"))).toBe(PARSE_ERROR_COPY);
  });

  it("never shows the backend's English detail", () => {
    for (const status of [400, 401, 403, 404, 409, 500]) {
      expect(unarchiveFailureMessage(http(status, "distinctive-server-detail-xyz")), String(status)).not.toContain(
        "distinctive-server-detail-xyz",
      );
    }
  });
});

describe("shouldRefreshAfterUnarchiveFailure: only a refusal that says the page is stale", () => {
  const http = (status: number) => new ApiError(status, "x", "x");

  it("re-reads on 400 (already un-archived) and 403 (owner or role changed)", () => {
    expect(shouldRefreshAfterUnarchiveFailure(http(400))).toBe(true);
    expect(shouldRefreshAfterUnarchiveFailure(http(403))).toBe(true);
  });

  it("does not re-read on anything else", () => {
    for (const status of [401, 404, 409, 500, 503]) {
      expect(shouldRefreshAfterUnarchiveFailure(http(status)), String(status)).toBe(false);
    }
    expect(
      shouldRefreshAfterUnarchiveFailure(new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network")),
    ).toBe(false);
    expect(shouldRefreshAfterUnarchiveFailure(new Error("x"))).toBe(false);
  });

  it("a parse failure with a 400-looking status is not a refusal", () => {
    expect(shouldRefreshAfterUnarchiveFailure(new ApiError(400, "x", undefined, "parse"))).toBe(false);
  });
});

describe("shouldDismissConfirmation: Escape leaves the box unless the action is running", () => {
  it("Escape dismisses an idle confirmation", () => {
    expect(shouldDismissConfirmation("Escape", false)).toBe(true);
  });

  it("not while the request is in flight", () => {
    expect(shouldDismissConfirmation("Escape", true)).toBe(false);
  });

  it("no other key dismisses it", () => {
    for (const key of ["Enter", " ", "Tab", "Esc", "escape", "a"]) {
      expect(shouldDismissConfirmation(key, false), key).toBe(false);
    }
  });
});

describe("the confirmation says what un-archiving does and does not do", () => {
  it("states that nothing is restored and the service is not restarted", () => {
    expect(UNARCHIVE_CONFIRMATION.doesNot).toContain("복원되는 것은 없");
    expect(UNARCHIVE_CONFIRMATION.doesNot).toContain("자동으로 다시 시작되지");
  });

  it("states the data caveat as a CONDITION, never as a fact about this project", () => {
    // The project read does not say whether it was deprovisioned, so the sentence must
    // not claim it. It names the condition ("already deleted") and then what a later deploy does.
    expect(UNARCHIVE_CONFIRMATION.dataCondition).toContain("이미 삭제했다면");
    expect(UNARCHIVE_CONFIRMATION.dataCondition).toContain("비어 있는 새 데이터베이스");
  });

  it("the data caveat is ONE sentence: the empty-database consequence is never a sentence of its own", () => {
    // Read alone, "이후 배포하면 비어 있는 새 데이터베이스가 만들어집니다" is false for a project that
    // was never deprovisioned. As the second half of the one conditional sentence it cannot be read
    // alone.
    const text = UNARCHIVE_CONFIRMATION.dataCondition;
    expect(sentences(text)).toHaveLength(1);
    const [only] = sentences(text);
    expect(only.indexOf("이미 삭제했다면")).toBeGreaterThanOrEqual(0);
    expect(only.indexOf("이미 삭제했다면")).toBeLessThan(only.indexOf("비어 있는 새 데이터베이스"));
  });

  it("no sentence anywhere in the confirmation or the notice speaks of the data without its condition", () => {
    const everything = [
      UNARCHIVE_CONFIRMATION.title,
      UNARCHIVE_CONFIRMATION.does,
      UNARCHIVE_CONFIRMATION.doesNot,
      UNARCHIVE_CONFIRMATION.dataCondition,
      UNARCHIVED_NOTICE,
    ].flatMap(sentences);

    const aboutData = everything.filter((s) => s.includes("데이터"));

    // Anti-vacuity: the caveat is in there, and it is found by this filter.
    expect(aboutData.length).toBeGreaterThan(0);
    for (const sentence of aboutData) {
      expect(sentence, sentence).toContain("했다면");
    }
  });

  it("the buttons and the success notice are distinct words", () => {
    const words = [UNARCHIVE_CONFIRMATION.confirm, UNARCHIVE_CONFIRMATION.cancel, UNARCHIVE_CONFIRMATION.busy];
    expect(new Set(words).size).toBe(3);
    expect(UNARCHIVED_NOTICE).toContain("보관을 해제했습니다");
  });
});

describe("what stays of a failed un-archive's alert when the control stops being offered", () => {
  it("nothing stays when there was no failure", () => {
    expect(failureAfterControlWithdrawn(null)).toBeNull();
  });

  it("a failure that did not make the control go is dropped: it was about a control that is not there now", () => {
    expect(failureAfterControlWithdrawn({ message: "서버 오류", explainsWithdrawal: false })).toBeNull();
  });

  it("the refusal that made the control go stays, unchanged: it says why the control is gone", () => {
    const refusal = { message: "이미 보관이 해제된 프로젝트입니다.", explainsWithdrawal: true };

    expect(failureAfterControlWithdrawn(refusal)).toBe(refusal);
  });
});
