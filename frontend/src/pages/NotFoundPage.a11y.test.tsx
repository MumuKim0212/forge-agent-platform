// @vitest-environment jsdom
/**
 * An unknown path inside the app shell shows the 404 screen: a heading,
 * a link back to the project list, and no live
 * region (a wrong address is not something the user just did, so nothing is
 * announced over their reading). Operability and ARIA only; the copy is pinned
 * once by the constants' literals here, not re-asserted elsewhere.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { NotFoundPage } from "./NotFoundPage";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/projects" element={<p>목록</p>} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("NotFoundPage", () => {
  it("has a heading and a link to /projects, and no live region", () => {
    renderAt("/admin/whatever");
    expect(screen.getByRole("heading", { name: "페이지를 찾을 수 없습니다" })).toBeTruthy();
    const link = screen.getByRole("link", { name: "프로젝트 목록으로 돌아가기" });
    expect(link.getAttribute("href")).toBe("/projects");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

});
