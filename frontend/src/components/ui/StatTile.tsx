import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "./cx";

export type StatTileTone = "neutral" | "success" | "danger" | "warning" | "accent";

/** `compact` is the page-header strip: the value reads as a line of text, not a headline number. */
export type StatTileSize = "default" | "compact";

export interface StatTileProps extends Omit<HTMLAttributes<HTMLDivElement>, "children"> {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  /** Colours the value only — the tile's own card surface stays neutral so
   * a row of tiles reads as one group, not a row of alerts. */
  tone?: StatTileTone;
  size?: StatTileSize;
}

const VALUE_TONE_CLASSES: Record<StatTileTone, string> = {
  neutral: "text-slate-900 dark:text-white",
  success: "text-success dark:text-emerald-400",
  danger: "text-danger dark:text-rose-400",
  warning: "text-amber-600 dark:text-amber-400",
  accent: "text-accent dark:text-indigo-400",
};

/**
 * The stat-tile grid shape both mockups draw (`admin_console.html:153-170`'s
 * label/value/note card) — `UsagePanel`'s platform summary and
 * `DeploymentHealthPanel`'s project counts are its two callers.
 * Carries no role/status/ownership logic: `tone` is a colour choice a
 * caller makes, the same shape as `Badge`'s `tone`.
 */
export function StatTile({ label, value, note, tone = "neutral", size = "default", className, ...rest }: StatTileProps) {
  const compact = size === "compact";
  return (
    <div
      className={cx(
        "rounded-card border border-slate-200 bg-white shadow-card",
        "dark:border-slate-700 dark:bg-slate-900",
        compact ? "px-3.5 py-2.5" : "p-4",
        className,
      )}
      {...rest}
    >
      <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
      <div className={cx(compact ? "mt-0.5 text-sm font-bold" : "mt-1 text-2xl font-black", VALUE_TONE_CLASSES[tone])}>
        {value}
      </div>
      {note && <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">{note}</div>}
    </div>
  );
}
