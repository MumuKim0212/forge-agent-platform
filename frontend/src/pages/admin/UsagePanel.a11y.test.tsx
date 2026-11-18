// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30: the quota controls of the usage tab are named per row.
 * Before, every input was `월 한도(USD)` and every button `적용`, so a screen reader's list of
 * controls, or a voice command, could not tell which project's or user's quota a control changed.
 * Scope is the accessible name only: which control belongs to whom, not the copy around it.
 *
 * The api modules and the identity hook are mocked; the cost dashboard under the table is stubbed
 * out (it has its own loader and nothing to say about these controls).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { projectsApi } from "../../api/projects";
import { usageApi } from "../../api/usage";
import { usersApi } from "../../api/users";
import type { PlatformUsage, Project, SubjectUsage, UsageReport, User } from "../../types/api";
import { UsagePanel } from "./UsagePanel";

vi.mock("../../api/usage", () => ({
  usageApi: { summary: vi.fn(), projects: vi.fn(), users: vi.fn(), setProjectQuota: vi.fn(), setUserQuota: vi.fn() },
}));
vi.mock("../../api/projects", () => ({ projectsApi: { list: vi.fn() } }));
vi.mock("../../api/users", () => ({ usersApi: { list: vi.fn() } }));
vi.mock("./CostDashboardSection", () => ({ CostDashboardSection: () => null }));
vi.mock("../../context/IdentityContext", () => ({
  useIdentity: () => ({ currentUser: { id: "u-admin", name: "관리자", role: "admin" } }),
}));

const WINDOW = {
  month: "2026-10",
  timezone: "Asia/Seoul",
  starts_at: "2026-10-01T00:00:00+09:00",
  ends_at: "2026-11-01T00:00:00+09:00",
};

function line(kind: string, subjectId: string | null): SubjectUsage {
  return {
    kind,
    subject_id: subjectId,
    spend_usd: 1.5,
    build_count: 2,
    unmeasured_build_count: 0,
    effective_limit_usd: subjectId === null ? null : 10,
    limit_source: subjectId === null ? null : "default",
    in_breach: false,
  };
}

const SUMMARY: PlatformUsage = {
  window: WINDOW,
  total_spend_usd: 3,
  unattributed_spend_usd: 0,
  build_count: 4,
  unmeasured_build_count: 0,
  default_limit_usd: 10,
  projects_in_breach: [],
  users_in_breach: [],
};

beforeEach(() => {
  vi.mocked(usageApi.summary).mockReset().mockResolvedValue(SUMMARY);
  vi.mocked(usageApi.projects)
    .mockReset()
    .mockResolvedValue({
      window: WINDOW,
      subjects: [line("project", "p-a"), line("project", "p-b"), line("project", null)],
    } as UsageReport);
  vi.mocked(usageApi.users)
    .mockReset()
    .mockResolvedValue({ window: WINDOW, subjects: [line("user", "u-1")] } as UsageReport);
  vi.mocked(usageApi.setProjectQuota).mockReset();
  vi.mocked(projectsApi.list)
    .mockReset()
    .mockResolvedValue([
      { id: "p-a", name: "알파" },
      { id: "p-b", name: "베타" },
    ] as Project[]);
  vi.mocked(usersApi.list)
    .mockReset()
    .mockResolvedValue([{ id: "u-1", name: "홍길동" }] as User[]);
});

describe("UsagePanel: every quota control is named for its row", () => {
  it("names each limit input for its subject, and no two share a name", async () => {
    render(<UsagePanel />);

    expect(await screen.findByRole("textbox", { name: "알파 월 한도(USD)" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "베타 월 한도(USD)" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "홍길동 월 한도(USD)" })).toBeInTheDocument();

    const names = screen.getAllByRole("textbox").map((box) => box.getAttribute("aria-label"));
    expect(names).toHaveLength(3);
    expect(new Set(names).size).toBe(names.length);
  });

  it("names each apply button for its subject, and the name still contains the visible word", async () => {
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    for (const name of ["알파 적용", "베타 적용", "홍길동 적용"]) {
      expect(screen.getByRole("button", { name }), name).toHaveTextContent("적용");
    }
    const applyNames = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label"))
      .filter((label): label is string => label !== null && label.endsWith("적용"));
    expect(applyNames).toHaveLength(3);
    expect(new Set(applyNames).size).toBe(applyNames.length);
  });

  it("keeps the name and the visible text in step while a quota is being saved", async () => {
    vi.mocked(usageApi.setProjectQuota).mockReturnValue(new Promise(() => {}));
    render(<UsagePanel />);
    fireEvent.click(await screen.findByRole("button", { name: "알파 적용" }));

    const saving = await screen.findByRole("button", { name: "알파 저장 중..." });
    expect(saving).toHaveTextContent("저장 중...");
    // The other rows keep their own names.
    expect(screen.getByRole("button", { name: "베타 적용" })).toBeInTheDocument();
  });

  it("gives the unattributed line no quota control to name", async () => {
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    const unattributed = screen.getAllByRole("row").find((row) => row.textContent?.includes("미귀속"));
    expect(unattributed).toBeDefined();
    expect(within(unattributed as HTMLElement).queryByRole("button")).toBeNull();
    expect(within(unattributed as HTMLElement).queryByRole("textbox")).toBeNull();
  });
});

describe("UsagePanel: trimmed copy", () => {
  // Literals on purpose: the sentence is the requirement, so it is not read back from QUOTA_WORDING.
  const SHORT_NOTE = "프로젝트별 합계에만 포함됩니다.";

  it("says the short note at the 미귀속 지출 tile and at the 미귀속 row, and nothing of the old sentence", async () => {
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    const notes = screen.getAllByText(SHORT_NOTE);
    expect(notes).toHaveLength(2);
    const inRow = notes.filter((el) => el.closest("tr") !== null);
    expect(inRow).toHaveLength(1);
    expect(inRow[0].closest("tr")?.textContent).toContain("미귀속 지출");
    // The other one is the 플랫폼 합계 tile, outside every table row.
    expect(notes.filter((el) => el.closest("tr") === null)).toHaveLength(1);
    expect(document.body.textContent).not.toContain("요청자를 특정할 수 없는");
  });

  it("shows a 0 override as $0.00 with 개별 지정 and the breach status, without the old zero-override sentence", async () => {
    vi.mocked(usageApi.projects).mockResolvedValue({
      window: WINDOW,
      subjects: [
        { ...line("project", "p-a"), spend_usd: 0.5, effective_limit_usd: 0, limit_source: "override", in_breach: true },
      ],
    } as UsageReport);
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    const row = screen.getAllByRole("row").find((r) => r.textContent?.includes("알파")) as HTMLElement;
    expect(row).toBeDefined();
    expect(row.textContent).toContain("$0.00");
    expect(row.textContent).toContain("개별 지정");
    expect(row.textContent).toContain("한도 초과 (경고)");
    expect(document.body.textContent).not.toContain("즉시 초과로 표시됩니다");
  });
});

describe("UsagePanel: layout trim", () => {
  // jsdom measures no layout, so the month label is checked by the class that forbids wrapping,
  // not by how it renders; the breach tile by its two block lines, not by where they break.
  function breachTileValue(): HTMLElement {
    const label = screen.getAllByText("한도 초과 (경고)").find((el) => el.nextElementSibling !== null);
    expect(label).toBeDefined();
    return (label as HTMLElement).nextElementSibling as HTMLElement;
  }

  it("keeps the month label on one line", async () => {
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    expect(screen.getByText("조회 월")).toHaveClass("whitespace-nowrap");
  });

  it("sizes the month select to its content, not full width", async () => {
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    // The label and the select are one control; jsdom measures no layout, so the assertion is on
    // the width class: the select carries the content-sized one and not the full-width one.
    const select = screen.getByRole("combobox", { name: "조회 월" });
    expect(select).toHaveClass("w-auto");
    expect(select).not.toHaveClass("w-full");
  });

  it.each([
    [2, 0, "프로젝트 2", "사용자 0"],
    [0, 3, "프로젝트 0", "사용자 3"],
    [0, 0, "프로젝트 0", "사용자 0"],
  ])("renders the breach counts (%i, %i) as two block lines in order", async (projects, users, first, second) => {
    vi.mocked(usageApi.summary).mockResolvedValue({
      ...SUMMARY,
      projects_in_breach: Array.from({ length: projects }, (_, i) => `p-${i}`),
      users_in_breach: Array.from({ length: users }, (_, i) => `u-${i}`),
    });
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    const value = breachTileValue();
    const lines = Array.from(value.children).filter((el) => el.tagName === "DIV");
    expect(lines.map((el) => el.textContent)).toEqual([first, second]);
    // Read aloud as one phrase, in order.
    expect(value.textContent).toBe(`${first}, ${second}`);
  });

  it("has no breach note under the tile and no explanation on a breach badge", async () => {
    vi.mocked(usageApi.summary).mockResolvedValue({ ...SUMMARY, projects_in_breach: ["p-a"] });
    vi.mocked(usageApi.projects).mockResolvedValue({
      window: WINDOW,
      subjects: [{ ...line("project", "p-a"), spend_usd: 20, in_breach: true }],
    } as UsageReport);
    render(<UsagePanel />);
    await screen.findByRole("textbox", { name: "알파 월 한도(USD)" });

    expect(document.body.textContent).not.toContain("쿼터는 경고만");
    expect(document.body.textContent).not.toContain("플랫폼은 아무것도 되돌리지 않습니다");
    // The row's badge is still there, as a warning marker without a tooltip sentence.
    const badge = screen.getAllByText("한도 초과 (경고)").find((el) => el.closest("tr") !== null) as HTMLElement;
    expect(badge).toBeDefined();
    expect(badge.getAttribute("title")).toBeNull();
    // The tile's note slot is gone: label and value are the tile's only children.
    expect(breachTileValue().parentElement?.children).toHaveLength(2);
  });
});
