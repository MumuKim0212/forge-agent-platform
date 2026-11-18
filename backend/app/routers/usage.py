"""The usage read API + the admin quota override.

The permission shape is the security-relevant part of this router, and it is
deliberately asymmetric between the by-id reads and the list reads:

- **By id** — a project's own owner, or the user themselves, plus `admin`.
- **Every list / cross-subject read** — `admin` only. There is no
  "filter by owner" parameter anywhere here on purpose: a filter is the usual
  way this leaks, because an unfiltered default then exposes everything and a
  forged filter value reads someone else's figure. An owner reads their own
  project by its id, and nothing else on this router answers to a non-admin
  without an ownership check.

Nothing here writes to `builds` and nothing here is called by the build
pipeline — see `services/usage.py`'s module docstring for why that separation
is the point rather than an implementation detail.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.project import Project
from app.models.user import User
from app.schemas.usage import (
    CostDashboardOut,
    PlatformUsageOut,
    QuotaOut,
    QuotaUpdate,
    SubjectUsageReportOut,
    UsageReportOut,
)
from app.services import usage, usage_dashboard
from app.services.roles import Role, holds_at_least

router = APIRouter(prefix="/usage", tags=["usage"])


def _window(month: str | None) -> usage.MonthWindow:
    """One window per request, resolved once and handed to every query
    (services/usage.resolve_month_window). A malformed month is 400 rather than
    a silent fallback to the current one."""
    try:
        return usage.resolve_month_window(month)
    except usage.InvalidMonthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/projects", response_model=UsageReportOut)
def list_project_usage(
    month: str | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> UsageReportOut:
    """All projects — a cross-subject read, so `admin` (spec rule 2)."""
    window = _window(month)
    return UsageReportOut(window=window, subjects=usage.project_usage(db, window))


@router.get("/projects/{project_id}", response_model=SubjectUsageReportOut)
def get_project_usage(
    project_id: str,
    month: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SubjectUsageReportOut:
    """A project's own usage: owner or `admin` (spec rule 1).

    The two halves stay separate checks, exactly as in
    `routers/deployments._require_owner_or_admin`: ownership is a different
    axis from role, so `admin` overrides ownership, `approver` does
    not, and a role the ladder does not recognise leaves ownership as the
    user's only way in."""
    project = _get_project(db, project_id)
    if current_user.id != project.owner_id and not holds_at_least(current_user.role, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Project owner or admin role required")

    window = _window(month)
    return SubjectUsageReportOut(
        window=window, subject=usage.project_usage_for(db, window, project)
    )


@router.get("/users", response_model=UsageReportOut)
def list_user_usage(
    month: str | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> UsageReportOut:
    """Every user's total **plus the unattributed line** — cross-subject, so
    `admin`. The unattributed line is always present, even at
    zero: it is how an admin can see that per-user totals do not have to
    reconcile against per-project ones."""
    window = _window(month)
    return UsageReportOut(window=window, subjects=usage.user_usage(db, window))


@router.get("/users/{user_id}", response_model=SubjectUsageReportOut)
def get_user_usage(
    user_id: str,
    month: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SubjectUsageReportOut:
    """A user's own total: themselves or `admin` (spec rule 1). Reading another
    user's figure is a cross-subject read and needs the `admin` rung.

    The authorization check runs **before** the row is loaded, deliberately: a
    non-admin asking about somebody else gets 403 whether or not that id
    exists, so the endpoint cannot be used to probe which user ids are real."""
    is_self = current_user.id == user_id
    if not is_self and not holds_at_least(current_user.role, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Own usage or admin role required")

    user = current_user if is_self else _get_user(db, user_id)
    window = _window(month)
    return SubjectUsageReportOut(window=window, subject=usage.user_usage_for(db, window, user))


@router.get("/summary", response_model=PlatformUsageOut)
def get_platform_usage(
    month: str | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> usage.PlatformUsage:
    """The platform-wide summary — `admin` (spec rule 2)."""
    return usage.platform_usage(db, _window(month))


@router.get("/dashboard", response_model=CostDashboardOut)
def get_cost_dashboard(
    month: str | None = None,
    limit: int = Query(
        usage_dashboard.DEFAULT_EXPENSIVE_BUILDS, ge=1, le=usage_dashboard.MAX_EXPENSIVE_BUILDS
    ),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> usage_dashboard.CostDashboard:
    """The cost dashboard: daily buckets, outcome breakdown and the most
    expensive builds for one month.
    A cross-subject read, so `admin` (spec rule 2).

    The role check is a dependency, so it runs before this body and before any
    query against `builds`: a non-admin never causes the window to be read, and a
    non-admin with a bad `limit` gets 403, not 422. `limit` outside 1..50 is 422
    (FastAPI's own validation); a malformed `month` is 400 like every read here."""
    return usage_dashboard.cost_dashboard(db, _window(month), limit=limit)


def _quota_response(subject_kind: str, subject: User | Project) -> QuotaOut:
    limit, source = usage.effective_limit(subject.monthly_quota_usd)
    return QuotaOut(
        subject_kind=subject_kind,
        subject_id=subject.id,
        monthly_quota_usd=subject.monthly_quota_usd,
        effective_limit_usd=limit,
        limit_source=source,
    )


def _apply_quota(
    db: Session, *, actor: User, subject_kind: str, subject: User | Project, body: QuotaUpdate
) -> QuotaOut:
    """The one write path behind both override endpoints: validate, apply,
    audit-log both values, commit."""
    try:
        usage.set_monthly_quota(
            db,
            actor_id=actor.id,
            subject_kind=subject_kind,
            subject=subject,
            new_quota=body.monthly_quota_usd,
        )
    except usage.QuotaValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(subject)
    return _quota_response(subject_kind, subject)


@router.patch("/quotas/users/{user_id}", response_model=QuotaOut)
def set_user_quota(
    user_id: str,
    body: QuotaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> QuotaOut:
    """Only `admin` may set or clear an override, and
    every change writes an `AuditLog` entry with actor, subject and both old
    and new values. Effective immediately — no restart, nothing to invalidate,
    because the effective limit is resolved per read."""
    return _apply_quota(
        db,
        actor=current_user,
        subject_kind=usage.UsageSubject.USER,
        subject=_get_user(db, user_id),
        body=body,
    )


@router.patch("/quotas/projects/{project_id}", response_model=QuotaOut)
def set_project_quota(
    project_id: str,
    body: QuotaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(Role.ADMIN)),
) -> QuotaOut:
    """`admin`-only, same rule and same audit shape as the user override —
    **not** the project owner: a subject raising its own limit would make the
    limit meaningless."""
    return _apply_quota(
        db,
        actor=current_user,
        subject_kind=usage.UsageSubject.PROJECT,
        subject=_get_project(db, project_id),
        body=body,
    )
