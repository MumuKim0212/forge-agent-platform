import { useState } from "react";

import { canAdministerPlatform, canRunEvals } from "../auth/roles";
import { useIdentity, type AuthMode } from "../context/IdentityContext";
import type { User } from "../types/api";
import { EmptyState } from "../components/ui/EmptyState";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { AgentEnginePanel } from "./admin/AgentEnginePanel";
import { ConfigurationPanel } from "./admin/ConfigurationPanel";
import { DeploymentHealthPanel } from "./admin/DeploymentHealthPanel";
import { EvalRunsPanel } from "./admin/EvalRunsPanel";
import { HostHealthPanel } from "./admin/HostHealthPanel";
import { RolesPanel } from "./admin/RolesPanel";
import { SignupRequestsPanel } from "./admin/SignupRequestsPanel";
import { TemplatesPanel } from "./admin/TemplatesPanel";
import { UsagePanel } from "./admin/UsagePanel";

/**
 * `/admin` — the platform-operator console.
 *
 * **A visibility surface with three narrow mutations**, not a configuration
 * manager: usage/cost, deployment health, templates and effective configuration
 * are read-only, and the only things this screen writes are per-subject quota
 * overrides, user roles and (ADR 0035) which agent engine builds run on. The
 * engine choice has its own tab, not a section of 플랫폼 설정, because that tab is
 * read-only by construction. The first two — both of which are already per-entity DB state with
 * existing audited APIs.
 *
 * Access: `admin`, through `canAdministerPlatform` — the same predicate `Layout`
 * uses to decide whether to render the nav entry, so a link can never appear for
 * a user this guard would turn away. The guard is presentation; every endpoint
 * the panels call independently requires `admin` (`deps.require_role`), which is
 * why each panel renders a 403 as a message rather than as an empty table.
 * Role-gated, not mode-gated for the original five tabs:
 * nothing they read depends on `mode`, so they are reachable in both `dev` and
 * `oidc`.
 *
 * The `가입 승인` tab (`SignupRequestsPanel`) is the one exception — it is
 * **also** mode-gated, on top of `admin`, via the module-level
 * `isTabVisible(tabId, mode)` — its backend surface (`routers/auth_local.py`)
 * exists only in `local` mode, so showing it in `dev`/`oidc` would offer a
 * control that can only 404 (the "never offer an action the user cannot
 * perform" rule applied to a whole tab). The static `TABS` array is
 * unfiltered (so `TabId` keeps covering every tab by construction); only the
 * nav render and the content render are filtered, both through the same
 * `mode === "local"` check.
 *
 * the console renders inside the shared shell in the light theme —
 * the shell draws its `h1`, so the page carries no heading of
 * its own, and it follows the OS dark preference like every other screen rather than
 * forcing a dark surface of its own.
 */
const TABS = [
  { id: "signups", label: "가입 승인" },
  { id: "usage", label: "사용량 · 쿼터" },
  { id: "roles", label: "역할" },
  { id: "deployments", label: "배포 상태" },
  { id: "health", label: "플랫폼 상태" },
  { id: "templates", label: "템플릿" },
  { id: "engine", label: "에이전트 엔진" },
  { id: "evals", label: "평가" },
  { id: "configuration", label: "플랫폼 설정" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

const TAB_ID_BASE = "admin-console";

/**
 * Whether the console offers a tab (exported and tested; it carries two gates that were
 * reachable by nothing). The evals tab follows the role gate `canRunEvals`; the sign-up approvals
 * tab follows the auth mode (its backend surface exists only in `local` mode); every other tab is
 * always offered to an admin. The nav and the content both read it, so a tab cannot be hidden in
 * one and rendered in the other. Presentation only: each panel's endpoint still requires `admin`.
 */
export function isTabVisible(tabId: TabId, mode: AuthMode | null, user: User | null): boolean {
  if (tabId === "evals") return canRunEvals(user);
  return tabId !== "signups" || mode === "local";
}

export function AdminConsolePage() {
  const { currentUser, mode } = useIdentity();
  // Lands on 가입 승인 in `local` mode (the tab an admin most often has a
  // reason to check) and on 사용량 · 쿼터 otherwise — never on a tab
  // `isTabVisible` would hide, so the console never opens onto a nav with no
  // highlighted entry. Read once at mount: `mode` is already resolved by the
  // time this page can be reached at all (`RequireIdentity` only renders it
  // once `currentUser` is set, which happens after mode detection).
  const [tab, setTab] = useState<TabId>(() =>
    isTabVisible("signups", mode, currentUser) ? "signups" : "usage",
  );

  if (!canAdministerPlatform(currentUser)) {
    // Refused, not blank: a direct navigation to `/admin` has to say what
    // happened. The backend would refuse every call this
    // page makes anyway — this only avoids rendering six failing panels to
    // explain it.
    return (
      <EmptyState
        tone="refused"
        title="관리자 콘솔은 admin 역할만 사용할 수 있습니다"
        description={
          <>
            현재 역할: {currentUser?.role ?? "없음"}. 권한이 필요하면 플랫폼 운영자에게 admin 부여를
            요청하세요.
          </>
        }
      />
    );
  }

  const visibleTabs = TABS.filter((t) => isTabVisible(t.id, mode, currentUser));

  return (
    <div>
      <Tabs idBase={TAB_ID_BASE} tabs={visibleTabs} activeId={tab} onChange={setTab} />

      <TabPanel idBase={TAB_ID_BASE} tabId={tab} className="mt-4">
        {/* The shell's h1 is the console; the panels below open at h3. This is the h2 between them,
            named for the tab, so the outline does not skip a level. */}
        <h2 className="sr-only">{TABS.find((t) => t.id === tab)?.label}</h2>
        {tab === "signups" && isTabVisible("signups", mode, currentUser) && <SignupRequestsPanel />}
        {tab === "usage" && <UsagePanel />}
        {tab === "roles" && <RolesPanel />}
        {tab === "deployments" && <DeploymentHealthPanel />}
        {tab === "health" && <HostHealthPanel />}
        {tab === "templates" && <TemplatesPanel />}
        {tab === "engine" && <AgentEnginePanel />}
        {tab === "evals" && isTabVisible("evals", mode, currentUser) && <EvalRunsPanel />}
        {tab === "configuration" && <ConfigurationPanel />}
      </TabPanel>
    </div>
  );
}
