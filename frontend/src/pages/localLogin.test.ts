/**
 * `LocalLoginForm`'s failure classification (ADR 0021: decision logic, not
 * rendering). There is one exception the
 * backend carves out of its otherwise-single failure message; this pins that
 * `classifyLocalLoginFailure` draws exactly that one line.
 */
import { describe, expect, it } from "vitest";

import { classifyLocalLoginFailure, localLoginErrorMessage } from "./localLogin";

describe("classifyLocalLoginFailure — 403 is the one exception", () => {
  it("403 -> awaiting_approval, carrying the backend's detail through unchanged", () => {
    // The exact literal `routers/auth_local._generic_login_failure`'s
    // sibling raises for a PENDING row — fed in as an input, not asserted
    // as an invented client-side string.
    const detail = "account is awaiting admin approval";

    expect(classifyLocalLoginFailure(403, detail)).toEqual({
      kind: "awaiting_approval",
      detail,
    });
  });
});

describe("classifyLocalLoginFailure — every other status is the generic error", () => {
  it("401 — the backend's merged unknown-email/wrong-password/locked message", () => {
    const detail = "invalid email or password";

    expect(classifyLocalLoginFailure(401, detail)).toEqual({ kind: "error", detail });
  });

  it("does not re-derive or drop the detail text for any other status", () => {
    for (const status of [400, 404, 422, 500, 502]) {
      expect(classifyLocalLoginFailure(status, "server said this")).toEqual({
        kind: "error",
        detail: "server said this",
      });
    }
  });

  it("an empty detail still classifies correctly — this function never invents copy", () => {
    expect(classifyLocalLoginFailure(401, "")).toEqual({ kind: "error", detail: "" });
    expect(classifyLocalLoginFailure(403, "")).toEqual({ kind: "awaiting_approval", detail: "" });
  });
});

describe("localLoginErrorMessage — the rendered text does not vary with detail", () => {
  // This is the test that would have caught an earlier defect:
  // every login failure rendered the backend's raw
  // `detail` string ("로그인에 실패했습니다: invalid email or password"),
  // in English, next to the rest of this Korean screen. Two different
  // `detail` strings — standing in for "the backend later got more
  // specific" — must still produce byte-identical rendered text, or the
  // backend's merge is leaking through the UI again.
  it("two different backend details produce the identical fixed sentence", () => {
    const unknownEmail = classifyLocalLoginFailure(401, "invalid email or password");
    const lockedAccount = classifyLocalLoginFailure(401, "account is locked, try again later");

    // Guard the shape, not just the values below — a change to
    // `classifyLocalLoginFailure`'s routing should fail this test loudly
    // instead of silently comparing two `awaiting_approval` failures.
    if (unknownEmail.kind !== "error" || lockedAccount.kind !== "error") {
      throw new Error('expected both classifications to be kind "error"');
    }

    const first = localLoginErrorMessage(unknownEmail);
    const second = localLoginErrorMessage(lockedAccount);

    expect(first).toBe(second);
    // The literal is spelled here, not imported from `localLogin.ts`
    // so a rename on the production side must
    // show up as a failing assertion, not silently follow along.
    expect(first).toBe("로그인에 실패했습니다. 이메일과 비밀번호를 확인해 주세요.");
  });

  it("an empty detail still produces the same fixed sentence", () => {
    const empty = classifyLocalLoginFailure(401, "");
    if (empty.kind !== "error") throw new Error('expected kind "error"');

    expect(localLoginErrorMessage(empty)).toBe(
      "로그인에 실패했습니다. 이메일과 비밀번호를 확인해 주세요.",
    );
  });
});
