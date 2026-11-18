import type { ReactNode } from "react";

import { Badge, Code, Panel } from "./ui";
import {
  agentSummaryView,
  CHANGED_NOT_MEASURED_LINE,
  changedFilesView,
  driftView,
  findingsView,
  requestTextLine,
  smokeChecksView,
} from "./measuredFactsLines";
import {
  asPreviewStartFailure,
  previewStartFailureLabel,
  previewStartFailureNextStep,
} from "./previewStartFailure";
import { policyCheckLabelWithId } from "./policyCheckDisplay";
import { REPAIR_NOTE_HEADING, repairApprovalView, repairTimelineLine } from "./repairAttempts";
import type { ChangedFiles, PolicyVerdict, SmokeChecks } from "../types/api";

// The sections of the approval detail that show what was ASKED and what the
// platform MEASURED. Every agent-chosen string below (a path, a finding's path and values)
// is React text inside a `Code` element: escaped by React, never a link, never markup, and
// bounded by the functions in `measuredFactsLines.ts`. None of these sections is a live region:
// they are the page's content, not the result of something the viewer just did.

const SECTION_HEADING = "mt-4 text-sm font-semibold text-slate-700 dark:text-slate-200";
const MUTED = "text-xs text-slate-500 dark:text-slate-400";

/**
 * One titled section. Unframed (the default) it is the plain `h4` heading the project timeline and the
 * unit tests render. `framed` (the approval detail's tabs) makes it its own bordered card with an
 * accent-bar heading, the project's `Panel` at level 3 (the pane's own heading is level 3 too, so the
 * outline skips nothing). The content is the same either way.
 */
function Section({ title, framed, children }: { title: string; framed?: boolean; children: ReactNode }) {
  if (framed) {
    return (
      <Panel title={title} headingLevel={3} compact>
        {children}
      </Panel>
    );
  }
  return (
    <>
      <h4 className={SECTION_HEADING}>{title}</h4>
      {children}
    </>
  );
}

/** The drift line: `보호 경로 원복: Dockerfile, compose.yaml`, the paths literal. */
export function DriftLine({ drift, className = "mt-2" }: { drift: string[] | null | undefined; className?: string }) {
  const view = driftView(drift);
  if (!view) return null;
  return (
    <p className={`${className} text-sm text-slate-700 dark:text-slate-200`}>
      {view.label}:{" "}
      {view.paths.map((path, i) => (
        <span key={i}>
          {i > 0 && ", "}
          <Code>{path}</Code>
        </span>
      ))}
    </p>
  );
}

/** What the requester asked for. User-authored text, shown as plain pre-wrapped text. */
export function RequestTextSection({ text, framed }: { text: string | null | undefined; framed?: boolean }) {
  return (
    <Section title="요청 내용" framed={framed}>
      {text ? (
        <p className="mt-2 whitespace-pre-wrap break-words text-sm text-slate-800 dark:text-slate-100">{requestTextLine(text)}</p>
      ) : (
        <p className={`mt-2 ${MUTED}`}>이 빌드의 요청 내용을 찾을 수 없습니다.</p>
      )}
    </Section>
  );
}

/**
 * The changed files and the protected-path drift. The heading, the counts line and the
 * drift line are measured facts and always show, outside any disclosure. Below the counts line each
 * kind present is one native `<details>`, closed by default, whose summary carries the kind's label
 * and TRUE count; opening it lists that kind's paths (bounded, `changedFilesView` decides). Inert
 * markup: no link, no script, no live region; a path is React text inside `Code`.
 */
export function ChangedFilesSection({
  changed,
  drift,
  framed,
}: {
  changed: ChangedFiles | null | undefined;
  drift: string[] | null | undefined;
  framed?: boolean;
}) {
  const view = changedFilesView(changed);
  return (
    <Section title="변경된 파일" framed={framed}>
      {view ? (
        <>
          <p className="mt-2 text-sm text-slate-700 dark:text-slate-200">{view.countsLine}</p>
          {view.groups.map((group) => (
            <details key={group.kind} className="mt-2">
              <summary className="cursor-pointer text-sm text-slate-700 dark:text-slate-200">
                <Badge>{group.label}</Badge> {group.countText}
              </summary>
              {group.paths.length > 0 && (
                <ul className="mt-2 list-none space-y-1 p-0 text-sm">
                  {group.paths.map((path, i) => (
                    <li key={i}>
                      <Code>{path}</Code>
                    </li>
                  ))}
                </ul>
              )}
              {group.hiddenLine && <p className={`mt-1 ${MUTED}`}>{group.hiddenLine}</p>}
            </details>
          ))}
        </>
      ) : (
        <p className={`mt-2 ${MUTED}`}>{CHANGED_NOT_MEASURED_LINE}</p>
      )}
      <DriftLine drift={drift} />
    </Section>
  );
}

/**
 * a preview that failed to start, in words, with the operator's next step where the platform
 * knows one. Renders nothing when the preview did not fail. Not the smoke check's "not measured":
 * that section stays beside it and says what it always says. `compact` = one line, for the timeline.
 */
export function PreviewStartFailureNote({
  code,
  compact,
  framed,
}: {
  code: string | null | undefined;
  compact?: boolean;
  framed?: boolean;
}) {
  const failure = asPreviewStartFailure(code);
  if (failure === null) return null;
  const next = previewStartFailureNextStep(failure);
  if (compact) {
    return (
      <div className="mt-1 text-slate-600 dark:text-slate-300">
        {previewStartFailureLabel(failure)}
        {next ? ` · ${next}` : ""}
      </div>
    );
  }
  return (
    <Section title="프리뷰 시작" framed={framed}>
      <p className="mt-2 text-sm text-slate-700 dark:text-slate-200">{previewStartFailureLabel(failure)}</p>
      {next && <p className={`mt-1 ${MUTED}`}>{next}</p>}
    </Section>
  );
}

/**
 * The build's repair attempts. Renders nothing unless a repair ran (`repairAttempts.ts`
 * decides; the value may be a number, null, or untyped timeline data). It states what the platform did and
 * that the smoke check beside it is of the shipped attempt; it never says the repair worked. A stored
 * fact about a past build, so not a live region. `compact` = one line, for the timeline.
 *
 * The full note also lists, per repair attempt, the routes it was started for (`routes`, the build's
 * untyped `repair_routes`), only when the note itself shows and the stored value is well formed; a build
 * with a count and no routes says nothing about them. Each path is React text inside `Code` (never a
 * link, control characters shown as markers), the status and outcome are the smoke section's words, and
 * the list is labelled as what an EARLIER attempt failed on, not as the current state.
 */
export function RepairAttemptsNote({
  attempts,
  routes,
  compact,
  framed,
}: {
  attempts: unknown;
  routes?: unknown;
  compact?: boolean;
  framed?: boolean;
}) {
  if (compact) {
    const line = repairTimelineLine(attempts);
    if (line === null) return null;
    return <div className="mt-1 text-slate-600 dark:text-slate-300">{line}</div>;
  }
  const view = repairApprovalView(attempts, routes);
  if (view === null) return null;
  return (
    <Section title={REPAIR_NOTE_HEADING} framed={framed}>
      <p className="mt-2 text-sm text-slate-700 dark:text-slate-200">{view.note}</p>
      {view.routes && (
        <>
          {view.routes.attempts.map((attempt, i) => (
            <div key={i} className="mt-2">
              <p className={MUTED}>{attempt.label}</p>
              <ul className="mt-1 list-none space-y-1 p-0 text-sm">
                {attempt.routes.map((route, j) => (
                  <li key={j}>
                    GET <Code>{route.path}</Code>{" "}
                    <span className="text-slate-600 dark:text-slate-300">{route.statusText}</span>{" "}
                    <span className="text-slate-600 dark:text-slate-300">{route.outcomeLabel}</span>
                  </li>
                ))}
              </ul>
              {attempt.hiddenLine && <p className={`mt-1 ${MUTED}`}>{attempt.hiddenLine}</p>}
            </div>
          ))}
          {view.routes.hiddenLine && <p className={`mt-1 ${MUTED}`}>{view.routes.hiddenLine}</p>}
        </>
      )}
    </Section>
  );
}

/**
 * What the platform saw when it called the preview's own GET routes. Three states
 * (`smokeChecksView`): not measured, measured with nothing to call, measured with routes. A
 * failing row says so in words (the badge) and in weight, not by colour alone. The path is
 * React text inside `Code`: never a link. A stored fact about a past build, so not a live region.
 */
export function SmokeChecksSection({ smoke, framed }: { smoke: SmokeChecks | null | undefined; framed?: boolean }) {
  const view = smokeChecksView(smoke);
  return (
    <Section title="프리뷰 응답 점검" framed={framed}>
      <p className={view.measured ? "mt-2 text-sm text-slate-700 dark:text-slate-200" : `mt-2 ${MUTED}`}>
        {view.summary}
      </p>
      {view.notes.length > 0 && <p className={`mt-1 ${MUTED}`}>{view.notes.join(" · ")}</p>}
      {view.routes.length > 0 && (
        <ul className="mt-2 list-none space-y-1 p-0 text-sm">
          {view.routes.map((route, i) => (
            <li key={i}>
              <Code>{route.path}</Code> <span className="text-slate-600 dark:text-slate-300">{route.statusText}</span>{" "}
              <span className={route.failed ? "font-semibold text-slate-900 dark:text-slate-50" : "text-slate-600 dark:text-slate-300"}>
                {route.outcomeLabel}
              </span>
              {route.failed && (
                <>
                  {" "}
                  <Badge tone="danger">실패</Badge>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/**
 * Each policy check with its findings. The checks scan the WHOLE workspace, not the
 * build's diff, so the heading says so: a finding is not read as
 * "introduced by this build".
 */
export function PolicyChecksSection({ verdict, framed }: { verdict: PolicyVerdict | null | undefined; framed?: boolean }) {
  if (!verdict) return null;
  return (
    <Section title="정책 검사 (작업공간 전체 기준)" framed={framed}>
      <p className={`mt-1 ${MUTED}`}>검사는 이 빌드의 변경분이 아니라 작업공간 전체를 대상으로 합니다.</p>
      <ul className="mt-2 space-y-3 text-sm">
        {verdict.checks.map((check) => {
          const findings = findingsView(check.findings);
          return (
            <li key={check.id}>
              <strong className="font-medium text-slate-800 dark:text-slate-100">{policyCheckLabelWithId(check.check_name)}</strong>{" "}
              <Badge tone={check.result === "flag" ? "danger" : "success"}>
                {check.result === "flag" ? "지적됨" : "통과"}
              </Badge>
              <div className="text-slate-500 dark:text-slate-400">{check.summary}</div>
              {findings.items.length > 0 && (
                <ul className="mt-1 list-none space-y-1 p-0">
                  {findings.items.map((finding, i) => (
                    <li key={i} className="text-xs text-slate-700 dark:text-slate-200">
                      {finding.path !== null && <Code>{finding.path}</Code>}
                      {finding.fields.map((field, j) => (
                        <span key={j} className="ml-2">
                          {field.key}: <Code>{field.value}</Code>
                        </span>
                      ))}
                    </li>
                  ))}
                </ul>
              )}
              {findings.hiddenLine && <div className={MUTED}>{findings.hiddenLine}</div>}
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

/**
 * what the AGENT wrote about its own work, under the label that says the platform
 * did not check it. It comes after every measured fact. Plain pre-wrapped text inside a
 * native `<details>` that starts collapsed: React escapes it, nothing in it becomes a link
 * or markup, and invisible / direction-changing characters are already visible markers
 * (`agentSummaryView`). Not a live region: it is content, not the result of an action.
 * Renders nothing for no summary.
 */
export function AgentSummary({
  text,
  className,
  framed,
}: {
  text: string | null | undefined;
  className?: string;
  /** The agent's words in their own box, dashed and muted so they never read as a measured card. */
  framed?: boolean;
}) {
  const view = agentSummaryView(text);
  if (!view) return null;
  return (
    <details
      className={
        framed
          ? "rounded-card border border-dashed border-slate-300 bg-slate-50 p-3 dark:border-slate-600 dark:bg-slate-800/60"
          : (className ?? "mt-4")
      }
    >
      <summary className="cursor-pointer text-sm font-semibold text-slate-700 dark:text-slate-200">
        {view.label}
      </summary>
      <p className="mt-2 whitespace-pre-wrap break-words text-sm text-slate-700 dark:text-slate-200">
        {view.text}
      </p>
    </details>
  );
}
