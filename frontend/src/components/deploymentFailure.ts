// how a failed deployment's reason code is worded, the counterpart of
// `buildFailure.ts` for deployments.
//
// The backend records one reason code on a FAILED deployment
// (`failure_reason`, one of eight) and no
// sentence beside it. The code is an internal name, so every surface that
// shows a deployment failure looks the label up here instead of printing it.

/** The eight reason codes of the deployment failure taxonomy. */
export const DEPLOYMENT_FAILURE_REASON_LABELS: Record<string, string> = {
  MERGE_FAILED: "변경 병합 실패",
  IMAGE_MISSING: "빌드 이미지를 찾을 수 없음",
  START_FAILED: "서비스 시작 실패",
  HEALTH_TIMEOUT: "상태 확인 시간 초과",
  ROUTE_FAILED: "접속 경로 등록 실패",
  // A precondition, not a fault: nothing broke, a setup step
  // is missing, so the wording names the step rather than a failure.
  PROVISION_UNCONFIGURED: "데이터 서비스 설정이 필요함",
  PROVISION_FAILED: "데이터 서비스 준비 실패",
  INFRA_ERROR: "인프라 오류",
};

/** Shown for a code this client does not know (a newer backend). */
export const UNKNOWN_DEPLOYMENT_FAILURE_LABEL = "알 수 없는 사유";

// one line saying what the requester can do next, per code (same rules as
// `BUILD_FAILURE_NEXT_STEPS`: an action the requester has, no cause claimed, no log/path/command).
// A deployment has no detail text, so this line is all the explanation the screen can add.
export const DEPLOYMENT_FAILURE_NEXT_STEPS: Record<string, string> = {
  MERGE_FAILED: "배포를 다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
  IMAGE_MISSING: "변경 요청으로 서비스를 다시 만든 뒤 배포해 보세요. 반복되면 관리자에게 문의하세요.",
  START_FAILED: "배포를 다시 시도해 보세요. 반복되면 변경 요청으로 수정하거나 관리자에게 문의하세요.",
  HEALTH_TIMEOUT: "배포를 다시 시도해 보세요. 반복되면 변경 요청으로 수정하거나 관리자에게 문의하세요.",
  ROUTE_FAILED: "배포를 다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
  PROVISION_UNCONFIGURED: "관리자에게 데이터 서비스 설정을 요청하세요. 설정이 끝나면 배포할 수 있습니다.",
  PROVISION_FAILED: "배포를 다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
  INFRA_ERROR: "배포를 다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
};

/** The next step for a code this client does not know: the one action that is always open. */
export const UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP = "관리자에게 문의하세요.";

export function deploymentFailureNextStep(code: string): string {
  return Object.prototype.hasOwnProperty.call(DEPLOYMENT_FAILURE_NEXT_STEPS, code)
    ? DEPLOYMENT_FAILURE_NEXT_STEPS[code]
    : UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP;
}

export function deploymentFailureReasonLabel(code: string): string {
  // An own-property check, not a bare index: a code such as "constructor"
  // must not resolve to something inherited from the object prototype.
  return Object.prototype.hasOwnProperty.call(DEPLOYMENT_FAILURE_REASON_LABELS, code)
    ? DEPLOYMENT_FAILURE_REASON_LABELS[code]
    : UNKNOWN_DEPLOYMENT_FAILURE_LABEL;
}

/** The headline line a failed deployment renders: the label, never the code. */
export function deploymentFailureLine(code: string): string {
  return `실패 사유: ${deploymentFailureReasonLabel(code)}`;
}

/** The line under a failed deployment's headline: what the requester can do next. */
export function deploymentFailureNextStepLine(code: string): string {
  return `다음 단계: ${deploymentFailureNextStep(code)}`;
}
