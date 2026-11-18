// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment: ARIA only. The chart is an image to assistive technology,
 * so its figures must be reachable as text; the metering caveat is a standing fact, so it
 * must not be a live region; and a project name's invisible characters stay visible.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { CostDashboard } from "../../types/api";
import { CostDashboardView } from "./CostDashboardView";

function dashboard(startsBefore: boolean): CostDashboard {
  return {
    window: { month: "2026-07", timezone: "Asia/Seoul", starts_at: "2026-06-30T15:00:00Z", ends_at: "2026-07-31T15:00:00Z" },
    limit: 10,
    days: [
      { day: "2026-07-01", spend_usd: 8, build_count: 2, unmeasured_build_count: 0, failed_spend_usd: 5 },
      { day: "2026-07-02", spend_usd: 0, build_count: 1, unmeasured_build_count: 1, failed_spend_usd: 0 },
      { day: "2026-07-03", spend_usd: 0, build_count: 0, unmeasured_build_count: 0, failed_spend_usd: 0 },
    ],
    outcomes: [
      { status: "SUCCEEDED", spend_usd: 3, build_count: 1, unmeasured_build_count: 1, failure_reasons: [] },
      {
        status: "FAILED",
        spend_usd: 5,
        build_count: 1,
        unmeasured_build_count: 0,
        failure_reasons: [{ failure_reason: "BUDGET_EXCEEDED", spend_usd: 5, build_count: 1, unmeasured_build_count: 0 }],
      },
    ],
    expensive_builds: [
      {
        build_id: "build-1",
        project_id: "proj-1",
        type: "create",
        status: "FAILED",
        failure_reason: "BUDGET_EXCEEDED",
        cost_usd: 5,
        cost_tokens: 100,
        ended_at: "2026-07-01T03:00:00Z",
        requester_id: null,
      },
    ],
    metering_caveat: { date: "2026-10-01", window_starts_before: startsBefore },
  };
}

function mount(startsBefore: boolean) {
  return render(
    <MemoryRouter>
      <CostDashboardView
        dashboard={dashboard(startsBefore)}
        projectNames={{ "proj-1": `report${String.fromCodePoint(0x202e)}txt` }}
        userNames={{}}
      />
    </MemoryRouter>,
  );
}

describe("CostDashboardView accessibility", () => {
  it("exposes the chart as a named, described image and its figures as a table", () => {
    mount(false);

    const chart = screen.getByRole("img", { name: "일별 지출" });
    expect(chart).toHaveAccessibleDescription(/아래 표/);

    const table = screen.getByRole("table", { name: "일별 지출" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4); // header + 3 days
    expect(within(rows[1]).getByText("$8.00")).toBeInTheDocument();
    expect(within(rows[1]).getByText("$5.00")).toBeInTheDocument();
    // Unmeasured is not rendered as a measured zero, and an empty day is not $0.00.
    expect(within(rows[2]).getByText("미측정 · 빌드 1건")).toBeInTheDocument();
    expect(within(rows[3]).getByText("빌드 없음")).toBeInTheDocument();
    // (the chart's own axis label "$0.00" is a scale, not a day's spend: look in the table.)
    expect(within(table).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("keeps the table behind a native disclosure", () => {
    mount(false);

    expect(screen.getByText("일별 수치 표로 보기").tagName).toBe("SUMMARY");
  });

  it("shows the metering caveat as a static note, not a live region", () => {
    mount(true);

    expect(screen.getByText(/2026-10-01 이전에 종료된 빌드/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(document.querySelector("[aria-live]")).toBeNull();
  });

  it("shows no caveat when the window starts on or after the date", () => {
    mount(false);

    expect(screen.queryByText(/에이전트 엔진이 직접 보고/)).not.toBeInTheDocument();
  });

  it("links each expensive build to its project, with the name's invisible character visible", () => {
    mount(false);

    const link = screen.getByRole("link", { name: "report⟨U+202E⟩txt" });
    expect(link).toHaveAttribute("href", "/projects/proj-1");
  });
});
