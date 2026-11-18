/**
 * Who may decide an approval request, and why not (ADR 0021).
 *
 * The console used to offer `승인` / `반려` for every PENDING request to any approver, and let the
 * backend answer 403 for the viewer's own request or 400 for one they had already approved. The
 * backend refuses three things on top of the role rung, and each is a plain fact in the payload:
 *
 * - **Self-approval is banned for both the requester AND the project's owner**
 *   (`services/approval._is_self_approval`), with no admin exception. So a requester who is an
 *   admin is refused, and so is an owner who did not request the change.
 * - **One approve decision per actor per request.** An approver who has already approved cannot
 *   approve again (they may still reject).
 * - **Only a PENDING request takes a decision.**
 *
 * These are PRESENTATION gates, mirroring rules the backend enforces: hiding a button is never the
 * control. The reasons are returned (not just a boolean) so the console can say why the buttons are
 * absent instead of leaving an approver wondering.
 */
import type { User } from "../types/api";
import { canDecideApprovals } from "./roles";

/**
 * The backend's `ApprovalStatus.PENDING`: the only status that takes a decision. A contract
 * string, so a literal (a rename on one side is invisible to `tsc`); pinned in the test.
 */
export const PENDING_APPROVAL_STATUS = "PENDING";

/** The one place the frontend decides a request is awaiting a decision. */
export function isApprovalPending(status: unknown): boolean {
  return status === PENDING_APPROVAL_STATUS;
}

/** The decision values the backend stores (`ApprovalDecision.decision`). Contract strings, pinned in
 * the test; the approve one is a gate input (one approval per actor), and both key the display
 * labels (`components/approvalDisplay.ts`), so the page spells neither. */
export const APPROVE_DECISION = "approve";
export const REJECT_DECISION = "reject";

/** The fields of an approval request these gates read: all of them are in the detail payload. */
export interface DecidableRequest {
  status: string;
  requester_id: string;
  /** Null when the project row is gone; such a request cannot be decided, so it fails closed. */
  project_owner_id: string | null;
  decisions: ReadonlyArray<{ actor_id: string; decision: string }>;
}

export type DecisionBlock =
  | "not_pending"
  | "role"
  | "unknown_owner"
  | "own_request"
  | "already_approved";

function ownRequestOrUnknown(user: User, request: DecidableRequest): DecisionBlock | null {
  // Fails closed: with no owner id the self-approval rule cannot be checked, so nothing is offered.
  if (typeof request.project_owner_id !== "string" || request.project_owner_id === "") {
    return "unknown_owner";
  }
  if (user.id === request.requester_id || user.id === request.project_owner_id) {
    return "own_request";
  }
  return null;
}

/** Why `user` cannot reject `request`, or `null` when the backend would take the decision. */
export function rejectBlock(user: User | null, request: DecidableRequest | null): DecisionBlock | null {
  if (!request || !isApprovalPending(request.status)) return "not_pending";
  if (!user || !canDecideApprovals(user)) return "role";
  return ownRequestOrUnknown(user, request);
}

/** Why `user` cannot approve `request`, or `null`. Everything that blocks a rejection blocks an
 * approval, plus having approved already. */
export function approveBlock(user: User | null, request: DecidableRequest | null): DecisionBlock | null {
  const common = rejectBlock(user, request);
  if (common !== null) return common;
  // `rejectBlock` returned null, so both are present.
  if (request!.decisions.some((d) => d.actor_id === user!.id && d.decision === APPROVE_DECISION)) {
    return "already_approved";
  }
  return null;
}

/** Whether to offer `승인`: the approver rung, not the requester, not the project's owner, not
 * someone who already approved, and the request still PENDING. */
export function canApproveRequest(user: User | null, request: DecidableRequest | null): boolean {
  return approveBlock(user, request) === null;
}

/** Whether to offer `반려` (an approver who already approved may still reject). */
export function canRejectRequest(user: User | null, request: DecidableRequest | null): boolean {
  return rejectBlock(user, request) === null;
}

const BLOCK_MESSAGES: Record<Exclude<DecisionBlock, "not_pending">, string> = {
  role: "승인/반려는 approver 또는 admin 역할만 가능합니다.",
  unknown_owner: "프로젝트 정보를 확인할 수 없어 결정할 수 없습니다.",
  own_request: "본인이 요청했거나 본인이 소유한 프로젝트의 요청은 직접 승인하거나 반려할 수 없습니다. 다른 승인자가 결정해야 합니다.",
  already_approved: "이미 이 요청을 승인했습니다. 필요한 승인 수가 모이면 자동으로 승인 처리됩니다.",
};

/** The sentence for a block, or `null` for `not_pending` (a decided request needs no explanation
 * of why it cannot be decided again: its status badge says it). */
export function decisionBlockMessage(block: DecisionBlock | null): string | null {
  if (block === null || block === "not_pending") return null;
  return BLOCK_MESSAGES[block];
}
