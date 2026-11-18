import { projectsApi } from "../../api/projects";
import { usageApi } from "../../api/usage";
import { usersApi } from "../../api/users";
import { Alert } from "../../components/ui/Alert";
import { Panel } from "../../components/ui/Panel";
import { Skeleton } from "../../components/ui/Skeleton";
import { visibleControls } from "../../components/ui/visibleControls";
import { useLoaderOnChange } from "../../loading/useLoader";
import type { CostDashboard } from "../../types/api";
import { CostDashboardView } from "./CostDashboardView";
import { DASHBOARD_WORDING, EXPENSIVE_BUILDS_SHOWN } from "./costDashboard";
import { panelErrorMessage } from "./panelError";
import { formatMonthWindow } from "./usageDisplay";

interface DashboardData {
  dashboard: CostDashboard;
  projectNames: Record<string, string>;
  userNames: Record<string, string>;
}

async function fetchDashboard(month: string, signal: AbortSignal): Promise<DashboardData> {
  const [dashboard, projects, users] = await Promise.all([
    usageApi.dashboard(month, EXPENSIVE_BUILDS_SHOWN, { signal }),
    projectsApi.list({ signal }),
    usersApi.list({ signal }),
  ]);
  return {
    dashboard,
    projectNames: Object.fromEntries(projects.map((p) => [p.id, visibleControls(p.name)])),
    userNames: Object.fromEntries(users.map((u) => [u.id, visibleControls(u.name)])),
  };
}

/**
 * The loader around `CostDashboardView`, in its own `Panel` (the title and the month window it
 * names are this panel's heading, an `<h3>` under the usage tab's visually-hidden `<h2>`, so the
 * view's three `<h4>` sub-sections sit one level below it): one read of `GET /usage/dashboard` for the
 * month the usage tab has selected (same `""` = current-month semantics as
 * `UsagePanel`), plus the project and user names the response only has ids for.
 *
 * It re-reads when the month changes or when `reloadToken` bumps (the tab's refresh
 * button), and a response is never shown under a different month's heading: the data is
 * dropped the moment a new read starts, and an older read is aborted and then ignored if it
 * answers late. This runs on the shared loader (`loading/loader.ts`) -- the guard it
 * had by hand (a `cancelled` flag) is now the loader's, the same one `UsagePanel` uses.
 */
export function CostDashboardSection({ month, reloadToken }: { month: string; reloadToken: number }) {
  const { data, loading, error } = useLoaderOnChange(
    (signal) => fetchDashboard(month, signal),
    [month, reloadToken],
    (err) => panelErrorMessage(err, "비용 대시보드를 불러오지 못했습니다."),
  );

  return (
    <Panel
      title={DASHBOARD_WORDING.title}
      titleNote={data ? `— ${formatMonthWindow(data.dashboard.window)}` : undefined}
      headingLevel={3}
      className="mt-4"
    >
      {error && (
        <Alert variant="error" live className="mt-3">
          {error}
        </Alert>
      )}
      {loading && <Skeleton label="비용 대시보드 불러오는 중" className="mt-3" />}
      {data && (
        <CostDashboardView dashboard={data.dashboard} projectNames={data.projectNames} userNames={data.userNames} />
      )}
    </Panel>
  );
}
