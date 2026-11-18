"""Usage read API.

Every usage response carries its `MonthWindowOut`: spec rule 3 requires a
response to state the month and timezone it was computed for, so a number can
never be mistaken for a different window.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict

from app.schemas.types import UtcDateTime


class MonthWindowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    month: str  # "YYYY-MM"
    timezone: str  # the platform timezone the boundaries were evaluated in
    starts_at: UtcDateTime
    ends_at: UtcDateTime  # exclusive


class SubjectUsageOut(BaseModel):
    """One rollup line. `unmeasured_build_count` is what keeps a NULL
    `cost_usd` from reading as a measured `$0.00`: sums treat NULL as zero,
    so a nonzero count here says "part of this
    window was never measured" rather than leaving the reader to assume it
    was."""

    model_config = ConfigDict(from_attributes=True)

    kind: str  # services/usage.UsageSubject
    subject_id: str | None  # None only on the unattributed line
    spend_usd: float
    build_count: int
    unmeasured_build_count: int
    # None only on the unattributed line — it has no subject to hold a limit.
    effective_limit_usd: float | None
    limit_source: str | None  # "override" | "default"
    in_breach: bool  # derived comparison, never stored


class SubjectUsageReportOut(BaseModel):
    window: MonthWindowOut
    subject: SubjectUsageOut


class UsageReportOut(BaseModel):
    window: MonthWindowOut
    subjects: list[SubjectUsageOut]


class PlatformUsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    window: MonthWindowOut
    total_spend_usd: float
    unattributed_spend_usd: float
    build_count: int
    unmeasured_build_count: int
    default_limit_usd: float
    projects_in_breach: list[str]
    users_in_breach: list[str]


class QuotaUpdate(BaseModel):
    """Input for the admin override endpoints. `null` **clears** the override
    back to the global default — which is not unlimited;
    `0.0` is a legal limit meaning "any spend is a breach". Only
    negative values are rejected, in `services/usage.set_monthly_quota`."""

    monthly_quota_usd: float | None


class QuotaOut(BaseModel):
    subject_kind: str
    subject_id: str
    monthly_quota_usd: float | None  # the stored override; None = no override
    effective_limit_usd: float  # what the override resolves to right now
    limit_source: str


class DailyBucketOut(BaseModel):
    """One calendar day in the platform timezone.
    `failed_spend_usd` is the part of `spend_usd` that came from FAILED
    builds."""

    model_config = ConfigDict(from_attributes=True)

    day: date
    spend_usd: float
    build_count: int
    unmeasured_build_count: int
    failed_spend_usd: float


class FailureReasonBreakdownOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    failure_reason: str | None  # None: FAILED with no recorded reason
    spend_usd: float
    build_count: int
    unmeasured_build_count: int


class OutcomeBreakdownOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    spend_usd: float
    build_count: int
    unmeasured_build_count: int
    failure_reasons: list[FailureReasonBreakdownOut]  # empty unless FAILED


class ExpensiveBuildOut(BaseModel):
    """One entry of the most-expensive list. `cost_usd` is never null here: an
    unmeasured build is not listed."""

    model_config = ConfigDict(from_attributes=True)

    build_id: str
    project_id: str
    type: str
    status: str
    failure_reason: str | None
    cost_usd: float
    cost_tokens: int | None
    ended_at: UtcDateTime
    requester_id: str | None  # None: unattributed


class MeteringCaveatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date  # a backend constant, not a setting
    window_starts_before: bool


class CostDashboardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    window: MonthWindowOut
    limit: int  # the N the expensive list was cut at
    days: list[DailyBucketOut]
    outcomes: list[OutcomeBreakdownOut]
    expensive_builds: list[ExpensiveBuildOut]
    metering_caveat: MeteringCaveatOut
