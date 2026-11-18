/**
 * What the shell's left navigation offers and which entry is the current page. Pure, no
 * React, so the decisions are testable in the `node` environment (ADR 0021).
 *
 * Presentation only: an entry that is absent here is not a permission, and an entry that is
 * present is not an authorisation. Every endpoint behind it still answers for itself.
 */
import { canAdministerPlatform } from "../auth/roles";
import type { User } from "../types/api";

/** The 404's heading and its tab title; the page itself (`NotFoundPage`) draws the heading. */
export const NOT_FOUND_TITLE = "페이지를 찾을 수 없습니다";

/** The sign-in pages sit outside the shell, so their tab titles are named here with the rest. */
export const LOGIN_TITLE = "로그인";
export const SIGNUP_TITLE = "가입 요청";

/** The suffix every tab title ends with. */
const APP_NAME = "Forge";

/** `<page title> · Forge`, or just `Forge` when there is no page title. */
export function documentTitle(pageTitle: string | null): string {
  if (pageTitle === null || pageTitle.trim() === "") return APP_NAME;
  return `${pageTitle} · ${APP_NAME}`;
}

/**
 * The tab title of a page inside the shell: the page's own title (a project's name) or the
 * section's, and the 404's when the path belongs to no section.
 */
export function shellDocumentTitle(pathname: string, pageTitle: string | null): string {
  return documentTitle(shellTitle(pageTitleFor(pathname), pageTitle) ?? NOT_FOUND_TITLE);
}

export interface NavItem {
  to: string;
  label: string;
}

/** The workspace entries every signed-in user sees. The paths are routes (`App.appRoutes`). */
const WORKSPACE_ITEMS: readonly NavItem[] = [
  { to: "/projects", label: "프로젝트" },
  { to: "/projects/new", label: "새 요청" },
  { to: "/approvals", label: "승인 콘솔" },
];

/**
 * The one console entry. It goes through the same predicate the page guards itself with
 * (`AdminConsolePage`), so a hidden link and an open page cannot disagree
 *.
 */
const ADMIN_ITEM: NavItem = { to: "/admin", label: "관리자 콘솔" };

export interface NavGroups {
  workspace: NavItem[];
  /** Empty for anyone who may not administer the platform. */
  administration: NavItem[];
}

export function navGroupsFor(user: User | null): NavGroups {
  return {
    workspace: [...WORKSPACE_ITEMS],
    administration: canAdministerPlatform(user) ? [ADMIN_ITEM] : [],
  };
}

/** The `to` of the entry that holds `pathname`, or null when no entry does (e.g. the 404). */
export function currentNavTo(pathname: string): string | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (path === "/projects/new") return "/projects/new";
  if (path === "/projects" || path.startsWith("/projects/")) return "/projects";
  if (path === "/approvals") return "/approvals";
  if (path === "/admin") return "/admin";
  return null;
}

/**
 * The page title the shell draws as the page's `h1` from the path alone, or null when the page
 * carries its own (the 404). It is the nav label of the entry that holds the path; the console
 * (`/admin`) is titled the same way, so a non-admin's refusal has its heading too. A page whose
 * title is data, not a section (a project's name), supplies it through `shellTitle`.
 */
export function pageTitleFor(pathname: string): string | null {
  const to = currentNavTo(pathname);
  if (to === null) return null;
  const all = [...WORKSPACE_ITEMS, ADMIN_ITEM];
  return all.find((item) => item.to === to)?.label ?? null;
}

/**
 * The title the shell draws: the page's own title when it supplied one, otherwise the path's. A
 * blank supplied title is ignored, so a page that has not loaded its name yet (or whose name is
 * only whitespace) is still titled by its section rather than by an empty heading.
 */
export function shellTitle(pathTitle: string | null, pageTitle: string | null): string | null {
  if (pageTitle !== null && pageTitle.trim() !== "") return pageTitle;
  return pathTitle;
}
