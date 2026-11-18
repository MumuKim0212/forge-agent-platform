import { useState } from "react";

import {
  approvalStatusLabel,
  approvalStatusTone,
  decisionLabel,
  riskTierLabel,
  riskTierTone,
} from "../components/approvalDisplay";
import {
  DEFAULT_DETAIL_TAB,
  approvalsLine,
  agentSummaryNotice,
  detailTabs,
  summaryRows,
  verdictChips,
  type DetailTabId,
} from "../components/approvalDetailView";
import {
  AgentSummary,
  ChangedFilesSection,
  DriftLine,
  PolicyChecksSection,
  PreviewStartFailureNote,
  RepairAttemptsNote,
  RequestTextSection,
  SmokeChecksSection,
} from "../components/MeasuredFacts";
import { Alert, Badge, Button, Card, Field, Input, Panel, StatTile, TabPanel, Tabs, cx } from "../components/ui";
import { FOCUS_RING } from "../components/ui/focusRing";
import { visibleControls } from "../components/ui/visibleControls";
import type { ApprovalRequest } from "../types/api";

// The open request as a pinned header,
// four tabs and a docked action bar. The header and the action bar never scroll; the tab content
// scrolls inside its own named region. Nothing here decides who may approve: the page passes the
// answers of the named gates in `auth/approvalGate.ts`, and this file only draws them. No part of it
// is a link to a file's content, and no headline in 요약 points at another tab.

const ID_BASE = "approval-detail";
const HEADING_ID = "approval-detail-heading";

const MUTED = "text-xs text-slate-500 dark:text-slate-400";
const FACT_LABEL = "font-medium text-slate-800 dark:text-slate-100";

export interface ApprovalDetailPaneProps {
  request: ApprovalRequest;
  /** From the list row the approver chose (the detail payload has neither), already passed through
   * `visibleControls` by the caller; null when the row is no longer in the list. */
  projectName: string | null;
  requesterName: string | null;
  diffRef: string | null;
  mayApprove: boolean;
  mayReject: boolean;
  /** Why the viewer may not decide, or null (a request that is simply not pending says nothing more). */
  blockMessage: string | null;
  /** The message of a failed read or decision. It is announced, and it lives in the action bar. */
  errorMessage: string | null;
  rejectComment: string;
  onRejectCommentChange: (value: string) => void;
  onApprove: () => void;
  onReject: () => void;
  acting: boolean;
}

// The height: from `md` up the pane is as tall as the screen minus what sits above it on the page
// (the shell's top bar, the page title and the risk key: about 13.5rem when nothing is announced
// above the columns), so the action bar is on screen without scrolling the page; it also sticks, so
// after the page scrolls it stays in view. Below `md` it stacks in the flow with no fixed height.
/**
 * The pane keeps its tab in its own state, and the caller keys it by the request's id: choosing
 * another request mounts a fresh pane on 요약, while a decision on the open request (same id) keeps
 * the tab it was made from.
 */
export function ApprovalDetailPane(props: ApprovalDetailPaneProps) {
  const { request } = props;
  const [tab, setTab] = useState<DetailTabId>(DEFAULT_DETAIL_TAB);
  return (
    <Card
      role="region"
      aria-labelledby={HEADING_ID}
      className="flex w-full flex-1 flex-col overflow-hidden md:sticky md:top-4 md:h-[calc(100vh-15rem)] md:min-h-[28rem]"
    >
      <PaneHeader {...props} />
      <Tabs
        idBase={ID_BASE}
        tabs={detailTabs(request)}
        activeId={tab}
        onChange={setTab}
        className="shrink-0 px-4"
      />
      {/* The scrolling area: a named region a keyboard can reach and scroll. Only the active panel is
          mounted inside it. */}
      <div
        role="region"
        aria-label="요청 상세 내용"
        tabIndex={0}
        className={cx("min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-3", FOCUS_RING)}
      >
        <TabPanel idBase={ID_BASE} tabId={tab} focusable={false} className="space-y-3">
          <ActiveTab tab={tab} {...props} />
        </TabPanel>
      </div>
      <ActionBar {...props} />
    </Card>
  );
}

function PaneHeader({ request, projectName, requesterName }: ApprovalDetailPaneProps) {
  const chips = verdictChips(request);
  return (
    <div className="shrink-0 border-b border-slate-200 px-4 pb-3 pt-4 dark:border-slate-700">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h3 id={HEADING_ID} className="text-base font-semibold text-slate-900 dark:text-slate-100">
          요청 상세
        </h3>
        {projectName !== null && (
          <span className="text-sm font-medium text-slate-800 dark:text-slate-100">{projectName}</span>
        )}
        <span className={MUTED}>ID: {request.id}</span>
      </div>
      <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">
        위험도: <Badge tone={riskTierTone(request.risk_tier)}>{riskTierLabel(request.risk_tier)}</Badge> · 상태:{" "}
        <Badge tone={approvalStatusTone(request.status)}>{approvalStatusLabel(request.status)}</Badge> · 필요 승인
        수: {request.required_approvals}
        {requesterName !== null && <> · 요청자: {requesterName}</>}
      </p>
      {/* Four verdicts, text only: a label and a value, no icon. They are neither links nor buttons. */}
      <div role="group" aria-label="점검 결과 요약" className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-4">
        {chips.map((chip) => (
          <StatTile
            key={chip.id}
            size="compact"
            label={chip.label}
            value={<Badge tone={chip.tone}>{chip.value}</Badge>}
          />
        ))}
      </div>
    </div>
  );
}

function ActiveTab({ tab, request, diffRef }: { tab: DetailTabId } & ApprovalDetailPaneProps) {
  switch (tab) {
    case "summary":
      return <SummaryTab request={request} diffRef={diffRef} />;
    case "changes":
      return <ChangedFilesSection changed={request.changed_files} drift={request.protected_path_drift} framed />;
    case "checks":
      return <ChecksTab request={request} />;
    case "history":
      return <HistoryTab request={request} />;
  }
}

/**
 * 요약: the request text, one card of measured-fact rows, the PR, and the agent notice, each its own card.
 * Plain text throughout: no link and no control, the other tabs hold the detail and nothing here switches to
 * them. Each row reads once (`summaryRows` decides which rows carry a label).
 */
function SummaryTab({ request, diffRef }: { request: ApprovalRequest; diffRef: string | null }) {
  const rows = summaryRows(request);
  const agentNotice = agentSummaryNotice(request.agent_summary);
  return (
    <>
      <RequestTextSection text={request.request_text} framed />
      <Panel title="측정된 사실 · 한눈에" headingLevel={3} compact>
        <ul className="mt-2 list-none space-y-1.5 p-0 text-sm text-slate-700 dark:text-slate-200">
          {rows.map((row) => (
            <li key={row.id}>
              {row.id === "drift" ? (
                <DriftLine drift={request.protected_path_drift} className="mt-0" />
              ) : (
                <>
                  {row.label !== null && <span className={FACT_LABEL}>{row.label} </span>}
                  {row.text}
                </>
              )}
            </li>
          ))}
        </ul>
      </Panel>
      {diffRef !== null && (
        <Panel title="PR" headingLevel={3} compact>
          <p className="mt-2 text-sm text-slate-700 dark:text-slate-200">{diffRef}</p>
        </Panel>
      )}
      {agentNotice !== null && (
        <Panel title="에이전트 요약" headingLevel={3} compact className="border-dashed">
          <p className={`mt-2 ${MUTED}`}>{agentNotice}</p>
        </Panel>
      )}
    </>
  );
}

/** 점검: what the platform found, in the order the spec fixes, and last the agent's own words. */
function ChecksTab({ request }: { request: ApprovalRequest }) {
  return (
    <>
      <PreviewStartFailureNote code={request.preview_start_failure} framed />
      {/* The repair note sits directly above the smoke check it qualifies. */}
      <RepairAttemptsNote attempts={request.repair_attempts} routes={request.repair_routes} framed />
      <SmokeChecksSection smoke={request.smoke_checks} framed />
      <PolicyChecksSection verdict={request.policy_verdict} framed />
      {/* The agent's own words, after every measured fact, collapsed, in a dashed muted box. */}
      <AgentSummary text={request.agent_summary} framed />
    </>
  );
}

function HistoryTab({ request }: { request: ApprovalRequest }) {
  return (
    <Panel title="결정 이력" headingLevel={3} compact>
      {request.decisions.length === 0 ? (
        <p className={`mt-2 ${MUTED}`}>아직 기록된 결정이 없습니다.</p>
      ) : (
        <ul className="mt-2 space-y-1 text-sm">
          {request.decisions.map((d) => (
            <li key={d.id}>
              {decisionLabel(d.decision)} — {new Date(d.created_at).toLocaleString()}
              {d.comment && (
                <div className="text-slate-500 dark:text-slate-400">코멘트: {visibleControls(d.comment)}</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * The decision controls, outside the tabs and shown in every one. What it offers is exactly what the
 * page's gates say (`mayApprove`, `mayReject`); when they say no, the refusal's reason is shown in the
 * buttons' place. The result of a read or a decision that failed is announced here, never inside a
 * tab panel, so switching tabs cannot swallow it.
 */
function ActionBar({
  request,
  mayApprove,
  mayReject,
  blockMessage,
  errorMessage,
  rejectComment,
  onRejectCommentChange,
  onApprove,
  onReject,
  acting,
}: ApprovalDetailPaneProps) {
  return (
    <div
      role="group"
      aria-label="결정"
      className="shrink-0 border-t border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-700 dark:bg-slate-800/60"
    >
      {errorMessage && (
        <Alert variant="error" live className="mb-2">
          {errorMessage}
        </Alert>
      )}
      <p className="text-xs font-medium text-slate-700 dark:text-slate-200">
        {approvalsLine(request.required_approvals, request.decisions)}
      </p>
      {blockMessage && (
        <Alert variant="warning" className="mt-2">
          {blockMessage}
        </Alert>
      )}
      {(mayApprove || mayReject) && (
        <div className="mt-2 flex items-end gap-3">
          {mayReject && (
            <Field label="반려 사유" htmlFor="approval-reject-comment" required className="mb-0! min-w-0 flex-1">
              <Input
                id="approval-reject-comment"
                value={rejectComment}
                onChange={(e) => onRejectCommentChange(e.target.value)}
                placeholder="반려 사유 (필수)"
              />
            </Field>
          )}
          {mayReject && (
            <Button variant="danger" onClick={onReject} busy={acting}>
              반려
            </Button>
          )}
          {mayApprove && (
            <Button onClick={onApprove} busy={acting}>
              승인
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
