/**
 * Who may decide an approval request, as truth tables (ADR 0021). The self-approval ban is
 * the part the console used to leave to the backend's 403: it bars the requester AND the project's
 * owner, with no admin exception (backend `approval._is_self_approval`).
 * The status words and the decision word are contract strings with the
 * backend, so they are literals here.
 */
import { describe, expect, it } from "vitest";

import type { User } from "../types/api";
import {
  APPROVE_DECISION,
  PENDING_APPROVAL_STATUS,
  REJECT_DECISION,
  approveBlock,
  canApproveRequest,
  canRejectRequest,
  decisionBlockMessage,
  isApprovalPending,
  rejectBlock,
  type DecidableRequest,
  type DecisionBlock,
} from "./approvalGate";

const REQUESTER = "user-requester";
const OWNER = "user-owner";
const APPROVER_A = "user-approver-a";
const APPROVER_B = "user-approver-b";
const ADMIN = "user-admin";

function user(id: string, role: string): User {
  return { id, name: id, email: `${id}@example.com`, role, team_id: null, created_at: "2026-10-17T00:00:00Z" };
}

function request(overrides: Partial<DecidableRequest> = {}): DecidableRequest {
  return {
    status: "PENDING",
    requester_id: REQUESTER,
    project_owner_id: OWNER,
    decisions: [],
    ...overrides,
  };
}

const approvedBy = (actorId: string) => ({ actor_id: actorId, decision: "approve" });

describe("the PENDING status is the backend's word", () => {
  it("pins the literal", () => {
    expect(PENDING_APPROVAL_STATUS).toBe("PENDING");
  });

  it("isApprovalPending is true for PENDING and for nothing else the backend sends", () => {
    expect(isApprovalPending("PENDING")).toBe(true);
    for (const status of ["APPROVED", "REJECTED", "AUTO_APPROVED", "SUPERSEDED", "pending", "", null, undefined, 1]) {
      expect(isApprovalPending(status), String(status)).toBe(false);
    }
  });
});

describe("the decision words are the backend's", () => {
  it("pins both literals (`ApprovalDecision.decision`); the display labels and the one-approval gate key off them", () => {
    expect(APPROVE_DECISION).toBe("approve");
    expect(REJECT_DECISION).toBe("reject");
  });
});

describe("canApproveRequest — the approver who is neither party nor already counted", () => {
  it("another approver may approve", () => {
    expect(canApproveRequest(user(APPROVER_A, "approver"), request())).toBe(true);
  });

  it("an admin who is neither the requester nor the owner may approve", () => {
    expect(canApproveRequest(user(ADMIN, "admin"), request())).toBe(true);
  });

  it("the requester may not, even holding the approver role", () => {
    expect(canApproveRequest(user(REQUESTER, "approver"), request())).toBe(false);
  });

  it("a requester who is an admin may not: the ban has no admin exception", () => {
    expect(canApproveRequest(user(REQUESTER, "admin"), request({ requester_id: REQUESTER }))).toBe(false);
    expect(canApproveRequest(user(ADMIN, "admin"), request({ requester_id: ADMIN }))).toBe(false);
  });

  it("the project's owner may not although they did not request the change", () => {
    expect(canApproveRequest(user(OWNER, "approver"), request())).toBe(false);
    expect(canApproveRequest(user(OWNER, "admin"), request())).toBe(false);
  });

  it("an approver who already approved may not approve again", () => {
    const pendingWithOneApproval = request({ decisions: [approvedBy(APPROVER_A)] });

    expect(canApproveRequest(user(APPROVER_A, "approver"), pendingWithOneApproval)).toBe(false);
    // ... while a different approver still may (a two-person rule needs two people).
    expect(canApproveRequest(user(APPROVER_B, "approver"), pendingWithOneApproval)).toBe(true);
  });

  it("someone else's approval does not count against me, and a reject by me is not an approval", () => {
    expect(canApproveRequest(user(APPROVER_B, "approver"), request({ decisions: [approvedBy(APPROVER_A)] }))).toBe(true);
    expect(
      canApproveRequest(
        user(APPROVER_A, "approver"),
        request({ decisions: [{ actor_id: APPROVER_A, decision: "reject" }] }),
      ),
    ).toBe(true);
  });

  it("a requester-role user may not (the approver rung)", () => {
    expect(canApproveRequest(user("user-plain", "requester"), request())).toBe(false);
  });

  it("an unrecognised role fails closed", () => {
    expect(canApproveRequest(user(APPROVER_A, "superuser"), request())).toBe(false);
    expect(canApproveRequest(user(APPROVER_A, ""), request())).toBe(false);
  });

  it("no user, or no request, fails closed", () => {
    expect(canApproveRequest(null, request())).toBe(false);
    expect(canApproveRequest(user(APPROVER_A, "approver"), null)).toBe(false);
  });

  it("only a PENDING request takes a decision", () => {
    for (const status of ["APPROVED", "REJECTED", "AUTO_APPROVED", "SUPERSEDED"]) {
      expect(canApproveRequest(user(APPROVER_A, "approver"), request({ status })), status).toBe(false);
    }
  });

  it("fails closed when the project (so its owner) is unknown", () => {
    expect(canApproveRequest(user(APPROVER_A, "approver"), request({ project_owner_id: null }))).toBe(false);
    expect(canApproveRequest(user(APPROVER_A, "approver"), request({ project_owner_id: "" }))).toBe(false);
  });
});

describe("canRejectRequest — the same bars, except having approved already", () => {
  it("another approver may reject", () => {
    expect(canRejectRequest(user(APPROVER_A, "approver"), request())).toBe(true);
  });

  it("an approver who already approved may still reject (the backend only counts approvals once)", () => {
    expect(canRejectRequest(user(APPROVER_A, "approver"), request({ decisions: [approvedBy(APPROVER_A)] }))).toBe(true);
  });

  it("the requester and the owner may not reject their own request either", () => {
    expect(canRejectRequest(user(REQUESTER, "admin"), request())).toBe(false);
    expect(canRejectRequest(user(OWNER, "approver"), request())).toBe(false);
  });

  it("a requester-role user, a decided request, a missing owner and no user all fail closed", () => {
    expect(canRejectRequest(user("user-plain", "requester"), request())).toBe(false);
    expect(canRejectRequest(user(APPROVER_A, "approver"), request({ status: "REJECTED" }))).toBe(false);
    expect(canRejectRequest(user(APPROVER_A, "approver"), request({ project_owner_id: null }))).toBe(false);
    expect(canRejectRequest(null, request())).toBe(false);
  });
});

describe("approveBlock / rejectBlock — the reason, for the words next to the missing buttons", () => {
  const cases: Array<[string, User | null, DecidableRequest | null, DecisionBlock | null, DecisionBlock | null]> = [
    ["another approver", user(APPROVER_A, "approver"), request(), null, null],
    ["the requester", user(REQUESTER, "approver"), request(), "own_request", "own_request"],
    ["the owner", user(OWNER, "approver"), request(), "own_request", "own_request"],
    ["an admin requester", user(REQUESTER, "admin"), request(), "own_request", "own_request"],
    ["already approved", user(APPROVER_A, "approver"), request({ decisions: [approvedBy(APPROVER_A)] }), "already_approved", null],
    ["a requester-role user", user("user-plain", "requester"), request(), "role", "role"],
    ["a decided request", user(APPROVER_A, "approver"), request({ status: "APPROVED" }), "not_pending", "not_pending"],
    ["a decided request wins over the role", user("user-plain", "requester"), request({ status: "APPROVED" }), "not_pending", "not_pending"],
    ["an unknown owner", user(APPROVER_A, "approver"), request({ project_owner_id: null }), "unknown_owner", "unknown_owner"],
    ["no request", user(APPROVER_A, "approver"), null, "not_pending", "not_pending"],
    ["no user", null, request(), "role", "role"],
  ];

  it.each(cases)("%s", (_label, who, what, approve, reject) => {
    expect(approveBlock(who, what)).toBe(approve);
    expect(rejectBlock(who, what)).toBe(reject);
  });
});

describe("decisionBlockMessage — what the console says instead of the missing buttons", () => {
  it("says nothing when nothing blocks, and nothing for a request that is simply decided", () => {
    expect(decisionBlockMessage(null)).toBeNull();
    expect(decisionBlockMessage("not_pending")).toBeNull();
  });

  it("has a different Korean sentence for each other block", () => {
    const blocks: DecisionBlock[] = ["role", "unknown_owner", "own_request", "already_approved"];
    const messages = blocks.map((block) => decisionBlockMessage(block));

    for (const message of messages) {
      expect(message).toMatch(/[가-힣]/);
    }
    expect(new Set(messages).size).toBe(blocks.length);
  });

  it("pins the self-approval and already-approved wording", () => {
    expect(decisionBlockMessage("own_request")).toBe(
      "본인이 요청했거나 본인이 소유한 프로젝트의 요청은 직접 승인하거나 반려할 수 없습니다. 다른 승인자가 결정해야 합니다.",
    );
    expect(decisionBlockMessage("already_approved")).toBe(
      "이미 이 요청을 승인했습니다. 필요한 승인 수가 모이면 자동으로 승인 처리됩니다.",
    );
    expect(decisionBlockMessage("role")).toBe("승인/반려는 approver 또는 admin 역할만 가능합니다.");
  });
});
