"""Deploy endpoints.

- `POST /projects/{id}/deployments` — the confirm action:
  owner-or-admin only; validates and creates the `Deployment` row
  in DEPLOYING. Execution (merge -> health-gated switchover) happens in the
  worker process, never here (for durability reasons).
- `GET /projects/{id}/deployments` — deployment history, newest first.
- `POST /projects/{id}/deployments/rollback` — one-click rollback:
  creates a new DEPLOYING row with `rollback_of` set.
- `POST /projects/{id}/deployments/stop` — stop the ACTIVE deployment:
  synchronous — one container remove, not a health-gated
  multi-step operation.

`GET|POST|... /projects/{id}/service[/{path}]` (dev-mode routing, the
platform-proxy mechanism) moved to its own conditionally-mounted router,
`routers/service_proxy.py` — ADR 0028 layer 2: it exists only
where Traefik does not route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.build import Build
from app.models.deployment import Deployment
from app.models.project import Project
from app.models.user import User
from app.schemas.deployment import DeployConfirmInput, DeploymentOut
from app.services.deploy import (
    DeployConflictError,
    DeployValidationError,
    confirm_deploy,
    request_rollback,
    stop_active_deployment,
)
from app.services import deploy_manager as deploy_manager_module
from app.services.deploy_manager import DeployManager
from app.services.roles import Role, holds_at_least

router = APIRouter(prefix="/projects/{project_id}", tags=["deployments"])


def _deploy_manager() -> DeployManager:
    """One seam for tests to stub Docker out of the API side (monkeypatched
    in tests/test_deployments_api.py)."""
    return DeployManager()


def _image_check_manager() -> DeployManager:
    """the seam the PROJECT READ (`GET /projects/{id}`, `routers/projects.py`) builds its
    image-presence check on. A separate seam from `_deploy_manager` because it has a different
    budget: this client gives up after `IMAGE_CHECK_TIMEOUT_SECONDS`, so a hung daemon is reported
    as `DOCKER_UNAVAILABLE` quickly instead of holding the read for docker-py's 60 s per call. The
    module constant is read at call time (a test shortens it)."""
    return DeployManager(timeout=deploy_manager_module.IMAGE_CHECK_TIMEOUT_SECONDS)


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_owner_or_admin(current_user: User, project: Project) -> None:
    """"Who: project owner or platform
    admin" — same rule for rollback and stop.

    the role half goes through the one ladder helper
    (`services/roles.holds_at_least`), never an inline string compare. The two
    halves stay independent checks because ownership is a separate axis from
    role: `admin` overrides ownership for operational reach,
    `approver` does not, and a role the ladder does not recognise leaves the
    owner check as the user's only way in."""
    if current_user.id != project.owner_id and not holds_at_least(current_user.role, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Project owner or admin role required")


@router.post(
    "/deployments",
    response_model=DeploymentOut,
    status_code=201,
    responses={
        409: {
            "description": (
                "Two distinct conflicts. PROJECT_ARCHIVED: the project is archived; un-archive "
                "it before confirming (the detail starts with that code). Otherwise another "
                "deployment of the project is already in flight, or the project is an eval "
                "project."
            )
        }
    },
)
def confirm_deployment(
    project_id: str,
    body: DeployConfirmInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Deployment:
    project = _get_project(db, project_id)
    _require_owner_or_admin(current_user, project)

    build = db.get(Build, body.build_id)
    if build is None or build.project_id != project_id:
        raise HTTPException(status_code=404, detail="Build not found")

    try:
        manager = _deploy_manager()
    except Exception as exc:  # noqa: BLE001 - Docker unreachable: cannot verify the image
        raise HTTPException(
            status_code=503, detail=f"cannot verify build image availability: {exc}"
        ) from exc

    try:
        deployment = confirm_deploy(
            db,
            project=project,
            build=build,
            actor_id=current_user.id,
            image_exists=manager.image_exists,
        )
    except DeployConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeployValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(deployment)
    return deployment


@router.get("/deployments", response_model=list[DeploymentOut])
def list_deployments(
    project_id: str,
    db: Session = Depends(get_db),
    # deployment history is a read,
    # so authenticated. Confirm/rollback/stop stay `own` or `admin`.
    # `proxy_service` (identity-free, ADR 0016) moved to its own
    # conditionally-mounted router, `routers/service_proxy.py` (ADR 0028
    # layer 2).
    _user: User = Depends(get_current_user),
) -> list[Deployment]:
    _get_project(db, project_id)
    return list(
        db.scalars(
            select(Deployment)
            .where(Deployment.project_id == project_id)
            .order_by(Deployment.created_at.desc())
        )
    )


@router.post(
    "/deployments/rollback",
    response_model=DeploymentOut,
    status_code=201,
    responses={
        409: {
            "description": (
                "PROJECT_ARCHIVED: the project is archived; un-archive it before rolling back "
                "(the detail starts with that code). Otherwise another deployment of the "
                "project is already in flight."
            )
        }
    },
)
def rollback_deployment(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Deployment:
    project = _get_project(db, project_id)
    _require_owner_or_admin(current_user, project)

    try:
        manager = _deploy_manager()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"cannot verify rollback image availability: {exc}"
        ) from exc

    try:
        deployment = request_rollback(
            db, project=project, actor_id=current_user.id, image_exists=manager.image_exists
        )
    except DeployConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeployValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(deployment)
    return deployment


@router.post("/deployments/stop", response_model=DeploymentOut)
def stop_deployment(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Deployment:
    project = _get_project(db, project_id)
    _require_owner_or_admin(current_user, project)

    try:
        manager = _deploy_manager()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"cannot reach Docker to stop: {exc}") from exc

    try:
        deployment = stop_active_deployment(
            db, project=project, actor_id=current_user.id, manager=manager
        )
    except DeployValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(deployment)
    return deployment
