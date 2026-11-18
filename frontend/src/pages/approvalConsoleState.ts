/**
 * What the approval console does when an answer arrives after the reader has moved on, and which
 * of its screens a list read leaves up (ADR 0021: decision logic, so a `node` test reaches it).
 *
 * Two defects of the same family: a result that arrives later is allowed to rewrite what the reader
 * chose or to hide what they were working on.
 *
 * - **A refusal never moves the pane.** An approve or reject is sent for the request that was open
 *   when it was clicked. Its answer can come back after the approver has clicked another row, and
 *   the old follow-up (read the refused request again) aborted the newer load and put the older
 *   request back, with its error, under the reader's hands. The follow-up now asks which request the
 *   reader last chose, and a refusal for any other request does not touch the pane.
 * - **A failed list re-read never hides the console.** The list loader keeps its data when a re-read
 *   fails and only sets an error; the page used to put that error in place of everything, including
 *   the open request and the news that a decision had just landed.
 */
import { visibleControls } from "../components/ui/visibleControls";
import type { ApprovalRequestSummary } from "../types/api";
import {
  approvalDecisionFailureMessage,
  shouldRereadAfterDecisionFailure,
  type ApprovalDecisionAction,
} from "./approvalMessages";

export interface DecisionFollowUp {
  /** What the console says about the refusal. For a request the reader has left, it names the
   * request, so the sentence is never read as being about the one now open. */
  message: string;
  /** Read the decided request into the pane again. Only when it is still the one the reader chose. */
  rereadPane: boolean;
  /** Read the list again: its rows are shared, so a stale one is stale whichever request is open. */
  rereadList: boolean;
}

export interface DecisionRefusal {
  err: unknown;
  action: ApprovalDecisionAction;
  /** The request the decision was sent for. */
  decidedId: string;
  decidedProjectName: string;
  /** The request the reader chose last (clicked, or opened first), `null` when none. */
  chosenId: string | null;
}

export function decisionRefusalFollowUp(refusal: DecisionRefusal): DecisionFollowUp {
  const stayed = refusal.chosenId === refusal.decidedId;
  const said = approvalDecisionFailureMessage(refusal.err, refusal.action);
  const reread = shouldRereadAfterDecisionFailure(refusal.err);
  return {
    message: stayed ? said : `‘${visibleControls(refusal.decidedProjectName)}’ 요청: ${said}`,
    rereadPane: reread && stayed,
    rereadList: reread,
  };
}

/** Whether a decision's own result may touch the reader's pane state (the typed reject comment):
 * only while the request it was sent for is still the one they chose. */
export function decisionStillChosen(decidedId: string, chosenId: string | null): boolean {
  return decidedId === chosenId;
}

export type ConsolePane = "skeleton" | "error" | "empty" | "console";

export interface ConsoleView {
  pane: ConsolePane;
  /** The failed first read, shown instead of the console. */
  fullError: string | null;
  /** A failed re-read, shown above the console, which stays. */
  staleNotice: string | null;
}

export const LIST_STALE_NOTICE = "목록을 새로 고치지 못했습니다. 아래 목록은 최신이 아닐 수 있습니다.";

/**
 * Which screen the console is on, from the list loader's state. The skeleton and the full-page
 * error are for the FIRST read only (`data === null`); once the console has a list, a failed
 * re-read is a notice above it, never a replacement for it.
 */
export function consoleView(list: {
  loading: boolean;
  data: ApprovalRequestSummary[] | null;
  error: string | null;
}): ConsoleView {
  if (list.data === null) {
    if (list.loading) return { pane: "skeleton", fullError: null, staleNotice: null };
    if (list.error !== null) return { pane: "error", fullError: list.error, staleNotice: null };
    return { pane: "empty", fullError: null, staleNotice: null };
  }
  return {
    pane: list.data.length === 0 ? "empty" : "console",
    fullError: null,
    staleNotice: list.error === null ? null : `${LIST_STALE_NOTICE} ${list.error}`,
  };
}
