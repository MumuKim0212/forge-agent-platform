/**
 * The `?status=` query parameter on `GET /admin/signup-requests`
 *, same reasoning `api/usage.test.ts` pins `monthQuery` with: a
 * renamed or dropped parameter is invisible to `tsc` and degrades silently
 * (contract strings that cross a boundary always get
 * a literal).
 */
import { describe, expect, it } from "vitest";

import { signupStatusQuery } from "./localAccounts";

describe("signupStatusQuery", () => {
  it("builds the parameter routers/auth_local.py reads", () => {
    expect(signupStatusQuery("PENDING")).toBe("?status=PENDING");
    expect(signupStatusQuery("REJECTED")).toBe("?status=REJECTED");
  });

  it("omits the parameter entirely when no status is given", () => {
    // Not `?status=`: the backend endpoint takes a plain `str` default, not
    // `str | None`, so an empty value would filter on the literal empty
    // string rather than mean "use the server default".
    expect(signupStatusQuery(null)).toBe("");
    expect(signupStatusQuery(undefined)).toBe("");
    expect(signupStatusQuery("")).toBe("");
  });

  it("encodes the value", () => {
    expect(signupStatusQuery("PENDING&status=ACTIVE")).toBe("?status=PENDING%26status%3DACTIVE");
  });
});
