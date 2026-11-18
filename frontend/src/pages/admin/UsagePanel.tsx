import { useState, type ReactNode } from "react";

import { projectsApi } from "../../api/projects";
import { usageApi } from "../../api/usage";
import { usersApi } from "../../api/users";
import { canEditQuota } from "../../auth/roles";
import { useIdentity } from "../../context/IdentityContext";
import { useLoaderOnChange } from "../../loading/useLoader";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Panel } from "../../components/ui/Panel";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatTile } from "../../components/ui/StatTile";
import { Table } from "../../components/ui/Table";
import { cx } from "../../components/ui/cx";
import { visibleControls } from "../../components/ui/visibleControls";
import type { PlatformUsage, SubjectUsage, UsageReport } from "../../types/api";
import { CostDashboardSection } from "./CostDashboardSection";
import { panelErrorMessage } from "./panelError";
import {
  LimitSource,
  QUOTA_WORDING,
  UsageSubjectKind,
  formatMonthWindow,
  formatUsd,
  isUnattributedLine,
  limitDisplay,
  parseQuotaInput,
  quotaApplyLabel,
  quotaApplyText,
  quotaInputLabel,
  quotaInputValue,
  recentMonths,
  spendDisplay,
} from "./usageDisplay";

/** How many months the selector offers, including the current one. Historical
 * months stay queryable — a rollover is a new window, not a deletion
 *. */
const MONTH_OPTIONS = 12;

interface UsageData {
  summary: PlatformUsage;
  projectReport: UsageReport;
  userReport: UsageReport;
  names: Record<string, string>;
}

/** Every read the tab shows for one month, as one load: a rollup and its window must never be on
 * screen together from two different requests, and a month switch aborts all five. */
async function fetchUsage(month: string, signal: AbortSignal): Promise<UsageData> {
  const [summary, projectReport, userReport, projectList, userList] = await Promise.all([
    usageApi.summary(month, { signal }),
    usageApi.projects(month, { signal }),
    usageApi.users(month, { signal }),
    projectsApi.list({ signal }),
    usersApi.list({ signal }),
  ]);
  return {
    summary,
    projectReport,
    userReport,
    names: {
      ...Object.fromEntries(projectList.map((p) => [p.id, visibleControls(p.name)])),
      ...Object.fromEntries(userList.map((u) => [u.id, visibleControls(u.name)])),
    },
  };
}

/** Spend, rendered so that unmeasured is never a measured `$0.00`
 * The classification is
 * `spendDisplay`'s; this only paints it. */
function Spend({ line }: { line: SubjectUsage }) {
  const display = spendDisplay(line);

  if (display.kind === "unmeasured") {
    // No amount at all: every build in the window is unmeasured, so the `0.0`
    // the API sums to is the absence of a measurement, not a figure.
    return (
      <Badge tone="warning" title={QUOTA_WORDING.unmeasuredExplanation}>
        {QUOTA_WORDING.unmeasured} · 빌드 {display.unmeasuredCount}건
      </Badge>
    );
  }

  return (
    <span>
      <strong className="text-slate-900 dark:text-white">{formatUsd(line.spend_usd)}</strong>
      {display.kind === "partially-unmeasured" && (
        <Badge tone="warning" className="ml-2" title={QUOTA_WORDING.unmeasuredExplanation}>
          {QUOTA_WORDING.unmeasured} {display.unmeasuredCount}건 제외
        </Badge>
      )}
      {display.kind === "no-builds" && (
        <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">{QUOTA_WORDING.noBuilds}</span>
      )}
    </span>
  );
}

/** The effective limit **and where it came from** — an operator who cannot tell
 * an override from the inherited default does not know whether clearing it does
 * anything. */
function Limit({ line }: { line: SubjectUsage }) {
  const display = limitDisplay(line.effective_limit_usd, line.limit_source);

  if (display.kind === "none") {
    return (
      <span className="text-xs text-slate-500 dark:text-slate-400" title={QUOTA_WORDING.unattributedExplanation}>
        {QUOTA_WORDING.noLimit}
      </span>
    );
  }

  const sourceLabel =
    display.kind === "override"
      ? QUOTA_WORDING.overrideLabel
      : display.kind === "default"
        ? QUOTA_WORDING.defaultLabel
        : QUOTA_WORDING.unlabelledLimitNote;

  return (
    <span>
      {display.amount}{" "}
      <span
        className={cx(
          "text-xs",
          display.kind === "override" ? "text-accent dark:text-indigo-400" : "text-slate-500 dark:text-slate-400",
        )}
      >
        ({sourceLabel})
      </span>
    </span>
  );
}

/** In breach or not. Breach is a derived read-time comparison that **warns** —
 * nothing was blocked, stopped or refused; the
 * marker's own label says so ("경고"), and the rule itself lives in the specs. */
function Breach({ inBreach }: { inBreach: boolean }) {
  if (!inBreach) {
    return <span className="text-xs text-slate-500 dark:text-slate-400">{QUOTA_WORDING.withinLimit}</span>;
  }
  return <Badge tone="danger">{QUOTA_WORDING.breach}</Badge>;
}

/** The breach tile's value: two deliberate lines, so it reads the same at any tile width.
 * The hidden separator keeps the spoken form "프로젝트 N, 사용자 M" (the lines are
 * separate blocks, so a reader would otherwise run the two counts together). */
function BreachCounts({ projects, users }: { projects: number; users: number }) {
  return (
    <>
      <div>프로젝트 {projects}</div>
      <span className="sr-only">, </span>
      <div>사용자 {users}</div>
    </>
  );
}

export function UsagePanel() {
  const { currentUser } = useIdentity();
  const mayEditQuota = canEditQuota(currentUser);

  const [month, setMonth] = useState<string>("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [quotaNotice, setQuotaNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  // Bumped by the refresh button so the cost dashboard section re-reads with the rest.
  const [reloadToken, setReloadToken] = useState(0);

  // Re-reads every figure when the month changes, and only the newest read may be shown: a slow
  // answer for the month the operator has already left is aborted and then ignored (before,
  // the last response won, not the last choice). The previous month's figures are cleared at the
  // switch, so nothing from it sits under the new month in the selector.
  const usage = useLoaderOnChange(
    (signal) => fetchUsage(month, signal),
    [month],
    (err) => panelErrorMessage(err, "사용량을 불러오지 못했습니다."),
  );
  const { loading, error } = usage;
  const summary = usage.data?.summary ?? null;
  const projectReport = usage.data?.projectReport ?? null;
  const userReport = usage.data?.userReport ?? null;
  const names = usage.data?.names ?? {};

  async function applyQuota(kind: string, subjectId: string) {
    const parsed = parseQuotaInput(overrides[subjectId] ?? "");
    setQuotaError(null);
    setQuotaNotice(null);

    if (parsed.kind === "invalid") {
      // Refused client-side with the reason next to the field. The backend
      // refuses it too (`services/usage.set_monthly_quota`) — this is not the
      // only check.
      setQuotaError(parsed.reason);
      return;
    }

    const value = parsed.kind === "clear" ? null : parsed.value;
    setSaving(subjectId);
    try {
      const updated =
        kind === UsageSubjectKind.PROJECT
          ? await usageApi.setProjectQuota(subjectId, { monthly_quota_usd: value })
          : await usageApi.setUserQuota(subjectId, { monthly_quota_usd: value });
      setQuotaNotice(
        value === null
          ? `${names[subjectId] ?? subjectId}의 개별 지정을 해제했습니다. 이제 ${QUOTA_WORDING.defaultLabel} ${formatUsd(updated.effective_limit_usd)}가 적용됩니다.`
          : `${names[subjectId] ?? subjectId}의 한도를 ${formatUsd(updated.effective_limit_usd)}로 지정했습니다.`,
      );
      // The refreshed figures replace the old ones in place; the quota the operator just set
      // is on the row it was typed on, so the table is not cleared for it.
      await usage.reload({ keepData: true });
    } catch (err) {
      setQuotaError(panelErrorMessage(err, "한도 변경에 실패했습니다."));
    } finally {
      setSaving(null);
    }
  }

  function rows(report: UsageReport, kind: string) {
    return report.subjects.map((line) => {
      const unattributed = isUnattributedLine(line);
      const subjectId = line.subject_id;
      // The row's name as the first cell shows it: what the quota controls below are named for.
      const rowName = subjectId === null ? "" : (names[subjectId] ?? subjectId);
      return (
        <Table.Row
          key={subjectId ?? "unattributed"}
          className={unattributed ? "bg-amber-50 dark:bg-amber-950/20" : undefined}
        >
          <Table.Cell>
            {unattributed ? (
              <>
                <strong className="text-slate-900 dark:text-white">{QUOTA_WORDING.unattributed}</strong>
                <div className="text-xs text-slate-500 dark:text-slate-400">
                  {QUOTA_WORDING.unattributedExplanation}
                </div>
              </>
            ) : (
              <>
                {names[subjectId ?? ""] ?? subjectId}
                <div className="text-[11px] text-slate-500 dark:text-slate-400">{subjectId}</div>
              </>
            )}
          </Table.Cell>
          <Table.Cell>
            <Spend line={line} />
          </Table.Cell>
          <Table.Cell>{line.build_count}</Table.Cell>
          <Table.Cell>
            <Limit line={line} />
          </Table.Cell>
          <Table.Cell>
            <Breach inBreach={line.in_breach} />
          </Table.Cell>
          {mayEditQuota && (
            <Table.Cell>
              {unattributed || subjectId === null ? (
                <span className="text-xs text-slate-500 dark:text-slate-400">{QUOTA_WORDING.noLimit}</span>
              ) : (
                <div className="flex items-center gap-2">
                  {/* Input is w-full, so its width comes from this box; shrink-0 keeps the panel's
                      padding from squeezing it (the table's own scroller takes the overflow). */}
                  <div className="w-32 shrink-0">
                    <Input
                      value={overrides[subjectId] ?? ""}
                      onChange={(e) => setOverrides({ ...overrides, [subjectId]: e.target.value })}
                      placeholder={
                        line.limit_source === LimitSource.OVERRIDE
                          ? quotaInputValue(line.effective_limit_usd)
                          : "비우면 기본값"
                      }
                      aria-label={quotaInputLabel(rowName)}
                    />
                  </div>
                  <Button
                    variant="secondary"
                    busy={saving === subjectId}
                    aria-label={quotaApplyLabel(rowName, saving === subjectId)}
                    onClick={() => applyQuota(kind, subjectId)}
                  >
                    {quotaApplyText(saving === subjectId)}
                  </Button>
                </div>
              )}
            </Table.Cell>
          )}
        </Table.Row>
      );
    });
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex flex-wrap items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
          <span className="whitespace-nowrap">조회 월</span>
          <Select value={month} onChange={(e) => setMonth(e.target.value)} fit>
            <option value="">이번 달</option>
            {recentMonths(new Date(), MONTH_OPTIONS).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
        </label>
        <Button
          variant="secondary"
          busy={loading}
          onClick={() => {
            setReloadToken((n) => n + 1);
            void usage.reload({ keepData: true });
          }}
        >
          새로 고침
        </Button>
      </div>

      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {loading && <Skeleton label="사용량 불러오는 중" className="mt-4" />}

      {summary && (
        <section className="mt-4">
          <h3 className="text-lg font-semibold text-slate-900 dark:text-white">
            플랫폼 합계{" "}
            <span className="text-sm font-normal text-slate-600 dark:text-slate-400">— {formatMonthWindow(summary.window)}</span>
          </h3>
          <p className="-mt-1 text-xs text-slate-600 dark:text-slate-400">
            집계 구간: {new Date(summary.window.starts_at).toLocaleString()} ~{" "}
            {new Date(summary.window.ends_at).toLocaleString()} (끝 시각 제외)
          </p>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="전체 지출"
              value={formatUsd(summary.total_spend_usd)}
              note={
                <>
                  빌드 {summary.build_count}건
                  {summary.unmeasured_build_count > 0 && (
                    <>
                      {" · "}
                      {QUOTA_WORDING.unmeasured} {summary.unmeasured_build_count}건
                    </>
                  )}
                </>
              }
            />
            <StatTile
              label={QUOTA_WORDING.unattributed}
              value={formatUsd(summary.unattributed_spend_usd)}
              note={QUOTA_WORDING.unattributedExplanation}
            />
            <StatTile label={`${QUOTA_WORDING.defaultLabel} 한도`} value={formatUsd(summary.default_limit_usd)} />
            <StatTile
              label={QUOTA_WORDING.breach}
              tone={summary.projects_in_breach.length + summary.users_in_breach.length > 0 ? "danger" : "neutral"}
              value={
                <BreachCounts projects={summary.projects_in_breach.length} users={summary.users_in_breach.length} />
              }
            />
          </div>
        </section>
      )}

      {quotaError && (
        <Alert variant="error" live className="mt-3">
          {quotaError}
        </Alert>
      )}
      {quotaNotice && (
        <Alert variant="success" live className="mt-3">
          {quotaNotice}
        </Alert>
      )}

      {projectReport && (
        <Panel
          title="프로젝트별"
          titleNote={`— ${formatMonthWindow(projectReport.window)}`}
          headingLevel={3}
          className="mt-4"
        >
          <UsageTable mayEditQuota={mayEditQuota} subjectLabel="프로젝트">
            {rows(projectReport, UsageSubjectKind.PROJECT)}
          </UsageTable>
        </Panel>
      )}

      {userReport && (
        <Panel
          title="사용자별"
          titleNote={`— ${formatMonthWindow(userReport.window)}`}
          headingLevel={3}
          className="mt-4"
        >
          <UsageTable mayEditQuota={mayEditQuota} subjectLabel="사용자">
            {rows(userReport, UsageSubjectKind.USER)}
          </UsageTable>
        </Panel>
      )}

      <CostDashboardSection month={month} reloadToken={reloadToken} />
    </div>
  );
}

function UsageTable({
  subjectLabel,
  mayEditQuota,
  children,
}: {
  subjectLabel: string;
  mayEditQuota: boolean;
  children: ReactNode;
}) {
  return (
    <Table className="mt-3">
      <Table.Head>
        <Table.Row>
          <Table.HeaderCell>{subjectLabel}</Table.HeaderCell>
          <Table.HeaderCell>지출</Table.HeaderCell>
          <Table.HeaderCell>빌드</Table.HeaderCell>
          <Table.HeaderCell>월 한도 (출처)</Table.HeaderCell>
          <Table.HeaderCell>상태</Table.HeaderCell>
          {mayEditQuota && <Table.HeaderCell>개별 지정 (비우면 해제)</Table.HeaderCell>}
        </Table.Row>
      </Table.Head>
      <Table.Body>{children}</Table.Body>
    </Table>
  );
}
