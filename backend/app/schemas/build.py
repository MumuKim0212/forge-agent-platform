from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator, model_serializer

from app.schemas.policy import PolicyVerdictOut
from app.schemas.types import UtcDateTime


class ChangedFileOut(BaseModel):
    """One measured change: `path` is chosen by the agent (data, shown literally)."""

    path: str
    change: str  # "added" | "modified" | "deleted"


class ChangedCountsOut(BaseModel):
    """The totals of a build's change set and whether the stored list is truncated. All
    the timeline sends (every authenticated reader sees it): never a path."""

    totals: dict[str, int]
    truncated: bool


class ChangedFilesOut(ChangedCountsOut):
    """what the platform measured between the build's input and the sanitised
    output (`Build.changed_files`): up to 500 entries in path order plus the true totals."""

    entries: list[ChangedFileOut]


class SmokeRouteOut(BaseModel):
    """one route the platform called on the live preview. `path` passed the smoke check's
    character filter (so it is safe to render); `status` is an integer the platform read, null
    exactly when `outcome` is `no_answer`. No body is ever stored."""

    model_config = ConfigDict(extra="forbid")

    path: str
    status: int | None
    outcome: Literal["ok", "client_error", "server_error", "no_answer", "too_large"]


class SmokeChecksOut(BaseModel):
    """`Build.smoke_checks`: what the platform saw when it called the preview's own
    parameterless GET routes. Informational. Absent (None where it is used) = not measured, which
    is different from `routes: []` = measured, nothing to call."""

    model_config = ConfigDict(extra="forbid")

    routes: list[SmokeRouteOut]
    skipped: int
    truncated: bool


class SqlFindingOut(BaseModel):
    """One SQL statement PostgreSQL's parser rejected in a build's code (`sql_syntax_check`): the
    file (a plain relative path), the line, and one of the platform's fixed sentences. Never the
    statement or the parser's own message."""

    model_config = ConfigDict(extra="forbid")

    path: str
    line: int
    error: str


class RepairAttemptOut(BaseModel):
    """One entry of `Build.repair_routes`: what one repair attempt was started for, as the platform
    measured it. `routes` are the failing routes (the same shape and filter as a smoke route),
    `sql` the SQL statements the parser rejected (ADR 0047 §Amendment 2026-11-25); either may be
    empty but not both. `attempt` is the 1-based number of the repair attempt. An entry with no
    findings is served exactly as it is stored, without a `sql` key."""

    model_config = ConfigDict(extra="forbid")

    attempt: int
    routes: list[SmokeRouteOut]
    sql: list[SqlFindingOut] = []

    @model_serializer(mode="wrap")
    def _omit_empty_sql(self, handler):
        data = handler(self)
        if not data.get("sql"):
            data.pop("sql", None)
        return data


_REPAIR_ROUTES = TypeAdapter(list[RepairAttemptOut])


def served_repair_routes(stored: object) -> list[RepairAttemptOut] | None:
    """What `Build.repair_routes` is served as: the stored list when it is exactly the shape the
    executor writes, otherwise None. NULL stays None ("no repair was started", or a build from before
    the column). A stored value that is not a non-empty list of `{attempt, routes: [{path, status,
    outcome}], sql?: [{path, line, error}]}` with at least one route or one finding per entry is never
    passed through (a started repair is never an empty list, so an empty one is as malformed as a
    string): it is served as None, which the consumers read as "not recorded", never as "no failing
    routes"."""
    if stored is None:
        return None
    try:
        entries = _REPAIR_ROUTES.validate_python(stored, strict=True)
    except ValidationError:
        return None
    if not entries or any(not entry.routes and not entry.sql for entry in entries):
        return None
    return entries


class BuildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    type: str
    agent_session_id: str | None
    # ADR 0035: measured at claim. `model` is the CONFIGURED model sent to the runner
    # (None for an engine that runs its own default); the trace's per-call model is the
    # authoritative wire value. None on both = a build from before engines were recorded.
    engine: str | None = None
    model: str | None = None
    status: str
    diff_ref: str | None
    cost_tokens: int | None
    cost_usd: float | None
    started_at: UtcDateTime | None
    ended_at: UtcDateTime | None
    heartbeat_at: UtcDateTime | None
    failure_reason: str | None
    failure_detail: str | None
    image_ref: str | None
    preview_url: str | None
    # In-build repair attempts started (a platform-measured count; 0 = none).
    repair_attempts: int = 0
    # The failing routes each repair attempt was started for, as stored (platform-measured);
    # None = no repair was started, or a build from before the column (or a malformed stored value).
    repair_routes: list[RepairAttemptOut] | None = None
    # Platform-authored code for a preview that failed to start; None = it did not fail.
    preview_start_failure: str | None = None
    preview_expires_at: UtcDateTime | None
    created_at: UtcDateTime
    # None = not measured.
    changed_files: ChangedFilesOut | None = None
    protected_path_drift: list[str] | None = None
    # the agent's own text (L3-authored), bounded, as stored: only ever on a
    # SUCCEEDED build. An agent claim -- the frontend labels it as such and renders it as
    # plain text; no code path reads it except its display.
    agent_summary: str | None = None
    # the build's policy verdict, if the gate
    # has run for it yet (None until then — a flagged verdict is not a
    # failed build, and previews/timeline entries exist before the gate
    # runs).
    policy_verdict: PolicyVerdictOut | None = None
    # mirrors
    # `services/approval.is_deploy_eligible(build)` so callers (and the
    # frontend) can read the bit directly off `BuildOut` without
    # re-deriving the rule. False until the gate/approval routing has run.
    deploy_eligible: bool = False

    @field_validator("repair_routes", mode="before")
    @classmethod
    def _repair_routes_as_stored_or_none(cls, value: object) -> object:
        return served_repair_routes(value)
