import type { ReactNode } from "react";

import { cx } from "./cx";

export interface DescriptionListItem {
  term: ReactNode;
  detail: ReactNode;
}

export interface DescriptionListProps {
  items: ReadonlyArray<DescriptionListItem>;
  className?: string;
}

/**
 * A real `<dl>`/`<dt>`/`<dd>` — the label/value pairs
 * `ConfigurationPanel`'s `Section`/`Row` helpers and `TemplatesPanel`'s
 * detail table both hand-rolled as two-column `<table>`s, neither of which
 * is a list of anything tabular. `<dt>`/`<dd>` pairs are grouped in a
 * `<div>` per item — valid inside `<dl>` (HTML5 content model: zero or more
 * groups of one or more `<dt>` followed by one or more `<dd>`, optionally
 * wrapped) — so each row can be laid out as a flex pair without breaking
 * the list semantics a screen reader relies on. Carries no role/status/
 * ownership logic (b5): `items` is a plain prop list.
 */
export function DescriptionList({ items, className }: DescriptionListProps) {
  return (
    <dl className={cx("divide-y divide-slate-100 dark:divide-slate-800", className)}>
      {items.map((item, index) => (
        <div key={index} className="flex items-baseline justify-between gap-4 py-2 text-sm">
          <dt className="text-slate-500 dark:text-slate-400">{item.term}</dt>
          <dd className="text-right font-medium text-slate-900 dark:text-slate-100">{item.detail}</dd>
        </div>
      ))}
    </dl>
  );
}
