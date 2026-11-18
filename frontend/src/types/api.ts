export interface User {
  id: string;
  name: string;
  email: string;
  role: string;
  team_id: string | null;
  created_at: string;
}

// mirrors backend `DevIdentityOut`. Deliberately narrower than `User` —
// `GET /dev/identities` is the one endpoint that answers unauthenticated
// requests, so it exposes no email and no role
//.
export interface DevIdentity {
  id: string;
  name: string;
}

export interface Team {
  id: string;
  name: string;
}

export interface Template {
  id: string;
  name: string;
  stack: string;
  dockerfile_ref: string;
  deploy_config_ref: string;
  policy_profile: string;
  version: string;
  enabled: boolean;
}

export interface Project {
  id: string;
  name: string;
  owner_id: string;
  team_id: string;
  template_id: string;
  intent_spec: Record<string, unknown> | null;
  status: string;
  repo_url: string | null;
  container_id: string | null;
  confirmed_ref: string | null;
  created_at: string;
  last_active_at: string;
}

/**
 * Mirrors backend `DeployControlsOut`: what the project's deploy controls may offer right
 * now, computed by the backend with the functions the confirm and rollback actions run. The state
 * half only: who may act (owner or admin) stays `canOperateDeployment`. The `*_reason` values are
 * the backend's `DeployRefusalCode` (pinned as literals in `pages/deployControls.ts`).
 */
export interface DeployControls {
  /** The one build a confirm would accept; null with `confirm_blocked_reason` otherwise. */
  confirmable_build_id: string | null;
  confirm_blocked_reason: string | null;
  /** The SUPERSEDED deployment a rollback would redeploy; null with `rollback_blocked_reason`. */
  rollback_target_id: string | null;
  rollback_blocked_reason: string | null;
  /** What a stop would stop. */
  active_deployment_id: string | null;
  /** A build is QUEUED/RUNNING or a deployment is DEPLOYING: the state will change on its own. */
  in_flight: boolean;
}

export interface ProjectDetail extends Project {
  owner_name: string;
  team_name: string;
  template_name: string;
  /**
   * the URL a person opens for the running service: absolute (Traefik) or the relative dev
   * route `/api/projects/{id}/service/`. Null unless the project has an ACTIVE deployment.
   */
  service_url: string | null;
  /** Filled by `GET /projects/{id}` only; null on the create response. */
  deploy_controls: DeployControls | null;
}

export interface ChangeRequest {
  id: string;
  project_id: string | null;
  requester_id: string;
  nl_request: string;
  intent_delta: Record<string, unknown> | null;
  build_id: string | null;
  created_at: string;
}

export interface TimelineEntry {
  kind: "change_request" | "build" | "audit_log" | "policy_verdict" | "approval_request" | "deployment";
  ts: string;
  data: Record<string, unknown>;
}

// mirrors backend schemas/deployment.py (deploy/rollback UI is
// separate; the schema lands now so the frontend types stay in sync).
export interface Deployment {
  id: string;
  project_id: string;
  build_id: string;
  confirmed_by_id: string;
  rollback_of: string | null;
  status: string;
  failure_reason: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
}

// what the platform measured about a build's change (mirrors backend
// schemas/build.py). `path` is chosen by the agent: data, never markup.
export interface ChangedFile {
  path: string;
  change: string; // "added" | "modified" | "deleted"
}

export interface ChangedCounts {
  totals: Record<string, number>;
  truncated: boolean;
}

export interface ChangedFiles extends ChangedCounts {
  entries: ChangedFile[];
}

// what the platform saw when it called the preview's own parameterless GET routes
// (mirrors backend schemas/build.py `SmokeChecksOut`). `status` is null exactly when `outcome`
// is "no_answer". `path` passed a character filter on the backend and is still shown as text.
export type SmokeOutcome = "ok" | "client_error" | "server_error" | "no_answer" | "too_large";

export interface SmokeRoute {
  path: string;
  status: number | null;
  outcome: SmokeOutcome;
}

// The failing routes one repair attempt was started for (`attempt` is the 1-based repair attempt).
export interface RepairAttempt {
  attempt: number;
  routes: SmokeRoute[];
}

export interface SmokeChecks {
  routes: SmokeRoute[];
  skipped: number;
  truncated: boolean;
}

export interface PolicyCheckResult {
  id: string;
  check_name: string;
  result: string;
  summary: string;
  findings: Record<string, unknown>[] | null;
}

export interface PolicyVerdict {
  id: string;
  build_id: string;
  risk_tier: string;
  created_at: string;
  checks: PolicyCheckResult[];
}

export interface Build {
  id: string;
  project_id: string;
  type: string;
  agent_session_id: string | null;
  // measured at claim; `model` is the configured one (the trace's per-call model is the wire value). null on older builds.
  engine: string | null;
  model: string | null;
  status: string;
  diff_ref: string | null;
  cost_tokens: number | null;
  cost_usd: number | null;
  started_at: string | null;
  ended_at: string | null;
  heartbeat_at: string | null;
  failure_reason: string | null;
  // the platform-authored sentence that goes with failure_reason; null unless FAILED (and for older failed rows).
  failure_detail: string | null;
  image_ref: string | null;
  preview_url: string | null;
  // platform-authored code for a preview that failed to start (null = it did not fail).
  preview_start_failure?: string | null;
  preview_expires_at: string | null;
  created_at: string;
  // In-build repair attempts started (0 = none ran).
  repair_attempts: number;
  // as on ApprovalRequest; null = none recorded.
  repair_routes?: RepairAttempt[] | null;
  // null = not measured (distinct from a measured build with no changes).
  changed_files: ChangedFiles | null;
  protected_path_drift: string[] | null;
  // the agent's own text, unverified; only ever on a SUCCEEDED build, null otherwise.
  agent_summary: string | null;
  policy_verdict: PolicyVerdict | null;
  deploy_eligible: boolean;
}

// mirrors backend schemas/approval.py.
export interface ApprovalDecision {
  id: string;
  actor_id: string;
  decision: string;
  comment: string | null;
  created_at: string;
}

export interface ApprovalRequest {
  id: string;
  build_id: string;
  project_id: string;
  requester_id: string;
  // the project's owner, barred from deciding like the requester (null if the project is gone).
  project_owner_id: string | null;
  policy_verdict_id: string | null;
  risk_tier: string;
  status: string;
  required_approvals: number;
  superseded_by_build_id: string | null;
  superseded_at: string | null;
  created_at: string;
  decisions: ApprovalDecision[];
  policy_verdict: PolicyVerdict | null;
  // the request text and what the platform measured (null = not measured / not found).
  request_text: string | null;
  changed_files: ChangedFiles | null;
  protected_path_drift: string[] | null;
  // the preview smoke check (null = not measured, which is not the same as no routes).
  smoke_checks: SmokeChecks | null;
  // why the preview did not start; a different fact from a smoke check that was not measured.
  preview_start_failure?: string | null;
  // repair attempts the build started (0 = none ran; null = the build is gone). Detail only, not on the list.
  repair_attempts?: number | null;
  // the routes each repair attempt was started for; null = none recorded (no repair, a build from before the column, or a malformed stored value), never "no failing routes".
  repair_routes?: RepairAttempt[] | null;
  // the agent's own text, unverified (null = none).
  agent_summary: string | null;
}

export interface ApprovalRequestSummary {
  id: string;
  build_id: string;
  project_id: string;
  project_name: string;
  requester_id: string;
  requester_name: string;
  risk_tier: string;
  status: string;
  required_approvals: number;
  approvals_so_far: number;
  flagged_checks: string[];
  diff_ref: string | null;
  preview_url: string | null;
  preview_start_failure?: string | null;
  changed_counts: ChangedCounts | null; // counts only in the list
  created_at: string;
}

export interface ApprovalDecisionInput {
  comment?: string | null;
}

export interface ProjectCreateInput {
  name: string;
  template_id: string;
  team_id: string;
  nl_request: string;
}

export interface ChangeRequestCreateInput {
  nl_request: string;
}

// input for `PATCH /users/{id}/role` (mirrors `schemas/user.UserRoleUpdate`).
// The value is one of `auth/roles.Role`'s three constants — the console offers a
// fixed select, never free-form entry.
export interface UserRoleUpdateInput {
  role: string;
}

// response of `GET /users/usable-admin-count` (mirrors `schemas/user.UsableAdminCountOut`).
export interface UsableAdminCount {
  count: number;
}

// --- usage / quotas (mirrors backend `schemas/usage.py`) ---------------

// Every usage response carries this: the usage read API
// requires a figure to state the month and timezone it was computed for, so the
// console never prints a number without its window.
export interface MonthWindow {
  month: string; // "YYYY-MM"
  timezone: string;
  starts_at: string;
  ends_at: string; // exclusive
}

export interface SubjectUsage {
  kind: string;
  // `null` **only** on the unattributed line: a build
  // with no resolvable requester. It is a real line, not a missing subject.
  subject_id: string | null;
  spend_usd: number;
  build_count: number;
  // Builds whose `cost_usd` is NULL — not measured, which is distinct from a
  // measured 0.0. This field is the only thing that keeps unmeasured spend from
  // rendering as `$0.00`.
  unmeasured_build_count: number;
  effective_limit_usd: number | null; // null only on the unattributed line
  limit_source: string | null; // "override" | "default"
  in_breach: boolean; // a derived read-time comparison. Warns; blocks nothing.
}

export interface UsageReport {
  window: MonthWindow;
  subjects: SubjectUsage[];
}

export interface PlatformUsage {
  window: MonthWindow;
  total_spend_usd: number;
  unattributed_spend_usd: number;
  build_count: number;
  unmeasured_build_count: number;
  default_limit_usd: number;
  projects_in_breach: string[];
  users_in_breach: string[];
}

export interface Quota {
  subject_kind: string;
  subject_id: string;
  monthly_quota_usd: number | null; // the stored override; null = none set
  effective_limit_usd: number; // what it resolves to right now
  limit_source: string;
}

// `null` **clears** the override back to the global default — which is not
// unlimited. `0.0` is a legal override meaning "any spend
// is a breach"; negatives are rejected by both sides.
export interface QuotaUpdateInput {
  monthly_quota_usd: number | null;
}

// --- cost dashboard (mirrors backend `schemas/usage.py`) ----------------

// One calendar day in the platform timezone. `failed_spend_usd` is the part of
// `spend_usd` that came from FAILED builds. A day with `build_count: 0` is a day
// with no builds, not a measured zero.
export interface DailyBucket {
  day: string; // "YYYY-MM-DD"
  spend_usd: number;
  build_count: number;
  unmeasured_build_count: number;
  failed_spend_usd: number;
}

export interface FailureReasonBreakdown {
  failure_reason: string | null; // null: FAILED with no recorded reason
  spend_usd: number;
  build_count: number;
  unmeasured_build_count: number;
}

export interface OutcomeBreakdown {
  status: string;
  spend_usd: number;
  build_count: number;
  unmeasured_build_count: number;
  failure_reasons: FailureReasonBreakdown[]; // empty unless FAILED
}

// `cost_usd` is never null here: an unmeasured build is not listed.
export interface ExpensiveBuild {
  build_id: string;
  project_id: string;
  type: string;
  status: string;
  failure_reason: string | null;
  cost_usd: number;
  cost_tokens: number | null;
  ended_at: string;
  requester_id: string | null; // null: unattributed
}

export interface MeteringCaveat {
  date: string; // "YYYY-MM-DD" -- a backend constant, not a setting
  window_starts_before: boolean;
}

export interface CostDashboard {
  window: MonthWindow;
  limit: number;
  days: DailyBucket[];
  outcomes: OutcomeBreakdown[];
  expensive_builds: ExpensiveBuild[];
  metering_caveat: MeteringCaveat;
}

// --- console reads (mirrors backend `schemas/console.py`) --------------

export interface ProjectDeploymentHealth {
  project_id: string;
  project_name: string;
  // Both slots, deliberately: `active_deployment === null` covers never-deployed,
  // deploying-now, stopped, superseded and failed, and collapsing them would
  // render every in-progress deploy as "not deployed" (schemas/console.py).
  active_deployment: Deployment | null;
  latest_deployment: Deployment | null;
}

export interface DeploymentHealth {
  projects: ProjectDeploymentHealth[]; // every project, including never-deployed ones
  recent_failures: Deployment[]; // newest first, bounded
  recent_failures_limit: number;
  recent_failures_truncated: boolean; // older failures exist beyond the bound
}

export interface PlatformConfigurationValues {
  policy_medium_tier_checks: string[];
  policy_high_tier_checks: string[];
  policy_medium_approvals_required: number;
  policy_high_approvals_required: number;
  build_token_budget: number;
  build_cost_budget_usd: number;
  sandbox_memory_limit_mb: number;
  sandbox_cpu_limit_cores: number;
  sandbox_pids_limit: number;
  build_max_duration_minutes: number;
  preview_ttl_minutes: number;
  quota_monthly_usd_default: number;
  platform_timezone: string;
  // this type had gone two fields behind the backend allowlist
  // since the sizing settings landed (never rendered by
  // `ConfigurationPanel.tsx`, so `tsc` had nothing to catch the gap) —
  // closed here as a carried-forward observation, not new scope.
  service_data_expected_services: number;
  service_data_connections_per_service: number;
  // the idle-project threshold,
  // displayed read-only.
  project_lifecycle_idle_threshold_days: number;
  // true when the idle sweep leaves a project with an
  // ACTIVE deployment alone altogether (no flag, no stop, no archive) because no traffic signal
  // exists where Traefik routes. Derived on the backend; shown beside the threshold.
  project_lifecycle_live_services_exempt: boolean;
  // the Traefik traffic signal's runtime state. Both null while the signal
  // is unconfigured or has never polled (ISO timestamps otherwise).
  // Traefik routes and TRAEFIK_METRICS_PORT is set. Null timestamps alone cannot tell
  // "configured, never read" from "not configured".
  traffic_signal_configured: boolean;
  traffic_signal_observed_since: string | null;
  traffic_signal_last_poll_at: string | null;
  // how often the worker measures host disk, and the two thresholds.
  host_disk_check_interval_seconds: number;
  host_disk_min_free_percent: number;
  host_disk_min_free_gb: number;
}

// Backend schemas/console.py HostHealthOut, `GET /console/health`, admin only.
export interface WorkerLeaseHealth {
  state: string; // NONE | RELEASED | HELD | STALE
  heartbeat_age_seconds: number | null;
  stale_after_seconds: number;
}

export interface DiskCheck {
  label: string; // docker_data_root | database_dir | local_git_root | backup_dir
  path: string | null;
  free_bytes: number | null;
  total_bytes: number | null;
  free_percent: number | null;
  state: string; // OK | BELOW_THRESHOLD | UNKNOWN
  checked_at: string;
}

// Backend schemas/console.py ServiceHealthOut: one ACTIVE service's latest check.
export interface ServiceHealth {
  deployment_id: string;
  project_id: string;
  project_name: string;
  state: string; // HEALTHY | UNHEALTHY | UNCHECKED
  last_result: string | null; // PASS | FAIL | UNKNOWN; null while UNCHECKED
  consecutive_failures: number;
  last_failure_kind: string | null; // CONTAINER_MISSING | NOT_RUNNING | RESTARTED | HEALTHZ_FAILED | HEALTHZ_TIMEOUT
  last_failure_at: string | null;
  failing_since: string | null;
  last_checked_at: string | null;
  state_changed_at: string | null;
  stale: boolean; // last check older than the checker's stale window: not evidence of anything current
}

// Backend ServiceCheckerOut: is the worker's service checker running.
export interface ServiceChecker {
  state: string; // NONE | ALIVE | STALE
  last_pass_started_at: string | null;
  last_pass_finished_at: string | null;
  interval_seconds: number;
  stale_after_seconds: number;
}

// Backend NotificationChannelOut: one operator-notification channel. `last_error_class` is an
// exception class name; the backend never sends a message, URL or response body.
export interface NotificationChannel {
  channel: string; // webhook | email
  configured: boolean;
  last_attempt_at: string | null;
  last_result: string | null; // OK | FAILED; null before any attempt
  last_error_class: string | null;
}

// Backend routers/notifications.py: `POST /platform/notifications/test`.
export interface NotificationTestChannelResult {
  channel: string;
  ok: boolean;
  error_class: string | null;
}

export interface NotificationTestResult {
  results: NotificationTestChannelResult[];
}

export interface HostHealth {
  worker: WorkerLeaseHealth;
  disk_min_free_percent: number;
  disk_min_free_gb: number;
  disks: DiskCheck[];
  service_failure_threshold: number;
  services: ServiceHealth[];
  service_checker: ServiceChecker | null;
  notification_channels: NotificationChannel[];
  notification_test_min_interval_seconds: number;
}

export interface PlatformConfiguration {
  source: string; // "environment"
  editable: boolean; // constant false
  change_requires_restart: boolean; // constant true
  values: PlatformConfigurationValues;
}

// --- local accounts (mirrors backend schemas/local_accounts.py) -------
// Only reachable when the backend is in `local` mode (ADR 0022).

export interface SignupCreateInput {
  email: string;
  name: string;
  department: string;
  password: string;
}

// The one shape `POST /auth/signup` ever returns, whether or not the email
// already existed (the response is identical
// either way, so there is nothing here that could vary by outcome).
export interface SignupAck {
  detail: string;
}

export interface LocalLoginInput {
  email: string;
  password: string;
}

// `GET /admin/signup-requests`' row shape (schemas/local_accounts.
// PendingSignupOut). `department` is nullable on the wire type even though
// signup requires it non-empty — it mirrors `User.department`'s column
// nullability, not the signup form's own validation.
export interface PendingSignup {
  id: string;
  email: string;
  name: string;
  department: string | null;
  requested_at: string;
  status: string;
}

export interface SignupApproveInput {
  role?: string | null;
}

export interface SignupRejectInput {
  reason?: string | null;
}

// mirrors backend schemas/build_trace.py (`GET /api/builds/{id}/trace`).
// `measured` is what the platform observed or computed; `claims` is what the agent (or the
// sandbox on its behalf) said. Every string in either is agent-influenced data.
export interface TraceThinking {
  chars: number;
}

export interface TraceStepMeasured {
  ordinal: number;
  started_at: string | null;
  ended_at: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  tokens: number | null;
  cost_usd: number | null;
  stop_reason: string | null;
  /** "recorded" | "unparsed" | "dropped" */
  content: string;
  thinking: TraceThinking[];
}

export interface TraceCallsMeasured {
  /** "captured" | "unavailable" */
  state: string;
  steps: TraceStepMeasured[];
  calls_total: number;
  content_truncated: boolean;
  omitted_calls: number;
  omitted_tokens: number;
  omitted_cost_usd: number;
}

export interface TraceDiffFile {
  path: string;
  /** "added" | "modified" | "deleted" */
  change: string;
  /** "text" | "binary" | "too_large" | "total_limit" */
  content: string;
  diff: string | null;
  truncated: boolean;
}

export interface TraceDiff {
  /** "computed" | "not_reached" | "unavailable" */
  state: string;
  files: TraceDiffFile[];
  files_total: number;
  truncated: boolean;
}

export interface TraceBlockClaim {
  /** "assistant_text" | "tool_use" | "tool_result" | "other" */
  kind: string;
  text?: string | null;
  chars?: number | null;
  truncated?: boolean;
  id?: string | null;
  name?: string | null;
  tool_use_id?: string | null;
  is_error?: boolean | null;
  type?: string | null;
}

export interface TraceStepClaims {
  ordinal: number;
  blocks: TraceBlockClaim[];
}

export interface BuildTrace {
  build_id: string;
  captured_at: string;
  measured: { calls: TraceCallsMeasured; diff: TraceDiff };
  claims: { calls: TraceStepClaims[]; log: { text: string | null; truncated: boolean } };
}

// ADR 0035 §Decision 5: the admin's agent engine selection (`routers/agent_engine.py`).
// `available` is the only fact about a key the API states; there is no key field.
export interface AgentEngineStatus {
  engine: string;
  available: boolean;
  model: string | null; // the configured model; null for an engine that runs its own default
  image: string;
  selected: boolean;
}

export interface AgentEngineSelection {
  engines: AgentEngineStatus[];
  selected: string;
  source: string; // "stored" (an admin's choice) | "default" (nobody has chosen yet)
  set_by_id: string | null;
  set_by_name: string | null;
  set_at: string | null;
}

export interface AgentEngineUpdateInput {
  engine: string;
}

// Backend `routers/evals.py`: an eval run and its cases. Admin-only endpoints.
// `check_outcomes` carries the platform's own structured outcome per request, never a response body.
export interface EvalCheckOutcome {
  index: number | null; // null for the /healthz request that precedes the checks
  method: string;
  path: string;
  expected_status: number;
  status: number | null; // the actual status; null when the request could not be made
  ok: boolean;
  outcome: string; // platform-authored English; the console words the outcome itself
}

export interface EvalCase {
  id: string;
  case_id: string;
  position: number;
  state: string; // PENDING | RUNNING | DONE
  result: string | null; // PASSED | FAILED | NOT_RUN_BUDGET, once DONE
  failure_reason: string | null;
  failed_check_index: number | null;
  check_outcomes: EvalCheckOutcome[] | null;
  project_id: string | null;
  build_id: string | null;
  policy_risk_tier: string | null;
  policy_flagged_checks: string[];
  cost_usd: number | null; // metered by the proxy; null = not measured
  cost_tokens: number | null;
  // repair attempts the case's build ran; null = not measured (never run, pending, or from before the column), never 0.
  repair_attempts: number | null;
  duration_seconds: number | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface EvalRun {
  id: string;
  status: string; // RUNNING | COMPLETED | STOPPED_BUDGET | FAILED
  engine: string;
  model: string | null;
  request_set_version: number;
  prompt_fingerprint: string | null; // first 12 hex of SHA-256 of the create-build system prompt; null = not recorded
  repair_max_attempts: number | null; // BUILD_REPAIR_MAX_ATTEMPTS the run recorded; null = not recorded (never 0)
  budget_usd: number;
  spend_usd: number;
  started_by_id: string;
  started_by_name: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  detail: string | null;
  total: number;
  passed: number;
  failed: number;
  not_run: number;
}

export interface EvalRunDetail extends EvalRun {
  cases: EvalCase[];
}

// what the start control needs before any run exists.
// An allowlist: availability is a boolean, and there is no key field.
export interface EvalStatus {
  budget_usd: number; // the effective EVAL_RUN_BUDGET_USD
  case_min_budget_usd: number;
  executor_active: boolean; // USE_SANDBOX_EXECUTOR plus at least one available engine
  engine: string | null; // the selected engine
  model: string | null;
  engine_available: boolean;
  run_in_progress: boolean;
  request_set_version: number | null; // null when the set cannot be loaded
  case_count: number | null;
}
