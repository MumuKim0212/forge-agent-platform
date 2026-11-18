/**
 * Signup-form validation (ADR 0021: decision logic, not rendering). Mirrors
 * backend `schemas/local_accounts.SignupCreate` — this suite pins the
 * client-side approximation's behaviour, not a character-for-character copy
 * of the backend's regex (the backend remains the enforcement point either
 * way; the rule about not re-implementing
 * production logic does not apply across the language boundary here, since
 * neither side imports the other's validator).
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { SIGNUP_PASSWORD_MIN_LENGTH, isSignupValid, signupSubmitFailureMessage, validateSignup } from "./signup";

const VALID: Parameters<typeof validateSignup>[0] = {
  email: "user@example.com",
  name: "김민지",
  department: "플랫폼팀",
  password: "correct horse",
};

describe("validateSignup — a fully valid form has no errors", () => {
  it("returns an empty error object", () => {
    expect(validateSignup(VALID)).toEqual({});
    expect(isSignupValid(validateSignup(VALID))).toBe(true);
  });
});

describe("validateSignup — email", () => {
  it("rejects empty and whitespace-only", () => {
    expect(validateSignup({ ...VALID, email: "" }).email).toBeTruthy();
    expect(validateSignup({ ...VALID, email: "   " }).email).toBeTruthy();
  });

  it("rejects shapes that do not look like an address", () => {
    for (const bad of ["no-at-sign", "user@", "user@host", "@host.com", "user @host.com"]) {
      expect(validateSignup({ ...VALID, email: bad }).email).toBeTruthy();
    }
  });

  it("accepts a leading/trailing-whitespace address that is otherwise valid", () => {
    // Trimmed before the shape check, mirroring the backend's `.strip()`.
    expect(validateSignup({ ...VALID, email: "  user@example.com  " }).email).toBeUndefined();
  });
});

describe("validateSignup — name and department are both required free text", () => {
  it("rejects empty and whitespace-only name", () => {
    expect(validateSignup({ ...VALID, name: "" }).name).toBeTruthy();
    expect(validateSignup({ ...VALID, name: "   " }).name).toBeTruthy();
  });

  it("rejects empty and whitespace-only department", () => {
    expect(validateSignup({ ...VALID, department: "" }).department).toBeTruthy();
    expect(validateSignup({ ...VALID, department: "   " }).department).toBeTruthy();
  });
});

describe("validateSignup — password", () => {
  it(`rejects shorter than ${SIGNUP_PASSWORD_MIN_LENGTH} characters`, () => {
    expect(validateSignup({ ...VALID, password: "a".repeat(SIGNUP_PASSWORD_MIN_LENGTH - 1) }).password).toBeTruthy();
  });

  it(`accepts exactly ${SIGNUP_PASSWORD_MIN_LENGTH} characters — the boundary`, () => {
    expect(validateSignup({ ...VALID, password: "a".repeat(SIGNUP_PASSWORD_MIN_LENGTH) }).password).toBeUndefined();
  });

  it("has no composition rule — an all-lowercase, all-numeric password is accepted", () => {
    // No composition rules.
    expect(validateSignup({ ...VALID, password: "12345678" }).password).toBeUndefined();
  });

  it("is never trimmed — leading/trailing whitespace counts toward length and is preserved", () => {
    const padded = ` ${"a".repeat(SIGNUP_PASSWORD_MIN_LENGTH - 2)} `;
    expect(padded.length).toBe(SIGNUP_PASSWORD_MIN_LENGTH);
    expect(validateSignup({ ...VALID, password: padded }).password).toBeUndefined();
  });

  // Password rules: at most 72 bytes in UTF-8. Literals on the expected side, so a drift of
  // the exported constant cannot move the assertion with it.
  it("accepts exactly 72 bytes and refuses 73", () => {
    expect(validateSignup({ ...VALID, password: "a".repeat(72) }).password).toBeUndefined();
    expect(validateSignup({ ...VALID, password: "a".repeat(73) }).password).toBeTruthy();
  });

  it("counts bytes, not characters — 24 Korean syllables fit (72 bytes), 25 do not (75 bytes)", () => {
    expect(validateSignup({ ...VALID, password: "가".repeat(24) }).password).toBeUndefined();
    expect(validateSignup({ ...VALID, password: "가".repeat(25) }).password).toBeTruthy();
  });

  it("states the limit in both characters and bytes, in Korean", () => {
    const message = validateSignup({ ...VALID, password: "가".repeat(25) }).password;
    expect(message).toBe(
      "비밀번호는 UTF-8 기준 72바이트 이하여야 합니다 (영문·숫자는 72자까지, 한글은 한 글자가 3바이트이므로 24자까지).",
    );
  });
});

describe("validateSignup — multiple invalid fields all get their own error", () => {
  it("reports every invalid field, not just the first", () => {
    const errors = validateSignup({ email: "bad", name: "", department: "", password: "short" });

    expect(Object.keys(errors).sort()).toEqual(["department", "email", "name", "password"]);
    expect(isSignupValid(errors)).toBe(false);
  });
});

describe("signupSubmitFailureMessage", () => {
  // Literals on the expected side: a one-sided edit of the copy fails here.
  const NETWORK = "서버에 연결하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
  const REFUSED = "가입 요청에 실패했습니다. 입력값을 확인한 뒤 다시 시도해 주세요.";
  const PLAIN = "가입 요청에 실패했습니다.";

  it("tells a dead server apart from a refusal, in Korean — it used to render the browser's English as the only text", () => {
    expect(signupSubmitFailureMessage(new ApiError(0, "x", undefined, "network"))).toBe(NETWORK);
    expect(signupSubmitFailureMessage(new ApiError(200, "x", undefined, "parse"))).toBe(
      "서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    );
  });

  it("gives a refusal its fixed sentence and never the backend's detail", () => {
    const message = signupSubmitFailureMessage(new ApiError(422, "email: value is not a valid email address"));

    expect(message).toBe(REFUSED);
    expect(message).not.toContain("valid email");
  });

  it("never renders a raw Error's own message, whatever the library said", () => {
    for (const err of [new TypeError("Failed to fetch"), new Error("anything"), "a string", null]) {
      const message = signupSubmitFailureMessage(err);
      expect(message).toBe(PLAIN);
      expect(message).not.toContain("Failed to fetch");
    }
  });
});
