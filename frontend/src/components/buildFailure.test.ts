/**
 * the wording of a failed build on the timeline. Decision logic,
 * not rendering (ADR 0021): the label map and the pure function that turns a
 * reason code plus a detail sentence into the two lines the timeline shows.
 */
import { describe, expect, it } from "vitest";

import {
  BUILD_FAILURE_NEXT_STEPS,
  BUILD_FAILURE_REASON_LABELS,
  UNKNOWN_BUILD_FAILURE_LABEL,
  UNKNOWN_BUILD_FAILURE_NEXT_STEP,
  buildFailureLines,
  buildFailureNextStep,
  buildFailureReasonLabel,
  requesterBuildDetail,
} from "./buildFailure";

// Spelled out, not read from the module under test (a contract
// string that crosses a boundary gets a literal). These are the backend's
// `BuildFailureReason` values.
const ALL_CODES = [
  "TIMEOUT",
  "RESOURCE_LIMIT",
  "BUDGET_EXCEEDED",
  "ENGINE_UNREACHABLE",
  "AGENT_ERROR",
  "HARVEST_ERROR",
  "INFRA_ERROR",
  "MISSING_ENTRYPOINT",
];

describe("buildFailureReasonLabel", () => {
  it("covers exactly the eight reason codes", () => {
    expect(Object.keys(BUILD_FAILURE_REASON_LABELS).sort()).toEqual([...ALL_CODES].sort());
  });

  it("maps every code to a distinct, non-empty Korean label that is not the code", () => {
    const labels = ALL_CODES.map((code) => buildFailureReasonLabel(code));
    for (const [i, code] of ALL_CODES.entries()) {
      expect(labels[i].trim()).not.toBe("");
      expect(labels[i]).not.toBe(code);
      expect(labels[i]).toMatch(/[가-힣]/);
    }
    expect(new Set(labels).size).toBe(ALL_CODES.length);
  });

  it("pins the wording of each label", () => {
    expect(buildFailureReasonLabel("TIMEOUT")).toBe("제한 시간 초과");
    expect(buildFailureReasonLabel("RESOURCE_LIMIT")).toBe("자원 한도 초과");
    expect(buildFailureReasonLabel("BUDGET_EXCEEDED")).toBe("빌드 예산 초과");
    expect(buildFailureReasonLabel("ENGINE_UNREACHABLE")).toBe("AI 엔진에 연결할 수 없음");
    expect(buildFailureReasonLabel("AGENT_ERROR")).toBe("에이전트 오류");
    expect(buildFailureReasonLabel("HARVEST_ERROR")).toBe("결과물 수집 실패");
    expect(buildFailureReasonLabel("INFRA_ERROR")).toBe("인프라 오류");
    expect(buildFailureReasonLabel("MISSING_ENTRYPOINT")).toBe("앱의 시작 파일이 없어 빌드를 완료할 수 없음");
  });

  it("maps an unknown code to the fallback, never to the code itself", () => {
    expect(buildFailureReasonLabel("SOMETHING_NEW")).toBe(UNKNOWN_BUILD_FAILURE_LABEL);
    expect(UNKNOWN_BUILD_FAILURE_LABEL).toBe("알 수 없는 사유");
    expect(buildFailureReasonLabel("")).toBe(UNKNOWN_BUILD_FAILURE_LABEL);
  });

  it("does not resolve a code through the object prototype", () => {
    for (const inherited of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(buildFailureReasonLabel(inherited)).toBe(UNKNOWN_BUILD_FAILURE_LABEL);
    }
  });
});

describe("buildFailureLines — the rendering contract", () => {
  it("leads with the label and never shows the raw code", () => {
    for (const code of ALL_CODES) {
      const { primary } = buildFailureLines(code, "any sentence");
      expect(primary.startsWith("실패 사유: ")).toBe(true);
      expect(primary).toContain(buildFailureReasonLabel(code));
      expect(primary).not.toContain(code);
    }
  });

  it("puts the detail on a separate, labelled secondary line", () => {
    const lines = buildFailureLines("TIMEOUT", "the build did not finish within 30 minutes");

    expect(lines.primary).toBe("실패 사유: 제한 시간 초과");
    expect(lines.secondary).toBe("상세: the build did not finish within 30 minutes");
    expect(lines.primary).not.toContain("30 minutes");
  });

  it("has no secondary line when the build recorded no detail", () => {
    expect(buildFailureLines("AGENT_ERROR", null).secondary).toBeNull();
    expect(buildFailureLines("AGENT_ERROR", undefined).secondary).toBeNull();
    expect(buildFailureLines("AGENT_ERROR", "").secondary).toBeNull();
    expect(buildFailureLines("AGENT_ERROR", "  \n ").secondary).toBeNull();
  });

  it("an unknown code still gets the fallback label, not the code", () => {
    const { primary } = buildFailureLines("SOMETHING_NEW", "d");

    expect(primary).toBe("실패 사유: 알 수 없는 사유");
    expect(primary).not.toContain("SOMETHING_NEW");
  });

  it("passes markup-looking detail through as text, unaltered, for React to escape", () => {
    const hostile = "<img src=x onerror=alert(1)> & <script>";

    expect(buildFailureLines("INFRA_ERROR", hostile).secondary).toBe(`상세: ${hostile}`);
  });
});

describe("buildFailureNextStep", () => {
  it("has a next step for exactly the codes that have a label", () => {
    expect(Object.keys(BUILD_FAILURE_NEXT_STEPS).sort()).toEqual([...ALL_CODES].sort());
  });

  it("is a non-empty Korean sentence for every code, and not the code", () => {
    for (const code of ALL_CODES) {
      const step = buildFailureNextStep(code);
      expect(step.trim(), code).not.toBe("");
      expect(step, code).toMatch(/[가-힣]/);
      expect(step, code).not.toContain(code);
    }
  });

  it("names an action the requester has, and no log, path or command", () => {
    for (const code of ALL_CODES) {
      const step = buildFailureNextStep(code);
      expect(step, code).toMatch(/다시 시도|변경 요청|관리자에게 문의/);
      expect(step, code).not.toMatch(/로그|log|명령|docker|[/\\`]/i);
    }
  });

  it("falls back to asking an administrator for a code it does not know", () => {
    expect(buildFailureNextStep("SOMETHING_NEW")).toBe(UNKNOWN_BUILD_FAILURE_NEXT_STEP);
    expect(UNKNOWN_BUILD_FAILURE_NEXT_STEP).toBe("관리자에게 문의하세요.");
    for (const inherited of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(buildFailureNextStep(inherited)).toBe(UNKNOWN_BUILD_FAILURE_NEXT_STEP);
    }
  });

  it("pins the wording of two, so a rewrite is a visible edit", () => {
    expect(buildFailureNextStep("HARVEST_ERROR")).toBe("다시 시도해 보세요. 반복되면 관리자에게 문의하세요.");
    expect(buildFailureNextStep("ENGINE_UNREACHABLE")).toBe(
      "잠시 후 다시 시도해 보세요. 계속되면 관리자에게 문의하세요.",
    );
  });

  it("buildFailureLines carries it as its own line for every code, known or not", () => {
    for (const code of [...ALL_CODES, "SOMETHING_NEW"]) {
      expect(buildFailureLines(code, null).nextStep).toBe(`다음 단계: ${buildFailureNextStep(code)}`);
    }
  });
});

describe("requesterBuildDetail: the operator's pointer to the worker log is not shown", () => {
  // The real shapes `sandbox_build_executor.py` and `worker.py` store (the backend test
  // `test_worker_log_phrase_shapes.py` limits the writers to these two tails).
  const SEE = "see the worker log";
  const OUTPUT = "its output is in the worker log";

  it("cuts the tail of every stored shape and keeps the platform's sentence", () => {
    const cases: [string, string][] = [
      [`the build executor raised KeyError; ${SEE}`, "the build executor raised KeyError"],
      [
        `harvesting the build's output failed at pushing the branch (OSError); ${SEE}`,
        "harvesting the build's output failed at pushing the branch (OSError)",
      ],
      [`the LLM proxy container failed to start; ${SEE}`, "the LLM proxy container failed to start"],
      [
        `the usage request to the proxy failed (ProxyStartError); ${SEE}`,
        "the usage request to the proxy failed (ProxyStartError)",
      ],
      [
        `the agent run in the sandbox ended unsuccessfully (AGENT_ERROR); ${OUTPUT}`,
        "the agent run in the sandbox ended unsuccessfully (AGENT_ERROR)",
      ],
      [
        `the policy gate failed (X) and its failure could not be recorded (Y), so the build has no policy verdict; ${SEE}`,
        "the policy gate failed (X) and its failure could not be recorded (Y), so the build has no policy verdict",
      ],
    ];
    for (const [stored, shown] of cases) {
      expect(requesterBuildDetail(stored), stored).toBe(shown);
    }
  });

  it("is case-insensitive and tolerates a trailing full stop or space", () => {
    expect(requesterBuildDetail("it broke; See The Worker Log. ")).toBe("it broke");
  });

  it("leaves a detail with no such tail untouched", () => {
    const plain = "the build did not finish within 30 minutes";
    expect(requesterBuildDetail(plain)).toBe(plain);
  });

  it("shows nothing when the phrase was all there was, or when there is no detail", () => {
    expect(requesterBuildDetail(SEE)).toBeNull();
    expect(requesterBuildDetail(`; ${SEE}`)).toBeNull();
    expect(requesterBuildDetail(null)).toBeNull();
    expect(requesterBuildDetail(undefined)).toBeNull();
    expect(requesterBuildDetail("   ")).toBeNull();
  });

  it("drops a detail whole when it still names the worker log in a wording it does not know", () => {
    expect(requesterBuildDetail("check the worker log for the failing step")).toBeNull();
    expect(requesterBuildDetail("it broke; ask ops, the worker log has more; see the worker log")).toBeNull();
  });

  it("buildFailureLines never puts the phrase on the screen", () => {
    const lines = buildFailureLines("INFRA_ERROR", `the build executor raised KeyError; ${SEE}`);

    expect(lines.secondary).toBe("상세: the build executor raised KeyError");
    expect(JSON.stringify(lines)).not.toMatch(/worker log/i);
  });
});
