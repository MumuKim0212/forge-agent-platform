"""Build image retention.

Every build image carries the ADR 0037 labels (`forge.instance`, `forge.role=build-image`,
`forge.build_id`, `forge.project_id`), stamped at build time by `image_assembly.assemble_image`.
This module decides which of this installation's build images are still needed and removes the
rest, from the worker, hourly.

* `keep_build_ids(db, now)` is the keep-set: **one named function**, the correctness condition of
  the sweep. A build's image is kept if the build is any of five things (listed in its docstring).
  Age is never a condition: a month-old image can be the live one.
* `sweep_build_images(db, ...)` lists the images labelled with this instance's id and the
  `build-image` role, takes the labels AGAIN off each image it was handed, and removes those whose
  build is not in the keep-set -- after re-checking the lease (ADR 0038) and one image at a time,
  with one audit entry each. Never forced: an image still in use (a container made from it
  exists) is logged and retried next cycle.

**What it never touches** (ADR 0037): an unlabelled image (older builds; the runbook names
the one-time manual command), another instance's image, an image whose role is not `build-image`,
an image with no `forge.build_id` label (cannot be judged, so it is left). Selection is by label
and by nothing else; no name or tag prefix is consulted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.services.audit import write_audit_log
from app.services.build_queue import _utcnow
from app.services.deploy import (
    _latest_successful_build,
    get_active_deployment,
    rollback_window,
)
from app.services.platform_labels import (
    LABEL_BUILD_ID,
    LABEL_INSTANCE,
    LABEL_PROJECT_ID,
    LABEL_ROLE,
    Role,
    instance_id,
)
from app.services.status import BuildStatus

logger = logging.getLogger("app.image_retention")

SWEEP_INTERVAL_SECONDS = 3600  # hourly; a constant, like the traffic poll's cadence


def keep_build_ids(db: Session, now: datetime | None = None) -> set[str]:
    """The ids of the builds whose images must stay. A build is kept if it is:

    1. the build of an `ACTIVE` or `DEPLOYING` deployment;
    2. a project's latest `SUCCEEDED` build -- the only one `confirm` accepts, which is what
       covers a build still waiting for approval;
    3. a build with a live preview (`preview_expires_at` in the future), which covers eval runs;
    4. a `RUNNING` build;
    5. one of the project's `ROLLBACK_WINDOW` most recently superseded builds
       (`deploy.rollback_window`, the same definition rollback itself walks).
    """
    moment = now or _utcnow()
    keep: set[str] = set()

    # 1. the running (or about to be running) service
    keep.update(
        db.scalars(
            select(Deployment.build_id).where(
                Deployment.status.in_((DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING))
            )
        )
    )

    # 2. the latest SUCCEEDED build of each project (`deploy._latest_successful_build`: confirm's own)
    for project_id in db.scalars(
        select(Build.project_id).where(Build.status == BuildStatus.SUCCEEDED).distinct()
    ).all():
        latest = _latest_successful_build(db, project_id)
        if latest is not None:
            keep.add(latest.id)

    # 3. a live preview
    keep.update(
        db.scalars(
            select(Build.id).where(
                Build.preview_expires_at.is_not(None), Build.preview_expires_at > moment
            )
        )
    )

    # 4. a build in flight
    keep.update(db.scalars(select(Build.id).where(Build.status == BuildStatus.RUNNING)))

    # 5. the rollback window of each project that has superseded deployments
    for project_id in db.scalars(
        select(Deployment.project_id).where(Deployment.status == DeploymentStatus.SUPERSEDED).distinct()
    ).all():
        active = get_active_deployment(db, project_id)
        for deployment in rollback_window(db, project_id, active.build_id if active else None):
            keep.add(deployment.build_id)

    return keep


def _docker_client():
    import docker

    return docker.from_env()


def sweep_build_images(
    db: Session,
    *,
    client=None,
    lease=None,
    now: datetime | None = None,
    scan_state: dict | None = None,
) -> int:
    """Removes this installation's labelled build images that are not in the keep-set; returns how
    many it removed. Never raises (a failure is logged and the sweep goes on or ends; the next
    cycle retries) -- except `LeaseLost`, a `SystemExit` that must reach the worker's loop.

    `scan_state` is the caller's dict across ticks (`run_iteration`'s own, like the idle and
    orphan sweeps'): `next_due_at` gates the sweep to `SWEEP_INTERVAL_SECONDS`, advanced before
    anything is attempted so an unreachable Docker is retried next hour, not next tick. `None`
    means always due (tests). `lease` is re-checked before EACH removal (a read-only SELECT);
    none means unchecked (unit tests)."""
    moment = now or _utcnow()
    if scan_state is not None:
        due = scan_state.get("next_due_at")
        if due is not None and moment < due:
            return 0
        scan_state["next_due_at"] = moment + timedelta(seconds=SWEEP_INTERVAL_SECONDS)
    try:
        return _sweep(db, client, lease, moment, scan_state)
    except Exception:  # noqa: BLE001 - a sweep that raises must never take the worker loop down
        logger.exception("build image sweep failed; it is retried next cycle")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def _sweep(db: Session, client, lease, moment: datetime, scan_state: dict | None) -> int:
    own = instance_id()
    try:
        client = client or _docker_client()
        images = client.images.list(
            filters={"label": [f"{LABEL_INSTANCE}={own}", f"{LABEL_ROLE}={Role.BUILD_IMAGE}"]}
        )
    except Exception as exc:  # noqa: BLE001 - Docker unreachable: a skipped sweep, not a failure
        first = scan_state is None or not scan_state.get("unavailable")
        if scan_state is not None:
            scan_state["unavailable"] = True
        (logger.warning if first else logger.debug)("build image sweep: Docker is unreachable (%s)", exc)
        return 0
    if scan_state is not None:
        scan_state["unavailable"] = False

    # Judged on the labels read off each image, not on the list filter alone (ADR 0037).
    candidates = []
    for image in images:
        labels = getattr(image, "labels", None) or {}
        if labels.get(LABEL_INSTANCE) != own or labels.get(LABEL_ROLE) != Role.BUILD_IMAGE:
            continue
        build_id = labels.get(LABEL_BUILD_ID)
        if not build_id:
            logger.debug("build image sweep: %s has no build id label; left alone", getattr(image, "id", "?"))
            continue
        candidates.append((image, build_id, labels))
    if not candidates:
        return 0

    keep = keep_build_ids(db, moment)
    removed = 0
    for image, build_id, labels in candidates:
        if build_id in keep:
            continue
        if lease is not None:
            lease.require()
        if _remove_one(db, client, image, build_id, labels):
            removed += 1
    return removed


def _remove_one(db: Session, client, image, build_id: str, labels: dict) -> bool:
    """Removes one image, unforced, and audits it. Removing by tag untags and deletes when it is
    the last one; an image with no tag is removed by id."""
    from docker.errors import ImageNotFound

    names = list(getattr(image, "tags", None) or []) or [image.id]
    try:
        for name in names:
            client.images.remove(name, force=False)
    except ImageNotFound:
        return False  # someone else got there: no line, no audit
    except Exception as exc:  # noqa: BLE001 - in use (409) or any other refusal: retried next cycle
        logger.warning(
            "build image sweep: %s of build %s not removed (retried next cycle): %s", names, build_id, exc
        )
        return False
    logger.warning("build image sweep: removed %s of build %s (not in the keep-set)", names, build_id)
    metadata = {"image": names[0], "build_id": build_id}
    if labels.get(LABEL_PROJECT_ID):
        metadata["project_id"] = labels[LABEL_PROJECT_ID]
    write_audit_log(db, actor_id=None, action="build_image.removed", target=f"build:{build_id}", metadata=metadata)
    db.commit()
    return True
