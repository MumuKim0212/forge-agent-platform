/**
 * The health panel's decisions: what a worker lease state and a disk
 * measurement mean on screen. Every state is words, never a colour alone, and a state this build
 * does not know reads as "needs checking", never as healthy (fail closed).
 *
 * Contract strings below are the backend's (`services/readiness.py` LeaseView states,
 * `services/host_disk.DiskState`, `Label`).
 */
import type { BadgeTone } from "../../components/ui/Badge";
import { visibleControls } from "../../components/ui/visibleControls";
import type {
  DiskCheck,
  NotificationChannel,
  NotificationTestResult,
  ServiceChecker,
  ServiceHealth,
  WorkerLeaseHealth,
} from "../../types/api";

export interface HealthDisplay {
  tone: BadgeTone;
  label: string;
}

/** "7초", "3분 5초", "2시간 10분", "1일 4시간": the lease age as an operator reads it. */
export function formatAge(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}초`;
  if (total < 3600) {
    const rest = total % 60;
    return rest === 0 ? `${Math.floor(total / 60)}분` : `${Math.floor(total / 60)}분 ${rest}초`;
  }
  if (total < 86400) {
    const minutes = Math.floor((total % 3600) / 60);
    return minutes === 0 ? `${Math.floor(total / 3600)}시간` : `${Math.floor(total / 3600)}시간 ${minutes}분`;
  }
  const hours = Math.floor((total % 86400) / 3600);
  return hours === 0 ? `${Math.floor(total / 86400)}일` : `${Math.floor(total / 86400)}일 ${hours}시간`;
}

export function workerDisplay(worker: WorkerLeaseHealth): HealthDisplay {
  switch (worker.state) {
    case "HELD":
      return { tone: "success", label: "작동 중" };
    case "STALE":
      return { tone: "danger", label: "응답 없음" };
    case "RELEASED":
      return { tone: "warning", label: "정상 종료됨 (실행 중인 워커 없음)" };
    case "NONE":
      return { tone: "warning", label: "워커가 한 번도 시작되지 않음" };
    default:
      return { tone: "warning", label: "상태 확인 필요" };
  }
}

/** The lease line under the state: the heartbeat age against the window after which it is stale. */
export function workerDetail(worker: WorkerLeaseHealth): string {
  if (worker.heartbeat_age_seconds === null) return "하트비트 기록 없음";
  return `마지막 하트비트 ${formatAge(worker.heartbeat_age_seconds)} 전 (${formatAge(worker.stale_after_seconds)} 넘으면 응답 없음)`;
}

export function diskDisplay(disk: DiskCheck): HealthDisplay {
  switch (disk.state) {
    case "OK":
      return { tone: "success", label: "여유 충분" };
    case "BELOW_THRESHOLD":
      return { tone: "danger", label: "임계값 미만" };
    case "UNKNOWN":
      return { tone: "warning", label: "측정 불가" };
    default:
      return { tone: "warning", label: "상태 확인 필요" };
  }
}

export const DISK_LABELS: Record<string, string> = {
  docker_data_root: "Docker 데이터 루트",
  database_dir: "플랫폼 DB 디렉터리",
  local_git_root: "로컬 git 저장소",
  backup_dir: "서비스 데이터 백업",
};

/** An unrecognised label is shown as it came, so a location added on the backend is never hidden. */
export function diskLabel(label: string): string {
  return DISK_LABELS[label] ?? label;
}

const GIB = 1024 ** 3;

export function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(1)} GB`;
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
}

export function formatPercent(percent: number | null): string {
  return percent === null ? "—" : `${percent.toFixed(1)}%`;
}

/** Locations that are not known to be fine: below threshold, unmeasurable, or a state we cannot read. */
export function disksNeedingAttention(disks: DiskCheck[]): number {
  return disks.filter((disk) => disk.state !== "OK").length;
}

/** Below-threshold first, then unknown, then the rest, so the row to act on is at the top. */
export function sortDisksByAttention(disks: DiskCheck[]): DiskCheck[] {
  const rank = (disk: DiskCheck) => (disk.state === "BELOW_THRESHOLD" ? 0 : disk.state === "OK" ? 2 : 1);
  return [...disks].sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
}

/**
 * A service's state in words. A service nobody has checked yet, and a state this build
 * does not know, are never read as healthy. A healthy service whose last check failed (below the
 * threshold) or could not be carried out says so, since "healthy" alone would hide a streak.
 */
export function serviceDisplay(service: ServiceHealth): HealthDisplay {
  switch (service.state) {
    case "UNHEALTHY":
      return { tone: "danger", label: service.stale ? "비정상 (점검 지연)" : "비정상" };
    case "UNCHECKED":
      return { tone: "warning", label: "아직 점검 안 됨" };
    case "HEALTHY":
      // An old reading is not evidence of anything current: never plain healthy.
      if (service.stale) return { tone: "warning", label: "점검 지연 (마지막 결과: 정상)" };
      if (service.last_result === "FAIL") return { tone: "warning", label: "정상 (최근 점검 실패)" };
      if (service.last_result === "UNKNOWN") return { tone: "warning", label: "정상 (점검 불가)" };
      return { tone: "success", label: "정상" };
    default:
      return { tone: "warning", label: "상태 확인 필요" };
  }
}

export const FAILURE_KIND_LABELS: Record<string, string> = {
  CONTAINER_MISSING: "컨테이너 없음",
  NOT_RUNNING: "실행 중이 아님",
  RESTARTED: "재시작됨",
  HEALTHZ_FAILED: "/healthz 실패",
  HEALTHZ_TIMEOUT: "/healthz 시간 초과",
};

/** An unrecognised kind is shown as it came, so a kind added on the backend is never hidden. */
export function failureKindLabel(kind: string | null): string {
  if (kind === null) return "—";
  return FAILURE_KIND_LABELS[kind] ?? kind;
}

/** Services that are not known to be fine: unhealthy, unchecked, stale, or a state we cannot read. */
export function servicesNeedingAttention(services: ServiceHealth[]): number {
  return services.filter((service) => service.state !== "HEALTHY" || service.stale).length;
}

/** The checker's own state in words. No record, or a state we cannot read, is never "running". */
export function checkerDisplay(checker: ServiceChecker | null): HealthDisplay {
  switch (checker?.state) {
    case "ALIVE":
      return { tone: "success", label: "서비스 점검 작동 중" };
    case "STALE":
      return { tone: "danger", label: "서비스 점검이 멈춤" };
    case "NONE":
      return { tone: "warning", label: "서비스 점검이 아직 실행되지 않음" };
    default:
      return { tone: "warning", label: "서비스 점검 상태 확인 필요" };
  }
}

/** Unhealthy first, then not yet checked, then healthy; by project name within each. */
export function sortServicesByAttention(services: ServiceHealth[]): ServiceHealth[] {
  const rank = (service: ServiceHealth) =>
    service.state === "UNHEALTHY" ? 0 : service.state === "HEALTHY" && !service.stale ? 2 : 1;
  return [...services].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      visibleControls(a.project_name).localeCompare(visibleControls(b.project_name)) ||
      a.deployment_id.localeCompare(b.deployment_id),
  );
}

/** The failure column: the last failure kind and, while it is still failing, since when. */
export function failureSummary(service: ServiceHealth, formatTime: (iso: string) => string): string {
  if (service.last_failure_kind === null) return "—";
  const kind = failureKindLabel(service.last_failure_kind);
  if (service.failing_since !== null) {
    return `${kind} · ${formatTime(service.failing_since)}부터 (연속 ${service.consecutive_failures}회)`;
  }
  return service.last_failure_at !== null
    ? `${kind} · 마지막 ${formatTime(service.last_failure_at)} (지금은 통과)`
    : kind;
}

/** The two channels' names. An unrecognised channel is shown as it came, never dropped. */
export function channelLabel(channel: string): string {
  switch (channel) {
    case "webhook":
      return "웹훅";
    case "email":
      return "이메일";
    default:
      return channel;
  }
}

/** A channel's state in words: not configured, never tried, last attempt succeeded or failed. */
export function channelDisplay(channel: NotificationChannel): HealthDisplay {
  if (!channel.configured) return { tone: "neutral", label: "설정 안 됨" };
  switch (channel.last_result) {
    case null:
      return { tone: "neutral", label: "아직 전송 기록 없음" };
    case "OK":
      return { tone: "success", label: "마지막 전송 성공" };
    case "FAILED":
      return { tone: "danger", label: "마지막 전송 실패" };
    default:
      return { tone: "warning", label: "상태 확인 필요" };
  }
}

export function anyChannelConfigured(channels: NotificationChannel[]): boolean {
  return channels.some((channel) => channel.configured);
}

/** Channels that are configured and whose last attempt failed: what the operator must look at. */
export function channelsNeedingAttention(channels: NotificationChannel[]): number {
  return channels.filter((channel) => channel.configured && channel.last_result !== "OK").length;
}

/** What the test message did, per channel, in one sentence each. A failure names the error class. */
export function testOutcomeLines(result: NotificationTestResult): { ok: boolean; text: string }[] {
  return result.results.map((item) => ({
    ok: item.ok,
    text: item.ok
      ? `${channelLabel(item.channel)}: 전송했습니다`
      : `${channelLabel(item.channel)}: 전송하지 못했습니다${item.error_class ? ` (${item.error_class})` : ""}`,
  }));
}

/** The refusals the test message has a specific sentence for; everything else is `panelError`'s. */
export function testRefusalMessage(status: number): string | null {
  if (status === 409) return "설정된 알림 채널이 없습니다. 환경 설정에서 웹훅이나 이메일을 설정하세요.";
  if (status === 429) return "방금 테스트 메시지를 보냈습니다. 잠시 뒤에 다시 시도하세요.";
  return null;
}
