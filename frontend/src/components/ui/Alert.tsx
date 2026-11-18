import type { ReactNode } from "react";

import { cx } from "./cx";

export type AlertVariant = "error" | "success" | "warning" | "info";

export interface AlertProps {
  variant: AlertVariant;
  /**
   * Opt-in live region. `role="alert"` is an *assertive* live
   * region — it interrupts a screen reader immediately, every time the
   * element (re)appears. That is right for a transient result of an
   * action the user just took (a load just failed, a submit just failed)
   * and wrong for a standing fact about the current view that happens to
   * render in the same tone (a permission note, a flagged-findings
   * summary, a past deployment's recorded failure reason sitting in
   * history). Defaults to `false` so a caller must say which one this is —
   * the primitive only sees `variant` (a colour), which cannot tell the
   * two apart. The defect this replaced: `role`
   * derived from `variant` alone put `role="alert"` on weeks-old failure
   * text in a project's deployment history and timeline, both read on
   * every render as if they had just happened.
   */
  live?: boolean;
  children: ReactNode;
  className?: string;
}

const VARIANT_CLASSES: Record<AlertVariant, string> = {
  error:
    "border-danger/30 bg-danger-subtle text-danger-strong dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300",
  success:
    "border-success/30 bg-success-subtle text-success-strong dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300",
  warning:
    "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-300",
  // A neutral notice that is neither a fault nor a caution: a state the user is
  // expected to be in, such as an account waiting for approval.
  info: "border-accent/30 bg-accent-subtle text-accent-hover dark:border-indigo-900 dark:bg-accent-subtle-dark dark:text-indigo-300",
};

/**
 * The inline notice these five screens hand-rolled 25+ times —
 * `<p style={{color: "crimson" | "#2e7d32" | WARN}}>`, none of them
 * announced to a screen reader. `role="alert"` now follows `live`, not
 * `variant` — the tone (error/success/warning) is a colour choice, and the
 * live-region behaviour is a separate fact about *when* the text appeared
 * that only the caller knows.
 *
 * Carries no role/status/ownership logic (b4): `variant` and `live` are
 * both plain props. A caller decides which of the three tones a given
 * piece of text is, and separately whether it is a fresh, transient result
 * worth interrupting for.
 */
export function Alert({ variant, live = false, children, className }: AlertProps) {
  return (
    <p
      role={live ? "alert" : undefined}
      className={cx("rounded-control border px-3 py-2 text-sm", VARIANT_CLASSES[variant], className)}
    >
      {children}
    </p>
  );
}
