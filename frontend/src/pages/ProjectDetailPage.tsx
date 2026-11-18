import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { deploymentsApi } from "../api/deployments";
import { projectsApi } from "../api/projects";
import {
  NL_REQUEST_MAX_CHARS,
  isAnyOverLimit,
  lengthError,
  lengthHint,
  lengthNotice,
  tooLongMessage,
} from "../api/requestLimits";
import { canOperateDeployment, canReadBuildTrace } from "../auth/roles";
import { deploymentFailureLine, deploymentFailureNextStepLine } from "../components/deploymentFailure";
import { StatusBadge } from "../components/StatusBadge";
import { Timeline } from "../components/Timeline";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  Panel,
  Skeleton,
  StatTile,
  TabPanel,
  Tabs,
  Textarea,
  linkClassName,
  tabElementId,
} from "../components/ui";
import { visibleControls } from "../components/ui/visibleControls";
import { usePageTitle } from "../components/pageTitle";
import { useIdentity } from "../context/IdentityContext";
import { useLoaderOnChange, usePolling } from "../loading/useLoader";
import type { Deployment, ProjectDetail, TimelineEntry } from "../types/api";
import { canSubmitChangeRequest, changeRequestBlockedMessage, noConfirmedRefMessage } from "./changeRequest";
import {
  PROJECT_POLL_INTERVAL_MS,
  canOfferConfirm,
  canOfferRollback,
  canOfferStop,
  confirmBlockedMessage,
  confirmTargetBuildId,
  deployActionFailure,
  deployFailureAfter,
  offersAnyDeployControl,
  shouldPollProject,
  shouldRefreshAfterDeployFailure,
  type DeployAction,
  type DeployFailure,
} from "./deployControls";
import {
  DEFAULT_PROJECT_TAB,
  PROJECT_TABS,
  deploymentCountLabel,
  lastDeploymentLabel,
  tabAfterChangeRequest,
  type ProjectTabId,
} from "./projectDetailView";
import { OPEN_SERVICE_LABEL, serviceLinkHref } from "./serviceLink";
import {
  UNARCHIVED_NOTICE,
  UNARCHIVE_CONFIRMATION,
  canUnarchiveProject,
  failureAfterControlWithdrawn,
  isProjectArchived,
  shouldDismissConfirmation,
  shouldRefreshAfterUnarchiveFailure,
  unarchiveFailureMessage,
  type UnarchiveFailure,
} from "./unarchive";

/**
 * What this page says when the change-request form fails.
 *
 * The previous per-call-site logic — `err instanceof ApiError ? err.message
 * || fallback : err instanceof Error ? err.message : fallback` — read like it
 * distinguished a real failure from a network one, but `api/client.ts` always
 * throws `ApiError extends Error`, so `err.message` (the backend's `detail`)
 * fired on every rejection either way. The Korean fallback each call site
 * already wrote is now the only text rendered. Hoisted out of the component
 * body (it closes over nothing) so it has one identity across renders and,
 * per ADR 0021, is importable by a test without mounting anything.
 *
 * A 409 `NO_CONFIRMED_REF` (the project has no confirmed deployment, so a change request has
 * nothing to modify) is worded as the same reason the disabled form shows (`changeRequest.ts`).
 *
 * One failure gets its own Korean message: a 422 saying the change
 * request is over its length limit (a stale tab, another client). The input
 * does not cut the text; it keeps it, shows the count, and disables submit
 * while it is over the limit (`isChangeRequestSubmitBlocked`), so this message
 * is the backstop for a request that got past it. Anything else is still the
 * fallback.
 *
 * the deploy controls (confirm / rollback / stop) no longer use this;
 * their refusals are worded by `deployActionFailureMessage` (`deployControls.ts`),
 * which tells the coded ones apart. This still words the page's own load failure
 * and the change-request form.
 */
export function projectDetailFailureMessage(err: unknown, fallback: string): string {
  return noConfirmedRefMessage(err) ?? tooLongMessage(err) ?? fallback;
}

/** Submit is blocked while the change request is over its limit; the
 * text is never cut to fit. A named predicate so a `node`-env test reaches it. */
export function isChangeRequestSubmitBlocked(nlRequest: string): boolean {
  return isAnyOverLimit([{ text: nlRequest, max: NL_REQUEST_MAX_CHARS }]);
}

// `Button` does not forward a ref, so the focus targets after the un-archive confirmation are found
// by id (the precedent: `EvalRunsPanel`'s start control).
const UNARCHIVE_BUTTON_ID = "project-unarchive-button";
const UNARCHIVE_CONFIRM_TITLE_ID = "project-unarchive-confirm-title";
const PROJECT_STATUS_ID = "project-status-line";
const TAB_ID_BASE = "project-tabs";

const LOAD_FAILURE_FALLBACK = "프로젝트 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";

/** Everything the page shows, read together so one load is one consistent view. */
interface ProjectPageData {
  project: ProjectDetail;
  timeline: TimelineEntry[];
  deployments: Deployment[];
}

async function loadProjectPage(id: string, signal: AbortSignal): Promise<ProjectPageData> {
  const [project, timeline, deployments] = await Promise.all([
    projectsApi.get(id, { signal }),
    projectsApi.timeline(id, { signal }),
    deploymentsApi.list(id, { signal }),
  ]);
  return { project, timeline, deployments };
}

export function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { currentUser } = useIdentity();
  // One loader for the page: a newer load supersedes an older one, and an unmount aborts
  // it. A re-read passes `keepData`, so the project, the timeline and the form stay on screen.
  const page = useLoaderOnChange<ProjectPageData>(
    (signal) => loadProjectPage(id ?? "", signal),
    [id],
    (err) => projectDetailFailureMessage(err, LOAD_FAILURE_FALLBACK),
  );
  // The shell's page title (its h1) names the project once it is known, instead of the generic
  // section label. The page itself carries no second heading for the name.
  usePageTitle(page.data === null ? null : visibleControls(page.data.project.name));
  const [nlRequest, setNlRequest] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deployFailure, setDeployFailure] = useState<DeployFailure | null>(null);
  const [deployActing, setDeployActing] = useState(false);
  const [confirmingUnarchive, setConfirmingUnarchive] = useState(false);
  const [unarchiving, setUnarchiving] = useState(false);
  const [unarchiveError, setUnarchiveError] = useState<UnarchiveFailure | null>(null);
  const [unarchiveNotice, setUnarchiveNotice] = useState<string | null>(null);
  // Bumped when the confirmation is left (cancelled, or an un-archive that ended), so the effect
  // below moves focus once the render has settled which controls exist and are enabled.
  const [focusBack, setFocusBack] = useState(0);
  const unarchiveConfirmRef = useRef<HTMLDivElement | null>(null);
  // Which tab is open is the page's own state, so a re-read of the data does not reset it. The
  // counter asks the effect below to move focus onto the open tab after a submit switched it: the
  // submit button that had focus is gone with its tab.
  const [tab, setTab] = useState<ProjectTabId>(DEFAULT_PROJECT_TAB);
  const [tabFocusRequests, setTabFocusRequests] = useState(0);

  // What may be offered comes from the backend (`GET /projects/{id}`'s `deploy_controls`), computed
  // by the functions the actions run; nothing here re-derives a rule from the timeline.
  const controls = page.data?.project.deploy_controls ?? null;
  // Asynchronous transitions refresh themselves: poll while a build is QUEUED/RUNNING or a
  // deployment is DEPLOYING, and stop when none is. The reads are `background`: they yield to a
  // read the user just asked for, show no skeleton, and a failure of one is a quiet note, not an
  // alert. The next read is scheduled after the last settled (`loading/poller.ts`).
  const polling = shouldPollProject(controls);
  usePolling(polling, () => page.reload({ background: true }), PROJECT_POLL_INTERVAL_MS);

  // The un-archive control is the owner's or an admin's, and only on an archived project.
  // Computed before the early returns below, because the effects that follow it must run on every
  // render.
  const unarchiveOffered = canUnarchiveProject(currentUser, page.data?.project ?? null);
  const showUnarchiveConfirmation = unarchiveOffered && confirmingUnarchive;

  // The confirmation is for the project as it was when it was opened: when the control stops being
  // offered (a read shows it un-archived, or the viewer lost the right), it is closed for good, so a
  // later archive does not bring an old question back. A failed attempt's alert goes with it, except
  // the refusal that made the control go, which is the explanation of why it is gone.
  useEffect(() => {
    if (!unarchiveOffered) {
      setConfirmingUnarchive(false);
      setUnarchiveError((current) => failureAfterControlWithdrawn(current));
    }
  }, [unarchiveOffered]);

  // The deploy failure alert ends when a read shows the condition it reported is gone ("이미 진행
  // 중인 배포가 있습니다" once nothing is in flight; `deployFailureAfter`). Keyed on the data,
  // so it runs when a read has landed (a poll, a refresh, the re-read after a refusal) and never at
  // the moment the failure is set: a stale page that has not yet heard of the deployment would
  // otherwise clear its own refusal.
  const pageData = page.data;
  useEffect(() => {
    if (pageData === null) return;
    const { deploy_controls: readControls } = pageData.project;
    const archived = isProjectArchived(pageData.project);
    setDeployFailure((current) => deployFailureAfter(current, { kind: "read", controls: readControls, archived }));
  }, [pageData]);

  useEffect(() => {
    if (showUnarchiveConfirmation) unarchiveConfirmRef.current?.focus();
  }, [showUnarchiveConfirmation]);

  // Focus goes back when the confirmation is left. It runs after the render because the trigger is
  // disabled while the confirmation is open, and a disabled button cannot take focus. When the
  // outcome removed the trigger (the project is no longer archived), the status line, which now
  // shows the result, takes focus instead of the page.
  useEffect(() => {
    if (focusBack === 0) return;
    const trigger = document.getElementById(UNARCHIVE_BUTTON_ID);
    if (trigger instanceof HTMLButtonElement && !trigger.disabled) trigger.focus();
    else document.getElementById(PROJECT_STATUS_ID)?.focus();
  }, [focusBack]);

  useEffect(() => {
    if (tabFocusRequests === 0) return;
    document.getElementById(tabElementId(TAB_ID_BASE, tab))?.focus();
    // Only a request moves focus; a later tab change by the reader must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabFocusRequests]);

  /** The reader pressed `새로 고침`: the alerts about earlier attempts end with it. */
  function refresh() {
    setUnarchiveError(null);
    setDeployFailure((current) => deployFailureAfter(current, { kind: "refresh" }));
    void page.reload({ keepData: true });
  }

  async function handleSubmitChangeRequest(e: React.FormEvent) {
    e.preventDefault();
    if (!id) return;
    if (isChangeRequestSubmitBlocked(nlRequest)) return;
    if (!canSubmitChangeRequest(page.data?.project ?? null)) return;
    setError(null);
    setUnarchiveError(null);
    setDeployFailure((current) => deployFailureAfter(current, { kind: "change_request" }));
    setSubmitting(true);
    try {
      await projectsApi.submitChangeRequest(id, { nl_request: nlRequest });
      setNlRequest("");
      await page.reload({ keepData: true });
      // The new entry is on the timeline: take the reader there.
      setTab((current) => tabAfterChangeRequest(current, "submitted"));
      setTabFocusRequests((n) => n + 1);
    } catch (err) {
      setError(projectDetailFailureMessage(err, "변경 요청 제출에 실패했습니다."));
      setTab((current) => tabAfterChangeRequest(current, "failed"));
    } finally {
      setSubmitting(false);
    }
  }

  /** One confirm / rollback / stop: the call, then a re-read; a refusal that says the page is stale
   * (400/409) re-reads too, so the control that was just refused does not stay on screen. */
  async function runDeployAction(action: DeployAction, perform: () => Promise<unknown>) {
    setDeployFailure(null);
    setUnarchiveError(null);
    setUnarchiveNotice(null);
    setDeployActing(true);
    try {
      await perform();
      await page.reload({ keepData: true });
    } catch (err) {
      setDeployFailure(deployActionFailure(err, action));
      // Not `refresh()`: that is the reader's own button and would clear the failure just set.
      if (shouldRefreshAfterDeployFailure(err)) void page.reload({ keepData: true });
    } finally {
      setDeployActing(false);
    }
  }

  function cancelUnarchive() {
    setConfirmingUnarchive(false);
    setFocusBack((n) => n + 1);
  }

  /** Un-archive, then re-read so `deploy_controls` reflects the new state. The confirmation stays
   * open (busy) until the re-read has landed, then closes and focus goes back. A refusal that says
   * the page is stale (400/403) re-reads too. */
  async function handleUnarchive() {
    if (!id || unarchiving || !unarchiveOffered) return;
    setUnarchiveError(null);
    setUnarchiveNotice(null);
    setDeployFailure(null);
    setUnarchiving(true);
    try {
      await projectsApi.unarchive(id);
      setUnarchiveNotice(UNARCHIVED_NOTICE);
      await page.reload({ keepData: true });
    } catch (err) {
      const explainsWithdrawal = shouldRefreshAfterUnarchiveFailure(err);
      setUnarchiveError({ message: unarchiveFailureMessage(err), explainsWithdrawal });
      if (explainsWithdrawal) await page.reload({ keepData: true });
    } finally {
      setUnarchiving(false);
      setConfirmingUnarchive(false);
      setFocusBack((n) => n + 1);
    }
  }

  function handleConfirmDeploy() {
    const buildId = confirmTargetBuildId(controls);
    if (!id || buildId === null) return;
    if (!window.confirm("이 빌드를 운영 환경에 배포하시겠습니까?")) return;
    void runDeployAction("confirm", () => deploymentsApi.confirm(id, buildId));
  }

  function handleRollback() {
    if (!id) return;
    if (!window.confirm("이전 배포로 롤백하시겠습니까?")) return;
    void runDeployAction("rollback", () => deploymentsApi.rollback(id));
  }

  function handleStop() {
    if (!id) return;
    if (!window.confirm("운영 중인 서비스를 중지하시겠습니까?")) return;
    void runDeployAction("stop", () => deploymentsApi.stop(id));
  }

  const data = page.data;
  if (data === null) {
    if (page.loading) {
      return <Skeleton label="프로젝트 정보를 불러오는 중" />;
    }
    // (c5) Never loaded: nothing to show but the failure, and a way to ask again.
    return (
      <EmptyState
        tone="neutral"
        title="프로젝트를 표시할 수 없습니다"
        description={page.error ?? LOAD_FAILURE_FALLBACK}
        action={
          <Button variant="secondary" onClick={refresh}>
            새로 고침
          </Button>
        }
      />
    );
  }

  const { project, timeline, deployments } = data;
  // Why the change-request form is disabled, or null when it is offered.
  const changeRequestBlocked = changeRequestBlockedMessage(project);
  // confirm/rollback/stop are `own`
  // or `admin`. Ownership and role are separate axes, so this is not a role
  // check. Presentation only — `routers/deployments._require_owner_or_admin`
  // still 403s, and `deployFailure` below still surfaces that.
  const canOperateDeploy = canOperateDeployment(currentUser, project);
  const confirmTargetId = confirmTargetBuildId(controls);
  const confirmBlocked = confirmBlockedMessage(controls);
  // the running service's URL, when there is one (null with no ACTIVE deployment).
  const serviceHref = serviceLinkHref(project);

  return (
    <div>
      {page.error && (
        // A later read failed while the last good project is still on screen. Announced only when
        // the reader asked for that read; a failed poll is a quiet note (a live region is an intent).
        <Alert variant="error" live={!page.errorIsBackground} className="mb-4">
          {page.error}
        </Alert>
      )}
      <p className="text-sm text-slate-600 dark:text-slate-300">
        소유자: {visibleControls(project.owner_name)} · 팀: {visibleControls(project.team_name)} · 템플릿:{" "}
        {visibleControls(project.template_name)}
        {serviceHref && (
          <>
            {" · "}
            <a href={serviceHref} target="_blank" rel="noopener noreferrer" className={linkClassName()}>
              {OPEN_SERVICE_LABEL}
            </a>
          </>
        )}
      </p>
      {/* What the page loaded says these three things and nothing more: the status, and the deployment
          records (the endpoint returns the whole history, so the count is the length of the list). */}
      <div className="mt-3 flex flex-wrap gap-3">
        {/* The status tile is the element that takes focus when the un-archive control is gone. */}
        <StatTile
          id={PROJECT_STATUS_ID}
          tabIndex={-1}
          size="compact"
          label="상태"
          value={<StatusBadge status={project.status} />}
          className="min-w-32 outline-none"
        />
        <StatTile size="compact" label="마지막 배포" value={lastDeploymentLabel(deployments)} className="min-w-32" />
        <StatTile size="compact" label="배포 횟수" value={deploymentCountLabel(deployments)} className="min-w-32" />
      </div>

      <Tabs idBase={TAB_ID_BASE} tabs={PROJECT_TABS} activeId={tab} onChange={setTab} className="mt-5" />

      <TabPanel idBase={TAB_ID_BASE} tabId={tab} className="mt-5">
        {tab === "deploy" && (
          <div className="grid items-start gap-5 md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
            <Panel title="배포 제어">
              <div className="mt-3 flex flex-wrap gap-2">
                {canOfferConfirm(controls) && (
                  <Button onClick={handleConfirmDeploy} busy={deployActing} disabled={!canOperateDeploy}>
                    {deployActing ? "처리 중..." : "배포 확정"}
                  </Button>
                )}
                {canOfferRollback(controls) && (
                  <Button variant="secondary" onClick={handleRollback} busy={deployActing} disabled={!canOperateDeploy}>
                    롤백
                  </Button>
                )}
                {canOfferStop(controls) && (
                  <Button variant="danger" onClick={handleStop} busy={deployActing} disabled={!canOperateDeploy}>
                    중지
                  </Button>
                )}
                {unarchiveOffered && (
                  <Button
                    id={UNARCHIVE_BUTTON_ID}
                    variant="secondary"
                    disabled={confirmingUnarchive || unarchiving}
                    onClick={() => setConfirmingUnarchive(true)}
                  >
                    보관 해제
                  </Button>
                )}
                <Button variant="secondary" onClick={refresh} busy={page.loading}>
                  새로 고침
                </Button>
              </div>
              {showUnarchiveConfirmation && (
                <Card className="mt-3 p-4">
                  <div
                    ref={unarchiveConfirmRef}
                    tabIndex={-1}
                    role="group"
                    aria-labelledby={UNARCHIVE_CONFIRM_TITLE_ID}
                    className="outline-none"
                    onKeyDown={(e) => {
                      if (shouldDismissConfirmation(e.key, unarchiving)) cancelUnarchive();
                    }}
                  >
                    <p id={UNARCHIVE_CONFIRM_TITLE_ID} className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      {UNARCHIVE_CONFIRMATION.title}
                    </p>
                    <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{UNARCHIVE_CONFIRMATION.does}</p>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{UNARCHIVE_CONFIRMATION.doesNot}</p>
                    <Alert variant="warning" className="mt-3">
                      {UNARCHIVE_CONFIRMATION.dataCondition}
                    </Alert>
                    <div className="mt-3 flex gap-2">
                      <Button variant="primary" busy={unarchiving} onClick={handleUnarchive}>
                        {unarchiving ? UNARCHIVE_CONFIRMATION.busy : UNARCHIVE_CONFIRMATION.confirm}
                      </Button>
                      <Button variant="secondary" disabled={unarchiving} onClick={cancelUnarchive}>
                        {UNARCHIVE_CONFIRMATION.cancel}
                      </Button>
                    </div>
                  </div>
                </Card>
              )}
              {confirmTargetId !== null && (
                <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">확정 대상 빌드 ID: {confirmTargetId}</p>
              )}
              {offersAnyDeployControl(controls) && !canOperateDeploy && (
                <Alert variant="warning" className="mt-3">
                  배포 확정 / 롤백 / 중지는 프로젝트 소유자 또는 admin 역할만 실행할 수 있습니다.
                </Alert>
              )}
              {canOperateDeploy && confirmBlocked !== null && (
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{confirmBlocked}</p>
              )}
              {polling && (
                <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                  진행 중인 빌드 또는 배포가 있어 {PROJECT_POLL_INTERVAL_MS / 1000}초마다 새로 읽습니다.
                </p>
              )}
              {deployFailure && (
                <Alert variant="error" live className="mt-3">
                  {deployFailure.message}
                </Alert>
              )}
              {unarchiveError && (
                <Alert variant="error" live className="mt-3">
                  {unarchiveError.message}
                </Alert>
              )}
              {/* A successful un-archive is the acting owner's own action: a polite status, not an alert.
                  The region is always mounted so that its text change is announced. */}
              <div role="status" className="mt-3 empty:hidden">
                {unarchiveNotice && <Alert variant="success">{unarchiveNotice}</Alert>}
              </div>
            </Panel>

            <Panel title="배포 이력">
              {deployments.length === 0 ? (
                <EmptyState tone="neutral" title="아직 배포 이력이 없습니다" className="mt-2" />
              ) : (
                <ul className="mt-2 divide-y divide-slate-100 dark:divide-slate-800">
                  {deployments.map((d) => (
                    <li key={d.id} className="py-2 text-sm">
                      <StatusBadge status={d.status} />{" "}
                      <span className="text-slate-500 dark:text-slate-400">
                        {new Date(d.created_at).toLocaleString()}
                      </span>
                      {d.rollback_of && (
                        <span className="ml-2 text-slate-500 dark:text-slate-400">(롤백)</span>
                      )}
                      {d.failure_reason && (
                        // A recorded fact about a past deployment, not a
                        // fresh result of anything the user just did — no `live`,
                        // so it does not re-announce itself as if it just happened.
                        <Alert variant="error" className="mt-1">
                          {deploymentFailureLine(d.failure_reason)}
                        </Alert>
                      )}
                      {d.failure_reason && (
                        // What the requester can do next, beside the label.
                        <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                          {deploymentFailureNextStepLine(d.failure_reason)}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
        )}

        {tab === "change-request" && (
          <Panel title="변경 요청 보내기" className="max-w-[45rem]">
            {changeRequestBlocked !== null && (
              // A standing fact about the project, not the result of something the reader just did:
              // not a live region.
              <Alert variant="info" className="mt-2">
                {changeRequestBlocked}
              </Alert>
            )}
            <form onSubmit={handleSubmitChangeRequest} className="mt-2 flex items-end gap-2">
              <Field
                label="변경 요청"
                htmlFor="detail-nl-request"
                hint={lengthHint(nlRequest, NL_REQUEST_MAX_CHARS)}
                error={lengthError(nlRequest, NL_REQUEST_MAX_CHARS)}
                errorNotice={lengthNotice("변경 요청")}
                className="mb-0 min-w-0 flex-1"
              >
                <Textarea
                  id="detail-nl-request"
                  value={nlRequest}
                  onChange={(e) => setNlRequest(e.target.value)}
                  invalid={lengthError(nlRequest, NL_REQUEST_MAX_CHARS) !== undefined}
                  rows={3}
                  placeholder="예: 목록에 검색창 추가해줘"
                  disabled={changeRequestBlocked !== null}
                  required
                />
              </Field>
              <Button type="submit" busy={submitting} disabled={changeRequestBlocked !== null || isChangeRequestSubmitBlocked(nlRequest)}>
                {submitting ? "제출 중..." : "제출"}
              </Button>
            </form>
            {error && (
              <Alert variant="error" live className="mt-3">
                {error}
              </Alert>
            )}
          </Panel>
        )}

        {tab === "timeline" && (
          <Panel title="타임라인">
            <div className="mt-3">
              <Timeline entries={timeline} canReadTrace={canReadBuildTrace(currentUser, project)} />
            </div>
          </Panel>
        )}
      </TabPanel>
    </div>
  );
}
