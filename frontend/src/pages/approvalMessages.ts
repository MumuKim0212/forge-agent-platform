/**
 * What the approval console says when a decision is refused or a read fails.
 *
 * The backend's `detail` is an English diagnostic and never reaches the screen. It used to
 * collapse every failure into one generic sentence per action, which hid the three refusals
 * an approver actually meets: the request is theirs (403), it is already decided or superseded
 * (400), and a clash with another decision (409). They are told apart by STATUS, the way
 * `roleChangeFailureMessage` does: the approval routes send no code in their `detail`, and a 403
 * here is either the self-approval ban or a role change since the page loaded, both of which the
 * 403 sentence names. (The deploy refusals do carry a code; see `deployControls.ts`.)
 */
import { ApiError, transportErrorMessage } from "../api/client";
import { isStateRefusal } from "../api/refusal";

export type ApprovalDecisionAction = "approve" | "reject";

const ACTION_FALLBACKS: Record<ApprovalDecisionAction, string> = {
  approve: "승인에 실패했습니다.",
  reject: "반려에 실패했습니다.",
};

export const FORBIDDEN_DECISION_MESSAGE =
  "이 요청은 직접 결정할 수 없습니다. 본인이 요청했거나 본인이 소유한 프로젝트의 요청은 다른 승인자가 결정해야 하고, approver 이상의 역할이 필요합니다.";
export const ALREADY_DECIDED_MESSAGE =
  "이미 승인·반려되었거나 더 새로운 빌드로 대체된 요청이거나, 이미 승인한 요청입니다. 화면을 최신 상태로 다시 불러옵니다.";
export const NOT_FOUND_DECISION_MESSAGE = "요청을 찾을 수 없습니다. 목록을 새로 고친 뒤 다시 확인하세요.";
export const CONFLICTING_DECISION_MESSAGE =
  "다른 결정과 동시에 처리되었습니다. 화면을 최신 상태로 다시 불러옵니다.";

/** The sentence for a refused or failed approve / reject. */
export function approvalDecisionFailureMessage(err: unknown, action: ApprovalDecisionAction): string {
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  if (err instanceof ApiError && err.kind === "http") {
    switch (err.status) {
      case 403:
        return FORBIDDEN_DECISION_MESSAGE;
      case 400:
        return ALREADY_DECIDED_MESSAGE;
      case 404:
        return NOT_FOUND_DECISION_MESSAGE;
      case 409:
        return CONFLICTING_DECISION_MESSAGE;
      default:
        break;
    }
  }
  return ACTION_FALLBACKS[action];
}

/** After a decision is refused with a 400 or 409 the request on screen is stale (decided,
 * superseded, or decided by someone else meanwhile), so it is read again: its buttons must not
 * stay up for a request that can no longer take them. */
export function shouldRereadAfterDecisionFailure(err: unknown): boolean {
  return isStateRefusal(err);
}

/** The sentence for a failed read of the list or of one request: the transport wording when the
 * server was never reached, else the caller's own fallback. */
export function approvalLoadFailureMessage(err: unknown, fallback: string): string {
  return transportErrorMessage(err) ?? fallback;
}
