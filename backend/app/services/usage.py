"""Monthly cost rollups, effective limits, and breach evaluation.

Everything here is **derived at read time**. There is no rollup table
(a materialized rollup is a second copy of a fact `builds` already
holds, and second copies drift), no cached total, no cached effective limit,
and no stored breach flag: a breach is the comparison `spend > effective
limit`, recomputed on every read, so there is nothing to set, clear, or let
drift out of sync.

**Nothing in the build pipeline imports this module, and that is the point.**
submitting a change request, queueing a build,
executing it and deploying it never consult accumulated spend, so no rollup
query can ever sit between a user and their work. The per-build budgets
(`BUILD_TOKEN_BUDGET` / `BUILD_COST_BUDGET_USD` -> `BUDGET_EXCEEDED`, enforced
by the LLM proxy) are a **separate, pre-existing mechanism** that does fail
closed per build; this module neither reads, extends, nor references them.
Per-build budgets are enforcement, monthly quotas are observation — the
distinction is a stated goal, not an accident.

What is metered is only what the platform already records: `builds.cost_usd`,
written by the LLM proxy at build end. `cost_tokens` is not used for any quota
comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.build import Build
from app.models.project import Project
from app.models.user import User
from app.services.approval import requester_ids_for_builds
from app.services.audit import write_audit_log

MONTH_FORMAT = "%Y-%m"


class UsageSubject:
    """The two rollup subjects plus the line that keeps
    them honest. `UNATTRIBUTED` is not a third subject kind you can set a quota
    on — it is spend whose requester could not be resolved, reported on its own
    line so per-user totals stay reconcilable against per-project ones."""

    PROJECT = "project"
    USER = "user"
    UNATTRIBUTED = "unattributed"


class QuotaLimitSource:
    """Which of the two layers supplied an effective limit."""

    OVERRIDE = "override"
    DEFAULT = "default"


class InvalidMonthError(ValueError):
    """The requested month is not a `YYYY-MM` calendar month. Surfaced as 400:
    guessing (or silently falling back to the current month) would answer a
    different question than the one asked, and a number can never be
    mistaken for a different window."""


class QuotaValidationError(ValueError):
    """A negative override. `0.0` is **legal** and means
    "any spend is a breach" — it is a way to flag a subject, not a synonym for
    unlimited and not a synonym for blocking, which this platform does not do.
    `None` is also legal: it clears the override back to the global default,
    which is likewise not unlimited (rule 1)."""


# ---------------------------------------------------------------------------
# The monthly window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthWindow:
    """One calendar month, resolved once and passed down.

    `starts_at`/`ends_at` are UTC instants (`ends_at` exclusive) converted from
    the month's boundaries **in the configured platform timezone**, so every
    query compares against `builds.ended_at` in the storage timezone while the
    boundary itself still means midnight-local on the 1st. Month-boundary
    handling is the likeliest source of subtle error here, which is why the
    window is computed in exactly one place and then handed to every query
    rather than recomputed per query."""

    month: str  # canonical "YYYY-MM"
    timezone: str  # the platform timezone the boundaries were evaluated in
    starts_at: datetime
    ends_at: datetime


def resolve_month_window(month: str | None = None) -> MonthWindow:
    """The one place a month becomes a pair of instants.

    `month=None` means the current month **in the platform timezone**, never in
    the server's local time and never in the requester's — "this month"
    must mean the same thing for every user regardless of
    their location.

    Historical months resolve exactly the same way, since a reset is a new
    window and never a deletion (same section): last month's figures are still
    a query away."""
    tz = settings.platform_tzinfo

    if month is None:
        now_local = datetime.now(tz)
        year, month_number = now_local.year, now_local.month
    else:
        try:
            parsed = datetime.strptime(month, MONTH_FORMAT)
        except ValueError as exc:
            raise InvalidMonthError(f"month must be YYYY-MM, got {month!r}") from exc
        year, month_number = parsed.year, parsed.month

    start_local = datetime(year, month_number, 1, tzinfo=tz)
    # The 1st of the next month, in the same zone — so a DST transition inside
    # either month shifts the boundary by its own offset rather than by a
    # fixed 24h * days arithmetic.
    if month_number == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month_number + 1, 1, tzinfo=tz)

    return MonthWindow(
        month=f"{year:04d}-{month_number:02d}",
        timezone=settings.platform_timezone,
        starts_at=start_local.astimezone(UTC),
        ends_at=end_local.astimezone(UTC),
    )


def _window_predicates(window: MonthWindow) -> tuple:
    """Which builds belong to a month.

    Cost counts against the month the build **ended**, so a retry never moves
    or double-counts spend.

    `ended_at IS NULL` is excluded **explicitly**, not as a side effect of the
    range comparison: a build that has not ended has no month to belong to, so
    it is in no window's total — it is not "this month's" and it is not lost
    either; it starts counting in whichever month it eventually ends in.
    The `IS NOT NULL` clause is redundant today (a comparison against NULL is
    never true, so the two range clauses already drop such a row) and is kept
    on purpose as a guard against a future refactor of the range; do not
    remove it as dead code.

    There is deliberately **no status filter**: failed builds count (spec rule
    3). A build that burned tokens and then failed spent real money, and
    excluding failures would make the total wrong in the direction that hides
    problems."""
    return (
        Build.ended_at.is_not(None),
        Build.ended_at >= window.starts_at,
        Build.ended_at < window.ends_at,
    )


# ---------------------------------------------------------------------------
# Limits and breach
# ---------------------------------------------------------------------------


def effective_limit(override: float | None) -> tuple[float, str]:
    """Resolve a subject's limit: the override if set, else the global default.

    `None` override means "use `QUOTA_MONTHLY_USD_DEFAULT`" — **it does not
    mean unlimited**. Resolved per subject per read and
    cached nowhere, so an admin's override applies to the very next
    read with no restart and no invalidation step."""
    if override is None:
        return settings.quota_monthly_usd_default, QuotaLimitSource.DEFAULT
    return override, QuotaLimitSource.OVERRIDE


def is_in_breach(spend_usd: float, limit_usd: float) -> bool:
    """`total > effective limit` — the whole of breach. Strictly greater, so a
    `0.0` limit is breached by any spend at all
    but not by a subject that spent nothing."""
    return spend_usd > limit_usd


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectUsage:
    """One reported line: what a subject spent in the window and how that
    compares to its limit."""

    kind: str
    subject_id: str | None  # None only for the unattributed line
    spend_usd: float
    build_count: int
    # Builds in the window whose `cost_usd` is NULL — *not measured*, which is
    # a different fact from a measured 0.0. Sums treat NULL as zero, so
    # without this count a
    # reader could not tell "spent nothing" from "we never captured it".
    unmeasured_build_count: int
    effective_limit_usd: float | None  # None only for the unattributed line
    limit_source: str | None
    in_breach: bool


@dataclass(frozen=True)
class _Totals:
    spend_usd: float = 0.0
    build_count: int = 0
    unmeasured_build_count: int = 0


_NO_SPEND = _Totals()


def _line(kind: str, subject_id: str | None, totals: _Totals, override: float | None) -> SubjectUsage:
    limit, source = effective_limit(override)
    return SubjectUsage(
        kind=kind,
        subject_id=subject_id,
        spend_usd=totals.spend_usd,
        build_count=totals.build_count,
        unmeasured_build_count=totals.unmeasured_build_count,
        effective_limit_usd=limit,
        limit_source=source,
        in_breach=is_in_breach(totals.spend_usd, limit),
    )


def _unattributed_line(totals: _Totals) -> SubjectUsage:
    """Spend with no resolvable requester.

    Always reported, even at zero, and never folded into a user: it has no
    subject to hold a limit, so it has no limit and cannot be in breach — the
    project rollup is where that spend is against a limit."""
    return SubjectUsage(
        kind=UsageSubject.UNATTRIBUTED,
        subject_id=None,
        spend_usd=totals.spend_usd,
        build_count=totals.build_count,
        unmeasured_build_count=totals.unmeasured_build_count,
        effective_limit_usd=None,
        limit_source=None,
        in_breach=False,
    )


def _project_totals(db: Session, window: MonthWindow) -> dict[str, _Totals]:
    """`SUM(cost_usd)` grouped by `builds.project_id` for the window — the
    per-project rollup, straight out of the rows that already exist.

    `coalesce(sum, 0.0)` because `cost_usd` is nullable: NULL sums as zero
    rather than making the whole SUM NULL. The
    NULL rows are counted separately so "not measured" survives the sum."""
    rows = db.execute(
        select(
            Build.project_id,
            func.coalesce(func.sum(Build.cost_usd), 0.0),
            func.count(Build.id),
            func.coalesce(func.sum(case((Build.cost_usd.is_(None), 1), else_=0)), 0),
        )
        .where(*_window_predicates(window))
        .group_by(Build.project_id)
    ).all()
    return {
        project_id: _Totals(
            spend_usd=float(spend), build_count=count, unmeasured_build_count=unmeasured
        )
        for project_id, spend, count, unmeasured in rows
    }


def _user_totals(db: Session, window: MonthWindow) -> dict[str | None, _Totals]:
    """The same window's spend, attributed per user instead — keyed by
    requester id, with **`None` for unattributed spend**.

    Attribution goes through `approval.requester_ids_for_builds`, the same join
    the self-approval ban resolves a requester with (the
    requester of the `ChangeRequest` whose `build_id` is that build). What this
    caller does *not* take is that function's owner fallback: a build with no
    change request is unattributed here, not silently credited to the project
    owner.

    A build with *several* change requests raises `AmbiguousRequesterError`
 and it is deliberately not caught here: spend credited to an
    arbitrary one of several requesters is a wrong number, and a data-integrity
    failure should surface (a 500) rather than be reported as unattributed. A
    database at head cannot produce it (unique `change_requests.build_id`).

    Per-user and per-project totals are independent views of the same spend and
    are not expected to partition each other (spec rule 2)."""
    rows = db.execute(
        select(Build.id, Build.cost_usd).where(*_window_predicates(window))
    ).all()
    attribution = requester_ids_for_builds(db, [build_id for build_id, _ in rows])

    totals: dict[str | None, _Totals] = {}
    for build_id, cost_usd in rows:
        key = attribution.get(build_id)
        running = totals.get(key, _NO_SPEND)
        totals[key] = _Totals(
            spend_usd=running.spend_usd + (cost_usd or 0.0),
            build_count=running.build_count + 1,
            unmeasured_build_count=running.unmeasured_build_count + (1 if cost_usd is None else 0),
        )
    return totals


def project_usage(db: Session, window: MonthWindow) -> list[SubjectUsage]:
    """Every project's line for the window, including projects that spent
    nothing — a project with no builds this month still has an effective limit,
    and omitting it would make "not in breach" indistinguishable from
    "not listed"."""
    totals = _project_totals(db, window)
    return [
        _line(UsageSubject.PROJECT, project.id, totals.get(project.id, _NO_SPEND), project.monthly_quota_usd)
        for project in db.scalars(select(Project).order_by(Project.name))
    ]


def project_usage_for(db: Session, window: MonthWindow, project: Project) -> SubjectUsage:
    return _line(
        UsageSubject.PROJECT,
        project.id,
        _project_totals(db, window).get(project.id, _NO_SPEND),
        project.monthly_quota_usd,
    )


def user_usage(db: Session, window: MonthWindow) -> list[SubjectUsage]:
    """Every user's line for the window, **plus the unattributed line** (always
    present, even at zero)."""
    totals = _user_totals(db, window)
    lines = [
        _line(UsageSubject.USER, user.id, totals.get(user.id, _NO_SPEND), user.monthly_quota_usd)
        for user in db.scalars(select(User).order_by(User.name))
    ]
    lines.append(_unattributed_line(totals.get(None, _NO_SPEND)))
    return lines


def user_usage_for(db: Session, window: MonthWindow, user: User) -> SubjectUsage:
    return _line(
        UsageSubject.USER,
        user.id,
        _user_totals(db, window).get(user.id, _NO_SPEND),
        user.monthly_quota_usd,
    )


@dataclass(frozen=True)
class PlatformUsage:
    """The platform-wide summary, assembled from the two
    rollups above rather than from a third query — so it can never disagree
    with the per-subject views it summarizes."""

    window: MonthWindow
    total_spend_usd: float
    unattributed_spend_usd: float
    build_count: int
    unmeasured_build_count: int
    default_limit_usd: float
    projects_in_breach: list[str]
    users_in_breach: list[str]


def platform_usage(db: Session, window: MonthWindow) -> PlatformUsage:
    projects = project_usage(db, window)
    users = user_usage(db, window)
    unattributed = next(line for line in users if line.kind == UsageSubject.UNATTRIBUTED)

    # Summed over the project lines, not the user lines: `builds.project_id` is
    # non-null, so the project rollup covers every build in the window, while
    # the user rollup deliberately leaves some of it unattributed.
    return PlatformUsage(
        window=window,
        total_spend_usd=sum(line.spend_usd for line in projects),
        unattributed_spend_usd=unattributed.spend_usd,
        build_count=sum(line.build_count for line in projects),
        unmeasured_build_count=sum(line.unmeasured_build_count for line in projects),
        default_limit_usd=settings.quota_monthly_usd_default,
        projects_in_breach=[line.subject_id for line in projects if line.in_breach],
        users_in_breach=[line.subject_id for line in users if line.in_breach],
    )


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def set_monthly_quota(
    db: Session,
    *,
    actor_id: str,
    subject_kind: str,
    subject: User | Project,
    new_quota: float | None,
) -> float | None:
    """Set (or clear, with `new_quota=None`) one subject's override and return
    the previous value.

    Raises `QuotaValidationError` rather than an HTTPException — the router maps
    it to a status code. Does not commit; the
    caller owns the transaction (same convention as `services/roles.py`).

    Every change is recorded in the append-only `AuditLog` with the acting
    admin, the subject, and **both** old and new values — the same shape as a
    role change, so a mutable column does not lose the
    history of who raised whose limit."""
    if new_quota is not None and new_quota < 0:
        raise QuotaValidationError(
            f"monthly_quota_usd must be >= 0 (0.0 means 'any spend is a breach'), "
            f"got {new_quota}"
        )

    old_quota = subject.monthly_quota_usd
    subject.monthly_quota_usd = new_quota
    write_audit_log(
        db,
        actor_id=actor_id,
        # `<entity>.quota_changed`, matching `user.role_changed`'s shape, so the
        # audit log stays filterable by entity+verb.
        action=f"{subject_kind}.quota_changed",
        target=f"{subject_kind}:{subject.id}",
        metadata={"old_monthly_quota_usd": old_quota, "new_monthly_quota_usd": new_quota},
    )
    return old_quota
