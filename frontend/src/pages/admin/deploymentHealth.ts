/**
 * Deployment health's one decision: what a project's two deployment slots mean
 * together.
 *
 * `active_deployment === null` is five different situations, and the whole reason
 * `GET /console/deployments` returns `latest_deployment` as well is that
 * collapsing them renders every in-progress deploy as "not deployed"
 * (schemas/console.py: `latest_deployment` is load-bearing). Collapsing
 * the *other* way is just as lossy and is the half people forget: a project can
 * be serving an old version **while** a newer deploy is in flight or has just
 * failed, and plain "운영중" hides exactly the thing an operator opened the
 * console to find. So both slots are read, and the states below keep the two
 * facts side by side instead of picking one.
 *
 * Read-only by construction: nothing here imports `api/deployments.ts` and there
 * is no action in this module. Deploy, rollback and stop stay on the project
 * detail page where owner-or-admin already applies.
 */
import type { BadgeTone } from "../../components/ui/Badge";
import type { Deployment, DeploymentHealth, ProjectDeploymentHealth } from "../../types/api";

/** `models/deployment.DeploymentStatus`. Cross-service contract strings. */
export const DeploymentStatus = {
  DEPLOYING: "DEPLOYING",
  ACTIVE: "ACTIVE",
  FAILED: "FAILED",
  SUPERSEDED: "SUPERSEDED",
  STOPPED: "STOPPED",
} as const;

export type DeploymentHealthState =
  | "SERVING"
  | "SERVING_DEPLOY_IN_PROGRESS"
  | "SERVING_LAST_ATTEMPT_FAILED"
  | "NEVER_DEPLOYED"
  | "DEPLOYING"
  | "LAST_ATTEMPT_FAILED"
  | "STOPPED"
  | "SUPERSEDED"
  | "UNKNOWN";

/** Every state has a label, and `tsc` says so: a state added to the union
 * without one is a compile error, not a row that renders its enum name. */
export const DEPLOYMENT_HEALTH_LABELS: Record<DeploymentHealthState, string> = {
  SERVING: "운영중",
  SERVING_DEPLOY_IN_PROGRESS: "운영중 · 새 배포 진행중",
  SERVING_LAST_ATTEMPT_FAILED: "운영중 · 마지막 배포 시도 실패",
  NEVER_DEPLOYED: "배포 이력 없음",
  DEPLOYING: "배포 진행중 (운영 버전 없음)",
  LAST_ATTEMPT_FAILED: "마지막 배포 실패 (운영 버전 없음)",
  STOPPED: "중지됨",
  SUPERSEDED: "이전 버전으로 대체됨",
  UNKNOWN: "상태 확인 필요",
};

/**
 * the nine raw hex/`crimson` literals above become named
 * `Badge` tones — the same status→tone mapping shape `StatusBadge.tsx`
 * already uses, and permitted by this module's own test
 * (`deploymentHealth.test.ts:176` loops `expect(COLORS[state]).toBeTruthy()`
 * over every state; it asserts no particular colour, so replacing the
 * values is allowed — dropping a key, or leaving one empty, is what it
 * actually guards).
 */
export const DEPLOYMENT_HEALTH_COLORS: Record<DeploymentHealthState, BadgeTone> = {
  SERVING: "success",
  SERVING_DEPLOY_IN_PROGRESS: "warning",
  SERVING_LAST_ATTEMPT_FAILED: "danger",
  NEVER_DEPLOYED: "neutral",
  DEPLOYING: "warning",
  LAST_ATTEMPT_FAILED: "danger",
  STOPPED: "neutral",
  SUPERSEDED: "neutral",
  UNKNOWN: "warning",
};

/**
 * A live deployment means the service is up, so every `SERVING*` state says so
 * first — but when `latest_deployment` is a *different, newer* row, what happened
 * to it is carried in the same state rather than dropped.
 *
 * A status this build does not recognise resolves to `UNKNOWN`, never to one of
 * the reassuring answers: a lifecycle state added backend-side should read as
 * "look at this", not as "serving" and not as "never deployed".
 */
export function deploymentHealthState(project: {
  active_deployment: Deployment | null;
  latest_deployment: Deployment | null;
}): DeploymentHealthState {
  const { active_deployment: active, latest_deployment: latest } = project;

  if (active !== null) {
    // `latest === null` cannot happen (the active row is itself a row, so it is
    // at worst the latest one) — treated as plain serving rather than as an
    // error, because inventing a fault from a shape that means "up" would be
    // the more misleading answer.
    if (latest === null || latest.id === active.id) return "SERVING";
    if (latest.status === DeploymentStatus.DEPLOYING) return "SERVING_DEPLOY_IN_PROGRESS";
    if (latest.status === DeploymentStatus.FAILED) return "SERVING_LAST_ATTEMPT_FAILED";
    return "SERVING";
  }

  if (latest === null) return "NEVER_DEPLOYED";

  switch (latest.status) {
    case DeploymentStatus.DEPLOYING:
      return "DEPLOYING";
    case DeploymentStatus.FAILED:
      return "LAST_ATTEMPT_FAILED";
    case DeploymentStatus.STOPPED:
      return "STOPPED";
    case DeploymentStatus.SUPERSEDED:
      return "SUPERSEDED";
    default:
      return "UNKNOWN";
  }
}

/**
 * What to say when the failure list is bounded. `recent_failures` is capped at
 * `recent_failures_limit`, and the response declares whether older failures exist
 * beyond it; presenting a truncated list as complete is the failure mode that
 * response shape exists to prevent, so the notice is produced here rather than
 * left to a caller's `&&`.
 *
 * `null` means the list really is everything there is.
 */
export function recentFailureNotice(
  health: Pick<DeploymentHealth, "recent_failures_limit" | "recent_failures_truncated">,
): string | null {
  if (!health.recent_failures_truncated) return null;
  return `최근 실패 ${health.recent_failures_limit}건까지만 표시됩니다. 이보다 오래된 실패가 더 있습니다 — 전체 이력은 각 프로젝트 상세의 배포 이력에서 확인하세요.`;
}

/** Attention first, then name — an operator opening the console should not have
 * to scan an alphabetical list for the one broken thing. Ordering only: every
 * project the API returned is still in the result. */
const STATE_PRIORITY: Record<DeploymentHealthState, number> = {
  LAST_ATTEMPT_FAILED: 0,
  UNKNOWN: 1,
  SERVING_LAST_ATTEMPT_FAILED: 2,
  DEPLOYING: 3,
  SERVING_DEPLOY_IN_PROGRESS: 4,
  STOPPED: 5,
  SUPERSEDED: 6,
  SERVING: 7,
  NEVER_DEPLOYED: 8,
};

/** The first state of `STATE_PRIORITY` that no longer asks an operator to look: everything ranked
 * ahead of it needs attention, it and everything behind it is settled. The one hand-kept fact
 * `ATTENTION_STATES` rests on; `deploymentHealth.test.ts` ties it to the sort order. */
const FIRST_SETTLED_STATE: DeploymentHealthState = "STOPPED";

/** The states that mean "an operator should look at this", derived from the priority table above
 * so the 주의 필요 count and the order `sortByAttention` puts projects in cannot disagree
 * about which projects those are. They used to be two hand-written lists. */
export const ATTENTION_STATES: ReadonlySet<DeploymentHealthState> = new Set(
  (Object.keys(STATE_PRIORITY) as DeploymentHealthState[]).filter(
    (state) => STATE_PRIORITY[state] < STATE_PRIORITY[FIRST_SETTLED_STATE],
  ),
);

/** How many of `projects` are in a state that needs attention (the 주의 필요 tile). */
export function attentionCount(
  projects: ReadonlyArray<Pick<ProjectDeploymentHealth, "active_deployment" | "latest_deployment">>,
): number {
  return projects.filter((project) => ATTENTION_STATES.has(deploymentHealthState(project))).length;
}

/**
 * How many of `projects` have an active deployment (the 현재 운영 중 tile). Counted from the
 * `active_deployment` slot alone, the same fact `deploymentHealthState` reads first: a project
 * serving an old version while a newer deploy is in flight or has failed still counts, and a project
 * whose only row is deploying, failed, stopped or superseded does not. Derived from the existing
 * read; nothing is asked of the API for it.
 */
export function servingCount(projects: ReadonlyArray<Pick<ProjectDeploymentHealth, "active_deployment">>): number {
  return projects.filter((project) => project.active_deployment !== null).length;
}

export function sortByAttention(projects: ProjectDeploymentHealth[]): ProjectDeploymentHealth[] {
  return [...projects].sort((a, b) => {
    const byState =
      STATE_PRIORITY[deploymentHealthState(a)] - STATE_PRIORITY[deploymentHealthState(b)];
    return byState !== 0 ? byState : a.project_name.localeCompare(b.project_name);
  });
}
