/**
 * Destination preservation across the 401 → login chain (ADR 0021: decision
 * logic, not rendering — `LoginPage` itself is never mounted here).
 *
 * What the chain is, and where each link is tested:
 *
 *   RequireIdentity  `/login?next=` + encodeURIComponent(pathname + search)
 *   LoginPage        safeNextPath(searchParams.get("next"))        <- here
 *   authApi          loginStartUrl(next)                          <- here
 *   backend          `_safe_next_path` -> login-state cookie      <- backend tests
 *
 * The interesting failures are all in the middle: a `next` that carries its own
 * query string losing everything after the `?`, and an off-site `next` reaching
 * a navigation. Both are pinned as a round trip rather than by asserting the
 * encoded string, because the encoded string is not the contract — surviving the
 * round trip is.
 */
import { describe, expect, it } from "vitest";

import { loginPathFor } from "../App";
import { authApi } from "../api/auth";
import { DEFAULT_POST_LOGIN_PATH, safeNextPath } from "./LoginPage";

/** What `RequireIdentity` builds — the real production function, so the encoding
 * is under test rather than re-implemented here. */
function loginUrlFor(pathname: string, search = ""): string {
  return loginPathFor(pathname, search);
}

/** What `LoginPage` reads back out of it, via react-router's `useSearchParams`
 * (i.e. `URLSearchParams` semantics). */
function nextParamOf(url: string): string | null {
  return new URL(url, "http://forge.test").searchParams.get("next");
}

describe("safeNextPath — same-site absolute paths only", () => {
  it("keeps a deep path, including its query string", () => {
    expect(safeNextPath("/projects/fc196ca1-1111-2222-3333-444455556666")).toBe(
      "/projects/fc196ca1-1111-2222-3333-444455556666",
    );
    expect(safeNextPath("/approvals?status=PENDING")).toBe("/approvals?status=PENDING");
  });

  it("falls back to the default when there is nothing to return to", () => {
    expect(safeNextPath(null)).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("")).toBe(DEFAULT_POST_LOGIN_PATH);
  });

  it("defaults to a real authenticated destination, never back to login", () => {
    // Pinned as a literal deliberately: every other assertion here compares
    // against the constant, so its *value* would otherwise go unchecked — and
    // `/login` is precisely the value that would turn the fallback into a
    // redirect loop.
    expect(DEFAULT_POST_LOGIN_PATH).toBe("/projects");
  });

  it("rejects anything that could leave the site", () => {
    // `//host` is protocol-relative — the open-redirect shape.
    expect(safeNextPath("//evil.test/steal")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("https://evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("http://evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("javascript:alert(1)")).toBe(DEFAULT_POST_LOGIN_PATH);
  });

  it("rejects a leading backslash, which a URL parser normalises to //", () => {
    // `/\host` starts with
    // `/` and not `//`, so the two checks above alone admit it — and a URL
    // parser treats a leading `/\` the same as `//` (protocol-relative).
    // Not exploitable today (nothing here hands `next` to a raw navigation,
    // and the backend's own `_safe_next_path` percent-encodes it into the
    // `Location` header regardless), but that safety rested on a library
    // property rather than on this function; this closes the gap locally.
    expect(safeNextPath("/\\evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\\/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    // A backslash anywhere in an otherwise same-site path is rejected too —
    // simpler and safer than trying to special-case only a leading one.
    expect(safeNextPath("/projects/\\evil")).toBe(DEFAULT_POST_LOGIN_PATH);
  });

  it("rejects a control character anywhere in the path", () => {
    // a browser strips
    // tab/LF/CR (and only those two rules, until now) before parsing a URL,
    // so `/\t/evil.test` resolves to `//evil.test` — another origin — while
    // starting with `/` and not `//` and carrying no backslash. Six cases,
    // matching the backend's `_safe_next_path` list side by side (see this
    // task's Result): tab, LF, CR, NUL, DEL, and one more C0 control
    // character (ESC) so the rule is proven to be "any control character",
    // not a hardcoded set of three.
    expect(safeNextPath("/\t/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\n/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\r/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\u0000/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\u007f/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("/\u001b/evil.test")).toBe(DEFAULT_POST_LOGIN_PATH);
  });

  it("rejects relative paths — the value is used as an absolute destination", () => {
    expect(safeNextPath("projects")).toBe(DEFAULT_POST_LOGIN_PATH);
    expect(safeNextPath("../projects")).toBe(DEFAULT_POST_LOGIN_PATH);
  });
});

describe("loginPathFor — its own /login?next= prefix", () => {
  it("is pinned as a literal, not only proven by round-tripping through it", () => {
    // Every round-trip test below reads `next` back out with
    // `URLSearchParams`, so a `loginPathFor` that returned, say,
    // `/signin?next=...` would keep every one of those tests green while
    // `RequireIdentity` navigated to a route that does not exist. This is
    // the one assertion on the prefix itself.
    expect(loginPathFor("/projects", "")).toBe("/login?next=%2Fprojects");
  });
});

describe("the destination survives the whole chain", () => {
  // "/projects" is deliberately not a row here: it equals
  // `DEFAULT_POST_LOGIN_PATH`, so a `loginPathFor`/`safeNextPath` chain
  // broken anywhere still lands on that same value and the row would pass
  // regardless of what the function under test actually did.
  // The two rows below both use a
  // destination that differs from the default, so each can actually fail.
  const destinations = ["/projects/fc196ca1-1111-2222-3333-444455556666", "/approvals"];

  for (const pathname of destinations) {
    it(`${pathname} round-trips through the login URL`, () => {
      const recovered = safeNextPath(nextParamOf(loginUrlFor(pathname)));

      expect(recovered).toBe(pathname);
    });
  }

  it("a destination with its own query string arrives intact", () => {
    // Without `encodeURIComponent` the `&`/`=` would be parsed as extra
    // parameters of the *login* URL and the tail would be silently dropped.
    const recovered = safeNextPath(nextParamOf(loginUrlFor("/approvals", "?status=PENDING&tab=2")));

    expect(recovered).toBe("/approvals?status=PENDING&tab=2");
  });

  it("reaches the backend login start still intact", () => {
    const recovered = safeNextPath(nextParamOf(loginUrlFor("/projects/abc", "?tab=deploy")));
    const startUrl = authApi.loginStartUrl(recovered);

    expect(startUrl.startsWith("/api/auth/login?next=")).toBe(true);
    expect(nextParamOf(startUrl)).toBe("/projects/abc?tab=deploy");
  });

  it("an off-site next is neutralised before it reaches the backend login start", () => {
    const recovered = safeNextPath(nextParamOf(loginUrlFor("//evil.test/steal")));

    expect(nextParamOf(authApi.loginStartUrl(recovered))).toBe(DEFAULT_POST_LOGIN_PATH);
  });
});
