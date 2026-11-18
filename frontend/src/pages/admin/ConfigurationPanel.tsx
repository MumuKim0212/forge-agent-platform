import { useEffect, useState, type ReactNode } from "react";

import { consoleApi } from "../../api/console";
import { riskTierLabel } from "../../components/approvalDisplay";
import { policyCheckLabelsWithId } from "../../components/policyCheckDisplay";
import { Alert } from "../../components/ui/Alert";
import { Badge } from "../../components/ui/Badge";
import { DescriptionList } from "../../components/ui/DescriptionList";
import { Skeleton } from "../../components/ui/Skeleton";
import type { PlatformConfiguration, PlatformConfigurationValues } from "../../types/api";
import { panelErrorMessage } from "./panelError";
import { exemptionReason, trafficSignalState } from "./trafficSignal";

/**
 * The effective platform configuration.
 *
 * **There is no edit control on this screen — not even a disabled one.** These
 * values are read from the environment once at startup, so an input
 * here could only fail; rule 3 says a disabled control with no explanation is
 * worse than a plainly read-only display, which is why the banner explains
 * *where* the change is made instead. The response's `source`, `editable` and
 * `change_requires_restart` are what it explains that from.
 *
 * now renders all **16** of the backend's allowlisted fields, not
 * 13 — `service_data_expected_services`/`service_data_connections_per_service`
 * and `project_lifecycle_idle_threshold_days` had been on the
 * wire (and in `PlatformConfigurationValues`) since those tasks, with no row
 * here to render them. Nothing here iterates the response object: a field the
 * backend adds still shows up only when someone adds it below, the same
 * deliberate-edit property the allowlist has on the server side.
 *
 * a 17th value, `project_lifecycle_live_services_exempt`, sits under the
 * idle threshold — the one place an operator is told that, while Traefik routes,
 * a project with an active deployment is never flagged, stopped or archived.
 */
export function ConfigurationPanel() {
  const [config, setConfig] = useState<PlatformConfiguration | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setConfig(await consoleApi.configuration());
      } catch (err) {
        setError(panelErrorMessage(err, "플랫폼 설정을 불러오지 못했습니다."));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <Skeleton label="플랫폼 설정 불러오는 중" />;
  if (error) {
    return (
      <Alert variant="error" live>
        {error}
      </Alert>
    );
  }
  if (config === null) return null;

  const v = config.values;

  return (
    <div>
      <Alert variant="warning">
        <strong>조회 전용</strong> — 아래 값은 서버 환경(<code>{config.source}</code>)에서 읽은 설정입니다.
        {!config.editable && " 이 화면에서는 수정할 수 없습니다."}
        {config.change_requires_restart && " 값을 바꾸려면 서버의 환경 설정을 변경한 뒤 재시작해야 합니다."}
        <span className="mt-1 block text-xs text-amber-700 dark:text-amber-400">
          사용자·프로젝트별 월 한도(개별 지정)는 「사용량 · 쿼터」 탭에서, 빌드에 쓰는 에이전트 엔진은
          「에이전트 엔진」 탭에서 즉시 변경할 수 있습니다(여기가 아닙니다). 그 값들만 DB에 있고, 나머지
          플랫폼 설정은 환경에 있습니다.
        </span>
      </Alert>

      <Section title="정책 게이트 — 위험도 판정 기준">
        <DescriptionList
          items={[
            { term: `${riskTierLabel("medium")} 위험도로 올리는 검사`, detail: checkList(v.policy_medium_tier_checks) },
            { term: `${riskTierLabel("high")} 위험도로 올리는 검사`, detail: checkList(v.policy_high_tier_checks) },
            { term: `${riskTierLabel("medium")} 위험도 필요 승인 수`, detail: v.policy_medium_approvals_required },
            { term: `${riskTierLabel("high")} 위험도 필요 승인 수`, detail: v.policy_high_approvals_required },
          ]}
        />
      </Section>

      <Section title="빌드 예산 — 빌드 단위로 강제되며, 월 쿼터와는 별개">
        <DescriptionList
          items={[
            { term: "빌드당 토큰 예산", detail: v.build_token_budget.toLocaleString() },
            { term: "빌드당 비용 예산 (USD)", detail: v.build_cost_budget_usd },
            { term: "빌드 최대 소요 시간 (분)", detail: v.build_max_duration_minutes },
          ]}
        />
      </Section>

      <Section title="샌드박스 자원 상한">
        <DescriptionList
          items={[
            { term: "메모리 (MB)", detail: v.sandbox_memory_limit_mb },
            { term: "CPU (코어)", detail: v.sandbox_cpu_limit_cores },
            { term: "프로세스 수(pids)", detail: v.sandbox_pids_limit },
          ]}
        />
      </Section>

      <Section title="프리뷰 · 월 쿼터 기본값">
        <DescriptionList
          items={[
            { term: "프리뷰 유지 시간 (분)", detail: v.preview_ttl_minutes },
            { term: "월 한도 전역 기본값 (USD)", detail: v.quota_monthly_usd_default },
            { term: "집계 기준 시간대", detail: v.platform_timezone },
          ]}
        />
      </Section>

      <Section title="데이터 서비스 · 프로젝트 생명주기">
        <DescriptionList
          items={[
            { term: "예상 서비스 수", detail: v.service_data_expected_services },
            { term: "서비스당 커넥션 수", detail: v.service_data_connections_per_service },
            { term: "유휴 판정 기준(일)", detail: v.project_lifecycle_idle_threshold_days },
            { term: "디스크 점검 주기 (초)", detail: v.host_disk_check_interval_seconds },
            { term: "디스크 여유 하한 (%)", detail: v.host_disk_min_free_percent },
            { term: "디스크 여유 하한 (GB)", detail: v.host_disk_min_free_gb },
            {
              term: "활성 서비스의 유휴 판정 제외",
              detail: exemptionReason(
                trafficSignalState(v, new Date()),
                v.project_lifecycle_live_services_exempt,
              ),
            },
          ]}
        />
      </Section>

      <Section title="유휴 판정 트래픽 신호 — 런타임 상태 (환경 설정 아님)">
        <TrafficSignal values={v} />
      </Section>
    </div>
  );
}

/** renders `trafficSignalState`; no state is decided in this markup. */
function TrafficSignal({ values }: { values: PlatformConfigurationValues }) {
  const state = trafficSignalState(values, new Date());
  const when = (iso: string | null) => (iso === null ? "—" : new Date(iso).toLocaleString());
  switch (state.kind) {
    case "traefik_not_routing":
      return (
        <p className="text-sm">
          <Badge tone="success">사용 가능</Badge> Traefik을 경유하지 않습니다. 서비스 프록시가 트래픽을 직접
          기록하므로 활성 서비스도 유휴 판정 대상입니다.
        </p>
      );
    case "signal_off":
      return (
        <Alert variant="info">
          Traefik이 라우팅하지만 트래픽 신호가 꺼져 있습니다. 활성 서비스는 유휴 판정에서 제외됩니다.
          신호를 켜려면 서버 환경의 <code>TRAEFIK_METRICS_PORT</code>를 설정하세요.
        </Alert>
      );
    case "configured_never_read":
      return (
        <Alert variant="warning">
          트래픽 신호는 설정되어 있으나 아직 한 번도 읽지 못했습니다. 활성 서비스는 유휴 판정에서 제외됩니다.
          워커 로그를 확인하세요. 흔한 원인은 메트릭 엔트리포인트가 <code>127.0.0.1</code>에만 바인딩되어 있지
          않아 워커가 거부한 경우, 포트가 잘못된 경우, 워커가 실행 중이 아닌 경우입니다.
        </Alert>
      );
    case "not_yet_usable":
      return (
        <Alert variant="warning">
          트래픽 신호는 켜져 있으나 아직 사용할 수 없습니다. 활성 서비스는 계속 유휴 판정에서 제외됩니다.
          <span className="mt-1 block text-xs">
            관찰 시작: {when(state.observedSince)} · 마지막 읽기: {when(state.lastPollAt)}
          </span>
          {state.unmet.includes("stale_read") && (
            <span className="mt-1 block text-xs">마지막 읽기가 3분보다 오래되었습니다.</span>
          )}
          {state.unmet.includes("window_too_short") && (
            <span className="mt-1 block text-xs">
              관찰을 시작한 지 유휴 판정 기준({values.project_lifecycle_idle_threshold_days}일)이 아직
              지나지 않았습니다.
            </span>
          )}
        </Alert>
      );
    case "usable":
      return (
        <div className="text-sm">
          <p>
            <Badge tone="success">사용 가능</Badge> 트래픽 신호가 켜져 있고 유효합니다. 활성 서비스도 기준일에
            따라 유휴 판정됩니다.
          </p>
          <p className="mt-1 text-xs">
            관찰 시작: {when(state.observedSince)} · 마지막 읽기: {when(state.lastPollAt)}
          </p>
        </div>
      );
  }
}

/** The tier check lists: each name in Korean with its identifier, the one an operator writes into `.env`. */
function checkList(values: string[]): string {
  return values.length === 0 ? "없음" : policyCheckLabelsWithId(values);
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-5">
      <h3 className="mb-1 text-sm font-semibold text-slate-900 dark:text-white">{title}</h3>
      {children}
    </section>
  );
}
