import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "./cx";

export type BadgeTone = "neutral" | "success" | "danger" | "warning" | "accent";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  /** Let a long note wrap onto several lines: drops the no-wrap rule and swaps the
   * pill for a rounded box (a pill that wraps looks broken). Default is the one-line pill. */
  wrap?: boolean;
  children: ReactNode;
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  success: "bg-success-subtle text-success-strong dark:bg-emerald-950 dark:text-emerald-300",
  danger: "bg-danger-subtle text-danger-strong dark:bg-rose-950 dark:text-rose-300",
  warning: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  accent: "bg-accent-subtle text-accent dark:bg-accent-subtle-dark dark:text-indigo-300",
};

/**
 * The pill shape used for status/role labels.
 * Takes a `tone`, never a status or role value — `StatusBadge.tsx` and
 * the admin panels decide which tone a given status/role maps to,
 * keeping that knowledge where it lives today.
 */
export function Badge({ tone = "neutral", wrap = false, className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cx(
        "inline-flex items-center px-2.5 py-0.5 text-xs font-semibold",
        wrap ? "rounded-lg" : "whitespace-nowrap rounded-full",
        TONE_CLASSES[tone],
        className,
      )}
      {...rest}
    >
      {children}
    </span>
  );
}
