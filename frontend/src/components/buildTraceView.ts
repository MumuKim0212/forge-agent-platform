// how one build's trace is worded and
// shaped for display. Pure functions, so the contract is testable without mounting anything
// (ADR 0021), in the shape of `measuredFactsLines.ts`.
//
// Two rules run through every function here.
//
// 1. Every string the agent (or the sandbox on its behalf) chose -- a tool name, a tool
//    input, a tool result, assistant text, a file path, a diff line, a log line, a model
//    name, a stop reason -- leaves this file through `visibleControls`: invisible and
//    direction-changing characters become visible `⟨U+XXXX⟩` markers, never interpreted.
//    It is DATA: never interpolated into a platform sentence, never markup, never a link.
//    A platform sentence below is built only from numbers and fixed words.
// 2. Wording keeps the trust split. A tool call is "요청함" (the model asked; the proxy never
//    sees whether it ran) and a tool result is "보고됨" (the sandbox said so). Never
//    "실행됨" or "반환됨".

import type {
  BuildTrace,
  TraceBlockClaim,
  TraceDiffFile,
  TraceStepClaims,
  TraceStepMeasured,
} from "../types/api";
import { changeKindLabel } from "./measuredFactsLines";
import { visibleControls } from "./ui/visibleControls";

/** The label every surface puts on agent-written text; the same wording as the summary's. */
export { AGENT_SUMMARY_LABEL as TRACE_CLAIMS_LABEL } from "./measuredFactsLines";

export const TRACE_MEASURED_HEADING = "플랫폼이 측정한 사실";
export const TRACE_OPEN_LABEL = "트레이스 보기";
export const TRACE_CLOSE_LABEL = "트레이스 닫기";

// ---- the entry point on a timeline build ------------------------------------------------

export type TraceEntryView =
  | { kind: "hidden" }
  | { kind: "open_button"; label: string }
  | { kind: "none"; text: string };

const BUILD_STATUSES_THAT_REACHED_AN_END = ["SUCCEEDED", "FAILED"];

/**
 * What a timeline build entry offers about its trace. Only a viewer the named predicate
 * (`canReadBuildTrace`) lets in sees anything. For them: a button when a trace exists, an
 * explicit "no trace" note for a build that has ended without one (an older build, or one
 * that never reached the agent), and nothing for a build still queued or running.
 */
export function traceEntryView(hasTrace: unknown, canRead: boolean, status: unknown): TraceEntryView {
  if (!canRead) return { kind: "hidden" };
  if (hasTrace === true) return { kind: "open_button", label: TRACE_OPEN_LABEL };
  if (typeof status === "string" && BUILD_STATUSES_THAT_REACHED_AN_END.includes(status)) {
    return { kind: "none", text: "이 빌드의 트레이스는 저장되어 있지 않습니다." };
  }
  return { kind: "hidden" };
}

// ---- a load that did not give a trace ---------------------------------------------------

export interface TraceLoadFailure {
  /** `refused`: the backend said 403. `none`: it said 404 (no trace stored). `error`: anything else. */
  kind: "refused" | "none" | "error";
  message: string;
}

/** The explained state for a failed trace read. The backend is the enforcement point, so a 403 is a state, not a crash. */
export function traceLoadFailure(status: number | null): TraceLoadFailure {
  if (status === 403) {
    return {
      kind: "refused",
      message: "이 빌드의 트레이스는 프로젝트 소유자와 승인자 이상만 볼 수 있습니다.",
    };
  }
  if (status === 404) {
    return {
      kind: "none",
      message:
        "이 빌드에는 저장된 트레이스가 없습니다. 트레이스 기능 이전의 빌드이거나 에이전트 실행 전에 끝난 빌드입니다.",
    };
  }
  return { kind: "error", message: "트레이스를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요." };
}

// ---- measured numbers -------------------------------------------------------------------

/** `$0.0123`: at least four and at most six decimals, so a sub-cent call is not shown as zero. */
export function formatCostUsd(usd: number | null | undefined): string {
  if (typeof usd !== "number" || !Number.isFinite(usd)) return "측정 안 됨";
  let text = usd.toFixed(6);
  while (text.length - text.indexOf(".") - 1 > 4 && text.endsWith("0")) text = text.slice(0, -1);
  return `$${text}`;
}

function formatCount(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? String(n) : "측정 안 됨";
}

/** `1.2초`, or null when either end of the call is missing or unreadable. */
export function durationLabel(startedAt: string | null, endedAt: string | null): string | null {
  if (!startedAt || !endedAt) return null;
  const ms = Date.parse(endedAt) - Date.parse(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return null;
  return `${(ms / 1000).toFixed(1)}초`;
}

const CONTENT_STATE_NOTES: Record<string, string> = {
  unparsed: "내용을 해석하지 못해 측정값만 기록됨",
  dropped: "기록 한도를 넘어 내용이 저장되지 않음 (측정값만 기록됨)",
};

export interface TraceStepRow {
  ordinal: number;
  /** The model name is provider-supplied text; shown as data. */
  model: string;
  startedAt: string | null;
  durationLabel: string | null;
  tokensLine: string;
  costLabel: string;
  stopReason: string;
  /** Why this call has no content in the claims section, or null when it does. */
  contentNote: string | null;
  /** Length of each thinking block; their text is never stored. */
  thinkingLine: string | null;
}

function stepRow(step: TraceStepMeasured): TraceStepRow {
  const parts = [`입력 ${formatCount(step.input_tokens)}`, `출력 ${formatCount(step.output_tokens)}`];
  if (step.cache_read_tokens) parts.push(`캐시 읽기 ${step.cache_read_tokens}`);
  if (step.cache_write_tokens) parts.push(`캐시 쓰기 ${step.cache_write_tokens}`);
  const thinking = Array.isArray(step.thinking) ? step.thinking : [];
  return {
    ordinal: step.ordinal,
    model: step.model ? visibleControls(step.model) : "알 수 없음",
    startedAt: step.started_at,
    durationLabel: durationLabel(step.started_at, step.ended_at),
    tokensLine: parts.join(" · "),
    costLabel: formatCostUsd(step.cost_usd),
    stopReason: step.stop_reason ? visibleControls(step.stop_reason) : "없음",
    contentNote: Object.prototype.hasOwnProperty.call(CONTENT_STATE_NOTES, step.content)
      ? CONTENT_STATE_NOTES[step.content]
      : null,
    thinkingLine:
      thinking.length > 0
        ? `생각 블록 ${thinking.length}개 (길이 ${thinking.map((t) => t.chars).join(", ")}자 · 내용은 저장하지 않음)`
        : null,
  };
}

export interface TraceMeasuredView {
  /** `호출 12회 · 토큰 34,567 · 비용 $0.1234`: all calls, the omitted ones included. */
  totalLine: string;
  /** Calls whose cost or tokens were not measured, so the total is a lower bound; null when none. */
  unmeteredLine: string | null;
  /** Calls folded into totals past the record's ceiling; null when none. */
  omittedLine: string | null;
  /** The call record hit its bound, so later calls keep only measured fields; null otherwise. */
  contentTruncatedLine: string | null;
  /** The proxy's record could not be read back; null otherwise. */
  unavailableLine: string | null;
  steps: TraceStepRow[];
  /** `변경 파일 N개` plus the diff's own state; the file contents are in the claims section. */
  diffLine: string;
}

const DIFF_STATE_LINES: Record<string, string> = {
  computed: "diff 계산됨",
  not_reached: "이 빌드는 게시 전에 끝나 diff가 없습니다",
  unavailable: "diff를 읽을 수 없습니다",
};

/**
 * The section that comes first: call count, per-call model / timing / tokens / cost /
 * `stop_reason`, and the total. The total is the sum of the listed calls plus the ones the
 * proxy folded into `omitted_*`, so it is the same figure as the build's cost (the backend
 * pins that equality).
 */
export function traceMeasuredView(trace: BuildTrace): TraceMeasuredView {
  const calls = trace.measured.calls;
  const steps = Array.isArray(calls.steps) ? calls.steps : [];
  let tokens = calls.omitted_tokens || 0;
  let cost = calls.omitted_cost_usd || 0;
  let unmetered = 0;
  for (const step of steps) {
    if (typeof step.tokens === "number") tokens += step.tokens;
    if (typeof step.cost_usd === "number") cost += step.cost_usd;
    if (typeof step.tokens !== "number" || typeof step.cost_usd !== "number") unmetered += 1;
  }
  const callCount = Math.max(calls.calls_total || 0, steps.length + (calls.omitted_calls || 0));
  const diff = trace.measured.diff;
  const diffState = Object.prototype.hasOwnProperty.call(DIFF_STATE_LINES, diff.state)
    ? DIFF_STATE_LINES[diff.state]
    : "diff 상태를 알 수 없습니다";
  const diffFiles = diff.state === "computed" ? ` · 변경 파일 ${diff.files_total}개` : "";
  return {
    totalLine: `호출 ${callCount}회 · 토큰 ${tokens.toLocaleString("en-US")} · 비용 ${formatCostUsd(cost)}`,
    unmeteredLine: unmetered > 0 ? `${unmetered}개 호출은 토큰이나 비용이 측정되지 않아 합계에 빠졌습니다.` : null,
    omittedLine:
      calls.omitted_calls > 0
        ? `호출 ${calls.omitted_calls}개는 개별 항목 없이 합계에만 포함됨 (토큰 ${calls.omitted_tokens} · 비용 ${formatCostUsd(calls.omitted_cost_usd)})`
        : null,
    contentTruncatedLine: calls.content_truncated
      ? "호출 기록이 한도에 닿아, 이후 호출은 내용 없이 측정값만 남아 있습니다."
      : null,
    unavailableLine:
      calls.state === "captured" ? null : "프록시의 호출 기록을 읽지 못해 호출별 정보가 없습니다.",
    steps: steps.map(stepRow),
    diffLine: `${diffState}${diffFiles}`,
  };
}

// ---- agent claims -----------------------------------------------------------------------

export interface TraceBlockView {
  kind: "assistant_text" | "tool_use" | "tool_result" | "other";
  /** A platform phrase: `도구 호출 요청함`, `도구 결과 보고됨`, ... Never "실행됨" / "반환됨". */
  label: string;
  /** A tool name or a block type: agent-chosen, shown beside the label as data, never inside it. */
  name: string | null;
  /** The block's text with invisible characters made visible; null when it has none. */
  text: string | null;
  /** The sandbox reported this result as an error. */
  reportedError: boolean;
  /** `8,000자까지만 저장됨 (원문 12,345자)`; null when nothing was cut. */
  truncatedNote: string | null;
}

const BLOCK_LABELS: Record<string, string> = {
  assistant_text: "에이전트 텍스트",
  tool_use: "도구 호출 요청함",
  tool_result: "도구 결과 보고됨",
  other: "기타 블록",
};

/** Characters the proxy keeps per content field; the note says how much survives. */
export const TRACE_BLOCK_MAX_CHARS = 8000;

function blockView(block: TraceBlockClaim): TraceBlockView {
  const kind = Object.prototype.hasOwnProperty.call(BLOCK_LABELS, block.kind)
    ? (block.kind as TraceBlockView["kind"])
    : "other";
  const rawName = kind === "other" ? block.type : block.name;
  const truncated = block.truncated === true;
  return {
    kind,
    label: BLOCK_LABELS[kind],
    name: typeof rawName === "string" && rawName !== "" ? visibleControls(rawName) : null,
    text: typeof block.text === "string" ? visibleControls(block.text) : null,
    reportedError: kind === "tool_result" && block.is_error === true,
    truncatedNote: truncated
      ? typeof block.chars === "number"
        ? `${TRACE_BLOCK_MAX_CHARS.toLocaleString("en-US")}자까지만 저장됨 (원문 ${block.chars.toLocaleString("en-US")}자)`
        : `${TRACE_BLOCK_MAX_CHARS.toLocaleString("en-US")}자까지만 저장됨`
      : null,
  };
}

export interface TraceCallClaimsView {
  ordinal: number;
  /** `호출 3 · 요청 2건 · 보고 1건 · 텍스트 1건`: counts only, so the collapsed row is a safe summary. */
  summary: string;
  blocks: TraceBlockView[];
  /** The call has no recorded content (unparsed, dropped, or bounded away). */
  empty: boolean;
}

function callClaims(claims: TraceStepClaims): TraceCallClaimsView {
  const blocks = (Array.isArray(claims.blocks) ? claims.blocks : []).map(blockView);
  const count = (k: TraceBlockView["kind"]) => blocks.filter((b) => b.kind === k).length;
  const parts = [`호출 ${claims.ordinal}`];
  if (count("tool_use") > 0) parts.push(`요청 ${count("tool_use")}건`);
  if (count("tool_result") > 0) parts.push(`보고 ${count("tool_result")}건`);
  if (count("assistant_text") > 0) parts.push(`텍스트 ${count("assistant_text")}건`);
  if (count("other") > 0) parts.push(`기타 ${count("other")}건`);
  if (blocks.length === 0) parts.push("기록된 내용 없음");
  return { ordinal: claims.ordinal, summary: parts.join(" · "), blocks, empty: blocks.length === 0 };
}

export interface TraceDiffFileView {
  /** The path the platform measured, with invisible characters made visible. Shown as data. */
  path: string;
  /** `추가` / `수정` / `삭제`. */
  changeLabel: string;
  /** Why there is no diff text, in platform words; null when there is text. */
  noContentNote: string | null;
  /** The diff text, invisible characters made visible; null when there is none. */
  text: string | null;
  truncatedNote: string | null;
  /** Added / removed line counts, computed from the text; null when there is none. */
  linesLine: string | null;
}

const NO_CONTENT_NOTES: Record<string, string> = {
  binary: "바이너리이거나 UTF-8이 아닌 파일이라 내용 없이 목록만 보여 줍니다.",
  too_large: "파일이 커서 diff 내용을 저장하지 않았습니다.",
  total_limit: "diff 전체 크기 한도를 넘어 이 파일의 내용은 저장하지 않았습니다.",
};

/** Added and removed lines of a unified diff, from the text (the `+++` / `---` file headers excluded). */
export function diffLineCounts(text: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

function diffFileView(file: TraceDiffFile): TraceDiffFileView {
  const text = typeof file.diff === "string" ? visibleControls(file.diff) : null;
  const counts = text !== null ? diffLineCounts(text) : null;
  return {
    path: visibleControls(file.path),
    changeLabel: changeKindLabel(file.change),
    noContentNote:
      text !== null
        ? null
        : Object.prototype.hasOwnProperty.call(NO_CONTENT_NOTES, file.content)
          ? NO_CONTENT_NOTES[file.content]
          : "내용이 없습니다.",
    text,
    truncatedNote: file.truncated ? "파일당 한도에서 잘려 일부만 저장됨" : null,
    linesLine: counts ? `+${counts.added} / -${counts.removed}` : null,
  };
}

export interface TraceDiffView {
  /** The diff's state in platform words (never reached publish, unavailable); null when computed. */
  stateNote: string | null;
  files: TraceDiffFileView[];
  /** The stored list or its content is partial; null otherwise. */
  truncatedLine: string | null;
}

export interface TraceLogView {
  /** The log tail with invisible characters made visible; null when there is none. */
  text: string | null;
  /** `없음` state in platform words when there is no text; null otherwise. */
  emptyNote: string | null;
  truncatedNote: string | null;
}

export interface TraceClaimsView {
  calls: TraceCallClaimsView[];
  diff: TraceDiffView;
  log: TraceLogView;
}

/** The sections under the agent-claim label: requested calls and reported results, the diff, the log. */
export function traceClaimsView(trace: BuildTrace): TraceClaimsView {
  const diff = trace.measured.diff;
  const logText = trace.claims.log?.text;
  return {
    calls: (Array.isArray(trace.claims.calls) ? trace.claims.calls : []).map(callClaims),
    diff: {
      stateNote: diff.state === "computed" ? null : DIFF_STATE_LINES[diff.state] ?? "diff 상태를 알 수 없습니다",
      files: (Array.isArray(diff.files) ? diff.files : []).map(diffFileView),
      truncatedLine: diff.truncated ? "diff가 한도에 닿아 일부 파일은 목록이나 내용이 잘렸습니다." : null,
    },
    log: {
      text: typeof logText === "string" && logText !== "" ? visibleControls(logText) : null,
      emptyNote: typeof logText === "string" && logText !== "" ? null : "저장된 로그가 없습니다.",
      truncatedNote: trace.claims.log?.truncated ? "로그가 한도를 넘어 일부만 저장되었습니다." : null,
    },
  };
}
