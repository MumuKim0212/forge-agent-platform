import type { SelectHTMLAttributes } from "react";

import { cx } from "./cx";
import { FOCUS_RING_FIELD, FOCUS_RING_FIELD_INVALID } from "./focusRing";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean;
  /** Size the control to its content instead of the full-width default. The width class is chosen
   * here, in the one place that owns it: a caller-supplied width class would sit beside the
   * default one, and which of the two wins is then a matter of stylesheet order, not intent. */
  fit?: boolean;
}

const BASE =
  "block rounded-control border px-3 py-2 text-sm shadow-control transition-colors " +
  "bg-white text-slate-900 " +
  "dark:bg-slate-800 dark:text-slate-100 " +
  "disabled:cursor-not-allowed disabled:opacity-60";

const VALID = `border-slate-300 ${FOCUS_RING_FIELD} dark:border-slate-600`;
const INVALID = `border-danger ${FOCUS_RING_FIELD_INVALID}`;

/** The three-value role select (`RolesPanel`/`SignupRequestsPanel`)
 * and any other fixed-option dropdown are this primitive's callers — no
 * free-form entry is a spec rule, not something this component enforces; it
 * only renders whatever `<option>`s the caller passes (c4). */
export function Select({ invalid, fit = false, className, children, ...rest }: SelectProps) {
  return (
    <select
      aria-invalid={invalid || undefined}
      className={cx(BASE, fit ? "w-auto" : "w-full", invalid ? INVALID : VALID, className)}
      {...rest}
    >
      {children}
    </select>
  );
}
