"""Approval routing, ApprovalRequest lifecycle,
deploy eligibility.

Turns a build's policy verdict into a real `ApprovalRequest`, and implements
the human decision workflow (approve/reject), the supersede-on-newer-build
rule, and the one queryable deploy-eligibility helper the deploy step consumes.

This is the impure shell analogous to `services/policy_gate.py`: it writes
through the same `db` session the caller (`sandbox_build_executor.py` /
routers) is already using, and does not commit itself — the caller commits
as part of its own transaction, except for the
router-driven decision endpoints, which do own their transaction the same
way other routers do (see `routers/approvals.py`).

an approval count is a count
of DISTINCT approvers, and three layers make that true rather than hoped for.
`uq_approval_decisions_one_approve_per_actor` (a partial unique index) refuses a
second approve row by one actor; `decide` counts distinct actors, not rows; and
`decide` takes the request row's lock and re-reads the request under it, so approve,
reject and the worker's supersede on one request are serialised, not interleaved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.services.audit import write_audit_log
from app.services.policy_checks import PolicyRiskTier
from app.services.project_status import advance
from app.services.status import ProjectStatus


class SelfApprovalError(Exception):
    """Raised when the requester/owner of a change tries to approve their
    own request (spec: "Self-approval is forbidden ... even with the
    approver role")."""


class AmbiguousRequesterError(Exception):
    """A build maps to more than one `ChangeRequest`, so "who requested it" has
    no single answer.

    Why this is an error and not a choice: the self-approval ban forbids exactly
    one identity (the requester) besides the project owner. A resolver that
    quietly kept one of several requesters would let every other one approve
    their own build -- it fails open for them. `change_requests.build_id` is
    unique, so this cannot occur in a database at head; the error is the
    backstop for a missing or bypassed constraint, and its answer is to refuse,
    never to pick.

    The message names the build and the row count only -- never a requester id
    or request text -- because it can reach `Build.failure_detail`, which every
    authenticated reader of the timeline sees."""

    def __init__(self, build_id: str, count: int, *, other_builds: int = 0) -> None:
        self.build_id = build_id
        self.count = count
        self.other_builds = other_builds
        more = f" (and {other_builds} other build(s) in the same lookup)" if other_builds else ""
        super().__init__(
            f"build {build_id} has {count} change requests (expected at most one), "
            f"so its requester is ambiguous{more}"
        )


class InvalidDecisionError(Exception):
    """Raised for a decision that violates the lifecycle (deciding on a
    request that's already terminal) or is missing required input (a
    rejection with no comment)."""


def _required_approvals_for(risk_tier: str) -> int:
    if risk_tier == PolicyRiskTier.HIGH:
        return max(1, settings.policy_high_approvals_required)
    if risk_tier == PolicyRiskTier.MEDIUM:
        return max(1, settings.policy_medium_approvals_required)
    return 1


def requester_ids_for_builds(db: Session, build_ids: Sequence[str]) -> dict[str, str]:
    """The attribution join itself: `ChangeRequest.requester_id` for the
    `ChangeRequest` whose `build_id` is that build, for many builds at once and
    **without** `resolve_requester_for_build`'s owner fallback. A build id
    absent from the returned mapping has no resolvable requester.

    Two consumers, one spelling of the join, because they need opposite things
    from the same gap:

    - the self-approval ban (`resolve_requester_for_build`, below) must never
      fail open, so it composes the project-owner fallback on top;
    - the per-user cost rollup (`services/usage.py`) must **not** fall
      back to the owner — unattributed spend is its own reported line, and
      folding it into an owner would make per-user totals stop reconciling
      against per-project ones).

    **Refuses ambiguity**: if any build id maps to more than one
    `ChangeRequest` it raises `AmbiguousRequesterError` rather than letting one
    row win -- this is the one place the join is spelled, so it covers both
    consumers. A database at head cannot reach it (unique `build_id`).

    One `IN` query rather than one per build: at the stated volume
    (hundreds of builds per month) that is the whole reason a derived rollup
    needs no materialized table."""
    if not build_ids:
        return {}
    rows = db.execute(
        select(ChangeRequest.build_id, ChangeRequest.requester_id)
        .where(ChangeRequest.build_id.in_(build_ids))
        .order_by(ChangeRequest.build_id)
    ).all()
    counts = Counter(build_id for build_id, _ in rows)
    ambiguous = sorted(build_id for build_id, count in counts.items() if count > 1)
    if ambiguous:
        raise AmbiguousRequesterError(
            ambiguous[0], counts[ambiguous[0]], other_builds=len(ambiguous) - 1
        )
    return {build_id: requester_id for build_id, requester_id in rows}


def resolve_requester_for_build(db: Session, *, build_id: str, project_owner_id: str) -> str:
    """Who actually asked for this build.

    "The requester of a build is the requester of the `ChangeRequest`
    referencing that build." Exactly one such row exists per API-created
    build — `routers/projects._create_change_request_and_build` writes the
    `ChangeRequest` and the `Build` in the same operation and sets
    `ChangeRequest.build_id`. The FK points that way round, so this reverse
    lookup is the whole mechanism: `Build` needs no `requester_id` column,
    and adding one would be a second source of truth for the same fact.

    The caller used to pass `project.owner_id` as a stand-in. That was
    sound only while requester == owner; with real users they diverge and the
    self-approval ban silently stopped covering the requester (which is a
    security-boundary defect).

    **Never fails open**, for two different reasons:

    - *no* `ChangeRequest` found (a build created outside the API path): it
      returns the project owner, so the ban is still evaluated against a real
      identity rather than skipped. That fallback is this function's own
      contribution -- the join lives in `requester_ids_for_builds` above, which
      reports the gap as absence instead (the cost rollup needs it that way; the ban does
      not);
    - *several* found: `requester_ids_for_builds` raises
      `AmbiguousRequesterError` and this function lets it propagate. It must not
      fall back to the owner here -- that would silently drop every requester
      -- and it must not pick one. The caller refuses to create an approval
      request from such a build."""
    return requester_ids_for_builds(db, [build_id]).get(build_id) or project_owner_id


def create_approval_request_for_verdict(
    db: Session,
    *,
    build: Build,
    verdict: PolicyVerdict,
    project_id: str,
    requester_id: str,
) -> ApprovalRequest:
    """every policy verdict creates
    exactly one ApprovalRequest linked to the build. `low` tier is
    AUTO_APPROVED (terminal at creation, audit trail stays uniform across
    tiers); `medium`/`high` are PENDING with `required_approvals` snapshotted
    from the current Settings."""
    status = (
        ApprovalStatus.AUTO_APPROVED
        if verdict.risk_tier == PolicyRiskTier.LOW
        else ApprovalStatus.PENDING
    )

    request = ApprovalRequest(
        build_id=build.id,
        project_id=project_id,
        requester_id=requester_id,
        policy_verdict_id=verdict.id,
        risk_tier=verdict.risk_tier,
        status=status,
        required_approvals=_required_approvals_for(verdict.risk_tier),
    )
    db.add(request)
    db.flush()  # assign request.id for the audit target below

    write_audit_log(
        db,
        actor_id=None,  # worker-driven, no human actor (same convention as policy_gate.py)
        action="approval_request.created",
        target=f"approval_request:{request.id}",
        metadata={"build_id": build.id, "risk_tier": verdict.risk_tier, "status": status},
    )

    # routing that
    # leaves the request PENDING (medium/high) is the project waiting on a
    # human. An AUTO_APPROVED build never passes through AWAITING_APPROVAL.
    if status == ApprovalStatus.PENDING:
        project = db.get(Project, project_id)
        if project is not None:
            advance(db, project, ProjectStatus.AWAITING_APPROVAL, cause=f"build:{build.id}")

    superseded = supersede_older_pending_requests(db, project_id=project_id, newer_build_id=build.id)
    for old in superseded:
        write_audit_log(
            db,
            actor_id=None,
            action="approval_request.superseded",
            target=f"approval_request:{old.id}",
            metadata={"superseded_by_build_id": build.id},
        )

    return request


def supersede_older_pending_requests(
    db: Session, *, project_id: str, newer_build_id: str
) -> list[ApprovalRequest]:
    """spec: "A newer successful build of the same project marks older
    un-actioned PENDING requests SUPERSEDED (not deleted)." Only touches
    other requests for the same project that are still PENDING and are not
    the newer build's own request.

    ONE `UPDATE ... WHERE status = 'PENDING' ... RETURNING`, not a read of the
    PENDING rows followed by a write to each. The old read-then-write could overwrite a
    decision that committed in between (a request approved a moment after the read ended
    up SUPERSEDED, losing the approval). As a single statement it takes each matching row's
    lock, waits for a decider that holds it, and re-checks `status = 'PENDING'` once it
    has the row (Postgres re-evaluates the condition on the committed row; on SQLite the
    statement holds the one writer lock for its whole run), so a request that was decided
    first is simply not matched and comes back out of the list."""
    now = datetime.now(timezone.utc)
    statement = (
        update(ApprovalRequest)
        .where(
            ApprovalRequest.project_id == project_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.build_id != newer_build_id,
        )
        .values(
            status=ApprovalStatus.SUPERSEDED,
            superseded_by_build_id=newer_build_id,
            superseded_at=now,
        )
        .returning(ApprovalRequest)
        # The rows come back with their new values, replacing any copy of them this session
        # already holds (a request loaded earlier in the same transaction).
        .execution_options(populate_existing=True)
    )
    return list(db.scalars(statement))


def _is_self_approval(request: ApprovalRequest, *, actor_id: str, project_owner_id: str) -> bool:
    """spec: "the requester/owner of the change cannot approve their own
    request, even with the approver role." Both the original requester and
    the build's project owner are blocked — either one initiating the
    change and then rubber-stamping it defeats the point of a second set of
    eyes."""
    return actor_id in (request.requester_id, project_owner_id)


def _lock_request(db: Session, request: ApprovalRequest) -> None:
    """Takes the request row's lock and re-reads its state under it.

    Everything `decide` checks (is it still PENDING? has this actor already approved?) must be
    read AFTER this, because the router loaded `request` before calling `decide`, and anything
    that committed since then -- another decider, the worker's supersede -- is not in that
    object. Without the lock two deciders both read PENDING and both write; without the
    re-read the lock only makes the second wait before acting on the stale status.

    Postgres: `SELECT ... FOR UPDATE` on the request row. Concurrent deciders (and the
    supersede's `UPDATE`) wait for the holder's commit or rollback, and the re-read after the
    wait returns the committed state (READ COMMITTED), so the loser sees `APPROVED`/`REJECTED`/
    `SUPERSEDED` and is refused.

    SQLite ignores `FOR UPDATE` -- it has no row locks. What it has is ONE writer for the
    whole database: a transaction that has begun writing holds the write lock until it ends
    and any other writer waits (then fails with "database is locked" past the busy timeout).
    So on SQLite the lock step is a no-op write to the request row (`status = status`), which
    begins the writing transaction up front; from here to the commit nobody else can write, and
    the re-read that follows returns the latest committed state. Without it SQLite would not
    serialise at all: its reads do not hold a snapshot, so two deciders could both read PENDING
    and both insert one after the other. (Proven both ways in
    `tests/test_approval_one_decision_per_actor.py`; SQLite is the dev engine, Postgres is
    production.)

    Flushes first so pending changes on `request` survive the re-read."""
    db.flush()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request.id)
            .values(status=ApprovalRequest.status)
            .execution_options(synchronize_session=False)
        )
    db.refresh(request, attribute_names=["status", "required_approvals"], with_for_update=True)


def _has_approved(db: Session, *, request_id: str, actor_id: str) -> bool:
    """The friendly early refusal: has this actor already approved this request? Not the guarantee
    (that is the unique index); it exists so the common case gets a clear message without an
    INSERT that fails."""
    return (
        db.query(ApprovalDecision)
        .filter_by(approval_request_id=request_id, actor_id=actor_id, decision="approve")
        .count()
        > 0
    )


def _already_approved_error() -> InvalidDecisionError:
    return InvalidDecisionError("this approver has already approved this request")


def decide(
    db: Session,
    request: ApprovalRequest,
    *,
    actor_id: str,
    project_owner_id: str,
    decision: str,
    comment: str | None,
) -> ApprovalRequest:
    """Applies one approve/reject decision. Raises `SelfApprovalError` /
    `InvalidDecisionError` rather than an HTTPException — the router maps
    those to the right status codes (HTTP
    concerns in the router, not the service). Does not commit; the caller
    commits (see module docstring).

    the request is locked and re-read before anything is checked
    (`_lock_request`), and the lock is held until the caller commits or rolls back.

    If the INSERT meets `uq_approval_decisions_one_approve_per_actor` (the duplicate check
    ran before another approval by this actor committed -- only possible where the lock did
    not serialise them), it is reported as the same "already approved" refusal, not a 500.
    **That path rolls the session back** (a flush that raised leaves the transaction unusable,
    the same rule as `deploy._insert_deploying`), so the caller's other pending work in this
    transaction is discarded. It is translated only when this actor's approve row really exists
    afterwards; any other integrity failure (a missing foreign key, say) propagates unchanged."""
    _lock_request(db, request)

    if request.status in ApprovalStatus.TERMINAL:
        raise InvalidDecisionError(f"approval request {request.id} is already {request.status}")

    if _is_self_approval(request, actor_id=actor_id, project_owner_id=project_owner_id):
        raise SelfApprovalError("requester/project owner cannot approve their own request")

    if decision == "reject":
        if not comment or not comment.strip():
            raise InvalidDecisionError("a rejection requires a non-empty comment")
    elif decision != "approve":
        raise InvalidDecisionError(f"unknown decision {decision!r}")

    if decision == "approve" and _has_approved(db, request_id=request.id, actor_id=actor_id):
        raise _already_approved_error()

    request_id = request.id
    db.add(
        ApprovalDecision(
            approval_request_id=request_id,
            actor_id=actor_id,
            decision=decision,
            comment=comment,
        )
    )
    try:
        db.flush()  # make the new decision row visible to the count below
    except IntegrityError as exc:
        db.rollback()
        if decision == "approve" and _has_approved(db, request_id=request_id, actor_id=actor_id):
            raise _already_approved_error() from exc
        raise

    if decision == "reject":
        request.status = ApprovalStatus.REJECTED
    else:
        # Distinct ACTORS, counted in the database (not the possibly-stale `request.decisions`
        # relationship, and not rows): the number the rule is about is how many different
        # people approved. The unique index already makes rows and actors equal in a database
        # at head; this stays correct without it (a restore from before the migration).
        approve_count = db.scalar(
            select(func.count(func.distinct(ApprovalDecision.actor_id))).where(
                ApprovalDecision.approval_request_id == request_id,
                ApprovalDecision.decision == "approve",
            )
        )
        if approve_count >= request.required_approvals:
            request.status = ApprovalStatus.APPROVED

    write_audit_log(
        db,
        actor_id=actor_id,
        action=f"approval_request.{decision}d" if decision == "approve" else "approval_request.rejected",
        target=f"approval_request:{request_id}",
        metadata={"comment": comment, "resulting_status": request.status},
    )

    return request


def is_deploy_eligible(build: Build) -> bool:
    """"A build is deploy-eligible
    iff its ApprovalRequest is APPROVED or AUTO_APPROVED." The deploy step consumes
    this helper rather than re-deriving the rule; `build.approval_request`
    is `None` until the gate has run for this build (deploy-ineligible by
    definition until then)."""
    request = build.approval_request
    if request is None:
        return False
    return request.status in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED)
