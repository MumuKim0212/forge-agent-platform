// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment. Pins exactly what the
 * a11y-test plumbing exists for, at the primitive itself rather than at one
 * of its callers: `tabIndex` on the rendered element, `aria-current`
 * tracking the `selected` prop, the accessible name, `Enter` activating,
 * `Space` not activating, and a keydown from a nested control not
 * activating the row. `ListRow` carries no internal selection state (b4:
 * props only) — the caller owns `selected`, so "tracks selection" is
 * pinned by re-rendering with a different `selected` value, not by firing
 * a key and reading the DOM back.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ListRow } from "./ListRow";

function rowTree(selected: boolean, onActivate: () => void) {
  return (
    <ul>
      <ListRow selected={selected} ariaLabel="포탈 데모 프로젝트 상세 보기" onActivate={onActivate}>
        <span>내용</span>
        <button type="button">중첩된 버튼</button>
      </ListRow>
    </ul>
  );
}

function renderRow(selected: boolean, onActivate = vi.fn()) {
  const { rerender } = render(rowTree(selected, onActivate));
  return {
    onActivate,
    row: screen.getByRole("listitem", { name: "포탈 데모 프로젝트 상세 보기" }),
    rerenderSelected: (next: boolean) => rerender(rowTree(next, onActivate)),
  };
}

describe("ListRow — keyboard operability, focus, ARIA", () => {
  it("carries tabIndex 0 on the rendered element", () => {
    const { row } = renderRow(false);
    expect(row.tabIndex).toBe(0);
  });

  it("carries an aria-label naming the row (row/listitem have no accessible name from content alone)", () => {
    const { row } = renderRow(false);
    expect(row).toHaveAttribute("aria-label", "포탈 데모 프로젝트 상세 보기");
  });

  it("aria-current tracks the selected prop", () => {
    const { row, rerenderSelected } = renderRow(false);
    expect(row).toHaveAttribute("aria-current", "false");

    rerenderSelected(true);
    expect(row).toHaveAttribute("aria-current", "true");
  });

  it("Enter activates the row", () => {
    const { row, onActivate } = renderRow(false);
    fireEvent.keyDown(row, { key: "Enter" });

    expect(onActivate).toHaveBeenCalledTimes(1);
  });

  it("Space does not activate the row", () => {
    const { row, onActivate } = renderRow(false);
    fireEvent.keyDown(row, { key: " " });

    expect(onActivate).not.toHaveBeenCalled();
  });

  it("a keydown bubbling up from a nested control does not activate the row", () => {
    const { row, onActivate } = renderRow(false);
    const nestedButton = row.querySelector("button");
    if (!nestedButton) {
      throw new Error("expected the row to render a nested button");
    }

    fireEvent.keyDown(nestedButton, { key: "Enter" });

    expect(onActivate).not.toHaveBeenCalled();
  });

  it("renders as a <tr> with role row when as=\"tr\" is passed (b2: must support both host elements)", () => {
    const onActivate = vi.fn();
    render(
      <table>
        <tbody>
          <ListRow as="tr" selected={false} ariaLabel="템플릿 상세 보기" onActivate={onActivate}>
            <td>내용</td>
          </ListRow>
        </tbody>
      </table>,
    );

    const row = screen.getByRole("row", { name: "템플릿 상세 보기" });
    expect(row.tagName).toBe("TR");
    fireEvent.keyDown(row, { key: "Enter" });
    expect(onActivate).toHaveBeenCalledTimes(1);
  });
});
