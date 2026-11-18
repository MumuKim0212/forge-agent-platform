/**
 * What a console panel says when a call fails.
 *
 * This exists because hiding the nav
 * entry is presentation, and **the backend is the enforcement point** — so a 403
 * is a state this screen genuinely reaches (a role revoked mid-session, or a
 * direct navigation by someone the nav never offered the link to), and it has to
 * render as an explanation rather than as an empty table that looks like "no
 * data".
 *
 * A `401` never arrives here: `api/client.ts`'s unauthorized hook clears the
 * session, which unmounts the whole authenticated subtree via `RequireIdentity`.
 */
import { ApiError, transportErrorMessage } from "../../api/client";

export function panelErrorMessage(err: unknown, fallback: string): string {
  // a call that never got a usable answer (server down, offline, a body that was not
  // JSON) says so in the shared Korean sentence. `ApiError` is the only shape `api/client.ts`
  // throws for it, so nothing here reads an `Error.message` any more.
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  if (err instanceof ApiError) {
    // this used to append ` (서버 응답: ${said})` (403 branch) or
    // `${fallback} ${said}` (every other status), putting the backend's
    // `detail` — an English diagnostic string — on screen. Dropped for the
    // same reason it was dropped from the login/signup screens: rendering
    // that string never protected anything, it just made this console's
    // copy depend on the backend's wording forever. The 403/other split
    // stays; only the server text is gone.
    if (err.status === 403) {
      return "이 정보는 admin 역할만 조회할 수 있습니다. 방금 역할이 바뀌었다면 새로 고침 후 다시 확인하세요.";
    }
    return fallback;
  }
  // Not an `ApiError`: a bug in the caller, not a failed call. Its `message` is a library's
  // English (before, "Failed to fetch" landed here and was appended to the Korean copy).
  return fallback;
}
