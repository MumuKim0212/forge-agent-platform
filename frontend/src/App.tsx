import { Navigate, useLocation, useRoutes, type RouteObject } from "react-router-dom";

import { Layout } from "./components/Layout";
import { useIdentity } from "./context/IdentityContext";
import { AdminConsolePage } from "./pages/AdminConsolePage";
import { ApprovalConsolePage } from "./pages/ApprovalConsolePage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectRegistryPage } from "./pages/ProjectRegistryPage";
import { RequestIntakePage } from "./pages/RequestIntakePage";
import { SignupRequestPage } from "./pages/SignupRequestPage";

/**
 * Where to send a user who could not reach `pathname`. The destination is
 * URL-encoded into a single `next` parameter so a target that carries its own
 * query string survives instead of being parsed as extra parameters of the
 * *login* URL. Exported so the encoding end of that chain is under test rather
 * than replicated in the test; `LoginPage.safeNextPath` is the other
 * end.
 */
export function loginPathFor(pathname: string, search: string): string {
  return `/login?next=${encodeURIComponent(`${pathname}${search}`)}`;
}

function RequireIdentity({ children }: { children: React.ReactNode }) {
  const { currentUser, loading } = useIdentity();
  const location = useLocation();

  if (loading) return <p>불러오는 중...</p>;
  if (!currentUser) {
    // Preserve where the user was headed. This covers both the initial visit
    // and a session that expired mid-use: a 401 on any call clears
    // `currentUser`, which unmounts this subtree and lands here with
    // `location` still pointing at the page they were on.
    return <Navigate to={loginPathFor(location.pathname, location.search)} replace />;
  }
  return <>{children}</>;
}

/**
 * The route table as data, so a test can match paths against it without
 * mounting anything. react-router ranks by specificity, not order, so
 * the `*` catch-all can only win for a path no other route matches.
 */
export const appRoutes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  // A sibling of `/login`, outside `RequireIdentity` for the same
  // reason: the signup-request form must be reachable with no identity
  // at all. Only meaningful in
  // `local` mode — `SignupRequestPage` itself bounces to `/login`
  // otherwise.
  { path: "/signup", element: <SignupRequestPage /> },
  {
    element: (
      <RequireIdentity>
        <Layout />
      </RequireIdentity>
    ),
    children: [
      { path: "/", element: <Navigate to="/projects" replace /> },
      { path: "/projects", element: <ProjectRegistryPage /> },
      { path: "/projects/new", element: <RequestIntakePage /> },
      { path: "/projects/:id", element: <ProjectDetailPage /> },
      { path: "/approvals", element: <ApprovalConsolePage /> },
      // Inside `RequireIdentity` like every other authenticated page —
      // the *role* gate is `AdminConsolePage`'s own `canAdministerPlatform`
      // check, so a direct navigation by a non-admin renders an explained
      // refusal instead of a redirect that would look like a broken link.
      // Role-gated, not mode-gated.
      { path: "/admin", element: <AdminConsolePage /> },
      // Inside the guarded layout on purpose: a signed-in user gets the
      // 404 in the shell; an anonymous visitor is redirected to login, as for
      // any protected path, so an unknown path is indistinguishable from a
      // real one before sign-in.
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export function App() {
  return useRoutes(appRoutes);
}
