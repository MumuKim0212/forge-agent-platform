// how a failed build is worded on the project timeline.
//
// The backend sends two things for a failed build: a reason code
// (`failure_reason`, one of eight) and a platform-authored sentence
// (`failure_detail`). The code is what the person reads first, in Korean;
// the sentence is a secondary diagnostic. The server's
// English text is never the user-facing message, so the label is looked up
// here from the code and the sentence only ever trails it.

/** The eight reason codes of the build failure taxonomy. */
export const BUILD_FAILURE_REASON_LABELS: Record<string, string> = {
  TIMEOUT: "제한 시간 초과",
  RESOURCE_LIMIT: "자원 한도 초과",
  BUDGET_EXCEEDED: "빌드 예산 초과",
  ENGINE_UNREACHABLE: "AI 엔진에 연결할 수 없음",
  AGENT_ERROR: "에이전트 오류",
  HARVEST_ERROR: "결과물 수집 실패",
  INFRA_ERROR: "인프라 오류",
  // the build's output has no file the template's service starts from.
  MISSING_ENTRYPOINT: "앱의 시작 파일이 없어 빌드를 완료할 수 없음",
};

/** Shown for a code this client does not know (a newer backend). */
export const UNKNOWN_BUILD_FAILURE_LABEL = "알 수 없는 사유";

// one line saying what the requester can do next, per code. It names an action the
// requester has (try again, send a change request, ask an administrator), never a cause the
// platform cannot know, and never a log, path or command. Kept beside the labels so a code
// cannot gain one without the other: `buildFailure.test.ts` requires the same key set.
export const BUILD_FAILURE_NEXT_STEPS: Record<string, string> = {
  TIMEOUT: "다시 시도해 보세요. 반복되면 요청을 나누어 변경 요청으로 보내거나 관리자에게 문의하세요.",
  RESOURCE_LIMIT: "요청을 나누어 변경 요청으로 다시 보내 보세요. 반복되면 관리자에게 문의하세요.",
  BUDGET_EXCEEDED: "요청을 나누어 변경 요청으로 다시 보내 보세요. 더 큰 예산이 필요하면 관리자에게 문의하세요.",
  ENGINE_UNREACHABLE: "잠시 후 다시 시도해 보세요. 계속되면 관리자에게 문의하세요.",
  AGENT_ERROR: "요청을 다른 말로 바꾸어 변경 요청으로 다시 보내 보세요. 반복되면 관리자에게 문의하세요.",
  HARVEST_ERROR: "다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
  INFRA_ERROR: "다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
  MISSING_ENTRYPOINT: "변경 요청으로 서비스를 다시 만들어 달라고 요청해 보세요. 반복되면 관리자에게 문의하세요.",
};

/** The next step for a code this client does not know: the one action that is always open. */
export const UNKNOWN_BUILD_FAILURE_NEXT_STEP = "관리자에게 문의하세요.";

export function buildFailureNextStep(code: string): string {
  return Object.prototype.hasOwnProperty.call(BUILD_FAILURE_NEXT_STEPS, code)
    ? BUILD_FAILURE_NEXT_STEPS[code]
    : UNKNOWN_BUILD_FAILURE_NEXT_STEP;
}

// the platform's detail sentences for an operator end with a pointer into its own log
// ("...; see the worker log", "...; its output is in the worker log"). A requester has no
// worker log, so the display drops that clause. The stored text is unchanged (it stays useful to
// an operator reading the API), and older rows carry the phrase too, which a rewrite at the
// writers would not reach. `tests/test_worker_log_phrase_shapes.py` keeps the writers to the two
// tails below, so a new wording cannot reach the screen unnoticed.
const OPERATOR_LOG_TAIL = /\s*;?\s*(?:see the worker log|its output is in the worker log)\s*\.?\s*$/i;

/**
 * The detail as a requester reads it: the platform's sentence without its pointer to the worker
 * log, or null when nothing else is left. A detail that still names a worker log after the tail
 * is cut (a wording this function does not know) is dropped whole rather than shown.
 */
export function requesterBuildDetail(detail: string | null | undefined): string | null {
  const trimmed = typeof detail === "string" ? detail.trim() : "";
  const cut = trimmed.replace(OPERATOR_LOG_TAIL, "").trim();
  if (cut === "" || /worker log/i.test(cut)) return null;
  return cut;
}

export function buildFailureReasonLabel(code: string): string {
  // An own-property check, not a bare index: a code such as "constructor"
  // must not resolve to something inherited from the object prototype.
  return Object.prototype.hasOwnProperty.call(BUILD_FAILURE_REASON_LABELS, code)
    ? BUILD_FAILURE_REASON_LABELS[code]
    : UNKNOWN_BUILD_FAILURE_LABEL;
}

export interface BuildFailureLines {
  /** The headline: the Korean label, never the raw code. */
  primary: string;
  /** The diagnostic sentence, or null when the build recorded none (older rows). */
  secondary: string | null;
  /** what the requester can do next, a line of its own under the label. */
  nextStep: string;
}

/**
 * The two lines a failed build renders. A pure function so the wording
 * contract is testable without mounting anything (ADR 0021).
 *
 * `detail` is rendered as plain text by the caller; this function never
 * builds markup.
 */
export function buildFailureLines(
  reason: string,
  detail: string | null | undefined,
): BuildFailureLines {
  const shown = requesterBuildDetail(detail);
  return {
    primary: `실패 사유: ${buildFailureReasonLabel(reason)}`,
    secondary: shown === null ? null : `상세: ${shown}`,
    nextStep: `다음 단계: ${buildFailureNextStep(reason)}`,
  };
}
