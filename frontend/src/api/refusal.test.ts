/**
 * A coded refusal is read from the PREFIX of the 4xx `detail`. The
 * wire shapes below are written out by hand, never produced by the code under test.
 */
import { describe, expect, it } from "vitest";

import { ApiError, NETWORK_ERROR_STATUS } from "./client";
import { isStateRefusal, refusalCode } from "./refusal";

describe("refusalCode — the code is the prefix of a string detail", () => {
  it("reads the code the backend put in front of its message", () => {
    const err = new ApiError(
      409,
      "PROJECT_ARCHIVED: project p1 is archived; un-archive it before confirming a deploy",
      "PROJECT_ARCHIVED: project p1 is archived; un-archive it before confirming a deploy",
    );

    expect(refusalCode(err)).toBe("PROJECT_ARCHIVED");
  });

  it("reads a code with digits and underscores", () => {
    const detail = "NOT_LATEST_SUCCESSFUL: build b1 is not the project's latest successful build";

    expect(refusalCode(new ApiError(400, detail, detail))).toBe("NOT_LATEST_SUCCESSFUL");
  });

  it("returns null when the detail has no code", () => {
    const detail = "another deployment of project p1 is already in flight";

    expect(refusalCode(new ApiError(409, detail, detail))).toBeNull();
  });

  it("does not take an ordinary sentence that merely starts with a capital word", () => {
    // `Project owner or admin role required` is a real backend detail: not code-shaped.
    const detail = "Project owner or admin role required";

    expect(refusalCode(new ApiError(403, detail, detail))).toBeNull();
  });

  it("needs the colon and the space: a bare upper-case word is not a code", () => {
    expect(refusalCode(new ApiError(409, "PROJECT_ARCHIVED", "PROJECT_ARCHIVED"))).toBeNull();
    expect(refusalCode(new ApiError(409, "PROJECT_ARCHIVED:x", "PROJECT_ARCHIVED:x"))).toBeNull();
  });

  it("does not take a lower-case or mid-string code", () => {
    expect(refusalCode(new ApiError(409, "archived: x", "archived: x"))).toBeNull();
    expect(refusalCode(new ApiError(409, "note PROJECT_ARCHIVED: x", "note PROJECT_ARCHIVED: x"))).toBeNull();
  });

  it("returns null for a detail that is not a string (a pydantic 422 array)", () => {
    const detail = [{ type: "string_too_long", loc: ["body", "nl_request"] }];

    expect(refusalCode(new ApiError(422, "Unprocessable Entity", detail))).toBeNull();
  });

  it("returns null for a failure that never reached the server, and for a non-ApiError", () => {
    expect(
      refusalCode(new ApiError(NETWORK_ERROR_STATUS, "offline", "PROJECT_ARCHIVED: x", "network")),
    ).toBeNull();
    expect(refusalCode(new Error("PROJECT_ARCHIVED: x"))).toBeNull();
    expect(refusalCode("PROJECT_ARCHIVED: x")).toBeNull();
    expect(refusalCode(null)).toBeNull();
  });
});

describe("isStateRefusal — only a 400 or 409 from the server says the view is stale", () => {
  const cases: Array<[string, unknown, boolean]> = [
    ["400", new ApiError(400, "x"), true],
    ["409", new ApiError(409, "x"), true],
    ["403 (a permission, a re-read does not change it)", new ApiError(403, "x"), false],
    ["404", new ApiError(404, "x"), false],
    ["422", new ApiError(422, "x"), false],
    ["500", new ApiError(500, "x"), false],
    ["503", new ApiError(503, "x"), false],
    ["network failure", new ApiError(NETWORK_ERROR_STATUS, "x", undefined, "network"), false],
    ["a 2xx whose body would not parse", new ApiError(200, "x", undefined, "parse"), false],
    ["a plain Error", new Error("x"), false],
    ["not an error at all", undefined, false],
  ];

  it.each(cases)("%s", (_label, err, expected) => {
    expect(isStateRefusal(err)).toBe(expected);
  });
});
