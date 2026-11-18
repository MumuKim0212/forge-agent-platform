/**
 * the wording of a failed deployment's reason code. Decision logic, not
 * rendering (ADR 0021): the label map and the lookup every deployment-failure
 * surface goes through. The backend side of the parity pin lives in
 * `backend/tests/test_deployment_failure_codes_parity.py`, which reads the
 * `ALL_DEPLOYMENT_CODES` literal below as text.
 */
import { describe, expect, it } from "vitest";

import {
  DEPLOYMENT_FAILURE_NEXT_STEPS,
  DEPLOYMENT_FAILURE_REASON_LABELS,
  UNKNOWN_DEPLOYMENT_FAILURE_LABEL,
  UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP,
  deploymentFailureLine,
  deploymentFailureNextStep,
  deploymentFailureNextStepLine,
  deploymentFailureReasonLabel,
} from "./deploymentFailure";

// Spelled out, not read from the module under test (a contract
// string that crosses a boundary gets a literal). These are the backend's
// `DeploymentFailureReason` values; a backend test compares them to the enum.
const ALL_DEPLOYMENT_CODES = [
  "MERGE_FAILED",
  "IMAGE_MISSING",
  "START_FAILED",
  "HEALTH_TIMEOUT",
  "ROUTE_FAILED",
  "PROVISION_UNCONFIGURED",
  "PROVISION_FAILED",
  "INFRA_ERROR",
];

describe("deploymentFailureReasonLabel", () => {
  it("covers exactly the eight reason codes", () => {
    expect(Object.keys(DEPLOYMENT_FAILURE_REASON_LABELS).sort()).toEqual(
      [...ALL_DEPLOYMENT_CODES].sort(),
    );
  });

  it("maps every code to a distinct, non-empty Korean label that is not the code", () => {
    const labels = ALL_DEPLOYMENT_CODES.map((code) => deploymentFailureReasonLabel(code));
    for (const [i, code] of ALL_DEPLOYMENT_CODES.entries()) {
      expect(labels[i].trim()).not.toBe("");
      expect(labels[i]).not.toBe(code);
      expect(labels[i]).toMatch(/[가-힣]/);
    }
    expect(new Set(labels).size).toBe(ALL_DEPLOYMENT_CODES.length);
  });

  it("pins the wording of each label", () => {
    expect(deploymentFailureReasonLabel("MERGE_FAILED")).toBe("변경 병합 실패");
    expect(deploymentFailureReasonLabel("IMAGE_MISSING")).toBe("빌드 이미지를 찾을 수 없음");
    expect(deploymentFailureReasonLabel("START_FAILED")).toBe("서비스 시작 실패");
    expect(deploymentFailureReasonLabel("HEALTH_TIMEOUT")).toBe("상태 확인 시간 초과");
    expect(deploymentFailureReasonLabel("ROUTE_FAILED")).toBe("접속 경로 등록 실패");
    expect(deploymentFailureReasonLabel("PROVISION_UNCONFIGURED")).toBe(
      "데이터 서비스 설정이 필요함",
    );
    expect(deploymentFailureReasonLabel("PROVISION_FAILED")).toBe("데이터 서비스 준비 실패");
    expect(deploymentFailureReasonLabel("INFRA_ERROR")).toBe("인프라 오류");
  });

  it("words the unconfigured precondition as a missing step, not as a failure", () => {
    expect(deploymentFailureReasonLabel("PROVISION_UNCONFIGURED")).not.toMatch(/실패|오류/);
  });

  it("maps an unknown code to the fallback, never to the code itself", () => {
    expect(deploymentFailureReasonLabel("SOMETHING_NEW")).toBe(UNKNOWN_DEPLOYMENT_FAILURE_LABEL);
    expect(UNKNOWN_DEPLOYMENT_FAILURE_LABEL).toBe("알 수 없는 사유");
    expect(deploymentFailureReasonLabel("")).toBe(UNKNOWN_DEPLOYMENT_FAILURE_LABEL);
  });

  it("does not resolve a code through the object prototype", () => {
    for (const inherited of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(deploymentFailureReasonLabel(inherited)).toBe(UNKNOWN_DEPLOYMENT_FAILURE_LABEL);
    }
  });
});

describe("deploymentFailureLine", () => {
  it("leads with 실패 사유 and the label, and never shows the raw code", () => {
    for (const code of ALL_DEPLOYMENT_CODES) {
      const line = deploymentFailureLine(code);
      expect(line.startsWith("실패 사유: ")).toBe(true);
      expect(line).toContain(deploymentFailureReasonLabel(code));
      expect(line).not.toContain(code);
    }
  });

  it("an unknown code still gets the fallback label, not the code", () => {
    expect(deploymentFailureLine("SOMETHING_NEW")).toBe("실패 사유: 알 수 없는 사유");
  });
});

describe("deploymentFailureNextStep", () => {
  it("has a next step for exactly the codes that have a label", () => {
    expect(Object.keys(DEPLOYMENT_FAILURE_NEXT_STEPS).sort()).toEqual([...ALL_DEPLOYMENT_CODES].sort());
  });

  it("is a non-empty Korean sentence for every code, and not the code", () => {
    for (const code of ALL_DEPLOYMENT_CODES) {
      const step = deploymentFailureNextStep(code);
      expect(step.trim(), code).not.toBe("");
      expect(step, code).toMatch(/[가-힣]/);
      expect(step, code).not.toContain(code);
    }
  });

  it("names an action the requester has, and no log, path or command", () => {
    for (const code of ALL_DEPLOYMENT_CODES) {
      const step = deploymentFailureNextStep(code);
      expect(step, code).toMatch(/다시 시도|변경 요청|관리자에게/);
      expect(step, code).not.toMatch(/로그|log|명령|docker|[/\\`]/i);
    }
  });

  it("sends the unconfigured precondition to an administrator, not to a retry", () => {
    const step = deploymentFailureNextStep("PROVISION_UNCONFIGURED");

    expect(step).toContain("관리자에게");
    expect(step).not.toContain("다시 시도");
  });

  it("falls back to asking an administrator for a code it does not know", () => {
    expect(deploymentFailureNextStep("SOMETHING_NEW")).toBe(UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP);
    expect(UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP).toBe("관리자에게 문의하세요.");
    for (const inherited of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(deploymentFailureNextStep(inherited)).toBe(UNKNOWN_DEPLOYMENT_FAILURE_NEXT_STEP);
    }
  });

  it("the line leads with 다음 단계 and carries the step", () => {
    expect(deploymentFailureNextStepLine("ROUTE_FAILED")).toBe(
      "다음 단계: 배포를 다시 시도해 보세요. 반복되면 관리자에게 문의하세요.",
    );
    expect(deploymentFailureNextStepLine("SOMETHING_NEW")).toBe("다음 단계: 관리자에게 문의하세요.");
  });
});
