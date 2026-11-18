/**
 * The usage panel's decision logic (ADR 0021: decision logic, not
 * rendering — and nothing can be mounted, so this is the only layer a test
 * reaches).
 *
 * Three of these are criteria that a *correct-looking* screen can silently
 * violate, which is why they are pinned as truth tables rather than eyeballed:
 * an unmeasured spend rendered as `$0.00`, an override rendered
 * indistinguishably from the inherited default, and a `0` override rendered as an
 * empty field or as "unlimited".
 */
import { describe, expect, it } from "vitest";

import {
  LimitSource,
  QUOTA_INPUT_ERRORS,
  QUOTA_WORDING,
  UsageSubjectKind,
  formatMonthWindow,
  formatUsd,
  isUnattributedLine,
  limitDisplay,
  parseQuotaInput,
  quotaApplyLabel,
  quotaApplyText,
  quotaInputLabel,
  quotaInputValue,
  recentMonths,
  spendDisplay,
} from "./usageDisplay";

describe("the cross-service constants", () => {
  it("match the strings backend services/usage.py sends", () => {
    // Literals, like `roles.test.ts` pins `Role`. These are the wire values on
    // `SubjectUsageOut.limit_source` / `.kind`; a frontend-only rename would
    // relabel every override as an inherited default — the exact distinction
    // the quota-override display exists to preserve, and
    // invisible to `tsc` because both sides are strings.
    expect(LimitSource).toEqual({ OVERRIDE: "override", DEFAULT: "default" });
    expect(UsageSubjectKind).toEqual({ PROJECT: "project", USER: "user" });
  });
});

describe("formatUsd", () => {
  it("always shows cents, including for zero", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(12.5)).toBe("$12.50");
    expect(formatUsd(100)).toBe("$100.00");
    expect(formatUsd(0.004)).toBe("$0.00");
  });
});

describe("formatMonthWindow", () => {
  it("states both the month and the timezone the figure was computed for", () => {
    // The window carries both
    // precisely so a number cannot be mistaken for a different one; dropping
    // either half from the display gives that back.
    expect(
      formatMonthWindow({
        month: "2026-03",
        timezone: "Asia/Seoul",
        starts_at: "2026-02-28T15:00:00Z",
        ends_at: "2026-03-31T15:00:00Z",
      }),
    ).toBe("2026-03 · Asia/Seoul 기준");
  });
});

describe("spendDisplay — unmeasured is never a measured zero", () => {
  const table: Array<{
    builds: number;
    unmeasured: number;
    expected: string;
    why: string;
  }> = [
    { builds: 0, unmeasured: 0, expected: "no-builds", why: "nothing happened this month" },
    { builds: 3, unmeasured: 0, expected: "measured", why: "every build has a cost" },
    { builds: 3, unmeasured: 1, expected: "partially-unmeasured", why: "part of the window" },
    { builds: 3, unmeasured: 3, expected: "unmeasured", why: "none of it was measured" },
    { builds: 1, unmeasured: 1, expected: "unmeasured", why: "the single build was not measured" },
    { builds: 0, unmeasured: 2, expected: "unmeasured", why: "inconsistent input stays honest" },
  ];

  for (const { builds, unmeasured, expected, why } of table) {
    it(`${builds} builds / ${unmeasured} unmeasured -> ${expected} (${why})`, () => {
      expect(spendDisplay({ build_count: builds, unmeasured_build_count: unmeasured }).kind).toBe(
        expected,
      );
    });
  }

  it("distinguishes a measured zero from an unmeasured zero", () => {
    // `cost_usd` is nullable and sums treat NULL as
    // zero, so **both** of these subjects report `spend_usd: 0.0`. If the two
    // classify the same way, the screen prints `$0.00` for money that was never
    // measured and there is no symptom to notice.
    const measuredZero = spendDisplay({ build_count: 2, unmeasured_build_count: 0 });
    const unmeasuredZero = spendDisplay({ build_count: 2, unmeasured_build_count: 2 });
    expect(measuredZero.kind).toBe("measured");
    expect(unmeasuredZero.kind).toBe("unmeasured");
    expect(measuredZero.kind).not.toBe(unmeasuredZero.kind);
  });

  it("carries the unmeasured count so the display can say how much is missing", () => {
    const partial = spendDisplay({ build_count: 5, unmeasured_build_count: 2 });
    expect(partial).toEqual({ kind: "partially-unmeasured", unmeasuredCount: 2 });
    expect(spendDisplay({ build_count: 5, unmeasured_build_count: 5 })).toEqual({
      kind: "unmeasured",
      unmeasuredCount: 5,
    });
  });

  it("gives the fully-unmeasured case no amount to print", () => {
    // The shape is the guard: there is no `amount` field on this variant, so a
    // caller cannot render one for spend that was never measured.
    expect(spendDisplay({ build_count: 1, unmeasured_build_count: 1 })).not.toHaveProperty(
      "amount",
    );
  });
});

describe("limitDisplay — override vs inherited default", () => {
  it("labels an override and a default differently", () => {
    expect(limitDisplay(200, "override")).toEqual({ kind: "override", amount: "$200.00" });
    expect(limitDisplay(100, "default")).toEqual({ kind: "default", amount: "$100.00" });
  });

  it("shows a 0 override as $0.00 and as an override — not as none and not as unlimited", () => {
    // `0.0` is a legal limit meaning "any spend is
    // a breach". The failure mode is a falsy check somewhere turning it into
    // "no limit set".
    const zero = limitDisplay(0, "override");
    expect(zero).toEqual({ kind: "override", amount: "$0.00" });
    expect(zero.kind).not.toBe("none");
  });

  it("shows no limit for the unattributed line, which has no subject to hold one", () => {
    expect(limitDisplay(null, null)).toEqual({ kind: "none" });
  });

  it("refuses to guess an unrecognised limit source", () => {
    // Saying *which* of the two it is is the whole job, so an unknown source
    // says "unlabelled" instead of quietly claiming the default.
    expect(limitDisplay(50, "inherited")).toEqual({ kind: "unlabelled", amount: "$50.00" });
    expect(limitDisplay(50, null)).toEqual({ kind: "unlabelled", amount: "$50.00" });
    expect(limitDisplay(50, "")).toEqual({ kind: "unlabelled", amount: "$50.00" });
  });
});

describe("isUnattributedLine", () => {
  it("is the line whose subject_id is null, tested for null and not for falsiness", () => {
    // An empty-string id is a *real* subject as far as
    // this predicate is concerned; a falsy check here would relabel it as
    // unattributed and hide a real subject's spend under the unattributed line.
    expect(isUnattributedLine({ subject_id: null })).toBe(true);
    expect(isUnattributedLine({ subject_id: "user-1" })).toBe(false);
    expect(isUnattributedLine({ subject_id: "" })).toBe(false);
  });
});

describe("QUOTA_WORDING — quotas warn, so no copy may claim otherwise", () => {
  // nothing in
  // the build path consults accumulated spend, so there is no blocking to
  // describe. Asserted over the whole record rather than over hand-picked
  // strings, so copy added later is covered without anyone remembering to.
  const FORBIDDEN = ["차단", "중지", "정지", "거부", "취소", "blocked", "stopped", "denied"];

  it("contains no wording that implies enforcement", () => {
    for (const [key, text] of Object.entries(QUOTA_WORDING)) {
      for (const word of FORBIDDEN) {
        expect(`${key}: ${text}`.includes(word)).toBe(false);
      }
    }
  });

  it("has no zero-override note: a 0 override is told apart by its amount and its source label alone", () => {
    expect(Object.keys(QUOTA_WORDING)).not.toContain("zeroOverrideNote");
  });

  it("has no breach explanation: the marker's own label says warning, the rule lives in the specs", () => {
    expect(Object.keys(QUOTA_WORDING)).not.toContain("breachExplanation");
    expect(QUOTA_WORDING.breach).toContain("경고");
  });

  it("has non-empty copy for every key", () => {
    for (const [key, text] of Object.entries(QUOTA_WORDING)) {
      expect(text.length, key).toBeGreaterThan(0);
    }
    // These must exist at all: a breach has to be marked as a warning.
    expect(QUOTA_WORDING.breach.length).toBeGreaterThan(0);
    expect(QUOTA_WORDING.unattributed.length).toBeGreaterThan(0);
    expect(QUOTA_WORDING.unmeasured.length).toBeGreaterThan(0);
  });
});

describe("parseQuotaInput", () => {
  it("treats an empty field as clearing the override", () => {
    expect(parseQuotaInput("")).toEqual({ kind: "clear" });
    expect(parseQuotaInput("   ")).toEqual({ kind: "clear" });
  });

  it("accepts zero as a real limit, not as a clear", () => {
    expect(parseQuotaInput("0")).toEqual({ kind: "set", value: 0 });
    expect(parseQuotaInput("0.0")).toEqual({ kind: "set", value: 0 });
    expect(parseQuotaInput("0")).not.toEqual({ kind: "clear" });
  });

  it("accepts ordinary amounts, trimmed", () => {
    expect(parseQuotaInput("12.5")).toEqual({ kind: "set", value: 12.5 });
    expect(parseQuotaInput(" 250 ")).toEqual({ kind: "set", value: 250 });
    expect(parseQuotaInput("1e2")).toEqual({ kind: "set", value: 100 });
  });

  it("normalises -0 to 0 rather than sending a negative zero", () => {
    const parsed = parseQuotaInput("-0");
    expect(parsed.kind).toBe("set");
    expect(Object.is(parsed.kind === "set" ? parsed.value : NaN, 0)).toBe(true);
  });

  it("refuses a negative value client-side, with a reason", () => {
    // The backend refuses it too
    // (`services/usage.set_monthly_quota`); this is so the operator sees why
    // without a round trip, not so the backend can stop checking.
    for (const raw of ["-1", "-0.01", "-250"]) {
      const parsed = parseQuotaInput(raw);
      expect(parsed.kind).toBe("invalid");
      expect(parsed.kind === "invalid" && parsed.reason.length > 0).toBe(true);
    }
  });

  it("refuses non-numeric text with a different reason than a negative value", () => {
    for (const raw of ["abc", "12abc", "$5", "--1"]) {
      expect(parseQuotaInput(raw).kind).toBe("invalid");
    }
    // Two distinct problems get two distinct explanations — a single generic
    // "invalid input" would satisfy the type and tell the operator nothing.
    expect(QUOTA_INPUT_ERRORS.negative).not.toBe(QUOTA_INPUT_ERRORS.notANumber);
  });
});

describe("quotaInputValue", () => {
  it("prefills an existing override, and only an absent one as empty", () => {
    // The pair that must not be confusable: an empty field means "no override",
    // so an existing override of 0 has to show as "0".
    expect(quotaInputValue(null)).toBe("");
    expect(quotaInputValue(0)).toBe("0");
    expect(quotaInputValue(12.5)).toBe("12.5");
  });
});

describe("recentMonths", () => {
  it("lists YYYY-MM newest first and rolls the year over", () => {
    expect(recentMonths(new Date("2026-01-15T00:00:00Z"), 3)).toEqual([
      "2026-01",
      "2025-12",
      "2025-11",
    ]);
  });

  it("zero-pads single-digit months", () => {
    expect(recentMonths(new Date("2026-09-01T00:00:00Z"), 2)).toEqual(["2026-09", "2026-08"]);
  });

  it("includes the current month first", () => {
    expect(recentMonths(new Date("2026-07-31T23:59:59Z"), 1)).toEqual(["2026-07"]);
  });
});

describe("per-row control names", () => {
  // Literals on the expected side. The names are what a screen reader reads and what a voice command
  // says: the subject first, then what the control is.
  it("names a quota input for its subject, unit included", () => {
    expect(quotaInputLabel("알파")).toBe("알파 월 한도(USD)");
  });

  it("names the apply button for its subject, ending in its visible word", () => {
    expect(quotaApplyLabel("알파", false)).toBe("알파 적용");
    expect(quotaApplyText(false)).toBe("적용");
  });

  it("follows the button's text while a quota is being saved, so the name never hides what is shown", () => {
    expect(quotaApplyText(true)).toBe("저장 중...");
    expect(quotaApplyLabel("알파", true)).toBe("알파 저장 중...");
  });

  it("gives two subjects two different names", () => {
    expect(quotaInputLabel("알파")).not.toBe(quotaInputLabel("베타"));
    expect(quotaApplyLabel("알파", false)).not.toBe(quotaApplyLabel("베타", false));
  });
});
