import { useRef, useState } from "react";

import { approvalsApi } from "../api/approvals";
import {
  approveBlock,
  canApproveRequest,
  canRejectRequest,
  decisionBlockMessage,
  isApprovalPending,
  rejectBlock,
} from "../auth/approvalGate";
import {
  approvalStatusLabel,
  approvalStatusTone,
  RISK_LEGEND_CLOSING,
  RISK_LEGEND_LEAD,
  riskLegendItems,
  riskTierLabel,
  riskTierTone,
} from "../components/approvalDisplay";
import { Alert, Badge, Button, Card, EmptyState, ListRow, Skeleton, linkClassName } from "../components/ui";
import { visibleControls } from "../components/ui/visibleControls";
import { changedCountsLine } from "../components/measuredFactsLines";
import { policyCheckLabels } from "../components/policyCheckDisplay";
import { previewStartFailureLabel } from "../components/previewStartFailure";
import { useIdentity } from "../context/IdentityContext";
import { useLoader, useLoaderOnChange } from "../loading/useLoader";
import type { ApprovalRequest, ApprovalRequestSummary } from "../types/api";
import { consoleView, decisionRefusalFollowUp, decisionStillChosen } from "./approvalConsoleState";
import { approvalLoadFailureMessage } from "./approvalMessages";
import { ApprovalDetailPane } from "./ApprovalDetailPane";

// What the page says when an approve/reject is refused, and which decisions it offers, are decided
// by named, tested functions: `approvalMessages.ts` and `auth/approvalGate.ts`
//. The backend's English `detail` never reaches the screen.

const LIST_LOAD_FAILURE_FALLBACK = "승인 요청 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
const DETAIL_LOAD_FAILURE_FALLBACK = "요청 상세 정보를 불러오지 못했습니다.";

export function ApprovalConsolePage() {
  const { currentUser } = useIdentity();
  // The list and the open request are two loads, each newest-wins. A failed list load
  // must not render as the empty-list copy (c5): its message is `loadError`, rendered apart.
  const list = useLoaderOnChange<ApprovalRequestSummary[]>(
    (signal) => approvalsApi.list(undefined, { signal }),
    [],
    (err) => approvalLoadFailureMessage(err, LIST_LOAD_FAILURE_FALLBACK),
  );
  // `openDetail` is the race: a second click while the first request is out. The loader aborts the
  // first and ignores its answer if it lands anyway, so the last *click* wins, not the last response.
  const detail = useLoader<ApprovalRequest>((err) =>
    approvalLoadFailureMessage(err, DETAIL_LOAD_FAILURE_FALLBACK),
  );
  const approvals = list.data ?? [];
  const selected = detail.data;
  // The skeleton and the full-page error are for the FIRST read. A re-read that keeps its data
  // (after a decision, or after a refusal) must not unmount the open request and the error it is
  // showing, and a failed one is a notice above the console (`consoleView`).
  const view = consoleView(list);
  const [rejectComment, setRejectComment] = useState("");
  const [acting, setActing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The request the approver chose last: set the moment a row is clicked, before its read lands,
  // because a decision's answer can arrive while that read is still out. Rows stay clickable while
  // a decision is in flight (reading another request is harmless, and a disabled row would drop
  // keyboard focus), so every follow-up of a decision asks this before it touches the pane.
  const chosenIdRef = useRef<string | null>(null);
  // The request the pane shows as of the last render, for settling `chosenIdRef` when a read fails.
  const shownIdRef = useRef<string | null>(null);
  shownIdRef.current = selected?.id ?? null;

  /** Clears the banner of a previous failure, whichever of the two loads or actions raised it. */
  function clearErrors() {
    setError(null);
    detail.clearError();
  }

  async function openDetail(id: string) {
    clearErrors();
    setRejectComment("");
    chosenIdRef.current = id;
    // Keeps the open request on screen until the new one arrives, as before; a failure leaves it
    // there, and then it is the request the approver is looking at, so it is the chosen one again.
    const outcome = await detail.run((signal) => approvalsApi.get(id, { signal }), { keepData: true });
    if (outcome.kind === "error" && chosenIdRef.current === id) chosenIdRef.current = shownIdRef.current;
  }

  /** A refused decision: say so, and read again what the refusal says is stale (a 400 or 409). The
   * pane is read again only if the refused request is still the one the approver chose; the
   * message names the request when they have moved on (`decisionRefusalFollowUp`). `openDetail`
   * would clear the message, so the pane read is started here. */
  function followRefusal(err: unknown, action: "approve" | "reject", decided: ApprovalRequest) {
    const followUp = decisionRefusalFollowUp({
      err,
      action,
      decidedId: decided.id,
      // The detail payload carries no project name; the list row the approver clicked does.
      decidedProjectName: approvals.find((a) => a.id === decided.id)?.project_name ?? decided.id,
      chosenId: chosenIdRef.current,
    });
    setError(followUp.message);
    if (followUp.rereadPane) {
      void detail.run((signal) => approvalsApi.get(decided.id, { signal }), { keepData: true });
    }
    if (followUp.rereadList) void list.reload({ keepData: true });
  }

  /** A decision's response replaces the open request only if that request is still the one open:
   * a later click on another row has already moved on, and the old answer must not drag it back. */
  function showDecided(updated: ApprovalRequest) {
    detail.update((current) => (current === null || current.id === updated.id ? updated : current));
  }

  async function handleApprove() {
    if (!selected) return;
    const decided = selected;
    clearErrors();
    setActing(true);
    try {
      const updated = await approvalsApi.approve(decided.id);
      showDecided(updated);
      await list.reload({ keepData: true });
    } catch (err) {
      followRefusal(err, "approve", decided);
    } finally {
      setActing(false);
    }
  }

  async function handleReject() {
    if (!selected) return;
    if (!rejectComment.trim()) {
      setError("반려 사유(코멘트)를 입력해야 합니다.");
      return;
    }
    const decided = selected;
    clearErrors();
    setActing(true);
    try {
      const updated = await approvalsApi.reject(decided.id, { comment: rejectComment });
      showDecided(updated);
      // The comment box now belongs to whichever request the approver moved to; clear it only if
      // that is still the one just rejected.
      if (decisionStillChosen(decided.id, chosenIdRef.current)) setRejectComment("");
      await list.reload({ keepData: true });
    } catch (err) {
      followRefusal(err, "reject", decided);
    } finally {
      setActing(false);
    }
  }

  // What the backend would accept, from named gates (auth/approvalGate.ts): the role rung, and not
  // the requester, not the project's owner, not someone who already approved, still PENDING. The
  // backend still refuses what a stale page offers; this only stops offering what it knows it will.
  const mayApprove = canApproveRequest(currentUser, selected);
  const mayReject = canRejectRequest(currentUser, selected);
  const blockMessage = decisionBlockMessage(
    rejectBlock(currentUser, selected) ?? approveBlock(currentUser, selected),
  );
  // The list row of the open request: the detail payload carries neither the project's nor the
  // requester's name. The message of a failed read or decision shows above the columns while no
  // request is open, and in the open request's action bar once one is.
  const selectedRow = selected ? approvals.find((a) => a.id === selected.id) : undefined;
  const errorMessage = error ?? detail.error;
  const pending = approvals.filter((a) => isApprovalPending(a.status));
  const decided = approvals.filter((a) => !isApprovalPending(a.status));

  return (
    <div>
      {view.pane === "skeleton" ? (
        <Skeleton label="승인 요청 목록을 불러오는 중" />
      ) : view.pane === "error" ? (
        <Alert variant="error" live>
          {view.fullError}
        </Alert>
      ) : (
        <>
          {/* A failed re-read of a list the console already has: the console stays, with a way to
              ask again. It is the result of something the approver just did (a decision, a
              retry), so it is announced. */}
          {view.staleNotice && (
            <Alert variant="warning" live className="mb-4">
              {view.staleNotice}{" "}
              <Button variant="secondary" onClick={() => void list.reload({ keepData: true })}>
                다시 시도
              </Button>
            </Alert>
          )}
          {view.pane === "empty" ? (
            <EmptyState
              title="아직 승인 요청이 없습니다"
              description="빌드가 정책 게이트를 통과하면 여기에 표시됩니다."
            />
          ) : (
            <div>
              <RiskLegend />
              {/* Above both columns while no request is open: `openDetail`'s own failure (e.g. a
                  request that was superseded/deleted between the list render and the click) can
                  fail before `selected` is ever set, and a message that lives only in the detail
                  pane's action bar would be unreachable on that exact path. Once a request is open
                  the same message is announced from its action bar instead (never both). */}
              {errorMessage && !selected && (
                <Alert variant="error" live className="mb-4">
                  {errorMessage}
                </Alert>
              )}
              <div className="flex flex-col items-start gap-6 md:flex-row">
                <div className="flex-1 self-stretch">
                  <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                    대기 중 ({pending.length})
                  </h3>
                  {pending.length === 0 ? (
                    <EmptyState tone="neutral" title="승인 대기 항목이 없습니다" className="mt-2" />
                  ) : (
                    <ApprovalList items={pending} onSelect={openDetail} selectedId={selected?.id} />
                  )}

                  <h3 className="mt-6 text-base font-semibold text-slate-900 dark:text-slate-100">
                    최근 처리됨 ({decided.length})
                  </h3>
                  {decided.length === 0 ? (
                    <EmptyState tone="neutral" title="처리된 항목이 없습니다" className="mt-2" />
                  ) : (
                    <ApprovalList items={decided} onSelect={openDetail} selectedId={selected?.id} />
                  )}
                </div>

                {!selected && (
                  // Plain text, no heading and no live region: it says where the detail will appear.
                  // Below `md` the detail stacks under the lists, where an empty card would only push
                  // them down, so it is hidden there.
                  <Card className="hidden w-full flex-1 p-4 md:block">
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      목록에서 요청을 선택하면 상세가 여기에 표시됩니다.
                    </p>
                  </Card>
                )}
                {selected && (
                  <ApprovalDetailPane
                    key={selected.id}
                    request={selected}
                    projectName={selectedRow ? visibleControls(selectedRow.project_name) : null}
                    requesterName={selectedRow ? visibleControls(selectedRow.requester_name) : null}
                    diffRef={selectedRow?.diff_ref ?? null}
                    mayApprove={mayApprove}
                    mayReject={mayReject}
                    blockMessage={blockMessage}
                    errorMessage={errorMessage}
                    rejectComment={rejectComment}
                    onRejectCommentChange={setRejectComment}
                    onApprove={handleApprove}
                    onReject={handleReject}
                    acting={acting}
                  />
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** The key to the risk badges on the rows: static text beside plain badges, no live region. */
function RiskLegend() {
  return (
    <div className="mb-4 text-xs text-slate-600 dark:text-slate-300">
      <p className="font-medium text-slate-700 dark:text-slate-200">{RISK_LEGEND_LEAD}</p>
      <ul className="mt-1 flex list-none flex-col gap-1 p-0 md:flex-row md:flex-wrap md:gap-x-5">
        {riskLegendItems().map((item) => (
          <li key={item.tier} className="flex items-start gap-2">
            <Badge tone={riskTierTone(item.tier)}>{item.label}</Badge>
            <span>{item.description}</span>
          </li>
        ))}
      </ul>
      <p className="mt-1">{RISK_LEGEND_CLOSING}</p>
    </div>
  );
}

function ApprovalList({
  items,
  onSelect,
  selectedId,
}: {
  items: ApprovalRequestSummary[];
  onSelect: (id: string) => void;
  selectedId?: string;
}) {
  return (
    <ul className="mt-2 list-none space-y-2 p-0">
      {items.map((a) => {
        const isSelected = a.id === selectedId;
        return (
          <ListRow
            key={a.id}
            selected={isSelected}
            ariaLabel={`${visibleControls(a.project_name)} 요청 상세 보기`}
            onActivate={() => onSelect(a.id)}
            className="block rounded-card border border-slate-200 p-3 text-sm dark:border-slate-700"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <strong className="font-medium text-slate-900 dark:text-slate-100">
                  {visibleControls(a.project_name)}
                </strong>{" "}
                <Badge tone={riskTierTone(a.risk_tier)}>{riskTierLabel(a.risk_tier)}</Badge>{" "}
                <Badge tone={approvalStatusTone(a.status)}>{approvalStatusLabel(a.status)}</Badge>
              </div>
              {/* A visual cue that the row opens the detail; the row's own aria-label already says so. */}
              <span aria-hidden="true" className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
                상세 ›
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              요청자: {visibleControls(a.requester_name)} · {new Date(a.created_at).toLocaleString()}
            </div>
            {a.flagged_checks.length > 0 && (
              <Alert variant="warning" className="mt-2">
                지적된 검사: {policyCheckLabels(a.flagged_checks)}
              </Alert>
            )}
            {changedCountsLine(a.changed_counts) && (
              <div className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                {changedCountsLine(a.changed_counts)}
              </div>
            )}
            <div className="mt-1 text-xs">
              {a.diff_ref && <span className="mr-3">PR: {a.diff_ref}</span>}
              {a.preview_url && (
                <a
                  href={a.preview_url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className={linkClassName()}
                >
                  프리뷰 열기
                </a>
              )}
              {!a.preview_url && a.preview_start_failure && (
                <span className="text-slate-600 dark:text-slate-300">
                  {previewStartFailureLabel(a.preview_start_failure)}
                </span>
              )}
            </div>
          </ListRow>
        );
      })}
    </ul>
  );
}
