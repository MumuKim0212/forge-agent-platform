"""The admin console's two missing read APIs.

Four of the console's six surfaces already have endpoints — usage and quota
overrides (`routers/usage.py`), role management (`routers/auth.py`), templates
(`routers/templates.py`). These are the two that did not exist:

- `GET /console/deployments` — deployment state across **all** projects. The
  deployments router is prefixed `/projects/{project_id}`, so before this there
  was no way to see more than one project at a time.
- `GET /console/configuration` — the effective operational configuration an
  operator otherwise has to read off a file on the host.

Two properties of this router are load-bearing, not incidental:

**It is read-only, and structurally so.** Deploy, rollback and stop stay on the
per-project router where owner-or-admin already applies.
Every route here is a `GET`; the console does not become a second
control path for the same actions, and `tests/test_console.py` asserts the
method set of every registered `/console` route rather than trusting review.

**The configuration endpoint never serialises `Settings`.** That object holds the
git host token, the OIDC client secret, the session and preview signing keys and
the LLM API credential alongside the operational values, so `model_dump()`, a
loop over fields, or "everything except a denylist" would each be a
credential-disclosure bug wearing the costume of a convenience feature. Every
exposed value is assigned by hand below, one line each, against a schema that
declares each field by name. A test asserts the response's key set against
literals and asserts it does not grow when a field is added to `Settings`.

Permission shape is the simple one: `admin` for everything, at `deps.require_role`.
Unlike `routers/usage.py` there is no by-id read and no
ownership axis here — a cross-project view has no single owner to defer to, and
`admin` is explicitly not a reason to relax the allowlist.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_role
from app.models.host_disk_check import HostDiskCheck
from app.models.notification import NotificationChannelStatus
from app.models.service_checker_status import SINGLETON_ID as CHECKER_ROW_ID, ServiceCheckerStatus
from app.models.service_health_check import ServiceHealthCheck
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.schemas.console import (
    DeploymentHealthOut,
    DiskCheckOut,
    HostHealthOut,
    NotificationChannelOut,
    ServiceCheckerOut,
    ServiceHealthOut,
    PlatformConfigurationOut,
    PlatformConfigurationValuesOut,
    ProjectDeploymentHealthOut,
    WorkerLeaseHealthOut,
)
from app.schemas.deployment import DeploymentOut
from app.services import notifications, worker_lease
from app.services.readiness import read_lease
from app.services.project_lifecycle import live_services_exempt_from_idle_sweep
from app.services.traefik_traffic import _aware as _aware_utc
from app.services.traefik_traffic import signal_is_configured, signal_state
from app.services.roles import Role
from app.services.status import ProjectKind

router = APIRouter(prefix="/console", tags=["console"])

# How many recent failures `GET /console/deployments` returns at most. Reported
# on the response as `recent_failures_limit` together with a
# `recent_failures_truncated` flag, so a bounded list can never be mistaken for
# the whole failure history (the health view asks
# for "recent failures", not all of them). The full history of one project stays
# available, unbounded, at `GET /projects/{id}/deployments`.
RECENT_FAILURE_LIMIT = 50

# Where the values in `GET /console/configuration` come from, and therefore where
# an operator has to go to change one (read once at startup from the
# environment; no runtime switching).
CONFIGURATION_SOURCE = "environment"


def _active_deployment_by_project(db: Session) -> dict[str, Deployment]:
    """The one `ACTIVE` deployment per project, keyed by project.

    "At most one ACTIVE per project" is enforced by the partial unique index
    `uq_deployments_one_active_per_project`, so this dict cannot silently drop a
    second row — there cannot be one.
    """
    return {
        deployment.project_id: deployment
        for deployment in db.scalars(
            select(Deployment).where(Deployment.status == DeploymentStatus.ACTIVE)
        )
    }


def _latest_deployment_by_project(db: Session) -> dict[str, Deployment]:
    """Each project's most recent deployment row whatever its status.

    This is what makes "no active deployment" legible: mid-deploy, stopped,
    superseded, failed and never-deployed are five different states that all have
    no `ACTIVE` row (see `ProjectDeploymentHealthOut`).

    Ranked in SQL rather than by reading the whole append-only history into the
    process and folding it: this returns at most one row per project regardless of
    how long that history gets. `row_number()` over `(created_at DESC, id DESC)`
    is portable to both backends (SQLite 3.25+ and Postgres) and the `id`
    tiebreak makes the result deterministic when two rows share a timestamp,
    which a plain `MAX(created_at)` group-by could not.
    """
    ranked = select(
        Deployment.id.label("deployment_id"),
        func.row_number()
        .over(
            partition_by=Deployment.project_id,
            order_by=[Deployment.created_at.desc(), Deployment.id.desc()],
        )
        .label("row_num"),
    ).subquery()

    newest_ids = select(ranked.c.deployment_id).where(ranked.c.row_num == 1)
    return {
        deployment.project_id: deployment
        for deployment in db.scalars(select(Deployment).where(Deployment.id.in_(newest_ids)))
    }


def _recent_failures(db: Session, limit: int) -> tuple[list[Deployment], bool]:
    """The newest `FAILED` deployments across all projects, and whether older
    ones exist beyond the limit.

    Fetches `limit + 1` rows and reports the extra one as truncation instead of
    running a second `COUNT`: one query, and the flag cannot disagree with the
    list it describes.
    """
    rows = list(
        db.scalars(
            select(Deployment)
            .where(Deployment.status == DeploymentStatus.FAILED)
            .order_by(Deployment.created_at.desc(), Deployment.id.desc())
            .limit(limit + 1)
        )
    )
    return rows[:limit], len(rows) > limit


def _deployment_out(deployment: Deployment | None) -> DeploymentOut | None:
    """`DeploymentOut` is reused unchanged rather than given a console-specific
    twin: one declared deployment shape means one place a field could ever be
    widened into. It carries no secret-bearing column — `Project.service_env_json`
    lives on the project, not the deployment, and is named nowhere in this
    module."""
    if deployment is None:
        return None
    return DeploymentOut.model_validate(deployment)


@router.get("/deployments", response_model=DeploymentHealthOut)
def get_deployment_health(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> DeploymentHealthOut:
    """Deployment state for every project in one call.

    **Every** project gets an entry, including one that has never been deployed:
    an operator needs to see those, and dropping them would make the view
    silently partial. Ordered by project name, matching `usage.project_usage`.

    Read-only. There is deliberately no action on this router.
    """
    active = _active_deployment_by_project(db)
    latest = _latest_deployment_by_project(db)
    failures, truncated = _recent_failures(db, RECENT_FAILURE_LIMIT)

    return DeploymentHealthOut(
        projects=[
            ProjectDeploymentHealthOut(
                project_id=project.id,
                project_name=project.name,
                active_deployment=_deployment_out(active.get(project.id)),
                latest_deployment=_deployment_out(latest.get(project.id)),
            )
            for project in db.scalars(
                # an eval project is a throwaway
                # with no deployment; it is not part of the platform's deployment health.
                select(Project).where(Project.kind != ProjectKind.EVAL).order_by(Project.name)
            )
        ],
        recent_failures=[DeploymentOut.model_validate(row) for row in failures],
        recent_failures_limit=RECENT_FAILURE_LIMIT,
        recent_failures_truncated=truncated,
    )


def _checker_stale_after_seconds() -> float:
    """Three intervals, but never less than one interval plus the cycle bound: a pass that runs to
    its bound is slow, not dead."""
    return max(
        3 * settings.service_check_interval_seconds,
        settings.service_check_interval_seconds + settings.service_check_cycle_timeout_seconds,
    )


def _service_checker(db: Session) -> ServiceCheckerOut:
    row = db.get(ServiceCheckerStatus, CHECKER_ROW_ID)
    stale_after = _checker_stale_after_seconds()
    started = _aware_utc(row.last_pass_started_at) if row is not None and row.last_pass_started_at else None
    finished = row.last_pass_finished_at if row is not None else None
    if started is None:
        state = "NONE"
    else:
        age = (datetime.now(timezone.utc) - started).total_seconds()
        state = "ALIVE" if age <= stale_after else "STALE"
    return ServiceCheckerOut(
        state=state,
        last_pass_started_at=started,
        last_pass_finished_at=finished,
        interval_seconds=settings.service_check_interval_seconds,
        stale_after_seconds=stale_after,
    )


def _service_health_rows(db: Session) -> list[ServiceHealthOut]:
    """every ACTIVE deployment with the worker's persisted check of it (none yet reads
    UNCHECKED, never healthy). Unhealthy first, then unchecked, then by project name."""
    rows = db.execute(
        select(Deployment.id, Project.id, Project.name, ServiceHealthCheck)
        .join(Project, Project.id == Deployment.project_id)
        .outerjoin(ServiceHealthCheck, ServiceHealthCheck.deployment_id == Deployment.id)
        .where(Deployment.status == DeploymentStatus.ACTIVE)
    ).all()

    stale_after = _checker_stale_after_seconds()
    now = datetime.now(timezone.utc)

    def _stale(check) -> bool:
        if check is None:
            return False  # UNCHECKED already says it
        return (now - _aware_utc(check.last_checked_at)).total_seconds() > stale_after

    out = [
        ServiceHealthOut(
            deployment_id=deployment_id,
            project_id=project_id,
            project_name=project_name,
            state=check.state if check is not None else "UNCHECKED",
            last_result=check.last_result if check is not None else None,
            consecutive_failures=check.consecutive_failures if check is not None else 0,
            last_failure_kind=check.last_failure_kind if check is not None else None,
            last_failure_at=check.last_failure_at if check is not None else None,
            failing_since=check.failing_since if check is not None else None,
            last_checked_at=check.last_checked_at if check is not None else None,
            state_changed_at=check.state_changed_at if check is not None else None,
            stale=_stale(check),
        )
        for deployment_id, project_id, project_name, check in rows
    ]
    rank = {"UNHEALTHY": 0, "UNCHECKED": 1}
    out.sort(key=lambda item: (rank.get(item.state, 2), item.project_name.lower(), item.deployment_id))
    return out


def _notification_channels(db: Session) -> list[NotificationChannelOut]:
    """both channels always listed (an unconfigured one says so). Whether a channel is
    configured is this process's settings; the last attempt is what the delivering process wrote."""
    configured = set(notifications.configured_channels())
    rows = {row.channel: row for row in db.scalars(select(NotificationChannelStatus))}
    out = []
    for channel in (notifications.CHANNEL_WEBHOOK, notifications.CHANNEL_EMAIL):
        row = rows.get(channel)
        out.append(
            NotificationChannelOut(
                channel=channel,
                configured=channel in configured,
                last_attempt_at=row.last_attempt_at if row is not None else None,
                last_result=row.last_result if row is not None else None,
                last_error_class=row.last_error_class if row is not None else None,
            )
        )
    return out


@router.get("/health", response_model=HostHealthOut)
def get_host_health(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> HostHealthOut:
    """Worker lease age and the latest host disk measurements.
    Read-only; the numbers are the worker's persisted results, nothing is measured here."""
    lease = read_lease(db)
    checks = db.scalars(select(HostDiskCheck).order_by(HostDiskCheck.label))
    return HostHealthOut(
        worker=WorkerLeaseHealthOut(
            state=lease.state,
            heartbeat_age_seconds=lease.heartbeat_age_seconds,
            stale_after_seconds=worker_lease.STALE_AFTER_SECONDS,
        ),
        disk_min_free_percent=settings.host_disk_min_free_percent,
        disk_min_free_gb=settings.host_disk_min_free_gb,
        service_failure_threshold=settings.service_check_failure_threshold,
        services=_service_health_rows(db),
        service_checker=_service_checker(db),
        notification_channels=_notification_channels(db),
        notification_test_min_interval_seconds=settings.notify_test_min_interval_seconds,
        disks=[
            DiskCheckOut(
                label=row.label,
                path=row.path,
                free_bytes=row.free_bytes,
                total_bytes=row.total_bytes,
                free_percent=row.free_percent,
                state=row.state,
                checked_at=row.checked_at,
            )
            for row in checks
        ],
    )


@router.get("/configuration", response_model=PlatformConfigurationOut)
def get_platform_configuration(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> PlatformConfigurationOut:
    """The effective operational configuration, read-only.

    **The assignments below are the allowlist.** Each exposed value is written
    out by hand; there is no reflection over `Settings`, no `model_dump()`, no
    field loop and no denylist, because `Settings` holds every platform
    credential in the same object as these values and a denylist silently leaks
    whatever a later change adds. Adding a
    value here is one line here plus one field on
    `PlatformConfigurationValuesOut` — deliberate, which is the point.

    Reads exactly one persisted row, the Traefik traffic signal's state, because
    whether live services are exempt from the idle sweep is a runtime fact; the per-subject
    quota overrides that *are* persisted and editable stay on `routers/usage.py` where they
    can be audited.
    """
    signal = signal_state(db) if signal_is_configured(settings) else None
    return PlatformConfigurationOut(
        source=CONFIGURATION_SOURCE,
        editable=False,
        change_requires_restart=True,
        values=PlatformConfigurationValuesOut(
            policy_medium_tier_checks=settings.policy_medium_tier_checks_list,
            policy_high_tier_checks=settings.policy_high_tier_checks_list,
            policy_medium_approvals_required=settings.policy_medium_approvals_required,
            policy_high_approvals_required=settings.policy_high_approvals_required,
            build_token_budget=settings.build_token_budget,
            build_cost_budget_usd=settings.build_cost_budget_usd,
            sandbox_memory_limit_mb=settings.sandbox_memory_limit_mb,
            sandbox_cpu_limit_cores=settings.sandbox_cpu_limit_cores,
            sandbox_pids_limit=settings.sandbox_pids_limit,
            build_max_duration_minutes=settings.build_max_duration_minutes,
            preview_ttl_minutes=settings.preview_ttl_minutes,
            quota_monthly_usd_default=settings.quota_monthly_usd_default,
            platform_timezone=settings.platform_timezone,
            service_data_expected_services=settings.service_data_expected_services,
            service_data_connections_per_service=settings.service_data_connections_per_service,
            project_lifecycle_idle_threshold_days=settings.project_lifecycle_idle_threshold_days,
            # derived, not a setting -- the sweep's own predicate, asked of the same
            # `settings` this view reads and of the persisted signal state, so the displayed
            # answer is the one the sweep acts on.
            project_lifecycle_live_services_exempt=live_services_exempt_from_idle_sweep(settings, db=db),
            # read-only facts of the Traefik traffic signal; null while unconfigured.
            # SQLite drops tzinfo on read; send aware UTC so a browser never reads it as local.
            traffic_signal_configured=signal_is_configured(settings),
            traffic_signal_observed_since=signal.observed_since if signal else None,
            traffic_signal_last_poll_at=signal.last_poll_at if signal else None,
            host_disk_check_interval_seconds=settings.host_disk_check_interval_seconds,
            host_disk_min_free_percent=settings.host_disk_min_free_percent,
            host_disk_min_free_gb=settings.host_disk_min_free_gb,
        ),
    )
