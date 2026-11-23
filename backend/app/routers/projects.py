from dataclasses import asdict

import docker
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.approval import ApprovalRequest
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.schemas.build import SmokeChecksOut, served_repair_routes
from app.schemas.change_request import ChangeRequestCreate, ChangeRequestOut
from app.schemas.types import to_utc
from app.schemas.project import (
    DeployControlsOut,
    ProjectCreate,
    ProjectDeprovisionRequest,
    ProjectDetailOut,
    ProjectOut,
    TimelineEntry,
)
from app.services.audit import write_audit_log
from app.services.build_queue import queue_wait
from app.services.build_trace import build_ids_with_trace, delete_traces_for_project
from app.services.deploy import DEPLOYMENT_LIVE_CODE, deploy_controls, list_live_deployments, service_url
from app.services.engines import sandbox_executor_active
from app.services.project_activity import touch_last_active
from app.services.project_status import advance
from app.services.roles import Role
from app.services.service_data_deprovision import ServiceDataDeprovisionError, deprovision_service_data
from app.services.status import BuildStatus, ProjectKind, ProjectStatus

# un-archiving reuses the deploy promotion rules'
# "project owner or platform admin" predicate verbatim, never a second
# inline comparison -- the first cross-router import in this codebase, and
# the alternative (copying the check here) is exactly the duplication the
# task calls out to avoid. `services/roles.py` still owns the ROLE half
# (`holds_at_least`); this predicate only adds the ownership half on top,
# which is router-local because ownership is not a role concept.
from app.routers import deployments as deployments_router
from app.routers.deployments import _require_owner_or_admin

router = APIRouter(prefix="/projects", tags=["projects"])


def _project_target(project_id: str) -> str:
    return f"project:{project_id}"


def _initial_build_status() -> str:
    """"Accepting a change
    request (create or modify) creates a Build in QUEUED. The API never
    executes builds; the worker does." Gated by the same engine-neutral opt-in
    as the worker's `_select_executor` (ADR 0035 §Decision 5): the flag
    AND at least one engine's key. A key without the flag (or the flag without
    a key) must not silently start queueing builds nothing will ever claim/run
    in a meaningful way. Without both, behavior stays exactly as before
    (ADR 0001: no fake pipeline states)."""
    if sandbox_executor_active(settings):
        return BuildStatus.QUEUED
    return BuildStatus.NOT_IMPLEMENTED


# the machine-readable prefix of the 409 detail (`CODE: message`, the deploy refusals'
# convention) a modify request is refused with while the project has no confirmed ref. The
# frontend pins it as a literal (`pages/changeRequest.ts`).
NO_CONFIRMED_REF_CODE = "NO_CONFIRMED_REF"


def _project_has_confirmed_ref(project: Project) -> bool:
    """a NAMED predicate (ADR 0021's shape) for "a modify build has something to start
    from". `Project.confirmed_ref` is set only by a confirmed deployment's merge, and an empty
    string is not a ref (the executor's staging reads it the same way)."""
    return bool(project.confirmed_ref)


def _create_change_request_and_build(
    db: Session, *, project: Project, requester_id: str, nl_request: str, build_type: str
) -> tuple[ChangeRequest, Build]:
    """One change request and the build it asks for, in the caller's transaction.

    a build that is
    actually QUEUED moves the project to BUILD_PENDING (forward-only; an
    ARCHIVED project stays ARCHIVED). A `NOT_IMPLEMENTED` build (no executor
    configured, ADR 0001) will never run, so it advances nothing -- no fake
    pipeline states."""
    project_id = project.id
    change_request = ChangeRequest(
        project_id=project_id, requester_id=requester_id, nl_request=nl_request
    )
    db.add(change_request)
    db.flush()  # assign change_request.id

    build = Build(project_id=project_id, type=build_type, status=_initial_build_status())
    db.add(build)
    db.flush()  # assign build.id

    change_request.build_id = build.id
    if build.status == BuildStatus.QUEUED:
        advance(
            db,
            project,
            ProjectStatus.BUILD_PENDING,
            cause=f"build:{build.id}",
            actor_id=requester_id,
        )
    return change_request, build


# The permission matrix: "List/read projects, timeline,
# builds, deployments — authenticated" (approval reads are scoped
# separately, ADR 0048). Every route on this
# router carries `get_current_user`. `proxy_preview` used to be the one
# identity-free exception here; ADR 0028 layer 2 moved it to its
# own conditionally-mounted router, `routers/preview_proxy.py` — see that
# module for the identity-free rationale (ADR 0016: the signed token is its
# access control), unchanged by the move.


@router.get("", response_model=list[ProjectOut])
def list_projects(
    owner_id: str | None = None,
    team_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Project]:
    stmt = select(Project)
    # eval projects are hidden from the default registry view;
    # `?kind=eval` shows them (and `?kind=normal` is the default, spelled out).
    stmt = stmt.where(Project.kind == (kind if kind else ProjectKind.NORMAL))
    if owner_id:
        stmt = stmt.where(Project.owner_id == owner_id)
    if team_id:
        stmt = stmt.where(Project.team_id == team_id)
    if status:
        stmt = stmt.where(Project.status == status)
    stmt = stmt.order_by(Project.created_at.desc())
    return list(db.scalars(stmt))


@router.post("", response_model=ProjectDetailOut, status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Project:
    template = db.get(Template, body.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    team = db.get(Team, body.team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    project = Project(
        name=body.name,
        owner_id=current_user.id,
        team_id=team.id,
        template_id=template.id,
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.flush()  # assign project.id

    _create_change_request_and_build(
        db,
        project=project,
        requester_id=current_user.id,
        nl_request=body.nl_request,
        build_type="create",
    )

    write_audit_log(
        db,
        actor_id=current_user.id,
        action="project.created",
        target=_project_target(project.id),
        metadata={"name": project.name},
    )

    db.commit()
    db.refresh(project)
    return _to_detail(db, project)


def _to_detail(db: Session, project: Project) -> ProjectDetailOut:
    owner = db.get(User, project.owner_id)
    team = db.get(Team, project.team_id)
    template = db.get(Template, project.template_id)
    return ProjectDetailOut(
        **ProjectOut.model_validate(project).model_dump(),
        owner_name=owner.name if owner else "",
        team_name=team.name if team else "",
        template_name=template.name if template else "",
        service_url=service_url(db, project),
    )


def _lazy_image_exists():
    """The read's `image_exists` seam (`deployments._image_check_manager`, which tests monkeypatch),
    built on first use: a project with nothing to deploy never asks Docker.

    Bounded: that manager's client gives up after `IMAGE_CHECK_TIMEOUT_SECONDS`, and the
    FIRST failure, building the client or asking it, is remembered and re-raised for every later
    check of the same read. The confirm side and the rollback side each ask, so without this a hung
    daemon would be waited on once per side."""
    manager = None
    failure: Exception | None = None

    def image_exists(image_ref):
        nonlocal manager, failure
        if failure is not None:
            raise failure
        try:
            if manager is None:
                manager = deployments_router._image_check_manager()
            return manager.image_exists(image_ref)
        except Exception as exc:  # noqa: BLE001 - deploy_controls reports any failure as DOCKER_UNAVAILABLE
            failure = exc
            raise

    return image_exists


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ProjectDetailOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    detail = _to_detail(db, project)
    # What the page may offer, from the
    # same functions the confirm and rollback actions run (never a copy of their rules).
    controls = deploy_controls(db, project, image_exists=_lazy_image_exists())
    return detail.model_copy(
        update={"deploy_controls": DeployControlsOut(**asdict(controls))}
    )


def _status_before_archival(db: Session, project_id: str) -> str:
    """recovers what `Project.status` held immediately before
    the automatic sweep archived it (`services/project_lifecycle._archive`)
    from that same action's own audit entry -- the newest
    `project.archived` entry targeting this project, per its own
    `previous_status` metadata key. No column carries this; the archive
    audit entry is the only record of it, by design.

    Falls back to `ProjectStatus.ACTIVE` if no such entry exists (an
    archive this task's own automatic path did not produce -- not expected
    to happen, since every archival this codebase can perform writes this
    entry first, but un-archiving must still resolve to *something* rather
    than 500 on a row a future path archives some other way)."""
    entry = db.scalars(
        select(AuditLog)
        .where(AuditLog.target == _project_target(project_id), AuditLog.action == "project.archived")
        .order_by(AuditLog.ts.desc())
    ).first()
    if entry is not None and entry.metadata_json:
        previous = entry.metadata_json.get("previous_status")
        if previous:
            return previous
    return ProjectStatus.ACTIVE


@router.post("/{project_id}/unarchive", response_model=ProjectDetailOut)
def unarchive_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectDetailOut:
    """"ARCHIVED is
    reversible ... un-archiving is an ordinary owner or admin action and
    restores nothing, because archival destroyed nothing." Ships in the
    same task that adds the automatic archiver, so `ARCHIVED` never
    becomes a one-way door before anything can write it (d1-d4)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _require_owner_or_admin(current_user, project)

    if project.status != ProjectStatus.ARCHIVED:
        raise HTTPException(status_code=400, detail="Project is not archived")

    restored_status = _status_before_archival(db, project.id)
    project.status = restored_status

    write_audit_log(
        db,
        actor_id=current_user.id,
        action="project.unarchived",
        target=_project_target(project.id),
        metadata={"restored_status": restored_status},
    )

    db.commit()
    db.refresh(project)
    return _to_detail(db, project)


def _confirms_target_project(body: ProjectDeprovisionRequest, project: Project) -> bool:
    """(d3): a NAMED predicate, not an inline `==`, so the confirmation
    check reads as its own concept rather than one more `if` among several
    -- the request body must echo the project's own id before anything
    destructive runs."""
    return body.project_id == project.id


def _population_at_archival(db: Session, project_id: str) -> str:
    """(d5): reads the population `_archive` already recorded on
    the newest `project.archived` audit entry (the same entry `_status_
    before_archival` reads `previous_status` from) rather than re-deriving
    it via `project_lifecycle.population_of` -- that function re-queries
    `Deployment` rows for a fact this project's own archival already
    resolved once. Falls back to `"unknown"` if no such entry exists (not
    expected in practice -- every `ARCHIVED` row on this platform is
    written by `project_lifecycle._archive`, which always writes this
    entry first -- but the deprovision action must still resolve to
    something rather than 500 on a row archived some other way)."""
    entry = db.scalars(
        select(AuditLog)
        .where(AuditLog.target == _project_target(project_id), AuditLog.action == "project.archived")
        .order_by(AuditLog.ts.desc())
    ).first()
    if entry is not None and entry.metadata_json:
        population = entry.metadata_json.get("population")
        if population:
            return population
    return "unknown"


def _docker_client():
    """the one Docker seam this router needs (Redis container
    removal only -- no image checks, no container starts, so a raw client
    is enough, unlike `routers/deployments.py::_deploy_manager`'s full
    `DeployManager`). Same testability shape: monkeypatched in
    `tests/test_projects.py`'s deprovision tests so those stay Docker-free."""
    return docker.from_env()


@router.post(
    "/{project_id}/deprovision",
    response_model=ProjectDetailOut,
    responses={
        409: {
            "description": (
                "DEPLOYMENT_LIVE: the project still has an ACTIVE or DEPLOYING deployment "
                "(named in the detail); stop it first. Nothing is dropped."
            )
        }
    },
)
def deprovision_project(
    project_id: str,
    body: ProjectDeprovisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> ProjectDetailOut:
    """the only route to destroying a service's data in
    this platform, and the only code outside `preview_data.py` permitted
    to issue a `DROP` (`services/service_data_deprovision.py`). Admin-only
    (d1) -- ownership grants no bypass here, unlike `unarchive_project`'s
    owner-or-admin, because "operator" is the `admin` role
    specifically. Never reached by elapsed time: nothing in `worker.py`
    calls this.

    Preconditions, all refused BEFORE any destructive call (d2, d3): the
    project must already be `ARCHIVED` (destruction is the last stage of a
    staged path, not a shortcut past it), it must have no `ACTIVE` or
    `DEPLOYING` deployment (409 `DEPLOYMENT_LIVE`, naming it), and the
    request body must echo the project's own id (`_confirms_target_project`)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.status != ProjectStatus.ARCHIVED:
        raise HTTPException(
            status_code=400,
            detail=f"Project must be archived before deprovisioning (current status: {project.status})",
        )

    # `ARCHIVED` was never proof that
    # nothing is serving, and `DROP DATABASE ... WITH (FORCE)` would cut a live
    # service's connections and drop its data. Refused on the deployment ROWS
    # (never on `project.status`), before the confirmation echo and before any
    # Docker or Postgres call; the admin stops the service first, which is an
    # explicit, audited step of its own.
    live = list_live_deployments(db, project.id)
    if live:
        named = ", ".join(f"{d.status} deployment {d.id}" for d in live)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{DEPLOYMENT_LIVE_CODE}: project {project.id} still has {named}; "
                "stop it before deprovisioning"
            ),
        )

    if not _confirms_target_project(body, project):
        raise HTTPException(status_code=400, detail="Confirmation project id does not match")

    try:
        docker_client = _docker_client()
    except Exception as exc:  # noqa: BLE001 - Docker unreachable: cannot remove the Redis container
        raise HTTPException(status_code=503, detail=f"cannot reach Docker: {exc}") from exc

    try:
        result = deprovision_service_data(
            project.id, docker_client=docker_client, delete_backups=body.delete_backups
        )
    except ServiceDataDeprovisionError as exc:
        raise HTTPException(status_code=502, detail=f"deprovisioning failed: {exc}") from exc

    # (a5): the stored service env is cleared in the SAME transaction as
    # the audit entry below -- a later `ensure_service_env` re-provisions
    # rather than handing out a DSN for a database that no longer exists.
    project.service_env_json = None

    # a build's trace goes with the project's other build data.
    # The builds themselves stay; the trace, which can hold diff content and
    # whatever the agent read, does not outlive the service's data.
    delete_traces_for_project(db, project.id)

    write_audit_log(
        db,
        actor_id=current_user.id,
        action="project.deprovisioned",
        target=_project_target(project.id),
        metadata={
            "population": _population_at_archival(db, project.id),
            "backups": "deleted" if body.delete_backups else "kept",
        },
    )

    db.commit()
    db.refresh(project)
    return _to_detail(db, project)


@router.get("/{project_id}/timeline", response_model=list[TimelineEntry])
def get_project_timeline(
    project_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[TimelineEntry]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    entries: list[TimelineEntry] = []

    change_requests = db.scalars(
        select(ChangeRequest).where(ChangeRequest.project_id == project_id)
    )
    for cr in change_requests:
        entries.append(
            TimelineEntry(
                kind="change_request",
                ts=cr.created_at,
                data={
                    "id": cr.id,
                    "nl_request": cr.nl_request,
                    "requester_id": cr.requester_id,
                    "build_id": cr.build_id,
                },
            )
        )

    builds = list(db.scalars(select(Build).where(Build.project_id == project_id)))
    # only WHETHER a build has a stored trace -- ids, never content. The trace
    # itself is served by `GET /builds/{id}/trace` behind its own, narrower access check.
    traced = build_ids_with_trace(db, [b.id for b in builds])
    for build in builds:
        entries.append(
            TimelineEntry(
                kind="build",
                ts=build.created_at,
                data={
                    "id": build.id,
                    "type": build.type,
                    "status": build.status,
                    # ADR 0035: measured at claim (the configured model; the trace's
                    # per-call model is the wire value). None on a build from before they
                    # were recorded.
                    "engine": build.engine,
                    "model": build.model,
                    "diff_ref": build.diff_ref,
                    "failure_reason": build.failure_reason,
                    # platform-authored text only (build_queue.mark_failed).
                    "failure_detail": build.failure_detail,
                    # for a QUEUED build only, {ahead, at_cap} -- two platform-measured
                    # numbers (build_queue.queue_wait); None for every other status.
                    "queue": queue_wait(db, build, concurrency_cap=settings.build_concurrency),
                    "preview_url": build.preview_url,
                    "preview_start_failure": build.preview_start_failure,
                    # A platform-measured count as stored (0 = none ran).
                    "repair_attempts": build.repair_attempts,
                    # The routes each repair was started for, as stored; None = none was
                    # started, or a build from before the column (or a malformed stored value).
                    "repair_routes": (
                        [entry.model_dump() for entry in attempts]
                        if (attempts := served_repair_routes(build.repair_routes)) is not None
                        else None
                    ),
                    "preview_expires_at": (
                        to_utc(build.preview_expires_at).isoformat() if build.preview_expires_at else None
                    ),
                    # the deploy surface (ProjectDetailPage) needs to
                    # know which build is confirmable without a separate
                    # builds endpoint — reuses the same
                    # services/approval.is_deploy_eligible rule BuildOut
                    # already exposes elsewhere.
                    "deploy_eligible": build.deploy_eligible,
                    # what the platform measured. COUNTS only -- every
                    # authenticated reader sees the timeline, and the agent-chosen file
                    # paths belong on the approval detail, not here. Both are None when
                    # the build was never measured (distinct from zero changes).
                    "changed_counts": (
                        {
                            "totals": build.changed_files["totals"],
                            "truncated": build.changed_files["truncated"],
                        }
                        if build.changed_files
                        else None
                    ),
                    "protected_path_drift": build.protected_path_drift,
                    # the platform's own smoke check of the preview's GET routes; None =
                    # not measured. Paths passed a character filter before they were stored.
                    "smoke_checks": (
                        SmokeChecksOut.model_validate(build.smoke_checks).model_dump()
                        if build.smoke_checks is not None
                        else None
                    ),
                    # the agent's own text -- an unverified claim, bounded at
                    # write. Display only: the UI shows it last, labelled, as plain text.
                    "agent_summary": build.agent_summary,
                    # a flag, not content.
                    "has_trace": build.id in traced,
                },
            )
        )

    # surface each build's policy verdict (tier
    # + per-check pass/flag summary) as its own timeline entry once the gate
    # has run. Approval routing / display polish is not done here.
    verdicts = db.scalars(
        select(PolicyVerdict)
        .join(Build, PolicyVerdict.build_id == Build.id)
        .where(Build.project_id == project_id)
    )
    for verdict in verdicts:
        entries.append(
            TimelineEntry(
                kind="policy_verdict",
                ts=verdict.created_at,
                data={
                    "id": verdict.id,
                    "build_id": verdict.build_id,
                    "risk_tier": verdict.risk_tier,
                    "checks": [
                        {"check_name": c.check_name, "result": c.result, "summary": c.summary}
                        for c in verdict.checks
                    ],
                },
            )
        )

    # "Requesters see their own
    # requests' status in the project timeline." One entry per
    # ApprovalRequest so the requester (and anyone else viewing the
    # project's timeline) sees routing/decision status without visiting the
    # approval console.
    approval_requests = db.scalars(
        select(ApprovalRequest)
        .join(Build, ApprovalRequest.build_id == Build.id)
        .where(Build.project_id == project_id)
    )
    for approval_request in approval_requests:
        entries.append(
            TimelineEntry(
                kind="approval_request",
                ts=approval_request.created_at,
                data={
                    "id": approval_request.id,
                    "build_id": approval_request.build_id,
                    "risk_tier": approval_request.risk_tier,
                    "status": approval_request.status,
                    "required_approvals": approval_request.required_approvals,
                    "approvals_so_far": sum(
                        1 for d in approval_request.decisions if d.decision == "approve"
                    ),
                },
            )
        )

    # one entry per Deployment row (history is
    # inspectable end-to-end), plus every deployment state-transition audit
    # entry (target `deployment:<id>`) so no transition is timeline-silent.
    deployments = list(
        db.scalars(select(Deployment).where(Deployment.project_id == project_id))
    )
    for deployment in deployments:
        entries.append(
            TimelineEntry(
                kind="deployment",
                ts=deployment.created_at,
                data={
                    "id": deployment.id,
                    "build_id": deployment.build_id,
                    "confirmed_by_id": deployment.confirmed_by_id,
                    "rollback_of": deployment.rollback_of,
                    "status": deployment.status,
                    "failure_reason": deployment.failure_reason,
                },
            )
        )
    deployment_targets = [f"deployment:{d.id}" for d in deployments]
    if deployment_targets:
        for entry in db.scalars(select(AuditLog).where(AuditLog.target.in_(deployment_targets))):
            entries.append(
                TimelineEntry(
                    kind="audit_log",
                    ts=entry.ts,
                    data={"action": entry.action, "actor_id": entry.actor_id, "target": entry.target},
                )
            )

    audit_entries = db.scalars(
        select(AuditLog).where(AuditLog.target == _project_target(project_id))
    )
    for entry in audit_entries:
        entries.append(
            TimelineEntry(
                kind="audit_log",
                ts=entry.ts,
                data={"action": entry.action, "actor_id": entry.actor_id},
            )
        )

    entries.sort(key=lambda e: e.ts)
    return entries


@router.get("/{project_id}/change-requests", response_model=list[ChangeRequestOut])
def list_change_requests(
    project_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[ChangeRequest]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return list(
        db.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project_id == project_id)
            .order_by(ChangeRequest.created_at)
        )
    )


@router.post(
    "/{project_id}/change-requests",
    response_model=ChangeRequestOut,
    status_code=201,
    responses={
        409: {
            "description": (
                "NO_CONFIRMED_REF: the project has no confirmed deployment yet, so there is "
                "nothing to modify (the detail starts with that code). No change request or "
                "build is created."
            )
        }
    },
)
def submit_change_request(
    project_id: str,
    body: ChangeRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChangeRequest:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # before a first deployment is confirmed there
    # is no confirmed ref, so nothing to modify. Refused before any row exists (the audit entry
    # below is written only for an accepted request, like every other refusal on this route).
    if not _project_has_confirmed_ref(project):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{NO_CONFIRMED_REF_CODE}: project {project.id} has no confirmed deployment yet, so "
                "there is nothing to modify; confirm a first deployment (or submit a new create "
                "request) before sending a change request"
            ),
        )

    change_request, _build = _create_change_request_and_build(
        db,
        project=project,
        requester_id=current_user.id,
        nl_request=body.nl_request,
        build_type="modify",
    )

    touch_last_active(project)

    write_audit_log(
        db,
        actor_id=current_user.id,
        action="change_request.submitted",
        target=_project_target(project.id),
        metadata={"change_request_id": change_request.id},
    )

    db.commit()
    db.refresh(change_request)
    return change_request
