// @vitest-environment jsdom
/**
 * The usage tab's heading outline. The console's tab panel opens with a visually-hidden `<h2>` for
 * the tab label (`AdminConsolePage`); this mounts `UsagePanel` and the real `CostDashboardSection` beneath
 * such a heading and reads the headings a screen reader would list. Each detail section is a level-3 heading,
 * the cost dashboard's blocks are level 4 under it, and no level is skipped going down.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { projectsApi } from "../../api/projects";
import { usageApi } from "../../api/usage";
import { usersApi } from "../../api/users";
import type { CostDashboard, PlatformUsage, Project, UsageReport, User } from "../../types/api";
import { UsagePanel } from "./UsagePanel";

vi.mock("../../api/usage", () => ({
  usageApi: {
    summary: vi.fn(),
    projects: vi.fn(),
    users: vi.fn(),
    dashboard: vi.fn(),
    setProjectQuota: vi.fn(),
    setUserQuota: vi.fn(),
  },
}));
vi.mock("../../api/projects", () => ({ projectsApi: { list: vi.fn() } }));
vi.mock("../../api/users", () => ({ usersApi: { list: vi.fn() } }));
vi.mock("../../context/IdentityContext", () => ({
  useIdentity: () => ({ currentUser: { id: "u-admin", name: "관리자", role: "admin" } }),
}));

const WINDOW = {
  month: "2026-10",
  timezone: "Asia/Seoul",
  starts_at: "2026-10-01T00:00:00+09:00",
  ends_at: "2026-11-01T00:00:00+09:00",
};

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

const DASHBOARD: CostDashboard = {
  window: WINDOW,
  limit: 10,
  days: [{ day: "2026-10-01", spend_usd: 3, build_count: 1, unmeasured_build_count: 0, failed_spend_usd: 0 }],
  outcomes: [{ status: "SUCCEEDED", spend_usd: 3, build_count: 1, unmeasured_build_count: 0, failure_reasons: [] }],
  expensive_builds: [],
  metering_caveat: { date: "2026-10-01", window_starts_before: false },
};

beforeEach(() => {
  vi.mocked(usageApi.summary).mockReset().mockResolvedValue(SUMMARY);
  vi.mocked(usageApi.projects)
    .mockReset()
    .mockResolvedValue({ window: WINDOW, subjects: [] } as UsageReport);
  vi.mocked(usageApi.users)
    .mockReset()
    .mockResolvedValue({ window: WINDOW, subjects: [] } as UsageReport);
  vi.mocked(usageApi.dashboard).mockReset().mockResolvedValue(DASHBOARD);
  vi.mocked(projectsApi.list).mockReset().mockResolvedValue([] as Project[]);
  vi.mocked(usersApi.list).mockReset().mockResolvedValue([] as User[]);
});

/** Each heading as `[level, first line of its text]`, in document order. */
function outline(): Array<[number, string]> {
  return screen.getAllByRole("heading").map((h) => [Number(h.tagName.slice(1)), h.textContent ?? ""]);
}

describe("UsagePanel heading outline", () => {
  it("puts every detail section at level 3 under the tab's level 2, the dashboard blocks at level 4, skipping none", async () => {
    render(
      <MemoryRouter>
        <h2>사용량 · 쿼터</h2>
        <UsagePanel />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: /^플랫폼 합계/ });
    await screen.findByRole("heading", { name: /^프로젝트별/ });
    await screen.findByRole("heading", { name: /^사용자별/ });
    await screen.findAllByRole("heading", { level: 4 });

    const levels = outline().map(([level]) => level);
    expect(levels[0]).toBe(2);
    expect(levels.filter((l) => l === 2)).toHaveLength(1);
    // No heading is more than one level deeper than the one before it.
    levels.slice(1).forEach((level, i) => expect(level - levels[i], `heading ${i + 1}`).toBeLessThanOrEqual(1));

    const byName = (prefix: string) => outline().find(([, text]) => text.startsWith(prefix));
    expect(byName("플랫폼 합계")?.[0]).toBe(3);
    expect(byName("프로젝트별")?.[0]).toBe(3);
    expect(byName("사용자별")?.[0]).toBe(3);
    expect(byName("비용 대시보드")?.[0]).toBe(3);
    expect(outline().filter(([level]) => level === 4)).toHaveLength(3);
  });
});
