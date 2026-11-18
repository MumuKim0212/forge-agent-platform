"""Tests for `app/services/approval.py`: turning a policy verdict
into a real ApprovalRequest, the approve/reject decision workflow
(self-approval forbidden, reject-requires-comment), the supersede-on-newer-
build rule, and the deploy-eligibility helper. Uses the standard in-memory
SQLite `db_session` fixture (tests/conftest.py) — no Docker, no network."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.services.approval import (
    InvalidDecisionError,
    SelfApprovalError,
    create_approval_request_for_verdict,
    decide,
    is_deploy_eligible,
    supersede_older_pending_requests,
)
from app.services.policy_checks import PolicyRiskTier
from app.services.status import ProjectStatus


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="approval-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _build(db_session, project):
    build = Build(project_id=project.id, type="create")
    db_session.add(build)
    db_session.flush()
    return build


def _verdict(db_session, build, risk_tier):
    verdict = PolicyVerdict(build_id=build.id, risk_tier=risk_tier)
    db_session.add(verdict)
    db_session.flush()
    return verdict


def _approver(db_session):
    return db_session.query(User).filter_by(role="approver").first()


def _admin(db_session):
    return db_session.query(User).filter_by(role="admin").first()


# ---------------------------------------------------------------------------
# Creation / auto-approve vs pending
# ---------------------------------------------------------------------------


def test_low_tier_is_auto_approved(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.LOW)

    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    assert request.status == ApprovalStatus.AUTO_APPROVED
    assert request.build_id == build.id


@pytest.mark.parametrize("tier", [PolicyRiskTier.MEDIUM, PolicyRiskTier.HIGH])
def test_medium_and_high_tier_are_pending(db_session, project_row, tier):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, tier)

    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    assert request.status == ApprovalStatus.PENDING
    assert request.required_approvals >= 1


def test_creation_is_audit_logged(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.HIGH)

    create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    entries = (
        db_session.query(AuditLog).filter(AuditLog.action == "approval_request.created").all()
    )
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Routing config (threshold logic)
# ---------------------------------------------------------------------------


def test_required_approvals_reads_from_settings(db_session, project_row, monkeypatch):
    monkeypatch.setattr(settings, "policy_high_approvals_required", 2)
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.HIGH)

    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    assert request.required_approvals == 2


# ---------------------------------------------------------------------------
# Decisions: approve / reject / self-approval / reject-requires-comment
# ---------------------------------------------------------------------------


def test_approve_transitions_to_approved(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    approver = _approver(db_session)
    decide(
        db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="approve", comment=None,
    )
    db_session.commit()

    assert request.status == ApprovalStatus.APPROVED


def test_reject_requires_two_approvals_stays_pending_after_one(db_session, project_row, monkeypatch):
    monkeypatch.setattr(settings, "policy_high_approvals_required", 2)
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.HIGH)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    approver = _approver(db_session)
    decide(
        db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="approve", comment=None,
    )
    db_session.commit()

    assert request.status == ApprovalStatus.PENDING

    admin = _admin(db_session)
    decide(
        db_session, request, actor_id=admin.id, project_owner_id=project_row.owner_id,
        decision="approve", comment=None,
    )
    db_session.commit()

    assert request.status == ApprovalStatus.APPROVED


def test_reject_transitions_to_rejected_and_is_terminal(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    approver = _approver(db_session)
    decide(
        db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="reject", comment="not appropriate for prod",
    )
    db_session.commit()

    assert request.status == ApprovalStatus.REJECTED

    with pytest.raises(InvalidDecisionError):
        decide(
            db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
            decision="approve", comment=None,
        )


def test_reject_without_comment_raises(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    approver = _approver(db_session)
    with pytest.raises(InvalidDecisionError):
        decide(
            db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
            decision="reject", comment="   ",
        )


def test_self_approval_by_requester_is_forbidden(db_session, project_row):
    build = _build(db_session, project_row)
    verdict = _verdict(db_session, build, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    with pytest.raises(SelfApprovalError):
        decide(
            db_session, request, actor_id=project_row.owner_id, project_owner_id=project_row.owner_id,
            decision="approve", comment=None,
        )


def test_project_owner_cannot_approve_even_when_not_the_requester(db_session, seeded_ids):
    other_requester = User(
        team_id=seeded_ids["team_id"], name="Other Requester",
        email="other-requester@example.com", role="requester",
    )
    db_session.add(other_requester)
    db_session.flush()

    project = Project(
        name="owner-not-requester",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()

    build = _build(db_session, project)
    verdict = _verdict(db_session, build, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id,
        requester_id=other_requester.id,
    )
    db_session.commit()

    with pytest.raises(SelfApprovalError):
        decide(
            db_session, request, actor_id=project.owner_id, project_owner_id=project.owner_id,
            decision="approve", comment=None,
        )


# ---------------------------------------------------------------------------
# Supersede on newer build
# ---------------------------------------------------------------------------


def test_newer_build_supersedes_older_pending_request(db_session, project_row):
    build1 = _build(db_session, project_row)
    verdict1 = _verdict(db_session, build1, PolicyRiskTier.MEDIUM)
    request1 = create_approval_request_for_verdict(
        db_session, build=build1, verdict=verdict1, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()
    assert request1.status == ApprovalStatus.PENDING

    build2 = _build(db_session, project_row)
    verdict2 = _verdict(db_session, build2, PolicyRiskTier.MEDIUM)
    create_approval_request_for_verdict(
        db_session, build=build2, verdict=verdict2, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    db_session.refresh(request1)
    assert request1.status == ApprovalStatus.SUPERSEDED
    assert request1.superseded_by_build_id == build2.id


def test_supersede_does_not_touch_already_decided_requests(db_session, project_row):
    build1 = _build(db_session, project_row)
    verdict1 = _verdict(db_session, build1, PolicyRiskTier.MEDIUM)
    request1 = create_approval_request_for_verdict(
        db_session, build=build1, verdict=verdict1, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()

    approver = _approver(db_session)
    decide(
        db_session, request1, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="approve", comment=None,
    )
    db_session.commit()
    assert request1.status == ApprovalStatus.APPROVED

    superseded = supersede_older_pending_requests(
        db_session, project_id=project_row.id, newer_build_id="some-other-build"
    )
    db_session.commit()

    assert superseded == []
    db_session.refresh(request1)
    assert request1.status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# Deploy eligibility
# ---------------------------------------------------------------------------


def test_deploy_eligible_true_for_approved_and_auto_approved(db_session, project_row):
    build_auto = _build(db_session, project_row)
    verdict_low = _verdict(db_session, build_auto, PolicyRiskTier.LOW)
    create_approval_request_for_verdict(
        db_session, build=build_auto, verdict=verdict_low, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()
    db_session.refresh(build_auto)
    assert is_deploy_eligible(build_auto) is True

    build_approved = _build(db_session, project_row)
    verdict_medium = _verdict(db_session, build_approved, PolicyRiskTier.MEDIUM)
    request = create_approval_request_for_verdict(
        db_session, build=build_approved, verdict=verdict_medium, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()
    approver = _approver(db_session)
    decide(
        db_session, request, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="approve", comment=None,
    )
    db_session.commit()
    db_session.refresh(build_approved)
    assert is_deploy_eligible(build_approved) is True


def test_deploy_eligible_false_for_pending_rejected_superseded_and_missing(db_session, project_row):
    build_no_request = _build(db_session, project_row)
    db_session.commit()
    db_session.refresh(build_no_request)
    assert is_deploy_eligible(build_no_request) is False

    build_pending = _build(db_session, project_row)
    verdict_pending = _verdict(db_session, build_pending, PolicyRiskTier.MEDIUM)
    create_approval_request_for_verdict(
        db_session, build=build_pending, verdict=verdict_pending, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()
    db_session.refresh(build_pending)
    assert is_deploy_eligible(build_pending) is False

    build_rejected = _build(db_session, project_row)
    verdict_rejected = _verdict(db_session, build_rejected, PolicyRiskTier.MEDIUM)
    request_rejected = create_approval_request_for_verdict(
        db_session, build=build_rejected, verdict=verdict_rejected, project_id=project_row.id,
        requester_id=project_row.owner_id,
    )
    db_session.commit()
    approver = _approver(db_session)
    decide(
        db_session, request_rejected, actor_id=approver.id, project_owner_id=project_row.owner_id,
        decision="reject", comment="no",
    )
    db_session.commit()
    db_session.refresh(build_rejected)
    assert is_deploy_eligible(build_rejected) is False
