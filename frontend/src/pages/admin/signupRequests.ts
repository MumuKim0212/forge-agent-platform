/**
 * The admin signup-approval + deactivation surface's non-rendering parts.
 *
 * `SignupRequestsPanel` only ever offers a button whose transition the
 * backend actually accepts — mirroring `services/signup.py`'s transition
 * table below — so the panel does not need its own copy of the invariant;
 * the backend stays the enforcement point regardless (every one of these
 * still has a 400/409 behind it, per `routers/auth_local.py`).
 */
import { ApiError, transportErrorMessage } from "../../api/client";
import { LAST_ADMIN_EXPLANATION } from "./roleChange";

/**
 * Mirrors backend `services/account_status.AccountStatus` — **UPPER_SNAKE**,
 * unlike `auth/roles.Role`'s lowercase values (`status` is a brand-new
 * column with no legacy-casing
 * constraint, unlike `role`). Pinned as a literal in
 * `signupRequests.test.ts` against the wire contract, the same discipline
 * `auth/roles.test.ts`'s cross-service-contract test uses for `Role`.
 */
export const AccountStatus = {
  PENDING: "PENDING",
  ACTIVE: "ACTIVE",
  REJECTED: "REJECTED",
} as const;

export type AccountStatusValue = (typeof AccountStatus)[keyof typeof AccountStatus];

const STATUS_LABELS: Record<AccountStatusValue, string> = {
  [AccountStatus.PENDING]: "대기중",
  [AccountStatus.ACTIVE]: "활성",
  [AccountStatus.REJECTED]: "거부됨",
};

/** Fail-closed: a status value this build has never heard of names itself
 * rather than borrowing another status's label — the same reasoning as
 * `deploymentHealth.ts`'s `UNKNOWN` state, applied to a string contract
 * instead of a derived one. `status` arrives from the server on every row,
 * so an unrecognised value is a reachable state, not a hypothetical. */
export function accountStatusLabel(status: string): string {
  return STATUS_LABELS[status as AccountStatusValue] ?? `알 수 없음 상태 (${status})`;
}

/** `PENDING` or `REJECTED` ->
 * `ACTIVE` (`services/signup.approve_signup`). */
export function canApprove(status: string): boolean {
  return status === AccountStatus.PENDING || status === AccountStatus.REJECTED;
}

/** `PENDING` -> `REJECTED` only (`services/signup.reject_signup`). An
 * already-`REJECTED` row is not rejected again, and an `ACTIVE` row is
 * *deactivated*, not rejected — a distinct backend transition with its own
 * guard (`canDeactivate`). */
export function canReject(status: string): boolean {
  return status === AccountStatus.PENDING;
}

/** `REJECTED` -> `PENDING` only (`services/signup.reopen_signup`). */
export function canReopen(status: string): boolean {
  return status === AccountStatus.REJECTED;
}

/** `ACTIVE` -> `REJECTED`, an admin revoking access
 * (`services/signup.deactivate_user`). The rule: "the last
 * admin cannot be deactivated" — enforced server-side by the same guard
 * role demotion uses (`services/roles.ensure_not_last_admin`); this
 * predicate only decides whether the row status makes the transition legal
 * at all, not who may attempt it (that's `auth/roles.canDeactivateUsers`). */
export function canDeactivate(status: string): boolean {
  return status === AccountStatus.ACTIVE;
}

/**
 * The message shown when an approve/reject/reopen/deactivate call is
 * rejected. Status codes are the contract (`routers/auth_local.py`: 400
 * illegal transition, 409 last admin on deactivate, `deps.require_role`:
 * 403), mirroring `pages/admin/roleChange.roleChangeFailureMessage`'s
 * per-status shape.
 *
 * The 409 case **imports and reuses** `LAST_ADMIN_EXPLANATION` from
 * `roleChange.ts` rather than writing a second copy of the explanation —
 * the admin-approval-surface criterion asks
 * this panel to "reuse the existing wording pattern from the role-change
 * panel rather than inventing a second voice", and importing the same
 * constant is the literal form of that: the two panels cannot drift apart
 * on this copy without a deliberate edit to the shared source.
 */
export function signupActionFailureMessage(err: unknown, fallback: string): string {
  // no answer, or an unreadable one -- the shared Korean sentence, and never the server's
  // `detail` suffix below (there is none to quote).
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  if (err instanceof ApiError) {
    const said = err.message.trim();
    const suffix = said === "" ? "" : ` (서버 응답: ${said})`;
    switch (err.status) {
      case 409:
        return `${LAST_ADMIN_EXPLANATION}${suffix}`;
      case 400:
        return `이 상태에서는 수행할 수 없는 작업입니다. 목록이 오래되었을 수 있으니 새로 고침 후 다시 시도하세요.${suffix}`;
      case 403:
        return `이 작업은 admin만 할 수 있습니다. 방금 본인 권한이 바뀌었다면 새로 고침 후 다시 확인하세요.${suffix}`;
      case 404:
        return `대상 사용자를 찾을 수 없습니다. 목록이 오래되었을 수 있으니 새로 고침 후 다시 시도하세요.${suffix}`;
      default:
        return said === "" ? fallback : `${fallback} (서버 응답: ${said})`;
    }
  }
  // Not an `ApiError`: a caller bug. A library's English `message` is never appended.
  return fallback;
}
