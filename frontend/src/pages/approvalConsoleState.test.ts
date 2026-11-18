/**
 * The approval console's two stale-answer defects, as pure decisions (ADR 0021): what a
 * refused decision does when the approver has since opened another request, and which screen a
 * failed list re-read leaves up. The page wiring (`chosenIdRef`, the render chain) is not mounted
 * by any test; these are the functions it calls.
 */
import { describe, expect, it } from "vitest";

import { ApiError, NETWORK_ERROR_COPY, NETWORK_ERROR_STATUS } from "../api/client";
import type { ApprovalRequestSummary } from "../types/api";
import {
  LIST_STALE_NOTICE,
  consoleView,
  decisionRefusalFollowUp,
  decisionStillChosen,
} from "./approvalConsoleState";
import {
  ALREADY_DECIDED_MESSAGE,
  CONFLICTING_DECISION_MESSAGE,
  FORBIDDEN_DECISION_MESSAGE,
} from "./approvalMessages";

const http = (status: number) => new ApiError(status, "x", "x");
const REQUEST_A = "approval-a";
const REQUEST_B = "approval-b";

function refusal(status: number, chosenId: string | null, name = "정산 도구") {
  return decisionRefusalFollowUp({
    err: http(status),
    action: "approve",
    decidedId: REQUEST_A,
    decidedProjectName: name,
    chosenId,
  });
}

describe("a refused decision, when the approver has not moved on", () => {
  it("reads the request again into the pane, and the list, and says the plain sentence", () => {
    const followUp = refusal(409, REQUEST_A);

    expect(followUp).toEqual({ message: CONFLICTING_DECISION_MESSAGE, rereadPane: true, rereadList: true });
  });

  it("a 400 (already decided) re-reads the same way", () => {
    expect(refusal(400, REQUEST_A)).toEqual({ message: ALREADY_DECIDED_MESSAGE, rereadPane: true, rereadList: true });
  });

  it("a 403 re-reads nothing: a read does not change who may decide", () => {
    expect(refusal(403, REQUEST_A)).toEqual({ message: FORBIDDEN_DECISION_MESSAGE, rereadPane: false, rereadList: false });
  });
});

describe("a refused decision, when the approver has clicked another request since", () => {
  it("does not read the refused request into the pane: the pane and the selection stay where the approver put them", () => {
    const followUp = refusal(409, REQUEST_B);

    expect(followUp.rereadPane).toBe(false);
  });

  it("still reads the list again: its rows are shared, so the refused row is stale whichever request is open", () => {
    expect(refusal(409, REQUEST_B).rereadList).toBe(true);
    expect(refusal(400, REQUEST_B).rereadList).toBe(true);
  });

  it("still reports the refusal, naming the request it was about, so it is not read as being about the open one", () => {
    const followUp = refusal(409, REQUEST_B, "정산 도구");

    expect(followUp.message).toContain("정산 도구");
    expect(followUp.message).toContain(CONFLICTING_DECISION_MESSAGE);
    expect(followUp.message).not.toBe(CONFLICTING_DECISION_MESSAGE);
  });

  it("names the request for a refusal that re-reads nothing, too", () => {
    const followUp = refusal(403, REQUEST_B, "정산 도구");

    expect(followUp.message).toContain("정산 도구");
    expect(followUp.message).toContain(FORBIDDEN_DECISION_MESSAGE);
    expect(followUp.rereadPane).toBe(false);
    expect(followUp.rereadList).toBe(false);
  });

  it("makes an invisible or direction-changing character in that name visible", () => {
    const followUp = refusal(409, REQUEST_B, "report\u{202E}txt");

    expect(followUp.message).toContain("⟨U+202E⟩");
    expect(followUp.message).not.toContain("\u{202E}");
  });

  it("nothing chosen is not 'stayed': the pane is not read for a request nobody has open", () => {
    expect(refusal(409, null).rereadPane).toBe(false);
  });

  it("a failure that never reached the server says so and re-reads nothing, in either case", () => {
    for (const chosenId of [REQUEST_A, REQUEST_B]) {
      const followUp = decisionRefusalFollowUp({
        err: new ApiError(NETWORK_ERROR_STATUS, NETWORK_ERROR_COPY, undefined, "network"),
        action: "reject",
        decidedId: REQUEST_A,
        decidedProjectName: "정산 도구",
        chosenId,
      });

      expect(followUp.message).toContain(NETWORK_ERROR_COPY);
      expect(followUp.rereadPane, chosenId).toBe(false);
      expect(followUp.rereadList, chosenId).toBe(false);
    }
  });
});

describe("decisionStillChosen: a decision's result may touch the reader's typed comment only while its request is the chosen one", () => {
  it("true for the same request, false for another or for none", () => {
    expect(decisionStillChosen(REQUEST_A, REQUEST_A)).toBe(true);
    expect(decisionStillChosen(REQUEST_A, REQUEST_B)).toBe(false);
    expect(decisionStillChosen(REQUEST_A, null)).toBe(false);
  });
});

function summary(id: string): ApprovalRequestSummary {
  return {
    id,
    build_id: "build",
    project_id: "project",
    project_name: "p",
    requester_id: "u",
    requester_name: "u",
    risk_tier: "low",
    status: "PENDING",
    required_approvals: 1,
    approvals_so_far: 0,
    flagged_checks: [],
    diff_ref: null,
    preview_url: null,
    changed_counts: null,
    created_at: "2026-10-28T00:00:00Z",
  };
}

describe("consoleView: which screen the list's state leaves up", () => {
  it("the first read in flight is the skeleton", () => {
    expect(consoleView({ loading: true, data: null, error: null })).toEqual({
      pane: "skeleton",
      fullError: null,
      staleNotice: null,
    });
  });

  it("a FAILED FIRST read is the full-page error, and nothing else", () => {
    expect(consoleView({ loading: false, data: null, error: "서버에 연결하지 못했습니다" })).toEqual({
      pane: "error",
      fullError: "서버에 연결하지 못했습니다",
      staleNotice: null,
    });
  });

  it("a failed RE-READ keeps the console, with a notice that says the list may be out of date and why", () => {
    const view = consoleView({ loading: false, data: [summary("a")], error: "서버에 연결하지 못했습니다" });

    expect(view.pane).toBe("console");
    expect(view.fullError).toBeNull();
    expect(view.staleNotice).toContain(LIST_STALE_NOTICE);
    expect(view.staleNotice).toContain("서버에 연결하지 못했습니다");
  });

  it("a failed re-read of an empty list keeps the empty screen, with the notice", () => {
    const view = consoleView({ loading: false, data: [], error: "x" });

    expect(view.pane).toBe("empty");
    expect(view.staleNotice).not.toBeNull();
    expect(view.fullError).toBeNull();
  });

  it("a re-read in flight over data keeps the console (no skeleton), without a notice", () => {
    expect(consoleView({ loading: true, data: [summary("a")], error: null })).toEqual({
      pane: "console",
      fullError: null,
      staleNotice: null,
    });
  });

  it("a healthy list is the console, an empty one the empty screen, neither with a notice", () => {
    expect(consoleView({ loading: false, data: [summary("a")], error: null })).toEqual({
      pane: "console",
      fullError: null,
      staleNotice: null,
    });
    expect(consoleView({ loading: false, data: [], error: null }).pane).toBe("empty");
  });

  it("the stale notice is worded as a warning about the list, never as the old full-page sentence", () => {
    expect(LIST_STALE_NOTICE).toContain("최신이 아닐 수 있습니다");
  });
});
