/**
 * How a policy check's name reads on screen: the one table every place that prints a check
 * name reads (the approval list row, the approval detail's check list, the project timeline, the eval
 * run line, the admin configuration tab and the risk key's descriptions). The wire names are
 * `PolicyCheckName` in `backend/app/services/policy_checks.py`, plus `gate_error`: not a check but the
 * single flagged row written when the gate itself failed. They are contract strings and stay exactly
 * as the backend sends them; only what a person reads is translated.
 *
 * Pure, no React (the shape of `approvalDisplay.ts`), so a test reaches it. A name this build has
 * never heard of is shown as it was sent.
 */
/** The row the gate writes, flagged, when the gate itself failed: not a check. A contract string. */
export const GATE_ERROR_CHECK = "gate_error";

export const POLICY_CHECK_LABELS: Readonly<Record<string, string>> = {
  secrets_pii: "비밀·개인정보",
  dependency_vulnerabilities: "의존성 취약점",
  egress_detection: "외부 접속",
  resource_limits: "자원 한도",
  template_compliance: "템플릿 규칙",
  destructive_ddl: "DB 구조 파괴",
  [GATE_ERROR_CHECK]: "검사 실행 오류",
};

/** An own-property lookup: `"constructor"` or `"__proto__"` sent as a name must not resolve to
 * something inherited from `Object.prototype`. */
function own(table: Readonly<Record<string, string>>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/** The Korean name alone, for a line a person reads (a list of what was flagged). */
export function policyCheckLabel(name: string): string {
  return own(POLICY_CHECK_LABELS, name) ?? name;
}

/** The Korean name followed by the identifier, `자원 한도 (resource_limits)`, where the identifier is
 * the evidence or the thing an operator types. A name with no Korean name is just the raw string. */
export function policyCheckLabelWithId(name: string): string {
  const label = own(POLICY_CHECK_LABELS, name);
  return label === undefined ? name : `${label} (${name})`;
}

/** Korean names joined the way every list line prints them. */
export function policyCheckLabels(names: ReadonlyArray<string>): string {
  return names.map(policyCheckLabel).join(", ");
}

/** The same, name with identifier, joined the same way (the configuration tab's tier lists). */
export function policyCheckLabelsWithId(names: ReadonlyArray<string>): string {
  return names.map(policyCheckLabelWithId).join(", ");
}
