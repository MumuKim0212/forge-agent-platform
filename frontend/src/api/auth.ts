import { api, apiUrl, openApiUrl } from "./client";
import type { DevIdentity, User } from "../types/api";

/** `/openapi.json` is served by FastAPI directly on the app root
 * (`main.create_app`'s `FastAPI(...)`), OUTSIDE every `/api`-prefixed
 * router. Its URL is derived from the configured API base
 * (`client.openApiUrl`), so a split dev setup asks the API it is configured
 * for rather than whatever origin served the page. See
 * `localAccountsEnabled` below for why the frontend fetches it at all. */

/** The path this module checks for in the schema's `paths` object — the
 * literal `routers/auth_local.py` registers only when `settings.
 * local_accounts_enabled`).
 * Its own constant so `localAccountsEnabled`'s body reads as "does this
 * fact hold" rather than repeating the string inline. */
const LOCAL_SIGNUP_ROUTE = "/api/auth/signup";

export const authApi = {
  /** The session probe. `401` means "not signed in", which is the normal
   * signed-out state and not an error to report. */
  me: () => api.get<User>("/users/me"),

  /** `POST /auth/logout` — clears the session cookie (204). */
  logout: () => api.post<void>("/auth/logout"),

  /**
   * The dev switcher bootstrap: id +
   * display name only, and **registered only when the backend is not in OIDC
   * mode**. Its 404 is therefore the mode signal — see `IdentityContext`.
   */
  devIdentities: () => api.get<DevIdentity[]>("/dev/identities"),

  /**
   * The OIDC login start. Returned as a URL rather than called, because the
   * IdP round-trip is a **browser navigation**: `fetch` would follow the 302
   * to the IdP inside XHR and land nowhere useful. `next` is where the backend
   * sends the browser after a successful callback (it sanitises the value to a
   * same-site path, so this cannot become an open redirect).
   */
  loginStartUrl: (next: string) => apiUrl(`/auth/login?next=${encodeURIComponent(next)}`),

  /**
   * whether the backend has the `local`-mode signup/login surface
   * registered — the second half of mode detection, used once the dev
   * probe above has already come back negative or malformed
   * (`IdentityContext.detectAuthMode`).
   *
   * Reads the fact from the backend's own OpenAPI schema
   * (`GET /openapi.json`) — is `/api/auth/signup` present in its `paths`
   * object — rather than inferring it from a status code.
   *
   * **Replaces the earlier approach** (probing `GET /auth/signup` and
   * treating a `405` Method Not Allowed as proof the route exists,
   * registered for `POST` only): that rested on a Starlette/FastAPI
   * implementation detail nothing here owns — a routing-layer upgrade that
   * ever answered a method mismatch with `404` instead of `405` would
   * silently flip every `local` deployment to `oidc`. The schema is
   * documented API; a status code is a side effect of routing.
   *
   * Every failure shape resolves to `false` — "not proven local", the same
   * fail-safe direction the old probe used: a non-2xx response, a body
   * that isn't JSON, a body with no `paths` object (or one that isn't an
   * object), `/api/auth/signup` simply absent from it, or a network
   * failure. Only a schema that both parses and actually names the route
   * proves `local`.
   *
   * A plain `fetch`, not `api.get` — `/openapi.json` lives outside every
   * `/api`-prefixed router (see `openApiUrl`), so
   * `apiUrl`/`api.get`'s `/api` prefix does not apply to it. Also why this
   * does not go through `api.get`'s `ApiError`/401-hook machinery: the
   * schema is unauthenticated and this probe fires before any identity is
   * known.
   */
  localAccountsEnabled: async (): Promise<boolean> => {
    let response: Response;
    try {
      response = await fetch(openApiUrl());
    } catch {
      return false;
    }
    if (!response.ok) {
      return false;
    }

    let schema: unknown;
    try {
      schema = await response.json();
    } catch {
      return false;
    }

    if (typeof schema !== "object" || schema === null) {
      return false;
    }
    const paths = (schema as { paths?: unknown }).paths;
    if (typeof paths !== "object" || paths === null) {
      return false;
    }

    return LOCAL_SIGNUP_ROUTE in paths;
  },
};
