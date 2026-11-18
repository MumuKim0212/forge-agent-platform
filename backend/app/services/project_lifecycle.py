"""
zombie path: the mechanics of the automatic idle-project sweep -- flag,
stop, archive.

Mechanics only, mirroring the backup sweep's split: the schedule (how often the sweep
actually runs), the per-project error-swallowing, and the top-level
never-raises guarantee all live in `worker.py::sweep_idle_projects`; this
module owns the SQL candidate query and the three per-project stages.

Three stages, one pass, per candidate:

  1. **Flag** (`_flag_candidate`) — an audit entry only. Nothing about the
     project changes. Committed by the caller BEFORE stage 2/3 run, so a
     project whose stop or archive step then fails still carries a
     timeline entry saying it was detected.
  2. **Stop** (`_stop_if_active`) — the active deployment, if any. A no-op,
     with no Docker call, for a project with no ACTIVE deployment (the
     never-deployed population, or one already stopped/superseded/
     failed). Calls `deploy.stop_active_deployment(..., touch_activity=
     False)` — trap T1: the sweep's own stop must not read as the human
     activity that same function records for `routers/deployments.py`'s
     stop endpoint, or every project it archives would have its idle
     clock reset to now in the same transaction that archives it.
  3. **Archive** (`_archive`) — `Project.status = ARCHIVED`, with its own
     audit entry carrying `previous_status` in metadata (trap T3): the
     only place that value is recoverable from, since un-archiving
     (`routers/projects.py::unarchive_project`) gets no new column.

Two populations are recorded in
every flag/archive entry's metadata but not branched on:
both get the identical automatic treatment here. A later, explicit
operator action on a named project, is where the distinction starts to
license anything destructive.

One population is left out unless the signal is trusted:
while Traefik routes, `last_active_at` shows that a deployed service is in use only
if the worker reads Traefik's request counters (`services/traefik_traffic.py`). A project with an
ACTIVE deployment is then a candidate only when that signal polled within the last 3 minutes and
has watched the whole idle window; otherwise it is never a candidate -- no flag, no stop, no
archive (`live_services_exempt_from_idle_sweep`, applied in `idle_candidates`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services.audit import write_audit_log
from app.services.deploy import get_active_deployment, stop_active_deployment
from app.services.project_activity import service_traffic_is_observed
from app.services import traefik_traffic
from app.services.status import ProjectKind, ProjectStatus

logger = logging.getLogger("app.project_lifecycle")

# Worker-driven transitions have no human actor -- same convention as
# worker.SYSTEM_ACTOR_ID / deploy.SYSTEM_ACTOR_ID, redeclared here rather
# than imported, matching those two modules' own precedent (each names its
# own copy rather than centralizing one).
SYSTEM_ACTOR_ID: str | None = None

# The zombie path's two populations.
POPULATION_NEVER_DEPLOYED = "never_deployed"
POPULATION_DEPLOYED_THEN_IDLE = "deployed_then_idle"

# Activity tracking landed on this date: the day
# `Project.last_active_at` became a real, continuously-maintained activity
# signal rather than a value set once at row creation and bumped only by a
# change request. Every project's history before this date is fabricated,
# not merely stale -- the field was not tracking activity at all, so
# judging it against a 90-day threshold on the day this sweep first runs
# would misread "nobody ever recorded a reason to touch this field" as
# "genuinely idle for the threshold window", for essentially every project
# on the platform at once (the case this guards against).
# The sweep refuses to act while the WINDOW it
# would be judging (`now - threshold`) is still earlier than this date --
# i.e. until the signal has actually existed for a full threshold's worth
# of time.
#
# INERT on a fresh install, permanently: every project gets a real
# `last_active_at` at row creation (`models/project.py`'s `_utcnow`
# default), so `now - threshold` there is never earlier than "now" itself,
# let alone a fixed date in the past. Do not delete this constant once it
# stops firing in practice -- it stays correct, just unreachable, until
# some future slice retires it as dead code on its own terms.
ACTIVITY_SIGNAL_EPOCH = datetime(2026, 9, 11, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _project_target(project_id: str) -> str:
    # Mirrors routers/projects.py's own helper of the same name and shape.
    # Services never import from routers in this codebase, so it is
    # redeclared here rather than shared.
    return f"project:{project_id}"


def population_of(db: Session, project_id: str) -> str:
    """The two populations differ in what a
    DESTRUCTIVE stage would cost, not in what this module does to them (that
    stage comes later). A project has ever been deployed iff it owns at least
    one `Deployment` row of any status -- true even for one that is now
    STOPPED/SUPERSEDED/FAILED, which is still `deployed_then_idle`:
    something real was created for it once. `.limit(1)` — existence, not
    a count."""
    ever_deployed = (
        db.scalars(select(Deployment.id).where(Deployment.project_id == project_id).limit(1)).first()
        is not None
    )
    return POPULATION_DEPLOYED_THEN_IDLE if ever_deployed else POPULATION_NEVER_DEPLOYED


def live_services_exempt_from_idle_sweep(
    cfg: Settings | None = None,
    *,
    db: Session | None = None,
    now: datetime | None = None,
    cutoff: datetime | None = None,
) -> bool:
    """
    Whether a project with an ACTIVE deployment is left out of the idle sweep
    altogether (no flag, no stop, no archive).

    * Traefik off: the platform-origin service proxy records traffic, so never exempt.
    * Traefik on and `TRAEFIK_METRICS_PORT` unset: no signal exists, exempt.
    * Traefik on and the port set: a runtime fact read from the persisted signal state. Exempt
      unless the last successful poll is at most 3 minutes old AND `observed_since <= cutoff`
      (the signal has watched the whole idle window; `cutoff` defaults to now minus the
      configured threshold). Without a `db` the state cannot be read, so: exempt.

    This is the one definition; `idle_candidates` applies it and the admin console's
    configuration view reports it, so what the operator is told cannot differ from what the
    sweep does."""
    cfg = settings if cfg is None else cfg
    if service_traffic_is_observed(cfg):
        return False
    if cfg.traefik_metrics_port is None or db is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if cutoff is None:
        cutoff = moment - timedelta(days=cfg.project_lifecycle_idle_threshold_days)
    return not traefik_traffic.signal_is_trusted(db, now=moment, cutoff=cutoff)


def idle_candidates(db: Session, *, cutoff: datetime, now: datetime | None = None) -> list[Project]:
    """(a2): the comparison runs in SQL, not Python -- every other
    timestamp comparison in this codebase does. `cutoff` is always
    tz-aware (built by the caller from `_utcnow()` or an injected `now`),
    so this reads correctly on both SQLite (dev/test) and Postgres (prod)
    without needing `project_activity._as_aware_utc`'s reload-normalization
    dance, which exists only for a Python-side comparison against a value
    read back from SQLite.

    **This query is the one place the sweep's population is chosen**, so it
    is where `live_services_exempt_from_idle_sweep` bites: every stage
    (flag, stop, archive) acts only on what this returns, and a candidate
    list shown to an operator later must not list what the platform will
    never act on. "Has an ACTIVE deployment" is read from `Deployment`
    rows, never from `Project.status` (spec rule 6)."""
    conditions = [
        Project.last_active_at < cutoff,
        Project.status != ProjectStatus.ARCHIVED,
        # an eval project is a throwaway, not a service anyone
        # abandoned -- it is never flagged, stopped or archived by the sweep.
        Project.kind != ProjectKind.EVAL,
    ]
    if live_services_exempt_from_idle_sweep(db=db, now=now, cutoff=cutoff):
        # no trusted traffic signal where Traefik routes -- see the function above.
        conditions.append(
            ~exists().where(
                Deployment.project_id == Project.id,
                Deployment.status == DeploymentStatus.ACTIVE,
            )
        )
    return list(db.scalars(select(Project).where(*conditions)))


def _flag_candidate(db: Session, project: Project, *, population: str) -> None:
    """Stage 1: the flag IS the record, not a delay.
    Changes nothing on `project`: flagging changes
    nothing. The caller commits this on its own, before stage 2/3 run."""
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="project.idle_flagged",
        target=_project_target(project.id),
        metadata={
            "last_active_at": project.last_active_at.isoformat(),
            "population": population,
        },
    )


def _stop_if_active(db: Session, project: Project, *, deploy_manager) -> None:
    """Stage 2. Returns without constructing or calling anything
    Docker-shaped when there is no ACTIVE deployment (a4) -- the ordinary
    shape for the never-deployed population and for a project already
    stopped/superseded/failed. Lazily constructs a real `DeployManager`
    only when a candidate actually has one to stop, mirroring
    `sweep_expired_previews`'s/`sweep_service_data_backups`'s own lazy
    construction; tests inject `deploy_manager` (a fake) so this path never
    needs Docker (f2)."""
    if get_active_deployment(db, project.id) is None:
        return

    manager = deploy_manager
    if manager is None:
        from app.services.deploy_manager import DeployManager

        manager = DeployManager()

    # `touch_activity=False` -- see deploy.py's own
    # docstring on `stop_active_deployment`. Any exception here (a Docker
    # failure, a container already gone) propagates to the caller
    # unchanged: (a4) a project whose stop raises is left unarchived, and
    # `worker.sweep_idle_projects` is what catches it and moves on.
    stop_active_deployment(
        db,
        project=project,
        actor_id=SYSTEM_ACTOR_ID,
        manager=manager,
        touch_activity=False,
    )


def _archive(db: Session, project: Project, *, population: str) -> None:
    """Stage 3 (trap T3): the archive audit entry's metadata carries
    `previous_status` -- the ONLY place `routers/projects.py::
    unarchive_project` can recover it from; no column is added for this."""
    previous_status = project.status
    project.status = ProjectStatus.ARCHIVED
    write_audit_log(
        db,
        actor_id=SYSTEM_ACTOR_ID,
        action="project.archived",
        target=_project_target(project.id),
        metadata={"previous_status": previous_status, "population": population},
    )


def process_idle_candidate(db: Session, project: Project, *, deploy_manager) -> None:
    """One candidate, all three stages, in the one order the spec allows
    (design decision 1). The flag is committed on its own before stage 2/3
    run: `worker.sweep_idle_projects` is the one that catches a stage-2/3
    exception, rolls back ONLY the uncommitted work from this call, and
    continues to the next candidate — the flag committed just above
    survives that rollback untouched."""
    population = population_of(db, project.id)
    _flag_candidate(db, project, population=population)
    db.commit()

    _stop_if_active(db, project, deploy_manager=deploy_manager)
    _archive(db, project, population=population)
