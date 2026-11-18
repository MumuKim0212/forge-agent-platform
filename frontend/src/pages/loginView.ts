/**
 * `LoginPage`'s mode dispatch, extracted as a pure function (ADR 0021:
 * decision logic, not rendering — `LoginPage` itself cannot be mounted in
 * this suite).
 *
 * Acceptance criterion: "`dev`
 * mode keeps its identity switcher and `oidc` mode keeps its SSO click, both
 * unchanged. A test pins that each mode renders its own entry path." Since
 * nothing here can be mounted, "renders its own entry path" is pinned as
 * this function returning the right *view kind* for every {loading, mode,
 * signedIn} combination — `LoginPage` then does nothing more than switch on
 * the result.
 */
import type { AuthMode } from "../context/IdentityContext";

export type LoginView = "loading" | "dev-switcher" | "redirect" | "local-login" | "oidc-sso";

export interface LoginViewInput {
  loading: boolean;
  mode: AuthMode | null;
  /** Whether `useIdentity().currentUser` is non-null. */
  signedIn: boolean;
}

/**
 * Order matches `LoginPage`'s original branching, extended by one case:
 *
 * 1. Still resolving (`loading`, or `mode` not yet set) -> `"loading"`.
 * 2. `dev` -> the switcher, deliberately reachable **even when signed in**
 *    (spec: it doubles as the switch-identity surface — the one place this
 *    function does not treat `signedIn` as a reason to leave).
 * 3. Already signed in (`local`/`oidc`) -> `"redirect"` to `next`.
 * 4. `local`, not signed in -> the local login form.
 * 5. `oidc`, not signed in -> the SSO panel.
 */
export function loginViewFor({ loading, mode, signedIn }: LoginViewInput): LoginView {
  if (loading || mode === null) return "loading";
  if (mode === "dev") return "dev-switcher";
  if (signedIn) return "redirect";
  if (mode === "local") return "local-login";
  return "oidc-sso";
}
