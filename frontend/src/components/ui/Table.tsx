import { useCallback, useEffect, useRef, useState } from "react";
import type {
  HTMLAttributes,
  TableHTMLAttributes,
  TdHTMLAttributes,
  ThHTMLAttributes,
} from "react";

import { cx } from "./cx";

/**
 * The header/cell shapes the five existing admin tables need —
 * `RolesPanel.tsx`, `SignupRequestsPanel.tsx`,
 * `TemplatesPanel.tsx`, `UsagePanel.tsx` (the four files duplicating private
 * `cell`/`head`/`WARN` constants) and `ConfigurationPanel.tsx`'s own inline
 * table. **None of those five files uses it yet** (their screens
 * arrive later) — this primitive exists so that work has
 * no shape left to invent. See the task's `## Result` for which primitive
 * replaces which constant.
 *
 * Compound export (`Table.Head`/`Table.Body`/`Table.Row`/`Table.HeaderCell`/
 * `Table.Cell`) rather than five separate named imports, since every caller
 * needs several of these together.
 */
/**
 * A wide table scrolls sideways inside its own box on a narrow screen. Columns past the
 * edge are otherwise invisible, so a soft shadow shows on whichever side still has more to scroll
 * to. Measured from the scroller (scroll position and size), so it works on any surface colour and
 * disappears when the table fits. Decoration only: the shadow carries no information a screen reader
 * needs, and the table stays the same scrollable region it was.
 */
function useScrollEdges() {
  const scroller = useRef<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ start: false, end: false });

  const measure = useCallback(() => {
    const el = scroller.current;
    if (el === null) return;
    const next = {
      start: el.scrollLeft > 1,
      end: el.scrollLeft + el.clientWidth < el.scrollWidth - 1,
    };
    setEdges((prev) => (prev.start === next.start && prev.end === next.end ? prev : next));
  }, []);

  useEffect(() => {
    measure();
    const el = scroller.current;
    if (el === null) return;
    window.addEventListener("resize", measure);
    // jsdom has no ResizeObserver; the window listener alone is enough there.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(el);
    if (el.firstElementChild) observer?.observe(el.firstElementChild);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [measure]);

  return { scroller, edges, measure };
}

const EDGE_SHADOW =
  "pointer-events-none absolute inset-y-0 w-5 transition-opacity from-slate-900/20 to-transparent dark:from-black/60";

function Root({ className, children, ...rest }: TableHTMLAttributes<HTMLTableElement>) {
  const { scroller, edges, measure } = useScrollEdges();
  return (
    <div className="relative">
      <div ref={scroller} onScroll={measure} className="overflow-x-auto">
        <table className={cx("w-full border-collapse text-left", className)} {...rest}>
          {children}
        </table>
      </div>
      <span
        aria-hidden="true"
        data-scroll-edge="start"
        className={cx(EDGE_SHADOW, "left-0 bg-linear-to-r", edges.start ? "opacity-100" : "opacity-0")}
      />
      <span
        aria-hidden="true"
        data-scroll-edge="end"
        className={cx(EDGE_SHADOW, "right-0 bg-linear-to-l", edges.end ? "opacity-100" : "opacity-0")}
      />
    </div>
  );
}

function Head({ className, children, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead className={cx("border-b border-slate-200 dark:border-slate-700", className)} {...rest}>
      {children}
    </thead>
  );
}

function Body({ className, children, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <tbody className={cx("divide-y divide-slate-100 dark:divide-slate-800", className)} {...rest}>
      {children}
    </tbody>
  );
}

function Row({ className, children, ...rest }: HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr className={cx("align-top", className)} {...rest}>
      {children}
    </tr>
  );
}

/** Mirrors the duplicated `head` constant: 12px, `slate-500`, left-aligned. */
function HeaderCell({ className, children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cx(
        "px-2 py-1.5 text-left text-xs font-medium text-slate-500 dark:text-slate-400",
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

/** Mirrors the duplicated `cell` constant: 14px, top-aligned. */
function Cell({ className, children, ...rest }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cx("px-2 py-1.5 align-top text-sm", className)} {...rest}>
      {children}
    </td>
  );
}

export const Table = Object.assign(Root, {
  Head,
  Body,
  Row,
  HeaderCell,
  Cell,
});
