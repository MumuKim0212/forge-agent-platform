/**
 * The project page's deploy controls, as named decisions (ADR 0021).
 *
 * The page used to decide for itself what to offer, from the timeline: the newest build of ANY
 * status, required to be SUCCEEDED (so a queued, running or failed newer build hid `배포 확정` for
 * a build the backend accepts), and `롤백` whenever some deployment was ACTIVE (while the backend
 * needs a SUPERSEDED target, so after a first deploy it always failed). Two copies of a rule, and
 * the frontend's was wrong.
 *
 * Now the backend states what is actionable (`DeployControls`, `GET /projects/{id}`), computed by
 * the functions `confirm_deploy` and `request_rollback` run. Everything here READS those fields;
 * nothing re-derives a rule, and no status literal is compared. WHO may act is a separate axis
 * (`canOperateDeployment`, owner or admin). All of it is presentation: the backend is the
 * enforcement point and still refuses what a stale page offers.
 */
import { isStateRefusal, refusalCode } from "../api/refusal";
import { ApiError, transportErrorMessage } from "../api/client";
import type { DeployControls } from "../types/api";

/**
 * The backend's `DeployRefusalCode` values: what a refused confirm or rollback says in its 4xx
 * `detail` prefix, and what `confirm_blocked_reason` / `rollback_blocked_reason` carry. A contract
 * string crossing a boundary, so each is a literal here and pinned as a literal in the test: a
 * one-sided rename is invisible to `tsc` and would degrade to the generic message.
 */
export const DeployRefusalCode = {
  PROJECT_ARCHIVED: "PROJECT_ARCHIVED",
  DEPLOYMENT_IN_FLIGHT: "DEPLOYMENT_IN_FLIGHT",
  EVAL_PROJECT: "EVAL_PROJECT",
  POSTGRES_UNCONFIGURED: "POSTGRES_UNCONFIGURED",
  BUILD_NOT_SUCCEEDED: "BUILD_NOT_SUCCEEDED",
  NOT_DEPLOY_ELIGIBLE: "NOT_DEPLOY_ELIGIBLE",
  NOT_LATEST_SUCCESSFUL: "NOT_LATEST_SUCCESSFUL",
  IMAGE_MISSING: "IMAGE_MISSING",
  NO_ROLLBACK_TARGET: "NO_ROLLBACK_TARGET",
  NO_SUCCESSFUL_BUILD: "NO_SUCCESSFUL_BUILD",
  DOCKER_UNAVAILABLE: "DOCKER_UNAVAILABLE",
} as const;

/** How often the page reads again while something is in flight (builds take minutes; a
 * deployment tens of seconds). The next read is scheduled after the last one settled
 * (`loading/poller.ts`), so a slow server is never asked twice at once. */
export const PROJECT_POLL_INTERVAL_MS = 5000;

// -- what to offer ---------------------------------------------------------------------------------

/** The build `배포 확정` would confirm, or null when the backend would refuse every build. Null
 * `controls` (a response without them) fails closed: nothing is offered. */
export function confirmTargetBuildId(controls: DeployControls | null): string | null {
  const id = controls?.confirmable_build_id;
  return typeof id === "string" && id !== "" ? id : null;
}

export function canOfferConfirm(controls: DeployControls | null): boolean {
  return confirmTargetBuildId(controls) !== null;
}

export function canOfferRollback(controls: DeployControls | null): boolean {
  const id = controls?.rollback_target_id;
  return typeof id === "string" && id !== "";
}

export function canOfferStop(controls: DeployControls | null): boolean {
  const id = controls?.active_deployment_id;
  return typeof id === "string" && id !== "";
}

/** Whether any deploy control would be offered: what decides if a viewer who may not operate them
 * is told so (a project with nothing to operate has nothing to explain). */
export function offersAnyDeployControl(controls: DeployControls | null): boolean {
  return canOfferConfirm(controls) || canOfferRollback(controls) || canOfferStop(controls);
}

// -- polling ---------------------------------------------------------------------------------------

/** Poll iff something is still moving: a build QUEUED/RUNNING or a deployment DEPLOYING. The
 * backend says so (`in_flight`); with no controls there is nothing to poll for. */
export function shouldPollProject(controls: DeployControls | null): boolean {
  return controls?.in_flight === true;
}

// -- what to say -----------------------------------------------------------------------------------

const REFUSAL_MESSAGES: Record<string, string> = {
  [DeployRefusalCode.PROJECT_ARCHIVED]:
    "보관된 프로젝트는 배포하거나 롤백할 수 없습니다. 먼저 보관을 해제하세요.",
  [DeployRefusalCode.DEPLOYMENT_IN_FLIGHT]:
    "이미 진행 중인 배포가 있습니다. 끝난 뒤에 다시 시도하세요.",
  [DeployRefusalCode.EVAL_PROJECT]: "평가용 프로젝트는 배포할 수 없습니다.",
  [DeployRefusalCode.POSTGRES_UNCONFIGURED]:
    "플랫폼에 서비스 데이터베이스 설정(SERVICE_POSTGRES_ADMIN_DSN)이 없어 배포할 수 없습니다. 운영자에게 설정을 요청하세요.",
  [DeployRefusalCode.BUILD_NOT_SUCCEEDED]: "성공하지 않은 빌드는 배포할 수 없습니다.",
  [DeployRefusalCode.NOT_DEPLOY_ELIGIBLE]:
    "가장 최근에 성공한 빌드가 아직 승인되지 않아 배포할 수 없습니다. 승인이 끝나면 배포할 수 있습니다.",
  [DeployRefusalCode.NOT_LATEST_SUCCESSFUL]:
    "더 최근에 성공한 빌드가 있어 이 빌드는 배포할 수 없습니다. 화면을 새로 고쳐 최신 빌드를 확인하세요.",
  [DeployRefusalCode.IMAGE_MISSING]:
    "빌드 이미지가 서버에 없어 배포할 수 없습니다. 변경 요청을 다시 보내 새로 빌드하세요.",
  [DeployRefusalCode.NO_ROLLBACK_TARGET]: "되돌아갈 이전 배포가 없습니다.",
  [DeployRefusalCode.NO_SUCCESSFUL_BUILD]: "성공한 빌드가 아직 없어 배포할 수 없습니다.",
  [DeployRefusalCode.DOCKER_UNAVAILABLE]:
    "Docker에 연결할 수 없어 배포 이미지를 확인하지 못했습니다. 잠시 후 새로 고침하세요.",
};

/** The Korean sentence for a refusal code, or `null` for a code this build has never heard of (a
 * newer backend). Own-property lookup: a code is data from the server. */
export function deployRefusalMessage(code: string | null | undefined): string | null {
  if (code === null || code === undefined) return null;
  return Object.prototype.hasOwnProperty.call(REFUSAL_MESSAGES, code) ? REFUSAL_MESSAGES[code] : null;
}

/** Why `배포 확정` is not offered, in words; `null` when it is offered, or when the reason is a
 * code this build cannot word (then the control is simply absent). */
export function confirmBlockedMessage(controls: DeployControls | null): string | null {
  if (controls === null || canOfferConfirm(controls)) return null;
  return deployRefusalMessage(controls.confirm_blocked_reason);
}

export type DeployAction = "confirm" | "rollback" | "stop";

const ACTION_FALLBACKS: Record<DeployAction, string> = {
  confirm: "배포 확정에 실패했습니다.",
  rollback: "롤백에 실패했습니다.",
  stop: "중지에 실패했습니다.",
};

const CONFLICT_MESSAGE = "다른 작업과 겹쳐 처리하지 못했습니다. 화면을 새로 고친 뒤 다시 시도하세요.";
const DOCKER_UNREACHABLE_MESSAGE = "Docker에 연결하지 못해 처리하지 못했습니다. 잠시 후 다시 시도하세요.";

/**
 * What the page says when a confirm, a rollback or a stop is refused or fails. A failure that
 * never reached the server says so (`transportErrorMessage`); a refusal the backend coded gets the
 * sentence for that code (archived, in flight, not the latest, ...); a `503` says Docker; a `409`
 * with no code a conflict; anything else the action's own fallback. The backend's English `detail`
 * never reaches the screen.
 */
export function deployActionFailureMessage(err: unknown, action: DeployAction): string {
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  const coded = deployRefusalMessage(refusalCode(err));
  if (coded !== null) return coded;
  if (err instanceof ApiError && err.kind === "http") {
    if (err.status === 503) return DOCKER_UNREACHABLE_MESSAGE;
    if (err.status === 409) return CONFLICT_MESSAGE;
  }
  return ACTION_FALLBACKS[action];
}

/** After a refused action the page reads again: a 400 or 409 means what it showed has moved on
 * (a newer build, an archive, a deployment that started), and the control that was just refused
 * should not stay on screen. */
export function shouldRefreshAfterDeployFailure(err: unknown): boolean {
  return isStateRefusal(err);
}

// -- the failure alert's lifetime ----------------------------------------------------------

/** A refused or failed confirm / rollback / stop as the page keeps it: the sentence, and the
 * backend's refusal code (null when it sent none), which is what says WHICH condition the sentence
 * reports, so a later read can tell whether that condition still holds. */
export interface DeployFailure {
  message: string;
  code: string | null;
}

export function deployActionFailure(err: unknown, action: DeployAction): DeployFailure {
  return { message: deployActionFailureMessage(err, action), code: refusalCode(err) };
}

/** What can happen to a standing deploy-failure alert. `refresh` is the reader pressing `새로 고침`;
 * `change_request` is a submitted change request (the build it starts changes what a refusal was
 * about); `read` is a read of the project that landed (a poll, a refresh, a re-read after a
 * refusal), carrying only what the rule below looks at. */
export type DeployFailureEvent =
  | { kind: "refresh" }
  | { kind: "change_request" }
  | { kind: "read"; controls: DeployControls | null; archived: boolean };

/**
 * What stays of the deploy failure alert (the un-archive alert's rule, written for the one
 * that outlived its cause). The alert is a result of one attempt, so it ends when the reader moves
 * on (`refresh`, `change_request`; a new action or an un-archive clears it where it starts), and it
 * ends when a read shows the condition it reported is gone:
 *
 * - `DEPLOYMENT_IN_FLIGHT` ("이미 진행 중인 배포가 있습니다") holds while the backend says work is in
 *   flight (`controls.in_flight`). That field also covers a running build, so the alert can outlast
 *   its deployment by a build's length, never end early. With no controls in the read, nothing says
 *   it still holds, so it goes.
 * - `PROJECT_ARCHIVED` holds while the project is archived.
 * - Any other refusal reports something a read cannot show to be over (a build that is not
 *   eligible, an image that is missing), so it stays until the reader moves on.
 *
 * The un-archive alert's own rule stays in `unarchive.ts`: its condition is the control going away,
 * which a `DeployControls` read does not express, so the two share the shape and not the code.
 */
export function deployFailureAfter(failure: DeployFailure | null, event: DeployFailureEvent): DeployFailure | null {
  if (failure === null) return null;
  switch (event.kind) {
    case "refresh":
    case "change_request":
      return null;
    case "read":
      if (failure.code === DeployRefusalCode.DEPLOYMENT_IN_FLIGHT) {
        return event.controls?.in_flight === true ? failure : null;
      }
      if (failure.code === DeployRefusalCode.PROJECT_ARCHIVED) return event.archived ? failure : null;
      return failure;
  }
}
