/**
 * Build-status decisions the markup needs, as named predicates (ADR 0021).
 *
 * `Timeline` used to compare `entry.data.status === "NOT_IMPLEMENTED"` inline. A status literal in
 * markup is the shape of a defect once fixed on the project page (a rule re-derived from a raw
 * status in JSX, and wrong); `test/statusLiteralGuard.test.ts` keeps it out of every `.tsx`, so a
 * comparison lives here, where a test can reach it.
 */

/** The backend's `BuildStatus.NOT_IMPLEMENTED`: the slice-1 stub state, a build no executor ran
 * (ADR 0001: no fake states). A contract string, so a literal; pinned in the test. */
export const NOT_IMPLEMENTED_BUILD_STATUS = "NOT_IMPLEMENTED";

/** Whether a build entry is the stub state, which the timeline explains in words. */
export function isBuildNotImplemented(status: unknown): boolean {
  return status === NOT_IMPLEMENTED_BUILD_STATUS;
}
