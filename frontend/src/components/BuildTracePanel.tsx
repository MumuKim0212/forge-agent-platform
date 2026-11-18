import { useEffect, useState } from "react";

import { buildsApi } from "../api/builds";
import { ApiError } from "../api/client";
import type { BuildTrace } from "../types/api";
import {
  TRACE_CLAIMS_LABEL,
  TRACE_CLOSE_LABEL,
  TRACE_MEASURED_HEADING,
  traceClaimsView,
  traceEntryView,
  traceLoadFailure,
  traceMeasuredView,
  type TraceBlockView,
  type TraceLoadFailure,
} from "./buildTraceView";
import { Alert, Badge, Button, Card, Code, Table } from "./ui";

// one build's trace, opened on
// demand from its timeline entry. Measured facts lead; everything the agent or the sandbox
// wrote follows under the agent-claim label.
//
// Every agent-chosen string below is React text inside a `Code` element (escaped by React,
// never a link, never markup) and has already been through `visibleControls` in
// `buildTraceView.ts`. There is no raw-HTML escape hatch and no markdown. Nothing here is
// announced: a trace is a record of something that already happened, and opening it is the
// viewer's own act.

const SECTION_HEADING = "mt-4 text-sm font-semibold text-slate-700 dark:text-slate-200";
const MUTED = "text-xs text-slate-500 dark:text-slate-400";
const TEXT_BLOCK = "block max-h-96 overflow-auto p-2";

function Block({ block }: { block: TraceBlockView }) {
  return (
    <li className="mt-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
        <Badge tone={block.kind === "tool_use" ? "accent" : "neutral"}>{block.label}</Badge>
        {block.name !== null && <Code>{block.name}</Code>}
        {block.reportedError && <Badge tone="warning">오류로 보고됨</Badge>}
      </div>
      {block.text !== null && <Code className={`${TEXT_BLOCK} mt-1`}>{block.text}</Code>}
      {block.truncatedNote && <p className={`mt-1 ${MUTED}`}>{block.truncatedNote}</p>}
    </li>
  );
}

/** The trace itself, given a loaded record. Exported for the mounted test. */
export function BuildTraceView({ trace }: { trace: BuildTrace }) {
  const measured = traceMeasuredView(trace);
  const claims = traceClaimsView(trace);
  return (
    <div className="text-sm">
      <h4 className={SECTION_HEADING}>{TRACE_MEASURED_HEADING}</h4>
      <p className="mt-2 font-medium text-slate-800 dark:text-slate-100">{measured.totalLine}</p>
      {measured.unavailableLine && <p className={`mt-1 ${MUTED}`}>{measured.unavailableLine}</p>}
      {measured.unmeteredLine && <p className={`mt-1 ${MUTED}`}>{measured.unmeteredLine}</p>}
      {measured.omittedLine && <p className={`mt-1 ${MUTED}`}>{measured.omittedLine}</p>}
      {measured.contentTruncatedLine && <p className={`mt-1 ${MUTED}`}>{measured.contentTruncatedLine}</p>}
      {measured.steps.length > 0 && (
        <Table className="mt-2">
          <Table.Head>
            <Table.Row>
              <Table.HeaderCell>호출</Table.HeaderCell>
              <Table.HeaderCell>모델</Table.HeaderCell>
              <Table.HeaderCell>시간</Table.HeaderCell>
              <Table.HeaderCell>토큰</Table.HeaderCell>
              <Table.HeaderCell>비용</Table.HeaderCell>
              <Table.HeaderCell>종료 사유</Table.HeaderCell>
            </Table.Row>
          </Table.Head>
          <Table.Body>
            {measured.steps.map((step) => (
              <Table.Row key={step.ordinal}>
                <Table.Cell>{step.ordinal}</Table.Cell>
                <Table.Cell>
                  <Code>{step.model}</Code>
                </Table.Cell>
                <Table.Cell>
                  {step.startedAt ? new Date(step.startedAt).toLocaleTimeString() : "측정 안 됨"}
                  {step.durationLabel && <span className={`ml-1 ${MUTED}`}>({step.durationLabel})</span>}
                </Table.Cell>
                <Table.Cell>
                  {step.tokensLine}
                  {step.thinkingLine && <div className={MUTED}>{step.thinkingLine}</div>}
                  {step.contentNote && <div className={MUTED}>{step.contentNote}</div>}
                </Table.Cell>
                <Table.Cell>{step.costLabel}</Table.Cell>
                <Table.Cell>
                  <Code>{step.stopReason}</Code>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table>
      )}
      <p className="mt-2 text-slate-700 dark:text-slate-200">{measured.diffLine}</p>

      <h4 className={SECTION_HEADING}>{TRACE_CLAIMS_LABEL}</h4>
      <p className={`mt-1 ${MUTED}`}>
        아래는 에이전트(또는 샌드박스)가 쓴 내용입니다. 도구 호출은 모델이 요청한 것이고, 도구 결과는 샌드박스가
        보고한 것입니다. 플랫폼이 실행 여부를 확인하지 않았습니다.
      </p>

      <h5 className={SECTION_HEADING}>호출별 요청과 보고</h5>
      {claims.calls.length === 0 ? (
        <p className={`mt-1 ${MUTED}`}>기록된 호출 내용이 없습니다.</p>
      ) : (
        claims.calls.map((call) => (
          <details key={call.ordinal} className="mt-1">
            <summary className="cursor-pointer text-sm text-slate-700 dark:text-slate-200">{call.summary}</summary>
            {call.blocks.length > 0 && (
              <ul className="list-none p-0">
                {call.blocks.map((block, i) => (
                  <Block key={i} block={block} />
                ))}
              </ul>
            )}
          </details>
        ))
      )}

      <h5 className={SECTION_HEADING}>변경 내용 (diff)</h5>
      {claims.diff.stateNote && <p className={`mt-1 ${MUTED}`}>{claims.diff.stateNote}</p>}
      {claims.diff.truncatedLine && <p className={`mt-1 ${MUTED}`}>{claims.diff.truncatedLine}</p>}
      {claims.diff.stateNote === null && claims.diff.files.length === 0 && (
        <p className={`mt-1 ${MUTED}`}>변경된 파일이 없습니다.</p>
      )}
      {claims.diff.files.map((file, i) => (
        <details key={i} className="mt-1">
          <summary className="cursor-pointer text-sm text-slate-700 dark:text-slate-200">
            <Badge>{file.changeLabel}</Badge> <Code>{file.path}</Code>
            {file.linesLine && <span className={`ml-2 ${MUTED}`}>{file.linesLine}</span>}
          </summary>
          {file.noContentNote && <p className={`mt-1 ${MUTED}`}>{file.noContentNote}</p>}
          {file.text !== null && <Code className={`${TEXT_BLOCK} mt-1`}>{file.text}</Code>}
          {file.truncatedNote && <p className={`mt-1 ${MUTED}`}>{file.truncatedNote}</p>}
        </details>
      ))}

      <h5 className={SECTION_HEADING}>샌드박스 로그</h5>
      {claims.log.emptyNote && <p className={`mt-1 ${MUTED}`}>{claims.log.emptyNote}</p>}
      {claims.log.truncatedNote && <p className={`mt-1 ${MUTED}`}>{claims.log.truncatedNote}</p>}
      {claims.log.text !== null && (
        <details className="mt-1">
          <summary className="cursor-pointer text-sm text-slate-700 dark:text-slate-200">로그 펼치기</summary>
          <Code className={`${TEXT_BLOCK} mt-1`}>{claims.log.text}</Code>
        </details>
      )}
    </div>
  );
}

type LoadState =
  | { phase: "loading" }
  | { phase: "loaded"; trace: BuildTrace }
  | { phase: "failed"; failure: TraceLoadFailure };

function BuildTraceLoader({ buildId }: { buildId: string }) {
  const [state, setState] = useState<LoadState>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    buildsApi
      .trace(buildId)
      .then((trace) => {
        if (!cancelled) setState({ phase: "loaded", trace });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ phase: "failed", failure: traceLoadFailure(err instanceof ApiError ? err.status : null) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [buildId]);

  if (state.phase === "loading") {
    return <p className={`mt-2 ${MUTED}`}>트레이스를 불러오는 중입니다.</p>;
  }
  if (state.phase === "failed") {
    // A refusal or an absence is a standing fact about this build, not the result of an
    // action to announce, so the notice is not a live region.
    return (
      <Alert variant={state.failure.kind === "error" ? "error" : "warning"} className="mt-2">
        {state.failure.message}
      </Alert>
    );
  }
  return <BuildTraceView trace={state.trace} />;
}

/**
 * The trace entry point on one timeline build, and the trace it opens. `hasTrace` is the
 * timeline's flag; `canRead` is the frontend named predicate (`canReadBuildTrace`), which
 * mirrors the backend rule. The backend still enforces: a 403 is shown as an explained
 * refusal. The trace is fetched only when opened.
 */
export function BuildTraceEntry({
  buildId,
  hasTrace,
  canRead,
  status,
}: {
  buildId: string;
  hasTrace: unknown;
  canRead: boolean;
  status: unknown;
}) {
  const [open, setOpen] = useState(false);
  const view = traceEntryView(hasTrace, canRead, status);
  if (view.kind === "hidden") return null;
  if (view.kind === "none") return <p className={`mt-1 ${MUTED}`}>{view.text}</p>;
  const panelId = `build-trace-${buildId}`;
  return (
    <div className="mt-1">
      <Button
        variant="secondary"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? TRACE_CLOSE_LABEL : view.label}
      </Button>
      {open && (
        <Card id={panelId} className="mt-2 p-3">
          <BuildTraceLoader buildId={buildId} />
        </Card>
      )}
    </div>
  );
}
