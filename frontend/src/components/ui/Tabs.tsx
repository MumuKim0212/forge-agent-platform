import type { KeyboardEvent, ReactNode } from "react";

import { cx } from "./cx";
import { FOCUS_RING } from "./focusRing";

export interface TabDefinition<TabId extends string> {
  id: TabId;
  label: string;
}

/**
 * `"tablist"` (default) switches panel content in place — real ARIA tab
 * semantics (`role="tablist"`/`"tab"`/`"tabpanel"`, `aria-selected`, roving
 * `tabIndex`, Left/Right arrow movement). `"filter"` narrows a list without
 * replacing what is beside it — `disabled`+`aria-current`, no `role="tab"`,
 * no arrow keys.
 *
 * **Why one component carries both:** `AdminConsolePage`'s
 * tabs are the tabs pattern by definition — they switch which panel is on
 * screen — and should carry its ARIA. `SignupRequestsPanel`'s status strip
 * *filters* a list that stays in place, which is a defensible different
 * idiom (it already used `disabled`+`aria-current` before this component
 * existed). Giving the filter strip `role="tab"`/`aria-selected` would be
 * wrong — a screen reader would announce three "tabs" that don't switch to
 * three different views — so the two kinds render genuinely different
 * markup rather than one shape reused past where it fits. `kind` is a
 * shape switch, the same reason `ListRow.as` exists, not a
 * role/status/ownership branch — no version of it reads a prop from
 * `auth/roles` or `context/IdentityContext` (b5).
 */
export type TabsKind = "tablist" | "filter";

export interface TabsProps<TabId extends string> {
  /** Prefixes the generated `id`s that link a tab button to its panel
   * (`aria-controls`/`aria-labelledby`) — must be unique per `Tabs`
   * instance on a page. Unused in `"filter"` mode (nothing there wires
   * `aria-controls`, since a filter strip has no associated tabpanel). */
  idBase: string;
  tabs: ReadonlyArray<TabDefinition<TabId>>;
  activeId: TabId;
  onChange: (id: TabId) => void;
  kind?: TabsKind;
  className?: string;
}

export function tabElementId(idBase: string, tabId: string): string {
  return `${idBase}-tab-${tabId}`;
}

export function tabPanelElementId(idBase: string, tabId: string): string {
  return `${idBase}-panel-${tabId}`;
}

const TAB_BASE =
  "rounded-t-control border-b-2 px-3 py-2 text-sm font-medium transition-colors " +
  FOCUS_RING;
// Light by default, dark through the OS-preference variant (the console now sits in the
// shared light shell, so the selected tab is no longer tuned for a forced-dark surface). The accent
// on the page background measures 6.0:1 and the idle label 7.2:1; in dark the lighter indigo is used.
// The selected tab is also marked by the bar beneath it and `aria-selected`, not by colour alone.
const TAB_SELECTED = "border-accent text-accent dark:border-indigo-400 dark:text-indigo-300";
const TAB_UNSELECTED =
  "border-transparent text-slate-600 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-200";

const FILTER_BASE =
  "rounded-control border px-3 py-1.5 text-sm font-medium transition-colors " +
  `${FOCUS_RING} disabled:cursor-default`;
// The status strip's selected pill text is the accent on its subtle fill (5.6:1 in light); in dark the
// lighter indigo on the dark fill. A selected pill is also `disabled` + `aria-current`, so it is
// not marked by colour alone.
const FILTER_SELECTED =
  "border-accent bg-accent-subtle text-accent dark:border-indigo-400 dark:bg-accent-subtle-dark dark:text-indigo-300";
const FILTER_UNSELECTED =
  "border-slate-300 bg-white text-slate-600 hover:bg-slate-50 " +
  "dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700";

/**
 * The tab-strip wiring `AdminConsolePage.TABS` and `SignupRequestsPanel.
 * STATUS_TABS` each hand-wrote separately, in one place — see
 * `kind` above for why they still render two different shapes. Carries no
 * role/status/ownership logic (b5): `tabs`/`activeId`/`onChange` are plain
 * props, and gating which tabs are *offered* stays the caller's job
 * (`AdminConsolePage`'s own `isTabVisible` is unchanged by this primitive).
 */
export function Tabs<TabId extends string>({
  idBase,
  tabs,
  activeId,
  onChange,
  kind = "tablist",
  className,
}: TabsProps<TabId>) {
  function handleTablistKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const currentIndex = tabs.findIndex((t) => t.id === activeId);
    const delta = event.key === "ArrowRight" ? 1 : -1;
    const nextIndex = (currentIndex + delta + tabs.length) % tabs.length;
    const next = tabs[nextIndex];
    onChange(next.id);
    document.getElementById(tabElementId(idBase, next.id))?.focus();
  }

  if (kind === "filter") {
    return (
      <div className={cx("flex flex-wrap gap-2", className)}>
        {tabs.map((t) => {
          const selected = t.id === activeId;
          return (
            <button
              key={t.id}
              type="button"
              aria-current={selected}
              disabled={selected}
              onClick={() => onChange(t.id)}
              className={cx(FILTER_BASE, selected ? FILTER_SELECTED : FILTER_UNSELECTED)}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div
      role="tablist"
      aria-orientation="horizontal"
      onKeyDown={handleTablistKeyDown}
      className={cx("flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-700", className)}
    >
      {tabs.map((t) => {
        const selected = t.id === activeId;
        return (
          <button
            key={t.id}
            id={tabElementId(idBase, t.id)}
            role="tab"
            type="button"
            aria-selected={selected}
            aria-controls={tabPanelElementId(idBase, t.id)}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
            className={cx(TAB_BASE, selected ? TAB_SELECTED : TAB_UNSELECTED)}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

export interface TabPanelProps<TabId extends string> {
  idBase: string;
  tabId: TabId;
  children: ReactNode;
  className?: string;
  /** Whether the panel is itself a keyboard stop (default: yes). A panel that sits inside a focusable,
   * named scroll region passes `false`, so the same content is not two stops in a row. */
  focusable?: boolean;
}

/** The `role="tabpanel"` half of `kind="tablist"` — `aria-labelledby` points
 * back at the tab that owns it, generated the same way `tabElementId` does
 * so the two can never drift apart. Not used by `kind="filter"`: a filter
 * strip narrows a list in place and has no separate panel to label. */
export function TabPanel<TabId extends string>({
  idBase,
  tabId,
  children,
  className,
  focusable = true,
}: TabPanelProps<TabId>) {
  return (
    <div
      role="tabpanel"
      id={tabPanelElementId(idBase, tabId)}
      aria-labelledby={tabElementId(idBase, tabId)}
      tabIndex={focusable ? 0 : undefined}
      className={className}
    >
      {children}
    </div>
  );
}
