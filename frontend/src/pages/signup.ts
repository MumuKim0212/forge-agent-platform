/**
 * Client-side signup-form validation.
 * Mirrors backend `schemas/local_accounts.SignupCreate` — **never
 * the enforcement point**: the backend re-validates every field
 * independently, and this exists only so a user sees a mistake before
 * submitting rather than after.
 */
import { ApiError, transportErrorMessage } from "../api/client";

export interface SignupFormValues {
  email: string;
  name: string;
  department: string;
  password: string;
}

export interface SignupFieldErrors {
  email?: string;
  name?: string;
  department?: string;
  password?: string;
}

/** Mirrors backend `SignupCreate.password`'s `Field(min_length=8)`
 * (minimum length 8 characters, no composition
 * rules). */
export const SIGNUP_PASSWORD_MIN_LENGTH = 8;

/** Mirrors backend `services/passwords.PASSWORD_MAX_BYTES`:
 * the password hash reads only the first 72 bytes of the
 * password in UTF-8, so a longer one is refused. Counted in bytes, not
 * characters: a Korean syllable is three bytes, so a 25-character Korean
 * passphrase is over the limit. The backend refuses it too; this is here so the
 * user is told the rule in plain terms, because the refusal the server sends
 * back is never shown on this form. */
export const SIGNUP_PASSWORD_MAX_BYTES = 72;

/** The password rule as it is said to the user, in characters and in bytes. */
export const SIGNUP_PASSWORD_MAX_BYTES_MESSAGE =
  `비밀번호는 UTF-8 기준 ${SIGNUP_PASSWORD_MAX_BYTES}바이트 이하여야 합니다 ` +
  `(영문·숫자는 ${SIGNUP_PASSWORD_MAX_BYTES}자까지, 한글은 한 글자가 3바이트이므로 ` +
  `${SIGNUP_PASSWORD_MAX_BYTES / 3}자까지).`;

/** Mirrors backend `schemas/local_accounts._EMAIL_LIKE` — deliberately
 * permissive (spec: "must look like an address", not RFC 5322 validation).
 * Not a security boundary on either side: the admin looks at the address
 * before approving regardless. */
const EMAIL_LIKE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/**
 * Returns one error per invalid field, none for a valid one. `email` is
 * trimmed before the shape check (mirroring the backend's `.strip()`);
 * `password` is never trimmed — the backend does not trim it either, and
 * doing so here would silently validate and submit a different string than
 * what the user typed.
 */
export function validateSignup(values: SignupFormValues): SignupFieldErrors {
  const errors: SignupFieldErrors = {};

  const email = values.email.trim();
  if (!email) {
    errors.email = "이메일을 입력해 주세요.";
  } else if (!EMAIL_LIKE.test(email)) {
    errors.email = "이메일 형식이 올바르지 않습니다.";
  }

  if (!values.name.trim()) {
    errors.name = "이름을 입력해 주세요.";
  }

  // Department is evidence for the approving admin,
  // never an authorization input — but it is still a *required* field, so
  // an empty one is still a validation error, just not a permission check.
  if (!values.department.trim()) {
    errors.department = "부서를 입력해 주세요.";
  }

  if (values.password.length < SIGNUP_PASSWORD_MIN_LENGTH) {
    errors.password = `비밀번호는 최소 ${SIGNUP_PASSWORD_MIN_LENGTH}자 이상이어야 합니다.`;
  } else if (new TextEncoder().encode(values.password).length > SIGNUP_PASSWORD_MAX_BYTES) {
    errors.password = SIGNUP_PASSWORD_MAX_BYTES_MESSAGE;
  }

  return errors;
}

export function isSignupValid(errors: SignupFieldErrors): boolean {
  return Object.keys(errors).length === 0;
}

/**
 * What the signup form says when `POST /auth/signup` fails (kept out of the component
 * so the rule is reachable by a test, ADR 0021). Three outcomes, none of them a backend or
 * library string:
 *
 * - the request never got an answer or the answer was unreadable: the shared transport sentence
 *   (`transportErrorMessage`). This screen used to render `err.message` for anything that
 *   was not an `ApiError`, which for a dead server is the browser's English "Failed to fetch" and
 *   was the *only* text on screen;
 * - the server refused the request (an `ApiError` with a status): a fixed sentence asking for a
 *   second look at the input -- the refusal's own `detail` stays off the screen;
 * - anything else: the plain failure sentence.
 */
export function signupSubmitFailureMessage(err: unknown): string {
  const transport = transportErrorMessage(err);
  if (transport !== null) return transport;
  if (err instanceof ApiError) {
    return "가입 요청에 실패했습니다. 입력값을 확인한 뒤 다시 시도해 주세요.";
  }
  return "가입 요청에 실패했습니다.";
}
