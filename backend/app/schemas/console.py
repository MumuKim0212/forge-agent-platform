"""Deployment health, templates and policy
configuration.

The two console read shapes the console UI renders. Both are **closed by
construction**: every field is declared by hand here and every response is built
with explicit keyword arguments in `routers/console.py` — no model is ever handed
a whole ORM row or a whole `Settings` object. That is why nothing a later slice
adds to `Project`, `Deployment` or `Settings` can appear in a response without an
edit to this file.

`ConfigDict(from_attributes=True)` is deliberately **absent** from every model
below except where it is inherited (`DeploymentOut`, reused unchanged). These
shapes sit next to two secret-bearing objects — `Project.service_env_json` and
the `Settings` singleton, which holds every platform credential — and
`from_attributes` is the switch that turns "build this from named values" into
"build this from that object", which is one refactor away from serialising the
object itself.
"""


from pydantic import BaseModel

from app.schemas.deployment import DeploymentOut
from app.schemas.types import UtcDateTime


class ProjectDeploymentHealthOut(BaseModel):
    """One project's deployment state.

    **Both** deployment slots are here because `active_deployment is None` is
    four operationally different situations that an operator must be able to tell
    apart, and the active row alone cannot distinguish them:

    | `active` | `latest` | means |
    |---|---|---|
    | set | same row | serving |
    | `None` | `None` | never deployed |
    | `None` | `DEPLOYING` | deploying right now |
    | `None` | `FAILED` (+ reason) | the last attempt broke |
    | `None` | `STOPPED`/`SUPERSEDED` | deliberately stopped |

    Without `latest_deployment` a project would read as "not deployed" for the
    whole duration of every deploy, and a failed deploy would be invisible unless
    it happened to fall inside the bounded `recent_failures` list.

    No secret-bearing field is reachable from here. `project_id`/`project_name`
    are named individually rather than nesting `ProjectOut`, so
    `Project.service_env_json` has no path
    into this response even if a future edit made `ProjectOut` careless.
    """

    project_id: str
    project_name: str
    active_deployment: DeploymentOut | None
    latest_deployment: DeploymentOut | None


class DeploymentHealthOut(BaseModel):
    """Deployment state across every project, plus recent failures.

    `projects` is **not bounded**: it carries one entry per `Project` row,
    including projects that have never been deployed. A truncated project list
    would read as "this is every project" while quietly hiding some, which is the
    same failure mode the spec calls out for usage rollups.

    `recent_failures` **is** bounded, and says so on the response rather than in
    prose only: `recent_failures_limit` is how many entries the endpoint will
    return at most and `recent_failures_truncated` is whether older failures
    exist beyond it. A client must not present the list as complete when
    `recent_failures_truncated` is true. Newest first.

    Failure entries carry `project_id`, not a project name — `projects` above is
    the complete project list, so a client already holds the id -> name mapping
    and there is no second place for a project field to be widened.
    """

    projects: list[ProjectDeploymentHealthOut]
    recent_failures: list[DeploymentOut]
    recent_failures_limit: int
    recent_failures_truncated: bool


class PlatformConfigurationValuesOut(BaseModel):
    """**This model is the allowlist**: exposing configuration is an
    explicit allowlist, never a serialization.

    `Settings` holds operational values and every platform credential — git host
    token, OIDC client secret, session and preview signing keys, the LLM API key
    — in one object. So the exposed set is not "settings minus the secrets": it
    is exactly the fields spelled out below, each one also assigned by hand in
    `routers/console.get_platform_configuration`. Adding a field here is a
    deliberate two-line edit, which is the point of rule 2 and not friction to
    remove later.

    Secret-shaped settings are **absent**, not redacted and not reported as an
    "is set" boolean (rule 3): a placeholder or a boolean is an invitation to
    make the real value readable, and it also tells a reader something they have
    no operational use for. Configuration this view cannot show simply is not in
    it.

    Field names mirror their `Settings` counterparts so a reader can map a
    displayed value back to the environment variable that sets it. The two tier
    lists are the parsed `*_list` forms rather than the raw comma-separated
    strings, because a console renders check names, not a config file line.
    """

    # which failing checks push a build into
    # each tier. A flagged check in neither list is treated as high (fail
    # closed), which is why seeing both lists matters to an operator.
    policy_medium_tier_checks: list[str]
    policy_high_tier_checks: list[str]

    # required human approvals per tier.
    # "low" auto-approves regardless of these two.
    policy_medium_approvals_required: int
    policy_high_approvals_required: int

    # per-build LLM budgets enforced by
    # the proxy. Unrelated to `quota_monthly_usd_default` below: these fail a
    # build closed, that one only warns.
    build_token_budget: int
    build_cost_budget_usd: float

    # Resource and time limits (ADR 0003 invariant 4).
    sandbox_memory_limit_mb: int
    sandbox_cpu_limit_cores: float
    sandbox_pids_limit: int

    # Max build wall-clock duration before the worker sweep fails a stale build
    # with TIMEOUT.
    build_max_duration_minutes: int

    # ADR 0016 — minutes a preview container stays live.
    preview_ttl_minutes: int

    # the global monthly limit for a subject
    # with no override. Per-subject overrides are DB state and are served by
    # `routers/usage.py`, not from here: they are editable, and everything in
    # this view is not.
    quota_monthly_usd_default: float

    # the zone every rollup boundary
    # is evaluated in.
    platform_timezone: str

    # ADR 0023 §Amendment 10 "Sizing"— the two inputs the shared
    # service Postgres's `max_connections` is sized from, and what a prod
    # operator sizes their OWN instance's `max_connections` against. The
    # ONLY two fields this task adds: `service_postgres_admin_dsn` is
    # credential-shaped and stays out of this view entirely (rule 3) — a
    # test plants it as a sentinel and asserts it never reaches the
    # response.
    service_data_expected_services: int
    service_data_connections_per_service: int

    # threshold: "operator-configurable ... and displayed -- not edited --
    # on the admin console." This is that display. Only the threshold, not
    # `project_lifecycle_zombie_scan_enabled`/`_interval_hours` — those two
    # are scheduling mechanics an operator has no reason to read off this
    # view, unlike the idle window itself, which is the one number that
    # changes what "idle" means for every project on the platform.
    project_lifecycle_idle_threshold_days: int

    # true when
    # the idle sweep leaves a project with an ACTIVE deployment alone altogether (no flag, no
    # stop, no archive) because no traffic signal exists where Traefik routes. Derived from
    # `traefik_enabled` by the sweep's own predicate rather than copied from a setting, and
    # shown beside the threshold so nobody wonders why a long-idle live service is never
    # flagged. `traefik_enabled` itself stays off this view; only what it means here is shown.
    project_lifecycle_live_services_exempt: bool

    # when the Traefik traffic signal's current unbroken observation began,
    # and when it last polled successfully. Null while the signal is unconfigured (Traefik off,
    # `TRAEFIK_METRICS_PORT` unset) or has never polled. Read-only facts; no setting is shown.
    # Traefik routes AND `TRAEFIK_METRICS_PORT` is set (`signal_is_configured`). Tells a
    # configured signal that has not read yet (timestamps null) from one never configured.
    traffic_signal_configured: bool
    traffic_signal_observed_since: UtcDateTime | None
    traffic_signal_last_poll_at: UtcDateTime | None

    # how often the worker measures host disk, and the two thresholds
    # a location is judged against. The latest measurements are served by `GET /console/health`.
    host_disk_check_interval_seconds: float
    host_disk_min_free_percent: float
    host_disk_min_free_gb: float


class PlatformConfigurationOut(BaseModel):
    """The effective configuration, wrapped in the three facts that stop a
    client from presenting it as editable.

    `editable` is a constant `false` and `change_requires_restart` a constant
    `true`. That is deliberate rather than lazy: making any of these values
    editable would move platform configuration into the database and supersede
    the environment-only rule, so it has to be a deliberate change to this contract, not a value
    that quietly starts varying. A console that renders a disabled edit control
    with no explanation is worse than a plainly read-only display (rule 3) —
    `source` is what lets it explain *where* the change is made instead.
    """

    source: str  # where these values were read from
    editable: bool
    change_requires_restart: bool
    values: PlatformConfigurationValuesOut


class WorkerLeaseHealthOut(BaseModel):
    """the worker lease as the console shows it. `state` is NONE (never held), RELEASED
    (graceful stop), HELD (fresh heartbeat) or STALE (heartbeat older than `stale_after_seconds`).
    No holder host or pid: the console needs liveness, not where the process runs."""

    state: str
    heartbeat_age_seconds: float | None
    stale_after_seconds: float


class DiskCheckOut(BaseModel):
    """One host location's latest measurement. `state` is OK, BELOW_THRESHOLD or UNKNOWN; sizes are
    null exactly when UNKNOWN. `label` names the location, `path` is where it was measured."""

    label: str
    path: str | None
    free_bytes: int | None
    total_bytes: int | None
    free_percent: float | None
    state: str
    checked_at: UtcDateTime


class ServiceHealthOut(BaseModel):
    """one ACTIVE service's latest check. `state` is HEALTHY, UNHEALTHY, or UNCHECKED (no
    check has run for this deployment yet). `last_result` is PASS, FAIL or UNKNOWN (a check that
    could not be carried out) and null while UNCHECKED. `last_failure_kind` (one of
    `service_health.FailureKind`) is the most recent failure, kept after a recovery; `failing_since`
    is when the current failure streak began and is null while the last result is not a failure."""

    deployment_id: str
    project_id: str
    project_name: str
    state: str
    last_result: str | None
    consecutive_failures: int
    last_failure_kind: str | None
    last_failure_at: UtcDateTime | None
    failing_since: UtcDateTime | None
    last_checked_at: UtcDateTime | None
    state_changed_at: UtcDateTime | None
    # True when `last_checked_at` is older than the checker's stale window (or there is none):
    # the row is then not evidence of anything current, whatever its `state` says.
    stale: bool = False


class ServiceCheckerOut(BaseModel):
    """Whether the worker's service checker is running. `state` is NONE (never ran),
    ALIVE (a pass started within `stale_after_seconds`) or STALE (it did not: the checker thread has
    died, or the worker is not running). Read from the one row the checker writes each pass."""

    state: str
    last_pass_started_at: UtcDateTime | None
    last_pass_finished_at: UtcDateTime | None
    interval_seconds: float
    stale_after_seconds: float


class NotificationChannelOut(BaseModel):
    """one operator-notification channel. `error_class` is the exception class name that ended
    the last failed attempt: never a message, URL or response body."""

    channel: str
    configured: bool
    last_attempt_at: UtcDateTime | None
    last_result: str | None
    last_error_class: str | None


class HostHealthOut(BaseModel):
    """Built field by field in `routers/console.get_host_health`; the
    thresholds are the two settings the disk check judges against. Per-service results and
    channel state add their own fields here."""

    worker: WorkerLeaseHealthOut
    disk_min_free_percent: float
    disk_min_free_gb: float
    disks: list[DiskCheckOut]
    # the failed-check count after which a service reads UNHEALTHY, and every ACTIVE service.
    service_failure_threshold: int = 3
    services: list[ServiceHealthOut] = []
    service_checker: ServiceCheckerOut | None = None
    # the two operator-notification channels, configured or not, and their last attempt.
    notification_channels: list[NotificationChannelOut] = []
    notification_test_min_interval_seconds: float = 30.0
