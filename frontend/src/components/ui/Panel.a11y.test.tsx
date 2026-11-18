// @vitest-environment jsdom
/**
 * A `Panel`'s title is a heading whose level the caller chooses, so a panel inside a region that is
 * already headed at level 2 (the admin console's tab panel) does not put a second level-2 heading beside it.
 * Scope is the outline a screen reader's heading list shows: the tag, the default, the name.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Panel, panelHeadingTag } from "./Panel";

describe("Panel heading level", () => {
  it("defaults to an h2, as every caller before the level became a prop got", () => {
    render(<Panel title="배포 제어">본문</Panel>);

    expect(screen.getByRole("heading", { level: 2, name: "배포 제어" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3 })).toBeNull();
  });

  it("renders the title at the level it is given, and at no other", () => {
    render(
      <Panel title="프로젝트별" headingLevel={3}>
        본문
      </Panel>,
    );

    expect(screen.getByRole("heading", { level: 3, name: "프로젝트별" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  });

  it("maps each allowed level to its own tag", () => {
    expect([2, 3, 4].map((level) => panelHeadingTag(level as 2 | 3 | 4))).toEqual(["h2", "h3", "h4"]);
  });

  it("keeps the note inside the same heading, after the title, with a space between", () => {
    render(
      <Panel title="사용자별" titleNote="— 2026-10" headingLevel={3}>
        본문
      </Panel>,
    );

    expect(screen.getByRole("heading", { level: 3 }).textContent).toBe("사용자별 — 2026-10");
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });

  it("sits one level above a sub-section heading with no level skipped", () => {
    render(
      <div>
        <h2>탭 이름</h2>
        <Panel title="비용 대시보드" headingLevel={3}>
          <h4>일별 지출</h4>
        </Panel>
      </div>,
    );

    const levels = screen.getAllByRole("heading").map((h) => Number(h.tagName.slice(1)));
    expect(levels).toEqual([2, 3, 4]);
  });
});

describe("Panel compact", () => {
  it("keeps the heading element, its level and its name, and the accent bar stays decoration", () => {
    const { container } = render(
      <Panel title="요청 내용" headingLevel={3} compact>
        본문
      </Panel>,
    );

    expect(screen.getByRole("heading", { level: 3, name: "요청 내용" })).toBeInTheDocument();
    expect(container.querySelector("[aria-hidden=true]")).not.toBeNull();
    expect(container.firstElementChild?.className).toContain("p-3");
    expect(container.firstElementChild?.className).not.toContain("p-4");
  });
});
