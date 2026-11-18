import type { TextareaHTMLAttributes } from "react";

import { cx } from "./cx";
import { FOCUS_RING_FIELD, FOCUS_RING_FIELD_INVALID } from "./focusRing";

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

const BASE =
  "block w-full rounded-control border px-3 py-2 text-sm shadow-control transition-colors " +
  "bg-white text-slate-900 placeholder:text-slate-400 " +
  "dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500 " +
  "disabled:cursor-not-allowed disabled:opacity-60";

const VALID = `border-slate-300 ${FOCUS_RING_FIELD} dark:border-slate-600`;
const INVALID = `border-danger ${FOCUS_RING_FIELD_INVALID}`;

/** Used by the request-intake / change-request free-text boxes —
 * not a caller in this task's scope, added now so that screen has no design
 * left to invent (c1 lists it as a minimum shape). */
export function Textarea({ invalid, className, ...rest }: TextareaProps) {
  return (
    <textarea
      aria-invalid={invalid || undefined}
      className={cx(BASE, invalid ? INVALID : VALID, className)}
      {...rest}
    />
  );
}
