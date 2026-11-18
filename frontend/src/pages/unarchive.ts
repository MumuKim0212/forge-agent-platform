/**
 * The project page's un-archive control, as named decisions (ADR 0021).
 *
 * An `ARCHIVED` project refuses deploy-confirm and rollback until it is un-archived,
 * and the page says "먼저 보관을 해제하세요". The only way to do that was
 * `POST /projects/{id}/unarchive`, which no screen called, so a non-developer owner could not follow
 * the platform's own instruction. This is the control and what it says.
 *
 * Everything here is PRESENTATION. The route is owner-or-admin and answers 400 for a project that is
 * not archived; hiding the control is never the control.
 */
import { ApiError, transportErrorMessage } from "../api/client";
import { canOperateDeployment } from "../auth/roles";
import type { User } from "../types/api";

/**
 * The backend's `ProjectStatus.ARCHIVED`. A contract string crossing a boundary, so a literal here
 * (a one-sided rename is invisible to `tsc`: the control would just never show); pinned in the test.
 */
export const ARCHIVED_PROJECT_STATUS = "ARCHIVED";

/** The one place the frontend decides a project is archived. */
export function isProjectArchived(project: { status: string } | null): boolean {
  return project !== null && project.status === ARCHIVED_PROJECT_STATUS;
}

/**
 * Whether the page offers `보관 해제`: the project is archived AND the viewer may operate it. WHO
 * is the existing owner-or-admin helper (`canOperateDeployment`, the same rule the backend's
 * `_require_owner_or_admin` applies to un-archive, confirm, rollback and stop), composed rather than
 * restated, so a change to that rule moves this one with it. Fails closed on a missing user or
 * project.
 */
export function canUnarchiveProject(
  user: User | null,
  project: { owner_id: string; status: string } | null,
): boolean {
  if (project === null) return false;
  return isProjectArchived(project) && canOperateDeployment(user, project);
}

/**
 * What the confirmation says. Un-archiving restores nothing, because archival destroyed nothing;
 * it does not start the service either. The one caveat is a
 * project an operator has since deprovisioned: it un-archives, but its data is gone and a later
 * deploy provisions a fresh, empty database.
 *
 * `GET /projects/{id}` does not say whether a project was deprovisioned (no field on the project
 * read, and the stored service env, which the deprovision clears, is never serialized), so the
 * caveat is shown always, worded as a condition ("if ... was already deleted"), never as a fact
 * about this project. It is ONE sentence, so the consequence ("a later deploy creates an empty
 * database") cannot be read without its condition: as a sentence of its own it would be false for a
 * project that was never deprovisioned.
 */
export const UNARCHIVE_CONFIRMATION = {
  title: "보관을 해제할까요?",
  does: "프로젝트가 보관되기 전의 상태로 돌아갑니다. 그 뒤에는 배포 확정과 롤백을 다시 할 수 있습니다.",
  doesNot:
    "보관은 아무것도 삭제하지 않았으므로 해제해도 복원되는 것은 없고, 중지된 서비스가 자동으로 다시 시작되지도 않습니다.",
  dataCondition:
    "이 서비스의 데이터를 운영자가 이미 삭제했다면 그 데이터는 돌아오지 않고, 이후 배포하면 비어 있는 새 데이터베이스가 만들어집니다.",
  confirm: "해제",
  busy: "해제하는 중...",
  cancel: "취소",
} as const;

/** Said, politely, after a successful un-archive: the acting owner's own action, not an alert. */
export const UNARCHIVED_NOTICE =
  "보관을 해제했습니다. 프로젝트 상태가 복원되었고, 서비스는 자동으로 다시 시작되지 않습니다.";

const FALLBACK_MESSAGE = "보관 해제에 실패했습니다.";
const SERVER_ERROR_MESSAGE = "서버에서 보관 해제를 처리하지 못했습니다. 잠시 후 다시 시도하세요.";

/** One sentence per status the route can answer (`routers/projects.unarchive_project`). */
const HTTP_MESSAGES: Record<number, string> = {
  400: "이미 보관이 해제된 프로젝트입니다. 화면을 새로 읽었습니다.",
  401: "로그인이 만료되었습니다. 다시 로그인한 뒤 시도하세요.",
  403: "프로젝트 소유자 또는 admin만 보관을 해제할 수 있습니다. 소유자나 역할이 바뀌었을 수 있어 화면을 새로 읽었습니다.",
  404: "프로젝트를 찾을 수 없습니다.",
};

/**
 * What the page says when un-archiving fails. A failure that never reached the server says so
 * (`transportErrorMessage`); each status the route answers gets its own sentence (400 not archived
 * any more, 401 session, 403 not owner or admin, 404 no such project); any other 5xx the server's
 * fault; anything else the fallback. The backend's English `detail` never reaches the screen
 *.
 */
export function unarchiveFailureMessage(err: unknown): string {
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  if (err instanceof ApiError && err.kind === "http") {
    if (Object.prototype.hasOwnProperty.call(HTTP_MESSAGES, err.status)) return HTTP_MESSAGES[err.status];
    if (err.status >= 500) return SERVER_ERROR_MESSAGE;
  }
  return FALLBACK_MESSAGE;
}

/**
 * After a refused un-archive the page reads again when the refusal says what it showed has moved
 * on: a 400 (someone already un-archived it) or a 403 (the owner or the viewer's role changed since
 * the page loaded). The control that was just refused then does not stay on screen.
 */
export function shouldRefreshAfterUnarchiveFailure(err: unknown): boolean {
  return err instanceof ApiError && err.kind === "http" && (err.status === 400 || err.status === 403);
}

/**
 * A failed un-archive as the page keeps it: the sentence, and whether the refusal is itself why the
 * control is about to go (the refusal re-reads, and the re-read shows the project un-archived or
 * the viewer without the right). `explainsWithdrawal` is `shouldRefreshAfterUnarchiveFailure`'s
 * answer, kept with the sentence so the page can tell the two apart later.
 */
export interface UnarchiveFailure {
  message: string;
  explainsWithdrawal: boolean;
}

/**
 * What stays of the failure alert when the control stops being offered. The alert belongs to an
 * attempt at a control; once the control is gone for some other reason (someone else un-archived the
 * project, the viewer's identity or role changed), "the server could not do it" is about something
 * the page no longer offers, so it goes. The exception is the refusal that made the control go: a
 * 400 ("already un-archived") or a 403 ("only the owner or an admin") whose re-read removed the
 * control is the explanation of why it is not there, so it stays until the next action
 * (`ProjectDetailPage` clears it when another action starts).
 */
export function failureAfterControlWithdrawn(failure: UnarchiveFailure | null): UnarchiveFailure | null {
  return failure !== null && failure.explainsWithdrawal ? failure : null;
}

/**
 * Escape leaves an inline confirmation, unless the action it asks about is already running (a
 * request in flight cannot be taken back by closing the box, and the box is what shows it is busy).
 */
export function shouldDismissConfirmation(key: string, busy: boolean): boolean {
  return key === "Escape" && !busy;
}
