import { useEffect, useState } from "react";

import { consoleApi } from "../../api/console";
import type { Deployment, DeploymentHealth } from "../../types/api";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { EmptyState } from "../../components/ui/EmptyState";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatTile } from "../../components/ui/StatTile";
import { Table } from "../../components/ui/Table";
import { FOCUS_RING } from "../../components/ui/focusRing";
import { TextLink } from "../../components/ui/TextLink";
import { StatusBadge } from "../../components/StatusBadge";
import { visibleControls } from "../../components/ui/visibleControls";
import {
  deploymentFailureLine,
  deploymentFailureReasonLabel,
} from "../../components/deploymentFailure";
import {
  DEPLOYMENT_HEALTH_COLORS,
  DEPLOYMENT_HEALTH_LABELS,
  attentionCount,
  deploymentHealthState,
  recentFailureNotice,
  servingCount,
  sortByAttention,
} from "./deploymentHealth";
import { panelErrorMessage } from "./panelError";

/**
 * Deployment health across every project.
 *
 * **Read-only, and there is no action here at all** — no deploy, no rollback, no
 * stop. Those stay on the project detail page, where owner-or-admin already
 * applies and where `canOperateDeployment` gates them (rule 2: the console does
 * not become a second control path for the same actions). The 프로젝트 열기 link
 * navigates to that existing page; it is not a control.
 *
 * the denser operations layout. Metric tiles on top, then the
 * per-project table with the recent failures (beside it from 2xl, stacked under it below, so the table is never clipped at 1280 px; the failure list scrolls inside its card) Every figure is derived from the
 * one `GET /console/deployments` read. The owner is not on that read and is not shown: this panel
 * makes no second read to resolve it. Status is always words (the badge carries its label), never a
 * colour alone.
 *
 * `service_env_json` and every other secret-bearing field are absent from the API
 * response by design (rule 3) and are named nowhere here.
 */

/** One deployment slot in a cell: its status as words, whether it was a rollback, when, and why it failed. */
function DeploymentCell({ deployment }: { deployment: Deployment | null }) {
  if (deployment === null) {
    return <span className="text-slate-500 dark:text-slate-400">없음</span>;
  }
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <StatusBadge status={deployment.status} />
        {deployment.rollback_of !== null && (
          <span className="text-xs text-slate-600 dark:text-slate-400">(롤백)</span>
        )}
      </div>
      <div className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-400" title={deployment.id}>
        {new Date(deployment.created_at).toLocaleString()}
      </div>
      {deployment.failure_reason !== null && (
        <p className="text-xs font-medium text-danger-strong dark:text-rose-300">
          {deploymentFailureLine(deployment.failure_reason)}
        </p>
      )}
    </div>
  );
}

export function DeploymentHealthPanel() {
  const [health, setHealth] = useState<DeploymentHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setHealth(await consoleApi.deploymentHealth());
    } catch (err) {
      setError(panelErrorMessage(err, "배포 상태를 불러오지 못했습니다."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const truncationNotice = health === null ? null : recentFailureNotice(health);
  const needAttention = health === null ? 0 : attentionCount(health.projects);
  const serving = health === null ? 0 : servingCount(health.projects);

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 text-sm text-slate-600 dark:text-slate-400">
          조회 전용 화면입니다. 배포 확정 · 롤백 · 중지는 각 프로젝트 상세 화면에서만 할 수 있습니다.
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
      {loading && <Skeleton label="배포 상태 불러오는 중" className="mt-4" />}

      {health && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile label="프로젝트 수" value={health.projects.length} note="전체 등록 프로젝트" />
            <StatTile
              label="현재 운영 중"
              tone={serving > 0 ? "success" : "neutral"}
              value={serving}
              note="활성 배포가 있는 프로젝트"
            />
            <StatTile
              label="주의 필요"
              tone={needAttention > 0 ? "danger" : "neutral"}
              value={needAttention}
              note="실패 · 진행 중 · 확인 필요"
            />
            <StatTile
              label="최근 배포 실패"
              tone={health.recent_failures.length > 0 ? "danger" : "neutral"}
              value={health.recent_failures.length}
              note={
                truncationNotice
                  ? `상한 ${health.recent_failures_limit}건 도달 — 더 있음`
                  : `최근 ${health.recent_failures_limit}건까지 기록`
              }
            />
          </div>

          <div className="mt-4 grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1.65fr)_minmax(18rem,0.8fr)]">
            <Card className="min-w-0 p-4">
              <h3 className="text-base font-semibold text-slate-900 dark:text-white">
                서비스 배포 상태 ({health.projects.length})
              </h3>
              {health.projects.length === 0 ? (
                <EmptyState className="mt-3" title="프로젝트가 없습니다" />
              ) : (
                <Table className="mt-3 min-w-xl">
                  <Table.Head>
                    <Table.Row>
                      <Table.HeaderCell scope="col">프로젝트</Table.HeaderCell>
                      <Table.HeaderCell scope="col">상태</Table.HeaderCell>
                      <Table.HeaderCell scope="col">운영 중 배포</Table.HeaderCell>
                      <Table.HeaderCell scope="col">최신 배포</Table.HeaderCell>
                    </Table.Row>
                  </Table.Head>
                  <Table.Body>
                    {/* Both slots are shown, never folded into one: the derived state is a
                        summary, not a replacement. */}
                    {sortByAttention(health.projects).map((project) => {
                      const state = deploymentHealthState(project);
                      return (
                        <Table.Row key={project.project_id}>
                          <Table.Cell className="min-w-40">
                            <strong className="font-semibold text-slate-900 dark:text-white">
                              {visibleControls(project.project_name)}
                            </strong>
                            <div>
                              <TextLink to={`/projects/${project.project_id}`} className="whitespace-nowrap text-xs">
                                프로젝트 열기
                              </TextLink>
                            </div>
                          </Table.Cell>
                          <Table.Cell>
                            <Badge tone={DEPLOYMENT_HEALTH_COLORS[state]}>{DEPLOYMENT_HEALTH_LABELS[state]}</Badge>
                          </Table.Cell>
                          <Table.Cell>
                            <DeploymentCell deployment={project.active_deployment} />
                          </Table.Cell>
                          <Table.Cell>
                            <DeploymentCell deployment={project.latest_deployment} />
                          </Table.Cell>
                        </Table.Row>
                      );
                    })}
                  </Table.Body>
                </Table>
              )}
            </Card>

            <Card className="min-w-0 p-4">
              <h3 className="text-base font-semibold text-slate-900 dark:text-white">
                최근 배포 실패 ({health.recent_failures.length})
              </h3>
              {truncationNotice && (
                <Alert variant="warning" className="mt-2">
                  {truncationNotice}
                </Alert>
              )}
              {health.recent_failures.length === 0 ? (
                <EmptyState className="mt-3" title="기록된 배포 실패가 없습니다" />
              ) : (
                // Bounded and scrollable inside the card (50 failures would otherwise outgrow the
                // table). A scroll region needs to be reachable from the keyboard and to have a name.
                <div
                  role="region"
                  aria-label="최근 배포 실패 목록"
                  tabIndex={0}
                  className={`mt-3 max-h-96 overflow-y-auto rounded-md px-2 ${FOCUS_RING}`}
                >
                  <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                    {health.recent_failures.map((failure) => (
                      <li key={failure.id} className="space-y-1 py-2.5 first:pt-0">
                        <TextLink to={`/projects/${failure.project_id}`} className="text-sm">
                          {projectNameFor(health, failure.project_id)}
                        </TextLink>
                        <div className="text-xs text-slate-600 dark:text-slate-400">
                          {new Date(failure.created_at).toLocaleString()}
                        </div>
                        <Badge tone="danger">
                          {failure.failure_reason === null
                            ? "사유 없음"
                            : deploymentFailureReasonLabel(failure.failure_reason)}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

/** Failure entries carry `project_id` and no name — `projects` is the complete
 * project list, so the mapping is held here rather than duplicated into a second
 * deployment-ish response shape. */
function projectNameFor(health: DeploymentHealth, projectId: string): string {
  const found = health.projects.find((p) => p.project_id === projectId);
  return found === undefined ? projectId : visibleControls(found.project_name);
}
