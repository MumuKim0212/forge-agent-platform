// @vitest-environment jsdom
/**
 * (ADR 0021 §Amendment): what tells an approver the detail exists and what a request's tier is
 * called. The legend's wording is `approvalDisplay.test.ts`'s; this file owns where it sits, that the row
 * cue does not change the row's accessible name, and that the empty detail column is plain text.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "../api/approvals";
import type { ApprovalRequest, ApprovalRequestSummary } from "../types/api";
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
  risk_tier: "medium",
  status: "PENDING",
  required_approvals: 1,
  approvals_so_far: 0,
  flagged_checks: [],
  diff_ref: null,
  preview_url: null,
  changed_counts: null,
  created_at: "2026-10-01T00:00:00Z",
};

const DETAIL: ApprovalRequest = {
  id: "a1",
  build_id: "b1",
  project_id: "p1",
  requester_id: "u-req",
  project_owner_id: "u-owner",
  policy_verdict_id: null,
  risk_tier: "medium",
  status: "PENDING",
  required_approvals: 1,
  superseded_by_build_id: null,
  superseded_at: null,
  created_at: "2026-10-01T00:00:00Z",
  decisions: [],
  policy_verdict: null,
  request_text: "휴가 신청 화면을 만들어 주세요",
  changed_files: null,
  protected_path_drift: [],
  smoke_checks: null,
  agent_summary: null,
};

const ROW_NAME = "휴가 도구 요청 상세 보기";
const PLACEHOLDER = "목록에서 요청을 선택하면 상세가 여기에 표시됩니다.";

beforeEach(() => {
  vi.mocked(approvalsApi.list).mockReset().mockResolvedValue([SUMMARY]);
  vi.mocked(approvalsApi.get).mockReset().mockResolvedValue(DETAIL);
});

describe("the risk legend", () => {
  it("is a static key before the first list heading: the real badges and a description each, no live region", async () => {
    render(<ApprovalConsolePage />);
    await screen.findByRole("listitem", { name: ROW_NAME });

    const lead = screen.getByText("위험도 — 정책 검사 결과");
    const firstHeading = screen.getByRole("heading", { name: /^대기 중/ });
    expect(lead.compareDocumentPosition(firstHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const key = lead.parentElement;
    if (!key) throw new Error("the legend has no container");
    expect(key.closest("[role=alert], [role=status], [aria-live]")).toBeNull();
    expect(key.querySelector("[role=alert], [role=status], [aria-live], button, a, input")).toBeNull();

    // Three items, each led by the level's badge word and followed by its meaning.
    const items = Array.from(key.querySelectorAll("li")).map((li) => li.textContent);
    expect(items).toEqual([
      "낮음지적 없음, 자동 승인",
      "중간의존성 취약점, 자원 한도, 템플릿 규칙, DB 구조 파괴 중 하나가 지적됨",
      "높음비밀·개인정보, 외부 접속 중 하나가 지적됨",
    ]);
    expect(key.textContent).toContain("중간·높음은 승인자 1명의 확인이 필요합니다.");
    expect(key.textContent).not.toMatch(/[a-z]+_[a-z]+/);
  });

  it("calls the value 위험도 in the detail, as the legend does", async () => {
    const user = userEvent.setup();
    render(<ApprovalConsolePage />);
    await user.click(await screen.findByRole("listitem", { name: ROW_NAME }));
    const pane = await screen.findByRole("region", { name: "요청 상세" });

    expect(pane.textContent).toContain("위험도:");
    expect(pane.textContent).not.toContain("리스크 티어");
  });
});

describe("the row's cue that it opens the detail", () => {
  it("is visible text hidden from assistive technology, so the row's name is unchanged", async () => {
    render(<ApprovalConsolePage />);
    const row = await screen.findByRole("listitem", { name: ROW_NAME });

    const cue = row.querySelector("[aria-hidden=true]");
    expect(cue?.textContent).toBe("상세 ›");
    expect(row.getAttribute("aria-label")).toBe(ROW_NAME);
  });

  it("still opens on Enter and not on Space", async () => {
    const user = userEvent.setup();
    render(<ApprovalConsolePage />);
    const row = await screen.findByRole("listitem", { name: ROW_NAME });

    row.focus();
    await user.keyboard(" ");
    expect(approvalsApi.get).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { name: "요청 상세" })).toBeInTheDocument();
  });
});

describe("the detail column before a request is chosen", () => {
  it("shows a hint with no heading and no live region, and the detail replaces it", async () => {
    const user = userEvent.setup();
    const { container } = render(<ApprovalConsolePage />);
    await screen.findByRole("listitem", { name: ROW_NAME });

    const hint = screen.getByText(PLACEHOLDER);
    expect(screen.queryByRole("heading", { name: "요청 상세" })).toBeNull();
    expect(hint.parentElement?.querySelector("h1, h2, h3, h4, h5, h6")).toBeNull();
    expect(hint.closest("[role=alert], [role=status], [aria-live]")).toBeNull();
    expect(container.querySelector("[role=alert], [role=status], [aria-live]")).toBeNull();

    await user.click(screen.getByRole("listitem", { name: ROW_NAME }));
    expect(await screen.findByRole("heading", { name: "요청 상세" })).toBeInTheDocument();
    expect(screen.queryByText(PLACEHOLDER)).toBeNull();
  });
});
