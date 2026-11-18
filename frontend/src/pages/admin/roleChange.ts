/**
 * Role management's non-rendering parts: the fixed set of grantable values, and
 * turning a rejection from `PATCH /users/{id}/role` into something an operator
 * can act on.
 *
 * The refusal that matters is the last-admin one. The backend answers `409`
 * (`services/roles.LastAdminError`) with a one-line detail, and the UI is asked to
 * "surface that as an explained refusal
 * rather than a bare error" — because "cannot demote the last remaining admin"
 * next to a select box reads as a bug in the console, not as a deliberate
 * platform invariant.
 */
import { Role, type RoleName } from "../../auth/roles";

/**
 * What should happen to the acting user's own identity once a role change
 * succeeds.
 * The backend re-reads `role`
 * per request regardless — this is presentation only — but the admin who
 * demotes themselves otherwise keeps seeing the old nav/console access
 * until a reload.
 *
 * Takes `refreshCurrentUser` (`IdentityContext`'s existing re-read of
 * `/users/me` — already exposed on the context, already called the same way
 * by `LocalLoginForm.tsx`) as a parameter rather than importing the context
 * directly, so the *decision* of whether to call it is testable without
 * mounting `RolesPanel` (ADR 0021: decision logic, not rendering).
 */
export async function refreshIdentityAfterRoleChange(
  isSelfChange: boolean,
  refreshCurrentUser: () => Promise<void>,
): Promise<void> {
  if (isSelfChange) {
    await refreshCurrentUser();
  }
}

/** The sentence naming how many admins the last-admin guard counts. The number is the
 * backend's (`GET /users/usable-admin-count`), so it cannot disagree with the 409 it explains;
 * `null` (not loaded, not readable, or a viewer who may not read it) drops the sentence rather
 * than substituting a recount from the directory, which carries neither status nor credential. */
export function adminCountSentence(count: number | null): string {
  return count === null ? "" : `현재 활성 admin: ${count}명.`;
}

/** The visible text of a row's apply button, and its accessible name: every row's button
 * said `적용`, so a screen reader's list of controls could not tell whose role it changed. The name
 * is the user's, then the visible text (WCAG 2.5.3: what a person sees is in the name). */
export function roleApplyText(saving: boolean): string {
  return saving ? "변경 중..." : "적용";
}

export function roleApplyLabel(userName: string, saving: boolean): string {
  return `${userName} ${roleApplyText(saving)}`;
}

/**
 * Exactly the three ladder values and nothing else (no free-form
 * entry). The values come from `auth/roles.Role` so the
 * console cannot drift from the one place role strings are spelled; the labels
 * name what the rung buys, since `requester`/`approver`/`admin` are the wire
 * values and are shown as-is elsewhere in the UI.
 */
export const ROLE_OPTIONS: ReadonlyArray<{ value: RoleName; label: string }> = [
  { value: Role.REQUESTER, label: "requester — 요청 제출, 본인 프로젝트 소유" },
  { value: Role.APPROVER, label: "approver — 위 권한 + 승인/반려 결정" },
  { value: Role.ADMIN, label: "admin — 위 권한 + 역할·쿼터·플랫폼 설정 조회" },
];

export const LAST_ADMIN_EXPLANATION =
  "마지막 남은 admin은 강등할 수 없습니다. admin이 0명이 되면 역할을 되돌릴 수 있는 사람이 없어 플랫폼이 역할 관리에서 영구적으로 잠깁니다. 먼저 다른 사용자에게 admin을 부여한 뒤 다시 시도하세요.";

export const SELF_CHANGE_NOTICE =
  "본인 역할입니다. 다른 admin이 남아 있으면 스스로 강등할 수 있고, 변경은 다음 요청부터 즉시 적용됩니다.";

/**
 * The message shown when a role change is rejected. Status codes are the
 * contract (`routers/auth.change_user_role_endpoint`: 400 unknown role, 409 last
 * admin; `deps.require_role`: 403), so each one is handled by number and gets
 * its own Korean explanation.
 *
 * **What happens next changed, and this paragraph used to argue the
 * opposite.** It read: "...and the server's own `detail` is kept in the
 * message rather than replaced — an explanation that hides what the server
 * said is harder to debug, not easier." That was a reasonable call when this
 * console was the only reader of `detail`. It is not this file's call to
 * make alone anymore: for the login/signup screens it was established that
 * rendering a backend string is not what protects anything — it puts
 * English text in a Korean UI, and it turns the backend's phrasing into a
 * de-facto UI contract that has to hold forever, wherever the backend
 * deliberately owns the wording. Neither point was specific to login; both
 * apply here the same way. `detail` is accepted below only so call sites
 * keep a stable signature (`ApiError.status`/`.message`) — it is not read.
 * The debuggability the old comment was protecting has not gone away: the
 * network tab and the server log both still have the raw `detail`, it is
 * just not duplicated onto the screen anymore.
 */
export function roleChangeFailureMessage(status: number, _detail: string): string {
  switch (status) {
    case 409:
      return LAST_ADMIN_EXPLANATION;
    case 400:
      return "역할 값이 올바르지 않습니다. requester / approver / admin 중 하나여야 합니다.";
    case 403:
      return "역할 변경은 admin만 할 수 있습니다. 방금 본인 권한이 바뀌었다면 새로 고침 후 다시 확인하세요.";
    case 404:
      return "대상 사용자를 찾을 수 없습니다. 목록이 오래되었을 수 있으니 새로 고침 후 다시 시도하세요.";
    default:
      return "역할 변경에 실패했습니다.";
  }
}
