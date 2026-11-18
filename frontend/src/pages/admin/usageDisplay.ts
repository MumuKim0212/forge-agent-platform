/**
 * The usage panel's decision logic, kept out of JSX so it is reachable by a test
 * (ADR 0021: `environment: "node"`, nothing can be mounted).
 *
 * Three rules are easy to
 * satisfy by accident and just as easy to break by accident, which is why each is
 * a function here rather than an inline ternary:
 *
 * 1. **Unmeasured is not a measured zero.** `cost_usd` is nullable; sums treat
 *    `NULL` as zero, so a subject whose every build went unmeasured reports
 *    `spend_usd: 0.0`. Printing `$0.00` there is a lie with no visible symptom.
 * 2. **An override must be distinguishable from the inherited default**, or an
 *    operator cannot tell whether clearing the override does anything.
 * 3. **A breach warns.** Nothing is blocked, stopped, or refused — there is no
 *    enforcement to describe.
 */
import type { MonthWindow, SubjectUsage } from "../../types/api";

/** `services/usage.QuotaLimitSource`. Cross-service contract strings: a rename
 * on one side only would silently label every override as an inherited default,
 * which is exactly the distinction rule 2 above exists to preserve. */
export const LimitSource = {
  OVERRIDE: "override",
  DEFAULT: "default",
} as const;

/** `services/usage.UsageSubject`. */
export const UsageSubjectKind = {
  PROJECT: "project",
  USER: "user",
} as const;

/**
 * All user-facing quota/usage copy in one record, for two reasons: the panel
 * reads its wording from one place, and the "no wording that implies
 * enforcement" rule can be asserted over the whole record rather than over
 * whichever strings a test author remembered (quotas warn, nothing blocks).
 */
export const QUOTA_WORDING = {
  breach: "한도 초과 (경고)",
  withinLimit: "한도 내",
  overrideLabel: "개별 지정",
  defaultLabel: "전역 기본값",
  unlabelledLimitNote: "이 화면이 알지 못하는 한도 출처입니다.",
  noLimit: "해당 없음",
  unattributed: "미귀속 지출",
  unattributedExplanation: "프로젝트별 합계에만 포함됩니다.",
  unmeasured: "미측정",
  unmeasuredExplanation:
    "비용이 기록되지 않은 빌드입니다 (에이전트까지 가지 못한 빌드 등). 0 USD를 쓴 것과는 다릅니다.",
  noBuilds: "이번 달 빌드 없음",
} as const;

export function formatUsd(amount: number): string {
  return `$${amount.toFixed(2)}`;
}

/** The month and timezone every figure on the screen was computed for
 * (the response carries both so a number can
 * never be mistaken for a different window). */
export function formatMonthWindow(window: MonthWindow): string {
  return `${window.month} · ${window.timezone} 기준`;
}

/**
 * How a spend figure may be rendered — see rule 1 above. Classification only;
 * the amount is formatted separately, and `unmeasured` deliberately carries no
 * amount so a caller cannot print `$0.00` for it.
 */
export type SpendDisplay =
  | { kind: "measured" }
  | { kind: "partially-unmeasured"; unmeasuredCount: number }
  | { kind: "unmeasured"; unmeasuredCount: number }
  | { kind: "no-builds" };

/**
 * Ordered so that **any** unmeasured build wins over "measured": if part of the
 * window was never measured, no reading of the figure is complete, and claiming
 * a measured zero is the one outcome that must not happen. A count larger than
 * `build_count` (which the API should never send) therefore also lands on
 * `unmeasured` rather than on `measured`.
 */
export function spendDisplay(line: {
  build_count: number;
  unmeasured_build_count: number;
}): SpendDisplay {
  const { build_count: builds, unmeasured_build_count: unmeasured } = line;
  if (unmeasured > 0 && unmeasured >= builds) {
    return { kind: "unmeasured", unmeasuredCount: unmeasured };
  }
  if (unmeasured > 0) {
    return { kind: "partially-unmeasured", unmeasuredCount: unmeasured };
  }
  if (builds === 0) return { kind: "no-builds" };
  return { kind: "measured" };
}

/** Whether the limit on screen is a per-subject override or the inherited global
 * default — rule 2 above. `none` is the unattributed line, which has no subject
 * and therefore no limit to show. */
export type LimitDisplay =
  | { kind: "none" }
  | { kind: "override"; amount: string }
  | { kind: "default"; amount: string }
  | { kind: "unlabelled"; amount: string };

export function limitDisplay(limit: number | null, source: string | null): LimitDisplay {
  if (limit === null) return { kind: "none" };
  const amount = formatUsd(limit);
  if (source === LimitSource.OVERRIDE) return { kind: "override", amount };
  if (source === LimitSource.DEFAULT) return { kind: "default", amount };
  // Not "probably the default": saying which of the two it is *is* the point of
  // this function, so an unrecognised source says so instead of guessing.
  return { kind: "unlabelled", amount };
}

/** The unattributed line is the one whose `subject_id` is null.
 * It is a real reported line and must never be folded into a
 * user or dropped. */
export function isUnattributedLine(line: Pick<SubjectUsage, "subject_id">): boolean {
  return line.subject_id === null;
}

export const QUOTA_INPUT_ERRORS = {
  notANumber: "숫자를 입력하세요. 비우면 개별 지정을 해제합니다.",
  negative: "음수 한도는 사용할 수 없습니다. 0 이상을 입력하세요.",
} as const;

/**
 * What the override field's text means. `clear` sends `null`, which returns the
 * subject to the global default — **not** to unlimited.
 *
 * The backend also rejects a negative value; this exists so the operator gets a
 * reason next to the field rather than a round trip, and so the UI is not the
 * *only* check in either direction.
 */
export type QuotaInputResult =
  | { kind: "clear" }
  | { kind: "set"; value: number }
  | { kind: "invalid"; reason: string };

export function parseQuotaInput(raw: string): QuotaInputResult {
  const trimmed = raw.trim();
  if (trimmed === "") return { kind: "clear" };

  const value = Number(trimmed);
  if (!Number.isFinite(value)) return { kind: "invalid", reason: QUOTA_INPUT_ERRORS.notANumber };
  if (value < 0) return { kind: "invalid", reason: QUOTA_INPUT_ERRORS.negative };

  // `Number("-0")` is `-0`, which would render as "-0" and read as a different
  // value from the `0` the operator typed. `-0 < 0` is false, so it gets here.
  return { kind: "set", value: value === 0 ? 0 : value };
}

/** The value to prefill the override field with. An existing override of `0`
 * must show as "0", not as an empty field — an empty field
 * is what "no override" looks like, and the two must not be confusable. */
export function quotaInputValue(storedOverride: number | null): string {
  return storedOverride === null ? "" : String(storedOverride);
}

/**
 * The accessible names of one row's quota controls. Every row used to carry the same
 * `월 한도(USD)` on its input and the same `적용` on its button, so a screen reader's list of
 * controls (or a voice command) could not tell which subject a control changed. The name is the
 * subject's, then what the control is. The button's name ends in its visible text, so a person who
 * says what they see ("적용") still matches (WCAG 2.5.3); `quotaApplyText` is that visible text, kept
 * here so the two cannot drift apart.
 */
export function quotaInputLabel(subjectName: string): string {
  return `${subjectName} 월 한도(USD)`;
}

export function quotaApplyText(saving: boolean): string {
  return saving ? "저장 중..." : "적용";
}

export function quotaApplyLabel(subjectName: string, saving: boolean): string {
  return `${subjectName} ${quotaApplyText(saving)}`;
}

/**
 * `YYYY-MM` options for the month selector, newest first, including the current
 * month. Computed in UTC so the list is deterministic wherever the browser is;
 * which month is "current" for rollup purposes is the platform timezone's
 * business and is stated by the window on every response, not inferred here.
 */
export function recentMonths(now: Date, count: number): string[] {
  const months: string[] = [];
  for (let back = 0; back < count; back += 1) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - back, 1));
    months.push(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  return months;
}
