/**
 * The cost dashboard's display logic,
 * kept out of JSX so a test reaches it (ADR 0021: `environment: "node"`).
 *
 * What a correct-looking screen could get wrong, and so lives here as a function:
 *
 * 1. **Unmeasured is never `$0.00`.** A day whose every build went unmeasured sums to
 *    `0.0`; a day with no builds also sums to `0.0`. They are different facts and each
 *    gets its own text (`spendText`).
 * 2. **Failed-build spend is a visible part of each day**, not a separate guess: the
 *    bar is split into the failed part and the rest, and the two always add up to the
 *    day's spend.
 * 3. **The chart is a view of numbers that exist as text.** Every figure the bars are
 *    drawn from is also produced by `dailyRows`, which the table renders.
 * 4. **The metering caveat is a static note**, decided here and never an alert.
 */
import { buildFailureReasonLabel } from "../../components/buildFailure";
import { visibleControls } from "../../components/ui/visibleControls";
import type {
  CostDashboard,
  DailyBucket,
  ExpensiveBuild,
  FailureReasonBreakdown,
  MeteringCaveat,
  OutcomeBreakdown,
} from "../../types/api";
import { QUOTA_WORDING, formatUsd, spendDisplay } from "./usageDisplay";

/** How many most-expensive builds the dashboard asks for. The backend's own default
 * is the same number; stated here so the request does not depend on it. */
export const EXPENSIVE_BUILDS_SHOWN = 10;

export const DASHBOARD_WORDING = {
  title: "비용 대시보드",
  dailyTitle: "일별 지출",
  outcomesTitle: "결과별 지출",
  expensiveTitle: "비용이 큰 빌드",
  noBuildsDay: "빌드 없음",
  noBuildsMonth: "이 달에 종료된 빌드가 없습니다.",
  failedLegend: "실패한 빌드의 지출",
  otherLegend: "그 외 빌드의 지출",
  unmeasuredMarker: "미측정 빌드가 있는 날",
  tableToggle: "일별 수치 표로 보기",
  noReason: "사유 기록 없음",
  noRequester: "미귀속",
  unmeasuredNotListed:
    "비용이 기록되지 않은(미측정) 빌드는 이 목록에 나오지 않습니다. 건수는 위의 일별·결과별 수치에 있습니다.",
  noEnforcement: "이 화면은 지출을 보여줄 뿐, 빌드나 배포를 막거나 되돌리지 않습니다.",
} as const;

// --- text for a spend figure ---------------------------------------------------

/**
 * One spend figure as text. `spendDisplay` is the classification the usage panel
 * already uses, so the two screens cannot disagree about when a number may be shown.
 * `unmeasured` and `no-builds` carry no amount at all.
 */
export function spendText(figure: {
  spend_usd: number;
  build_count: number;
  unmeasured_build_count: number;
}): string {
  const display = spendDisplay(figure);
  switch (display.kind) {
    case "unmeasured":
      return `${QUOTA_WORDING.unmeasured} · 빌드 ${display.unmeasuredCount}건`;
    case "partially-unmeasured":
      return `${formatUsd(figure.spend_usd)} (${QUOTA_WORDING.unmeasured} ${display.unmeasuredCount}건 제외)`;
    case "no-builds":
      return DASHBOARD_WORDING.noBuildsDay;
    case "measured":
      return formatUsd(figure.spend_usd);
  }
}

// --- the daily table (and the chart's accessible description) -------------------

export interface DailyRow {
  day: string; // "YYYY-MM-DD"
  /** "7/5" -- the label under a bar. */
  shortLabel: string;
  spend: string;
  /** The failed part, or `null` when there was none to report. */
  failedSpend: string | null;
  builds: number;
  unmeasured: number;
}

/** `"2026-07-05"` -> `"7/5"`. Pure string work: a calendar day carries no timezone, so
 * it must never be turned into a `Date` (which would reinterpret it in the browser's). */
export function shortDayLabel(day: string): string {
  const [, month, date] = day.split("-");
  return `${Number(month)}/${Number(date)}`;
}

export function dailyRows(days: DailyBucket[]): DailyRow[] {
  return days.map((d) => ({
    day: d.day,
    shortLabel: shortDayLabel(d.day),
    spend: spendText(d),
    failedSpend: d.failed_spend_usd > 0 ? formatUsd(d.failed_spend_usd) : null,
    builds: d.build_count,
    unmeasured: d.unmeasured_build_count,
  }));
}

/** The one-line summary of the whole chart, for its accessible description. The
 * per-day figures are in the table, which this points to. */
export function chartSummary(days: DailyBucket[]): string {
  const total = days.reduce((sum, d) => sum + d.spend_usd, 0);
  const failed = days.reduce((sum, d) => sum + d.failed_spend_usd, 0);
  const builds = days.reduce((sum, d) => sum + d.build_count, 0);
  if (builds === 0) return DASHBOARD_WORDING.noBuildsMonth;
  const peak = days.reduce((best, d) => (d.spend_usd > best.spend_usd ? d : best), days[0]);
  return (
    `${days.length}일 동안 합계 ${formatUsd(total)}, 그중 실패한 빌드 ${formatUsd(failed)}. ` +
    `가장 많이 쓴 날은 ${shortDayLabel(peak.day)} ${formatUsd(peak.spend_usd)}. ` +
    "날짜별 수치는 아래 표에 있습니다."
  );
}

// --- the chart's geometry ------------------------------------------------------

export const CHART = {
  width: 640,
  height: 220,
  left: 52,
  right: 8,
  top: 10,
  bottom: 28,
  /** Fraction of a day's slot the bar fills; the rest is the gap between bars. */
  barFill: 0.7,
} as const;

export interface ChartBar {
  day: string;
  label: string;
  /** Whether to print `label` under this bar (every 5th day, so 31 labels do not collide). */
  showLabel: boolean;
  x: number;
  width: number;
  /** The failed-build part, at the bottom of the bar. */
  failedY: number;
  failedHeight: number;
  /** Everything else, stacked on top of it. */
  otherY: number;
  otherHeight: number;
  /** True when the day holds unmeasured builds (drawn as a marker on the axis). */
  hasUnmeasured: boolean;
}

export interface ChartTick {
  y: number;
  label: string;
}

export interface ChartGeometry {
  bars: ChartBar[];
  ticks: ChartTick[];
  /** The value at the top of the plot area. */
  maxUsd: number;
  baselineY: number;
}

/** The smallest of 1, 2, 5 x 10^k that is >= `value` (and >= 1 when there is nothing to
 * scale), so the axis tops out on a round number. */
export function niceCeiling(value: number): number {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  for (const step of [1, 2, 5, 10]) {
    if (step * base >= value) return step * base;
  }
  return 10 * base;
}

/** Round to 2 decimals so the geometry is stable text, not float noise. */
function px(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * Buckets -> bar rectangles. `failedHeight + otherHeight` is the day's whole spend at
 * the axis scale and the failed part is clamped into it, so a day cannot draw more
 * failed spend than it spent. Heights are proportional to spend and nothing else: a
 * day with no spend (none, or only unmeasured) has height 0, and its marker, not a
 * bar, says "unmeasured".
 */
export function chartGeometry(days: DailyBucket[]): ChartGeometry {
  const plotWidth = CHART.width - CHART.left - CHART.right;
  const plotHeight = CHART.height - CHART.top - CHART.bottom;
  const baselineY = CHART.top + plotHeight;
  const maxUsd = niceCeiling(days.reduce((max, d) => Math.max(max, d.spend_usd), 0));
  const slot = days.length > 0 ? plotWidth / days.length : plotWidth;
  const scale = plotHeight / maxUsd;

  const bars = days.map((d, index) => {
    const total = Math.max(d.spend_usd, 0);
    const failed = Math.min(Math.max(d.failed_spend_usd, 0), total);
    const failedHeight = failed * scale;
    const otherHeight = (total - failed) * scale;
    return {
      day: d.day,
      label: shortDayLabel(d.day),
      showLabel: index % 5 === 0,
      x: px(CHART.left + index * slot + (slot * (1 - CHART.barFill)) / 2),
      width: px(slot * CHART.barFill),
      failedY: px(baselineY - failedHeight),
      failedHeight: px(failedHeight),
      otherY: px(baselineY - failedHeight - otherHeight),
      otherHeight: px(otherHeight),
      hasUnmeasured: d.unmeasured_build_count > 0,
    };
  });

  const ticks = [0, 0.5, 1].map((fraction) => ({
    y: px(baselineY - fraction * plotHeight),
    label: formatUsd(maxUsd * fraction),
  }));

  return { bars, ticks, maxUsd, baselineY: px(baselineY) };
}

// --- the outcome breakdown ------------------------------------------------------

/** `StatusBadge`'s Korean wording for the two terminal statuses; any other status the
 * backend might one day report is shown as its code rather than hidden or guessed at. */
const STATUS_LABELS: Record<string, string> = { SUCCEEDED: "성공", FAILED: "실패" };

export function outcomeStatusLabel(status: string): string {
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? STATUS_LABELS[status] : status;
}

/** A failure reason in Korean, the same words the build timeline uses
 * (`buildFailure.ts`) -- never the raw code. `null` is a FAILED build the platform
 * recorded no reason for. */
export function failureReasonText(reason: string | null): string {
  return reason === null ? DASHBOARD_WORDING.noReason : buildFailureReasonLabel(reason);
}

export interface OutcomeRow {
  key: string;
  label: string;
  spend: string;
  builds: number;
  /** One indented line per failure reason, FAILED only. */
  reasons: { key: string; label: string; spend: string; builds: number }[];
}

export function outcomeRows(outcomes: OutcomeBreakdown[]): OutcomeRow[] {
  return outcomes.map((o) => ({
    key: o.status,
    label: outcomeStatusLabel(o.status),
    spend: spendText(o),
    builds: o.build_count,
    reasons: o.failure_reasons.map((r: FailureReasonBreakdown) => ({
      key: r.failure_reason ?? "none",
      label: failureReasonText(r.failure_reason),
      spend: spendText(r),
      builds: r.build_count,
    })),
  }));
}

// --- the most expensive builds -------------------------------------------------

export interface ExpensiveRow {
  buildId: string;
  projectId: string;
  /** Plain text, with invisible / direction-changing characters made visible
   *. Falls back to the id. */
  projectName: string;
  /** Never `$0.00` for a missing figure: a non-number reads as unmeasured. */
  cost: string;
  tokens: string;
  status: string;
  reason: string | null;
  endedAt: string;
  requester: string;
}

/** The instant in the platform timezone the figures were computed in, so the date on a
 * row is the same day the daily chart put it on. */
export function formatEndedAt(iso: string, timezone: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function expensiveRows(
  builds: ExpensiveBuild[],
  projectNames: Record<string, string>,
  timezone: string,
  userNames: Record<string, string> = {},
): ExpensiveRow[] {
  return builds.map((b) => {
    const name = Object.prototype.hasOwnProperty.call(projectNames, b.project_id)
      ? projectNames[b.project_id]
      : b.project_id;
    return {
      buildId: b.build_id,
      projectId: b.project_id,
      projectName: visibleControls(name),
      cost: typeof b.cost_usd === "number" ? formatUsd(b.cost_usd) : QUOTA_WORDING.unmeasured,
      tokens: b.cost_tokens === null ? "—" : b.cost_tokens.toLocaleString("en-US"),
      status: outcomeStatusLabel(b.status),
      reason: b.status === "FAILED" ? failureReasonText(b.failure_reason) : null,
      endedAt: `${formatEndedAt(b.ended_at, timezone)} (${timezone})`,
      requester:
        b.requester_id === null
          ? DASHBOARD_WORDING.noRequester
          : visibleControls(
              Object.prototype.hasOwnProperty.call(userNames, b.requester_id)
                ? userNames[b.requester_id]
                : b.requester_id,
            ),
    };
  });
}

// --- the metering caveat -------------------------------------------------------

/**
 * The static note, or `null` when the window starts on or after the caveat date.
 * Decided from the response's own flag, never from a date the frontend knows: the date
 * is a fact about the backend's history and arrives with the response.
 */
export function meteringCaveatNote(caveat: MeteringCaveat): string | null {
  if (!caveat.window_starts_before) return null;
  return `${caveat.date} 이전에 종료된 빌드의 비용은 에이전트 엔진이 직접 보고한 값일 수 있어, 그 이전 수치는 참고용입니다.`;
}

/** Whether the dashboard has anything to chart at all (a month with no builds draws
 * the empty-state sentence instead of 31 empty slots). */
export function hasBuilds(dashboard: Pick<CostDashboard, "days">): boolean {
  return dashboard.days.some((d) => d.build_count > 0);
}
