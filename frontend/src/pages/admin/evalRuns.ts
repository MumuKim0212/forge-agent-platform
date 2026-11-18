/**
 * The 평가 tab's decision logic. Pure
 * functions over the API's shapes, so what an admin reads and when the start control may be pressed
 * have truth tables (ADR 0021: nothing can be mounted in the node test environment).
 *
 * Rules this file carries:
 * - Starting a run spends real money, so the control is DISABLED, with the reason shown, whenever it
 *   cannot work or is not safe: a run in progress, an inactive executor, or the selected engine
 *   unavailable. It fails CLOSED: until both the runs and the engine selection are known, it
 *   is disabled.
 * - The budget, the executor and the selected engine come from the backend's status read
 *   (`GET /evals/status`), never from a past run or a constant here: a confirmation must not state a
 *   cap the server does not apply.
 * - A cost the proxy could not measure (null) never renders as `$0.00`.
 * - Server text is never put on screen: every label and sentence is worded here from codes
 *   and structured fields. A response body is never stored by the backend, so there is none to show.
 */
import { riskTierLabel } from "../../components/approvalDisplay";
import { buildFailureReasonLabel } from "../../components/buildFailure";
import { engineLabel } from "../../components/engineDisplay";
import { caseAttemptsText, repairSettingLabel } from "../../components/repairAttempts";
import { policyCheckLabels } from "../../components/policyCheckDisplay";
import type { BadgeTone } from "../../components/ui/Badge";
import { visibleControls } from "../../components/ui/visibleControls";
import type { EvalCase, EvalCheckOutcome, EvalRun, EvalStatus } from "../../types/api";
import { NO_MODEL_TEXT } from "./agentEngine";
import { formatUsd } from "./usageDisplay";

/** How often a run in progress is re-read. */
export const POLL_INTERVAL_MS = 5000;

export const EvalRunStatus = {
  RUNNING: "RUNNING",
  COMPLETED: "COMPLETED",
  STOPPED_BUDGET: "STOPPED_BUDGET",
  FAILED: "FAILED",
} as const;

export const EvalCaseResult = {
  PASSED: "PASSED",
  FAILED: "FAILED",
  NOT_RUN_BUDGET: "NOT_RUN_BUDGET",
} as const;

// ---------------------------------------------------------------------------------------------
// Money, time
// ---------------------------------------------------------------------------------------------

export const UNMEASURED_COST_TEXT = "측정되지 않음";

/** A spend figure. `formatUsd` (two decimals) would show a real but tiny cost as `$0.00`, which
 * reads as "free"; below one cent it shows four decimals instead. */
export function formatCost(amount: number): string {
  return amount > 0 && amount < 0.01 ? `$${amount.toFixed(4)}` : formatUsd(amount);
}

/** A case's cost: the proxy's measurement, or the explicit absence of one. */
export function caseCostText(cost: number | null): string {
  return cost === null ? UNMEASURED_COST_TEXT : formatCost(cost);
}

export function formatDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return null;
  const whole = Math.round(seconds);
  if (whole < 60) return `${whole}초`;
  return `${Math.floor(whole / 60)}분 ${whole % 60}초`;
}

export function formatTokens(tokens: number | null): string {
  return tokens === null ? "—" : tokens.toLocaleString("en-US");
}

function defaultFormat(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

// ---------------------------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------------------------

const RUN_STATUS_LABELS: Record<string, string> = {
  RUNNING: "진행 중",
  COMPLETED: "완료",
  STOPPED_BUDGET: "예산 소진으로 중단",
  FAILED: "실패",
};

const RUN_STATUS_TONES: Record<string, BadgeTone> = {
  RUNNING: "accent",
  COMPLETED: "success",
  STOPPED_BUDGET: "warning",
  FAILED: "danger",
};

export const UNKNOWN_RUN_STATUS_LABEL = "알 수 없는 상태";

export function runStatusLabel(status: string): string {
  return Object.prototype.hasOwnProperty.call(RUN_STATUS_LABELS, status)
    ? RUN_STATUS_LABELS[status]
    : UNKNOWN_RUN_STATUS_LABEL;
}

export function runStatusTone(status: string): BadgeTone {
  return Object.prototype.hasOwnProperty.call(RUN_STATUS_TONES, status) ? RUN_STATUS_TONES[status] : "neutral";
}

/** True only for a run that is in progress. An unrecognised status is NOT in progress: it is
 * shown as unknown, and cannot keep the page polling forever. */
export function isRunInProgress(run: Pick<EvalRun, "status">): boolean {
  return run.status === EvalRunStatus.RUNNING;
}

/** Whether the runs list should be re-read: while any run is in progress, and not after. */
export function shouldPoll(runs: ReadonlyArray<Pick<EvalRun, "status">>): boolean {
  return runs.some(isRunInProgress);
}

/** A standing sentence for a run that did not complete, in place of the server's English detail. */
export function runOutcomeNote(run: Pick<EvalRun, "status">): string | null {
  switch (run.status) {
    case EvalRunStatus.STOPPED_BUDGET:
      return "실행 예산이 남지 않아 나머지 케이스를 실행하지 않았습니다. 이미 실행한 케이스의 결과는 유효합니다.";
    case EvalRunStatus.FAILED:
      return "실행을 끝까지 진행하지 못했습니다. 진행 중이던 케이스는 실패로 기록되고 시작하지 못한 케이스는 대기 상태로 남습니다. 사유는 서버 로그와 감사 로그에서 확인하세요.";
    default:
      return null;
  }
}

export const NO_FINGERPRINT_TEXT = "기록 없음";

// which system prompt a run was made under. A run from before it was recorded has none.
export function fingerprintLabel(fingerprint: string | null): string {
  return fingerprint !== null && fingerprint !== "" ? fingerprint : NO_FINGERPRINT_TEXT;
}

export interface RunRowView {
  id: string;
  engineText: string;
  modelText: string;
  versionText: string;
  fingerprintText: string;
  // the repair setting the run recorded (`수리 설정 기록 없음` when NULL, never 0). Not a verdict.
  repairSettingText: string;
  statusLabel: string;
  statusTone: BadgeTone;
  inProgress: boolean;
  passedText: string;
  spendText: string;
  startedByText: string;
  startedAtText: string;
  durationText: string;
}

export function runRows(
  runs: ReadonlyArray<EvalRun>,
  format: (iso: string) => string = defaultFormat,
): RunRowView[] {
  return runs.map((run) => {
    const inProgress = isRunInProgress(run);
    const duration = formatDuration(run.duration_seconds);
    return {
      id: run.id,
      engineText: engineLabel(run.engine),
      modelText: run.model !== null && run.model !== "" ? run.model : NO_MODEL_TEXT,
      versionText: `v${run.request_set_version}`,
      fingerprintText: fingerprintLabel(run.prompt_fingerprint),
      repairSettingText: repairSettingLabel(run.repair_max_attempts),
      statusLabel: runStatusLabel(run.status),
      statusTone: runStatusTone(run.status),
      inProgress,
      passedText:
        run.not_run > 0 ? `${run.passed} / ${run.total} (미실행 ${run.not_run})` : `${run.passed} / ${run.total}`,
      // While running, the spend is only the cases already judged.
      spendText: `${formatCost(run.spend_usd)} / ${formatCost(run.budget_usd)}`,
      startedByText:
        run.started_by_name !== null && run.started_by_name !== ""
          ? visibleControls(run.started_by_name)
          : "알 수 없는 사용자",
      startedAtText: format(run.started_at),
      durationText: duration ?? (inProgress ? "진행 중" : "—"),
    };
  });
}

// ---------------------------------------------------------------------------------------------
// The start control
// ---------------------------------------------------------------------------------------------

export const REASON_LOADING = "실행 상태를 불러오는 중이라 아직 시작할 수 없습니다.";
export const REASON_RUN_IN_PROGRESS = "평가 실행이 진행 중입니다. 끝난 뒤에 새로 시작할 수 있습니다.";
export const REASON_EXECUTOR_INACTIVE =
  "샌드박스 실행기가 비활성이라 빌드를 실행할 수 없습니다. 서버 설정(USE_SANDBOX_EXECUTOR)과 에이전트 엔진 키를 확인하세요.";
export const REASON_ENGINE_UNAVAILABLE =
  "선택된 에이전트 엔진의 키가 설정되어 있지 않아 시작할 수 없습니다. 에이전트 엔진 탭에서 사용 가능한 엔진을 선택하세요.";
export const REASON_REQUEST_SET_UNUSABLE =
  "평가 요청 세트를 불러올 수 없어 시작할 수 없습니다. 서버 로그를 확인하세요.";

export interface StartControlState {
  disabled: boolean;
  /** Why it is disabled; null when it is enabled. */
  reason: string | null;
}

/**
 * Whether a run may be started now, from the backend's own status read (`GET /evals/status`,
 * which is computed by the same functions `POST /evals/runs` refuses on) and the runs list. Checked
 * in a fixed order, the first applicable reason wins. A missing input means "not loaded yet", which
 * disables (fail closed). The runs list is consulted as well as the status's `run_in_progress`,
 * because the list is the one that is re-read while a run is in progress.
 */
export function startControlState(
  status: EvalStatus | null,
  runs: ReadonlyArray<Pick<EvalRun, "status">> | null,
): StartControlState {
  if (status === null || runs === null) return { disabled: true, reason: REASON_LOADING };
  if (status.run_in_progress || runs.some(isRunInProgress)) {
    return { disabled: true, reason: REASON_RUN_IN_PROGRESS };
  }
  if (!status.executor_active) return { disabled: true, reason: REASON_EXECUTOR_INACTIVE };
  if (!status.engine_available) return { disabled: true, reason: REASON_ENGINE_UNAVAILABLE };
  if (status.request_set_version === null || status.case_count === null) {
    return { disabled: true, reason: REASON_REQUEST_SET_UNUSABLE };
  }
  return { disabled: false, reason: null };
}

/** The line stated beside the start control: the server's effective run budget. */
export function budgetHeadline(status: Pick<EvalStatus, "budget_usd">): string {
  return `회당 최대 ${formatCost(status.budget_usd)}`;
}

/** What the budget means, from the same read: the floor below which no further case starts. */
export function budgetNote(status: Pick<EvalStatus, "budget_usd" | "case_min_budget_usd">): string {
  return `실행 예산 ${formatCost(status.budget_usd)}은 판정이 끝난 케이스의 실측 비용으로 차감되고, 남은 예산이 ${formatCost(status.case_min_budget_usd)} 미만이면 나머지 케이스는 실행하지 않습니다.`;
}

export interface ConfirmationView {
  title: string;
  engineText: string;
  modelText: string;
  maxSpendText: string;
  setText: string;
  warning: string;
}

/** What the confirmation names before a run starts: the engine and model that WILL run (the
 * currently selected ones), the most it can spend, and what it will run. Null when the status names
 * no engine or no request set. */
export function confirmationView(status: EvalStatus): ConfirmationView | null {
  if (status.engine === null || status.request_set_version === null || status.case_count === null) return null;
  return {
    title: "평가 실행을 시작할까요?",
    engineText: engineLabel(status.engine),
    modelText: status.model !== null && status.model !== "" ? status.model : NO_MODEL_TEXT,
    maxSpendText: `최대 ${formatCost(status.budget_usd)}`,
    setText: `요청 세트 v${status.request_set_version} · 케이스 ${status.case_count}개`,
    warning:
      "실제 LLM 호출 비용이 발생합니다. 시작하면 요청 세트의 케이스가 순서대로 빌드되고, 실행 예산이 소진되면 중단됩니다.",
  };
}

/** The message when `POST /evals/runs` is refused. Status codes are the contract; the server's
 * `detail` is never put on screen. */
export function evalStartFailureMessage(status: number): string {
  switch (status) {
    case 409:
      return "이미 진행 중인 평가 실행이 있어 시작하지 않았습니다. 목록을 새로 고쳐 확인하세요.";
    case 422:
      return "평가 실행을 시작할 수 없습니다. 샌드박스 실행기가 비활성이거나 선택된 에이전트 엔진을 사용할 수 없습니다. 서버 설정과 에이전트 엔진 탭을 확인하세요.";
    case 403:
      return "평가 실행은 admin만 할 수 있습니다. 방금 본인 권한이 바뀌었다면 새로 고침 후 다시 확인하세요.";
    default:
      return "평가 실행을 시작하지 못했습니다. 실행이 시작되었는지 아래 목록에서 확인하세요.";
  }
}

export function startSuccessMessage(run: Pick<EvalRun, "engine" | "model" | "budget_usd">): string {
  const model = run.model !== null && run.model !== "" ? run.model : NO_MODEL_TEXT;
  return `평가 실행을 시작했습니다: ${engineLabel(run.engine)} · ${model}, 예산 ${formatCost(run.budget_usd)}. 케이스는 워커가 순서대로 실행합니다.`;
}

// ---------------------------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------------------------

const CASE_RESULT_LABELS: Record<string, string> = {
  PASSED: "통과",
  FAILED: "실패",
  NOT_RUN_BUDGET: "예산 부족으로 실행 안 함",
};

const CASE_RESULT_TONES: Record<string, BadgeTone> = {
  PASSED: "success",
  FAILED: "danger",
  NOT_RUN_BUDGET: "warning",
};

const CASE_STATE_LABELS: Record<string, string> = {
  PENDING: "대기 중",
  RUNNING: "빌드 진행 중",
};

/** The eval-specific reason codes (`models/eval_run.EvalFailureReason`); a failed BUILD records the
 * build's own code, worded by `buildFailureReasonLabel`. */
const EVAL_REASON_LABELS: Record<string, string> = {
  PREVIEW_UNHEALTHY: "프리뷰가 정상 응답하지 않음 (/healthz)",
  NOT_RUN_BUDGET: "실행 예산이 남지 않아 실행하지 않음",
  RUN_STUCK: "실행이 멈춰 중단됨",
  ENGINE_CHANGED: "빌드가 실행 도중 바뀐 엔진에서 돌아 평가하지 않음",
};

export function caseResultLabel(result: string | null, state: string): string {
  if (result === null) {
    return Object.prototype.hasOwnProperty.call(CASE_STATE_LABELS, state)
      ? CASE_STATE_LABELS[state]
      : "알 수 없는 상태";
  }
  return Object.prototype.hasOwnProperty.call(CASE_RESULT_LABELS, result)
    ? CASE_RESULT_LABELS[result]
    : "알 수 없는 결과";
}

export function caseResultTone(result: string | null): BadgeTone {
  if (result === null) return "neutral";
  return Object.prototype.hasOwnProperty.call(CASE_RESULT_TONES, result) ? CASE_RESULT_TONES[result] : "neutral";
}

/** The Korean reason a case failed, or null when it recorded none. `CHECK_FAILED` names the failing
 * check by its 1-based number. */
export function failureText(failureReason: string | null, failedCheckIndex: number | null): string | null {
  if (failureReason === null) return null;
  if (failureReason === "CHECK_FAILED") {
    return failedCheckIndex === null ? "검사 실패" : `검사 ${failedCheckIndex + 1}번 실패`;
  }
  return Object.prototype.hasOwnProperty.call(EVAL_REASON_LABELS, failureReason)
    ? EVAL_REASON_LABELS[failureReason]
    : buildFailureReasonLabel(failureReason);
}

/** One request the judge made, worded from its structured fields (never from the English outcome
 * string, and there is no body to show). Index null is the `/healthz` request. */
export function checkOutcomeLine(outcome: EvalCheckOutcome): string {
  const name = outcome.index === null ? "상태 확인" : `검사 ${outcome.index + 1}`;
  const verdict = outcome.ok ? "통과" : "실패";
  let detail: string;
  if (outcome.status === null) detail = "요청을 보내지 못함";
  else if (outcome.status !== outcome.expected_status)
    detail = `응답 ${outcome.status}, 기대 ${outcome.expected_status}`;
  else if (!outcome.ok) detail = `응답 ${outcome.status}, 본문에 기대한 문구가 없음`;
  else detail = `응답 ${outcome.status}`;
  return `${name} · ${outcome.method} ${outcome.path} · ${verdict} (${detail})`;
}

/** The policy gate's verdict on the case's build, when it has one. Eval builds are gated but never
 * reach the approval queue, so this is the only place the verdict is seen. */
export function policyLine(tier: string | null, flagged: ReadonlyArray<string>): string | null {
  if (tier === null) return null;
  const label = riskTierLabel(tier);
  return flagged.length > 0
    ? `정책 게이트 리스크: ${label} · 지적된 검사: ${policyCheckLabels(flagged)}`
    : `정책 게이트 리스크: ${label}`;
}

/** The project page that carries the case's build and its trace (an eval project is an ordinary
 * project page, owned by the admin who started the run). Null until the case has a build. */
export function caseBuildPath(c: Pick<EvalCase, "project_id" | "build_id">): string | null {
  if (c.project_id === null || c.build_id === null) return null;
  return `/projects/${encodeURIComponent(c.project_id)}`;
}

export interface CaseRowView {
  id: string;
  caseId: string;
  resultLabel: string;
  resultTone: BadgeTone;
  failureText: string | null;
  checkLines: string[];
  policyText: string | null;
  costText: string;
  tokensText: string;
  // repair attempts the case's build ran; `—` unless the case was judged and measured.
  repairAttemptsText: string;
  durationText: string;
  buildPath: string | null;
}

/** A case that has not been judged, or was never run, has no measured cost to show. */
function hasMeasuredFigures(c: Pick<EvalCase, "state" | "result">): boolean {
  return c.state === "DONE" && c.result !== EvalCaseResult.NOT_RUN_BUDGET;
}

export function caseRows(cases: ReadonlyArray<EvalCase>): CaseRowView[] {
  return [...cases]
    .sort((a, b) => a.position - b.position)
    .map((c) => ({
      id: c.id,
      caseId: c.case_id,
      resultLabel: caseResultLabel(c.result, c.state),
      resultTone: caseResultTone(c.result),
      failureText: failureText(c.failure_reason, c.failed_check_index),
      checkLines: (c.check_outcomes ?? []).map(checkOutcomeLine),
      policyText: policyLine(c.policy_risk_tier, c.policy_flagged_checks),
      costText: hasMeasuredFigures(c) ? caseCostText(c.cost_usd) : "—",
      tokensText: hasMeasuredFigures(c) ? formatTokens(c.cost_tokens) : "—",
      repairAttemptsText: caseAttemptsText(c.repair_attempts, hasMeasuredFigures(c)),
      durationText: formatDuration(c.duration_seconds) ?? "—",
      buildPath: caseBuildPath(c),
    }));
}
