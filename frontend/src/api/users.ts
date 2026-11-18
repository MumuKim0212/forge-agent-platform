import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type { UsableAdminCount, User, UserRoleUpdateInput } from "../types/api";

/**
 * The user directory and the one role-grant verb (`routers/auth.py`).
 *
 * `api/auth.ts` deliberately stays about the *session* (`me`, `logout`, the dev
 * bootstrap, the login start URL). This module is about *other* users, which is
 * a different consumer — the admin console's role panel — even though the two
 * share a backend router.
 *
 * `list()` returns the previously removed directory read in the shape the role console
 * needs: `UserOut`, which carries `role`. It is authenticated, not admin-only —
 * only `changeRole` requires `admin` (`deps.require_role`).
 */
export const usersApi = {
  list: (options?: RequestOptions) => api.get<User[]>("/users", options),

  /** `admin` only. The number of admins the last-admin guard counts: active
   * ones that hold a credential for the running auth mode. The directory read above
   * carries neither status nor credential, so this cannot be recomputed in the
   * browser; the role panel shows this number. */
  usableAdminCount: (options?: RequestOptions) =>
    api.get<UsableAdminCount>("/users/usable-admin-count", options),

  /** `admin` only. Rejects an unknown role (400) and **demoting the last
   * remaining `admin`** (409). The console
   * explains that 409 rather than showing it raw; see
   * `pages/admin/roleChange.roleChangeFailureMessage`. */
  changeRole: (userId: string, input: UserRoleUpdateInput) =>
    api.patch<User>(`/users/${pathSegment(userId)}/role`, input),
};
