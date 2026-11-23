/**
 * What the shell's navigation offers a user, and which entry is the current page. The paths
 * and labels are written out as literals: they are routes and pinned copy, and deriving them from
 * the module would let a rename move both sides at once.
 */
import { describe, expect, it } from "vitest";

import type { User } from "../types/api";
import {
  LOGIN_TITLE,
  SIGNUP_TITLE,
  currentNavTo,
  documentTitle,
  navGroupsFor,
  pageTitleFor,
  shellDocumentTitle,
  shellTitle,
} from "./shellNav";

function userWithRole(role: string): User {
  return { id: "u-1", name: "테스터", email: "t@example.test", role, team_id: null, created_at: "2026-01-01T00:00:00Z" };
}

describe("navGroupsFor", () => {
  it("offers every signed-in user the two workspace entries, in order, and no approval console below approver", () => {
    expect(navGroupsFor(userWithRole("requester")).workspace).toEqual([
      { to: "/projects", label: "프로젝트" },
      { to: "/projects/new", label: "새 요청" },
    ]);
  });

  it.each(["approver", "admin"])("offers the approval console, last, to %s (ADR 0048)", (role) => {
    expect(navGroupsFor(userWithRole(role)).workspace).toEqual([
      { to: "/projects", label: "프로젝트" },
      { to: "/projects/new", label: "새 요청" },
      { to: "/approvals", label: "승인 콘솔" },
    ]);
  });

  it("offers no approval console for an unrecognised role or no user (fail closed)", () => {
    for (const user of [userWithRole("superuser"), userWithRole(""), null]) {
      expect(navGroupsFor(user).workspace.map((item) => item.to)).toEqual(["/projects", "/projects/new"]);
    }
  });

  it("offers the one console entry to an admin and nobody below", () => {
    expect(navGroupsFor(userWithRole("admin")).administration).toEqual([{ to: "/admin", label: "관리자 콘솔" }]);
    expect(navGroupsFor(userWithRole("approver")).administration).toEqual([]);
    expect(navGroupsFor(userWithRole("requester")).administration).toEqual([]);
  });

  it("offers no console entry for an unrecognised role or no user (fail closed)", () => {
    expect(navGroupsFor(userWithRole("superuser")).administration).toEqual([]);
    expect(navGroupsFor(userWithRole("")).administration).toEqual([]);
    expect(navGroupsFor(null).administration).toEqual([]);
  });
});

describe("currentNavTo", () => {
  it.each([
    ["/projects", "/projects"],
    ["/projects/", "/projects"],
    ["/projects/abc-123", "/projects"],
    ["/projects/new", "/projects/new"],
    ["/approvals", "/approvals"],
    ["/admin", "/admin"],
  ])("%s is held by %s", (pathname, expected) => {
    expect(currentNavTo(pathname)).toBe(expected);
  });

  it("is held by no entry for a path that is not a nav destination", () => {
    expect(currentNavTo("/nope")).toBeNull();
    expect(currentNavTo("/")).toBeNull();
    expect(currentNavTo("/projectsX")).toBeNull();
  });
});

describe("pageTitleFor", () => {
  it("titles a workspace page with its nav label", () => {
    expect(pageTitleFor("/projects")).toBe("프로젝트");
    expect(pageTitleFor("/projects/abc-123")).toBe("프로젝트");
    expect(pageTitleFor("/projects/new")).toBe("새 요청");
    expect(pageTitleFor("/approvals")).toBe("승인 콘솔");
  });

  it("titles the console like any other entry, so its refusal has a heading too", () => {
    expect(pageTitleFor("/admin")).toBe("관리자 콘솔");
  });

  it("leaves the title to a page that carries its own (the 404)", () => {
    expect(pageTitleFor("/nope")).toBeNull();
  });
});

describe("shellTitle", () => {
  it("prefers the title a page supplied (a project's name) over the section's", () => {
    expect(shellTitle("프로젝트", "휴가 도구")).toBe("휴가 도구");
  });

  it("falls back to the section's title when the page has not supplied one", () => {
    expect(shellTitle("프로젝트", null)).toBe("프로젝트");
    expect(shellTitle(null, null)).toBeNull();
  });

  it("ignores a blank supplied title rather than drawing an empty heading", () => {
    expect(shellTitle("프로젝트", "")).toBe("프로젝트");
    expect(shellTitle("프로젝트", "   ")).toBe("프로젝트");
  });

  it("uses a supplied title even where the path has none", () => {
    expect(shellTitle(null, "휴가 도구")).toBe("휴가 도구");
  });
});

describe("the tab title of every route", () => {
  // Literals on purpose: the strings are what a tab, the history and a screen reader show.
  const ROUTES: Array<[path: string, title: string]> = [
    ["/projects", "프로젝트 · Forge"],
    ["/projects/new", "새 요청 · Forge"],
    ["/approvals", "승인 콘솔 · Forge"],
    ["/admin", "관리자 콘솔 · Forge"],
    ["/no/such/page", "페이지를 찾을 수 없습니다 · Forge"],
  ];
  it.each(ROUTES)("%s is titled %s", (path, title) => {
    expect(shellDocumentTitle(path, null)).toBe(title);
  });

  it("a project page is titled by the project's name once it is known, by the section before", () => {
    expect(shellDocumentTitle("/projects/abc", "휴가 도구")).toBe("휴가 도구 · Forge");
    expect(shellDocumentTitle("/projects/abc", null)).toBe("프로젝트 · Forge");
  });

  it("the sign-in pages have their own", () => {
    expect(documentTitle(LOGIN_TITLE)).toBe("로그인 · Forge");
    expect(documentTitle(SIGNUP_TITLE)).toBe("가입 요청 · Forge");
  });

  it("no title at all is just the product name", () => {
    expect(documentTitle(null)).toBe("Forge");
    expect(documentTitle("  ")).toBe("Forge");
  });
});
