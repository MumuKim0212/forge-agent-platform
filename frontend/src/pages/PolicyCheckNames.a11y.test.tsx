// @vitest-environment jsdom
/**
 * (ADR 0021 §Amendment): the places a policy check's name reaches the screen print its Korean
 * name, and the two places where the identifier is the evidence print it too. The table is
 * `policyCheckDisplay.test.ts`'s; this file only proves each surface reads it (and that a name the
 * table does not know is printed as sent), so a surface that quietly went back to the raw name fails.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "../api/approvals";
import { consoleApi } from "../api/console";
import { Timeline } from "../components/Timeline";
import type { ApprovalRequest, ApprovalRequestSummary, PlatformConfiguration } from "../types/api";
import { ConfigurationPanel } from "./admin/ConfigurationPanel";
import { ApprovalConsolePage } from "./ApprovalConsolePage";

vi.mock("../api/approvals", () => ({
  approvalsApi: { list: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn() },
}));

vi.mock("../api/console", () => ({ consoleApi: { configuration: vi.fn() } }));

vi.mock("../context/IdentityContext", () => ({
  useIdentity: () => ({
    currentUser: {
      id: "u-approver",
      name: "승인자",
      email: "approver@example.test",
      role: "approver",
      team_id: null,
      created_at: "2026-10-01T00:00:00Z",
    },
    mode: "local",
    loading: false,
  }),
}));

const SUMMARY: ApprovalRequestSummary = {
  id: "a1",
  build_id: "b1",
  project_id: "p1",
  project_name: "휴가 도구",
  requester_id: "u-req",
  requester_name: "요청자",
  risk_tier: "high",
  status: "PENDING",
  required_approvals: 1,
  approvals_so_far: 0,
  flagged_checks: ["secrets_pii", "egress_detection", "sast"],
  diff_ref: null,
  preview_url: null,
  changed_counts: null,
  created_at: "2026-10-01T00:00:00Z",
};

const DETAIL: ApprovalRequest = {
  id: "a1",
  build_id: "b1",
  project_id: "p1",
  requester_id: "u-req",
  project_owner_id: "u-owner",
  policy_verdict_id: "v1",
  risk_tier: "high",
  status: "PENDING",
  required_approvals: 1,
  superseded_by_build_id: null,
  superseded_at: null,
  created_at: "2026-10-01T00:00:00Z",
  decisions: [],
  policy_verdict: {
    id: "v1",
    build_id: "b1",
    risk_tier: "high",
    created_at: "2026-10-01T00:00:00Z",
    checks: [
      { id: "c1", check_name: "secrets_pii", result: "flag", summary: "1 finding", findings: null },
      { id: "c2", check_name: "resource_limits", result: "pass", summary: "ok", findings: null },
      { id: "c3", check_name: "sast", result: "pass", summary: "ok", findings: null },
    ],
  },
  request_text: "휴가 신청 화면을 만들어 주세요",
  changed_files: null,
  protected_path_drift: [],
  smoke_checks: null,
  agent_summary: null,
};

beforeEach(() => {
  vi.mocked(approvalsApi.list).mockReset().mockResolvedValue([SUMMARY]);
  vi.mocked(approvalsApi.get).mockReset().mockResolvedValue(DETAIL);
  vi.mocked(consoleApi.configuration).mockReset();
});

describe("the approval console", () => {
  it("prints the flagged checks of a list row in Korean only, an unknown name as sent", async () => {
    render(<ApprovalConsolePage />);
    const row = await screen.findByRole("listitem", { name: "휴가 도구 요청 상세 보기" });

    expect(row.textContent).toContain("지적된 검사: 비밀·개인정보, 외부 접속, sast");
    expect(row.textContent).not.toContain("secrets_pii");
    expect(row.textContent).not.toContain("egress_detection");
  });

  it("prints each check of the detail as the Korean name followed by its identifier", async () => {
    const user = userEvent.setup();
    render(<ApprovalConsolePage />);
    await user.click(await screen.findByRole("listitem", { name: "휴가 도구 요청 상세 보기" }));
    await user.click(await screen.findByRole("tab", { name: /^점검/ }));
    const heading = await screen.findByRole("heading", { name: /^정책 검사/ });
    const list = heading.parentElement?.querySelector("ul");
    if (!list) throw new Error("the policy checks have no list");

    const names = Array.from(list.querySelectorAll(":scope > li > strong")).map((s) => s.textContent);
    expect(names).toEqual(["비밀·개인정보 (secrets_pii)", "자원 한도 (resource_limits)", "sast"]);
  });
});

describe("the project timeline's policy entry", () => {
  const entries = (checks: { check_name: string; result: string; summary: string }[]) => [
    { kind: "policy_verdict" as const, ts: "2026-10-01T00:00:00Z", data: { risk_tier: "high", checks } },
  ];

  it("says 위험도 and prints the flagged checks in Korean only, a passing check not at all", () => {
    const { container } = render(
      <Timeline
        entries={entries([
          { check_name: "egress_detection", result: "flag", summary: "" },
          { check_name: "template_compliance", result: "pass", summary: "" },
          { check_name: "sast", result: "flag", summary: "" },
        ])}
      />,
    );

    const text = container.textContent ?? "";
    expect(text).toContain("정책 검사: 위험도");
    expect(text).toContain("지적된 검사: 외부 접속, sast");
    expect(text).not.toContain("egress_detection");
    expect(text).not.toContain("템플릿 규칙");
    expect(text).not.toContain("리스크 티어");
  });
});

describe("the configuration tab's tier rows", () => {
  const CONFIG: PlatformConfiguration = {
    source: "environment",
    editable: false,
    change_requires_restart: true,
    values: {
      policy_medium_tier_checks: ["dependency_vulnerabilities", "resource_limits", "sast"],
      policy_high_tier_checks: ["secrets_pii", "egress_detection"],
      policy_medium_approvals_required: 1,
      policy_high_approvals_required: 1,
      build_token_budget: 1000,
      build_cost_budget_usd: 1,
      sandbox_memory_limit_mb: 512,
      sandbox_cpu_limit_cores: 1,
      sandbox_pids_limit: 100,
      build_max_duration_minutes: 10,
      preview_ttl_minutes: 30,
      quota_monthly_usd_default: 10,
      platform_timezone: "Asia/Seoul",
      service_data_expected_services: 1,
      service_data_connections_per_service: 1,
      project_lifecycle_idle_threshold_days: 90,
      project_lifecycle_live_services_exempt: true,
      traffic_signal_configured: false,
      traffic_signal_observed_since: null,
      traffic_signal_last_poll_at: null,
      host_disk_check_interval_seconds: 60,
      host_disk_min_free_percent: 10,
      host_disk_min_free_gb: 5,
    },
  };

  it("labels the rows with the level word, 위험도, and lists names with their identifiers", async () => {
    vi.mocked(consoleApi.configuration).mockResolvedValue(CONFIG);
    const { container } = render(<ConfigurationPanel />);
    await screen.findByText("중간 위험도로 올리는 검사");

    const detailOf = (term: string) => screen.getByText(term).closest("div")?.textContent ?? "";
    expect(detailOf("중간 위험도로 올리는 검사")).toContain(
      "의존성 취약점 (dependency_vulnerabilities), 자원 한도 (resource_limits), sast",
    );
    expect(detailOf("높음 위험도로 올리는 검사")).toContain("비밀·개인정보 (secrets_pii), 외부 접속 (egress_detection)");
    expect(screen.getByText("중간 위험도 필요 승인 수")).toBeInTheDocument();
    expect(screen.getByText("높음 위험도 필요 승인 수")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "정책 게이트 — 위험도 판정 기준" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("티어");
  });
});
