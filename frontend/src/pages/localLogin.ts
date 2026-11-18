/**
 * How `LocalLoginForm` turns a failed `POST /auth/login` into a UI state.
 *
 * The one distinction that matters: **403 is a normal, informational state**
 * (the account exists, the password was right, an admin has not looked at
 * it yet), while **everything else is the single
 * generic failure** the backend deliberately merges "unknown email / wrong
 * password / locked account / no password ever set" into.
 * This function draws exactly that one line and no others — it must
 * never grow a second branch that re-distinguishes what the backend already
 * chose to merge (presentation must not re-derive a security
 * decision the backend made on purpose).
 *
 * `detail` is carried through **unchanged** ("as given" — criterion B): this
 * function does not compose its own copy for the generic-failure case, so
 * there is nothing here that could accidentally reintroduce a
 * cause-distinguishing message.
 *
 * That is a classification decision, not a rendering one. `detail` still
 * travels through this function's return value (kept for completeness and
 * for the tests below), but nothing downstream reads it for `kind ===
 * "error"` — `localLoginErrorMessage` renders one fixed sentence instead
 *. See that function's comment, and `LocalLoginForm.tsx`'s, for why
 * a fixed string is what actually protects spec rule 2's merge.
 */
export type LocalLoginFailure =
  | { kind: "awaiting_approval"; detail: string }
  | { kind: "error"; detail: string };

/** Only called for an `ApiError` (an actual HTTP response) — a network
 * failure or a probe timeout never reaches this function, and the caller
 * handles that shape separately with its own fallback copy. */
export function classifyLocalLoginFailure(status: number, detail: string): LocalLoginFailure {
  if (status === 403) return { kind: "awaiting_approval", detail };
  return { kind: "error", detail };
}

/**
 * The one Korean sentence `LocalLoginForm` renders for `kind === "error"`
 *. Takes the classified failure as
 * its parameter — rather than no parameter at all — specifically so that
 * choice is a checkable decision instead of an unstated assumption:
 * `localLogin.test.ts` classifies two different backend `detail` strings
 * and asserts this returns the identical sentence for both.
 *
 * Rendering the backend's `detail` was never what protected spec rule 2's
 * merge of "unknown email / wrong password / locked / never had a
 * password" into one message — it made that property depend on the
 * backend's wording forever, so a later, more specific `detail` (a
 * reasonable diagnostic-field change) would have silently started leaking
 * which cause it was, with nothing here to catch it. Ignoring `detail`
 * entirely and returning a fixed string is what makes the merge actually
 * hold: the rendered text cannot vary no matter what `detail` says next.
 */
export function localLoginErrorMessage(
  _failure: Extract<LocalLoginFailure, { kind: "error" }>,
): string {
  return "로그인에 실패했습니다. 이메일과 비밀번호를 확인해 주세요.";
}
