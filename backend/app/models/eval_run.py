"""Eval runs and their cases.

A run is one admin-triggered pass of the versioned request set over real builds, on the
engine and model selected when it started, bounded by a per-run budget. A case is one entry
of the set within one run: it owns the eval project and the create build it queued, and
records how it was judged.

Platform state only. Nothing in a run or a case is an agent claim: the result comes from the
build's status, the preview's `/healthz` and the platform's own HTTP checks, and the cost is
copied from the build's `cost_usd`, which the LLM proxy metered (ADR 0014). The run's `spend_usd`
is derived from those case costs by `services/eval_runner`, the single writer.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin, _utcnow


class EvalRunStatus:
    """`RUNNING -> COMPLETED | STOPPED_BUDGET | FAILED`."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    STOPPED_BUDGET = "STOPPED_BUDGET"
    FAILED = "FAILED"


class EvalCaseState:
    """Where a case is in its own life. `PENDING` -> `RUNNING` (its build is queued or running)
    -> `DONE` (judged, or not run). A `DONE` case is never touched again: that is what keeps a
    restarted worker from judging it twice."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"


class EvalCaseResult:
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUN_BUDGET = "NOT_RUN_BUDGET"


class EvalFailureReason:
    """A failed case's first failing reason. A failed build
    records its own `BuildFailureReason` code instead; `NOT_RUN_BUDGET` is also a result."""

    PREVIEW_UNHEALTHY = "PREVIEW_UNHEALTHY"
    CHECK_FAILED = "CHECK_FAILED"
    NOT_RUN_BUDGET = "NOT_RUN_BUDGET"
    # A case that was in flight when the run was ended as stuck.
    RUN_STUCK = "RUN_STUCK"
    # The case's build ran on a different engine/model than the run's: the selection moved between
    # queueing and claim (ADR 0035 §5 resolves the engine at claim). The case is not judged, so a
    # run never reports a case under an engine it did not run on.
    ENGINE_CHANGED = "ENGINE_CHANGED"


class EvalRun(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "eval_runs"
    # At most one run in progress, enforced by the database on top of the API's 409 (same
    # shape as the one-ACTIVE-deployment index): two concurrent starts cannot both win.
    __table_args__ = (
        Index(
            "uq_eval_runs_one_running",
            "status",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    status: Mapped[str] = mapped_column(default=EvalRunStatus.RUNNING)
    # The engine and model selected when the run started, recorded once; the run never
    # switches. `model` is NULL for an engine that runs its own default.
    engine: Mapped[str]
    model: Mapped[str | None] = mapped_column(nullable=True)
    request_set_version: Mapped[int] = mapped_column(Integer)
    # a short fingerprint of the system prompt a create build would get at run start,
    # computed by the same composer the executor uses. NULL on runs from before it existed.
    prompt_fingerprint: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # The effective `BUILD_REPAIR_MAX_ATTEMPTS` when the run started, so a run
    # with the repair loop on and one with it off are distinguishable. NULL is "not recorded"
    # (every run from before the column) and is never read as 0.
    repair_max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # `EVAL_RUN_BUDGET_USD` at the moment the run started.
    budget_usd: Mapped[float] = mapped_column(Float)
    # Sum of the finished cases' metered cost; a NULL (unmeasured) cost adds nothing here and
    # exhausts the remaining budget instead (`services/eval_runner.remaining_budget`).
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Refreshed whenever a case advances; the stuck-run check reads it.
    last_progress_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Platform-authored sentence for a `FAILED` run (or the budget stop); never L3 text.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalCase(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_eval_cases_run_case"),
        UniqueConstraint("run_id", "position", name="uq_eval_cases_run_position"),
    )

    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id"))
    # The request set's stable case id, and the case's order in it.
    case_id: Mapped[str]
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(default=EvalCaseState.PENDING)
    # NULL until judged (or marked not run).
    result: Mapped[str | None] = mapped_column(nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(nullable=True)
    # For `CHECK_FAILED`: the zero-based index of the first failing check.
    failed_check_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # One entry per check made, platform-authored: {index, method, path, expected_status,
    # status (the actual one, or null), ok, outcome}. Never a response body.
    check_outcomes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # indexed -- Postgres does not index a foreign key, and it checks this one whenever
    # a project row is deleted (the deprovision path, `demo_data --reset`).
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    build_id: Mapped[str | None] = mapped_column(ForeignKey("builds.id"), nullable=True)
    # Copied from the case's build when it is judged: the proxy's metered figures.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many repair attempts the case's build ran (`builds.repair_attempts`, copied when
    # the case is judged). NULL means not measured (not run, pending, stuck, or from before the
    # column) and is never 0: 0 is a measurement ("the first attempt shipped, or the loop was off").
    repair_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
