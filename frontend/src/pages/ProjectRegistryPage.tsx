import { useEffect, useState } from "react";

import { projectsApi } from "../api/projects";
import { usersApi } from "../api/users";
import { StatusBadge } from "../components/StatusBadge";
import { Alert, ButtonLink, Card, EmptyState, Skeleton, Table, TextLink } from "../components/ui";
import { visibleControls } from "../components/ui/visibleControls";
import type { Project } from "../types/api";
import { ownerNameFor, ownerNamesFrom, UNRESOLVED_OWNER_LABEL, type OwnerNames } from "./ownerNames";

/**
 * What this page says when `GET /projects` fails.
 * There used to be no failure branch at all: a rejected
 * fetch left `projects` at its initial `[]` and rendered the *empty-list*
 * copy — an error looking like success, the same class of defect
 * `ApprovalConsolePage.load` had. No existing
 * `*FailureMessage` helper lived in this file to reuse (unlike
 * `ProjectDetailPage`/`ApprovalConsolePage`, this page has exactly one
 * fetch and no other action), so this is the new one.
 */
export function projectRegistryFailureMessage(_err: unknown, fallback: string): string {
  return fallback;
}

const LOAD_FAILURE_FALLBACK = "프로젝트 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";

const NO_OWNER_NAMES: OwnerNames = ownerNamesFrom([]);

export function ProjectRegistryPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [ownerNames, setOwnerNames] = useState<OwnerNames>(NO_OWNER_NAMES);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    // Both reads settle before the rows show, so an owner never flashes as unresolved and then
    // changes. The directory is best-effort: if it fails the rows still list, with the
    // owner shown as unresolved, never as an id.
    Promise.all([
      projectsApi.list(),
      usersApi.list().then(ownerNamesFrom, () => NO_OWNER_NAMES),
    ])
      .then(([list, names]) => {
        setProjects(list);
        setOwnerNames(names);
      })
      .catch((err) => setLoadError(projectRegistryFailureMessage(err, LOAD_FAILURE_FALLBACK)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <Skeleton label="프로젝트 목록을 불러오는 중" />;
  }

  if (loadError) {
    return (
      <Alert variant="error" live>
        {loadError}
      </Alert>
    );
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-600 dark:text-slate-300">
          프로젝트 {projects.length}개
        </p>
        <ButtonLink to="/projects/new">새 요청</ButtonLink>
      </div>
      {projects.length === 0 ? (
        <EmptyState
          title="아직 생성된 프로젝트가 없습니다"
          description={
            <>
              "새 요청"에서 첫 요청을 제출해보세요.
            </>
          }
        />
      ) : (
        <Card className="p-2">
          <Table className="min-w-2xl">
            <Table.Head>
              <Table.Row>
                <Table.HeaderCell>이름</Table.HeaderCell>
                <Table.HeaderCell>소유자</Table.HeaderCell>
                <Table.HeaderCell>상태</Table.HeaderCell>
                <Table.HeaderCell>생성일</Table.HeaderCell>
                <Table.HeaderCell>최근 활동</Table.HeaderCell>
              </Table.Row>
            </Table.Head>
            <Table.Body>
              {projects.map((project) => {
                const owner = ownerNameFor(ownerNames, project.owner_id);
                return (
                  <Table.Row key={project.id}>
                    <Table.Cell>
                      <TextLink to={`/projects/${project.id}`}>{visibleControls(project.name)}</TextLink>
                    </Table.Cell>
                    <Table.Cell>
                      {owner === null ? (
                        <span className="text-slate-500 dark:text-slate-400">{UNRESOLVED_OWNER_LABEL}</span>
                      ) : (
                        visibleControls(owner)
                      )}
                    </Table.Cell>
                    <Table.Cell>
                      <StatusBadge status={project.status} />
                    </Table.Cell>
                    <Table.Cell className="whitespace-nowrap">{new Date(project.created_at).toLocaleString()}</Table.Cell>
                    <Table.Cell className="whitespace-nowrap">{new Date(project.last_active_at).toLocaleString()}</Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </Table>
        </Card>
      )}
    </div>
  );
}
