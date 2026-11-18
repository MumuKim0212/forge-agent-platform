/**
 * The `month` query parameter, on its own.
 *
 * It is one line of production code and it is here because of how it fails: a
 * renamed or dropped parameter is invisible to `tsc` (both sides are strings) and
 * silently degrades to "the backend answered for the current month", so the
 * console shows this month's figures under a historical month's heading. Nothing
 * throws and nothing looks wrong (contract strings that
 * cross a boundary always get a literal).
 */
import { describe, expect, it } from "vitest";

import { dashboardQuery, monthQuery } from "./usage";

describe("monthQuery", () => {
  it("builds the parameter `routers/usage.py` reads", () => {
    expect(monthQuery("2026-07")).toBe("?month=2026-07");
  });

  it("omits the parameter entirely for the current month", () => {
    // Not `?month=`: `routers/usage._window` 400s on a value it cannot parse,
    // whereas an absent parameter is what "current month" means to the backend.
    expect(monthQuery(null)).toBe("");
    expect(monthQuery(undefined)).toBe("");
    expect(monthQuery("")).toBe("");
  });

  it("encodes the value", () => {
    expect(monthQuery("2026-07&month=2020-01")).toBe("?month=2026-07%26month%3D2020-01");
  });
});

describe("dashboardQuery", () => {
  it("sends the two parameters `routers/usage.get_cost_dashboard` reads, by those names", () => {
    expect(dashboardQuery("2026-07", 25)).toBe("?month=2026-07&limit=25");
  });

  it("omits what is not given, so the backend's defaults apply", () => {
    expect(dashboardQuery(null)).toBe("");
    expect(dashboardQuery("", undefined)).toBe("");
    expect(dashboardQuery("2026-07")).toBe("?month=2026-07");
    expect(dashboardQuery(null, 5)).toBe("?limit=5");
  });

  it("encodes both values", () => {
    expect(dashboardQuery("2026-07&x=1")).toBe("?month=2026-07%26x%3D1");
  });
});
