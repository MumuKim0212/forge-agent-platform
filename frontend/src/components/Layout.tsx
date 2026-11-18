import { useState } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";

import { useIdentity } from "../context/IdentityContext";
import { ErrorBoundary } from "./ErrorBoundary";
import { useDocumentTitle } from "./documentTitle";
import { PageTitleContext } from "./pageTitle";
import { currentNavTo, navGroupsFor, pageTitleFor, shellDocumentTitle, shellTitle, type NavItem } from "./shellNav";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { FOCUS_RING } from "./ui/focusRing";
import { TextLink } from "./ui/TextLink";

const NAV_ITEM_CLASSES =
  `block rounded-control px-3 py-2 text-sm font-medium transition-colors ${FOCUS_RING}`;
const NAV_ITEM_IDLE =
  "text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-slate-100";
const NAV_ITEM_CURRENT =
  "bg-accent-subtle font-semibold text-accent dark:bg-accent-subtle-dark dark:text-indigo-300";

function NavItemLink({ item, isCurrent }: { item: NavItem; isCurrent: boolean }) {
  return (
    <Link
      to={item.to}
      aria-current={isCurrent ? "page" : undefined}
      className={`${NAV_ITEM_CLASSES} ${isCurrent ? NAV_ITEM_CURRENT : NAV_ITEM_IDLE}`}
    >
      {item.label}
    </Link>
  );
}

/**
 * the shared application shell. A left navigation that reflows
 * to a row above the content on a narrow screen, a compact top bar for who is signed in, and one
 * content width with the page's title above it. `/login` and `/signup` render outside it.
 */
export function Layout() {
  const { currentUser, mode, logout } = useIdentity();
  const [loggingOut, setLoggingOut] = useState(false);
  const { pathname } = useLocation();
  const groups = navGroupsFor(currentUser);
  const current = currentNavTo(pathname);
  // A page whose title is data (a project's name) supplies it (`usePageTitle`); the path decides otherwise.
  const [pageTitle, setPageTitle] = useState<string | null>(null);
  const title = shellTitle(pageTitleFor(pathname), pageTitle);
  useDocumentTitle(shellDocumentTitle(pathname, pageTitle));

  async function handleLogout() {
    setLoggingOut(true);
    try {
      await logout();
      // No navigation needed: `currentUser` is now null, so `RequireIdentity`
      // unmounts this whole subtree and redirects to /login. Nothing rendered
      // here survives, which is the point — a signed-out app must not keep the
      // previous session's data on screen.
    } finally {
      setLoggingOut(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 md:flex dark:bg-slate-950 dark:text-slate-100">
      <div className="border-b border-slate-200 bg-white px-3 py-3 md:sticky md:top-0 md:h-screen md:w-56 md:shrink-0 md:border-b-0 md:border-r md:px-3 md:py-5 dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-3 flex items-center gap-2 px-3 text-base font-bold tracking-tight md:mb-6">
          <span
            aria-hidden="true"
            className="flex h-7 w-7 items-center justify-center rounded-control bg-accent text-sm font-black text-white"
          >
            F
          </span>
          Forge
        </div>
        <nav aria-label="주 메뉴">
          <ul className="flex flex-wrap gap-1 md:flex-col">
            {groups.workspace.map((item) => (
              <li key={item.to}>
                <NavItemLink item={item} isCurrent={item.to === current} />
              </li>
            ))}
            {/* no console entry point for a non-admin
. `navGroupsFor` offers it only behind
                `canAdministerPlatform`, the **same** predicate that guards the route in
                `AdminConsolePage`, so a hidden link and an open page cannot disagree — and hiding it
                is presentation only: every endpoint the console calls requires `admin` on its own
                (rule 2). On a narrow screen it sits in the same row; the divider is wide-screen only. */}
            {groups.administration.map((item, index) => (
              <li
                key={item.to}
                className={index === 0 ? "md:mt-3 md:border-t md:border-slate-200 md:pt-3 dark:md:border-slate-800" : undefined}
              >
                <NavItemLink item={item} isCurrent={item.to === current} />
              </li>
            ))}
          </ul>
        </nav>
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex min-h-14 flex-wrap items-center justify-end gap-3 border-b border-slate-200 bg-white px-4 py-2 md:px-8 dark:border-slate-800 dark:bg-slate-900">
          {currentUser ? (
            <>
              <span className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-200">
                {currentUser.name}
                <Badge tone="neutral">{currentUser.role}</Badge>
              </span>
              {/* 전환 is the dev switcher; in `oidc` mode there is nothing to
                  switch to, and /login would only offer the IdP again. */}
              {mode === "dev" && (
                <TextLink to="/login" className="text-sm">
                  전환
                </TextLink>
              )}
              <Button variant="secondary" busy={loggingOut} onClick={handleLogout}>
                {loggingOut ? "로그아웃 중..." : "로그아웃"}
              </Button>
            </>
          ) : (
            <TextLink to="/login" className="text-sm">
              로그인
            </TextLink>
          )}
        </header>
        <main className="mx-auto w-full max-w-6xl px-4 py-6 md:px-8 md:py-8">
          {title !== null && (
            <h1 className="mb-5 text-2xl font-bold tracking-tight text-slate-900 dark:text-slate-100">{title}</h1>
          )}
          {/* a page that throws while rendering stops here, not at the root, so the nav and the
              logout button above stay up. Keyed by the path: moving to another page mounts a fresh
              boundary, so the fallback does not outlive the page that caused it. */}
          <PageTitleContext.Provider value={setPageTitle}>
            <ErrorBoundary key={pathname}>
              <Outlet />
            </ErrorBoundary>
          </PageTitleContext.Provider>
        </main>
      </div>
    </div>
  );
}
