// how what the platform MEASURED about a build is worded.
// Pure functions, so the contract is testable
// without mounting anything (ADR 0021), in the shape of `buildFailure.ts`.
//
// The one rule that matters here: a file path, a finding's path and a finding's values
// are chosen by the agent. They are DATA. Every function below returns them as the
// strings they are — never altered, never interpolated into a platform sentence, never
// turned into markup or a link. A caller renders them as React text in a code style.
// Platform sentences (counts, labels, "외 N개") are built only from numbers and fixed
// words.

import type {
  ChangedCounts,
  ChangedFile,
  ChangedFiles,
  SmokeChecks,
  SmokeOutcome,
  SmokeRoute,
} from "../types/api";
import { visibleControls } from "./ui/visibleControls";

/** The approval detail lists this many changed files per kind, then a "외 N개" line. */
export const MAX_FILES_SHOWN = 50;
/** ... and this many findings per check. */
export const MAX_FINDINGS_SHOWN = 20;
/** A finding's value (a URL, a matched pattern) is cut at this many characters. */
export const MAX_FINDING_VALUE_LENGTH = 300;

const CHANGE_LABELS: Record<string, string> = {
  added: "추가",
  modified: "수정",
  deleted: "삭제",
};

/** The Korean word for a change kind; a kind this client does not know is shown as sent. */
export function changeKindLabel(change: string): string {
  return Object.prototype.hasOwnProperty.call(CHANGE_LABELS, change) ? CHANGE_LABELS[change] : change;
}

function count(totals: Record<string, number>, kind: string): number {
  const n = totals[kind];
  return typeof n === "number" && Number.isFinite(n) ? n : 0;
}

/**
 * `변경 파일 12개 (추가 3 · 수정 8 · 삭제 1)`, or null when the build was not measured
 * (null is not zero: nothing is shown rather than an invented "변경 파일 0개").
 * With `truncated` the stored list is partial, which the line says.
 */
export function changedCountsLine(counts: ChangedCounts | null | undefined): string | null {
  if (!counts || typeof counts.totals !== "object" || counts.totals === null) return null;
  const added = count(counts.totals, "added");
  const modified = count(counts.totals, "modified");
  const deleted = count(counts.totals, "deleted");
  const line = `변경 파일 ${added + modified + deleted}개 (추가 ${added} · 수정 ${modified} · 삭제 ${deleted})`;
  return counts.truncated === true ? `${line} · 목록은 일부만 저장됨` : line;
}

/** The sentence for a build whose changed files were not measured (null is not zero). */
export const CHANGED_NOT_MEASURED_LINE = "이 빌드는 변경 파일이 측정되지 않았습니다.";

/**
 * How many files the build changed, from the stored TOTALS (the true number: the stored path list is
 * capped, so its length can be smaller). The same sum the counts line prints. Null when not measured.
 */
export function changedTotal(counts: ChangedCounts | null | undefined): number | null {
  if (!counts || typeof counts.totals !== "object" || counts.totals === null) return null;
  return count(counts.totals, "added") + count(counts.totals, "modified") + count(counts.totals, "deleted");
}

/** Reads the counts out of a timeline entry's untyped `data`; null for anything else. */
export function asChangedCounts(value: unknown): ChangedCounts | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as { totals?: unknown; truncated?: unknown };
  if (typeof v.totals !== "object" || v.totals === null) return null;
  return { totals: v.totals as Record<string, number>, truncated: v.truncated === true };
}

/** Reads a list of strings out of untyped data; null when it is not one (not measured). */
export function asStringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((v): v is string => typeof v === "string");
}

export interface DriftView {
  /** `보호 경로 원복`: platform words. */
  label: string;
  /** The declared protected entries the agent changed (then restored), shown literally. */
  paths: string[];
}

/** The drift line's parts, or null when there is none (null and an empty list alike). */
export function driftView(drift: string[] | null | undefined): DriftView | null {
  if (!drift || drift.length === 0) return null;
  return { label: "보호 경로 원복", paths: [...drift] };
}

/** The kinds the platform itself names, in the order the approval detail lists them. */
const KNOWN_CHANGE_KINDS = ["added", "modified", "deleted"];

export interface ChangedFileGroup {
  /** The `change` value as stored (an unknown one is kept, never dropped). */
  kind: string;
  /** `추가` / `수정` / `삭제`; an unknown kind is shown as sent. */
  label: string;
  /** The group's TRUE count (from the totals), not the number of stored entries. */
  count: number;
  /** `600개`: platform words around the number, shown beside the label. */
  countText: string;
  /** At most `limit` stored paths of this kind, exactly as measured, in stored order. */
  paths: string[];
  /** `외 N개` (N = the true count minus the paths listed) when some are not listed; null otherwise. */
  hiddenLine: string | null;
}

export interface ChangedFilesView {
  /** The counts line, always shown (never inside a disclosure). */
  countsLine: string;
  /** One group per kind that has files: 추가, 수정, 삭제, then any other kind in a stable order. */
  groups: ChangedFileGroup[];
}

/**
 * The approval detail's changed-files block: the counts line
 * and one group per kind present, each with its TRUE count and at most `limit` of its stored paths
 * and an "외 N개" line for the rest (the backend stores at most 500 entries, so a kind can hold
 * fewer paths than its count). A kind the client does not know gets its own group, after the
 * three it knows, in plain code-point order. Null for a build that was not measured.
 */
export function changedFilesView(
  changed: ChangedFiles | null | undefined,
  limit: number = MAX_FILES_SHOWN,
): ChangedFilesView | null {
  const countsLine = changedCountsLine(changed);
  if (!changed || countsLine === null) return null;
  const entries: ChangedFile[] = Array.isArray(changed.entries) ? changed.entries : [];
  const stored = new Map<string, string[]>();
  for (const entry of entries) {
    const kind = String(entry.change);
    const paths = stored.get(kind);
    if (paths) paths.push(entry.path);
    else stored.set(kind, [entry.path]);
  }
  const unknownKinds = new Set<string>(stored.keys());
  for (const kind of Object.keys(changed.totals)) {
    if (count(changed.totals, kind) > 0) unknownKinds.add(kind);
  }
  for (const kind of KNOWN_CHANGE_KINDS) unknownKinds.delete(kind);
  const kinds = [...KNOWN_CHANGE_KINDS, ...[...unknownKinds].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))];
  const groups: ChangedFileGroup[] = [];
  for (const kind of kinds) {
    const storedPaths = stored.get(kind) ?? [];
    const total = Math.max(count(changed.totals, kind), storedPaths.length);
    if (total === 0) continue;
    const paths = storedPaths.slice(0, limit);
    const hidden = total - paths.length;
    groups.push({
      kind,
      label: changeKindLabel(kind),
      count: total,
      countText: `${total}개`,
      paths,
      hiddenLine: hidden > 0 ? `외 ${hidden}개` : null,
    });
  }
  return { countsLine, groups };
}

export interface FindingField {
  key: string;
  value: string;
}

export interface FindingView {
  /** The finding's path, exactly as stored; null when the finding has none. */
  path: string | null;
  /** Every other field, as text. A value is cut at `MAX_FINDING_VALUE_LENGTH`. */
  fields: FindingField[];
}

export interface FindingsView {
  items: FindingView[];
  /** `외 N개` when a check has more findings than are shown; null otherwise. */
  hiddenLine: string | null;
}

function asText(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  return text.length > MAX_FINDING_VALUE_LENGTH ? `${text.slice(0, MAX_FINDING_VALUE_LENGTH)}…` : text;
}

/**
 * One check's findings: the first `limit` of them and a "외 N개" line. A finding's `path` is
 * kept apart and untouched (it is a file name the agent chose); its other fields (type,
 * pattern, match, detail, ...) are text. Null findings (a check with none) give no items.
 */
export function findingsView(
  findings: Record<string, unknown>[] | null | undefined,
  limit: number = MAX_FINDINGS_SHOWN,
): FindingsView {
  const all = Array.isArray(findings) ? findings : [];
  const items = all.slice(0, limit).map((finding): FindingView => {
    const { path, ...rest } = finding ?? {};
    const pathIsText = typeof path === "string";
    // A `path` that is not text is not dropped: it is shown with the other fields.
    const others = pathIsText || path === undefined ? rest : { path, ...rest };
    return {
      path: pathIsText ? path : null,
      fields: Object.entries(others).map(([key, value]) => ({ key, value: asText(value) })),
    };
  });
  const hidden = all.length - items.length;
  return { items, hiddenLine: hidden > 0 ? `외 ${hidden}개` : null };
}

/** The label every surface puts on the agent's own text. */
export const AGENT_SUMMARY_LABEL = "에이전트 작성 · 플랫폼이 확인하지 않은 내용";

export interface AgentSummaryView {
  label: string;
  /** The agent's text as sent, with invisible / direction-changing characters made visible; line breaks kept. */
  text: string;
}

/**
 * The agent's summary as a surface shows it: the fixed label plus the text, plain,
 * never markup and never a link.
 * Null for no text (null, not a string, or only whitespace): nothing is shown for it.
 */
export function agentSummaryView(value: unknown): AgentSummaryView | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  return { label: AGENT_SUMMARY_LABEL, text: visibleControls(value) };
}

/**
 * A requester's text as a surface shows it to someone else (any string shown to someone
 * other than its author): invisible and
 * direction-changing characters made visible, line breaks kept, nothing else touched.
 */
export function requestTextLine(value: unknown): string {
  return visibleControls(typeof value === "string" ? value : String(value));
}

// The preview smoke check: the platform called the
// preview's own parameterless GET routes. The wording of that measured fact, in three states
// that are never conflated: not measured (null), measured with nothing to call, measured with
// routes. A route's path is a string the platform filtered but this module does not trust: it is
// returned as it came and a caller renders it as text.

/** One Korean label per outcome. The `Record` type makes a new backend outcome a compile error. */
export const SMOKE_OUTCOME_LABELS: Record<SmokeOutcome, string> = {
  ok: "응답함",
  client_error: "클라이언트 오류 (4xx)",
  server_error: "서버 오류 (5xx)",
  no_answer: "응답하지 못함",
  too_large: "응답이 너무 큼",
};

/** `status` for a route that never answered. */
export const SMOKE_NO_ANSWER_STATUS = "응답 없음";

/** Not measured. Deliberately has no number and no word that reads as a pass. */
export const SMOKE_NOT_MEASURED_LINE = "측정되지 않음: 점검 결과가 없습니다. 이는 확인되었다는 뜻이 아닙니다.";

/** Measured, and the service exposes no route the check can call. */
export const SMOKE_NO_ROUTES_LINE = "호출할 수 있는 GET 경로가 없음";

/** The note for a list the check cut short. */
export const SMOKE_PARTIAL_NOTE = "일부만 점검됨";

/**
 * The one definition of a route that failed. A 5xx is a failure whatever the outcome says: the
 * backend gives `too_large` precedence over the status class, so a 500 with an oversized body
 * arrives as `too_large` with status 500. A 4xx is the route's own answer (a missing
 * parameter, a login wall) and a 3xx is a redirect that is not followed: neither is a failure.
 */
export function isSmokeRouteFailure(route: { status: number | null; outcome: string }): boolean {
  if (route.outcome === "server_error" || route.outcome === "no_answer") return true;
  return typeof route.status === "number" && route.status >= 500;
}

export interface SmokeRouteView {
  /** Exactly the stored path; a caller renders it as text, never as a link. */
  path: string;
  /** The status number as text, or `응답 없음`. */
  statusText: string;
  /** The Korean outcome label; an outcome this client does not know is shown as sent. */
  outcomeLabel: string;
  failed: boolean;
}

export interface SmokeChecksView {
  /** False only for NULL: the check never ran, or its result was not stored. */
  measured: boolean;
  /** The first line: the whole of the state for not measured and for no routes. */
  summary: string;
  /** `건너뛴 경로 K개` and/or `일부만 점검됨`; empty when neither applies. */
  notes: string[];
  routes: SmokeRouteView[];
}

export interface SmokeCounts {
  called: number;
  failed: number;
}

/** How many routes were called and how many of those failed. */
export function smokeCounts(routes: SmokeRoute[]): SmokeCounts {
  return { called: routes.length, failed: routes.filter(isSmokeRouteFailure).length };
}

function smokeOutcomeLabel(outcome: string): string {
  return Object.prototype.hasOwnProperty.call(SMOKE_OUTCOME_LABELS, outcome)
    ? SMOKE_OUTCOME_LABELS[outcome as SmokeOutcome]
    : outcome;
}

/**
 * One route as every surface that lists measured routes words it (the smoke check, and the routes a
 * repair attempt was started for): the path exactly as stored, the status number or `응답 없음`,
 * the Korean outcome label (an outcome this client does not know is shown as sent).
 */
export function smokeRouteView(route: { path: string; status: number | null; outcome: string }): SmokeRouteView {
  return {
    path: route.path,
    statusText: typeof route.status === "number" ? String(route.status) : SMOKE_NO_ANSWER_STATUS,
    outcomeLabel: smokeOutcomeLabel(route.outcome),
    failed: isSmokeRouteFailure(route),
  };
}

/**
 * The smoke check as the approval detail words it. NULL (and anything that is not the shape)
 * is NOT MEASURED, never an invented "0 routes" or "0 failed": that is a different fact.
 */
export function smokeChecksView(smoke: SmokeChecks | null | undefined): SmokeChecksView {
  if (!smoke || !Array.isArray(smoke.routes)) {
    return { measured: false, summary: SMOKE_NOT_MEASURED_LINE, notes: [], routes: [] };
  }
  const notes: string[] = [];
  if (typeof smoke.skipped === "number" && Number.isFinite(smoke.skipped) && smoke.skipped > 0) {
    notes.push(`건너뛴 경로 ${smoke.skipped}개`);
  }
  if (smoke.truncated === true) notes.push(SMOKE_PARTIAL_NOTE);
  if (smoke.routes.length === 0) {
    return { measured: true, summary: SMOKE_NO_ROUTES_LINE, notes, routes: [] };
  }
  const { called, failed } = smokeCounts(smoke.routes);
  return {
    measured: true,
    summary: failed > 0 ? `호출한 GET 경로 ${called}개 중 ${failed}개 실패` : `호출한 GET 경로 ${called}개 · 실패 없음`,
    notes,
    routes: smoke.routes.map(smokeRouteView),
  };
}
