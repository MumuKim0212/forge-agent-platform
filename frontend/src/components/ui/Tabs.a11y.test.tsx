// @vitest-environment jsdom
/**
 * Scope is exactly ADR 0021 §Amendment's: keyboard operability,
 * focus, and ARIA on the `kind="tablist"` shape `AdminConsolePage` uses —
 * not rendering, not copy, not layout. A couple of `kind="filter"` checks
 * are included too, since T4's whole point is that the two kinds must stay
 * observably different (no `role="tab"`, no `aria-selected` on the filter
 * strip) — that boundary is exactly the kind of thing a mounted assertion
 * is for.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Tabs } from "./Tabs";

const TABS = [
  { id: "a", label: "Alpha" },
  { id: "b", label: "Beta" },
  { id: "c", label: "Gamma" },
] as const;

describe("Tabs kind=\"tablist\" — real tab semantics", () => {
  it("renders role=tablist with one role=tab per entry", () => {
    render(<Tabs idBase="t" tabs={TABS} activeId="a" onChange={vi.fn()} />);
    expect(screen.getByRole("tablist")).toBeTruthy();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
  });

  it("exactly one tab is aria-selected", () => {
    render(<Tabs idBase="t" tabs={TABS} activeId="b" onChange={vi.fn()} />);
    const selected = screen.getAllByRole("tab").filter((el) => el.getAttribute("aria-selected") === "true");
    expect(selected).toHaveLength(1);
    expect(selected[0]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Beta" })).toBe(selected[0]);
  });

  it("roving tabIndex: the selected tab is 0, the others are -1", () => {
    render(<Tabs idBase="t" tabs={TABS} activeId="a" onChange={vi.fn()} />);
    expect(screen.getByRole("tab", { name: "Alpha" }).tabIndex).toBe(0);
    expect(screen.getByRole("tab", { name: "Beta" }).tabIndex).toBe(-1);
    expect(screen.getByRole("tab", { name: "Gamma" }).tabIndex).toBe(-1);
  });

  it("ArrowRight moves selection to the next tab and focuses it", () => {
    const onChange = vi.fn();
    render(<Tabs idBase="t" tabs={TABS} activeId="a" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("b");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Beta" }));
  });

  it("ArrowLeft from the first tab wraps to the last", () => {
    const onChange = vi.fn();
    render(<Tabs idBase="t" tabs={TABS} activeId="a" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowLeft" });
    expect(onChange).toHaveBeenCalledWith("c");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Gamma" }));
  });

  it("a non-arrow key does not move selection", () => {
    const onChange = vi.fn();
    render(<Tabs idBase="t" tabs={TABS} activeId="a" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("aria-controls on a tab points at the id TabPanel would generate", () => {
    render(<Tabs idBase="admin-console" tabs={TABS} activeId="a" onChange={vi.fn()} />);
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute(
      "aria-controls",
      "admin-console-panel-a",
    );
  });
});

describe("Tabs kind=\"filter\" — a different idiom, not a tablist", () => {
  it("renders no role=tab and no role=tablist", () => {
    render(<Tabs idBase="f" kind="filter" tabs={TABS} activeId="a" onChange={vi.fn()} />);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });

  it("marks the active entry with aria-current and disables it, like the pre-existing status strip", () => {
    render(<Tabs idBase="f" kind="filter" tabs={TABS} activeId="b" onChange={vi.fn()} />);
    const active = screen.getByRole("button", { name: "Beta" });
    expect(active).toHaveAttribute("aria-current", "true");
    expect(active).toBeDisabled();
    const inactive = screen.getByRole("button", { name: "Alpha" });
    expect(inactive).toHaveAttribute("aria-current", "false");
    expect(inactive).not.toBeDisabled();
  });
});
