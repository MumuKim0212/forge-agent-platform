/**
 * `LoginPage`'s mode dispatch (ADR 0021: decision logic, not rendering).
 * Acceptance criterion: "`dev` mode keeps its identity switcher and
 * `oidc` mode keeps its SSO click, both unchanged. A test pins that each
 * mode renders its own entry path." — pinned here as a full truth table
 * over `loginViewFor`, since `LoginPage` itself cannot be mounted.
 */
import { describe, expect, it } from "vitest";

import { loginViewFor } from "./loginView";

describe("loginViewFor — still resolving", () => {
  it("is loading while `loading` is true, regardless of mode or session", () => {
    expect(loginViewFor({ loading: true, mode: null, signedIn: false })).toBe("loading");
    expect(loginViewFor({ loading: true, mode: "dev", signedIn: true })).toBe("loading");
    expect(loginViewFor({ loading: true, mode: "local", signedIn: false })).toBe("loading");
    expect(loginViewFor({ loading: true, mode: "oidc", signedIn: true })).toBe("loading");
  });

  it("is loading when mode has not resolved yet, even if `loading` already flipped", () => {
    // Defensive: a mode-detection race should not read as any concrete mode.
    expect(loginViewFor({ loading: false, mode: null, signedIn: false })).toBe("loading");
  });
});

describe("loginViewFor — dev mode is reachable even while signed in", () => {
  it("is the switcher whether or not a user is already selected", () => {
    // Dev mode doubles as the switch-identity surface — the one case where
    // being signed in is not a reason to redirect away.
    expect(loginViewFor({ loading: false, mode: "dev", signedIn: false })).toBe("dev-switcher");
    expect(loginViewFor({ loading: false, mode: "dev", signedIn: true })).toBe("dev-switcher");
  });
});

describe("loginViewFor — local and oidc redirect once signed in", () => {
  it("local, signed in -> redirect", () => {
    expect(loginViewFor({ loading: false, mode: "local", signedIn: true })).toBe("redirect");
  });

  it("oidc, signed in -> redirect", () => {
    expect(loginViewFor({ loading: false, mode: "oidc", signedIn: true })).toBe("redirect");
  });
});

describe("loginViewFor — each remaining mode renders its own entry path", () => {
  it("local, not signed in -> the local login form", () => {
    expect(loginViewFor({ loading: false, mode: "local", signedIn: false })).toBe("local-login");
  });

  it("oidc, not signed in -> the SSO panel", () => {
    expect(loginViewFor({ loading: false, mode: "oidc", signedIn: false })).toBe("oidc-sso");
  });
});
