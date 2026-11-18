import { describe, expect, it } from "vitest";

import type { DiskCheck, NotificationChannel, ServiceHealth, WorkerLeaseHealth } from "../../types/api";
import {
  anyChannelConfigured,
  channelDisplay,
  channelLabel,
  channelsNeedingAttention,
  testOutcomeLines,
  testRefusalMessage,
  diskDisplay,
  diskLabel,
  disksNeedingAttention,
  formatAge,
  checkerDisplay,
  failureKindLabel,
  failureSummary,
  formatBytes,
  serviceDisplay,
  servicesNeedingAttention,
  sortDisksByAttention,
  sortServicesByAttention,
  workerDetail,
  workerDisplay,
} from "./hostHealth";

function worker(state: string, age: number | null = 5): WorkerLeaseHealth {
  return { state, heartbeat_age_seconds: age, stale_after_seconds: 60 };
}

function disk(label: string, state: string): DiskCheck {
  return {
    label,
    path: "/x",
    free_bytes: state === "UNKNOWN" ? null : 1,
    total_bytes: state === "UNKNOWN" ? null : 10,
    free_percent: state === "UNKNOWN" ? null : 10,
    state,
    checked_at: "2026-11-14T00:00:00Z",
  };
}

describe("workerDisplay", () => {
  it("reads each backend state as words with its tone", () => {
    expect(workerDisplay(worker("HELD"))).toEqual({ tone: "success", label: "작동 중" });
    expect(workerDisplay(worker("STALE"))).toEqual({ tone: "danger", label: "응답 없음" });
    expect(workerDisplay(worker("RELEASED")).tone).toBe("warning");
    expect(workerDisplay(worker("NONE")).tone).toBe("warning");
  });

  it("never reads a state it does not know as healthy", () => {
    expect(workerDisplay(worker("SOMETHING_NEW"))).toEqual({ tone: "warning", label: "상태 확인 필요" });
  });
});

describe("workerDetail", () => {
  it("states the age against the stale window", () => {
    expect(workerDetail(worker("HELD", 7))).toBe("마지막 하트비트 7초 전 (1분 넘으면 응답 없음)");
  });
  it("says so when there is no heartbeat", () => {
    expect(workerDetail(worker("NONE", null))).toBe("하트비트 기록 없음");
  });
});

describe("formatAge", () => {
  it("formats seconds, minutes, hours and days", () => {
    expect(formatAge(7.9)).toBe("7초");
    expect(formatAge(60)).toBe("1분");
    expect(formatAge(185)).toBe("3분 5초");
    expect(formatAge(7800)).toBe("2시간 10분");
    expect(formatAge(100000)).toBe("1일 3시간");
    expect(formatAge(-5)).toBe("0초");
  });
});

describe("diskDisplay", () => {
  it("says below-threshold and unknown in words, and unknown is not OK", () => {
    expect(diskDisplay(disk("a", "OK"))).toEqual({ tone: "success", label: "여유 충분" });
    expect(diskDisplay(disk("a", "BELOW_THRESHOLD"))).toEqual({ tone: "danger", label: "임계값 미만" });
    expect(diskDisplay(disk("a", "UNKNOWN"))).toEqual({ tone: "warning", label: "측정 불가" });
  });
  it("never reads an unrecognised state as OK", () => {
    expect(diskDisplay(disk("a", "FINE")).label).toBe("상태 확인 필요");
  });
});

describe("disk helpers", () => {
  it("formats sizes and shows a dash for none", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(formatBytes(300 * 1024 ** 2)).toBe("300 MB");
  });
  it("names known locations and passes an unknown one through", () => {
    expect(diskLabel("docker_data_root")).toBe("Docker 데이터 루트");
    expect(diskLabel("future_dir")).toBe("future_dir");
  });
  it("counts everything that is not OK and sorts the worst first", () => {
    const disks = [disk("a", "OK"), disk("b", "UNKNOWN"), disk("c", "BELOW_THRESHOLD")];
    expect(disksNeedingAttention(disks)).toBe(2);
    expect(sortDisksByAttention(disks).map((d) => d.label)).toEqual(["c", "b", "a"]);
  });
});

function service(name: string, state: string, extra: Partial<ServiceHealth> = {}): ServiceHealth {
  return {
    deployment_id: `d-${name}`,
    project_id: `p-${name}`,
    project_name: name,
    state,
    last_result: state === "UNCHECKED" ? null : "PASS",
    consecutive_failures: 0,
    last_failure_kind: null,
    last_failure_at: null,
    failing_since: null,
    last_checked_at: null,
    state_changed_at: null,
    stale: false,
    ...extra,
  };
}

describe("serviceDisplay", () => {
  it("reads each state in words, and unchecked is not healthy", () => {
    expect(serviceDisplay(service("a", "HEALTHY"))).toEqual({ tone: "success", label: "정상" });
    expect(serviceDisplay(service("a", "UNHEALTHY"))).toEqual({ tone: "danger", label: "비정상" });
    expect(serviceDisplay(service("a", "UNCHECKED"))).toEqual({ tone: "warning", label: "아직 점검 안 됨" });
  });
  it("does not hide a failing streak or an unavailable check behind plain healthy", () => {
    expect(serviceDisplay(service("a", "HEALTHY", { last_result: "FAIL" })).tone).toBe("warning");
    expect(serviceDisplay(service("a", "HEALTHY", { last_result: "UNKNOWN" })).label).toBe("정상 (점검 불가)");
  });
  it("never reads an unrecognised state as healthy", () => {
    expect(serviceDisplay(service("a", "FINE")).label).toBe("상태 확인 필요");
  });
});

describe("service helpers", () => {
  it("names each failure kind and passes an unknown one through", () => {
    expect(failureKindLabel("CONTAINER_MISSING")).toBe("컨테이너 없음");
    expect(failureKindLabel("NOT_RUNNING")).toBe("실행 중이 아님");
    expect(failureKindLabel("RESTARTED")).toBe("재시작됨");
    expect(failureKindLabel("HEALTHZ_FAILED")).toBe("/healthz 실패");
    expect(failureKindLabel("HEALTHZ_TIMEOUT")).toBe("/healthz 시간 초과");
    expect(failureKindLabel("FUTURE_KIND")).toBe("FUTURE_KIND");
    expect(failureKindLabel(null)).toBe("—");
  });
  it("counts everything that is not healthy and sorts the worst first", () => {
    const services = [service("c", "HEALTHY"), service("b", "UNCHECKED"), service("a", "UNHEALTHY")];
    expect(servicesNeedingAttention(services)).toBe(2);
    expect(sortServicesByAttention(services).map((s) => s.project_name)).toEqual(["a", "b", "c"]);
  });
  it("says since when while failing, and that it passes now after a recovery", () => {
    const at = (iso: string) => `@${iso}`;
    const failing = service("a", "UNHEALTHY", {
      last_failure_kind: "HEALTHZ_TIMEOUT",
      failing_since: "T1",
      last_failure_at: "T2",
      consecutive_failures: 5,
    });
    expect(failureSummary(failing, at)).toBe("/healthz 시간 초과 · @T1부터 (연속 5회)");
    const recovered = service("a", "HEALTHY", { last_failure_kind: "RESTARTED", last_failure_at: "T2" });
    expect(failureSummary(recovered, at)).toBe("재시작됨 · 마지막 @T2 (지금은 통과)");
    expect(failureSummary(service("a", "HEALTHY"), at)).toBe("—");
  });
});

describe("staleness and the checker", () => {
  it("a stale row is never plainly healthy, and stale unhealthy stays unhealthy", () => {
    expect(serviceDisplay(service("a", "HEALTHY", { stale: true }))).toEqual({
      tone: "warning",
      label: "점검 지연 (마지막 결과: 정상)",
    });
    expect(serviceDisplay(service("a", "UNHEALTHY", { stale: true }))).toEqual({
      tone: "danger",
      label: "비정상 (점검 지연)",
    });
  });
  it("counts and sorts a stale healthy row with the ones needing attention", () => {
    const services = [service("c", "HEALTHY"), service("b", "HEALTHY", { stale: true })];
    expect(servicesNeedingAttention(services)).toBe(1);
    expect(sortServicesByAttention(services).map((s) => s.project_name)).toEqual(["b", "c"]);
  });
  it("reads the checker in words and never reads a missing or unknown one as running", () => {
    const checker = (state: string) => ({
      state,
      last_pass_started_at: null,
      last_pass_finished_at: null,
      interval_seconds: 60,
      stale_after_seconds: 180,
    });
    expect(checkerDisplay(checker("ALIVE")).tone).toBe("success");
    expect(checkerDisplay(checker("STALE"))).toEqual({ tone: "danger", label: "서비스 점검이 멈춤" });
    expect(checkerDisplay(checker("NONE")).tone).toBe("warning");
    expect(checkerDisplay(checker("???")).label).toBe("서비스 점검 상태 확인 필요");
    expect(checkerDisplay(null).label).toBe("서비스 점검 상태 확인 필요");
  });
});

function channel(over: Partial<NotificationChannel> = {}): NotificationChannel {
  return { channel: "webhook", configured: true, last_attempt_at: null, last_result: null, last_error_class: null, ...over };
}

describe("notification channels", () => {
  it("reads each state in words: not configured, never tried, ok, failed", () => {
    expect(channelDisplay(channel({ configured: false })).label).toBe("설정 안 됨");
    expect(channelDisplay(channel()).label).toBe("아직 전송 기록 없음");
    expect(channelDisplay(channel({ last_result: "OK" }))).toEqual({ tone: "success", label: "마지막 전송 성공" });
    expect(channelDisplay(channel({ last_result: "FAILED" }))).toEqual({ tone: "danger", label: "마지막 전송 실패" });
  });

  it("never reads an unrecognised result as a success", () => {
    expect(channelDisplay(channel({ last_result: "SOMETHING" })).tone).toBe("warning");
  });

  it("names the channels and passes an unknown one through", () => {
    expect([channelLabel("webhook"), channelLabel("email"), channelLabel("sms")]).toEqual(["웹훅", "이메일", "sms"]);
  });

  it("knows whether any channel exists and which ones need a look", () => {
    expect(anyChannelConfigured([channel({ configured: false })])).toBe(false);
    expect(anyChannelConfigured([channel({ configured: false }), channel({ channel: "email" })])).toBe(true);
    expect(channelsNeedingAttention([channel({ last_result: "OK" }), channel({ last_result: "FAILED" }), channel()])).toBe(2);
    expect(channelsNeedingAttention([channel({ configured: false, last_result: "FAILED" })])).toBe(0);
  });

  it("words the test outcome per channel, a failure with its error class", () => {
    expect(
      testOutcomeLines({
        results: [
          { channel: "webhook", ok: true, error_class: null },
          { channel: "email", ok: false, error_class: "SSLCertVerificationError" },
        ],
      }),
    ).toEqual([
      { ok: true, text: "웹훅: 전송했습니다" },
      { ok: false, text: "이메일: 전송하지 못했습니다 (SSLCertVerificationError)" },
    ]);
  });

  it("has a sentence for the two refusals of the test and none for anything else", () => {
    expect(testRefusalMessage(409)).toMatch(/설정된 알림 채널이 없습니다/);
    expect(testRefusalMessage(429)).toMatch(/방금 테스트 메시지를 보냈습니다/);
    expect(testRefusalMessage(500)).toBeNull();
  });
});
