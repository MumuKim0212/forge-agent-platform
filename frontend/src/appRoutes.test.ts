import { matchRoutes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { appRoutes } from "./App";

/** The route pattern a path resolves to, or null when nothing matches. */
function resolvedPattern(pathname: string): string | null {
  const matches = matchRoutes(appRoutes, pathname);
  if (!matches) return null;
  return matches[matches.length - 1].route.path ?? null;
}

describe("route table: the 404 catch-all shadows no real route", () => {
  // Written out as literals on purpose: deriving them from `appRoutes` would
  // let a deleted route disappear from both sides at once.
  const KNOWN: Array<[path: string, pattern: string]> = [
    ["/", "/"],
    ["/login", "/login"],
    ["/signup", "/signup"],
    ["/projects", "/projects"],
    ["/projects/new", "/projects/new"],
    ["/projects/abc-123", "/projects/:id"],
    ["/approvals", "/approvals"],
    ["/admin", "/admin"],
  ];

  it.each(KNOWN)("%s still resolves to %s", (path, pattern) => {
    expect(resolvedPattern(path)).toBe(pattern);
  });

  it.each([
    "/requests/new",
    "/admin/whatever",
    "/projects/abc/extra",
    "/nope",
    "/approvals/1",
    "/login/extra",
  ])("%s resolves to the catch-all", (path) => {
    expect(resolvedPattern(path)).toBe("*");
  });
});

describe("route table: the catch-all sits behind the identity guard", () => {
  // An unknown path must go through the same guarded layout as a real
  // protected path, so an anonymous visitor is redirected to login and the
  // 404 does not reveal which paths exist. The layout route has no `path`, so
  // the guard is "the match chain has a pathless parent".
  function chainPaths(pathname: string): Array<string | undefined> {
    return (matchRoutes(appRoutes, pathname) ?? []).map((m) => m.route.path);
  }

  it("an unknown path matches under the pathless guarded layout, like /approvals", () => {
    expect(chainPaths("/approvals")).toEqual([undefined, "/approvals"]);
    expect(chainPaths("/requests/new")).toEqual([undefined, "*"]);
  });

  it("the public routes are not under it", () => {
    expect(chainPaths("/login")).toEqual(["/login"]);
    expect(chainPaths("/signup")).toEqual(["/signup"]);
  });
});
