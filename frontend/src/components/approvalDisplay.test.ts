/**
 * How a risk tier and an approval request's status read on screen: one source, read by the
 * approval console, the project timeline and the eval run's policy line. Pure (ADR 0021).
 *
 * The wire values below are literals, as befits a string that crosses a
 * boundary: backend `services/policy_checks.PolicyRiskTier` and `models/approval.ApprovalStatus`.
 * Nothing machine-checks them against the backend; they are the list a status added there has to be
 * added to here, in the same commit, or its badge reads as the raw enum name.
 */
import { describe, expect, it } from "vitest";

import {
  APPROVAL_STATUS_LABELS,
  APPROVAL_STATUS_TONES,
  RISK_TIER_LABELS,
  RISK_TIER_TONES,
  approvalStatusLabel,
  approvalStatusTone,
  decisionLabel,
  riskLegendItems,
  RISK_LEGEND_CLOSING,
  RISK_LEGEND_LEAD,
  riskTierLabel,
  riskTierTone,
} from "./approvalDisplay";

const BADGE_TONES = ["neutral", "success", "danger", "warning", "accent"];

// [wire value, label, tone]: what the two copies this replaces both said.
const RISK_TIERS = [
  ["low", "낮음", "success"],
  ["medium", "중간", "warning"],
  ["high", "높음", "danger"],
] as const;

const APPROVAL_STATUSES = [
  ["PENDING", "승인 대기", "neutral"],
  ["AUTO_APPROVED", "자동 승인", "success"],
  ["APPROVED", "승인됨", "success"],
  ["REJECTED", "반려됨", "danger"],
  ["SUPERSEDED", "새 빌드로 대체됨", "neutral"],
] as const;

describe("every risk tier the API can send has a label and a tone", () => {
  it.each(RISK_TIERS)("%s reads as %s in the %s tone", (tier, label, tone) => {
    expect(riskTierLabel(tier)).toBe(label);
    expect(riskTierTone(tier)).toBe(tone);
  });

  it("covers exactly those tiers, with distinct labels, none of them the raw value", () => {
    const wire = RISK_TIERS.map(([tier]) => tier);
    expect(Object.keys(RISK_TIER_LABELS).sort()).toEqual([...wire].sort());
    expect(Object.keys(RISK_TIER_TONES).sort()).toEqual([...wire].sort());
    const labels = wire.map((tier) => riskTierLabel(tier));
    expect(new Set(labels).size).toBe(labels.length);
    for (const tier of wire) {
      expect(riskTierLabel(tier)).not.toBe(tier);
      expect(BADGE_TONES).toContain(riskTierTone(tier));
    }
  });
});

describe("every approval status the API can send has a label and a tone", () => {
  it.each(APPROVAL_STATUSES)("%s reads as %s in the %s tone", (status, label, tone) => {
    expect(approvalStatusLabel(status)).toBe(label);
    expect(approvalStatusTone(status)).toBe(tone);
  });

  it("covers exactly those statuses, with distinct labels, none of them the raw value", () => {
    const wire = APPROVAL_STATUSES.map(([status]) => status);
    expect(Object.keys(APPROVAL_STATUS_LABELS).sort()).toEqual([...wire].sort());
    expect(Object.keys(APPROVAL_STATUS_TONES).sort()).toEqual([...wire].sort());
    const labels = wire.map((status) => approvalStatusLabel(status));
    expect(new Set(labels).size).toBe(labels.length);
    for (const status of wire) {
      expect(approvalStatusLabel(status)).not.toBe(status);
      expect(BADGE_TONES).toContain(approvalStatusTone(status));
    }
  });
});

describe("a value this build has never heard of", () => {
  it("is shown as it was sent, in the neutral tone, never as a known one", () => {
    expect(riskTierLabel("critical")).toBe("critical");
    expect(riskTierTone("critical")).toBe("neutral");
    expect(approvalStatusLabel("ESCALATED")).toBe("ESCALATED");
    expect(approvalStatusTone("ESCALATED")).toBe("neutral");
  });

  it("is not confused with something inherited from Object.prototype", () => {
    for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(riskTierLabel(name)).toBe(name);
      expect(riskTierTone(name)).toBe("neutral");
      expect(approvalStatusLabel(name)).toBe(name);
      expect(approvalStatusTone(name)).toBe("neutral");
    }
  });
});

describe("there is one source: no other file spells these labels", () => {
  // The two copies this replaced sat in `Timeline.tsx` and `ApprovalConsolePage.tsx` (and a third,
  // labels only, in `evalRuns.ts`). A label quoted anywhere else in non-test source is a copy again.
  // `"승인 대기"` is left out: `StatusBadge` spells it too, for a *project* status of a different name.
  const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;
  const LABELS = ["낮음", "중간", "높음", "자동 승인", "승인됨", "반려됨", "새 빌드로 대체됨"];

  /** The file names the glob found (it keys files beside this one as `./x`, the rest as `../dir/x`). */
  const fileNames = () => Object.keys(SOURCE_FILES).map((file) => file.split("/").pop());

  it("reaches the source tree", () => {
    expect(fileNames()).toContain("approvalDisplay.ts");
    expect(fileNames()).toContain("Timeline.tsx");
    expect(fileNames()).toContain("ApprovalConsolePage.tsx");
  });

  it.each(LABELS)("quotes %s only in approvalDisplay.ts", (label) => {
    const holders = Object.entries(SOURCE_FILES)
      .filter(([file]) => !/\.test\.tsx?$/.test(file))
      .filter(([, source]) => source.includes(`"${label}"`))
      .map(([file]) => file.split("/").pop());
    expect(holders).toEqual(["approvalDisplay.ts"]);
  });
});

describe("decisionLabel: the decision history's words, from one mapping", () => {
  it("names each decision the backend records", () => {
    expect(decisionLabel("approve")).toBe("승인");
    expect(decisionLabel("reject")).toBe("반려");
  });

  it("shows a value this build has never heard of as it was sent, never as one of the known ones", () => {
    expect(decisionLabel("defer")).toBe("defer");
    expect(decisionLabel("constructor")).toBe("constructor");
    expect(decisionLabel("")).toBe("");
  });
});

// The key above the approval list. The descriptions are pinned once as literals; the level
// words are read from RISK_TIER_LABELS, so a relabel there must reach the key. The check names
// come from POLICY_CHECK_LABELS (the high description lost "노출": the table's name is 비밀·개인정보).
// No check identifier appears in it.
describe("the risk legend", () => {
  it("describes each level in Korean, in tier order", () => {
    expect(riskLegendItems()).toEqual([
      { tier: "low", label: "낮음", description: "지적 없음, 자동 승인" },
      {
        tier: "medium",
        label: "중간",
        description: "의존성 취약점, 자원 한도, 템플릿 규칙, DB 구조 파괴 중 하나가 지적됨",
      },
      { tier: "high", label: "높음", description: "비밀·개인정보, 외부 접속 중 하나가 지적됨" },
    ]);
    expect(RISK_LEGEND_LEAD).toBe("위험도 — 정책 검사 결과");
    expect(RISK_LEGEND_CLOSING).toBe("중간·높음은 승인자 1명의 확인이 필요합니다.");
  });

  it("carries no snake_case identifier and no hedging suffix", () => {
    const all = [RISK_LEGEND_LEAD, RISK_LEGEND_CLOSING, ...riskLegendItems().map((i) => i.description)].join(" ");
    expect(all).not.toMatch(/[a-z]+_[a-z]+/);
    expect(all).not.toContain("기본 기준");
  });

  it("names every level the badges can show, with the same label the badge shows", () => {
    expect(riskLegendItems().map((i) => i.label)).toEqual(
      RISK_TIERS.map(([wire]) => riskTierLabel(wire)),
    );
    expect(Object.values(RISK_TIER_LABELS)).toHaveLength(RISK_TIERS.length);
  });

  it("follows a relabel of a tier", () => {
    const original = RISK_TIER_LABELS.medium;
    (RISK_TIER_LABELS as Record<string, string>).medium = "보통";
    try {
      expect(riskLegendItems().map((i) => i.label)).toEqual(["낮음", "보통", "높음"]);
    } finally {
      (RISK_TIER_LABELS as Record<string, string>).medium = original;
    }
  });
});
