// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30 (focus, ARIA and keyboard operability only). When a page
 * throws while rendering, the user must still have a way out: the fallback is announced, takes
 * focus (the element that held it is gone), and its one action works from the keyboard -- and the
 * shell around the page (nav, logout) is still there and still works.
 *
 * This runs in jsdom, not a browser. It shows that the boundary catches a render exception and
 * that `Layout` places it around the outlet and resets it on navigation; it does not show how any
 * of it looks, nor that `window.location.reload()` reloads a real page (the reload action is
 * injected here).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";
import { Layout } from "./Layout";

const logout = vi.fn(async () => undefined);
vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: { id: "u1", name: "김민지", role: "requester" },
    mode: "oidc",
    logout,
  }),
}));

function Boom(): never {
  throw new Error("render exploded");
}

beforeEach(() => {
  logout.mockClear();
  // React logs every caught render error, and so does the boundary; the noise is not the point.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

describe("ErrorBoundary", () => {
  it("renders its children untouched when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>정상 화면</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("정상 화면")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("announces the failure as an alert and moves focus to its heading", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert").textContent).toContain("예기치 않은 오류");
    const heading = screen.getByRole("heading", { name: "화면을 표시하지 못했습니다" });
    expect(document.activeElement).toBe(heading);
  });

  it("puts the cause in the console and none of it on screen", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    expect(console.error).toHaveBeenCalledWith("A page failed to render.", expect.any(Error), expect.any(String));
    expect(document.body.textContent).not.toContain("render exploded");
  });

  it("offers a reload that works from the keyboard", async () => {
    const onReload = vi.fn();
    const user = userEvent.setup();
    render(
      <ErrorBoundary onReload={onReload}>
        <Boom />
      </ErrorBoundary>,
    );

    await user.tab();
    const button = screen.getByRole("button", { name: "페이지 새로 고침" });
    expect(document.activeElement).toBe(button);
    await user.keyboard("{Enter}");

    expect(onReload).toHaveBeenCalledTimes(1);
  });
});

describe("Layout holds a boundary around the page", () => {
  function renderApp(initialPath: string) {
    return render(
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/projects" element={<p>프로젝트 화면</p>} />
            <Route path="/boom" element={<Boom />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
  }

  it("keeps the nav and the logout button when the page throws", async () => {
    const user = userEvent.setup();
    renderApp("/boom");

    expect(screen.getByRole("alert").textContent).toContain("예기치 않은 오류");
    expect(screen.getByRole("link", { name: "프로젝트" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "새 요청" })).toBeTruthy();
    const logoutButton = screen.getByRole("button", { name: "로그아웃" });
    await user.click(logoutButton);

    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("recovers when the user moves to another page: the fallback does not outlive the page that caused it", async () => {
    const user = userEvent.setup();
    renderApp("/boom");
    expect(screen.queryByRole("alert")).not.toBeNull();

    await user.click(screen.getByRole("link", { name: "프로젝트" }));

    expect(screen.getByText("프로젝트 화면")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
