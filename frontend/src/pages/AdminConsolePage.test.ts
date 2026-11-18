/**
 * `isTabVisible` carried two gates that nothing reached: the evals tab follows the role gate
 * `canRunEvals`, and the sign-up approvals tab follows the auth mode (its backend exists only in
 * `local` mode, so offering it elsewhere is a control that can only 404). It is exported and pinned
 * here as a truth table over every tab, every mode and every role (ADR 0021). The mode and role
 * words are contract strings with the backend and the identity context, so they are literals.
 */
import { describe, expect, it } from "vitest";

import type { AuthMode } from "../context/IdentityContext";
import type { User } from "../types/api";
import { isTabVisible, type TabId } from "./AdminConsolePage";

function user(role: string): User {
  return { id: "u1", name: "u1", email: "u1@example.com", role, team_id: null, created_at: "2026-10-17T00:00:00Z" };
}

const MODES: Array<AuthMode | null> = ["dev", "oidc", "local", null];
const ROLES = ["admin", "approver", "requester", "superuser", ""];
const USERS: Array<User | null> = [...ROLES.map(user), null];

const ALWAYS_OFFERED: TabId[] = ["usage", "roles", "deployments", "templates", "engine", "configuration"];

describe("isTabVisible", () => {
  it("offers the sign-up approvals tab in local mode and only there", () => {
    for (const who of USERS) {
      expect(isTabVisible("signups", "local", who), `local/${who?.role}`).toBe(true);
      for (const mode of ["dev", "oidc", null] as const) {
        expect(isTabVisible("signups", mode, who), `${mode}/${who?.role}`).toBe(false);
      }
    }
  });

  it("offers the evals tab to an admin and to nobody else, in every mode", () => {
    for (const mode of MODES) {
      expect(isTabVisible("evals", mode, user("admin")), `${mode}/admin`).toBe(true);
      for (const role of ["approver", "requester", "superuser", ""]) {
        expect(isTabVisible("evals", mode, user(role)), `${mode}/${role}`).toBe(false);
      }
      expect(isTabVisible("evals", mode, null), `${mode}/no user`).toBe(false);
    }
  });

  it("offers every other tab whatever the mode or the user (the page itself refuses a non-admin)", () => {
    for (const tab of ALWAYS_OFFERED) {
      for (const mode of MODES) {
        for (const who of USERS) {
          expect(isTabVisible(tab, mode, who), `${tab}/${mode}/${who?.role}`).toBe(true);
        }
      }
    }
  });

  it("the two gates do not leak into each other: local mode does not open evals, admin does not open signups", () => {
    expect(isTabVisible("evals", "local", user("requester"))).toBe(false);
    expect(isTabVisible("signups", "dev", user("admin"))).toBe(false);
    expect(isTabVisible("signups", "oidc", user("admin"))).toBe(false);
  });
});
