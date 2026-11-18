"""Check catalog and risk tiers.

Persists the policy gate's verdict for a build: one `PolicyVerdict` row (the
computed risk tier) plus five `PolicyCheckResult` rows (one per check in the
catalog) linked to it. Both are written once, together, right after a
build's publish/preview steps succeed — verdicts are immutable per build
(spec: "No re-running checks on an unchanged build"), so no code path ever
updates these rows after creation, only inserts them.

Kept as dedicated tables rather than reusing `ApprovalRequest.policy_results`
(a JSON blob) because per-check findings need to be individually queryable
for the approval console (per-check results and findings)
without parsing JSON on every read.
"""

from sqlalchemy import ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IdMixin


class PolicyVerdict(IdMixin, CreatedAtMixin, Base):
    """One verdict per build (at most one — enforced by the caller never
    running the gate twice for the same build, not by a DB constraint, to
    keep this additive/simple). `created_at`
    (`CreatedAtMixin`) is the verdict timestamp shown in the timeline."""

    __tablename__ = "policy_verdicts"

    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"), unique=True)
    # One of PolicyRiskTier ("low" | "medium" | "high").
    risk_tier: Mapped[str]

    checks: Mapped[list["PolicyCheckResult"]] = relationship(
        "PolicyCheckResult", order_by="PolicyCheckResult.check_name", lazy="selectin"
    )


class PolicyCheckResult(IdMixin, Base):
    """One row per check per verdict (five per build). No `CreatedAtMixin`
    timestamp of its own — all five share the parent verdict's `created_at`
    for ordering purposes; append-only like `AuditLog`."""

    __tablename__ = "policy_check_results"

    verdict_id: Mapped[str] = mapped_column(ForeignKey("policy_verdicts.id"))
    # One of PolicyCheckName (e.g. "secrets_pii", "dependency_vulnerabilities",
    # "egress_detection", "resource_limits", "template_compliance").
    check_name: Mapped[str]
    # "pass" | "flag" (PolicyCheckStatus).
    result: Mapped[str]
    summary: Mapped[str]
    # Machine-readable findings; redacted/truncated before storage (never a
    # full secret value — secrets hygiene).
    findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
