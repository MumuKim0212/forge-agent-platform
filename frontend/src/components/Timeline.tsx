import { isApprovalPending } from "../auth/approvalGate";
import { approvalStatusLabel, approvalStatusTone, riskTierLabel, riskTierTone } from "./approvalDisplay";
import { Alert, Badge, EmptyState, linkClassName } from "./ui";
import { buildFailureLines } from "./buildFailure";
import { queueWaitLine } from "./buildQueue";
import { isBuildNotImplemented } from "./buildStatus";
import { BuildTraceEntry } from "./BuildTracePanel";
import { deploymentFailureLine, deploymentFailureNextStepLine } from "./deploymentFailure";
import { buildEngineLine } from "./engineDisplay";
import { AgentSummary, DriftLine, PreviewStartFailureNote, RepairAttemptsNote } from "./MeasuredFacts";
import { asPreviewStartFailure } from "./previewStartFailure";
import { asChangedCounts, asStringList, changedCountsLine, requestTextLine } from "./measuredFactsLines";
import { policyCheckLabels } from "./policyCheckDisplay";
import { StatusBadge } from "./StatusBadge";
import type { TimelineEntry } from "../types/api";

function renderEntry(entry: TimelineEntry, canReadTrace: boolean) {
  switch (entry.kind) {
    case "change_request":
      return (
        <>
          <strong>변경 요청</strong>: {requestTextLine(entry.data.nl_request)}
        </>
      );
    case "build": {
      const diffRef = entry.data.diff_ref ? String(entry.data.diff_ref) : null;
      const failure = entry.data.failure_reason
        ? buildFailureLines(
            String(entry.data.failure_reason),
            typeof entry.data.failure_detail === "string" ? entry.data.failure_detail : null,
          )
        : null;
      // what the platform measured. Nothing for a build that was not measured.
      const engineLine = buildEngineLine(
        typeof entry.data.engine === "string" ? entry.data.engine : null,
        typeof entry.data.model === "string" ? entry.data.model : null,
      );
      // why a QUEUED build is still waiting; null for every other status.
      const waitLine = queueWaitLine(entry.data.queue);
      const countsLine = changedCountsLine(asChangedCounts(entry.data.changed_counts));
      const drift = asStringList(entry.data.protected_path_drift);
      const startFailure = asPreviewStartFailure(entry.data.preview_start_failure);
      const previewUrl = entry.data.preview_url ? String(entry.data.preview_url) : null;
      const previewExpiresAt = entry.data.preview_expires_at
        ? String(entry.data.preview_expires_at)
        : null;
      return (
        <>
          <strong>빌드 ({String(entry.data.type)})</strong>{" "}
          <StatusBadge status={String(entry.data.status)} />
          {isBuildNotImplemented(entry.data.status) && (
            <span className="ml-2 text-slate-500 dark:text-slate-400">
              에이전트 빌드 단계는 아직 구현되지 않았습니다.
            </span>
          )}
          {waitLine && <div className="mt-1 text-slate-600 dark:text-slate-300">{waitLine}</div>}
          {diffRef && (
            <div className="mt-1">
              PR: <span>{diffRef}</span>
            </div>
          )}
          {/* the engine and the configured model, recorded by the platform at claim. */}
          {engineLine && <div className="mt-1">{engineLine}</div>}
          {countsLine && <div className="mt-1">{countsLine}</div>}
          <DriftLine drift={drift} />
          <PreviewStartFailureNote code={startFailure} compact />
          {/* shown only when a repair ran; `data` is untyped, the note reads it safely. */}
          <RepairAttemptsNote attempts={entry.data.repair_attempts} compact />
          {previewUrl && (
            <div className="mt-1">
              <a
                href={previewUrl}
                target="_blank"
                rel="noreferrer"
                className={linkClassName()}
              >
                프리뷰 열기
              </a>
              {previewExpiresAt && (
                <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                  (만료: {new Date(previewExpiresAt).toLocaleString()})
                </span>
              )}
            </div>
          )}
          {failure && (
            <>
              {/* A timeline entry is a record of something that
                  already happened, however long ago — not a fresh result of
                  the viewer's own action, so no `live`, here or on the
                  detail line below. */}
              <Alert variant="error" className="mt-1">
                {failure.primary}
              </Alert>
              {failure.secondary && (
                // the platform's own sentence, as plain text (React
                // escapes it) and visibly secondary to the Korean label.
                <p className="mt-1 break-words text-xs text-slate-500 dark:text-slate-400">
                  {failure.secondary}
                </p>
              )}
              {/* what the requester can do next, from the platform's own wording. */}
              <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">{failure.nextStep}</p>
            </>
          )}
          {/* the agent's own words come last, after every measured fact, collapsed. */}
          <AgentSummary
            text={typeof entry.data.agent_summary === "string" ? entry.data.agent_summary : null}
            className="mt-1"
          />
          {/* the trace opens on demand from here; owner or approver+ only (the
              backend enforces it, `canReadTrace` only decides whether to offer it). */}
          <BuildTraceEntry
            buildId={String(entry.data.id)}
            hasTrace={entry.data.has_trace}
            canRead={canReadTrace}
            status={entry.data.status}
          />
        </>
      );
    }
    case "audit_log":
      return (
        <>
          <em>감사 로그</em>: {String(entry.data.action)}
        </>
      );
    case "policy_verdict": {
      // 정책 게이트 판정(위험도 + 지적된 검사).
      const tier = String(entry.data.risk_tier);
      const checks = Array.isArray(entry.data.checks)
        ? (entry.data.checks as { check_name: string; result: string; summary: string }[])
        : [];
      const flagged = checks.filter((c) => c.result === "flag");
      return (
        <>
          <strong>정책 검사</strong>: 위험도{" "}
          <Badge tone={riskTierTone(tier)}>{riskTierLabel(tier)}</Badge>
          {flagged.length > 0 && (
            <Alert variant="warning" className="mt-1">
              지적된 검사: {policyCheckLabels(flagged.map((c) => c.check_name))}
            </Alert>
          )}
        </>
      );
    }
    case "approval_request": {
      // 승인 요청 상태 — 요청자가 프로젝트 타임라인에서 본인 요청의
      // 진행 상태를 확인하는 자리.
      const status = String(entry.data.status);
      return (
        <>
          <strong>승인 요청</strong>:{" "}
          <Badge tone={approvalStatusTone(status)}>{approvalStatusLabel(status)}</Badge>
          {isApprovalPending(status) && (
            <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
              ({Number(entry.data.approvals_so_far ?? 0)}/{Number(entry.data.required_approvals ?? 1)}{" "}
              승인)
            </span>
          )}
        </>
      );
    }
    case "deployment": {
      // 배포 이력 항목 — 상태 배지 + 실패 사유 + 롤백 표시
      // (a project's deployment history is inspectable).
      const failureReason = entry.data.failure_reason ? String(entry.data.failure_reason) : null;
      return (
        <>
          <strong>배포</strong> <StatusBadge status={String(entry.data.status)} />
          {entry.data.rollback_of != null && (
            <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">(롤백)</span>
          )}
          {failureReason && (
            // Same reasoning as the build entry above — a past
            // record, not a fresh action result.
            <Alert variant="error" className="mt-1">
              {deploymentFailureLine(failureReason)}
            </Alert>
          )}
          {failureReason && (
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
              {deploymentFailureNextStepLine(failureReason)}
            </p>
          )}
        </>
      );
    }
    default:
      return null;
  }
}

/**
 * `canReadTrace` is the caller's answer from the named predicate `canReadBuildTrace`
 * (owner or approver+). It defaults to false: a caller that does not say gets no
 * trace entry point.
 */
export function Timeline({ entries, canReadTrace = false }: { entries: TimelineEntry[]; canReadTrace?: boolean }) {
  if (entries.length === 0) {
    return <EmptyState tone="neutral" title="아직 활동이 없습니다" />;
  }

  return (
    <ul className="mt-2 list-none divide-y divide-slate-100 p-0 dark:divide-slate-800">
      {entries.map((entry, idx) => (
        <li key={idx} className="py-2 text-sm">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            {new Date(entry.ts).toLocaleString()}
          </div>
          <div className="mt-0.5">{renderEntry(entry, canReadTrace)}</div>
        </li>
      ))}
    </ul>
  );
}
