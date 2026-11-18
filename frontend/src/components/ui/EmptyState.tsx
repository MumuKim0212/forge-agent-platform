import type { ReactNode } from "react";

import { cx } from "./cx";

export type EmptyStateTone = "neutral" | "refused";

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  /** `"neutral"` is an empty list or a not-found result; `"refused"` is a
   * permission-refused block (T2 names both as states neither mockup
   * draws). One shape, not three components, because all three are "there
   * is nothing to show here, and here is why" with a different tone and
   * copy — never a different layout. */
  tone?: EmptyStateTone;
  className?: string;
}

const TONE_CLASSES: Record<EmptyStateTone, string> = {
  neutral:
    "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300",
  refused:
    "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200",
};

/**
 * Carries no role/status/ownership knowledge (c4): a caller (e.g.
 * `AdminConsolePage`'s non-admin refusal, `ProjectDetailPage`'s 404, any
 * panel's empty list) decides the tone and writes the copy; this
 * only renders the block.
 */
export function EmptyState({
  title,
  description,
  icon,
  action,
  tone = "neutral",
  className,
}: EmptyStateProps) {
  return (
    <div
      role={tone === "refused" ? "alert" : undefined}
      className={cx("rounded-card border px-6 py-8 text-center", TONE_CLASSES[tone], className)}
    >
      {icon && (
        <div aria-hidden="true" className="mb-2 flex justify-center text-2xl">
          {icon}
        </div>
      )}
      <p className="text-sm font-semibold">{title}</p>
      {description && <p className="mt-1 text-sm">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
