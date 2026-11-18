
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { Table } from "../../components/ui/Table";
import { TextLink } from "../../components/ui/TextLink";
import type { CostDashboard } from "../../types/api";
import { DailySpendChart } from "./DailySpendChart";
import {
  DASHBOARD_WORDING,
  expensiveRows,
  hasBuilds,
  meteringCaveatNote,
  outcomeRows,
} from "./costDashboard";

/**
 * The cost dashboard for one loaded response.
 * Presentational, and headless at the top: its title and month window are the `Panel` heading
 * `CostDashboardSection` puts around it, so the three `<h4>` blocks below are one level under that.
 * It is handed the response and the id -> name lookups, so it can be
 * mounted without a network.
 *
 * The metering caveat is a plain static `Alert` with no `live`: nothing just happened,
 * so it must not interrupt a screen reader.
 */
export function CostDashboardView({
  dashboard,
  projectNames,
  userNames,
}: {
  dashboard: CostDashboard;
  projectNames: Record<string, string>;
  userNames: Record<string, string>;
}) {
  const caveat = meteringCaveatNote(dashboard.metering_caveat);
  const outcomes = outcomeRows(dashboard.outcomes);
  const expensive = expensiveRows(dashboard.expensive_builds, projectNames, dashboard.window.timezone, userNames);

  return (
    <div>
      <p className="mt-3 text-xs text-slate-600 dark:text-slate-400">{DASHBOARD_WORDING.noEnforcement}</p>

      {caveat && (
        <Alert variant="warning" className="mt-2">
          {caveat}
        </Alert>
      )}

      <section className="mt-3" aria-labelledby="cost-daily-heading">
        <h4 id="cost-daily-heading" className="text-sm font-semibold text-slate-900 dark:text-white">
          {DASHBOARD_WORDING.dailyTitle}
        </h4>
        {hasBuilds(dashboard) ? (
          <DailySpendChart days={dashboard.days} />
        ) : (
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">{DASHBOARD_WORDING.noBuildsMonth}</p>
        )}
      </section>

      <section className="mt-5" aria-labelledby="cost-outcomes-heading">
        <h4 id="cost-outcomes-heading" className="text-sm font-semibold text-slate-900 dark:text-white">
          {DASHBOARD_WORDING.outcomesTitle}
        </h4>
        {outcomes.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">{DASHBOARD_WORDING.noBuildsMonth}</p>
        ) : (
          <Table className="mt-2">
            <Table.Head>
              <Table.Row>
                <Table.HeaderCell scope="col">결과</Table.HeaderCell>
                <Table.HeaderCell scope="col">지출</Table.HeaderCell>
                <Table.HeaderCell scope="col">빌드</Table.HeaderCell>
              </Table.Row>
            </Table.Head>
            <Table.Body>
              {outcomes.flatMap((outcome) => [
                <Table.Row key={outcome.key}>
                  <Table.Cell>
                    <strong className="text-slate-900 dark:text-white">{outcome.label}</strong>
                  </Table.Cell>
                  <Table.Cell>{outcome.spend}</Table.Cell>
                  <Table.Cell>{outcome.builds}</Table.Cell>
                </Table.Row>,
                ...outcome.reasons.map((reason) => (
                  <Table.Row key={`${outcome.key}:${reason.key}`}>
                    <Table.Cell className="pl-6 text-slate-700 dark:text-slate-300">└ {reason.label}</Table.Cell>
                    <Table.Cell>{reason.spend}</Table.Cell>
                    <Table.Cell>{reason.builds}</Table.Cell>
                  </Table.Row>
                )),
              ])}
            </Table.Body>
          </Table>
        )}
      </section>

      <section className="mt-5" aria-labelledby="cost-expensive-heading">
        <h4 id="cost-expensive-heading" className="text-sm font-semibold text-slate-900 dark:text-white">
          {DASHBOARD_WORDING.expensiveTitle}
        </h4>
        <p className="text-xs text-slate-600 dark:text-slate-400">{DASHBOARD_WORDING.unmeasuredNotListed}</p>
        {expensive.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">{DASHBOARD_WORDING.noBuildsMonth}</p>
        ) : (
          <Table className="mt-2">
            <Table.Head>
              <Table.Row>
                <Table.HeaderCell scope="col">프로젝트</Table.HeaderCell>
                <Table.HeaderCell scope="col">비용</Table.HeaderCell>
                <Table.HeaderCell scope="col">결과</Table.HeaderCell>
                <Table.HeaderCell scope="col">종료</Table.HeaderCell>
                <Table.HeaderCell scope="col">요청자</Table.HeaderCell>
              </Table.Row>
            </Table.Head>
            <Table.Body>
              {expensive.map((row) => (
                <Table.Row key={row.buildId}>
                  <Table.Cell>
                    <TextLink to={`/projects/${row.projectId}`}>{row.projectName}</TextLink>
                    <div className="text-[11px] text-slate-500 dark:text-slate-400">{row.buildId}</div>
                  </Table.Cell>
                  <Table.Cell>
                    <strong className="text-slate-900 dark:text-white">{row.cost}</strong>
                    <div className="text-[11px] text-slate-500 dark:text-slate-400">토큰 {row.tokens}</div>
                  </Table.Cell>
                  <Table.Cell>
                    {row.reason === null ? (
                      row.status
                    ) : (
                      <Badge tone="danger">
                        {row.status} · {row.reason}
                      </Badge>
                    )}
                  </Table.Cell>
                  <Table.Cell className="text-xs">{row.endedAt}</Table.Cell>
                  <Table.Cell>{row.requester}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </section>
    </div>
  );
}
