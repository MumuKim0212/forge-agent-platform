// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. Scope is the ADR's: focus and ARIA on the eval
 * start control and its in-page confirmation — not rendering, not copy. Opening the confirmation
 * moves focus into it; leaving it by cancelling, or by a start that is refused, must put focus
 * back on the start control (or, when that control is then disabled and cannot hold focus, on the
 * note that says why), never drop it to the page.
 *
 * The api module and the identity hook are mocked; nothing else the panel imports needs a provider
 * (no run is listed, so no case link, so no router).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { evalsApi } from "../../api/evals";
import type { EvalRun, EvalRunDetail, EvalStatus } from "../../types/api";
import { EvalRunsPanel } from "./EvalRunsPanel";

vi.mock("../../api/evals", () => ({
  evalsApi: { status: vi.fn(), listRuns: vi.fn(), getRun: vi.fn(), startRun: vi.fn() },
}));
vi.mock("../../context/IdentityContext", () => ({
  useIdentity: () => ({ currentUser: { id: "u1", name: "관리자", role: "admin" } }),
}));

const statusMock = vi.mocked(evalsApi.status);
const listMock = vi.mocked(evalsApi.listRuns);
const startMock = vi.mocked(evalsApi.startRun);

const READY: EvalStatus = {
  budget_usd: 1,
  case_min_budget_usd: 0.05,
  executor_active: true,
  engine: "openai",
  model: "gpt-5-mini",
  engine_available: true,
  run_in_progress: false,
  request_set_version: 1,
  case_count: 3,
};

async function openConfirmation() {
  render(<EvalRunsPanel />);
  const start = await screen.findByRole("button", { name: "평가 실행 시작" });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  const group = await screen.findByRole("group", { name: "평가 실행을 시작할까요?" });
  return { start, group };
}

describe("EvalRunsPanel start confirmation — focus", () => {
  beforeEach(() => {
    statusMock.mockReset().mockResolvedValue(READY);
    listMock.mockReset().mockResolvedValue([] as EvalRun[]);
    startMock.mockReset();
  });

  it("moves focus into the confirmation when it opens", async () => {
    const { group } = await openConfirmation();

    await waitFor(() => expect(document.activeElement).toBe(group));
  });

  it("returns focus to the start button after cancelling", async () => {
    const { start } = await openConfirmation();

    fireEvent.click(screen.getByRole("button", { name: "취소" }));

    await waitFor(() => expect(screen.queryByRole("group", { name: "평가 실행을 시작할까요?" })).toBeNull());
    expect(start).toBeEnabled();
    await waitFor(() => expect(document.activeElement).toBe(start));
  });

  it("returns focus to the start button after a start that is refused (422) and leaves it enabled", async () => {
    startMock.mockRejectedValue(new ApiError(422, "refused"));
    const { start } = await openConfirmation();

    fireEvent.click(screen.getByRole("button", { name: "시작" }));

    await screen.findByText(/평가 실행을 시작할 수 없습니다/);
    await waitFor(() => expect(document.activeElement).toBe(start));
  });

  it("puts focus on the note that explains why, when the refusal leaves the start button disabled (409)", async () => {
    startMock.mockRejectedValue(new ApiError(409, "already running"));
    const { start } = await openConfirmation();
    statusMock.mockResolvedValue({ ...READY, run_in_progress: true });

    fireEvent.click(screen.getByRole("button", { name: "시작" }));

    await screen.findByText(/이미 진행 중인 평가 실행/);
    await waitFor(() => expect(start).toBeDisabled());
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById("eval-start-note")));
  });

  it("puts focus on the note after a successful start, when the button is disabled because the run is in progress", async () => {
    const started = { id: "run-1", cases: [] } as unknown as EvalRunDetail;
    startMock.mockResolvedValue(started);
    const { start } = await openConfirmation();
    statusMock.mockResolvedValue({ ...READY, run_in_progress: true });

    fireEvent.click(screen.getByRole("button", { name: "시작" }));

    await waitFor(() => expect(start).toBeDisabled());
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById("eval-start-note")));
  });
});

describe("EvalRunsPanel repair labels are plain text, not live regions", () => {
  const RUN: EvalRun = {
    id: "run-1",
    status: "COMPLETED",
    engine: "openai",
    model: "gpt-5-mini",
    request_set_version: 3,
    prompt_fingerprint: "3f9a1c07be52",
    repair_max_attempts: 2,
    budget_usd: 1,
    spend_usd: 0.3,
    started_by_id: "u1",
    started_by_name: "관리자",
    started_at: "2026-11-18T01:00:00Z",
    ended_at: "2026-11-18T01:05:30Z",
    duration_seconds: 330,
    detail: null,
    total: 1,
    passed: 1,
    failed: 0,
    not_run: 0,
  };
  const DETAIL: EvalRunDetail = {
    ...RUN,
    cases: [
      {
        id: "c1",
        case_id: "room-bookings",
        position: 0,
        state: "DONE",
        result: "PASSED",
        failure_reason: null,
        failed_check_index: null,
        check_outcomes: [],
        project_id: null,
        build_id: null,
        policy_risk_tier: null,
        policy_flagged_checks: [],
        cost_usd: 0.3,
        cost_tokens: 1000,
        repair_attempts: 1,
        duration_seconds: 75,
        started_at: null,
        ended_at: null,
      },
    ],
  };

  beforeEach(() => {
    statusMock.mockReset().mockResolvedValue(READY);
    listMock.mockReset().mockResolvedValue([RUN]);
    vi.mocked(evalsApi.getRun).mockReset().mockResolvedValue(DETAIL);
  });

  it("shows the recorded setting and the case's attempts with no alert, status or aria-live around them", async () => {
    const { baseElement } = render(<EvalRunsPanel />);

    const setting = await screen.findAllByText("수리 시도 최대 2회");
    const attempts = await screen.findByText("1회");
    expect(screen.getByRole("columnheader", { name: "수리 시도" })).toBeInTheDocument();
    for (const element of [...setting, attempts]) {
      expect(element.closest("[role=alert],[role=status],[aria-live]")).toBeNull();
    }
    expect(baseElement.querySelector("[aria-live]")).toBeNull();
  });

  it("says not recorded for a run from before the setting existed", async () => {
    listMock.mockResolvedValue([{ ...RUN, repair_max_attempts: null }]);
    vi.mocked(evalsApi.getRun).mockResolvedValue({ ...DETAIL, repair_max_attempts: null });
    render(<EvalRunsPanel />);

    expect((await screen.findAllByText("수리 설정 기록 없음")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/수리 시도 최대/)).not.toBeInTheDocument();
  });
});
