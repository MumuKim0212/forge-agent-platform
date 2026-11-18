/**
 * `IdentitySwitcherPage`'s select handler used to render `err.message`
 * — supposedly a network-failure fallback, but `api/client.ts` always
 * throws `ApiError extends Error`, so the Korean fallback never actually
 * rendered. `identitySwitchFailureMessage` is the decision layer that
 * replaces it (ADR 0021: decision logic, not rendering).
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { identitySwitchFailureMessage } from "./IdentitySwitcherPage";

describe("identitySwitchFailureMessage — the backend's detail never reaches the screen", () => {
  it("returns the caller's Korean fallback for an ApiError, not its detail", () => {
    const err = new ApiError(401, "Unknown user identity");
    const message = identitySwitchFailureMessage(err, "아이덴티티 선택에 실패했습니다.");

    expect(message).toBe("아이덴티티 선택에 실패했습니다.");
    expect(message).not.toContain("Unknown user identity");
  });

  it("ignores a non-ApiError failure the same way", () => {
    expect(identitySwitchFailureMessage(new Error("network down"), "아이덴티티 선택에 실패했습니다.")).toBe(
      "아이덴티티 선택에 실패했습니다.",
    );
    expect(identitySwitchFailureMessage("not even an Error", "아이덴티티 선택에 실패했습니다.")).toBe(
      "아이덴티티 선택에 실패했습니다.",
    );
  });

  it("never leaks the server's own words, whatever the detail says", () => {
    const err = new ApiError(500, "distinctive-server-detail-xyz");

    expect(identitySwitchFailureMessage(err, "아이덴티티 선택에 실패했습니다.")).not.toContain(
      "distinctive-server-detail-xyz",
    );
  });
});
