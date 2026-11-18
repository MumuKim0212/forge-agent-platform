/**
 * The project page's change-request form, as named decisions (ADR 0021).
 *
 * A change request is a MODIFY request, and a modify build starts from the project's confirmed ref.
 * Until a first deployment is confirmed there is none, so the backend refuses the request with a
 * `409` whose `detail` starts `NO_CONFIRMED_REF: ` and creates nothing. The page does not offer a
 * path the backend refuses: the form is disabled with the reason below, and a `409` that gets
 * through anyway (a stale page) is worded as that same reason.
 *
 * Whether a deployment was ever confirmed is the project read's own `confirmed_ref` (null until the
 * first confirm merges a build). Presentation only: the backend is the enforcement point.
 */
import { ApiError } from "../api/client";
import { refusalCode } from "../api/refusal";

/**
 * The backend's `NO_CONFIRMED_REF_CODE` (`routers/projects.py`): the prefix of the refusal's
 * `detail`. A contract string crossing a boundary, so a literal here, pinned as a literal in the
 * test: a one-sided rename is invisible to `tsc` and would degrade to the generic failure text.
 */
export const NO_CONFIRMED_REF_CODE = "NO_CONFIRMED_REF";

/** One line, shown beside the disabled form and as the answer to the refusal. */
export const NO_CONFIRMED_REF_MESSAGE =
  "첫 배포를 확정한 뒤에 변경 요청을 보낼 수 있습니다. 배포 전 서비스는 새 요청으로 다시 만들어 주세요.";

/** Whether a deployment was ever confirmed for this project: `confirmed_ref` is a non-empty string.
 * Fails closed on a missing project or a response without the field. */
export function hasConfirmedDeployment(project: { confirmed_ref?: string | null } | null): boolean {
  const ref = project?.confirmed_ref;
  return typeof ref === "string" && ref !== "";
}

/** Whether the page offers the change-request form. A state rule only: the backend asks no role of
 * a requester here, so nothing about the viewer enters into it. */
export function canSubmitChangeRequest(project: { confirmed_ref?: string | null } | null): boolean {
  return hasConfirmedDeployment(project);
}

/** Why the form is disabled, or `null` when it is offered. */
export function changeRequestBlockedMessage(project: { confirmed_ref?: string | null } | null): string | null {
  return canSubmitChangeRequest(project) ? null : NO_CONFIRMED_REF_MESSAGE;
}

/** The backend refused a change request because the project has no confirmed ref: a `409` carrying
 * the code. Any other 409, or the code under another status, is not this refusal. */
export function isNoConfirmedRefRefusal(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && refusalCode(err) === NO_CONFIRMED_REF_CODE;
}

/** The Korean reason for that refusal, or `null` for any other failure. */
export function noConfirmedRefMessage(err: unknown): string | null {
  return isNoConfirmedRefRefusal(err) ? NO_CONFIRMED_REF_MESSAGE : null;
}
