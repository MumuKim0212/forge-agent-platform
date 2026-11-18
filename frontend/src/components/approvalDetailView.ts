// What the approval detail's pinned
// header, tab labels and summary headlines SAY, as pure functions so the contract is testable without
// mounting anything (ADR 0021). Every function reads only what the platform measured (counts, the
// smoke check, the policy verdict, the preview start failure) or whether the agent wrote a summary;
// none reads the agent's text or any path. A fact that was NOT measured is worded as not measured
// and is never read as a pass.

import { APPROVE_DECISION } from "../auth/approvalGate";
import type { ApprovalRequest, PolicyVerdict } from "../types/api";
import {
  CHANGED_NOT_MEASURED_LINE,
  agentSummaryView,
  changedCountsLine,
  changedTotal,
  driftView,
  smokeChecksView,
} from "./measuredFactsLines";
import { GATE_ERROR_CHECK, policyCheckLabels } from "./policyCheckDisplay";
import { asPreviewStartFailure, previewStartFailureLabel } from "./previewStartFailure";

/** The fields of an approval request the header, the tab counts and the headlines read. */
export type DetailSource = Pick<
  ApprovalRequest,
  | "changed_files"
  | "protected_path_drift"
  | "smoke_checks"
  | "preview_start_failure"
  | "policy_verdict"
  | "agent_summary"
  | "decisions"
>;

// --- the four verdict chips -------------------------------------------------------------------

/** A chip may be coloured, never only coloured: the value is always words. */
export type ChipTone = "neutral" | "warning" | "danger";

export interface VerdictChip {
  id: "changes" | "preview" | "policy" | "agent";
  label: string;
  value: string;
  tone: ChipTone;
}

/** The wire value of a flagged check's `result`. A contract string, pinned in the test. */
const FLAGGED_RESULT = "flag";

/** The one definition of a check the policy gate flagged. */
export function isFlaggedCheck(check: { result: string }): boolean {
  return check.result === FLAGGED_RESULT;
}

/** The names of the flagged checks, in the verdict's order. */
export function flaggedCheckNames(verdict: PolicyVerdict | null | undefined): string[] {
  if (!verdict) return [];
  return verdict.checks.filter(isFlaggedCheck).map((check) => check.check_name);
}

function changesChip(source: DetailSource): VerdictChip {
  const total = changedTotal(source.changed_files);
  if (total === null) return { id: "changes", label: "변경", value: "측정 안 됨", tone: "warning" };
  const reverted = source.protected_path_drift?.length ?? 0;
  if (reverted > 0) return { id: "changes", label: "변경", value: `${total}개 · 원복 ${reverted}`, tone: "warning" };
  return { id: "changes", label: "변경", value: `${total}개`, tone: "neutral" };
}

function previewChip(source: DetailSource): VerdictChip {
  // No preview ran at all: a different fact from a smoke check that was not measured.
  if (asPreviewStartFailure(source.preview_start_failure) !== null) {
    return { id: "preview", label: "프리뷰", value: "시작 실패", tone: "danger" };
  }
  const view = smokeChecksView(source.smoke_checks);
  if (!view.measured) return { id: "preview", label: "프리뷰", value: "측정 안 됨", tone: "warning" };
  if (view.routes.length === 0) return { id: "preview", label: "프리뷰", value: "점검할 경로 없음", tone: "neutral" };
  const failed = view.routes.filter((route) => route.failed).length;
  if (failed > 0) return { id: "preview", label: "프리뷰", value: `${failed}/${view.routes.length} 실패`, tone: "danger" };
  return { id: "preview", label: "프리뷰", value: "실패 없음", tone: "neutral" };
}

function policyChip(source: DetailSource): VerdictChip {
  const verdict = source.policy_verdict;
  // No verdict, or one with no check in it, measured nothing: never "통과".
  if (!verdict || verdict.checks.length === 0) {
    return { id: "policy", label: "정책", value: "결과 없음", tone: "warning" };
  }
  if (verdict.checks.some((check) => check.check_name === GATE_ERROR_CHECK)) {
    return { id: "policy", label: "정책", value: policyCheckLabels([GATE_ERROR_CHECK]), tone: "danger" };
  }
  const flagged = flaggedCheckNames(verdict).length;
  if (flagged > 0) return { id: "policy", label: "정책", value: `지적 ${flagged}`, tone: "danger" };
  return { id: "policy", label: "정책", value: "통과", tone: "neutral" };
}

function agentChip(source: DetailSource): VerdictChip {
  if (agentSummaryView(source.agent_summary) !== null) {
    return { id: "agent", label: "에이전트", value: "있음 · 미확인", tone: "neutral" };
  }
  return { id: "agent", label: "에이전트", value: "없음", tone: "neutral" };
}

/**
 * The four chips of the detail header, in a set order: 변경, 프리뷰, 정책, 에이전트. Text only: a
 * label and a value, with a tone that may colour them. The agent chip says only that a summary
 * exists and the platform has not checked it.
 */
export function verdictChips(source: DetailSource): VerdictChip[] {
  return [changesChip(source), previewChip(source), policyChip(source), agentChip(source)];
}

// --- the tab labels and their counts -----------------------------------------------------------

export type DetailTabId = "summary" | "changes" | "checks" | "history";

/** The tab a request opens on; choosing another request returns to it. */
export const DEFAULT_DETAIL_TAB: DetailTabId = "summary";

/** The 변경 tab's count: the TRUE number of changed files (the totals), not the stored entries. Null
 * when the build was not measured (then the tab shows no count: null is not zero). */
export function changedTabCount(source: DetailSource): number | null {
  return changedTotal(source.changed_files);
}

/**
 * The 점검 tab's count: the items that need an approver's attention. The flagged policy checks, plus
 * each smoke route that failed, plus one when the preview failed to start. A fact that was not
 * measured adds nothing here (the chip says "측정 안 됨"); it is not an item the platform found.
 */
export function attentionCount(source: DetailSource): number {
  const view = smokeChecksView(source.smoke_checks);
  const failedRoutes = view.routes.filter((route) => route.failed).length;
  const startFailure = asPreviewStartFailure(source.preview_start_failure) !== null ? 1 : 0;
  return flaggedCheckNames(source.policy_verdict).length + failedRoutes + startFailure;
}

/** The 이력 tab's count: how many decisions were recorded. */
export function historyTabCount(source: DetailSource): number {
  return source.decisions.length;
}

/** `변경 (640)`, or just the name when there is no count to show. */
export function tabLabel(name: string, count: number | null): string {
  return count === null ? name : `${name} (${count})`;
}

export interface DetailTab {
  id: DetailTabId;
  label: string;
}

/** The four tabs in their set order, each label carrying its count (요약 has none). */
export function detailTabs(source: DetailSource): DetailTab[] {
  return [
    { id: "summary", label: "요약" },
    { id: "changes", label: tabLabel("변경", changedTabCount(source)) },
    { id: "checks", label: tabLabel("점검", attentionCount(source)) },
    { id: "history", label: tabLabel("이력", historyTabCount(source)) },
  ];
}

// --- the 요약 tab's headlines (plain text, no pointer to another tab) -------------------------------

/**
 * One row of the 요약 tab's measured-facts card. A row reads ONCE: `label` is set only where the text
 * does not already name its subject (the smoke and policy lines); the changed-files counts line starts
 * with `변경 파일` and the start failure with `프리뷰를 시작하지 못함`, so they carry no label. The drift
 * row has no text here: its paths are agent-chosen, so the page draws them as literal code text.
 */
export interface SummaryRow {
  id: "changed" | "drift" | "previewStart" | "smoke" | "policy";
  label: string | null;
  text: string | null;
}

/** The policy headline: how many checks ran, how many were flagged, which (Korean names). */
export function policyHeadline(verdict: PolicyVerdict | null | undefined): string {
  if (!verdict || verdict.checks.length === 0) {
    return "정책 검사 결과가 없습니다. 이는 통과했다는 뜻이 아닙니다.";
  }
  const names = flaggedCheckNames(verdict);
  if (names.length === 0) return `검사 ${verdict.checks.length}개 중 지적 없음`;
  return `검사 ${verdict.checks.length}개 중 지적 ${names.length} — ${policyCheckLabels(names)}`;
}

/** What the 요약 tab says about an agent-written summary: that it exists, is unchecked, and sits at
 * the end of the 점검 tab. The words only; the tab does not link there. */
export function agentSummaryNotice(text: string | null | undefined): string | null {
  if (agentSummaryView(text) === null) return null;
  return "에이전트가 작성한 요약이 있습니다. 플랫폼이 확인하지 않은 내용이며, 점검 탭의 맨 끝에 접혀 있습니다.";
}

/** The rows of the 요약 measured-facts card, in order: the counts line, the drift row (only when paths were
 * restored), the preview start failure (only when it failed), the smoke headline, the policy headline. */
export function summaryRows(source: DetailSource): SummaryRow[] {
  const rows: SummaryRow[] = [
    { id: "changed", label: null, text: changedCountsLine(source.changed_files) ?? CHANGED_NOT_MEASURED_LINE },
  ];
  if (driftView(source.protected_path_drift) !== null) rows.push({ id: "drift", label: null, text: null });
  const failure = asPreviewStartFailure(source.preview_start_failure);
  if (failure !== null) rows.push({ id: "previewStart", label: null, text: previewStartFailureLabel(failure) });
  rows.push({ id: "smoke", label: "프리뷰 응답 점검", text: smokeChecksView(source.smoke_checks).summary });
  rows.push({ id: "policy", label: "정책 검사", text: policyHeadline(source.policy_verdict) });
  return rows;
}

// --- the action bar's approvals line ---------------------------------------------------------------

/** How many approve decisions the request has so far (the reject decision is not one). */
export function approvalsSoFar(decisions: ReadonlyArray<{ decision: string }>): number {
  return decisions.filter((d) => d.decision === APPROVE_DECISION).length;
}

/** `필요 승인 수 1 · 현재 0`: the line the action bar shows beside the buttons. */
export function approvalsLine(required: number, decisions: ReadonlyArray<{ decision: string }>): string {
  return `필요 승인 수 ${required} · 현재 ${approvalsSoFar(decisions)}`;
}
