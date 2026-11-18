import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { authApi } from "../api/auth";
import {
  ApiError,
  readStoredDevUserId,
  setDevUserId,
  setUnauthorizedHandler,
} from "../api/client";
import type { DevIdentity, User } from "../types/api";

/** Which authentication mechanism the **backend** is running. Never a
 * build-time constant — see
 * `detectAuthMode`. `"local"` sits alongside the original two. */
export type AuthMode = "dev" | "local" | "oidc";

interface IdentityContextValue {
  currentUser: User | null;
  /** `null` until the bootstrap has asked the backend. */
  mode: AuthMode | null;
  /** Dev mode only; always empty in `local`/`oidc` mode. */
  devIdentities: DevIdentity[];
  loading: boolean;
  selectDevIdentity: (userId: string) => Promise<void>;
  /** Re-reads `/users/me` without changing which mode/identity is active —
   * what `LocalLoginForm` calls right after a successful `POST /auth/login`
   * to pick up the session the backend just issued. Exposed rather
   * than duplicated: it is the same function the bootstrap effect already
   * uses internally. */
  refreshCurrentUser: () => Promise<void>;
  logout: () => Promise<void>;
}

const IdentityContext = createContext<IdentityContextValue | undefined>(undefined);

/** Upper bound on a single mode-detection probe. Without this, a
 * backend that accepts the TCP connection but never answers leaves the app
 * on "불러오는 중..." forever — a documented gap
 *, closed here because this task
 * already rewrites `detectAuthMode` to add the second probe below. Does not
 * abort the underlying `fetch` (that would need threading an
 * `AbortController` through `api/client.ts`); the in-flight request is left
 * to resolve or fail on its own and its result is simply discarded. */
const MODE_PROBE_TIMEOUT_MS = 5000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`mode probe timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

/**
 * Ask the backend which of `dev` / `local` / `oidc` it is running, in two
 * steps — every failure shape resolves to the most restrictive answer, not
 * the most convenient one.
 *
 * **Step 1 — `dev`.** `GET /dev/identities` is registered only when the
 * backend is not session-backed (`main.create_app`, `not
 * settings.session_auth_enabled` — widened from `not
 * oidc_enabled`, so this route is now absent in **both** `local` and `oidc`,
 * not just `oidc`). Only a `200` whose body is actually an array asserts
 * `dev` (the `Array.isArray()` guard — a `200` carrying valid non-array
 * JSON used to assert `dev` outright, the *least* restrictive mode, which is
 * exactly backwards for an ambiguous shape); every other outcome falls
 * through to step 2 rather than assuming `oidc` outright, which is the
 * change made when a third mode appeared — before it, "not dev" and "oidc"
 * were the same fact.
 *
 * **Step 2 — `local` vs `oidc`.** `authApi.localAccountsEnabled()`
 * reads the backend's own OpenAPI schema (`GET /openapi.json`) and checks
 * whether `/api/auth/signup` is present in its `paths` — see that
 * function's own comment for why this replaced a `GET /auth/signup`
 * 405-vs-404 status-code probe. Only a confirmed `true` asserts `local`;
 * anything else — including the probe timing out — resolves to `oidc`,
 * preserving the original fail-safe direction: being wrong this way shows
 * an unusable SSO button rather than a credential-free identity picker
 * (`dev`) or a signup form nobody can submit against a real backend.
 *
 * Exported only so the fail-safe behaviour is testable without mounting the
 * provider (ADR 0021: decision logic, not rendering).
 */
export async function detectAuthMode(): Promise<{ mode: AuthMode; devIdentities: DevIdentity[] }> {
  try {
    const devIdentities = await withTimeout(authApi.devIdentities(), MODE_PROBE_TIMEOUT_MS);
    if (Array.isArray(devIdentities)) {
      return { mode: "dev", devIdentities };
    }
    // Not the documented contract (`DevIdentity[]`) — treated exactly like
    // a probe failure below, falling through to step 2 rather than
    // asserting `dev` off an unproven body.
  } catch {
    // Not dev (404, another failure shape, or the probe timed out) — ask
    // whether `local` is the answer before settling for `oidc`.
  }

  try {
    if (await withTimeout(authApi.localAccountsEnabled(), MODE_PROBE_TIMEOUT_MS)) {
      return { mode: "local", devIdentities: [] };
    }
  } catch {
    // Timeout or network failure on the second probe: still not proven
    // local, falls through to the same `oidc` default as an absent route
    // in the schema would.
  }

  return { mode: "oidc", devIdentities: [] };
}

export function IdentityProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<AuthMode | null>(null);
  const [devIdentities, setDevIdentities] = useState<DevIdentity[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  /** Resolve the session from the server. `role` is deliberately **never**
   * persisted client-side: the backend keeps it out of the session payload so a
   * demotion takes effect on the next request,
   * and caching it here would put the staleness straight back. */
  const loadCurrentUser = useCallback(async () => {
    try {
      setCurrentUser(await authApi.me());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setCurrentUser(null);
        return;
      }
      throw err;
    }
  }, []);

  useEffect(() => {
    // A 401 from *any* endpoint means the session is gone; dropping
    // `currentUser` unmounts the authenticated subtree, so `RequireIdentity`
    // takes over and redirects to login with the current location preserved.
    setUnauthorizedHandler(() => setCurrentUser(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      const detected = await detectAuthMode();
      if (cancelled) return;
      setMode(detected.mode);
      setDevIdentities(detected.devIdentities);

      // Dev mode carries identity in a header, so the previously chosen id has
      // to be re-armed before `GET /users/me` can resolve anything. This is
      // the only path that ever populates the header.
      if (detected.mode === "dev") {
        setDevUserId(readStoredDevUserId());
      }

      try {
        await loadCurrentUser();
      } catch {
        if (!cancelled) setCurrentUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [loadCurrentUser]);

  /** Dev mode only. Arms the header, then reads the identity back from the
   * server so `role` comes from the DB rather than from the switcher's
   * id-and-name-only payload. */
  const selectDevIdentity = useCallback(
    async (userId: string) => {
      setDevUserId(userId);
      await loadCurrentUser();
    },
    [loadCurrentUser],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      // Local state is cleared whether or not the call succeeded — leaving a
      // rendered session on screen after the user asked to leave is the worse
      // failure. In `oidc`/`local` mode the cookie is httpOnly, so the backend
      // call is the only thing that can clear it; in `dev` mode clearing the
      // header is, and this `finally` runs in every mode regardless.
      //
      // `setDevUserId` touches `localStorage`, which can throw (a full or
      // disabled store). Nested so a throw there can never skip
      // `setCurrentUser(null)` below — that line is what actually unmounts
      // the authenticated subtree via `RequireIdentity`; a throwing store
      // must not leave the user rendered as still signed in (a review
      // finding, handled here since this code already touches
      // `logout`'s neighbourhood). Best-effort only: the dev-mode storage key
      // is cleanup, not the safety property, so the error is swallowed
      // rather than surfaced.
      try {
        setDevUserId(null);
      } catch {
        // swallowed deliberately — see comment above
      }
      setCurrentUser(null);
    }
  }, []);

  return (
    <IdentityContext.Provider
      value={{
        currentUser,
        mode,
        devIdentities,
        loading,
        selectDevIdentity,
        refreshCurrentUser: loadCurrentUser,
        logout,
      }}
    >
      {children}
    </IdentityContext.Provider>
  );
}

export function useIdentity(): IdentityContextValue {
  const ctx = useContext(IdentityContext);
  if (!ctx) {
    throw new Error("useIdentity must be used within an IdentityProvider");
  }
  return ctx;
}
