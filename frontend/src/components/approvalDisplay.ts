/**
 * How a risk tier and an approval request's status read on screen: the one source
 * `ApprovalConsolePage` (list and detail), `Timeline` (the `policy_verdict` and
 * `approval_request` entries) and the eval run's policy line all read, where each used to keep
 * its own copy of the same labels and tones. The copies were once left apart on purpose; two copies
 * of one fact drift, and a tier shown red in one place and amber in the next is a defect.
 *
 * Pure, no React (the shape of `buildFailure.ts`), so a test reaches it. The wire values are
 * contract strings: `PolicyRiskTier` (`services/policy_checks.py`) and `ApprovalStatus`
 * (`models/approval.py`) on the backend. A value this build has never heard of is shown as it
 * was sent, in the neutral tone, never as one of the known ones.
 */
import { APPROVE_DECISION, REJECT_DECISION } from "../auth/approvalGate";
import { policyCheckLabels } from "./policyCheckDisplay";
import type { BadgeTone } from "./ui/Badge";

export const RISK_TIER_LABELS: Readonly<Record<string, string>> = {
  low: "낮음",
  medium: "중간",
  high: "높음",
};

export const RISK_LEGEND_LEAD = "위험도 — 정책 검사 결과";
export const RISK_LEGEND_CLOSING = "중간·높음은 승인자 1명의 확인이 필요합니다.";

// What each tier means, as the policy gate's checks it raises on. The check names come from `POLICY_CHECK_LABELS`, so the key and the lists that
// print a flagged check cannot drift apart.
const RISK_LEGEND_DESCRIPTIONS: Readonly<Record<"low" | "medium" | "high", string>> = {
  low: "지적 없음, 자동 승인",
  medium: `${policyCheckLabels(["dependency_vulnerabilities", "resource_limits", "template_compliance", "destructive_ddl"])} 중 하나가 지적됨`,
  high: `${policyCheckLabels(["secrets_pii", "egress_detection"])} 중 하나가 지적됨`,
};

export interface RiskLegendItem {
  tier: "low" | "medium" | "high";
  /** The badge's word, from `RISK_TIER_LABELS` (through `riskTierLabel`). */
  label: string;
  description: string;
}

/**
 * The key above the approval list that says what the 낮음 / 중간 / 높음 badges are (위험도, the policy
 * gate's risk tier, not a priority) and what each level means. The page draws each item as the real
 * risk badge (same label and tone helpers as the rows) followed by the description. The level words
 * come from `RISK_TIER_LABELS`, so a relabel there reaches the key. Fixed wording: nothing about it
 * changes because of an action, so it is never a live region.
 */
export function riskLegendItems(): RiskLegendItem[] {
  return (["low", "medium", "high"] as const).map((tier) => ({
    tier,
    label: riskTierLabel(tier),
    description: RISK_LEGEND_DESCRIPTIONS[tier],
  }));
}

// Tone knowledge stays beside the label knowledge, not inside `Badge`.
export const RISK_TIER_TONES: Readonly<Record<string, BadgeTone>> = {
  low: "success",
  medium: "warning",
  high: "danger",
};

export const APPROVAL_STATUS_LABELS: Readonly<Record<string, string>> = {
  PENDING: "승인 대기",
  AUTO_APPROVED: "자동 승인",
  APPROVED: "승인됨",
  REJECTED: "반려됨",
  SUPERSEDED: "새 빌드로 대체됨",
};

export const APPROVAL_STATUS_TONES: Readonly<Record<string, BadgeTone>> = {
  PENDING: "neutral",
  AUTO_APPROVED: "success",
  APPROVED: "success",
  REJECTED: "danger",
  SUPERSEDED: "neutral",
};

/** An own-property lookup: `"constructor"` or `"__proto__"` sent as a value must not resolve to
 * something inherited from `Object.prototype`. */
function own<T>(table: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

export function riskTierLabel(tier: string): string {
  return own(RISK_TIER_LABELS, tier) ?? tier;
}

export function riskTierTone(tier: string): BadgeTone {
  return own(RISK_TIER_TONES, tier) ?? "neutral";
}

export function approvalStatusLabel(status: string): string {
  return own(APPROVAL_STATUS_LABELS, status) ?? status;
}

export function approvalStatusTone(status: string): BadgeTone {
  return own(APPROVAL_STATUS_TONES, status) ?? "neutral";
}

/** What a recorded decision is called in the decision history: a display label only, the
 * gate on `approve` is `auth/approvalGate.ts`'s. Keyed by that module's constants, so there is one
 * spelling of each wire value. */
export const DECISION_LABELS: Readonly<Record<string, string>> = {
  [APPROVE_DECISION]: "승인",
  [REJECT_DECISION]: "반려",
};

export function decisionLabel(decision: string): string {
  return own(DECISION_LABELS, decision) ?? decision;
}
