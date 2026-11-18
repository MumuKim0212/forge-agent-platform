/**
 * The role ladder, mirroring backend `app/services/roles.py`.
 *
 * This is presentation only. **The backend is the enforcement point** — every
 * check here has a matching 403 behind it, and the reason these predicates
 * exist is so the UI does not offer an action that is going to fail, not to
 * secure anything. Hiding a control is never the sole control
 *.
 */
import type { User } from "../types/api";

export const Role = {
  REQUESTER: "requester",
  APPROVER: "approver",
  ADMIN: "admin",
} as const;

export type RoleName = (typeof Role)[keyof typeof Role];

/** Total order: `requester < approver < admin`. */
const RANK: Record<string, number> = {
  [Role.REQUESTER]: 1,
  [Role.APPROVER]: 2,
  [Role.ADMIN]: 3,
};

/**
 * The one role comparison in the frontend. **Fail-closed**: an unrecognised or
 * missing value grants nothing, matching the backend's `holds_at_least`.
 * A role string arrives from the server, so "a
 * value this build has never heard of" is a reachable state, not a bug.
 */
export function holdsAtLeast(role: string | null | undefined, minimum: RoleName): boolean {
  if (!role) return false;
  const held = RANK[role];
  if (held === undefined) return false;
  return held >= RANK[minimum];
}

/** Approving / rejecting an `ApprovalRequest` requires
 * `approver`. This is the role rung only. The self-approval ban (the requester
 * and the project's owner, no admin exception) depends on the request, so it is
 * modelled one level up, in `approvalGate.ts` (`canApproveRequest` /
 * `canRejectRequest`), which composes this; the backend still enforces it, so
 * a stale page can legitimately get a 403.
 * Offer the buttons through those gates, not through this alone. */
export function canDecideApprovals(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.APPROVER);
}

/**
 * Confirm deploy / rollback / stop — `own` or
 * `admin`, mirroring `routers/deployments._require_owner_or_admin`.
 *
 * Ownership and role are **separate axes** (project ownership is a
 * separate axis): a `requester` owns their own projects and needs no grant,
 * `admin` overrides ownership, and `approver` does not. A role check alone
 * would be wrong in both directions.
 */
export function canOperateDeployment(
  user: User | null,
  project: { owner_id: string } | null,
): boolean {
  if (!user || !project) return false;
  return user.id === project.owner_id || holdsAtLeast(user.role, Role.ADMIN);
}

/**
 * "The whole console requires `admin`. A
 * non-admin gets no console entry point and is refused if they navigate
 * directly."
 *
 * The **same** predicate answers both questions — the nav entry in `Layout` and
 * the route guard in `AdminConsolePage` — so a non-admin cannot be shown a link
 * to a page that would then let them in, or vice versa. Rule 2 still applies:
 * this is presentation, and every endpoint the console calls independently
 * requires `admin` (`deps.require_role`), so a 403 must still render.
 */
export function canAdministerPlatform(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * "Only `admin` may set or clear an
 * override", mirroring `routers/usage.set_user_quota` / `set_project_quota`.
 *
 * Deliberately **not** owner-or-admin, unlike `canOperateDeployment`: a
 * project's owner may *read* that project's usage, but a subject that can raise
 * its own limit has no limit. Ownership is not an input here, and that is the
 * decision this predicate records.
 */
export function canEditQuota(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * "Only `admin` may change a role",
 * mirroring `routers/auth.change_user_role_endpoint`.
 *
 * Separate from `canEditQuota` even though both resolve to the same rung today:
 * they are two different backend endpoints with two different audit actions, and
 * a future change to either rung must not silently move the other.
 */
export function canGrantRoles(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * Whether a role change targets the acting user (self-demotion is allowed
 * as long as another admin remains).
 *
 * Not a permission gate — it changes nothing about what is allowed. It exists so
 * the console can say *this is you* on the row that is about to change, which
 * the criterion asks for, without any page hand-writing an id comparison. Exact
 * match on both sides, like `canOperateDeployment`'s owner comparison, and
 * false when either operand is missing.
 */
export function isSelfRoleChange(
  actor: User | null,
  target: { id: string } | null,
): boolean {
  if (!actor || !target) return false;
  return actor.id === target.id;
}

/**
 * "Approve / reject /
 * reopen a signup request — `admin`", mirroring `routers/auth_local.py`'s
 * three signup-transition endpoints (all behind `require_role(Role.ADMIN)`).
 *
 * Separate from `canDeactivateUsers` even though both resolve to `admin`
 * today, same reasoning as `canGrantRoles` vs `canEditQuota`: two different
 * backend endpoint groups (`/admin/signup-requests/...` vs
 * `/admin/users/{id}/deactivate`) with two different audit actions, so a
 * future rung change to one must not silently move the other.
 */
export function canReviewSignups(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * "Deactivate a user —
 * `admin`, and never the last admin", mirroring
 * `routers/auth_local.deactivate_user_endpoint`.
 *
 * The last-admin guard itself is enforced server-side
 * (`services/roles.ensure_not_last_admin` via `services/signup.
 * deactivate_user`, 409); this predicate only decides whether the console
 * offers the button at all.
 */
export function canDeactivateUsers(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * "Select the agent engine (ADR 0035) — `admin`",
 * mirroring `routers/agent_engine.py` (both verbs behind `require_role(Role.ADMIN)`).
 *
 * Its own predicate, separate from `canGrantRoles`/`canEditQuota` though the rung is the same
 * today: a different endpoint with a different audit action (`platform.engine_changed`), so a
 * future rung change to one must not silently move the others. Approver does not clear it:
 * the choice changes which vendor and cost path every build takes.
 */
export function canSelectAgentEngine(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * starting an eval run and reading its results are
 * `admin` only, mirroring `routers/evals.py` (all three routes behind `require_role(Role.ADMIN)`).
 *
 * Its own predicate: starting a run spends real money (up to the run budget), so a future rung
 * change to the engine choice or the role grant must not silently move this one. Approver does not
 * clear it. Presentation only: the backend is the enforcement point.
 */
export function canRunEvals(user: User | null): boolean {
  return holdsAtLeast(user?.role, Role.ADMIN);
}

/**
 * a build's trace is for the
 * project's owner, and for `approver` and above, mirroring the backend's
 * `roles.may_read_build_trace`. NARROWER than the timeline on purpose (any authenticated
 * user reads that).
 *
 * Ownership and role are separate axes: the owner needs no role and `approver`
 * needs no ownership. Unlike `canOperateDeployment`, `admin` is not a separate branch: it
 * clears the `approver` rung. Fail-closed on a missing user, project or unrecognised role.
 * Presentation only: the endpoint answers 403 on its own.
 */
export function canReadBuildTrace(user: User | null, project: { owner_id: string } | null): boolean {
  if (!user) return false;
  const isOwner = project !== null && user.id === project.owner_id;
  return isOwner || holdsAtLeast(user.role, Role.APPROVER);
}
