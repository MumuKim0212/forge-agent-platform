import { ApiError } from "./client";

/**
 * Reading a backend refusal.
 *
 * A refusal the backend wants a screen to tell apart carries a machine-readable code as the PREFIX
 * of its string `detail`: `PROJECT_ARCHIVED: project p1 is archived; ...`. The code is not a second
 * field because `ApiError.message` is the `detail` string as received, so a structured detail would
 * have shown as the bare status text on every screen that renders it. The code's values are the
 * backend's, spelled as literals by the screen that maps them (`pages/deployControls.ts`).
 *
 * The prose after the prefix is the backend's English diagnostic and is never shown.
 */
const CODE_PREFIX = /^([A-Z][A-Z0-9_]*): /;

/** The code a refused request's `detail` starts with, or `null` (no code, a non-string detail, a
 * transport failure, or not an `ApiError`). Any code-shaped prefix is returned; the caller decides
 * which codes it knows. */
export function refusalCode(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.kind !== "http") return null;
  if (typeof err.detail !== "string") return null;
  const match = CODE_PREFIX.exec(err.detail);
  return match === null ? null : match[1];
}

/**
 * The backend answered that what was asked no longer matches its state: a rule refused it (`400`)
 * or something conflicted with it (`409`). What the screen was showing is probably stale, so it
 * should read again. Not `403` (a permission, which a re-read does not change) and not a transport
 * failure.
 */
export function isStateRefusal(err: unknown): boolean {
  return err instanceof ApiError && err.kind === "http" && (err.status === 400 || err.status === 409);
}
