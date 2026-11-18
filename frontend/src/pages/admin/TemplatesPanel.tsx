import { useEffect, useState } from "react";

import { templatesApi } from "../../api/templates";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Card } from "../../components/ui/Card";
import { DescriptionList, type DescriptionListItem } from "../../components/ui/DescriptionList";
import { EmptyState } from "../../components/ui/EmptyState";
import { ListRow } from "../../components/ui/ListRow";
import { Skeleton } from "../../components/ui/Skeleton";
import { Table } from "../../components/ui/Table";
import type { Template } from "../../types/api";
import { panelErrorMessage } from "./panelError";

/**
 * Templates: list and inspect, **read-only**.
 * Template authoring and a second template
 * shape are a later slice, so there is no create, edit, enable or disable control
 * here — not even a disabled one, which rule 3 rejects as worse than a plainly
 * read-only display.
 */
const FIELD_LABELS: Array<{ key: keyof Template; label: string }> = [
  { key: "id", label: "ID" },
  { key: "name", label: "이름" },
  { key: "stack", label: "스택" },
  { key: "version", label: "버전" },
  { key: "dockerfile_ref", label: "Dockerfile 참조" },
  { key: "deploy_config_ref", label: "배포 설정 참조" },
  { key: "policy_profile", label: "정책 프로파일" },
  { key: "enabled", label: "사용 가능" },
];

function fieldValue(template: Template, key: keyof Template): string {
  const value = template[key];
  if (typeof value === "boolean") return value ? "예" : "아니오";
  return String(value);
}

export function TemplatesPanel() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selected, setSelected] = useState<Template | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setTemplates(await templatesApi.list());
      } catch (err) {
        setError(panelErrorMessage(err, "템플릿 목록을 불러오지 못했습니다."));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function inspect(id: string) {
    setError(null);
    try {
      // A real read of `GET /templates/{id}` rather than reusing the list row, so
      // the detail pane shows what the platform would serve for that id now.
      setSelected(await templatesApi.get(id));
    } catch (err) {
      setError(panelErrorMessage(err, "템플릿을 불러오지 못했습니다."));
    }
  }

  const detailItems: DescriptionListItem[] = selected
    ? FIELD_LABELS.map(({ key, label }) => ({ term: label, detail: fieldValue(selected, key) }))
    : [];

  return (
    <div>
      <p className="text-sm text-slate-600 dark:text-slate-400">조회 전용입니다. 템플릿 추가·수정은 이 화면에서 할 수 없습니다.</p>
      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {loading ? (
        <Skeleton label="템플릿 목록 불러오는 중" className="mt-4" />
      ) : templates.length === 0 ? (
        <EmptyState className="mt-4" title="등록된 템플릿이 없습니다" />
      ) : (
        <div className="mt-3 flex flex-col gap-6 lg:flex-row lg:items-start">
          <Table className="lg:flex-1">
            <Table.Head>
              <Table.Row>
                <Table.HeaderCell>이름</Table.HeaderCell>
                <Table.HeaderCell>스택</Table.HeaderCell>
                <Table.HeaderCell>버전</Table.HeaderCell>
                <Table.HeaderCell>사용 가능</Table.HeaderCell>
              </Table.Row>
            </Table.Head>
            <Table.Body>
              {templates.map((template) => {
                const isSelected = template.id === selected?.id;
                return (
                  <ListRow
                    key={template.id}
                    as="tr"
                    selected={isSelected}
                    ariaLabel={`${template.name} 상세 보기`}
                    onActivate={() => inspect(template.id)}
                  >
                    <Table.Cell>{template.name}</Table.Cell>
                    <Table.Cell>{template.stack}</Table.Cell>
                    <Table.Cell>{template.version}</Table.Cell>
                    <Table.Cell>
                      {template.enabled ? (
                        <Badge tone="success">사용 가능</Badge>
                      ) : (
                        <Badge tone="neutral">사용 중지</Badge>
                      )}
                    </Table.Cell>
                  </ListRow>
                );
              })}
            </Table.Body>
          </Table>

          {selected && (
            <Card className="p-4 lg:flex-1">
              <h3 className="mb-2 text-base font-semibold text-slate-900 dark:text-white">템플릿 상세</h3>
              <DescriptionList items={detailItems} />
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
