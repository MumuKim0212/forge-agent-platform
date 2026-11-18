// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. Scope is ARIA and what a screen reader is told: every
 * project's status is read out as words in a table with named columns, never as colour alone, and
 * the one thing the health view links to is the project's own page. Not layout, not copy beyond
 * the status labels themselves, and not how it looks (jsdom has no layout; the denser arrangement
 * and the narrow-screen reflow were checked in a browser, see the task's Result).
 *
 * The health read is the only thing the panel needs mocked; the router is for its links.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { consoleApi } from "../../api/console";
import type { Deployment, DeploymentHealth } from "../../types/api";
import { DeploymentHealthPanel } from "./DeploymentHealthPanel";

vi.mock("../../api/console", () => ({
  consoleApi: { deploymentHealth: vi.fn(), configuration: vi.fn() },
}));

const healthMock = vi.mocked(consoleApi.deploymentHealth);

function deployment(id: string, projectId: string, status: string, failureReason: string | null = null): Deployment {
  return {
    id,
    project_id: projectId,
    build_id: `b-${id}`,
    confirmed_by_id: "u1",
    rollback_of: null,
    status,
    failure_reason: failureReason,
    created_at: "2026-11-01T09:00:00Z",
    started_at: null,
    ended_at: null,
  };
}

const SERVING_ROW = deployment("d-a", "p-serving", "ACTIVE");
const FAILED_ROW = deployment("d-f", "p-failed", "FAILED", "START_FAILED");
const OLD_FAILED_ROW = deployment("d-g", "p-serving", "FAILED", "HEALTH_TIMEOUT");

function fixture(overrides: Partial<DeploymentHealth> = {}): DeploymentHealth {
  return {
    projects: [
      { project_id: "p-serving", project_name: "휴가 도구", active_deployment: SERVING_ROW, latest_deployment: OLD_FAILED_ROW },
      { project_id: "p-failed", project_name: "명함 신청", active_deployment: null, latest_deployment: FAILED_ROW },
      { project_id: "p-new", project_name: "새 서비스", active_deployment: null, latest_deployment: null },
    ],
    recent_failures: [FAILED_ROW, OLD_FAILED_ROW],
    recent_failures_limit: 20,
    recent_failures_truncated: false,
    ...overrides,
  };
}

async function renderPanel(health: DeploymentHealth) {
  healthMock.mockResolvedValue(health);
  render(
    <MemoryRouter>
      <DeploymentHealthPanel />
    </MemoryRouter>,
  );
  return screen.findByRole("table");
}

function rowFor(table: HTMLElement, projectName: string) {
  return within(table)
    .getAllByRole("row")
    .find((row) => within(row).queryByText(projectName) !== null) as HTMLElement;
}

describe("DeploymentHealthPanel — status is words, in a labelled table", () => {
  beforeEach(() => healthMock.mockReset());

  it("is a table with the four named columns", async () => {
    const table = await renderPanel(fixture());

    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "프로젝트",
      "상태",
      "운영 중 배포",
      "최신 배포",
    ]);
  });

  it("reads each project's state as text, so no row relies on a colour", async () => {
    const table = await renderPanel(fixture());

    expect(within(rowFor(table, "휴가 도구")).getByText("운영중 · 마지막 배포 시도 실패")).toBeTruthy();
    expect(within(rowFor(table, "명함 신청")).getByText("마지막 배포 실패 (운영 버전 없음)")).toBeTruthy();
    expect(within(rowFor(table, "새 서비스")).getByText("배포 이력 없음")).toBeTruthy();
  });

  it("states the failure reason in words, in the row and in the recent-failures list", async () => {
    const table = await renderPanel(fixture());

    expect(within(rowFor(table, "명함 신청")).getByText("실패 사유: 서비스 시작 실패")).toBeTruthy();
    const failures = screen.getByRole("heading", { name: /^최근 배포 실패/ }).parentElement as HTMLElement;
    expect(within(failures).getByText("서비스 시작 실패")).toBeTruthy();
    expect(within(failures).getByText("상태 확인 시간 초과")).toBeTruthy();
  });

  it("says when a failure has no recorded reason, rather than leaving a blank", async () => {
    await renderPanel(
      fixture({ recent_failures: [deployment("d-x", "p-failed", "FAILED", null)] }),
    );

    const failures = screen.getByRole("heading", { name: /^최근 배포 실패/ }).parentElement as HTMLElement;
    expect(within(failures).getByText("사유 없음")).toBeTruthy();
  });

  it("shows the truncation notice when older failures exist beyond the bound", async () => {
    await renderPanel(fixture({ recent_failures_truncated: true }));

    expect(screen.getByText(/최근 실패 20건까지만 표시됩니다/)).toBeTruthy();
  });

  it("keeps the recent-failures list in a named, keyboard-focusable scroll region", async () => {
    await renderPanel(fixture());

    const region = screen.getByRole("region", { name: "최근 배포 실패 목록" });
    expect(region.getAttribute("tabindex")).toBe("0");
    expect(within(region).getAllByRole("listitem")).toHaveLength(2);
  });

  it("offers no deploy, rollback or stop control, and links only to a project's own page", async () => {
    await renderPanel(fixture());

    for (const name of [/배포 확정/, /롤백/, /중지/]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["새로 고침"]);
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs.length).toBeGreaterThan(0);
    for (const href of hrefs) expect(href).toMatch(/^\/projects\/p-(serving|failed|new)$/);
  });
});

describe("DeploymentHealthPanel — the metric tiles are derived from the one read", () => {
  beforeEach(() => healthMock.mockReset());

  function tileValue(label: string): string | null {
    const labelEl = screen.getByText(label);
    return labelEl.nextElementSibling?.textContent ?? null;
  }

  it("counts projects, those with an active deployment, those needing attention, and recent failures", async () => {
    await renderPanel(fixture());

    expect(tileValue("프로젝트 수")).toBe("3");
    expect(tileValue("현재 운영 중")).toBe("1");
    // serving-with-a-failed-latest and failed-with-nothing-serving need attention; never-deployed does not
    expect(tileValue("주의 필요")).toBe("2");
    expect(tileValue("최근 배포 실패")).toBe("2");
  });
});
