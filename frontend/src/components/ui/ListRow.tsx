import type { KeyboardEvent, ReactNode } from "react";

import { shouldActivateRow } from "../../a11y/activation";
import { cx } from "./cx";

export type ListRowElement = "li" | "tr";

export interface ListRowProps {
  /** Which element to render as — `li` (`ApprovalConsolePage`)
   * or `tr` (`TemplatesPanel`, once it adopts this primitive). See
   * this file's own docstring for why both are supported by one component
   * rather than two. */
  as?: ListRowElement;
  selected: boolean;
  ariaLabel: string;
  onActivate: () => void;
  children: ReactNode;
  className?: string;
}

const SELECTED_CLASSES = "bg-accent-subtle dark:bg-accent-subtle-dark";

/**
 * The `tabIndex`/`aria-current`/`aria-label`/`shouldActivateRow`/selected-
 * background wiring that was hand-written twice (`TemplatesPanel.tsx`'s `<tr>`,
 * `ApprovalConsolePage.tsx`'s `<li>`) — one place now.
 *
 * Carries no role/status/ownership logic (b4): `selected` and `onActivate`
 * are plain props. The row keeps its element's own implicit role (`row` for
 * `tr`, `listitem` for `li`) rather than claiming `role="button"`, which
 * would override that role and leave the surrounding `<table>`/`<ul>`
 * incoherent to a screen reader — so this
 * primitive never renders a `<button>` itself.
 *
 * **Two element types, one component, because a caller cannot mix them
 * afterward**: `ApprovalConsolePage.ApprovalList` needs `<li>` inside a
 * `<ul>`; `TemplatesPanel` needs `<tr>` inside a `<tbody>`. A
 * generic `as={ListRowElement}` prop keeps the keyboard/ARIA logic in one
 * place without forcing either caller into the other's markup shape — the
 * two JSX branches below are the only place that differs, and they render
 * identical props onto whichever intrinsic element `as` names.
 */
export function ListRow({
  as = "li",
  selected,
  ariaLabel,
  onActivate,
  children,
  className,
}: ListRowProps) {
  const rowClassName = cx("cursor-pointer", selected && SELECTED_CLASSES, className);

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    // Target-guarded (the nested-interactive-content hazard):
    // a keydown that started on a nested control (e.g. the
    // approval list's preview link) bubbles up here too, and must not be
    // hijacked into activating the row instead.
    if (shouldActivateRow(event.key, event.target, event.currentTarget)) {
      event.preventDefault();
      onActivate();
    }
  }

  if (as === "tr") {
    return (
      <tr
        tabIndex={0}
        aria-current={selected}
        aria-label={ariaLabel}
        onClick={onActivate}
        onKeyDown={handleKeyDown}
        className={rowClassName}
      >
        {children}
      </tr>
    );
  }

  return (
    <li
      tabIndex={0}
      aria-current={selected}
      aria-label={ariaLabel}
      onClick={onActivate}
      onKeyDown={handleKeyDown}
      className={rowClassName}
    >
      {children}
    </li>
  );
}
