from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IdMixin
from app.services.status import BuildStatus


class Build(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "builds"

    # `ix_builds_status_created_at` is the worker's claim, every poll:
    # `WHERE status = 'QUEUED' ORDER BY created_at` (the leading `status` also answers the
    # RUNNING count and the sweeps). `project_id` and `ended_at` are `index=True` below.
    __table_args__ = (Index("ix_builds_status_created_at", "status", "created_at"),)

    # indexed -- the timeline, `has_work_in_flight`, the latest-successful-build lookup,
    # the per-project usage rollup, and the joins from verdicts/approval requests to a project.
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str]  # "create" | "modify"
    agent_session_id: Mapped[str | None] = mapped_column(nullable=True)
    # ADR 0035 §Decision 5: written when the worker claims the build, by
    # `worker.assign_engine` from `services/engines.resolve_engine`. Measured facts,
    # never agent claims. `engine` is `anthropic` | `openai`; `model` is the model the
    # runner was configured with (NULL for an engine that runs its own default). The
    # trace's per-call model is the authoritative wire value and can differ (the API
    # reports a dated snapshot). NULL on both = a build from before engines were recorded.
    engine: Mapped[str | None] = mapped_column(nullable=True)
    model: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(default=BuildStatus.NOT_IMPLEMENTED)
    diff_ref: Mapped[str | None] = mapped_column(nullable=True)
    cost_tokens: Mapped[int | None] = mapped_column(nullable=True)
    # USD cost recorded from the LLM proxy's usage accounting at
    # build end (success or failure) alongside cost_tokens. Nullable: only
    # populated for builds that actually ran an agent (sandbox executor).
    # `Float` is explicit on purpose: a bare `Mapped[float]` takes its SQL
    # type from SQLAlchemy's annotation map -- `Float` in 2.0, `Double` in 2.1 --
    # and the migrations built `Float`. Same for both `monthly_quota_usd` columns.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # the per-build cost ceiling this build's proxy token is issued with.
    # NULL = the setting (`BUILD_COST_BUDGET_USD`), which is every normal build; an eval case's
    # build carries `min(BUILD_COST_BUDGET_USD, what is left of the run budget)`. Written when
    # the build is created, never by the executor or the engine.
    cost_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # indexed -- the monthly usage windows filter `ended_at >= start AND ended_at < end`.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # worker touches this while RUNNING; the timeout sweep uses
    # started_at (not heartbeat_at) against the max build duration, but a
    # future liveness check (worker crash vs. slow step) can use this too.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # One reason code from the failure taxonomy
    # (BuildFailureReason); set only when status == FAILED.
    failure_reason: Mapped[str | None] = mapped_column(nullable=True)
    # the platform-authored sentence that goes with failure_reason
    #. Set only
    # when status == FAILED, always by `build_queue.mark_failed`, which bounds
    # it. Never L3-authored text and never free-form library exception text.
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # what the AGENT wrote
    # about its own work (`.forge/result.json`), L3-authored. Written only when the build
    # ends SUCCEEDED, by `build_queue.mark_succeeded`, bounded there and stored as received
    # (no stripping, no escaping -- rendering makes it safe). NULL on every other build.
    # It is an agent claim: displayed after the measured facts, never an input to any
    # decision (an `ast` test pins which files may read it).
    agent_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # the OCI image tag image assembly produced for a successful
    # publish (e.g. "forge-build-<project>-<build>"). Nullable: only
    # populated for builds that reached the publish step successfully.
    image_ref: Mapped[str | None] = mapped_column(nullable=True)
    # what the platform
    # MEASURED about the agent's change, between the build's input tree and the sanitised
    # tree, before protected-path restore (`services/change_set.py`). `changed_files` is
    # `{"entries": [{"path", "change"}], "totals": {"added", "modified", "deleted"},
    # "truncated": bool}` (<= 500 entries; the paths are agent-chosen strings);
    # `protected_path_drift` is the list of DECLARED protected entries the agent changed.
    # NULL on both = not measured (the build never reached the sanitised tree), which is
    # distinct from an empty list / zero totals = measured, nothing changed.
    changed_files: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    protected_path_drift: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # what the PLATFORM saw when it
    # called the live preview's own parameterless GET routes (`services/preview_smoke.py`):
    # `{"routes": [{"path", "status", "outcome"}], "skipped": int, "truncated": bool}`. The
    # paths passed a strict character filter; `status` is an integer the platform read (null
    # exactly when `outcome` is `no_answer`); no response body is stored. NULL = not measured
    # (no preview, or the check could not look), which is distinct from a measured result with
    # zero routes. Informational only: never an input to the build's status or to an approval.
    smoke_checks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ADR 0047 §5: how many in-build repair attempts the platform STARTED for this build
    # (a repair is a new single-use sandbox run on the same build, token and budget, started
    # because the preview's smoke check measured a failing route). A measured fact set by the
    # platform, never by the agent; 0 = the first attempt shipped, or repair is off. It counts
    # attempts that started, including one that was then discarded.
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # ADR 0047 §Amendment 2026-11-19: the failing routes each repair attempt was started for,
    # `[{"attempt": n, "routes": [{"path", "status", "outcome"}]}]`, one entry per attempt started
    # (so `len == repair_attempts` whenever it is not NULL), written with the count from the same
    # `failing_routes` list the repair prompt is composed from. Platform-measured, safe-filtered
    # paths only: never a body, a log line, a trace excerpt or agent text. NULL = no repair attempt
    # was started, or a build from before the column; never an empty list.
    repair_routes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # ADR 0016: TTL preview container. `preview_url` is the signed,
    # expiring URL the frontend shows on the project detail page (never a
    # raw secret beyond what the URL itself grants); `preview_expires_at` is
    # the token's expiry (<= preview_ttl_minutes from issuance);
    # `preview_token_hash` stores only a SHA-256 hash of the signed token —
    # never the raw token — so a DB read alone can't grant preview access.
    # All nullable: only populated for a build whose preview is currently
    # live (replaced/torn down -> cleared back to None).
    preview_url: Mapped[str | None] = mapped_column(nullable=True)
    # the platform-authored code (`status.PreviewStartFailure`) for a preview that failed
    # to start; NULL = it started, or was never attempted. Deliberately NOT among the columns
    # `build_queue.clear_preview_fields` nulls, so "failed to start" stays distinguishable from
    # "expired". Never exception text.
    preview_start_failure: Mapped[str | None] = mapped_column(nullable=True)
    preview_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preview_token_hash: Mapped[str | None] = mapped_column(nullable=True)

    # the build's policy verdict, if the gate
    # has run yet. `uselist=False` — at most one verdict per build (see
    # PolicyVerdict.build_id's unique=True). lazy="joined" so BuildOut's
    # from_attributes serialization (routers/projects.py, worker.py) never
    # needs a caller to remember to eager-load this relationship.
    policy_verdict: Mapped["PolicyVerdict | None"] = relationship(
        "PolicyVerdict", uselist=False, lazy="joined", viewonly=True
    )

    # the ApprovalRequest created from this
    # build's policy verdict, if the gate (and approval routing) has run
    # yet. `uselist=False` — at most one per build (`ApprovalRequest.
    # build_id` is unique=True). lazy="joined" so `BuildOut`/`services/
    # approval.is_deploy_eligible` never need a caller to remember to
    # eager-load this relationship. Explicit `primaryjoin`/`foreign_keys`:
    # approval_requests has a second FK to builds (`superseded_by_build_id`),
    # so the join would otherwise be ambiguous.
    approval_request: Mapped["ApprovalRequest | None"] = relationship(
        "ApprovalRequest",
        primaryjoin="Build.id == ApprovalRequest.build_id",
        foreign_keys="ApprovalRequest.build_id",
        uselist=False,
        lazy="joined",
        viewonly=True,
    )

    @property
    def deploy_eligible(self) -> bool:
        """"a build is
        deploy-eligible iff its ApprovalRequest is APPROVED or
        AUTO_APPROVED." A plain Python property (not a mapped column) so
        `BuildOut.model_validate(build, from_attributes=True)` picks it up
        without a migration; the single source of truth for the rule itself
        is `services/approval.is_deploy_eligible` — this property delegates
        to it so the rule is never duplicated."""
        from app.services.approval import is_deploy_eligible

        return is_deploy_eligible(self)
