import { cx } from "./cx";

export interface SkeletonProps {
  className?: string;
  /** A skeleton has no text of its own, so a screen-reader label has to
   * stand in for it (T2: "no loading skeleton" anywhere in either mockup —
   * `LoginPage`'s `"loading"` view, d2, is this primitive's first caller). */
  label: string;
}

/** Three placeholder bars, not a spinner: distinguishes "content is coming"
 * (this) from "an action is in flight" (`Button`'s `busy`). */
export function Skeleton({ className, label }: SkeletonProps) {
  return (
    <div role="status" aria-label={label} className={cx("animate-pulse space-y-3", className)}>
      <div className="h-4 w-1/3 rounded bg-slate-200 dark:bg-slate-700" />
      <div className="h-4 w-full rounded bg-slate-200 dark:bg-slate-700" />
      <div className="h-4 w-2/3 rounded bg-slate-200 dark:bg-slate-700" />
    </div>
  );
}
