import { api, type RequestOptions } from "./client";
import { pathSegment } from "./path";
import type { CostDashboard, PlatformUsage, Quota, QuotaUpdateInput, UsageReport } from "../types/api";

/**
 * The `month` query parameter (`routers/usage.py`: `month: str | None = None` on
 * every read). Omitted entirely for "current month" rather than sent empty — the
 * backend resolves `None` to the current window and would 400 on `?month=`.
 *
 * Exported so the parameter *name* is covered by a test with a literal on the
 * expected side. A one-sided rename here is invisible to `tsc` and degrades in
 * the quietest possible way: the value is dropped, the backend answers for the
 * current month, and the console shows this month's figures under a historical
 * month's heading (contract strings that cross a boundary).
 */
export function monthQuery(month: string | null | undefined): string {
  return month ? `?month=${encodeURIComponent(month)}` : "";
}

/**
 * `GET /usage/dashboard`'s query string: `month` exactly as `monthQuery`
 * sends it, plus `limit` (how many most-expensive builds; the backend accepts 1..50
 * and answers 422 outside it). Omitted entirely when not given, so the backend's own
 * defaults (current month, 10 builds) apply. The parameter *names* are contract
 * strings pinned with literals in `usage.test.ts`.
 */
export function dashboardQuery(month: string | null | undefined, limit?: number): string {
  const params: string[] = [];
  if (month) params.push(`month=${encodeURIComponent(month)}`);
  if (limit !== undefined) params.push(`limit=${encodeURIComponent(String(limit))}`);
  return params.length > 0 ? `?${params.join("&")}` : "";
}

// `routers/usage.py`'s two by-id reads (`/usage/projects/{id}`, `/usage/users/{id}`)
// are deliberately **not** wrapped here. They are the owner-or-self surface for a
// per-project or per-user view, and the console reads the admin-scoped
// cross-subject lists instead — a wrapper with no caller is what was removed
// from `api/templates.ts`, and adding two back on spec would repeat that.
export const usageApi = {
  /** Every project, `admin` only (cross-subject read). */
  projects: (month?: string | null, options?: RequestOptions) =>
    api.get<UsageReport>(`/usage/projects${monthQuery(month)}`, options),

  /** Every user **plus the unattributed line**, `admin` only. */
  users: (month?: string | null, options?: RequestOptions) =>
    api.get<UsageReport>(`/usage/users${monthQuery(month)}`, options),

  /** The cost dashboard (daily buckets, outcome breakdown, most expensive builds), `admin` only. */
  dashboard: (month?: string | null, limit?: number, options?: RequestOptions) =>
    api.get<CostDashboard>(`/usage/dashboard${dashboardQuery(month, limit)}`, options),

  /** The platform-wide summary, `admin` only. */
  summary: (month?: string | null, options?: RequestOptions) =>
    api.get<PlatformUsage>(`/usage/summary${monthQuery(month)}`, options),

  /** `admin` only. `monthly_quota_usd: null` clears the override back to the
   * global default; `0` is a legal limit, not a clear. */
  setUserQuota: (userId: string, input: QuotaUpdateInput) =>
    api.patch<Quota>(`/usage/quotas/users/${pathSegment(userId)}`, input),

  setProjectQuota: (projectId: string, input: QuotaUpdateInput) =>
    api.patch<Quota>(`/usage/quotas/projects/${pathSegment(projectId)}`, input),
};
