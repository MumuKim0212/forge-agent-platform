import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "./cx";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

/** The bordered, rounded surface every panel and form sits on.
 * No variant prop: the mockups draw exactly one card shape
 * in both themes, so there is nothing to parameterise yet. */
export function Card({ className, children, ...rest }: CardProps) {
  return (
    <div
      className={cx(
        "rounded-card border border-slate-200 bg-white shadow-card",
        "dark:border-slate-700 dark:bg-slate-900",
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}
