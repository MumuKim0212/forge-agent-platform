/**
 * `ProjectRegistryPage`'s fetch had no failure branch at all: a
 * rejected `GET /projects` left the empty-list copy on screen — an error
 * looking like success. `projectRegistryFailureMessage` is the same
 * module-level decision-layer shape used by the other four
 * pages (ADR 0021: decision logic, not rendering), new here because this
 * page never had one.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { projectRegistryFailureMessage } from "./ProjectRegistryPage";

describe("projectRegistryFailureMessage — the backend's detail never reaches the screen", () => {
  it("returns the caller's Korean fallback for an ApiError, not its detail", () => {
    const err = new ApiError(500, "internal server error");
    const message = projectRegistryFailureMessage(err, "프로젝트 목록을 불러오지 못했습니다.");

    expect(message).toBe("프로젝트 목록을 불러오지 못했습니다.");
    expect(message).not.toContain("internal server error");
  });

  it("ignores a non-ApiError failure the same way", () => {
    expect(
      projectRegistryFailureMessage(new Error("network down"), "프로젝트 목록을 불러오지 못했습니다."),
    ).toBe("프로젝트 목록을 불러오지 못했습니다.");
    expect(
      projectRegistryFailureMessage("not even an Error", "프로젝트 목록을 불러오지 못했습니다."),
    ).toBe("프로젝트 목록을 불러오지 못했습니다.");
  });

  it("never leaks the server's own words, whatever the detail says", () => {
    const err = new ApiError(503, "distinctive-server-detail-xyz");

    expect(projectRegistryFailureMessage(err, "프로젝트 목록을 불러오지 못했습니다.")).not.toContain(
      "distinctive-server-detail-xyz",
    );
  });
});
