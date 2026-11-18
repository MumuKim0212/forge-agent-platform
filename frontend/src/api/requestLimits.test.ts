/**
 * The frontend half of the requester-text length cap (backend
 * `schemas/change_request.NL_REQUEST_MAX_CHARS` / `schemas/project.
 * PROJECT_NAME_MAX_CHARS`, pinned there by `test_request_length_cap.py`).
 * Decision logic only (ADR 0021): how characters are counted, what a box may
 * hold, and which failure is "too long".
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import {
  NL_REQUEST_MAX_CHARS,
  PROJECT_NAME_MAX_CHARS,
  countChars,
  isAnyOverLimit,
  lengthError,
  lengthHint,
  lengthNotice,
  lengthState,
  tooLongField,
  tooLongMessage,
} from "./requestLimits";

describe("the limits mirror the backend's", () => {
  it("are the documented values (a contract that crosses a boundary gets a literal)", () => {
    expect(NL_REQUEST_MAX_CHARS).toBe(8000);
    expect(PROJECT_NAME_MAX_CHARS).toBe(100);
  });
});

describe("counting agrees with the server (Python len = code points)", () => {
  it("counts an emoji as one character although JS .length says two", () => {
    expect("😀".length).toBe(2); // the trap: what `maxlength` / `.length` would use
    expect(countChars("😀")).toBe(1);
    expect(countChars("😀".repeat(8000))).toBe(8000);
  });

  it("counts Korean syllables and ASCII one each", () => {
    expect(countChars("가나다abc")).toBe(6);
    expect(countChars("")).toBe(0);
  });

  it("counts a lone surrogate as one, as Python does for the same JSON", () => {
    expect(countChars("\ud83d")).toBe(1);
  });
});

describe("the counter the user sees -- the text is kept, never cut", () => {
  it("reads `count / max` and is not over below the limit", () => {
    expect(lengthState("가".repeat(1234), 8000)).toEqual({
      count: 1234,
      max: 8000,
      label: "1234 / 8000",
      over: false,
      excess: 0,
    });
    expect(lengthHint("가".repeat(1234), 8000)).toBe("1234 / 8000");
    expect(lengthError("가".repeat(1234), 8000)).toBeUndefined();
  });

  it("is not over at exactly the limit (the server accepts exactly max)", () => {
    expect(lengthState("가".repeat(8000), 8000).over).toBe(false);
    expect(lengthState("😀".repeat(8000), 8000).over).toBe(false);
    expect(lengthError("가".repeat(8000), 8000)).toBeUndefined();
  });

  it("is over one past the limit and says how many characters to remove", () => {
    const state = lengthState("가".repeat(8001), 8000);

    expect(state.over).toBe(true);
    expect(state.excess).toBe(1);
    expect(lengthError("가".repeat(8001), 8000)).toContain("8001 / 8000");
    expect(lengthError("가".repeat(8001), 8000)).toContain("1자를 줄여 주세요");
  });

  it("reports the real excess of a pasted wall of text, counting emoji once", () => {
    expect(lengthState("가".repeat(9100), 8000).excess).toBe(1100);
    expect(lengthState("😀".repeat(8050), 8000).excess).toBe(50);
    expect(lengthError("x".repeat(130), 100)).toContain("30자를 줄여 주세요");
  });
});

describe("isAnyOverLimit -- what disables submit", () => {
  it("is false when every field fits, including a field exactly at its limit", () => {
    expect(
      isAnyOverLimit([
        { text: "이름", max: 100 },
        { text: "가".repeat(8000), max: 8000 },
      ]),
    ).toBe(false);
    expect(isAnyOverLimit([])).toBe(false);
  });

  it("is true when any one field is over, whichever it is", () => {
    expect(
      isAnyOverLimit([
        { text: "x".repeat(101), max: 100 },
        { text: "ok", max: 8000 },
      ]),
    ).toBe(true);
    expect(
      isAnyOverLimit([
        { text: "ok", max: 100 },
        { text: "가".repeat(8001), max: 8000 },
      ]),
    ).toBe(true);
  });

  it("counts code points, so 8000 emoji (16000 UTF-16 units) do not block", () => {
    expect(isAnyOverLimit([{ text: "😀".repeat(8000), max: 8000 }])).toBe(false);
    expect(isAnyOverLimit([{ text: "😀".repeat(8001), max: 8000 }])).toBe(true);
  });
});

// The entry as the backend sends it (also asserted on the backend side in
// test_request_length_cap.py::_assert_too_long). It carries no `input`: the
// handler in services/validation_errors.py drops the submitted value, and nothing here reads it.
function tooLong422(field: string) {
  return new ApiError(
    422,
    "[object Object]",
    [
      {
        type: "string_too_long",
        loc: ["body", field],
        msg: "String should have at most 8000 characters",
        ctx: { max_length: 8000 },
      },
    ],
  );
}

describe("tooLongField / tooLongMessage -- only an over-long 422 gets its own message", () => {
  it("recognises the request field and the name field", () => {
    expect(tooLongField(tooLong422("nl_request"))).toBe("nl_request");
    expect(tooLongField(tooLong422("name"))).toBe("name");
  });

  it("says in Korean that the request is too long, with the limit", () => {
    const message = tooLongMessage(tooLong422("nl_request"));

    expect(message).toContain("요청 내용이 너무 깁니다");
    expect(message).toContain("8000");
  });

  it("says in Korean that the name is too long, with the limit", () => {
    const message = tooLongMessage(tooLong422("name"));

    expect(message).toContain("프로젝트 이름이 너무 깁니다");
    expect(message).toContain("100");
  });

  it("is null for a different 422, a different status, a string detail or a non-ApiError", () => {
    const missing = new ApiError(422, "x", [{ type: "missing", loc: ["body", "nl_request"] }]);
    const otherField = new ApiError(422, "x", [{ type: "string_too_long", loc: ["body", "team_id"] }]);
    const wrongStatus = new ApiError(400, "x", [{ type: "string_too_long", loc: ["body", "name"] }]);
    const stringDetail = new ApiError(422, "Template not found", "Template not found");

    for (const err of [missing, otherField, wrongStatus, stringDetail, new Error("boom"), null, "x"]) {
      expect(tooLongField(err)).toBeNull();
      expect(tooLongMessage(err)).toBeNull();
    }
  });
});

describe("lengthNotice -- the one polite sentence for crossing a limit", () => {
  it("names the field and says the limit was crossed", () => {
    expect(lengthNotice("프로젝트 이름")).toBe("프로젝트 이름: 글자 수 제한을 넘었습니다.");
  });

  it("carries no count, so it is the same string however far past the limit the user types", () => {
    // `Field`'s polite region re-announces any change of its text; the count lives in `lengthError`.
    expect(lengthNotice("이름")).not.toMatch(/[0-9]/);
    expect(lengthNotice("이름")).toBe(lengthNotice("이름"));
  });
});
