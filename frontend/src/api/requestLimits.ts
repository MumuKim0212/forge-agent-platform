/**
 * Length limits on what a requester types.
 * Every accepted request is placed verbatim in the build agent's prompt,
 * and the LLM proxy checks the budget only before a call, so the API rejects (422)
 * an over-long request instead of truncating it. This module is the frontend half:
 * **never the enforcement point** -- it stops the user before the server has to.
 *
 * **Counting.** The backend counts characters as Python `len` does: code points.
 * JS `.length` and the HTML `maxlength` attribute count UTF-16 units, so an emoji
 * is 2 there and 1 on the server -- `maxlength` would block a request the server
 * accepts. Everything here counts code points (`for...of` iterates by code point;
 * a lone surrogate counts as 1 on both sides), so the two agree exactly and the
 * frontend is never stricter than the server.
 *
 * **The text is never edited.** A request cut to fit is a different request, so
 * the field keeps whatever the user typed or pasted, the counter shows how far
 * over it is, and submit is blocked (`isAnyOverLimit`) until it fits -- exactly
 * what the server would 422, nothing more.
 */
import { ApiError } from "./client";

/** Mirrors backend `schemas/change_request.NL_REQUEST_MAX_CHARS`. */
export const NL_REQUEST_MAX_CHARS = 8000;

/** Mirrors backend `schemas/project.PROJECT_NAME_MAX_CHARS`. */
export const PROJECT_NAME_MAX_CHARS = 100;

/** Characters as the server counts them (code points). */
export function countChars(text: string): number {
  let count = 0;
  for (const _ of text) count += 1;
  return count;
}

export interface LengthState {
  count: number;
  max: number;
  /** The counter the user sees, e.g. `1234 / 8000`. */
  label: string;
  /** More than the server accepts. Exactly `max` is not over. */
  over: boolean;
  /** How many characters to remove to fit (0 when not over). */
  excess: number;
}

export function lengthState(text: string, max: number): LengthState {
  const count = countChars(text);
  const excess = Math.max(0, count - max);
  return { count, max, label: `${count} / ${max}`, over: count > max, excess };
}

/** The neutral counter under a limited input (`1234 / 8000`). */
export function lengthHint(text: string, max: number): string {
  return lengthState(text, max).label;
}

/** The over-limit message for `Field`'s `error` (which replaces the hint), or
 * `undefined` while the text fits. Says how many characters to remove. */
export function lengthError(text: string, max: number): string | undefined {
  const state = lengthState(text, max);
  if (!state.over) return undefined;
  return `${state.label} — ${state.excess}자 초과입니다. ${state.excess}자를 줄여 주세요.`;
}

/** The one sentence a screen reader hears when the user types past a limit, for `Field`'s
 * `errorNotice`: a polite, once-only announcement. It carries the field's name and **no count**,
 * so it is the same string on every keystroke and the region is not re-announced while the user
 * keeps typing; the count lives in `lengthError`, which is only the control's description. */
export function lengthNotice(fieldName: string): string {
  return `${fieldName}: 글자 수 제한을 넘었습니다.`;
}

export interface LimitedText {
  text: string;
  max: number;
}

/** True while any limited field is over its limit: the submit `Button` is
 * disabled on it and the submit handler returns early on it. */
export function isAnyOverLimit(fields: readonly LimitedText[]): boolean {
  return fields.some((field) => lengthState(field.text, field.max).over);
}

export type LimitedField = "nl_request" | "name";

/** Which limited field a failed request overran, or `null` when the failure is
 * anything else. Recognises pydantic's 422 `string_too_long` entry (pinned by
 * `backend/tests/test_request_length_cap.py`); anything that does not match
 * exactly stays `null`, so the caller falls back to its generic message. */
export function tooLongField(err: unknown): LimitedField | null {
  if (!(err instanceof ApiError) || err.status !== 422 || !Array.isArray(err.detail)) return null;
  for (const entry of err.detail) {
    if (typeof entry !== "object" || entry === null) continue;
    const { type, loc } = entry as { type?: unknown; loc?: unknown };
    if (type !== "string_too_long" || !Array.isArray(loc)) continue;
    const field = loc[loc.length - 1];
    if (field === "nl_request" || field === "name") return field;
  }
  return null;
}

/** Korean message for an over-long field, or `null` for any other failure. */
export function tooLongMessage(err: unknown): string | null {
  switch (tooLongField(err)) {
    case "nl_request":
      return `요청 내용이 너무 깁니다. ${NL_REQUEST_MAX_CHARS}자 이하로 줄여 주세요.`;
    case "name":
      return `프로젝트 이름이 너무 깁니다. ${PROJECT_NAME_MAX_CHARS}자 이하로 줄여 주세요.`;
    default:
      return null;
  }
}
