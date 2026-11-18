// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30. Scope is what a screen reader is told: each disk
 * location's state is read out as words in a table with named columns (never colour alone), a
 * location that could not be measured says so and is not reported as fine, and the one control is
 * a named button. Not layout, not copy beyond the state labels.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { consoleApi } from "../../api/console";
import { notificationsApi } from "../../api/notifications";
import type { HostHealth } from "../../types/api";
import { HostHealthPanel } from "./HostHealthPanel";

vi.mock("../../api/console", () => ({
  consoleApi: { deploymentHealth: vi.fn(), configuration: vi.fn(), hostHealth: vi.fn() },
}));

vi.mock("../../api/notifications", () => ({ notificationsApi: { sendTest: vi.fn() } }));

const healthMock = vi.mocked(consoleApi.hostHealth);
const sendTestMock = vi.mocked(notificationsApi.sendTest);

const FIXTURE: HostHealth = {
  worker: { state: "STALE", heartbeat_age_seconds: 130, stale_after_seconds: 60 },
  disk_min_free_percent: 10,
  disk_min_free_gb: 5,
  service_failure_threshold: 3,
  services: [],
  service_checker: null,
  notification_channels: [],
  notification_test_min_interval_seconds: 30,
  disks: [
    {
      label: "backup_dir",
      path: "/srv/backups",
      free_bytes: 900 * 1024 ** 3,
      total_bytes: 1000 * 1024 ** 3,
      free_percent: 90,
      state: "OK",
      checked_at: "2026-11-14T00:00:00Z",
    },
    {
      label: "docker_data_root",
      path: null,
      free_bytes: null,
      total_bytes: null,
      free_percent: null,
      state: "UNKNOWN",
      checked_at: "2026-11-14T00:00:00Z",
    },
    {
      label: "local_git_root",
      path: "/srv/git",
      free_bytes: 4 * 1024 ** 3,
      total_bytes: 20 * 1024 ** 3,
      free_percent: 20,
      state: "BELOW_THRESHOLD",
      checked_at: "2026-11-14T00:00:00Z",
    },
  ],
};

function rowFor(table: HTMLElement, text: string) {
  return within(table)
    .getAllByRole("row")
    .find((row) => within(row).queryByText(text) !== null) as HTMLElement;
}

describe("HostHealthPanel — state is words in a labelled table", () => {
  beforeEach(() => healthMock.mockReset());

  it("names the columns and reads each location's state as text, worst first", async () => {
    healthMock.mockResolvedValue(FIXTURE);
    render(<HostHealthPanel />);
    const table = await screen.findByRole("table", { name: "호스트 디스크" });

    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "위치",
      "상태",
      "여유 / 전체",
      "여유 비율",
      "점검 시각",
    ]);
    expect(within(rowFor(table, "로컬 git 저장소")).getByText("임계값 미만")).toBeTruthy();
    expect(within(rowFor(table, "Docker 데이터 루트")).getByText("측정 불가")).toBeTruthy();
    expect(within(rowFor(table, "서비스 데이터 백업")).getByText("여유 충분")).toBeTruthy();
    const order = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.textContent ?? "");
    expect(order[0]).toContain("로컬 git 저장소");
    expect(order[2]).toContain("서비스 데이터 백업");
  });

  it("says in words that the worker is not responding", async () => {
    healthMock.mockResolvedValue(FIXTURE);
    render(<HostHealthPanel />);
    expect(await screen.findByText("응답 없음")).toBeTruthy();
    expect(screen.getByText(/마지막 하트비트 2분 10초 전/)).toBeTruthy();
  });

  it("has one named control and, when there is no result yet, an explanation instead of an empty table", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, worker: { state: "NONE", heartbeat_age_seconds: null, stale_after_seconds: 60 }, disks: [] });
    render(<HostHealthPanel />);
    expect(await screen.findByText("아직 디스크 점검 결과가 없습니다")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["새로 고침"]);
  });
});

const SERVICES: HostHealth["services"] = [
  {
    deployment_id: "d-fine",
    project_id: "p-fine",
    project_name: "정상 서비스",
    state: "HEALTHY",
    last_result: "PASS",
    consecutive_failures: 0,
    last_failure_kind: null,
    last_failure_at: null,
    failing_since: null,
    last_checked_at: "2026-11-14T00:00:00Z",
    state_changed_at: "2026-11-14T00:00:00Z",
    stale: false,
  },
  {
    deployment_id: "d-new",
    project_id: "p-new",
    project_name: "새 서비스",
    state: "UNCHECKED",
    last_result: null,
    consecutive_failures: 0,
    last_failure_kind: null,
    last_failure_at: null,
    failing_since: null,
    last_checked_at: null,
    state_changed_at: null,
    stale: false,
  },
  {
    deployment_id: "d-sick",
    project_id: "p-sick",
    project_name: "아픈 서비스",
    state: "UNHEALTHY",
    last_result: "FAIL",
    consecutive_failures: 4,
    last_failure_kind: "RESTARTED",
    last_failure_at: "2026-11-14T00:00:00Z",
    failing_since: "2026-11-13T23:50:00Z",
    last_checked_at: "2026-11-14T00:00:00Z",
    state_changed_at: "2026-11-13T23:55:00Z",
    stale: false,
  },
];

describe("HostHealthPanel — per-service section", () => {
  beforeEach(() => healthMock.mockReset());

  it("names the columns and reads each service's state and last failure as text, worst first", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, services: SERVICES });
    render(<HostHealthPanel />);
    const table = await screen.findByRole("table", { name: "서비스 상태" });

    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "서비스",
      "상태",
      "마지막 실패",
      "마지막 점검",
    ]);
    expect(within(rowFor(table, "아픈 서비스")).getByText("비정상")).toBeTruthy();
    expect(within(rowFor(table, "아픈 서비스")).getByText(/재시작됨/)).toBeTruthy();
    expect(within(rowFor(table, "아픈 서비스")).getByText(/부터 \(연속 4회\)/)).toBeTruthy();
    expect(within(rowFor(table, "새 서비스")).getByText("아직 점검 안 됨")).toBeTruthy();
    expect(within(rowFor(table, "새 서비스")).getByText("점검 기록 없음")).toBeTruthy();
    expect(within(rowFor(table, "정상 서비스")).getByText("정상")).toBeTruthy();
    const order = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.textContent ?? "");
    expect(order[0]).toContain("아픈 서비스");
    expect(order[1]).toContain("새 서비스");
    expect(order[2]).toContain("정상 서비스");
  });

  it("explains an empty list instead of showing an empty table", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, services: [] });
    render(<HostHealthPanel />);
    expect(await screen.findByText("운영 중인 서비스가 없습니다")).toBeTruthy();
    expect(screen.queryByRole("table", { name: "서비스 상태" })).toBeNull();
  });
});

describe("HostHealthPanel — a service that has not been checked lately is never plainly healthy", () => {
  beforeEach(() => healthMock.mockReset());

  it("words a stale healthy row as delayed and says the checker has stopped", async () => {
    const stale = { ...SERVICES[0], stale: true };
    healthMock.mockResolvedValue({
      ...FIXTURE,
      services: [stale],
      service_checker: {
        state: "STALE",
        last_pass_started_at: "2026-11-13T00:00:00Z",
        last_pass_finished_at: "2026-11-13T00:00:01Z",
        interval_seconds: 60,
        stale_after_seconds: 180,
      },
    });
    render(<HostHealthPanel />);
    const table = await screen.findByRole("table", { name: "서비스 상태" });
    expect(within(rowFor(table, "정상 서비스")).getByText("점검 지연 (마지막 결과: 정상)")).toBeTruthy();
    expect(within(rowFor(table, "정상 서비스")).queryByText("정상")).toBeNull();
    expect(screen.getByText("서비스 점검이 멈춤")).toBeTruthy();
  });

  it("says a running checker is running", async () => {
    healthMock.mockResolvedValue({
      ...FIXTURE,
      services: SERVICES,
      service_checker: {
        state: "ALIVE",
        last_pass_started_at: "2026-11-14T00:00:00Z",
        last_pass_finished_at: "2026-11-14T00:00:01Z",
        interval_seconds: 60,
        stale_after_seconds: 180,
      },
    });
    render(<HostHealthPanel />);
    expect(await screen.findByText("서비스 점검 작동 중")).toBeTruthy();
  });
});

const CHANNELS: HostHealth["notification_channels"] = [
  { channel: "webhook", configured: true, last_attempt_at: "2026-11-14T00:00:00Z", last_result: "FAILED", last_error_class: "ReadTimeout" },
  { channel: "email", configured: false, last_attempt_at: null, last_result: null, last_error_class: null },
];

describe("HostHealthPanel — notification channels and the test message", () => {
  beforeEach(() => {
    healthMock.mockReset();
    sendTestMock.mockReset();
  });

  it("names the columns and reads each channel's state as words", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, notification_channels: CHANNELS });
    render(<HostHealthPanel />);
    const table = await screen.findByRole("table", { name: "알림 채널" });

    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "채널",
      "상태",
      "마지막 시도",
      "오류 종류",
    ]);
    expect(within(rowFor(table, "웹훅")).getByText("마지막 전송 실패")).toBeTruthy();
    expect(within(rowFor(table, "웹훅")).getByText("ReadTimeout")).toBeTruthy();
    expect(within(rowFor(table, "이메일")).getByText("설정 안 됨")).toBeTruthy();
  });

  it("offers no test button, and says why, when no channel is configured", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, notification_channels: CHANNELS.map((c) => ({ ...c, configured: false })) });
    render(<HostHealthPanel />);
    expect(await screen.findByText(/설정된 알림 채널이 없습니다\. 웹훅 또는 이메일/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "테스트 메시지 보내기" })).toBeNull();
  });

  it("sends the test from a named button and reports each channel's outcome in words", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, notification_channels: CHANNELS });
    sendTestMock.mockResolvedValue({
      results: [
        { channel: "webhook", ok: false, error_class: "HTTP_500" },
        { channel: "email", ok: true, error_class: null },
      ],
    });
    render(<HostHealthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "테스트 메시지 보내기" }));

    expect(await screen.findByText("웹훅: 전송하지 못했습니다 (HTTP_500)")).toBeTruthy();
    expect(screen.getByText("이메일: 전송했습니다")).toBeTruthy();
    expect(sendTestMock).toHaveBeenCalledTimes(1);
  });

  it("says in words that a second test was refused, not a blank failure", async () => {
    healthMock.mockResolvedValue({ ...FIXTURE, notification_channels: CHANNELS });
    sendTestMock.mockRejectedValue(new ApiError(429, "Too Many Requests"));
    render(<HostHealthPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "테스트 메시지 보내기" }));

    await waitFor(() => expect(screen.getByText(/방금 테스트 메시지를 보냈습니다/)).toBeTruthy());
  });
});
