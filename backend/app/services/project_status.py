"""Project status forward transitions.

`advance` is the ONE function that moves `Project.status` along the pipeline
(SUBMITTED -> BUILD_PENDING -> PREVIEW_READY -> AWAITING_APPROVAL -> ACTIVE).
The three other writers of that column are named and few: project creation
(`routers/projects.create_project`), archive (`project_lifecycle._archive`)
and un-archive (`routers/projects.unarchive_project`); `demo_data.py` sets
statuses on seed rows. `tests/test_project_status.py` pins that list by
walking `app/`.

Rules:

- **Forward only.** A target applies only if it is later in `FORWARD_ORDER`
  than the current status. A second build on a PREVIEW_READY/ACTIVE project,
  or an auto-approved build (which never reaches AWAITING_APPROVAL), is a
  no-op rather than a regression.
- **ARCHIVED is untouched.** Activity on an archived project does not
  un-archive it; only the explicit un-archive action does.
- **Audited.** Every applied transition writes `project.status_changed`
  (old, new, cause) in the caller's transaction, which shows on the timeline.
- Never inferred at read time, and never the gate on destroying data: the
  zombie path's population is decided by Deployment-row existence
  (`project_lifecycle.population_of`), not by this column.

Does not commit; the caller commits as part of its own transaction.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.project import Project
from app.services.audit import write_audit_log
from app.services.status import ProjectStatus

# Lifecycle order of the statuses a forward transition can move between.
# ARCHIVED is deliberately absent: it is neither a source nor a target here.
FORWARD_ORDER: tuple[str, ...] = (
    ProjectStatus.DRAFT,
    ProjectStatus.SUBMITTED,
    ProjectStatus.BUILD_PENDING,
    ProjectStatus.PREVIEW_READY,
    ProjectStatus.AWAITING_APPROVAL,
    ProjectStatus.ACTIVE,
)

STATUS_CHANGED_ACTION = "project.status_changed"


def is_forward_move(current: str, target: str) -> bool:
    """True iff `target` is strictly later than `current` in `FORWARD_ORDER`.
    A status outside the order (ARCHIVED, or a value nothing writes) is never
    a source or a target: fail closed, no move."""
    if current not in FORWARD_ORDER or target not in FORWARD_ORDER:
        return False
    return FORWARD_ORDER.index(target) > FORWARD_ORDER.index(current)


def advance(
    db: Session,
    project: Project,
    target: str,
    *,
    cause: str,
    actor_id: str | None = None,
) -> bool:
    """Move `project` forward to `target` if that is a forward move; return
    whether it moved. `cause` names the event (`"build:<id>"` /
    `"deployment:<id>"`) and is recorded on the audit entry. `actor_id` is the
    human whose action it was, or None for the platform's own follow-through.

    Raises `ValueError` for a target outside `FORWARD_ORDER` (ARCHIVED
    included): that is a programming error at a call site, not a state of the
    world to be ignored."""
    if target not in FORWARD_ORDER:
        raise ValueError(f"{target!r} is not a forward-transition target")
    old = project.status
    if not is_forward_move(old, target):
        return False
    project.status = target
    write_audit_log(
        db,
        actor_id=actor_id,
        action=STATUS_CHANGED_ACTION,
        target=f"project:{project.id}",
        metadata={"old": old, "new": target, "cause": cause},
    )
    return True
