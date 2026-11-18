import type { InputHTMLAttributes } from "react";

import { cx } from "./cx";
import { FOCUS_RING_FIELD, FOCUS_RING_FIELD_INVALID } from "./focusRing";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Set by the caller alongside `Field`'s `error` — kept as a separate prop
   * rather than derived from it, since a field can be invalid without this
   * control being the one rendering the message (c4: no validation logic
   * lives here). */
  invalid?: boolean;
}

const BASE =
  "block w-full rounded-control border px-3 py-2 text-sm shadow-control transition-colors " +
  "bg-white text-slate-900 placeholder:text-slate-400 " +
  "dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500 " +
  "disabled:cursor-not-allowed disabled:opacity-60";

const VALID = `border-slate-300 ${FOCUS_RING_FIELD} dark:border-slate-600`;
const INVALID = `border-danger ${FOCUS_RING_FIELD_INVALID}`;

export function Input({ invalid, className, ...rest }: InputProps) {
  return (
    <input
      aria-invalid={invalid || undefined}
      className={cx(BASE, invalid ? INVALID : VALID, className)}
      {...rest}
    />
  );
}
