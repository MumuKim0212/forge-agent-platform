import type { HTMLAttributes, ReactNode } from "react";

import { Card } from "./Card";
import { cx } from "./cx";

/** The heading levels a `Panel` title may take. Not `1`: the page's `<h1>` is the shell's. */
export type PanelHeadingLevel = 2 | 3 | 4;

/**
 * The tag for each level, spelled out so a level can never become a tag that is not a heading, and so a
 * test can pin the mapping to the outline it produces.
 */
const HEADING_TAG = { 2: "h2", 3: "h3", 4: "h4" } as const;

/** The tag a `Panel` renders its title in for a given level (exported for the outline test). */
export function panelHeadingTag(level: PanelHeadingLevel): "h2" | "h3" | "h4" {
  return HEADING_TAG[level];
}

export interface PanelProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  /** The box's own heading: it names one section of a page whose `<h1>` is the shell's. */
  title: string;
  /**
   * Which heading level the title is. Default 2: a page whose `<h1>` is the shell's, with the panels directly
   * under it. A panel that sits inside a region already headed at level 2 (the admin console's tab panel opens
   * with a visually-hidden `<h2>` carrying the tab label) passes 3, so the outline neither repeats a level nor
   * skips one.
   */
  headingLevel?: PanelHeadingLevel;
  /** A muted run after the title in the same heading (e.g. the month window the box shows); not a second heading. */
  titleNote?: string;
  /** A tighter box for a section inside a narrow pane: less padding and a `text-sm` heading. The accent
   * bar, the border and the heading element are the same, so it is still the one Panel shape. */
  compact?: boolean;
  children: ReactNode;
}

/**
 * A `Card` that names itself: the surface plus a heading that is visibly heavier than the body
 * text, marked by a short accent bar. The bar is decoration (`aria-hidden`); the heading is the
 * title text (plus the optional note) alone. No variant prop and no role/status/ownership logic: what goes inside is the
 * caller's, as with `Card`.
 */
export function Panel({ title, headingLevel = 2, titleNote, compact = false, className, children, ...rest }: PanelProps) {
  const Heading = HEADING_TAG[headingLevel];
  return (
    <Card className={cx(compact ? "p-3" : "p-4 md:px-5", className)} {...rest}>
      <Heading
        className={cx(
          "flex items-center gap-2 font-semibold text-slate-900 dark:text-slate-100",
          compact ? "text-sm" : "text-base",
        )}
      >
        <span aria-hidden="true" className="h-4 w-1 shrink-0 rounded-sm bg-accent dark:bg-indigo-400" />
        {title}
        {titleNote && (
          <>
            {" "}
            <span className="text-sm font-normal text-slate-600 dark:text-slate-400">{titleNote}</span>
          </>
        )}
      </Heading>
      {children}
    </Card>
  );
}
