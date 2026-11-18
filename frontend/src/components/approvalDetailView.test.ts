/**
 * What the approval detail's header chips, tab labels and 요약 headlines say. Every expected
 * string is a literal, so a reworded chip or a miscounted tab goes red here and not only on screen.
 * The request below is the demo data's 사내 설문조사 도구: 640 changed files of which 500 are stored,
 * two protected paths restored, 5 smoke routes of which 2 failed, 2 of 6 checks flagged, an agent
 * summary present.
 */
import { describe, expect, it } from "vitest";

import type { PolicyCheckResult, PolicyVerdict, SmokeChecks } from "../types/api";
import {
  DEFAULT_DETAIL_TAB,
  agentSummaryNotice,
  approvalsLine,
  approvalsSoFar,
  attentionCount,
  changedTabCount,
  detailTabs,
  flaggedCheckNames,
  historyTabCount,
  isFlaggedCheck,
  policyHeadline,
  summaryRows,
  tabLabel,
  verdictChips,
  type DetailSource,
} from "./approvalDetailView";
import { changedCountsLine, changedTotal } from "./measuredFactsLines";

function check(name: string, result: "pass" | "flag"): PolicyCheckResult {
  return { id: `c-${name}`, check_name: name, result, summary: "", findings: null };
}

const SIX_CHECKS: PolicyCheckResult[] = [
  check("secrets_pii", "flag"),
  check("dependency_vulnerabilities", "pass"),
  check("egress_detection", "flag"),
  check("resource_limits", "pass"),
  check("template_compliance", "pass"),
  check("destructive_ddl", "pass"),
];

function verdict(checks: PolicyCheckResult[]): PolicyVerdict {
  return { id: "v1", build_id: "b1", risk_tier: "high", created_at: "2026-11-24T00:00:00Z", checks };
}

const SMOKE_FIVE_TWO_FAILED: SmokeChecks = {
  routes: [
    { path: "/", status: 200, outcome: "ok" },
    { path: "/healthz", status: 200, outcome: "ok" },
    { path: "/responses", status: 500, outcome: "server_error" },
    { path: "/slow", status: null, outcome: "no_answer" },
    { path: "/export", status: 200, outcome: "too_large" },
  ],
  skipped: 1,
  truncated: false,
};

/** The 640-file demo request: only 3 of its paths are stored here, the totals are the truth. */
function demo(overrides: Partial<DetailSource> = {}): DetailSource {
  return {
    changed_files: {
      entries: [
        { path: "a.py", change: "added" },
        { path: "b.py", change: "added" },
        { path: "c.py", change: "modified" },
      ],
      totals: { added: 600, modified: 38, deleted: 2 },
      truncated: true,
    },
    protected_path_drift: ["Dockerfile", "compose.yaml"],
    smoke_checks: SMOKE_FIVE_TWO_FAILED,
    preview_start_failure: null,
    policy_verdict: verdict(SIX_CHECKS),
    agent_summary: "설문 도구를 만들었습니다.",
    decisions: [],
    ...overrides,
  };
}

const values = (source: DetailSource) => verdictChips(source).map((chip) => `${chip.label}|${chip.value}|${chip.tone}`);

describe("verdictChips: the four chips of the detail header", () => {
  it("reads the 640-file demo request in four literal chips, in a set order", () => {
    expect(verdictChips(demo()).map((chip) => chip.id)).toEqual(["changes", "preview", "policy", "agent"]);
    expect(values(demo())).toEqual([
      "변경|640개 · 원복 2|warning",
      "프리뷰|2/5 실패|danger",
      "정책|지적 2|danger",
      "에이전트|있음 · 미확인|neutral",
    ]);
  });

  it("says just the count when no protected path was restored, whether drift is empty or absent", () => {
    expect(values(demo({ protected_path_drift: [] }))[0]).toBe("변경|640개|neutral");
    expect(values(demo({ protected_path_drift: null }))[0]).toBe("변경|640개|neutral");
  });

  it("says 측정 안 됨 for changed files that were not measured, never 0개", () => {
    expect(values(demo({ changed_files: null }))[0]).toBe("변경|측정 안 됨|warning");
  });

  it("counts a measured build of zero changed files as 0개, which is not 'not measured'", () => {
    const none = { entries: [], totals: { added: 0, modified: 0, deleted: 0 }, truncated: false };
    expect(values(demo({ changed_files: none, protected_path_drift: [] }))[0]).toBe("변경|0개|neutral");
  });

  it("tells a smoke check that was not measured from one that measured nothing and from a clean one", () => {
    expect(values(demo({ smoke_checks: null }))[1]).toBe("프리뷰|측정 안 됨|warning");
    expect(values(demo({ smoke_checks: { routes: [], skipped: 2, truncated: false } }))[1]).toBe(
      "프리뷰|점검할 경로 없음|neutral",
    );
    expect(
      values(demo({ smoke_checks: { routes: [{ path: "/", status: 200, outcome: "ok" }], skipped: 0, truncated: false } }))[1],
    ).toBe("프리뷰|실패 없음|neutral");
  });

  it("counts a 5xx under a too_large outcome as a failed route", () => {
    const smoke: SmokeChecks = {
      routes: [
        { path: "/", status: 200, outcome: "ok" },
        { path: "/big", status: 500, outcome: "too_large" },
      ],
      skipped: 0,
      truncated: false,
    };
    expect(values(demo({ smoke_checks: smoke }))[1]).toBe("프리뷰|1/2 실패|danger");
  });

  it("says the preview did not start, whatever the smoke check holds, and not that it was not measured", () => {
    expect(values(demo({ preview_start_failure: "UNEXPECTED", smoke_checks: null }))[1]).toBe("프리뷰|시작 실패|danger");
    expect(values(demo({ preview_start_failure: "IMAGE_MISSING" }))[1]).toBe("프리뷰|시작 실패|danger");
    expect(values(demo({ preview_start_failure: "" }))[1]).toBe("프리뷰|2/5 실패|danger");
  });

  it("says 통과 only for a verdict that has checks and none flagged", () => {
    const clean = verdict(SIX_CHECKS.map((c) => check(c.check_name, "pass")));
    expect(values(demo({ policy_verdict: clean }))[2]).toBe("정책|통과|neutral");
  });

  it("counts the flagged checks, and a single one reads 지적 1", () => {
    const one = verdict([check("secrets_pii", "flag"), check("resource_limits", "pass")]);
    expect(values(demo({ policy_verdict: one }))[2]).toBe("정책|지적 1|danger");
  });

  it("reads a gate_error row as an execution error, not as a count of findings", () => {
    const failed = verdict([check("gate_error", "flag")]);
    expect(values(demo({ policy_verdict: failed }))[2]).toBe("정책|검사 실행 오류|danger");
    // Whatever else is in the verdict, any gate_error row wins.
    const mixed = verdict([check("secrets_pii", "flag"), check("gate_error", "flag")]);
    expect(values(demo({ policy_verdict: mixed }))[2]).toBe("정책|검사 실행 오류|danger");
  });

  it("never reads no verdict, or a verdict with no check, as a pass", () => {
    expect(values(demo({ policy_verdict: null }))[2]).toBe("정책|결과 없음|warning");
    expect(values(demo({ policy_verdict: verdict([]) }))[2]).toBe("정책|결과 없음|warning");
  });

  it("says 없음 when there is no agent summary, for null, empty and whitespace alike", () => {
    expect(values(demo({ agent_summary: null }))[3]).toBe("에이전트|없음|neutral");
    expect(values(demo({ agent_summary: "" }))[3]).toBe("에이전트|없음|neutral");
    expect(values(demo({ agent_summary: "  \n " }))[3]).toBe("에이전트|없음|neutral");
  });

  it("carries only words: no icon glyph or mark character in any label or value", () => {
    const all = [
      demo(),
      demo({ changed_files: null, smoke_checks: null, policy_verdict: null, agent_summary: null }),
      demo({ preview_start_failure: "UNEXPECTED", policy_verdict: verdict([check("gate_error", "flag")]) }),
    ].flatMap((source) => verdictChips(source));
    for (const chip of all) {
      for (const text of [chip.label, chip.value]) {
        expect(text, text).not.toMatch(/[!?✕✓✔×✗⚠▲●○]/);
        expect(text.trim().length, text).toBeGreaterThan(0);
      }
    }
  });
});

describe("isFlaggedCheck and flaggedCheckNames", () => {
  it("flags exactly the wire value flag", () => {
    expect(isFlaggedCheck({ result: "flag" })).toBe(true);
    expect(isFlaggedCheck({ result: "pass" })).toBe(false);
    expect(isFlaggedCheck({ result: "FLAG" })).toBe(false);
  });

  it("lists the flagged names in the verdict's order, and none for no verdict", () => {
    expect(flaggedCheckNames(verdict(SIX_CHECKS))).toEqual(["secrets_pii", "egress_detection"]);
    expect(flaggedCheckNames(null)).toEqual([]);
    expect(flaggedCheckNames(undefined)).toEqual([]);
  });
});

describe("the tab counts", () => {
  it("counts the changed files from the true totals, not the stored entries", () => {
    // 3 entries are stored; the build changed 640.
    expect(demo().changed_files?.entries).toHaveLength(3);
    expect(changedTabCount(demo())).toBe(640);
  });

  it("agrees with the counts line, which prints the same sum", () => {
    const changed = demo().changed_files;
    expect(changedCountsLine(changed)).toBe("변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨");
    expect(changedTotal(changed)).toBe(640);
  });

  it("has no count for changed files that were not measured, and 0 for a measured empty build", () => {
    expect(changedTabCount(demo({ changed_files: null }))).toBeNull();
    expect(changedTabCount(demo({ changed_files: { entries: [], totals: {}, truncated: false } }))).toBe(0);
  });

  it("counts attention items: flagged checks plus failing routes, and one for a preview that did not start", () => {
    // 2 flagged + 2 failed routes (the 500 and the no answer; the too_large 200 is not a failure).
    expect(attentionCount(demo())).toBe(4);
    expect(attentionCount(demo({ preview_start_failure: "UNEXPECTED" }))).toBe(5);
    expect(attentionCount(demo({ policy_verdict: null }))).toBe(2);
    expect(attentionCount(demo({ smoke_checks: null }))).toBe(2);
  });

  it("counts nothing for facts that were not measured, and 0 for a clean build", () => {
    expect(attentionCount(demo({ smoke_checks: null, policy_verdict: null }))).toBe(0);
    const clean = verdict(SIX_CHECKS.map((c) => check(c.check_name, "pass")));
    const smoke: SmokeChecks = { routes: [{ path: "/", status: 200, outcome: "ok" }], skipped: 0, truncated: false };
    expect(attentionCount(demo({ policy_verdict: clean, smoke_checks: smoke }))).toBe(0);
  });

  it("counts a gate_error row as one flagged item", () => {
    expect(attentionCount(demo({ smoke_checks: null, policy_verdict: verdict([check("gate_error", "flag")]) }))).toBe(1);
  });

  it("counts the decisions", () => {
    expect(historyTabCount(demo())).toBe(0);
    const two = [
      { id: "d1", actor_id: "u1", decision: "approve", comment: null, created_at: "2026-11-24T00:00:00Z" },
      { id: "d2", actor_id: "u2", decision: "reject", comment: "no", created_at: "2026-11-24T00:00:00Z" },
    ];
    expect(historyTabCount(demo({ decisions: two }))).toBe(2);
  });

  it("labels a tab with its count in parentheses, and leaves the label alone without one", () => {
    expect(tabLabel("변경", 640)).toBe("변경 (640)");
    expect(tabLabel("이력", 0)).toBe("이력 (0)");
    expect(tabLabel("변경", null)).toBe("변경");
  });

  it("offers four tabs in a set order, 요약 first and without a count, and opens on 요약", () => {
    expect(detailTabs(demo())).toEqual([
      { id: "summary", label: "요약" },
      { id: "changes", label: "변경 (640)" },
      { id: "checks", label: "점검 (4)" },
      { id: "history", label: "이력 (0)" },
    ]);
    expect(DEFAULT_DETAIL_TAB).toBe("summary");
  });
});

describe("the 요약 rows", () => {
  it("reads each measured fact of the demo request once, the drift as its own row, with no pointer to a tab", () => {
    const rows = summaryRows(demo());
    expect(rows).toEqual([
      { id: "changed", label: null, text: "변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨" },
      { id: "drift", label: null, text: null },
      { id: "smoke", label: "프리뷰 응답 점검", text: "호출한 GET 경로 5개 중 2개 실패" },
      { id: "policy", label: "정책 검사", text: "검사 6개 중 지적 2 — 비밀·개인정보, 외부 접속" },
    ]);
    // Each row reads once: the changed row has no label of its own (its text starts with the subject),
    // and no row's text repeats its own label.
    const lines = rows.map((row) => [row.label, row.text].filter((part) => part !== null).join(" "));
    expect(lines[0]).toBe("변경 파일 640개 (추가 600 · 수정 38 · 삭제 2) · 목록은 일부만 저장됨");
    expect(lines.join(" | ").split("변경 파일 640개").length - 1).toBe(1);
    for (const row of rows) {
      if (row.label !== null) expect(row.text ?? "").not.toContain(row.label);
      expect(`${row.label ?? ""}${row.text ?? ""}`).not.toMatch(/›|탭으로|바로가기/);
    }
  });

  it("leaves the drift row out when no path was restored", () => {
    expect(summaryRows(demo({ protected_path_drift: [] })).map((row) => row.id)).toEqual(["changed", "smoke", "policy"]);
    expect(summaryRows(demo({ protected_path_drift: null })).map((row) => row.id)).toEqual(["changed", "smoke", "policy"]);
  });

  it("says not measured in words for changed files and the smoke check, never as a pass", () => {
    const rows = summaryRows(demo({ changed_files: null, smoke_checks: null }));
    expect(rows[0].text).toBe("이 빌드는 변경 파일이 측정되지 않았습니다.");
    expect(rows.find((row) => row.id === "smoke")?.text).toBe(
      "측정되지 않음: 점검 결과가 없습니다. 이는 확인되었다는 뜻이 아닙니다.",
    );
  });

  it("adds the preview start failure row, by its Korean label, only when the preview failed", () => {
    const failed = summaryRows(demo({ preview_start_failure: "SIGNING_SECRET_MISSING" }));
    expect(failed.map((row) => row.id)).toEqual(["changed", "drift", "previewStart", "smoke", "policy"]);
    expect(failed.find((row) => row.id === "previewStart")?.text).toBe("프리뷰를 시작하지 못함 (프리뷰 서명 설정 누락)");
  });

  it("words the policy line for no flag, for no verdict and for a gate error", () => {
    expect(policyHeadline(verdict(SIX_CHECKS.map((c) => check(c.check_name, "pass"))))).toBe("검사 6개 중 지적 없음");
    expect(policyHeadline(null)).toBe("정책 검사 결과가 없습니다. 이는 통과했다는 뜻이 아닙니다.");
    expect(policyHeadline(verdict([]))).toBe("정책 검사 결과가 없습니다. 이는 통과했다는 뜻이 아닙니다.");
    expect(policyHeadline(verdict([check("gate_error", "flag")]))).toBe("검사 1개 중 지적 1 — 검사 실행 오류");
    // A name the client does not know is shown as sent.
    expect(policyHeadline(verdict([check("sast", "flag")]))).toBe("검사 1개 중 지적 1 — sast");
  });

  it("has an agent notice only when a summary exists, and never quotes it", () => {
    expect(agentSummaryNotice(null)).toBeNull();
    expect(agentSummaryNotice("   ")).toBeNull();
    const notice = agentSummaryNotice("비밀 문장");
    expect(notice).not.toBeNull();
    expect(notice).not.toContain("비밀 문장");
  });
});

describe("the action bar's approvals line", () => {
  it("reads 필요 승인 수 N · 현재 M from the approve decisions only", () => {
    expect(approvalsLine(1, [])).toBe("필요 승인 수 1 · 현재 0");
    const decisions = [{ decision: "approve" }, { decision: "reject" }, { decision: "approve" }];
    expect(approvalsSoFar(decisions)).toBe(2);
    expect(approvalsLine(2, decisions)).toBe("필요 승인 수 2 · 현재 2");
  });
});
