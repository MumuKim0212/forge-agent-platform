/**
 * The 평가 tab's decisions (ADR 0021: logic, not rendering).
 *
 * What an admin relies on, as truth tables: the start control is disabled, with a reason, for each
 * thing that makes a start unsafe (and while nothing is loaded yet); the budget and the engine/model
 * named before a start are the ones in force; an unmeasured cost never reads as free; polling is on
 * only while a run is in progress; and every sentence is worded from a code, never from server text.
 */
import { describe, expect, it } from "vitest";

import type { EvalCase, EvalCheckOutcome, EvalRun, EvalStatus } from "../../types/api";
import {
  fingerprintLabel,
  POLL_INTERVAL_MS,
  REASON_LOADING,
  REASON_ENGINE_UNAVAILABLE,
  REASON_EXECUTOR_INACTIVE,
  REASON_REQUEST_SET_UNUSABLE,
  REASON_RUN_IN_PROGRESS,
  UNKNOWN_RUN_STATUS_LABEL,
  UNMEASURED_COST_TEXT,
  budgetHeadline,
  budgetNote,
  caseBuildPath,
  caseCostText,
  caseResultLabel,
  caseRows,
  checkOutcomeLine,
  confirmationView,
  evalStartFailureMessage,
  failureText,
  formatCost,
  formatDuration,
  isRunInProgress,
  policyLine,
  runOutcomeNote,
  runRows,
  runStatusLabel,
  shouldPoll,
  startControlState,
  startSuccessMessage,
} from "./evalRuns";
import { NO_MODEL_TEXT } from "./agentEngine";

function run(over: Partial<EvalRun> = {}): EvalRun {
  return {
    id: "run-1",
    status: "COMPLETED",
    engine: "openai",
    model: "gpt-5-mini",
    request_set_version: 1,
    prompt_fingerprint: "3f9a1c07be52",
    repair_max_attempts: 2,
    budget_usd: 1.0,
    spend_usd: 0.3,
    started_by_id: "u1",
    started_by_name: "관리자",
    started_at: "2026-10-13T01:00:00Z",
    ended_at: "2026-10-13T01:05:30Z",
    duration_seconds: 330,
    detail: "English detail that must never reach the screen",
    total: 3,
    passed: 2,
    failed: 1,
    not_run: 0,
    ...over,
  };
}

function status(over: Partial<EvalStatus> = {}): EvalStatus {
  return {
    budget_usd: 1.0,
    case_min_budget_usd: 0.05,
    executor_active: true,
    engine: "openai",
    model: "gpt-5-mini",
    engine_available: true,
    run_in_progress: false,
    request_set_version: 1,
    case_count: 3,
    ...over,
  };
}

function evalCase(over: Partial<EvalCase> = {}): EvalCase {
  return {
    id: "c1",
    case_id: "static-page",
    position: 0,
    state: "DONE",
    result: "PASSED",
    failure_reason: null,
    failed_check_index: null,
    check_outcomes: [],
    project_id: "p1",
    build_id: "b1",
    policy_risk_tier: null,
    policy_flagged_checks: [],
    cost_usd: 0.3,
    cost_tokens: 12345,
    repair_attempts: 1,
    duration_seconds: 75,
    started_at: null,
    ended_at: null,
    ...over,
  };
}

describe("the start control", () => {
  it("is enabled only with nothing running, an active executor, the selected engine available and a usable set", () => {
    expect(startControlState(status(), [run()])).toEqual({ disabled: false, reason: null });
    expect(startControlState(status(), [])).toEqual({ disabled: false, reason: null });
  });

  it("is disabled, with the reason, while a run is in progress (by the list or by the status)", () => {
    const expected = { disabled: true, reason: REASON_RUN_IN_PROGRESS };
    expect(startControlState(status(), [run({ status: "RUNNING" }), run({ id: "old" })])).toEqual(expected);
    expect(startControlState(status({ run_in_progress: true }), [])).toEqual(expected);
  });

  it("is disabled, with the reason, when the executor is inactive, even with the selected engine available", () => {
    const state = startControlState(status({ executor_active: false, engine_available: true }), []);
    expect(state).toEqual({ disabled: true, reason: REASON_EXECUTOR_INACTIVE });
  });

  it("is disabled, with the reason, when the selected engine is unavailable though the executor is active", () => {
    const state = startControlState(status({ engine_available: false }), []);
    expect(state).toEqual({ disabled: true, reason: REASON_ENGINE_UNAVAILABLE });
  });

  it("is disabled when the request set cannot be loaded", () => {
    expect(startControlState(status({ request_set_version: null, case_count: null }), [])).toEqual({
      disabled: true,
      reason: REASON_REQUEST_SET_UNUSABLE,
    });
  });

  it("fails closed until both the status and the runs are known", () => {
    expect(startControlState(null, [])).toEqual({ disabled: true, reason: REASON_LOADING });
    expect(startControlState(status(), null)).toEqual({ disabled: true, reason: REASON_LOADING });
    expect(startControlState(null, null)).toEqual({ disabled: true, reason: REASON_LOADING });
  });

  it("reports a run in progress before an executor problem (the first applicable reason wins)", () => {
    const state = startControlState(status({ executor_active: false, engine_available: false }), [
      run({ status: "RUNNING" }),
    ]);
    expect(state.reason).toBe(REASON_RUN_IN_PROGRESS);
  });

  it("gives each disabled state its own, non-empty reason", () => {
    const reasons = [
      REASON_LOADING,
      REASON_RUN_IN_PROGRESS,
      REASON_EXECUTOR_INACTIVE,
      REASON_ENGINE_UNAVAILABLE,
      REASON_REQUEST_SET_UNUSABLE,
    ];
    expect(new Set(reasons).size).toBe(5);
    for (const reason of reasons) expect(reason.trim()).not.toBe("");
    expect(REASON_RUN_IN_PROGRESS).toContain("진행 중");
    expect(REASON_EXECUTOR_INACTIVE).toContain("실행기");
    expect(REASON_ENGINE_UNAVAILABLE).toContain("선택된");
  });

  it("does not treat an unrecognised run status as in progress", () => {
    expect(startControlState(status(), [run({ status: "SOMETHING_NEW" })]).disabled).toBe(false);
  });
});

describe("the budget stated beside the control", () => {
  it("is the server's effective budget from the status read", () => {
    expect(budgetHeadline(status({ budget_usd: 2.5 }))).toBe("회당 최대 $2.50");
    expect(budgetHeadline(status({ budget_usd: 1 }))).toBe("회당 최대 $1.00");
  });

  it("explains how the budget is spent, with the floor from the same read", () => {
    const note = budgetNote(status({ budget_usd: 2.5, case_min_budget_usd: 0.2 }));
    expect(note).toContain("$2.50");
    expect(note).toContain("$0.20");
  });
});

describe("the confirmation", () => {
  it("names the engine and model that will run, the most it can spend, and what it will run", () => {
    const view = confirmationView(status({ budget_usd: 2.5 }));
    expect(view).not.toBeNull();
    expect(view?.engineText).toBe("OpenAI");
    expect(view?.modelText).toBe("gpt-5-mini");
    expect(view?.maxSpendText).toBe("최대 $2.50");
    expect(view?.setText).toBe("요청 세트 v1 · 케이스 3개");
    expect(view?.warning).toContain("비용");
  });

  it("says so when the engine runs its own default model", () => {
    const view = confirmationView(status({ engine: "anthropic", model: null }));
    expect(view?.engineText).toBe("Anthropic (Claude)");
    expect(view?.modelText).toBe(NO_MODEL_TEXT);
  });

  it("has nothing to confirm when the status names no engine or no request set", () => {
    expect(confirmationView(status({ engine: null }))).toBeNull();
    expect(confirmationView(status({ request_set_version: null, case_count: null }))).toBeNull();
  });
});

describe("money and time", () => {
  it("never shows a real but tiny cost as $0.00", () => {
    expect(formatCost(0.00051)).toBe("$0.0005");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(0.3)).toBe("$0.30");
    expect(formatCost(1)).toBe("$1.00");
  });

  it("never shows an unmeasured cost as $0.00", () => {
    expect(caseCostText(null)).toBe(UNMEASURED_COST_TEXT);
    expect(caseCostText(null)).not.toContain("$");
    expect(caseCostText(0)).toBe("$0.00"); // a MEASURED zero is a different fact
  });

  it("formats durations, and has none for a missing or invalid one", () => {
    expect(formatDuration(45)).toBe("45초");
    expect(formatDuration(330)).toBe("5분 30초");
    expect(formatDuration(0)).toBe("0초");
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(-1)).toBeNull();
    expect(formatDuration(Number.NaN)).toBeNull();
  });
});

describe("the runs list", () => {
  it("states engine/model, set version, status, passed/total, spend against budget, who, duration", () => {
    const [row] = runRows([run()], (iso) => `<${iso}>`);
    expect(row).toMatchObject({
      engineText: "OpenAI",
      modelText: "gpt-5-mini",
      versionText: "v1",
      fingerprintText: "3f9a1c07be52",
      statusLabel: "완료",
      statusTone: "success",
      passedText: "2 / 3",
      spendText: "$0.30 / $1.00",
      startedByText: "관리자",
      startedAtText: "<2026-10-13T01:00:00Z>",
      durationText: "5분 30초",
      inProgress: false,
    });
  });

  it("shows the prompt fingerprint, and \"기록 없음\" for a run from before it was recorded", () => {
    const rows = runRows([
      run({ prompt_fingerprint: "3f9a1c07be52" }),
      run({ prompt_fingerprint: "aaaaaaaaaaaa" }),
      run({ prompt_fingerprint: null }),
    ]);
    expect(rows.map((r) => r.fingerprintText)).toEqual(["3f9a1c07be52", "aaaaaaaaaaaa", "기록 없음"]);
    expect(fingerprintLabel("")).toBe("기록 없음");
  });

  // a literal per row, so a change of wording or a NULL read as 0 goes red here.
  it.each([
    [2, "수리 시도 최대 2회"],
    [1, "수리 시도 최대 1회"],
    [0, "수리 시도 최대 0회 (수리 사용 안 함)"],
    [null, "수리 설정 기록 없음"],
  ])("states the repair setting a run recorded: %s -> %s", (recorded, text) => {
    const [row] = runRows([run({ repair_max_attempts: recorded })]);
    expect(row.repairSettingText).toBe(text);
  });

  it("marks no run as better: the repair setting line carries no verdict wording", () => {
    const rows = runRows([run({ repair_max_attempts: 2 }), run({ repair_max_attempts: 0 }), run({ repair_max_attempts: null })]);
    for (const row of rows) expect(row.repairSettingText).not.toMatch(/성공|효과|개선|좋|나은|해결|수정/);
  });

  it("counts cases that were not run when the budget stopped the run", () => {
    const [row] = runRows([run({ status: "STOPPED_BUDGET", total: 3, passed: 1, not_run: 2 })]);
    expect(row.passedText).toBe("1 / 3 (미실행 2)");
    expect(row.statusLabel).toBe("예산 소진으로 중단");
  });

  it("shows a run in progress as in progress, with no duration yet", () => {
    const [row] = runRows([run({ status: "RUNNING", duration_seconds: null, ended_at: null })]);
    expect(row.inProgress).toBe(true);
    expect(row.durationText).toBe("진행 중");
  });

  it("never credits anyone when the starter is unknown, and names the default model when none is set", () => {
    const [row] = runRows([run({ started_by_name: null, model: null })]);
    expect(row.startedByText).toBe("알 수 없는 사용자");
    expect(row.modelText).toBe(NO_MODEL_TEXT);
  });

  it("shows an unrecognised status as unknown rather than guessing", () => {
    expect(runStatusLabel("WAT")).toBe(UNKNOWN_RUN_STATUS_LABEL);
    expect(runStatusLabel("constructor")).toBe(UNKNOWN_RUN_STATUS_LABEL);
  });

  it("explains a stopped or failed run in Korean, and says nothing for one that completed", () => {
    expect(runOutcomeNote({ status: "STOPPED_BUDGET" })).toContain("예산");
    expect(runOutcomeNote({ status: "FAILED" })).toContain("끝까지");
    expect(runOutcomeNote({ status: "COMPLETED" })).toBeNull();
    expect(runOutcomeNote({ status: "RUNNING" })).toBeNull();
    // The server's own English detail is never a source.
    expect(runOutcomeNote({ status: "FAILED" })).not.toContain("English");
  });
});

describe("polling", () => {
  it("is on only while some run is in progress", () => {
    expect(shouldPoll([run({ status: "RUNNING" })])).toBe(true);
    expect(shouldPoll([run(), run({ status: "RUNNING" })])).toBe(true);
    expect(shouldPoll([run(), run({ status: "FAILED" }), run({ status: "STOPPED_BUDGET" })])).toBe(false);
    expect(shouldPoll([])).toBe(false);
  });

  it("is off for a run whose status is not recognised, so it cannot poll forever", () => {
    expect(shouldPoll([run({ status: "WAT" })])).toBe(false);
    expect(isRunInProgress({ status: "running" })).toBe(false);
  });

  it("re-reads every five seconds", () => {
    expect(POLL_INTERVAL_MS).toBe(5000);
  });
});

describe("a case's failure reason", () => {
  it("words each eval reason in Korean", () => {
    expect(failureText("PREVIEW_UNHEALTHY", null)).toContain("/healthz");
    expect(failureText("RUN_STUCK", null)).toContain("멈춰");
    expect(failureText("NOT_RUN_BUDGET", null)).toContain("예산");
    expect(failureText("ENGINE_CHANGED", null)).toContain("엔진");
  });

  it("names the failing check by its one-based number", () => {
    expect(failureText("CHECK_FAILED", 0)).toBe("검사 1번 실패");
    expect(failureText("CHECK_FAILED", 1)).toBe("검사 2번 실패");
    expect(failureText("CHECK_FAILED", null)).toBe("검사 실패");
  });

  it("reuses the build failure labels for a build's own code", () => {
    expect(failureText("BUDGET_EXCEEDED", null)).toBe("빌드 예산 초과");
    expect(failureText("TIMEOUT", null)).toBe("제한 시간 초과");
    expect(failureText("MISSING_ENTRYPOINT", null)).toBe("앱의 시작 파일이 없어 빌드를 완료할 수 없음");
    expect(failureText("AGENT_ERROR", null)).toBe("에이전트 오류");
  });

  it("never shows a raw code it does not know", () => {
    const text = failureText("SOMETHING_NEW", null);
    expect(text).toBe("알 수 없는 사유");
    expect(text).not.toContain("SOMETHING_NEW");
  });

  it("is absent for a case with no failure", () => {
    expect(failureText(null, null)).toBeNull();
  });
});

describe("a case's result", () => {
  it("words a judged result, and the state of one not yet judged", () => {
    expect(caseResultLabel("PASSED", "DONE")).toBe("통과");
    expect(caseResultLabel("FAILED", "DONE")).toBe("실패");
    expect(caseResultLabel("NOT_RUN_BUDGET", "DONE")).toBe("예산 부족으로 실행 안 함");
    expect(caseResultLabel(null, "PENDING")).toBe("대기 중");
    expect(caseResultLabel(null, "RUNNING")).toBe("빌드 진행 중");
    expect(caseResultLabel(null, "WAT")).toBe("알 수 없는 상태");
    expect(caseResultLabel("WAT", "DONE")).toBe("알 수 없는 결과");
  });
});

describe("check outcomes", () => {
  const base: EvalCheckOutcome = {
    index: 0,
    method: "GET",
    path: "/",
    expected_status: 200,
    status: 200,
    ok: true,
    outcome: "English platform text",
  };

  it("words a pass, a wrong status, a missing text and an unmade request from structured fields", () => {
    expect(checkOutcomeLine(base)).toBe("검사 1 · GET / · 통과 (응답 200)");
    expect(checkOutcomeLine({ ...base, status: 500, ok: false })).toBe("검사 1 · GET / · 실패 (응답 500, 기대 200)");
    expect(checkOutcomeLine({ ...base, ok: false })).toBe("검사 1 · GET / · 실패 (응답 200, 본문에 기대한 문구가 없음)");
    expect(checkOutcomeLine({ ...base, status: null, ok: false })).toBe("검사 1 · GET / · 실패 (요청을 보내지 못함)");
  });

  it("calls the /healthz request a status check, and never prints the English outcome", () => {
    const line = checkOutcomeLine({ ...base, index: null, path: "/healthz" });
    expect(line).toBe("상태 확인 · GET /healthz · 통과 (응답 200)");
    expect(line).not.toContain("English");
  });
});

describe("a case's policy verdict", () => {
  it("states the tier and the flagged checks, and nothing before the gate has run", () => {
    expect(policyLine(null, [])).toBeNull();
    expect(policyLine("low", [])).toBe("정책 게이트 리스크: 낮음");
    expect(policyLine("high", ["secrets", "egress"])).toBe("정책 게이트 리스크: 높음 · 지적된 검사: secrets, egress");
  });

  it("names the real checks in Korean only, in order, with the unknown ones as sent", () => {
    expect(policyLine("high", ["egress_detection", "secrets_pii"])).toBe(
      "정책 게이트 리스크: 높음 · 지적된 검사: 외부 접속, 비밀·개인정보",
    );
    expect(policyLine("high", ["gate_error"])).toBe("정책 게이트 리스크: 높음 · 지적된 검사: 검사 실행 오류");
    expect(policyLine("medium", ["resource_limits", "sast"])).toBe(
      "정책 게이트 리스크: 중간 · 지적된 검사: 자원 한도, sast",
    );
    expect(policyLine("high", ["egress_detection"])).not.toContain("egress_detection");
  });
});

describe("the link to a case's build", () => {
  it("goes to the project page, and exists only once the case has a build", () => {
    expect(caseBuildPath({ project_id: "p1", build_id: "b1" })).toBe("/projects/p1");
    expect(caseBuildPath({ project_id: null, build_id: null })).toBeNull();
    expect(caseBuildPath({ project_id: "p1", build_id: null })).toBeNull();
  });

  it("encodes the id it puts in the path", () => {
    expect(caseBuildPath({ project_id: "a/b", build_id: "b1" })).toBe("/projects/a%2Fb");
  });
});

describe("the cases table", () => {
  it("orders by position and carries result, reason, checks, cost, tokens, duration and the build link", () => {
    const rows = caseRows([
      evalCase({ id: "c2", case_id: "database-page", position: 1, result: "FAILED", failure_reason: "CHECK_FAILED", failed_check_index: 0, cost_usd: 0.00051, cost_tokens: 800, duration_seconds: 20 }),
      evalCase({
        id: "c1",
        check_outcomes: [{ index: null, method: "GET", path: "/healthz", expected_status: 200, status: 200, ok: true, outcome: "x" }],
        policy_risk_tier: "medium",
        policy_flagged_checks: ["sast"],
      }),
    ]);
    expect(rows.map((r) => r.caseId)).toEqual(["static-page", "database-page"]);
    expect(rows[0]).toMatchObject({
      resultLabel: "통과",
      costText: "$0.30",
      tokensText: "12,345",
      durationText: "1분 15초",
      buildPath: "/projects/p1",
      checkLines: ["상태 확인 · GET /healthz · 통과 (응답 200)"],
      policyText: "정책 게이트 리스크: 중간 · 지적된 검사: sast",
    });
    expect(rows[1]).toMatchObject({
      resultLabel: "실패",
      failureText: "검사 1번 실패",
      costText: "$0.0005",
      tokensText: "800",
    });
  });

  it("shows an unmeasured cost as such, never as $0.00", () => {
    const [row] = caseRows([evalCase({ cost_usd: null, cost_tokens: null })]);
    expect(row.costText).toBe(UNMEASURED_COST_TEXT);
    expect(row.tokensText).toBe("—");
  });

  // the attempts a case's build ran. 0 is a figure; NULL, not run and pending are "—".
  it("shows the attempts a judged case ran: 0 and 1 as figures, NULL as a dash (never 0)", () => {
    const rows = caseRows([
      evalCase({ id: "a", position: 0, repair_attempts: 0 }),
      evalCase({ id: "b", position: 1, repair_attempts: 1 }),
      evalCase({ id: "c", position: 2, repair_attempts: null }),
      evalCase({ id: "d", position: 3, result: "FAILED", failure_reason: "CHECK_FAILED", failed_check_index: 0, repair_attempts: 2 }),
    ]);
    expect(rows.map((r) => r.repairAttemptsText)).toEqual(["0회", "1회", "—", "2회"]);
  });

  it("shows a dash for the attempts of a case that was not run or is pending, whatever the field holds", () => {
    const rows = caseRows([
      evalCase({ id: "a", position: 0, state: "DONE", result: "NOT_RUN_BUDGET", failure_reason: "NOT_RUN_BUDGET", cost_usd: null, cost_tokens: null, repair_attempts: 0, project_id: null, build_id: null }),
      evalCase({ id: "b", position: 1, state: "PENDING", result: null, cost_usd: null, cost_tokens: null, repair_attempts: 1, project_id: null, build_id: null }),
      evalCase({ id: "c", position: 2, state: "RUNNING", result: null, cost_usd: null, cost_tokens: null, repair_attempts: null, project_id: null, build_id: null }),
    ]);
    expect(rows.map((r) => r.repairAttemptsText)).toEqual(["—", "—", "—"]);
  });

  it("shows no cost for a case that was never run or is still waiting", () => {
    const rows = caseRows([
      evalCase({ id: "a", position: 0, state: "DONE", result: "NOT_RUN_BUDGET", failure_reason: "NOT_RUN_BUDGET", cost_usd: null, cost_tokens: null, project_id: null, build_id: null }),
      evalCase({ id: "b", position: 1, state: "PENDING", result: null, cost_usd: null, cost_tokens: null, project_id: null, build_id: null }),
    ]);
    expect(rows[0]).toMatchObject({ costText: "—", tokensText: "—", buildPath: null });
    expect(rows[1]).toMatchObject({ costText: "—", resultLabel: "대기 중", buildPath: null });
  });
});

describe("start messages", () => {
  it("explains a refused start by its status, never by the server text", () => {
    expect(evalStartFailureMessage(409)).toContain("이미 진행 중");
    expect(evalStartFailureMessage(422)).toContain("실행기");
    expect(evalStartFailureMessage(403)).toContain("admin");
    expect(evalStartFailureMessage(500)).toContain("시작하지 못했습니다");
  });

  it("states the engine, model and budget of the run that started", () => {
    const message = startSuccessMessage({ engine: "openai", model: "gpt-5-mini", budget_usd: 1 });
    expect(message).toContain("OpenAI");
    expect(message).toContain("gpt-5-mini");
    expect(message).toContain("$1.00");
  });
});
