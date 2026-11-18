/**
 * How a policy check's name reads on screen: one table, read by the approval list and detail,
 * the project timeline, the eval run line, the configuration tab and the risk key. Pure (ADR 0021).
 *
 * The wire names are literals, as a string that crosses a boundary calls for:
 * backend `services/policy_checks.PolicyCheckName`, plus `gate_error` (the one flagged row written
 * when the gate itself failed). Nothing machine-checks them against the backend; a check added there
 * has to be added here, in the same commit, or it reads as its raw identifier.
 */
import { describe, expect, it } from "vitest";

import {
  POLICY_CHECK_LABELS,
  policyCheckLabel,
  policyCheckLabelWithId,
  policyCheckLabels,
  policyCheckLabelsWithId,
} from "./policyCheckDisplay";

// [wire name, Korean name]
const POLICY_CHECKS = [
  ["secrets_pii", "비밀·개인정보"],
  ["dependency_vulnerabilities", "의존성 취약점"],
  ["egress_detection", "외부 접속"],
  ["resource_limits", "자원 한도"],
  ["template_compliance", "템플릿 규칙"],
  ["destructive_ddl", "DB 구조 파괴"],
  ["gate_error", "검사 실행 오류"],
] as const;

describe("every policy check the gate can name has a Korean name", () => {
  it.each(POLICY_CHECKS)("%s reads as %s, and as `%s (id)` with the identifier", (name, label) => {
    expect(policyCheckLabel(name)).toBe(label);
    expect(policyCheckLabelWithId(name)).toBe(`${label} (${name})`);
  });

  it("covers exactly those names, with distinct labels, none of them the raw name", () => {
    const wire = POLICY_CHECKS.map(([name]) => name);
    expect(Object.keys(POLICY_CHECK_LABELS).sort()).toEqual([...wire].sort());
    const labels = wire.map((name) => policyCheckLabel(name));
    expect(new Set(labels).size).toBe(labels.length);
    for (const name of wire) expect(policyCheckLabel(name)).not.toBe(name);
  });
});

describe("a name this build has never heard of", () => {
  it("is shown as it was sent, by both helpers", () => {
    expect(policyCheckLabel("sast")).toBe("sast");
    expect(policyCheckLabelWithId("sast")).toBe("sast");
    expect(policyCheckLabel("")).toBe("");
    expect(policyCheckLabelWithId("")).toBe("");
  });

  it("is not confused with something inherited from Object.prototype", () => {
    for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(policyCheckLabel(name)).toBe(name);
      expect(policyCheckLabelWithId(name)).toBe(name);
    }
  });
});

describe("the list helpers", () => {
  it("join with a comma and a space, in the order given, an unknown name unchanged", () => {
    expect(policyCheckLabels(["egress_detection", "sast", "secrets_pii"])).toBe("외부 접속, sast, 비밀·개인정보");
    expect(policyCheckLabelsWithId(["egress_detection", "sast"])).toBe("외부 접속 (egress_detection), sast");
  });

  it("give an empty string for no names", () => {
    expect(policyCheckLabels([])).toBe("");
    expect(policyCheckLabelsWithId([])).toBe("");
  });
});

describe("there is one source: no other file spells these names", () => {
  // `"자원 한도"` is a whole quoted literal on purpose: `buildFailure.ts` has `"자원 한도 초과"`, a
  // different phrase. A check name quoted anywhere else in non-test source is a copy that can drift.
  const SOURCE_FILES = import.meta.glob("../**/*.{ts,tsx}", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;
  const LABELS = POLICY_CHECKS.map(([, label]) => label);

  it("reaches the source tree", () => {
    const names = Object.keys(SOURCE_FILES).map((file) => file.split("/").pop());
    expect(names).toContain("policyCheckDisplay.ts");
    expect(names).toContain("approvalDisplay.ts");
    expect(names).toContain("ApprovalConsolePage.tsx");
  });

  it.each(LABELS)("quotes %s only in policyCheckDisplay.ts", (label) => {
    const holders = Object.entries(SOURCE_FILES)
      .filter(([file]) => !/\.test\.tsx?$/.test(file))
      .filter(([, source]) => source.includes(`"${label}"`))
      .map(([file]) => file.split("/").pop());
    expect(holders).toEqual(["policyCheckDisplay.ts"]);
  });
});
