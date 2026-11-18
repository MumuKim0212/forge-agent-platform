from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IdMixin


class ApprovalStatus:
    """ApprovalRequest lifecycle.

    ```
    PENDING -> APPROVED | REJECTED | SUPERSEDED   (human tiers)
    AUTO_APPROVED                                 (low tier, terminal at creation)
    ```
    `REJECTED`/`SUPERSEDED`/`AUTO_APPROVED` are all terminal. `PENDING` is the
    only non-terminal state.
    """

    PENDING = "PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"

    TERMINAL = (AUTO_APPROVED, APPROVED, REJECTED, SUPERSEDED)


class ApprovalRequest(IdMixin, CreatedAtMixin, Base):
    """extends the slice-1 plumbing-only table (it first got
    real callers). One row per build's policy verdict —
    created in the same transaction as the
    `PolicyVerdict` that triggers it.

    Shape decisions (see task Result for full justification):
    - `project_id`/`requester_id` are denormalized off the build/project so
      the approval console's list query doesn't
      need a join through `Build`/`ChangeRequest` just to show "who/what
      project" — cheap to keep in sync since this row is created once and
      never re-derived.
    - `required_approvals` snapshots `Settings.policy_medium_approvals_
      required`/`policy_high_approvals_required` *at creation time* so a
      later config change (spec: "raising high to 2 is a config change, not
      code") never silently reinterprets an already-decided or in-flight
      request.
    - Individual decisions live in `ApprovalDecision` (below), not as single
      `approver_id`/`decision` columns on this row — the task's out-of-scope
      note allows a minimal count>1 implementation only if it is not wrong;
      storing approvals as rows and gating on `required_approvals` is the
      only way to support count>1 correctly (a single-approver-id column
      cannot represent "2 different people approved"), so that's what this
      does even though every configured default today is 1.
    """

    __tablename__ = "approval_requests"

    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"), unique=True)
    # indexed -- `supersede_older_pending_requests` filters on it for every new verdict,
    # and Postgres does not index a foreign key by itself.
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    requester_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    policy_verdict_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_verdicts.id"), nullable=True
    )

    # One of PolicyRiskTier ("low" | "medium" | "high") — copied from the
    # triggering PolicyVerdict at creation time.
    risk_tier: Mapped[str] = mapped_column(default="unknown")
    status: Mapped[str] = mapped_column(default=ApprovalStatus.PENDING)
    required_approvals: Mapped[int] = mapped_column(Integer, default=1)

    # Superseded bookkeeping (spec: "A newer successful build of the same
    # project supersedes older un-actioned PENDING requests"). Nullable:
    # only set when status transitions to SUPERSEDED.
    superseded_by_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("builds.id"), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    decisions: Mapped[list["ApprovalDecision"]] = relationship(
        "ApprovalDecision", order_by="ApprovalDecision.created_at", lazy="selectin"
    )

    # The triggering PolicyVerdict (with its per-check results), for the
    # approval console's detail view (it shows
    # per-check results with findings). lazy="selectin"
    # so `ApprovalRequestOut`'s from_attributes serialization always has it.
    policy_verdict: Mapped["PolicyVerdict | None"] = relationship(
        "PolicyVerdict", uselist=False, lazy="selectin", viewonly=True
    )


class ApprovalDecision(IdMixin, CreatedAtMixin, Base):
    """One row per human approve/reject action on an ApprovalRequest. Kept
    separate from ApprovalRequest itself so `required_approvals` > 1 is a
    correct count of distinct approvers, not a single overwritable column.
    Append-only: no code path updates or deletes a decision row. (Two deliberate
    exceptions, neither application code: migration `d2c8f5a71e39` removed the later
    duplicates of one actor's approve decision, auditing each; and `demo_data --reset`
    removes the demo dataset's rows.)"""

    __tablename__ = "approval_decisions"

    # one actor holds at most one APPROVE
    # decision per request, so an approval count is a count of distinct approvers and two
    # parallel clicks by one approver can never satisfy a two-person rule. Partial, because a
    # reject is terminal (the request takes one) and an approver who has approved a still-PENDING
    # request may reject it later -- an index over every decision would forbid that.
    __table_args__ = (
        Index(
            "uq_approval_decisions_one_approve_per_actor",
            "approval_request_id",
            "actor_id",
            unique=True,
            sqlite_where=text("decision = 'approve'"),
            postgresql_where=text("decision = 'approve'"),
        ),
    )

    approval_request_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # "approve" | "reject".
    decision: Mapped[str]
    # Required (non-empty) for "reject"; optional for "approve".
    comment: Mapped[str | None] = mapped_column(nullable=True)
