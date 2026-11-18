/**
 * The cost dashboard's display logic (ADR 0021: decision logic, not rendering).
 * Expected values are literals, not re-derivations of the production formulas.
 */
import { describe, expect, it } from "vitest";

import type { DailyBucket, ExpensiveBuild, MeteringCaveat } from "../../types/api";
import {
  CHART,
  chartGeometry,
  chartSummary,
  dailyRows,
  expensiveRows,
  failureReasonText,
  formatEndedAt,
  hasBuilds,
  meteringCaveatNote,
  niceCeiling,
  outcomeRows,
  outcomeStatusLabel,
  shortDayLabel,
  spendText,
} from "./costDashboard";

function day(overrides: Partial<DailyBucket> & { day: string }): DailyBucket {
  return {
    spend_usd: 0,
    build_count: 0,
    unmeasured_build_count: 0,
    failed_spend_usd: 0,
    ...overrides,
  };
}

describe("spendText: unmeasured is never $0.00", () => {
  it("a day with no builds says so, not $0.00", () => {
    expect(spendText({ spend_usd: 0, build_count: 0, unmeasured_build_count: 0 })).toBe("빌드 없음");
  });

  it("a day whose every build is unmeasured carries no amount", () => {
    const text = spendText({ spend_usd: 0, build_count: 2, unmeasured_build_count: 2 });
    expect(text).toBe("미측정 · 빌드 2건");
    expect(text).not.toContain("$");
  });

  it("a partly unmeasured day shows the measured amount and the exclusion", () => {
    expect(spendText({ spend_usd: 1.5, build_count: 3, unmeasured_build_count: 1 })).toBe(
      "$1.50 (미측정 1건 제외)",
    );
  });

  it("a measured zero is a measured $0.00", () => {
    expect(spendText({ spend_usd: 0, build_count: 1, unmeasured_build_count: 0 })).toBe("$0.00");
  });
});

describe("day labels", () => {
  it("are plain string work, never a Date", () => {
    expect(shortDayLabel("2026-07-05")).toBe("7/5");
    expect(shortDayLabel("2026-12-31")).toBe("12/31");
  });

  it("dailyRows gives the failed part only when there is one", () => {
    const rows = dailyRows([
      day({ day: "2026-07-01", spend_usd: 8, build_count: 2, failed_spend_usd: 5 }),
      day({ day: "2026-07-02" }),
    ]);
    expect(rows[0]).toEqual({
      day: "2026-07-01",
      shortLabel: "7/1",
      spend: "$8.00",
      failedSpend: "$5.00",
      builds: 2,
      unmeasured: 0,
    });
    expect(rows[1].failedSpend).toBeNull();
    expect(rows[1].spend).toBe("빌드 없음");
  });
});

describe("niceCeiling", () => {
  it.each([
    [0, 1],
    [0.3, 0.5],
    [1, 1],
    [1.2, 2],
    [3, 5],
    [5.01, 10],
    [42, 50],
    [101, 200],
  ])("%s -> %s", (value, expected) => {
    expect(niceCeiling(value)).toBe(expected);
  });
});

describe("chartGeometry", () => {
  const plotHeight = CHART.height - CHART.top - CHART.bottom; // 182
  const days = [
    day({ day: "2026-07-01", spend_usd: 10, build_count: 2, failed_spend_usd: 4 }),
    day({ day: "2026-07-02", spend_usd: 5, build_count: 1 }),
    day({ day: "2026-07-03", spend_usd: 0, build_count: 1, unmeasured_build_count: 1 }),
    day({ day: "2026-07-04" }),
  ];

  it("scales to a round ceiling and splits each bar into failed and the rest", () => {
    const g = chartGeometry(days);
    expect(g.maxUsd).toBe(10);
    expect(g.baselineY).toBe(192);

    // Day 1: 10 of 10 fills the plot; failed is 4/10 of it, at the bottom.
    expect(g.bars[0].failedHeight).toBe(72.8);
    expect(g.bars[0].otherHeight).toBe(109.2);
    expect(g.bars[0].failedY).toBe(119.2);
    expect(g.bars[0].otherY).toBe(10);
    expect(g.bars[0].failedHeight + g.bars[0].otherHeight).toBeCloseTo(plotHeight, 1);

    // Day 2: half the scale, nothing failed.
    expect(g.bars[1].failedHeight).toBe(0);
    expect(g.bars[1].otherHeight).toBe(91);
  });

  it("draws no bar for a day with no spend, and flags unmeasured instead", () => {
    const g = chartGeometry(days);
    expect(g.bars[2].failedHeight + g.bars[2].otherHeight).toBe(0);
    expect(g.bars[2].hasUnmeasured).toBe(true);
    expect(g.bars[3].hasUnmeasured).toBe(false);
  });

  it("never draws more failed spend than the day spent", () => {
    const g = chartGeometry([day({ day: "2026-07-01", spend_usd: 2, build_count: 1, failed_spend_usd: 3 })]);
    expect(g.bars[0].otherHeight).toBe(0);
    expect(g.bars[0].failedHeight).toBe(plotHeight); // 2 of ceiling 2
  });

  it("lays bars left to right inside the plot, labelling every 5th day", () => {
    const g = chartGeometry(Array.from({ length: 31 }, (_, n) => day({ day: `2026-07-${String(n + 1).padStart(2, "0")}` })));
    expect(g.bars).toHaveLength(31);
    expect(g.bars[0].x).toBeGreaterThanOrEqual(CHART.left);
    expect(g.bars[30].x + g.bars[30].width).toBeLessThanOrEqual(CHART.width - CHART.right);
    expect(g.bars.filter((b) => b.showLabel).map((b) => b.label)).toEqual([
      "7/1",
      "7/6",
      "7/11",
      "7/16",
      "7/21",
      "7/26",
      "7/31",
    ]);
  });

  it("an all-zero month still has a valid axis", () => {
    const g = chartGeometry([day({ day: "2026-07-01" })]);
    expect(g.maxUsd).toBe(1);
    expect(g.ticks.map((t) => t.label)).toEqual(["$0.00", "$0.50", "$1.00"]);
  });

  it("an empty list does not throw", () => {
    expect(chartGeometry([]).bars).toEqual([]);
  });
});

describe("chartSummary", () => {
  it("states the totals, the failed part and the peak, and points at the table", () => {
    const text = chartSummary([
      day({ day: "2026-07-01", spend_usd: 10, build_count: 2, failed_spend_usd: 4 }),
      day({ day: "2026-07-02", spend_usd: 5, build_count: 1 }),
    ]);
    expect(text).toContain("합계 $15.00");
    expect(text).toContain("실패한 빌드 $4.00");
    expect(text).toContain("7/1 $10.00");
    expect(text).toContain("아래 표");
  });

  it("says there were no builds rather than describing a flat chart", () => {
    expect(chartSummary([day({ day: "2026-07-01" })])).toBe("이 달에 종료된 빌드가 없습니다.");
  });

  it("hasBuilds is false for a month of empty days", () => {
    expect(hasBuilds({ days: [day({ day: "2026-07-01" })] })).toBe(false);
    expect(hasBuilds({ days: [day({ day: "2026-07-01", build_count: 1 })] })).toBe(true);
  });
});

describe("outcomes", () => {
  it("name failure reasons in Korean, never the raw code", () => {
    expect(failureReasonText("BUDGET_EXCEEDED")).toBe("빌드 예산 초과");
    expect(failureReasonText("TIMEOUT")).toBe("제한 시간 초과");
    expect(failureReasonText("MISSING_ENTRYPOINT")).toBe("앱의 시작 파일이 없어 빌드를 완료할 수 없음");
    expect(failureReasonText("SOMETHING_NEW")).toBe("알 수 없는 사유");
    expect(failureReasonText(null)).toBe("사유 기록 없음");
  });

  it("label the terminal statuses", () => {
    expect(outcomeStatusLabel("SUCCEEDED")).toBe("성공");
    expect(outcomeStatusLabel("FAILED")).toBe("실패");
    expect(outcomeStatusLabel("constructor")).toBe("constructor");
  });

  it("outcomeRows nests the reasons under FAILED", () => {
    const rows = outcomeRows([
      { status: "SUCCEEDED", spend_usd: 3, build_count: 2, unmeasured_build_count: 0, failure_reasons: [] },
      {
        status: "FAILED",
        spend_usd: 4.5,
        build_count: 2,
        unmeasured_build_count: 1,
        failure_reasons: [
          { failure_reason: "BUDGET_EXCEEDED", spend_usd: 4.5, build_count: 1, unmeasured_build_count: 0 },
          { failure_reason: "INFRA_ERROR", spend_usd: 0, build_count: 1, unmeasured_build_count: 1 },
        ],
      },
    ]);
    expect(rows.map((r) => [r.label, r.spend, r.builds])).toEqual([
      ["성공", "$3.00", 2],
      ["실패", "$4.50 (미측정 1건 제외)", 2],
    ]);
    expect(rows[1].reasons.map((r) => [r.label, r.spend])).toEqual([
      ["빌드 예산 초과", "$4.50"],
      ["인프라 오류", "미측정 · 빌드 1건"],
    ]);
    expect(rows[0].reasons).toEqual([]);
  });
});

describe("expensiveRows", () => {
  const build = (overrides: Partial<ExpensiveBuild> = {}): ExpensiveBuild => ({
    build_id: "b1",
    project_id: "p1",
    type: "create",
    status: "FAILED",
    failure_reason: "TIMEOUT",
    cost_usd: 9.5,
    cost_tokens: 1234567,
    ended_at: "2026-07-10T16:00:00Z",
    requester_id: null,
    ...overrides,
  });

  it("formats cost with formatUsd, shows the Korean reason and marks a null requester unattributed", () => {
    const [row] = expensiveRows([build()], { p1: "Alpha" }, "Asia/Seoul");
    expect(row.cost).toBe("$9.50");
    expect(row.tokens).toBe("1,234,567");
    expect(row.status).toBe("실패");
    expect(row.reason).toBe("제한 시간 초과");
    expect(row.requester).toBe("미귀속");
    expect(row.projectId).toBe("p1");
    expect(row.projectName).toBe("Alpha");
  });

  it("makes invisible and direction-changing characters in a project name visible", () => {
    const [row] = expensiveRows([build()], { p1: `report${String.fromCodePoint(0x202e)}txt` }, "UTC");
    expect(row.projectName).toBe("report⟨U+202E⟩txt");
  });

  it("falls back to the id for an unknown project and does not read the prototype", () => {
    expect(expensiveRows([build({ project_id: "gone" })], {}, "UTC")[0].projectName).toBe("gone");
    expect(expensiveRows([build({ project_id: "constructor" })], {}, "UTC")[0].projectName).toBe("constructor");
  });

  it("a non-number cost reads as unmeasured, never $0.00", () => {
    const bad = build({ cost_usd: null as unknown as number });
    expect(expensiveRows([bad], {}, "UTC")[0].cost).toBe("미측정");
  });

  it("has no failure reason line for a succeeded build", () => {
    expect(expensiveRows([build({ status: "SUCCEEDED", failure_reason: null })], {}, "UTC")[0].reason).toBeNull();
  });

  it("states the end time in the platform timezone, not the browser's", () => {
    // 16:00 UTC is 01:00 the next day in Seoul.
    expect(formatEndedAt("2026-07-10T16:00:00Z", "Asia/Seoul")).toContain("07. 11.");
    expect(formatEndedAt("2026-07-10T16:00:00Z", "UTC")).toContain("07. 10.");
    expect(formatEndedAt("not a date", "UTC")).toBe("not a date");
  });
});

describe("meteringCaveatNote", () => {
  const caveat = (before: boolean): MeteringCaveat => ({ date: "2026-10-01", window_starts_before: before });

  it("is a note naming the date when the window starts before it", () => {
    const note = meteringCaveatNote(caveat(true));
    expect(note).toContain("2026-10-01");
    expect(note).toContain("에이전트 엔진이 직접 보고");
  });

  it("is absent when the window does not", () => {
    expect(meteringCaveatNote(caveat(false))).toBeNull();
  });
});
