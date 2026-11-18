import { useEffect, useState } from "react";

import { ApiError } from "../../api/client";
import { agentEngineApi } from "../../api/agentEngine";
import { canSelectAgentEngine } from "../../auth/roles";
import { useIdentity } from "../../context/IdentityContext";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { Table } from "../../components/ui/Table";
import type { AgentEngineSelection } from "../../types/api";
import {
  CHANGE_SCOPE_NOTICE,
  SELECTED_BUT_UNAVAILABLE_NOTICE,
  changeSuccessMessage,
  engineChangeFailureMessage,
  engineRows,
  selectionProvenance,
} from "./agentEngine";
import { panelErrorMessage } from "./panelError";

/**
 * Agent engine selection (ADR 0035 §Decision 5).
 *
 * Each engine is shown with its availability (the only fact about its key), its configured model
 * and image, and whether it is the one builds use now. An admin selects an available engine; an
 * unavailable one shows why and offers no control. The change applies to builds claimed after it,
 * and the panel says so. Every change is audited backend-side with actor, old and new engine.
 *
 * The write is `PATCH /api/platform/agent-engine`, not a console path: the console router stays
 * read-only.
 */
export function AgentEnginePanel() {
  const { currentUser } = useIdentity();
  const mayChoose = canSelectAgentEngine(currentUser);

  const [selection, setSelection] = useState<AgentEngineSelection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changeError, setChangeError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setSelection(await agentEngineApi.get());
    } catch (err) {
      setError(panelErrorMessage(err, "에이전트 엔진 설정을 불러오지 못했습니다."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function choose(engine: string) {
    setChangeError(null);
    setNotice(null);
    setSaving(engine);
    try {
      const updated = await agentEngineApi.select({ engine });
      setSelection(updated);
      setNotice(changeSuccessMessage(updated));
    } catch (err) {
      setChangeError(
        err instanceof ApiError
          ? engineChangeFailureMessage(err.status)
          : panelErrorMessage(err, "에이전트 엔진 변경에 실패했습니다."),
      );
    } finally {
      setSaving(null);
    }
  }

  const rows = selection ? engineRows(selection) : [];
  const provenance = selection ? selectionProvenance(selection) : null;

  return (
    <div>
      <p className="text-sm text-slate-600 dark:text-slate-400">빌드를 실행하는 에이전트 엔진을 고릅니다.</p>
      <Alert variant="warning" className="mt-3">
        {CHANGE_SCOPE_NOTICE}
      </Alert>
      {!mayChoose && (
        <Alert variant="warning" className="mt-3">
          엔진 선택은 admin만 가능합니다. 아래 목록은 조회 전용입니다.
        </Alert>
      )}

      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {changeError && (
        <Alert variant="error" live className="mt-3">
          {changeError}
        </Alert>
      )}
      {/* A successful change is the acting user's own action: a polite status, not an alert.
          The region is always mounted so that its text change is announced. */}
      <div role="status" className="mt-3 empty:hidden">
        {notice && <Alert variant="success">{notice}</Alert>}
      </div>

      {loading ? (
        <Skeleton label="에이전트 엔진 불러오는 중" className="mt-4" />
      ) : (
        selection && (
          <>
            <Table className="mt-3">
              <Table.Head>
                <Table.Row>
                  <Table.HeaderCell>엔진</Table.HeaderCell>
                  <Table.HeaderCell>상태</Table.HeaderCell>
                  <Table.HeaderCell>모델</Table.HeaderCell>
                  <Table.HeaderCell>샌드박스 이미지</Table.HeaderCell>
                  <Table.HeaderCell>선택</Table.HeaderCell>
                </Table.Row>
              </Table.Head>
              <Table.Body>
                {rows.map((row) => (
                  <Table.Row key={row.engine}>
                    <Table.Cell>
                      {row.label}
                      <div className="text-[11px] text-slate-500 dark:text-slate-400">{row.engine}</div>
                    </Table.Cell>
                    <Table.Cell>
                      <Badge tone={row.available ? "success" : "warning"}>{row.availabilityText}</Badge>
                    </Table.Cell>
                    <Table.Cell>{row.modelText}</Table.Cell>
                    <Table.Cell>
                      <code className="text-xs">{row.image}</code>
                    </Table.Cell>
                    <Table.Cell>
                      <div className="flex items-center gap-2">
                        {row.selected && <Badge tone="accent">선택됨</Badge>}
                        {mayChoose && row.canSelect && (
                          <Button
                            variant="secondary"
                            busy={saving === row.engine}
                            disabled={saving !== null}
                            onClick={() => choose(row.engine)}
                            aria-label={`${row.label} 엔진 선택`}
                          >
                            {saving === row.engine ? "변경 중..." : "선택"}
                          </Button>
                        )}
                      </div>
                      {row.selectedButUnavailable && (
                        <Alert variant="warning" className="mt-2">
                          {SELECTED_BUT_UNAVAILABLE_NOTICE}
                        </Alert>
                      )}
                    </Table.Cell>
                  </Table.Row>
                ))}
              </Table.Body>
            </Table>
            {provenance && <p className="mt-3 text-sm text-slate-600 dark:text-slate-400">{provenance}</p>}
          </>
        )
      )}
    </div>
  );
}
