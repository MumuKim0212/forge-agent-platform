// @vitest-environment jsdom
/**
 * ADR 0021 §Amendment 2026-08-30 (landmarks, current-page marking and keyboard operability
 * only). The shell's left navigation is a named landmark, marks the current page with
 * `aria-current="page"` (not colour alone), and offers the 관리자 콘솔 entry to an admin only.
 *
 * Runs in jsdom, not a browser: it does not show how the shell looks, nor that it reflows on a
 * narrow screen. Hiding the admin entry is presentation; the backend refuses a non-admin on its own.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Layout } from "./Layout";
import { usePageTitle } from "./pageTitle";

const identity: { role: string; mode: "dev" | "oidc" } = { role: "requester", mode: "oidc" };
vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: { id: "u1", name: "김민지", email: "m@example.test", role: identity.role, team_id: null, created_at: "" },
    mode: identity.mode,
    logout: async () => undefined,
  }),
}));

beforeEach(() => {
  identity.role = "requester";
  identity.mode = "oidc";
});

function renderShell(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/projects" element={<p>목록 화면</p>} />
          <Route path="/projects/new" element={<p>요청 화면</p>} />
          <Route path="/projects/:id" element={<p>상세 화면</p>} />
          <Route path="/approvals" element={<p>승인 화면</p>} />
          <Route path="/admin" element={<p>콘솔 화면</p>} />
          <Route path="/nope" element={<p>없는 화면</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function navLinks() {
  return within(screen.getByRole("navigation", { name: "주 메뉴" })).getAllByRole("link");
}

describe("the shell's landmarks", () => {
  it("has one named navigation, one banner and one main", () => {
    renderShell("/projects");

    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(screen.getByRole("navigation", { name: "주 메뉴" })).toBeTruthy();
    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it("titles the page with one h1 inside main", () => {
    renderShell("/projects");

    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toBe("프로젝트");
    expect(within(screen.getByRole("main")).getByRole("heading", { level: 1 })).toBe(headings[0]);
  });
});

describe("current-page marking", () => {
  function currentLabels() {
    return navLinks()
      .filter((link) => link.getAttribute("aria-current") === "page")
      .map((link) => link.textContent);
  }

  it("marks exactly the entry for the page", () => {
    renderShell("/approvals");

    expect(currentLabels()).toEqual(["승인 콘솔"]);
  });

  it("keeps 프로젝트 current on a project's page, and 새 요청 on the intake page", () => {
    const first = renderShell("/projects/abc");
    expect(currentLabels()).toEqual(["프로젝트"]);
    first.unmount();

    renderShell("/projects/new");
    expect(currentLabels()).toEqual(["새 요청"]);
  });

  it("marks nothing on a path that is not a nav destination", () => {
    renderShell("/nope");

    expect(currentLabels()).toEqual([]);
  });
});

describe("the 관리자 콘솔 entry", () => {
  it("is in the navigation for an admin, last", () => {
    identity.role = "admin";
    renderShell("/projects");

    expect(within(screen.getByRole("navigation", { name: "주 메뉴" })).getByRole("link", { name: "관리자 콘솔" }).getAttribute("href")).toBe(
      "/admin",
    );
    expect(navLinks().map((link) => link.textContent)).toEqual(["프로젝트", "새 요청", "승인 콘솔", "관리자 콘솔"]);
  });

  it.each(["requester", "approver", "superuser"])("is absent for role %s", (role) => {
    identity.role = role;
    renderShell("/projects");

    expect(screen.queryByRole("link", { name: "관리자 콘솔" })).toBeNull();
    expect(navLinks().map((link) => link.textContent)).toEqual(["프로젝트", "새 요청", "승인 콘솔"]);
  });
});

describe("the top bar", () => {
  it("shows 전환 in dev mode only, and 로그아웃 in both", () => {
    identity.mode = "dev";
    const dev = renderShell("/projects");
    expect(screen.getByRole("link", { name: "전환" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "로그아웃" })).toBeTruthy();
    dev.unmount();

    identity.mode = "oidc";
    renderShell("/projects");
    expect(screen.queryByRole("link", { name: "전환" })).toBeNull();
    expect(screen.getByRole("button", { name: "로그아웃" })).toBeTruthy();
  });
});

describe("the page title", () => {
  function Named({ name }: { name: string | null }) {
    usePageTitle(name);
    return <p>상세 화면</p>;
  }

  function renderDetail(name: string | null, path = "/projects/abc") {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/projects" element={<p>목록 화면</p>} />
            <Route path="/projects/:id" element={<Named name={name} />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
  }

  it("is the project's name, in the one h1, once a project page supplies it", () => {
    renderDetail("휴가 도구");

    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toBe("휴가 도구");
  });

  it("is the section's label while the project page has no name to supply", () => {
    renderDetail(null);

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("프로젝트");
  });

  it("is the section's label again after leaving the project page", async () => {
    const view = renderDetail("휴가 도구");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("휴가 도구");

    view.unmount();
    renderShell("/projects");

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("프로젝트");
  });

  it("titles the console, and so its refusal, with the shell's h1", () => {
    identity.role = "requester";
    renderShell("/admin");

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("관리자 콘솔");
  });
});
