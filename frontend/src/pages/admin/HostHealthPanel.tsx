import { useEffect, useState } from "react";

import { consoleApi } from "../../api/console";
import { notificationsApi } from "../../api/notifications";
import { ApiError } from "../../api/client";
import type { HostHealth, NotificationTestResult } from "../../types/api";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { EmptyState } from "../../components/ui/EmptyState";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatTile } from "../../components/ui/StatTile";
import { Table } from "../../components/ui/Table";
import { visibleControls } from "../../components/ui/visibleControls";
import { Code } from "../../components/ui/Code";
import {
  anyChannelConfigured,
  channelDisplay,
  channelLabel,
  channelsNeedingAttention,
  diskDisplay,
  checkerDisplay,
  diskLabel,
  disksNeedingAttention,
  failureSummary,
  formatAge,
  formatBytes,
  formatPercent,
  serviceDisplay,
  servicesNeedingAttention,
  sortDisksByAttention,
  sortServicesByAttention,
  testOutcomeLines,
  testRefusalMessage,
  workerDetail,
  workerDisplay,
} from "./hostHealth";
import { panelErrorMessage } from "./panelError";

/**
 * Platform health: the worker lease and the host disk, read-only.
 *
 * One read, `GET /console/health`, of what the worker last persisted: nothing is measured when this
 * opens, so the disk rows carry the time of the worker's measurement. Status is always words (the
 * badge carries its label), never a colour alone, and a location that could not be measured is
 * shown as unmeasurable, not as fine. It also shows each ACTIVE service's latest check (state, the
 * last failure kind and since when); a service the worker has not checked yet reads "not checked",
 * never healthy. It also shows the two operator-notification channels (configured or not, last
 * attempt and its result, the error CLASS only) and the one write on this screen, "send a test
 * message": it goes to `POST /platform/notifications/test`, not to the read-only console router.
 */
export function HostHealthPanel() {
  const [health, setHealth] = useState<HostHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testOutcome, setTestOutcome] = useState<NotificationTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  async function sendTest() {
    setTesting(true);
    setTestOutcome(null);
    setTestError(null);
    try {
      setTestOutcome(await notificationsApi.sendTest());
      // The result is also what the table below shows: read it back rather than guess it.
      setHealth(await consoleApi.hostHealth());
    } catch (err) {
      const refusal = err instanceof ApiError ? testRefusalMessage(err.status) : null;
      setTestError(refusal ?? panelErrorMessage(err, "테스트 메시지를 보내지 못했습니다."));
    } finally {
      setTesting(false);
    }
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setHealth(await consoleApi.hostHealth());
    } catch (err) {
      setError(panelErrorMessage(err, "상태를 불러오지 못했습니다."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const worker = health === null ? null : workerDisplay(health.worker);
  const needAttention = health === null ? 0 : disksNeedingAttention(health.disks);
  const checker = health === null ? null : checkerDisplay(health.service_checker);
  const servicesAttention = health === null ? 0 : servicesNeedingAttention(health.services);
  const channelsAttention = health === null ? 0 : channelsNeedingAttention(health.notification_channels);

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 text-sm text-slate-600 dark:text-slate-400">
          조회 전용 화면입니다. 값은 워커가 마지막으로 점검한 결과이며, 이 화면을 연다고 새로 측정하지 않습니다.
        </p>
        <Button variant="secondary" busy={loading} onClick={load}>
          새로 고침
        </Button>
      </div>

      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {loading && <Skeleton label="상태 불러오는 중" className="mt-4" />}

      {health && worker && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile
              label="워커"
              tone={worker.tone}
              value={worker.label}
              note={workerDetail(health.worker)}
            />
            <StatTile
              label="서비스 주의 필요"
              tone={servicesAttention > 0 ? "danger" : "neutral"}
              value={servicesAttention}
              note={`운영 중 ${health.services.length}개 중 비정상 · 아직 점검 안 됨`}
            />
            <StatTile
              label="디스크 주의 필요"
              tone={needAttention > 0 ? "danger" : "neutral"}
              value={needAttention}
              note={`점검 위치 ${health.disks.length}곳 중 임계값 미만 · 측정 불가`}
            />
            <StatTile
              label="디스크 여유 하한"
              value={`${health.disk_min_free_percent}% / ${health.disk_min_free_gb} GB`}
              note="둘 중 하나라도 밑돌면 임계값 미만"
            />
          </div>

          <Card className="mt-4 min-w-0 p-4">
            <h3 className="text-base font-semibold text-slate-900 dark:text-white">
              서비스 상태 ({health.services.length})
            </h3>
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
              운영 중인 서비스를 워커가 주기적으로 점검한 결과입니다. 연속 {health.service_failure_threshold}회 실패하면
              비정상이 되고, 한 번 통과하면 다시 정상이 됩니다. 점검은 서비스를 재시작하거나 상태를 바꾸지 않습니다.
            </p>
            {checker && health.service_checker && (
              <p className="mt-2 text-sm">
                <Badge tone={checker.tone}>{checker.label}</Badge>
                <span className="ml-2 text-xs text-slate-600 dark:text-slate-400">
                  {health.service_checker.last_pass_started_at === null
                    ? "실행 기록 없음"
                    : `마지막 점검 시작 ${new Date(health.service_checker.last_pass_started_at).toLocaleString()} (${formatAge(health.service_checker.stale_after_seconds)} 넘게 없으면 멈춘 것으로 봄)`}
                </span>
              </p>
            )}
            {health.services.length === 0 ? (
              <EmptyState
                className="mt-3"
                title="운영 중인 서비스가 없습니다"
                description="배포가 활성화되면 워커가 점검을 시작하고 여기에 나타납니다."
              />
            ) : (
              <Table className="mt-3 min-w-xl" aria-label="서비스 상태">
                <Table.Head>
                  <Table.Row>
                    <Table.HeaderCell scope="col">서비스</Table.HeaderCell>
                    <Table.HeaderCell scope="col">상태</Table.HeaderCell>
                    <Table.HeaderCell scope="col">마지막 실패</Table.HeaderCell>
                    <Table.HeaderCell scope="col">마지막 점검</Table.HeaderCell>
                  </Table.Row>
                </Table.Head>
                <Table.Body>
                  {sortServicesByAttention(health.services).map((service) => {
                    const display = serviceDisplay(service);
                    return (
                      <Table.Row key={service.deployment_id}>
                        <Table.Cell className="min-w-40">
                          <strong className="font-semibold text-slate-900 dark:text-white">
                            {visibleControls(service.project_name)}
                          </strong>
                        </Table.Cell>
                        <Table.Cell>
                          <Badge tone={display.tone}>{display.label}</Badge>
                        </Table.Cell>
                        <Table.Cell className="text-xs text-slate-700 dark:text-slate-300">
                          {failureSummary(service, (iso) => new Date(iso).toLocaleString())}
                        </Table.Cell>
                        <Table.Cell className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-400">
                          {service.last_checked_at === null
                            ? "점검 기록 없음"
                            : new Date(service.last_checked_at).toLocaleString()}
                        </Table.Cell>
                      </Table.Row>
                    );
                  })}
                </Table.Body>
              </Table>
            )}
          </Card>

          <Card className="mt-4 min-w-0 p-4">
            <h3 className="text-base font-semibold text-slate-900 dark:text-white">알림 채널</h3>
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
              워커 중단, 서비스 비정상, 디스크 부족과 각각의 복구를 운영자에게 알립니다. 이벤트마다 한 번만 보내며 메시지에는
              이름, 시각, 콘솔 링크만 담깁니다. 채널은 환경 설정으로 켜고, 바꾼 뒤에는 재시작이 필요합니다.
              {channelsAttention > 0 && " 마지막 전송이 실패했거나 아직 시도하지 않은 채널이 있습니다."}
            </p>
            {anyChannelConfigured(health.notification_channels) && (
              <Table className="mt-3 min-w-xl" aria-label="알림 채널">
                <Table.Head>
                  <Table.Row>
                    <Table.HeaderCell scope="col">채널</Table.HeaderCell>
                    <Table.HeaderCell scope="col">상태</Table.HeaderCell>
                    <Table.HeaderCell scope="col">마지막 시도</Table.HeaderCell>
                    <Table.HeaderCell scope="col">오류 종류</Table.HeaderCell>
                  </Table.Row>
                </Table.Head>
                <Table.Body>
                  {health.notification_channels.map((channel) => {
                    const display = channelDisplay(channel);
                    return (
                      <Table.Row key={channel.channel}>
                        <Table.Cell className="font-semibold text-slate-900 dark:text-white">
                          {channelLabel(channel.channel)}
                        </Table.Cell>
                        <Table.Cell>
                          <Badge tone={display.tone}>{display.label}</Badge>
                        </Table.Cell>
                        <Table.Cell className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-400">
                          {channel.last_attempt_at === null
                            ? "기록 없음"
                            : new Date(channel.last_attempt_at).toLocaleString()}
                        </Table.Cell>
                        <Table.Cell className="text-xs text-slate-700 dark:text-slate-300">
                          {channel.last_error_class === null ? "—" : <Code>{channel.last_error_class}</Code>}
                        </Table.Cell>
                      </Table.Row>
                    );
                  })}
                </Table.Body>
              </Table>
            )}
            {anyChannelConfigured(health.notification_channels) ? (
              <div className="mt-3">
                <Button variant="secondary" busy={testing} onClick={sendTest}>
                  테스트 메시지 보내기
                </Button>
                <span className="ml-2 text-xs text-slate-600 dark:text-slate-400">
                  설정된 모든 채널에 즉시 보내며 감사 로그에 남습니다. {health.notification_test_min_interval_seconds}초에 한 번만 보낼 수 있습니다.
                </span>
              </div>
            ) : (
              <p className="mt-3 text-sm text-slate-700 dark:text-slate-300">
                설정된 알림 채널이 없습니다. 웹훅 또는 이메일을 설정하면 여기에서 테스트 메시지를 보낼 수 있습니다.
              </p>
            )}
            {testError && (
              <Alert variant="error" live className="mt-3">
                {testError}
              </Alert>
            )}
            {testOutcome &&
              testOutcomeLines(testOutcome).map((line) => (
                <Alert key={line.text} variant={line.ok ? "success" : "error"} live className="mt-3">
                  {line.text}
                </Alert>
              ))}
          </Card>

          <Card className="mt-4 min-w-0 p-4">
            <h3 className="text-base font-semibold text-slate-900 dark:text-white">
              호스트 디스크 ({health.disks.length})
            </h3>
            {health.disks.length === 0 ? (
              <EmptyState
                className="mt-3"
                title="아직 디스크 점검 결과가 없습니다"
                description="워커가 시작되어 첫 점검을 마치면 여기에 나타납니다."
              />
            ) : (
              <Table className="mt-3 min-w-xl" aria-label="호스트 디스크">
                <Table.Head>
                  <Table.Row>
                    <Table.HeaderCell scope="col">위치</Table.HeaderCell>
                    <Table.HeaderCell scope="col">상태</Table.HeaderCell>
                    <Table.HeaderCell scope="col">여유 / 전체</Table.HeaderCell>
                    <Table.HeaderCell scope="col">여유 비율</Table.HeaderCell>
                    <Table.HeaderCell scope="col">점검 시각</Table.HeaderCell>
                  </Table.Row>
                </Table.Head>
                <Table.Body>
                  {sortDisksByAttention(health.disks).map((disk) => {
                    const display = diskDisplay(disk);
                    return (
                      <Table.Row key={disk.label}>
                        <Table.Cell className="min-w-40">
                          <strong className="font-semibold text-slate-900 dark:text-white">
                            {diskLabel(disk.label)}
                          </strong>
                          <div className="break-all text-xs text-slate-600 dark:text-slate-400">
                            {disk.path === null ? "경로를 알 수 없음" : <Code>{disk.path}</Code>}
                          </div>
                        </Table.Cell>
                        <Table.Cell>
                          <Badge tone={display.tone}>{display.label}</Badge>
                        </Table.Cell>
                        <Table.Cell className="whitespace-nowrap">
                          {formatBytes(disk.free_bytes)} / {formatBytes(disk.total_bytes)}
                        </Table.Cell>
                        <Table.Cell className="whitespace-nowrap">{formatPercent(disk.free_percent)}</Table.Cell>
                        <Table.Cell className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-400">
                          {new Date(disk.checked_at).toLocaleString()}
                        </Table.Cell>
                      </Table.Row>
                    );
                  })}
                </Table.Body>
              </Table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
