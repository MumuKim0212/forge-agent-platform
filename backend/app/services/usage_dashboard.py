"""The cost dashboard read.

One admin-only read that shows the *shape* of a month's spend: when the money
went (daily buckets), on what outcomes (status, and for `FAILED` the failure
reason), and on which builds (the N most expensive). Derived from `builds` at
read time like every other usage read; nothing is stored.

**It reconciles by construction, not by a second query that happens to agree.**
The window is fetched once, with `usage._window_predicates` -- the same
predicate every other usage read uses -- and the daily buckets and the outcome
breakdown are both folded out of those same rows. A build therefore cannot
appear in one view and not the other, and a build outside the window appears in
neither. Only the summation order differs from
`platform_usage`'s SQL `SUM`, so a comparison against it is exact up to float
addition error, never a count off by one.

**Nothing in the build pipeline imports this module** (pinned by
`tests/test_usage.py`, which matches any module with `usage`
in its name).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.build import Build
from app.services.approval import requester_ids_for_builds
from app.services.status import BuildStatus
from app.services.usage import MonthWindow, _window_predicates

# From 2026-10-01 on the LLM proxy is the only source of
# `cost_usd` (ADR 0014 §Amendment 2026-10-01). Rows that ended before it may carry
# engine-reported figures. This is a fact about history, so it is a constant, not a
# `Settings` field: an operator who "configured" it would be rewriting the past.
METERING_CAVEAT_DATE = date(2026, 10, 1)

DEFAULT_EXPENSIVE_BUILDS = 10
MAX_EXPENSIVE_BUILDS = 50

# Statuses are listed in this order, then any other status alphabetically, so the
# breakdown never reshuffles between two reads of the same data.
_STATUS_ORDER = (BuildStatus.SUCCEEDED, BuildStatus.FAILED)


@dataclass
class _Acc:
    spend_usd: float = 0.0
    build_count: int = 0
    unmeasured_build_count: int = 0

    def add(self, cost_usd: float | None) -> None:
        self.spend_usd += cost_usd or 0.0
        self.build_count += 1
        self.unmeasured_build_count += 1 if cost_usd is None else 0


@dataclass(frozen=True)
class DailyBucket:
    day: date  # a calendar day in the platform timezone
    spend_usd: float
    build_count: int
    unmeasured_build_count: int
    failed_spend_usd: float  # spend of FAILED builds, a subset of `spend_usd`


@dataclass(frozen=True)
class FailureReasonBreakdown:
    failure_reason: str | None  # None: a FAILED build recorded without a reason
    spend_usd: float
    build_count: int
    unmeasured_build_count: int


@dataclass(frozen=True)
class OutcomeBreakdown:
    status: str
    spend_usd: float
    build_count: int
    unmeasured_build_count: int
    # Only for FAILED; empty for every other status.
    failure_reasons: list[FailureReasonBreakdown] = field(default_factory=list)


@dataclass(frozen=True)
class ExpensiveBuild:
    build_id: str
    project_id: str
    type: str
    status: str
    failure_reason: str | None
    cost_usd: float  # measured only: unmeasured builds are never listed
    cost_tokens: int | None
    ended_at: datetime
    requester_id: str | None  # None: unattributed


@dataclass(frozen=True)
class MeteringCaveat:
    date: date
    window_starts_before: bool


@dataclass(frozen=True)
class CostDashboard:
    window: MonthWindow
    limit: int
    days: list[DailyBucket]
    outcomes: list[OutcomeBreakdown]
    expensive_builds: list[ExpensiveBuild]
    metering_caveat: MeteringCaveat


def _as_utc(value: datetime) -> datetime:
    """SQLite hands a `timezone=True` column back naive (the UTC instant it was
    stored as); Postgres hands it back aware. Treat naive as UTC so the daily
    bucketing is the same on both."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def metering_caveat_for(window: MonthWindow) -> MeteringCaveat:
    """Whether the window starts before the caveat date, compared as calendar
    dates in the platform timezone (the window's own boundaries are local
    midnights)."""
    starts_local = window.starts_at.astimezone(settings.platform_tzinfo).date()
    return MeteringCaveat(
        date=METERING_CAVEAT_DATE, window_starts_before=starts_local < METERING_CAVEAT_DATE
    )


def _days_of(window: MonthWindow) -> list[date]:
    tz = settings.platform_tzinfo
    first = window.starts_at.astimezone(tz).date()
    after_last = window.ends_at.astimezone(tz).date()
    return [first + timedelta(days=n) for n in range((after_last - first).days)]


def cost_dashboard(
    db: Session,
    window: MonthWindow,
    *,
    limit: int = DEFAULT_EXPENSIVE_BUILDS,
    today: date | None = None,
) -> CostDashboard:
    """The dashboard for one window.

    `today` is a calendar date in the platform timezone and exists so a test
    never depends on the real date; production passes `None`.

    Day omission: in the current month, days after
    `today` are left out. A day after `today` that nonetheless holds builds
    (a clock skew, a build stamped in the future) is **kept**: dropping it would
    make the buckets disagree with the summary, which is the one thing this read
    must not do (rule 1)."""
    tz = settings.platform_tzinfo
    if today is None:
        today = datetime.now(tz).date()

    rows = db.execute(
        select(
            Build.id,
            Build.project_id,
            Build.type,
            Build.status,
            Build.failure_reason,
            Build.cost_usd,
            Build.cost_tokens,
            Build.ended_at,
        ).where(*_window_predicates(window))
    ).all()

    per_day: dict[date, _Acc] = defaultdict(_Acc)
    failed_per_day: dict[date, float] = defaultdict(float)
    per_status: dict[str, _Acc] = defaultdict(_Acc)
    per_reason: dict[str | None, _Acc] = defaultdict(_Acc)

    for _id, _project, _type, status, failure_reason, cost_usd, _tokens, ended_at in rows:
        day = _as_utc(ended_at).astimezone(tz).date()
        per_day[day].add(cost_usd)
        per_status[status].add(cost_usd)
        if status == BuildStatus.FAILED:
            failed_per_day[day] += cost_usd or 0.0
            per_reason[failure_reason].add(cost_usd)

    current_month = today.strftime("%Y-%m") == window.month
    days = []
    for day in _days_of(window):
        acc = per_day.get(day)
        if acc is None and current_month and day > today:
            continue
        acc = acc or _Acc()
        days.append(
            DailyBucket(
                day=day,
                spend_usd=acc.spend_usd,
                build_count=acc.build_count,
                unmeasured_build_count=acc.unmeasured_build_count,
                failed_spend_usd=failed_per_day.get(day, 0.0),
            )
        )

    def _status_key(status: str) -> tuple[int, str]:
        return (_STATUS_ORDER.index(status), "") if status in _STATUS_ORDER else (len(_STATUS_ORDER), status)

    reasons = [
        FailureReasonBreakdown(
            failure_reason=reason,
            spend_usd=acc.spend_usd,
            build_count=acc.build_count,
            unmeasured_build_count=acc.unmeasured_build_count,
        )
        # Most expensive reason first; ties (and the None reason) by name so the
        # order is a function of the data alone.
        for reason, acc in sorted(
            per_reason.items(), key=lambda kv: (-kv[1].spend_usd, kv[0] is None, kv[0] or "")
        )
    ]
    outcomes = [
        OutcomeBreakdown(
            status=status,
            spend_usd=acc.spend_usd,
            build_count=acc.build_count,
            unmeasured_build_count=acc.unmeasured_build_count,
            failure_reasons=reasons if status == BuildStatus.FAILED else [],
        )
        for status, acc in sorted(per_status.items(), key=lambda kv: _status_key(kv[0]))
    ]

    # Highest measured cost first. Tie-break: build id ascending, so two builds with
    # the same cost always come back in the same order (and `limit` cuts at the same
    # place) on every read.
    measured = [row for row in rows if row.cost_usd is not None]
    measured.sort(key=lambda row: (-row.cost_usd, row.id))
    top = measured[:limit]
    attribution = requester_ids_for_builds(db, [row.id for row in top])
    expensive = [
        ExpensiveBuild(
            build_id=row.id,
            project_id=row.project_id,
            type=row.type,
            status=row.status,
            failure_reason=row.failure_reason,
            cost_usd=row.cost_usd,
            cost_tokens=row.cost_tokens,
            ended_at=row.ended_at,
            requester_id=attribution.get(row.id),
        )
        for row in top
    ]

    return CostDashboard(
        window=window,
        limit=limit,
        days=days,
        outcomes=outcomes,
        expensive_builds=expensive,
        metering_caveat=metering_caveat_for(window),
    )
