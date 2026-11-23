/**
 * The role ladder and the two matrix-row predicates (ADR 0021: decision logic,
 * not rendering).
 *
 * Why this file exists rather than a component test: `canOperateDeployment`'s
 * owner-**or**-admin composition was an actual defect in `ProjectDetailPage`
 * earlier (a role check where an ownership check belonged), and the
 * fail-closed default is the kind of thing that regresses to fail-open in a
 * one-line edit. Both are truth tables, so they are pinned as truth tables.
 */
import { describe, expect, it } from "vitest";

import {
  Role,
  canAdministerPlatform,
  canDecideApprovals,
  canDeactivateUsers,
  canEditQuota,
  canGrantRoles,
  canOpenApprovalConsole,
  canOperateDeployment,
  canReadBuildTrace,
  canReviewSignups,
  canRunEvals,
  canSelectAgentEngine,
  holdsAtLeast,
  isSelfRoleChange,
} from "./roles";
import type { User } from "../types/api";

const OWNER_ID = "user-owner";
const OTHER_ID = "user-other";

function makeUser(role: string, id: string = OWNER_ID): User {
  return {
    id,
    name: "테스터",
    email: "tester@example.com",
    role,
    team_id: null,
    created_at: "2026-07-30T00:00:00Z",
  };
}

const project = { owner_id: OWNER_ID };

/** Values a build has never heard of. A role string arrives from the server, so
 * this is a reachable state (roles.ts §holdsAtLeast), not a hypothetical. */
const UNRECOGNISED = ["superadmin", "owner", "REQUESTER", "Admin", "admin ", "", "guest"];

describe("the Role values are a cross-service contract", () => {
  it("match the strings backend services/roles.py sends", () => {
    // Pinned as literals, and this is the one assertion in the file that does
    // not feed `Role.*` in as an input — every other test here passes the
    // constants to a function that ranks them via a `RANK` table built from the
    // same constants, so a rename is internally self-consistent and the truth
    // tables all stay green.
    //
    // The failure this catches is not cosmetic. These strings are the wire
    // contract: the backend sends `role: "approver"` on `GET /users/me`, and a
    // frontend-side rename makes `RANK[serverRole]` `undefined` for every real
    // user. `holdsAtLeast` then fails closed, so every approver silently loses
    // approve/reject and every admin loses the deploy override — invisible to
    // `tsc` (all three are strings) and presenting as a permissions bug rather
    // than a crash. Asserted on the whole object so adding a rung also has to
    // be deliberate: a new role needs new ladder tests, not a silent pass.
    expect(Role).toEqual({
      REQUESTER: "requester",
      APPROVER: "approver",
      ADMIN: "admin",
    });
  });
});

describe("holdsAtLeast — the ladder", () => {
  it("holds at every rung, including equality", () => {
    // requester
    expect(holdsAtLeast(Role.REQUESTER, Role.REQUESTER)).toBe(true);
    expect(holdsAtLeast(Role.REQUESTER, Role.APPROVER)).toBe(false);
    expect(holdsAtLeast(Role.REQUESTER, Role.ADMIN)).toBe(false);
    // approver
    expect(holdsAtLeast(Role.APPROVER, Role.REQUESTER)).toBe(true);
    expect(holdsAtLeast(Role.APPROVER, Role.APPROVER)).toBe(true);
    expect(holdsAtLeast(Role.APPROVER, Role.ADMIN)).toBe(false);
    // admin
    expect(holdsAtLeast(Role.ADMIN, Role.REQUESTER)).toBe(true);
    expect(holdsAtLeast(Role.ADMIN, Role.APPROVER)).toBe(true);
    expect(holdsAtLeast(Role.ADMIN, Role.ADMIN)).toBe(true);
  });

  it("is a strict comparison, not a substring or prefix match", () => {
    expect(holdsAtLeast("administrator", Role.ADMIN)).toBe(false);
    expect(holdsAtLeast("admin2", Role.REQUESTER)).toBe(false);
  });
});

describe("holdsAtLeast — fail-closed", () => {
  it("grants nothing for null or undefined", () => {
    for (const minimum of [Role.REQUESTER, Role.APPROVER, Role.ADMIN]) {
      expect(holdsAtLeast(null, minimum)).toBe(false);
      expect(holdsAtLeast(undefined, minimum)).toBe(false);
    }
  });

  it("grants nothing for an unrecognised role — not even the baseline rung", () => {
    // The failure mode this pins: treating an unknown role as `requester`
    // (the bottom of the ladder) instead of as no role at all.
    for (const role of UNRECOGNISED) {
      expect(holdsAtLeast(role, Role.REQUESTER)).toBe(false);
      expect(holdsAtLeast(role, Role.APPROVER)).toBe(false);
      expect(holdsAtLeast(role, Role.ADMIN)).toBe(false);
    }
  });

  it("does not inherit privileges from `Object.prototype` keys", () => {
    // `RANK` is a plain object literal, so `RANK["toString"]` is *not*
    // `undefined` and the explicit guard in `holdsAtLeast` does not catch it —
    // the numeric comparison does. Pinned because the result is right for a
    // subtler reason than the other unrecognised values.
    expect(holdsAtLeast("constructor", Role.REQUESTER)).toBe(false);
    expect(holdsAtLeast("toString", Role.ADMIN)).toBe(false);
    expect(holdsAtLeast("valueOf", Role.REQUESTER)).toBe(false);
  });
});

describe("canDecideApprovals — approver and above", () => {
  it("is gated on approver", () => {
    expect(canDecideApprovals(makeUser(Role.REQUESTER))).toBe(false);
    expect(canDecideApprovals(makeUser(Role.APPROVER))).toBe(true);
    expect(canDecideApprovals(makeUser(Role.ADMIN))).toBe(true);
  });

  it("denies with no user and with an unrecognised role", () => {
    expect(canDecideApprovals(null)).toBe(false);
    for (const role of UNRECOGNISED) {
      expect(canDecideApprovals(makeUser(role))).toBe(false);
    }
  });

  it("does not consider ownership — approving is a pure role decision", () => {
    // The complement of `canOperateDeployment`: owning the project must not
    // buy an approval right.
    expect(canDecideApprovals(makeUser(Role.REQUESTER, project.owner_id))).toBe(false);
  });
});

describe("canOperateDeployment — owner OR admin, two separate axes", () => {
  // {owner, non-owner} x {requester, approver, admin}. Ownership is
  // not a rung on the ladder, so neither axis alone answers this.
  const table: Array<{ role: string; owner: boolean; expected: boolean; why: string }> = [
    { role: Role.REQUESTER, owner: true, expected: true, why: "owner needs no role grant" },
    { role: Role.APPROVER, owner: true, expected: true, why: "owner, incidentally approver" },
    { role: Role.ADMIN, owner: true, expected: true, why: "owner and admin" },
    { role: Role.REQUESTER, owner: false, expected: false, why: "neither axis" },
    { role: Role.APPROVER, owner: false, expected: false, why: "approver does NOT override ownership" },
    { role: Role.ADMIN, owner: false, expected: true, why: "admin overrides ownership" },
  ];

  for (const { role, owner, expected, why } of table) {
    it(`${owner ? "owner" : "non-owner"} + ${role} -> ${expected} (${why})`, () => {
      const user = makeUser(role, owner ? OWNER_ID : OTHER_ID);
      expect(canOperateDeployment(user, project)).toBe(expected);
    });
  }

  it("leaves ownership as the way in when the role is unrecognised", () => {
    // Fail-closed must not become a second denial for the legitimate owner:
    // an unknown role loses the admin override, nothing more.
    for (const role of UNRECOGNISED) {
      expect(canOperateDeployment(makeUser(role, OWNER_ID), project)).toBe(true);
      expect(canOperateDeployment(makeUser(role, OTHER_ID), project)).toBe(false);
    }
  });

  it("denies when either operand is missing", () => {
    expect(canOperateDeployment(null, project)).toBe(false);
    expect(canOperateDeployment(makeUser(Role.ADMIN, OTHER_ID), null)).toBe(false);
    expect(canOperateDeployment(null, null)).toBe(false);
  });

  it("compares owner ids exactly — no case folding, no prefix match", () => {
    expect(canOperateDeployment(makeUser(Role.REQUESTER, OWNER_ID.toUpperCase()), project)).toBe(
      false,
    );
    expect(canOperateDeployment(makeUser(Role.REQUESTER, `${OWNER_ID}x`), project)).toBe(false);
  });
});

/**
 * The three admin-console gates. They resolve to the same rung today, so
 * the risk is not that one of them is wrong *now* — it is that the console's
 * gating lives inline in JSX where nothing can reach it (the `ProjectDetailPage`
 * shape that had to be fixed, and un-mountable by construction here). Each one is a
 * named predicate with its own truth table so a rung change has to be
 * deliberate per decision, and so the console's gates are covered at all.
 *
 * Two more follow, same rung and same shape: `canReviewSignups`
 * (approve/reject/reopen a signup request) and `canDeactivateUsers`
 * (deactivate an active user) — separate backend endpoint groups, separate
 * predicates, per the `canGrantRoles`/`canEditQuota` precedent above.
 */
describe("the admin console gates — admin only, each on its own", () => {
  const gates: Array<{ name: string; gate: (user: User | null) => boolean }> = [
    { name: "canAdministerPlatform", gate: canAdministerPlatform },
    { name: "canEditQuota", gate: canEditQuota },
    { name: "canGrantRoles", gate: canGrantRoles },
    { name: "canReviewSignups", gate: canReviewSignups },
    { name: "canDeactivateUsers", gate: canDeactivateUsers },
    { name: "canSelectAgentEngine", gate: canSelectAgentEngine },
    { name: "canRunEvals", gate: canRunEvals },
  ];

  for (const { name, gate } of gates) {
    it(`${name} holds for admin and nothing below it`, () => {
      expect(gate(makeUser(Role.REQUESTER))).toBe(false);
      expect(gate(makeUser(Role.APPROVER))).toBe(false);
      expect(gate(makeUser(Role.ADMIN))).toBe(true);
    });

    it(`${name} is fail-closed with no user and with an unrecognised role`, () => {
      expect(gate(null)).toBe(false);
      for (const role of UNRECOGNISED) {
        expect(gate(makeUser(role))).toBe(false);
      }
    });

    it(`${name} does not consider ownership — the console is not a per-project surface`, () => {
      // The complement of `canOperateDeployment`. Owning a project buys reach
      // over *that* project (ownership is a
      // separate axis); it must not open a cross-project console, and it must
      // not let a subject raise its own quota (the limits name
      // `admin` alone).
      expect(gate(makeUser(Role.REQUESTER, project.owner_id))).toBe(false);
      expect(gate(makeUser(Role.APPROVER, project.owner_id))).toBe(false);
    });
  }
});

describe("canOpenApprovalConsole (ADR 0048)", () => {
  // Literal expectations, one row per rung: approver and above, nobody below, nothing unknown.
  it.each([
    ["requester", false],
    ["approver", true],
    ["admin", true],
  ])("%s: %s", (role, expected) => {
    expect(canOpenApprovalConsole(makeUser(role))).toBe(expected);
  });

  it("is false with no user, and for every unrecognised role (fail closed)", () => {
    expect(canOpenApprovalConsole(null)).toBe(false);
    for (const role of UNRECOGNISED) {
      expect(canOpenApprovalConsole(makeUser(role))).toBe(false);
    }
  });

  it("does not consider ownership: owning a project does not open the cross-project console", () => {
    expect(canOpenApprovalConsole(makeUser(Role.REQUESTER, project.owner_id))).toBe(false);
  });
});

describe("isSelfRoleChange — which row is 'you'", () => {
  it("is true only when the ids match exactly", () => {
    expect(isSelfRoleChange(makeUser(Role.ADMIN, OWNER_ID), { id: OWNER_ID })).toBe(true);
    expect(isSelfRoleChange(makeUser(Role.ADMIN, OWNER_ID), { id: OTHER_ID })).toBe(false);
    expect(isSelfRoleChange(makeUser(Role.ADMIN, OWNER_ID), { id: OWNER_ID.toUpperCase() })).toBe(
      false,
    );
    expect(isSelfRoleChange(makeUser(Role.ADMIN, OWNER_ID), { id: `${OWNER_ID}x` })).toBe(false);
  });

  it("does not depend on the role — every rung can be the acting user", () => {
    for (const role of [Role.REQUESTER, Role.APPROVER, Role.ADMIN, ...UNRECOGNISED]) {
      expect(isSelfRoleChange(makeUser(role, OWNER_ID), { id: OWNER_ID })).toBe(true);
    }
  });

  it("is false when either operand is missing", () => {
    expect(isSelfRoleChange(null, { id: OWNER_ID })).toBe(false);
    expect(isSelfRoleChange(makeUser(Role.ADMIN, OWNER_ID), null)).toBe(false);
    expect(isSelfRoleChange(null, null)).toBe(false);
  });
});

describe("canReadBuildTrace", () => {
  // Literal expectations: owner OR approver+, each axis independent, fail-closed.
  const cases: Array<{ role: string; owner: boolean; expected: boolean }> = [
    { role: "requester", owner: true, expected: true },
    { role: "requester", owner: false, expected: false },
    { role: "approver", owner: true, expected: true },
    { role: "approver", owner: false, expected: true },
    { role: "admin", owner: true, expected: true },
    { role: "admin", owner: false, expected: true },
    { role: "something-new", owner: false, expected: false },
    { role: "something-new", owner: true, expected: true },
  ];

  for (const { role, owner, expected } of cases) {
    it(`${role} ${owner ? "owning" : "not owning"} the project: ${expected}`, () => {
      expect(canReadBuildTrace(makeUser(role, owner ? OWNER_ID : OTHER_ID), project)).toBe(expected);
    });
  }

  it("is false with no user, and for a non-owner with no project", () => {
    expect(canReadBuildTrace(null, project)).toBe(false);
    expect(canReadBuildTrace(makeUser(Role.REQUESTER, OTHER_ID), null)).toBe(false);
  });

  it("still lets approver+ in when the project is not loaded", () => {
    expect(canReadBuildTrace(makeUser(Role.APPROVER, OTHER_ID), null)).toBe(true);
  });
});
