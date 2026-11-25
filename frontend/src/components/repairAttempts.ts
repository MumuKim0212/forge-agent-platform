// How the in-build repair is worded on the timeline, in the approval console and in the eval run
// view. One module decides everything about display, so no markup compares a number and a surface
// cannot word the count differently from another. `repairAttempts.test.ts` pins every branch.
//
// The count is a platform measurement (how many repair attempts the build started). It never says
// the repair worked: the smoke check beside it says what the shipped attempt looks like. 0 means
// the first attempt shipped or the loop was off, and gets no note. NULL is "not recorded" (a build
// or run from before the column) and is never read as 0.

import { smokeRouteView, type SmokeRouteView } from "./measuredFactsLines";

/** Reads a count out of an untyped value (a timeline entry's `data`): a non-negative integer, else null. */
export function asRepairAttempts(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** True only for an integer of 1 or more: a repair actually ran. False for 0, null, undefined, a
 * fraction, NaN, a negative, a boolean, a string or anything else. */
export function shouldShowRepairNote(value: unknown): boolean {
  const attempts = asRepairAttempts(value);
  return attempts !== null && attempts >= 1;
}

/** The compact timeline line; null when there is nothing to say. */
export function repairTimelineLine(value: unknown): string | null {
  const attempts = asRepairAttempts(value);
  if (attempts === null || !shouldShowRepairNote(attempts)) return null;
  return `수리 시도 ${attempts}회: 플랫폼의 점검(프리뷰 응답 점검 또는 SQL 문법 점검)에서 문제가 확인되어 에이전트에게 다시 시도하게 했습니다.`;
}

export const REPAIR_NOTE_HEADING = "수리 시도";

/** The approval console's note; null when there is nothing to say. Says what the platform did and
 * which attempt the smoke check below belongs to, and nothing about whether it helped. */
export function repairApprovalNote(value: unknown): string | null {
  const attempts = asRepairAttempts(value);
  if (attempts === null || !shouldShowRepairNote(attempts)) return null;
  return (
    `플랫폼의 점검(프리뷰 응답 점검 또는 SQL 문법 점검)에서 문제가 확인되어 에이전트에게 ${attempts}회 다시 시도하게 했습니다. ` +
    "아래 프리뷰 응답 점검은 최종으로 전달된 시도의 결과입니다."
  );
}

export const NO_REPAIR_SETTING_TEXT = "수리 설정 기록 없음";

/** What an eval run recorded for the repair loop. 0 is worded as off; NULL (or anything that is not
 * a count) is "not recorded", never 0. */
export function repairSettingLabel(maxAttempts: unknown): string {
  const max = asRepairAttempts(maxAttempts);
  if (max === null) return NO_REPAIR_SETTING_TEXT;
  return shouldShowRepairNote(max) ? `수리 시도 최대 ${max}회` : "수리 시도 최대 0회 (수리 사용 안 함)";
}

export const NO_CASE_ATTEMPTS_TEXT = "—";

/** The attempts a case's build ran. A figure only once the case was judged and measured; "—" for
 * NULL, a case not run, a pending case or one from before the column. 0 is shown as 0. */
export function caseAttemptsText(attempts: unknown, measured: boolean): string {
  const count = asRepairAttempts(attempts);
  return measured && count !== null ? `${count}회` : NO_CASE_ATTEMPTS_TEXT;
}

// The routes each repair attempt was started for, read out of the build's untyped `repair_routes`.
// A path is a measured fact that carries agent-chosen characters: it is returned exactly as stored and
// a caller renders it as React text in `Code`, which turns control and direction-changing
// characters into `⟨U+XXXX⟩` markers; it is never interpolated into a sentence here. The wording of a route (status,
// outcome) is the smoke section's (`smokeRouteView`), not a second one. The list says what an EARLIER
// attempt failed on; it never says the repair worked and is not the shipped attempt's smoke check.

/** At most this many repair attempts are listed ... */
export const MAX_REPAIR_ATTEMPTS_SHOWN = 3;
/** ... and this many routes under each. */
export const MAX_REPAIR_ROUTES_SHOWN = 20;

/** One SQL statement PostgreSQL's parser rejected: where, and the platform's sentence in words. */
export interface SqlFindingView {
  path: string;
  line: number;
  text: string;
}

/** At most this many SQL findings are listed under an attempt (the backend stores at most 10). */
export const MAX_SQL_FINDINGS_SHOWN = 10;

export interface RepairAttemptRoutesView {
  /** Platform words plus the number: which repair attempt, and that the routes are from the one before. */
  label: string;
  routes: SmokeRouteView[];
  /** `외 N개` when the attempt has more routes than are shown; null otherwise. */
  hiddenLine: string | null;
  /** Platform words plus the number, for the SQL statements the attempt was started for. */
  sqlLabel: string;
  sqlFindings: SqlFindingView[];
  /** `외 N개` when the attempt has more findings than are shown; null otherwise. */
  sqlHiddenLine: string | null;
}

export interface RepairRoutesView {
  attempts: RepairAttemptRoutesView[];
  /** `외 N회 시도` when more attempts are stored than are shown; null otherwise. */
  hiddenLine: string | null;
}

function isStoredRoute(value: unknown): value is { path: string; status: number | null; outcome: string } {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const route = value as { path?: unknown; status?: unknown; outcome?: unknown };
  return (
    typeof route.path === "string" &&
    (route.status === null || (typeof route.status === "number" && Number.isInteger(route.status))) &&
    typeof route.outcome === "string"
  );
}

function isStoredSql(value: unknown): value is { path: string; line: number; error: string } {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const finding = value as { path?: unknown; line?: unknown; error?: unknown };
  return (
    typeof finding.path === "string" &&
    typeof finding.line === "number" &&
    Number.isInteger(finding.line) &&
    finding.line >= 1 &&
    typeof finding.error === "string"
  );
}

type StoredAttempt = {
  attempt: number;
  routes: { path: string; status: number | null; outcome: string }[];
  sql?: { path: string; line: number; error: string }[];
};

// An entry is a started repair: it has at least one failing route or one rejected SQL statement. `sql`
// is optional (an entry written before it existed, or with no findings, has none); when present it
// must be well formed, like everything else here.
function isStoredAttempt(value: unknown): value is StoredAttempt {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const entry = value as { attempt?: unknown; routes?: unknown; sql?: unknown };
  if (!(typeof entry.attempt === "number" && Number.isInteger(entry.attempt) && entry.attempt >= 1)) return false;
  if (!Array.isArray(entry.routes) || !entry.routes.every(isStoredRoute)) return false;
  if (entry.sql !== undefined && !(Array.isArray(entry.sql) && entry.sql.every(isStoredSql))) return false;
  return entry.routes.length > 0 || (Array.isArray(entry.sql) && entry.sql.length > 0);
}

const SQL_NEAR = /^syntax error at or near "([A-Za-z_][A-Za-z0-9_]{0,31})"$/;

/** The platform's fixed SQL sentence in words. A sentence that is not one of its forms is shown as the
 * plain "문법 오류": nothing stored is interpolated except a bare identifier. */
export function sqlErrorText(error: string): string {
  if (error === "syntax error at end of input") return "문장이 끝나는 곳에서 문법 오류";
  const near = SQL_NEAR.exec(error);
  return near ? `"${near[1]}" 근처에서 문법 오류` : "문법 오류";
}

/**
 * The routes each repair attempt was started for, or null. Null for anything that is not exactly a
 * non-empty list of `{attempt >= 1, routes: non-empty list of {path, status, outcome}}`: a string, an
 * object, `[]`, one malformed entry among valid ones (never a partial list), a non-array `routes`.
 * Null means "not recorded", which a caller words as nothing at all, never as "no failing routes".
 * Bounded to `MAX_REPAIR_ATTEMPTS_SHOWN` attempts and `MAX_REPAIR_ROUTES_SHOWN` routes each, in the
 * stored order, with a count of what was left out. Never throws.
 */
export function repairRoutesView(value: unknown): RepairRoutesView | null {
  try {
    if (!Array.isArray(value) || value.length === 0 || !value.every(isStoredAttempt)) return null;
    const shown = value.slice(0, MAX_REPAIR_ATTEMPTS_SHOWN);
    const hiddenAttempts = value.length - shown.length;
    return {
      attempts: shown.map((entry) => {
        const routes = entry.routes.slice(0, MAX_REPAIR_ROUTES_SHOWN);
        const omitted = entry.routes.length - routes.length;
        const stored = entry.sql ?? [];
        const sql = stored.slice(0, MAX_SQL_FINDINGS_SHOWN);
        const sqlOmitted = stored.length - sql.length;
        return {
          label: `수리 시도 ${entry.attempt}: 이전 시도에서 실패한 경로`,
          routes: routes.map(smokeRouteView),
          hiddenLine: omitted > 0 ? `외 ${omitted}개` : null,
          sqlLabel: `수리 시도 ${entry.attempt}: 이전 시도에서 PostgreSQL 파서가 거부한 SQL 문장`,
          sqlFindings: sql.map((finding) => ({
            path: finding.path,
            line: finding.line,
            text: sqlErrorText(finding.error),
          })),
          sqlHiddenLine: sqlOmitted > 0 ? `외 ${sqlOmitted}개` : null,
        };
      }),
      hiddenLine: hiddenAttempts > 0 ? `외 ${hiddenAttempts}회 시도` : null,
    };
  } catch {
    return null;
  }
}

export interface RepairApprovalView {
  /** The platform sentence (`repairApprovalNote`). */
  note: string;
  /** The routes under it; null when none are recorded, and then the note stands alone. */
  routes: RepairRoutesView | null;
}

/**
 * What the approval console shows for a build's repair: the note, plus the routes only when the note
 * itself shows (a count of 1 or more) and the stored routes are well formed. Null for no note at all,
 * so routes with a count of 0, null or absent are never shown on their own.
 */
export function repairApprovalView(attempts: unknown, routes: unknown): RepairApprovalView | null {
  const note = repairApprovalNote(attempts);
  if (note === null) return null;
  return { note, routes: repairRoutesView(routes) };
}
