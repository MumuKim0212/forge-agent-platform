/**
 * The one place `Timeline` learns that a build is the slice-1 stub state. The status word
 * is the backend's `BuildStatus.NOT_IMPLEMENTED`, so it is a literal here.
 */
import { describe, expect, it } from "vitest";

import { NOT_IMPLEMENTED_BUILD_STATUS, isBuildNotImplemented } from "./buildStatus";

describe("isBuildNotImplemented", () => {
  it("pins the backend's word", () => {
    expect(NOT_IMPLEMENTED_BUILD_STATUS).toBe("NOT_IMPLEMENTED");
  });

  it("is true for the stub state only", () => {
    expect(isBuildNotImplemented("NOT_IMPLEMENTED")).toBe(true);
    for (const status of ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "not_implemented", "", null, undefined, 0]) {
      expect(isBuildNotImplemented(status), String(status)).toBe(false);
    }
  });
});
