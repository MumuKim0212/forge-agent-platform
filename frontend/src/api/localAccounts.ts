import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type {
  LocalLoginInput,
  PendingSignup,
  SignupAck,
  SignupApproveInput,
  SignupCreateInput,
  SignupRejectInput,
  User,
} from "../types/api";

/**
 * The `?status=` query parameter on `GET /admin/signup-requests`
 * (`routers/auth_local.list_signup_requests_endpoint` defaults it to
 * `PENDING` server-side when omitted). Exported for the same reason
 * `usage.monthQuery` is — a renamed or dropped parameter here is invisible
 * to `tsc` and degrades silently: the request still succeeds, just against
 * the wrong status filter (contract strings that
 * cross a boundary always get a literal).
 */
export function signupStatusQuery(status: string | null | undefined): string {
  return status ? `?status=${encodeURIComponent(status)}` : "";
}

/**
 * `local`-mode signup/login and the admin signup-approval + deactivation
 * surface (`routers/auth_local.py`). Every path
 * here is registered **only** when the backend is in `local` mode — a call
 * against `dev`/`oidc` 404s, the same shape `authApi.devIdentities()` takes
 * outside `dev` mode.
 *
 * Deliberately a separate module from `api/auth.ts`: that module is the
 * *session* (`me`/`logout`/dev bootstrap/oidc login URL, plus the
 * mode-detection probe this feature added), this one is signup requests and
 * the accounts they become — a different consumer even where both end up on
 * `routers/auth_local.py`/`routers/auth.py`.
 */
export const localAccountsApi = {
  /** Unauthenticated. Always the identical ack whether or not the email
   * already existed — nothing on the frontend
   * needs to (or can) tell the two outcomes apart. */
  signup: (input: SignupCreateInput) => api.post<SignupAck>("/auth/signup", input),

  /** Unauthenticated. 200 + the session cookie on success. On failure the
   * backend distinguishes only "awaiting approval" (403) from everything
   * else (401, one generic message); this wrapper does
   * not interpret that, it only carries the response through. */
  login: (input: LocalLoginInput) => api.post<User>("/auth/login", input),

  /** `admin` only. */
  listSignupRequests: (status?: string, options?: RequestOptions) =>
    api.get<PendingSignup[]>(`/admin/signup-requests${signupStatusQuery(status)}`, options),

  /** `admin` only. `role` defaults to `requester` server-side when omitted
   * or `null`. */
  approveSignup: (userId: string, input: SignupApproveInput = {}) =>
    api.post<User>(`/admin/signup-requests/${pathSegment(userId)}/approve`, input),

  /** `admin` only. `reason` is optional and audited, never shown back to
   * the requester — there is no notification of any kind (ADR 0022 §8). */
  rejectSignup: (userId: string, input: SignupRejectInput = {}) =>
    api.post<User>(`/admin/signup-requests/${pathSegment(userId)}/reject`, input),

  /** `admin` only. `REJECTED` -> `PENDING`. */
  reopenSignup: (userId: string) => api.post<User>(`/admin/signup-requests/${pathSegment(userId)}/reopen`),

  /** `admin` only. `ACTIVE` -> `REJECTED`; the backend rejects with 409 when
   * the target is the last remaining admin
   * (`services/roles.ensure_not_last_admin`). */
  deactivateUser: (userId: string) => api.post<User>(`/admin/users/${pathSegment(userId)}/deactivate`),
};
