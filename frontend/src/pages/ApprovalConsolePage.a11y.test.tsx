// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment: where the preview smoke check sits in the approval console's
 * detail. The order is the structure a screen-reader user moves through, and
 * request text, changed files and drift, the
 * smoke check, per-check results, the agent's own words last, and the decision controls after every
 * measured fact. That order runs across the four tabs (요약, 변경, 점검, 이력), with the
 * decision controls docked outside them. This file owns that order and the absence of a live region around the section;
 * the wording is `measuredFactsLines.test.ts`'s and the list semantics are `MeasuredFacts.a11y.test.tsx`'s.
 *
 * The api module and the identity hook are mocked; a person clicks the row, as an approver does.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "../api/approvals";
import type { ApprovalRequest, ApprovalRequestSummary, SmokeChecks } from "../types/api";
import { ApprovalConsolePage } from "./ApprovalConsolePage";

vi.mock("../api/approvals", () => ({
  approvalsApi: { list: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn() },
}));

vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: {
      id: "u-approver",
      name: "승인자",
      email: "approver@example.test",
      role: "approver",
      team_id: null,
      created_at: "2026-10-01T00:00:00Z",
    },
    mode: "local",
    loading: false,
  }),
}));

const SUMMARY: ApprovalRequestSummary = {
  id: "a1",
  build_id: "b1",
  project_id: "p1",
  project_name: "휴가 도구",
  requester_id: "u-req",
  requester_name: "요청자",
  risk_tier: "low",
  status: "PENDING",
  required_approvals: 1,
  approvals_so_far: 0,
  flagged_checks: [],
  diff_ref: null,
  preview_url: null,
  changed_counts: null,
  created_at: "2026-10-01T00:00:00Z",
};

function detail(smoke: SmokeChecks | null, repairAttempts?: number | null, repairRoutes?: unknown): ApprovalRequest {
  return {
    id: "a1",
    build_id: "b1",
    project_id: "p1",
    requester_id: "u-req",
    project_owner_id: "u-owner",
    policy_verdict_id: "v1",
    risk_tier: "low",
    status: "PENDING",
    required_approvals: 1,
    superseded_by_build_id: null,
    superseded_at: null,
    created_at: "2026-10-01T00:00:00Z",
    decisions: [],
    policy_verdict: {
      id: "v1",
      build_id: "b1",
      risk_tier: "low",
      created_at: "2026-10-01T00:00:00Z",
      checks: [{ id: "c1", check_name: "secrets_pii", result: "pass", summary: "ok", findings: null }],
    },
    request_text: "휴가 신청 화면을 만들어 주세요",
    changed_files: {
      entries: [{ path: "app/app.py", change: "modified" }],
      totals: { added: 0, modified: 1, deleted: 0 },
      truncated: false,
    },
    protected_path_drift: ["Dockerfile"],
    smoke_checks: smoke,
    ...(repairAttempts === undefined ? {} : { repair_attempts: repairAttempts }),
    ...(repairRoutes === undefined ? {} : { repair_routes: repairRoutes as ApprovalRequest["repair_routes"] }),
    agent_summary: "에이전트의 설명",
  };
}

const SMOKE: SmokeChecks = {
  routes: [
    { path: "/", status: 200, outcome: "ok" },
    { path: "/boom", status: 500, outcome: "server_error" },
  ],
  skipped: 0,
  truncated: false,
};

beforeEach(() => {
  vi.mocked(approvalsApi.list).mockReset().mockResolvedValue([SUMMARY]);
  vi.mocked(approvalsApi.get).mockReset();
});

async function openDetail(
  smoke: SmokeChecks | null,
  repairAttempts?: number | null,
  repairRoutes?: unknown,
): Promise<HTMLElement> {
  vi.mocked(approvalsApi.get).mockResolvedValue(detail(smoke, repairAttempts, repairRoutes));
  const user = userEvent.setup();
  render(<ApprovalConsolePage />);
  await user.click(await screen.findByRole("listitem", { name: "휴가 도구 요청 상세 보기" }));
  return screen.findByRole("region", { name: "요청 상세" });
}

/** Opens a tab by a person's click, the way an approver does. */
async function chooseTab(name: RegExp | string): Promise<void> {
  await userEvent.click(screen.getByRole("tab", { name }));
}

function isBefore(first: Element, second: Element): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
}

const AGENT_LABEL = "에이전트 작성 · 플랫폼이 확인하지 않은 내용";

describe("the preview smoke check inside the approval console's detail", () => {
  it("sits in the 점검 tab after the changed files' tab, before the policy checks, the agent text and the decision controls", async () => {
    await openDetail(SMOKE);

    // The changed files and the drift live in the tab before the checks tab.
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["요약", "변경 (1)", "점검 (1)", "이력 (0)"]);
    await chooseTab(/^점검/);

    const smoke = screen.getByRole("heading", { name: "프리뷰 응답 점검" });
    const policy = screen.getByRole("heading", { name: /^정책 검사/ });
    // The agent's summary by its own label, not by position: the changed-files groups are details too.
    const agent = screen.getByText(AGENT_LABEL).closest("details");
    const approve = screen.getByRole("button", { name: "승인" });
    expect(agent).not.toBeNull();

    expect(isBefore(smoke, policy)).toBe(true);
    expect(isBefore(smoke, agent!)).toBe(true);
    // The decision controls are outside the tabs, after everything the tab shows.
    expect(isBefore(smoke, approve)).toBe(true);
    expect(isBefore(agent!, approve)).toBe(true);
    // The route list is part of the section, ahead of the policy checks.
    expect(isBefore(screen.getByText("/boom"), policy)).toBe(true);
  });

  it("keeps the spec's whole content order across the four tabs, in the order the tabs come", async () => {
    vi.mocked(approvalsApi.get).mockResolvedValue({
      ...detail(SMOKE, 2),
      preview_start_failure: "UNEXPECTED",
      decisions: [{ id: "d1", actor_id: "u-x", decision: "approve", comment: null, created_at: "2026-10-01T00:00:00Z" }],
    });
    const user = userEvent.setup();
    render(<ApprovalConsolePage />);
    await user.click(await screen.findByRole("listitem", { name: "휴가 도구 요청 상세 보기" }));
    await screen.findByRole("region", { name: "요청 상세" });

    // Walk the tabs left to right and note the content markers each shows, in document order. One
    // list, so the order is pinned across tabs (the tab strip's own order is the other half).
    const seen: string[] = [];

    // 요약: the request text first (the headline lines follow it).
    const summaryText = screen.getByRole("heading", { name: "요청 내용" });
    const headlines = screen.getByRole("heading", { name: "측정된 사실 · 한눈에" });
    expect(isBefore(summaryText, headlines)).toBe(true);
    seen.push("요청 내용");

    await chooseTab(/^변경/);
    const changedHeading = screen.getByRole("heading", { name: "변경된 파일" });
    const groups = Array.from(screen.getByRole("tabpanel").querySelectorAll("details"));
    expect(groups).toHaveLength(1);
    expect(groups[0].open).toBe(false);
    expect(groups[0].querySelector("summary")?.textContent).toBe("수정 1개");
    expect(isBefore(changedHeading, groups[0])).toBe(true);
    expect(isBefore(groups[0], screen.getByText(/보호 경로 원복/))).toBe(true);
    seen.push("변경된 파일");

    await chooseTab(/^점검/);
    const panel = screen.getByRole("tabpanel");
    const agent = screen.getByText(AGENT_LABEL).closest("details")!;
    // The changed-files groups are not here, and the agent's details is the only one.
    expect(Array.from(panel.querySelectorAll("details"))).toEqual([agent]);
    expect(agent.open).toBe(false);
    const checks = [
      screen.getByRole("heading", { name: "프리뷰 시작" }),
      screen.getByRole("heading", { name: "수리 시도" }),
      screen.getByRole("heading", { name: "프리뷰 응답 점검" }),
      screen.getByRole("heading", { name: /^정책 검사/ }),
      agent,
    ];
    for (let i = 1; i < checks.length; i++) {
      expect(isBefore(checks[i - 1], checks[i]), `checks position ${i}`).toBe(true);
    }
    seen.push("프리뷰 시작", "수리 시도", "프리뷰 응답 점검", "정책 검사", "에이전트 요약");

    await chooseTab(/^이력/);
    expect(screen.getByRole("heading", { name: "결정 이력" })).toBeInTheDocument();
    seen.push("결정 이력");

    expect(seen).toEqual([
      "요청 내용",
      "변경된 파일",
      "프리뷰 시작",
      "수리 시도",
      "프리뷰 응답 점검",
      "정책 검사",
      "에이전트 요약",
      "결정 이력",
    ]);
  });

  it("is there for a build that was not measured, with no live region around it", async () => {
    const pane = await openDetail(null);
    await chooseTab(/^점검/);

    expect(screen.getByRole("heading", { name: "프리뷰 응답 점검" })).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel");
    expect(panel.querySelector("[aria-live]")).toBeNull();
    expect(panel.querySelector("[role=alert]")).toBeNull();
    expect(panel.querySelector("[role=status]")).toBeNull();
    // Nor does the pane as a whole announce anything that did not just happen because of the user.
    expect(pane.querySelector("[aria-live]")).toBeNull();
    expect(pane.querySelector("[role=alert]")).toBeNull();
    expect(pane.querySelector("[role=status]")).toBeNull();
  });
});

describe("the repair note inside the approval console's detail", () => {
  it("sits directly above the smoke check in the 점검 tab, not on the list, when a repair ran", async () => {
    vi.mocked(approvalsApi.get).mockResolvedValue(detail(SMOKE, 2));
    const user = userEvent.setup();
    render(<ApprovalConsolePage />);
    const row = await screen.findByRole("listitem", { name: "휴가 도구 요청 상세 보기" });
    // The list carries no repair count; the note appears only once the detail is open.
    expect(screen.queryByText(/수리 시도/)).not.toBeInTheDocument();
    await user.click(row);
    const pane = await screen.findByRole("region", { name: "요청 상세" });
    // Not in 요약: the note belongs to the checks.
    expect(screen.queryByRole("heading", { name: "수리 시도" })).not.toBeInTheDocument();
    await chooseTab(/^점검/);

    const repair = screen.getByRole("heading", { name: "수리 시도" });
    const smoke = screen.getByRole("heading", { name: "프리뷰 응답 점검" });
    expect(isBefore(repair, smoke)).toBe(true);
    // Directly above: the note's paragraph follows the repair heading inside its card, and the smoke
    // check's card is the very next card after the repair note's.
    const note = repair.nextElementSibling;
    expect(note?.textContent).toContain("2회");
    expect(repair.parentElement?.nextElementSibling).toBe(smoke.parentElement);
    expect(pane.querySelector("[role=alert]")).toBeNull();
    expect(pane.querySelector("[role=status]")).toBeNull();
    expect(pane.querySelector("[aria-live]")).toBeNull();
  });

  it.each([
    ["0", 0],
    ["null", null],
    ["absent", undefined],
  ])("shows nothing when the count is %s", async (_label, value) => {
    const pane = await openDetail(SMOKE, value);
    await chooseTab(/^점검/);

    expect(screen.getByRole("heading", { name: "프리뷰 응답 점검" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "수리 시도" })).not.toBeInTheDocument();
    expect(pane.textContent).not.toContain("다시 시도하게 했습니다");
  });
});

describe("the routes each repair attempt was started for, inside the approval console's detail", () => {
  const STORED = [
    { attempt: 1, routes: [{ path: "/ra", status: 500, outcome: "server_error" }] },
    { attempt: 2, routes: [{ path: "/rb", status: null, outcome: "no_answer" }] },
  ];

  it("sit between the repair note's heading and the smoke check, which stays the shipped attempt's", async () => {
    const pane = await openDetail(SMOKE, 2, STORED);
    await chooseTab(/^점검/);

    const repair = screen.getByRole("heading", { name: "수리 시도" });
    const smoke = screen.getByRole("heading", { name: "프리뷰 응답 점검" });
    const label = screen.getByText("수리 시도 1: 이전 시도에서 실패한 경로");
    const earlier = Array.from(pane.querySelectorAll("code")).find((c) => c.textContent === "/rb");
    expect(earlier).toBeDefined();
    expect(isBefore(repair, label)).toBe(true);
    expect(isBefore(earlier!, smoke)).toBe(true);
    expect(pane.querySelector("[role=alert]")).toBeNull();
    expect(pane.querySelector("[role=status]")).toBeNull();
    expect(pane.querySelector("[aria-live]")).toBeNull();
  });

  it.each([
    ["a count of 1 and null routes", 1, null, true],
    ["a count of 1 and absent routes", 1, undefined, true],
    ["a count of 1 and a malformed value", 1, [{ attempt: 1, routes: "x" }], true],
    ["a count of 0 and routes", 0, STORED, false],
    ["an absent count and routes", undefined, STORED, false],
    ["a null count and routes", null, STORED, false],
  ])("with %s shows no routes", async (_label, count, routes, noteShows) => {
    await openDetail(SMOKE, count, routes);
    await chooseTab(/^점검/);

    expect(screen.queryByText(/이전 시도에서 실패한 경로/)).not.toBeInTheDocument();
    expect(screen.queryByText("/ra", { selector: "code" })).not.toBeInTheDocument();
    expect(screen.queryByText("/rb", { selector: "code" })).not.toBeInTheDocument();
    if (noteShows) {
      expect(screen.getByRole("heading", { name: "수리 시도" })).toBeInTheDocument();
    } else {
      expect(screen.queryByRole("heading", { name: "수리 시도" })).not.toBeInTheDocument();
    }
  });
});
