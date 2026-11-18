// @vitest-environment jsdom
/**
 * (ADR 0021 §Amendment): the approval detail as a pinned header, four tabs and a docked action
 * bar. Scope is structure, keyboard operability, focus and ARIA, which is all jsdom can say: it
 * measures NO layout, so that the content area really scrolls on its own and that the buttons sit
 * inside a 1440x900 viewport are NOT checked here (a real-screen check measured them). What the chips and counts say is `approvalDetailView.test.ts`'s.
 *
 * The api module and the identity hook are mocked; a person clicks the row, as an approver does.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "../api/approvals";
import type { ApprovalRequest, ApprovalRequestSummary } from "../types/api";
import { ApprovalConsolePage } from "./ApprovalConsolePage";

vi.mock("../api/approvals", () => ({
  approvalsApi: { list: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn() },
}));

// The viewer, set per test: an approver who is neither the requester nor the owner by default.
const viewer: { id: string } = { id: "u-approver" };

vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: {
      id: viewer.id,
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

function summary(id: string, projectName: string): ApprovalRequestSummary {
  return {
    id,
    build_id: `b-${id}`,
    project_id: `p-${id}`,
    project_name: projectName,
    requester_id: "u-req",
    requester_name: "최수아",
    risk_tier: "high",
    status: "PENDING",
    required_approvals: 1,
    approvals_so_far: 0,
    flagged_checks: ["secrets_pii", "egress_detection"],
    diff_ref: "forge/survey#12",
    preview_url: null,
    changed_counts: { totals: { added: 600, modified: 38, deleted: 2 }, truncated: true },
    created_at: "2026-10-01T00:00:00Z",
  };
}

/** The demo's 640-file request, with the stored list shorter than the true count. */
function request(id: string, overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    id,
    build_id: `b-${id}`,
    project_id: `p-${id}`,
    requester_id: "u-req",
    project_owner_id: "u-owner",
    policy_verdict_id: "v1",
    risk_tier: "high",
    status: "PENDING",
    required_approvals: 1,
    superseded_by_build_id: null,
    superseded_at: null,
    created_at: "2026-10-01T00:00:00Z",
    decisions: [],
    policy_verdict: {
      id: "v1",
      build_id: `b-${id}`,
      risk_tier: "high",
      created_at: "2026-10-01T00:00:00Z",
      checks: [
        { id: "c1", check_name: "secrets_pii", result: "flag", summary: "1 finding", findings: null },
        { id: "c2", check_name: "egress_detection", result: "flag", summary: "1 finding", findings: null },
        { id: "c3", check_name: "resource_limits", result: "pass", summary: "ok", findings: null },
      ],
    },
    request_text: "설문을 만들고 응답을 모을 수 있는 도구를 만들어줘",
    changed_files: {
      entries: [
        { path: "app/generated/form_001.py", change: "added" },
        { path: "app/services/responses.py", change: "modified" },
      ],
      totals: { added: 600, modified: 38, deleted: 2 },
      truncated: true,
    },
    protected_path_drift: ["Dockerfile", "compose.yaml"],
    smoke_checks: {
      routes: [
        { path: "/", status: 200, outcome: "ok" },
        { path: "/responses", status: 500, outcome: "server_error" },
        { path: "/slow", status: null, outcome: "no_answer" },
      ],
      skipped: 0,
      truncated: false,
    },
    agent_summary: "에이전트가 쓴 본문",
    ...overrides,
  };
}

const AGENT_LABEL = "에이전트 작성 · 플랫폼이 확인하지 않은 내용";
const ROW_A = "휴가 도구 요청 상세 보기";
const ROW_B = "재고 도구 요청 상세 보기";

function mockDetails(byId: Record<string, ApprovalRequest>) {
  vi.mocked(approvalsApi.get).mockImplementation((id: string) => Promise.resolve(byId[id]));
}

beforeEach(() => {
  viewer.id = "u-approver";
  vi.mocked(approvalsApi.list).mockReset().mockResolvedValue([summary("a1", "휴가 도구"), summary("a2", "재고 도구")]);
  vi.mocked(approvalsApi.get).mockReset();
  vi.mocked(approvalsApi.approve).mockReset();
  vi.mocked(approvalsApi.reject).mockReset();
  mockDetails({ a1: request("a1"), a2: request("a2") });
});

/** Mounts the console and opens the request by clicking its row. */
async function openRow(name: string): Promise<HTMLElement> {
  render(<ApprovalConsolePage />);
  return clickRow(name);
}

async function clickRow(name: string): Promise<HTMLElement> {
  await userEvent.click(await screen.findByRole("listitem", { name }));
  return screen.findByRole("region", { name: "요청 상세" });
}

async function chooseTab(name: RegExp | string): Promise<void> {
  await userEvent.click(screen.getByRole("tab", { name }));
}

const selectedTab = () => screen.getAllByRole("tab").filter((tab) => tab.getAttribute("aria-selected") === "true");

describe("the pinned header and its four chips", () => {
  it("shows the id, tier, status, required approvals, project and requester above the tabs", async () => {
    const pane = await openRow(ROW_A);
    const header = screen.getByRole("heading", { name: "요청 상세" }).parentElement!.parentElement!;

    expect(header.textContent).toContain("ID: a1");
    expect(header.textContent).toContain("휴가 도구");
    expect(header.textContent).toContain("위험도: 높음");
    expect(header.textContent).toContain("상태: 승인 대기");
    expect(header.textContent).toContain("필요 승인 수: 1");
    expect(header.textContent).toContain("요청자: 최수아");
    expect(isBefore(header, screen.getByRole("tablist"))).toBe(true);
    expect(pane.contains(header)).toBe(true);
  });

  it("holds four chips of words only: a label and a value, no icon, no control", async () => {
    await openRow(ROW_A);
    const group = screen.getByRole("group", { name: "점검 결과 요약" });

    // Literal text of the four chips of the demo-shaped request (label then value).
    expect(group.textContent).toBe("변경640개 · 원복 2프리뷰2/3 실패정책지적 2에이전트있음 · 미확인");
    expect(group.querySelectorAll("svg, img, i, a, button, input, [role=button], [role=link], [tabindex]")).toHaveLength(0);
    // No mark character standing alone in the group's text.
    for (const text of Array.from(group.querySelectorAll("div")).map((el) => el.textContent ?? "")) {
      expect(text).not.toMatch(/(^|\s)[!?✕×✓✔]($|\s)/);
    }
    expect(group.closest("[role=tabpanel]")).toBeNull();
  });
});

describe("the tabs", () => {
  it("offers 요약, 변경, 점검, 이력 with their counts and opens on 요약 with only that panel mounted", async () => {
    await openRow(ROW_A);

    expect(screen.getByRole("tablist")).toBeTruthy();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "요약",
      "변경 (640)",
      "점검 (4)",
      "이력 (0)",
    ]);
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["요약"]);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(selectedTab()[0].id);
    expect(screen.getByRole("heading", { name: "요청 내용" })).toBeInTheDocument();
    // Nothing of the other tabs is in the document.
    expect(screen.queryByRole("heading", { name: "변경된 파일" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "프리뷰 응답 점검" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "결정 이력" })).toBeNull();
  });

  it("switches by click and mounts only the chosen panel", async () => {
    await openRow(ROW_A);

    await chooseTab(/^변경/);
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["변경 (640)"]);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "변경된 파일" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "요청 내용" })).toBeNull();

    await chooseTab(/^이력/);
    expect(screen.getByRole("heading", { name: "결정 이력" })).toBeInTheDocument();
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  });

  it("switches by the arrow keys the primitive provides, moving focus with the selection", async () => {
    const user = userEvent.setup();
    await openRow(ROW_A);

    screen.getByRole("tab", { name: "요약" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["변경 (640)"]);
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: /^변경/ }));
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["이력 (0)"]);
    await user.keyboard("{ArrowRight}");
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["요약"]);
    await user.keyboard("{ArrowLeft}");
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["이력 (0)"]);
  });

  it("returns to 요약 when another request is chosen, and keeps the tab when the same request is decided on", async () => {
    await openRow(ROW_A);
    await chooseTab(/^점검/);
    expect(screen.getByRole("heading", { name: "프리뷰 응답 점검" })).toBeInTheDocument();

    await clickRow(ROW_B);
    await waitFor(() => expect(screen.getByText("ID: a2")).toBeInTheDocument());
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["요약"]);
    expect(screen.getByRole("heading", { name: "요청 내용" })).toBeInTheDocument();

    // A decision on the open request does not take the approver out of the tab they decided from.
    await chooseTab(/^변경/);
    vi.mocked(approvalsApi.reject).mockResolvedValue(request("a2", { status: "REJECTED" }));
    await userEvent.type(screen.getByLabelText(/반려 사유/), "정책 지적");
    await userEvent.click(screen.getByRole("button", { name: "반려" }));
    await waitFor(() => expect(approvalsApi.reject).toHaveBeenCalledWith("a2", { comment: "정책 지적" }));
    // The answer replaced the open request (its status now reads 반려됨) and the tab stayed.
    expect(await within(screen.getByRole("region", { name: "요청 상세" })).findByText("반려됨")).toBeInTheDocument();
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["변경 (640)"]);
  });
});

describe("the 요약 tab has no link and no control", () => {
  it("is plain text: no anchor, no button, nothing that switches a tab, and the headlines are literal", async () => {
    await openRow(ROW_A);
    const panel = screen.getByRole("tabpanel");

    expect(panel.querySelectorAll("a")).toHaveLength(0);
    expect(panel.querySelectorAll("button, [role=button], [role=link], [role=tab], input, select, textarea")).toHaveLength(0);
    expect(panel.querySelectorAll("[tabindex]")).toHaveLength(0);
    const lines = Array.from(panel.querySelectorAll("li")).map((li) => li.textContent);
    expect(lines).toEqual([
      "변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨",
      "보호 경로 원복: Dockerfile, compose.yaml",
      "프리뷰 응답 점검 호출한 GET 경로 3개 중 2개 실패",
      "정책 검사 검사 3개 중 지적 2 — 비밀·개인정보, 외부 접속",
    ]);
    // Each headline reads once: the counts line is not preceded by a second label.
    expect(panel.textContent?.split("변경 파일 640개").length).toBe(2);
    expect(panel.textContent).not.toContain("변경된 파일");
    expect(screen.getByRole("heading", { name: "PR" }).parentElement?.textContent).toBe("PRforge/survey#12");
    expect(panel.textContent).toContain("점검 탭의 맨 끝에 접혀 있습니다.");
    // The agent's own words are not here: only the notice that they exist.
    expect(panel.textContent).not.toContain("에이전트가 쓴 본문");
    // Nothing in the whole header is a control either.
    const group = screen.getByRole("group", { name: "점검 결과 요약" });
    expect(group.querySelectorAll("a, button, [role=button]")).toHaveLength(0);
  });
});

/** The card a section heading sits in: the project's `Panel` (a `Card`: rounded, bordered). */
function cardOf(heading: HTMLElement): HTMLElement {
  const card = heading.parentElement;
  if (!card) throw new Error("the heading has no card");
  return card;
}

describe("every section is its own card (jsdom cannot see appearance, so structure and heading level only)", () => {
  function sectionHeadings(): HTMLElement[] {
    return within(screen.getByRole("tabpanel")).getAllByRole("heading");
  }

  it("gives 요약 a card each, in order: the request text, the measured facts, the PR, the agent notice", async () => {
    await openRow(ROW_A);
    const panel = screen.getByRole("tabpanel");

    const headings = sectionHeadings();
    expect(headings.map((h) => h.textContent)).toEqual(["요청 내용", "측정된 사실 · 한눈에", "PR", "에이전트 요약"]);
    for (const heading of headings) {
      expect(heading.tagName, heading.textContent ?? "").toBe("H3");
      const card = cardOf(heading);
      expect(card.className, heading.textContent ?? "").toContain("rounded-card");
      expect(card.className, heading.textContent ?? "").toContain("border");
      // The accent bar is part of the heading and is decoration.
      expect(heading.querySelector("[aria-hidden=true]"), heading.textContent ?? "").not.toBeNull();
    }
    // One card per section, and nothing else sits loose in the panel.
    expect(Array.from(panel.children)).toEqual(headings.map(cardOf));
  });

  it("leaves the PR card out when the row has no PR, and the notice card out when there is no agent summary", async () => {
    vi.mocked(approvalsApi.list).mockResolvedValue([{ ...summary("a1", "휴가 도구"), diff_ref: null }, summary("a2", "재고 도구")]);
    mockDetails({ a1: request("a1", { agent_summary: null }), a2: request("a2") });
    await openRow(ROW_A);

    expect(sectionHeadings().map((h) => h.textContent)).toEqual(["요청 내용", "측정된 사실 · 한눈에"]);
  });

  it("gives 변경 one card, whose paths stay inside it", async () => {
    await openRow(ROW_A);
    await chooseTab(/^변경/);

    const headings = sectionHeadings();
    expect(headings.map((h) => h.textContent)).toEqual(["변경된 파일"]);
    expect(headings[0].tagName).toBe("H3");
    expect(cardOf(headings[0]).className).toContain("rounded-card");
    expect(cardOf(headings[0]).querySelectorAll("details")).toHaveLength(3);
    expect(screen.getByRole("tabpanel").children).toHaveLength(1);
  });

  it("gives 점검 a card per section in the spec order, and the agent's words last in their own dashed box", async () => {
    mockDetails({
      a1: request("a1", { preview_start_failure: "UNEXPECTED", repair_attempts: 2 }),
      a2: request("a2"),
    });
    await openRow(ROW_A);
    await chooseTab(/^점검/);
    const panel = screen.getByRole("tabpanel");

    const headings = sectionHeadings();
    expect(headings.map((h) => h.textContent)).toEqual([
      "프리뷰 시작",
      "수리 시도",
      "프리뷰 응답 점검",
      "정책 검사 (작업공간 전체 기준)",
    ]);
    for (const heading of headings) {
      expect(heading.tagName, heading.textContent ?? "").toBe("H3");
      expect(cardOf(heading).className, heading.textContent ?? "").toContain("rounded-card");
    }
    // The agent summary is found by its own label, never by position, and is the panel's last child.
    const agent = screen.getByText(AGENT_LABEL).closest("details");
    expect(agent).not.toBeNull();
    expect(agent!.className).toContain("border-dashed");
    expect(agent!.className).toContain("rounded-card");
    expect(panel.lastElementChild).toBe(agent);
    expect(Array.from(panel.children)).toEqual([...headings.map(cardOf), agent]);
  });

  it("gives 이력 one card, with an empty-state sentence when there are no decisions", async () => {
    await openRow(ROW_A);
    await chooseTab(/^이력/);

    const headings = sectionHeadings();
    expect(headings.map((h) => h.textContent)).toEqual(["결정 이력"]);
    expect(cardOf(headings[0]).className).toContain("rounded-card");
    expect(cardOf(headings[0]).textContent).toContain("아직 기록된 결정이 없습니다.");
  });
});

describe("the changed files stay inert text", () => {
  it("holds no link and no button in the groups, and clicking a path changes nothing", async () => {
    await openRow(ROW_A);
    await chooseTab(/^변경/);
    const panel = screen.getByRole("tabpanel");
    const groups = Array.from(panel.querySelectorAll("details"));
    expect(groups.map((g) => g.querySelector("summary")?.textContent)).toEqual(["추가 600개", "수정 38개", "삭제 2개"]);

    for (const group of groups) {
      expect(group.querySelectorAll("a, button, [role=button], [role=link]")).toHaveLength(0);
    }
    const before = panel.innerHTML;
    const path = screen.getByText("app/services/responses.py");
    expect(path.tagName).toBe("CODE");
    await userEvent.click(path);
    expect(panel.innerHTML).toBe(before);
    expect(selectedTab().map((tab) => tab.textContent)).toEqual(["변경 (640)"]);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  });
});

describe("the docked action bar", () => {
  it("is outside every tab panel and shows the same controls and approvals line in all four tabs", async () => {
    await openRow(ROW_A);

    for (const name of [/^요약/, /^변경/, /^점검/, /^이력/]) {
      await chooseTab(name);
      const bar = screen.getByRole("group", { name: "결정" });
      expect(bar.closest("[role=tabpanel]"), String(name)).toBeNull();
      expect(screen.getByRole("tabpanel").contains(bar), String(name)).toBe(false);
      expect(within(bar).getByRole("button", { name: "승인" })).toBeInTheDocument();
      expect(within(bar).getByRole("button", { name: "반려" })).toBeInTheDocument();
      expect(within(bar).getByLabelText(/반려 사유/)).toBeInTheDocument();
      expect(bar.textContent).toContain("필요 승인 수 1 · 현재 0");
    }
  });

  it("approves from any tab through the same handler", async () => {
    vi.mocked(approvalsApi.approve).mockResolvedValue(request("a1", { status: "APPROVED" }));
    await openRow(ROW_A);
    await chooseTab(/^점검/);

    await userEvent.click(screen.getByRole("button", { name: "승인" }));

    await waitFor(() => expect(approvalsApi.approve).toHaveBeenCalledWith("a1"));
    expect(approvalsApi.reject).not.toHaveBeenCalled();
  });

  it("refuses a rejection without a reason in an announced message inside the bar, which a tab change does not swallow", async () => {
    await openRow(ROW_A);
    expect(screen.queryByRole("alert")).toBeNull();
    await chooseTab(/^변경/);

    await userEvent.click(screen.getByRole("button", { name: "반려" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("반려 사유(코멘트)를 입력해야 합니다.");
    expect(approvalsApi.reject).not.toHaveBeenCalled();
    const bar = screen.getByRole("group", { name: "결정" });
    expect(bar.contains(alert)).toBe(true);
    expect(alert.closest("[role=tabpanel]")).toBeNull();
    // The message outlives a tab change.
    await chooseTab(/^이력/);
    expect(screen.getByRole("alert")).toBe(alert);
  });

  it("announces a failed approval inside the bar and nowhere else", async () => {
    vi.mocked(approvalsApi.approve).mockRejectedValue(new Error("boom"));
    await openRow(ROW_A);
    await chooseTab(/^점검/);

    await userEvent.click(screen.getByRole("button", { name: "승인" }));

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0].textContent).toBe("승인에 실패했습니다.");
    expect(screen.getByRole("group", { name: "결정" }).contains(alerts[0])).toBe(true);
    expect(alerts[0].closest("[role=tabpanel]")).toBeNull();
  });

  it("shows the refusal instead of the buttons for the requester's own request", async () => {
    viewer.id = "u-req";
    await openRow(ROW_A);

    const bar = screen.getByRole("group", { name: "결정" });
    expect(within(bar).queryByRole("button")).toBeNull();
    expect(within(bar).queryByLabelText(/반려 사유/)).toBeNull();
    expect(bar.textContent).toContain("본인이 요청했거나 본인이 소유한 프로젝트의 요청은 직접 승인하거나 반려할 수 없습니다.");
    expect(bar.textContent).toContain("필요 승인 수 1 · 현재 0");
    await chooseTab(/^점검/);
    expect(within(screen.getByRole("group", { name: "결정" })).queryByRole("button")).toBeNull();
    // A standing fact about the request, not something that just happened.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers nothing for a request that is no longer pending, and says how many approvals it has", async () => {
    mockDetails({
      a1: request("a1", {
        status: "APPROVED",
        decisions: [{ id: "d1", actor_id: "u-other", decision: "approve", comment: null, created_at: "2026-10-02T00:00:00Z" }],
      }),
    });
    await openRow(ROW_A);

    const bar = screen.getByRole("group", { name: "결정" });
    expect(within(bar).queryByRole("button")).toBeNull();
    expect(bar.textContent).toBe("필요 승인 수 1 · 현재 1");
  });

  it("offers only the rejection to someone who already approved, with the reason why not approving", async () => {
    mockDetails({
      a1: request("a1", {
        decisions: [{ id: "d1", actor_id: "u-approver", decision: "approve", comment: null, created_at: "2026-10-02T00:00:00Z" }],
      }),
    });
    await openRow(ROW_A);

    const bar = screen.getByRole("group", { name: "결정" });
    expect(within(bar).queryByRole("button", { name: "승인" })).toBeNull();
    expect(within(bar).getByRole("button", { name: "반려" })).toBeInTheDocument();
    expect(bar.textContent).toContain("이미 이 요청을 승인했습니다.");
  });
});

describe("the pane's structure: jsdom cannot measure layout, so only the order of the parts is pinned", () => {
  it("is header, tab strip, a named focusable scroll region holding the active panel, then the action bar", async () => {
    const pane = await openRow(ROW_A);
    await chooseTab(/^점검/);

    const parts = Array.from(pane.children);
    const region = screen.getByRole("region", { name: "요청 상세 내용" });
    const bar = screen.getByRole("group", { name: "결정" });
    const header = screen.getByRole("heading", { name: "요청 상세" }).parentElement!.parentElement!;
    expect(parts).toEqual([header, screen.getByRole("tablist"), region, bar]);

    expect(region.tabIndex).toBe(0);
    expect(region.contains(screen.getByRole("tabpanel"))).toBe(true);
    // The panel is not a second keyboard stop inside the focusable region.
    expect(screen.getByRole("tabpanel").getAttribute("tabindex")).toBeNull();
    // Neither the header nor the bar is inside the scrolling region; the tab strip is not either.
    expect(region.contains(header)).toBe(false);
    expect(region.contains(bar)).toBe(false);
    expect(region.contains(screen.getByRole("tablist"))).toBe(false);
  });

  it("can be reached by keyboard: the region takes focus", async () => {
    await openRow(ROW_A);
    const region = screen.getByRole("region", { name: "요청 상세 내용" });

    region.focus();

    expect(document.activeElement).toBe(region);
  });
});

function isBefore(first: Element, second: Element): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
}
