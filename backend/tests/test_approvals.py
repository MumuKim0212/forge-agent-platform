"""API tests for the approval console endpoints (routers/approvals.py)
and the approval-request timeline surfacing (routers/projects.py). Uses the
standard in-memory SQLite fixtures — no Docker, no network, no key."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.approval import ApprovalDecision, ApprovalStatus
from app.models.build import Build
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.services import approval as approval_service
from app.services.approval import create_approval_request_for_verdict
from app.services.policy_checks import PolicyRiskTier
from app.services.status import ProjectStatus


@pytest.fixture()
def approver_id(db_session):
    return db_session.query(User).filter_by(role="approver").first().id


@pytest.fixture()
def admin_id(db_session):
    return db_session.query(User).filter_by(role="admin").first().id


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="Approval API Test Project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _make_request(db_session, project, risk_tier=PolicyRiskTier.MEDIUM, flag_check=None):
    build = Build(project_id=project.id, type="create", diff_ref="forge-build-x#1")
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=risk_tier)
    db_session.add(verdict)
    db_session.flush()
    if flag_check:
        db_session.add(
            PolicyCheckResult(
                verdict_id=verdict.id,
                check_name=flag_check,
                result="flag",
                summary="flagged in test",
                findings=[{"kind": "test"}],
            )
        )
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id,
        requester_id=project.owner_id,
    )
    db_session.commit()
    return build, request


# ---------------------------------------------------------------------------
# GET /approvals (list) + GET /approvals/{id} (detail)
# ---------------------------------------------------------------------------


def test_list_approvals_returns_live_requests_with_summary_fields(
    client, db_session, project_row, seeded_ids, approver_id
):
    _build, request = _make_request(
        db_session, project_row, risk_tier=PolicyRiskTier.HIGH, flag_check="secrets_pii"
    )

    resp = client.get("/api/approvals", headers={"X-Forge-User-Id": approver_id})
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == request.id
    assert item["project_name"] == "Approval API Test Project"
    assert item["requester_id"] == seeded_ids["user_id"]
    assert item["risk_tier"] == PolicyRiskTier.HIGH
    assert item["status"] == ApprovalStatus.PENDING
    assert item["flagged_checks"] == ["secrets_pii"]
    assert item["diff_ref"] == "forge-build-x#1"


def test_list_approvals_status_filter(client, db_session, project_row, approver_id):
    _make_request(db_session, project_row, risk_tier=PolicyRiskTier.LOW)
    headers = {"X-Forge-User-Id": approver_id}

    resp = client.get("/api/approvals?status=PENDING", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get("/api/approvals?status=AUTO_APPROVED", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_approvals_is_refused_to_a_requester_with_and_without_the_status_filter(
    client, db_session, project_row, seeded_ids, approver_id
):
    """ADR 0048 section 1: the requester who made the request and owns the project is still
    refused the list, and the `status` filter is no way around it. The same call as an
    approver returns the request, so the refusal is the role and not an empty result."""
    _build, request = _make_request(db_session, project_row)
    requester_headers = {"X-Forge-User-Id": seeded_ids["user_id"]}

    plain = client.get("/api/approvals", headers=requester_headers)
    filtered = client.get("/api/approvals?status=PENDING", headers=requester_headers)
    assert (plain.status_code, filtered.status_code) == (403, 403)
    assert plain.json() == {"detail": "Approver or admin role required"}
    assert filtered.json() == {"detail": "Approver or admin role required"}

    as_approver = client.get("/api/approvals?status=PENDING", headers={"X-Forge-User-Id": approver_id})
    assert as_approver.status_code == 200
    assert [i["id"] for i in as_approver.json()] == [request.id]


def test_list_approvals_is_open_to_an_admin_and_needs_identity(
    client, db_session, project_row, admin_id
):
    _build, request = _make_request(db_session, project_row)

    as_admin = client.get("/api/approvals", headers={"X-Forge-User-Id": admin_id})
    assert as_admin.status_code == 200
    assert [i["id"] for i in as_admin.json()] == [request.id]
    assert client.get("/api/approvals").status_code == 401


def test_list_approvals_refuses_an_unknown_role(client, db_session, project_row, seeded_ids):
    _make_request(db_session, project_row)
    user = db_session.get(User, seeded_ids["user_id"])
    user.role = "wizard"
    db_session.commit()

    resp = client.get("/api/approvals", headers={"X-Forge-User-Id": user.id})
    assert resp.status_code == 403


def test_detail_is_read_by_the_requester_who_is_not_the_owner(
    client, db_session, seeded_ids, approver_id
):
    """ADR 0048 section 2, the requester half on its own: the project belongs to someone
    else (the approver here), the request was made by the plain requester."""
    project = Project(
        name="Owned By Someone Else",
        owner_id=approver_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="modify", diff_ref="forge-build-req#1")
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id,
        requester_id=seeded_ids["user_id"],
    )
    db_session.commit()

    resp = client.get(f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": seeded_ids["user_id"]})
    assert resp.status_code == 200
    assert resp.json()["id"] == request.id
    assert resp.json()["requester_id"] == seeded_ids["user_id"]


def test_detail_is_refused_to_an_unrelated_requester_but_read_by_an_approver(
    client, db_session, project_row, seeded_ids, approver_id
):
    _build, request = _make_request(db_session, project_row)
    outsider = User(name="Outsider", email="outsider@example.com", role="requester")
    db_session.add(outsider)
    db_session.commit()

    refused = client.get(f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": outsider.id})
    assert refused.status_code == 403
    assert "decisions" not in refused.json()

    allowed = client.get(f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": approver_id})
    assert allowed.status_code == 200
    assert allowed.json()["id"] == request.id


def test_may_read_approval_request_is_fail_closed():
    from types import SimpleNamespace

    from app.services.roles import Role, may_read_approval_request

    me = SimpleNamespace(id="u1", role=Role.REQUESTER)
    assert may_read_approval_request(me, "u1", "u9") is True  # the requester
    assert may_read_approval_request(me, "u9", "u1") is True  # the project's owner
    assert may_read_approval_request(me, "u9", "u8") is False  # neither
    assert may_read_approval_request(me, None, None) is False  # no ids is not everyone's
    assert may_read_approval_request(SimpleNamespace(id="u1", role="wizard"), "u9", "u8") is False
    assert may_read_approval_request(SimpleNamespace(id="u1", role=None), "u9", "u8") is False
    assert may_read_approval_request(SimpleNamespace(id="u3", role=Role.APPROVER), "u9", "u8") is True
    assert may_read_approval_request(SimpleNamespace(id="u3", role=Role.ADMIN), None, None) is True


def test_get_approval_detail_includes_decisions_and_per_check_results(
    client, db_session, project_row, seeded_ids
):
    _build, request = _make_request(
        db_session, project_row, flag_check="dependency_vulnerabilities"
    )

    resp = client.get(
        f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == ApprovalStatus.PENDING
    assert body["policy_verdict_id"] is not None
    assert body["decisions"] == []
    # Detail view shows per-check results.
    assert body["policy_verdict"] is not None
    check_names = [c["check_name"] for c in body["policy_verdict"]["checks"]]
    assert "dependency_vulnerabilities" in check_names


def test_approval_detail_names_the_project_owner_apart_from_the_requester(
    client, db_session, seeded_ids, approver_id, admin_id
):
    """the console decides from the payload whether the viewer is barred by the
    self-approval rule. The rule bars the requester AND the project's owner
    (`approval._is_self_approval`), so the detail has to carry both ids: here they differ, and
    the owner is an approver who did not request the change."""
    project = Project(
        name="Owner Is An Approver",
        owner_id=approver_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="modify", diff_ref="forge-build-owner#1")
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id,
        requester_id=seeded_ids["user_id"],
    )
    db_session.commit()

    detail = client.get(
        f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": approver_id}
    ).json()

    assert detail["requester_id"] == seeded_ids["user_id"]
    assert detail["project_owner_id"] == approver_id

    # The owner holds the approver role and did not request the change, yet is refused (403);
    # this is the rule the console's `canApproveRequest` mirrors. A different approver is not.
    owner_decides = client.post(
        f"/api/approvals/{request.id}/approve", headers={"X-Forge-User-Id": approver_id}, json={}
    )
    assert owner_decides.status_code == 403
    other_decides = client.post(
        f"/api/approvals/{request.id}/approve", headers={"X-Forge-User-Id": admin_id}, json={}
    )
    assert other_decides.status_code == 200


def test_approval_detail_owner_is_in_the_decision_responses_too(
    client, db_session, project_row, seeded_ids, admin_id
):
    _build, request = _make_request(db_session, project_row)

    decided = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": admin_id},
        json={},
    ).json()

    assert decided["project_owner_id"] == seeded_ids["user_id"]


def test_get_approval_404_on_unknown_id(client, approver_id):
    resp = client.get(
        "/api/approvals/does-not-exist", headers={"X-Forge-User-Id": approver_id}
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Approval request not found"}


# ---------------------------------------------------------------------------
# POST /approvals/{id}/approve
# ---------------------------------------------------------------------------


def test_approve_happy_path(client, db_session, project_row, approver_id):
    _build, request = _make_request(db_session, project_row)

    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": approver_id},
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == ApprovalStatus.APPROVED
    assert len(body["decisions"]) == 1
    assert body["decisions"][0]["actor_id"] == approver_id
    assert body["decisions"][0]["decision"] == "approve"


def test_approve_requires_identity(client, db_session, project_row):
    _build, request = _make_request(db_session, project_row)
    resp = client.post(f"/api/approvals/{request.id}/approve", json={})
    assert resp.status_code == 401


def test_approve_403_for_non_approver_role(client, db_session, project_row, seeded_ids):
    _build, request = _make_request(db_session, project_row)
    # seeded_ids["user_id"] is the requester-role user.
    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={},
    )
    assert resp.status_code == 403


def test_self_approval_403_even_with_approver_role(client, db_session, seeded_ids, approver_id):
    # A project owned (and requested) by the approver themselves.
    project = Project(
        name="Self Approval Project",
        owner_id=approver_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    _build, request = _make_request(db_session, project)

    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": approver_id},
        json={},
    )
    assert resp.status_code == 403

    # Someone else with the approver/admin role can still approve it.
    admin = db_session.query(User).filter_by(role="admin").first()
    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": admin.id},
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == ApprovalStatus.APPROVED


def test_approve_404_on_unknown_id(client, approver_id):
    resp = client.post(
        "/api/approvals/nope/approve", headers={"X-Forge-User-Id": approver_id}, json={}
    )
    assert resp.status_code == 404


def test_approve_400_on_already_decided_request(client, db_session, project_row, approver_id, admin_id):
    _build, request = _make_request(db_session, project_row)
    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": approver_id},
        json={},
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": admin_id},
        json={},
    )
    assert resp.status_code == 400


def test_one_approver_approving_twice_is_a_400_and_a_two_person_request_stays_pending(
    client, db_session, project_row, approver_id, monkeypatch
):
    """the second click by the same approver is refused with the existing 400, however
    many times it is repeated, and never counts toward the second approval."""
    monkeypatch.setattr(settings, "policy_high_approvals_required", 2)
    _build, request = _make_request(db_session, project_row, risk_tier=PolicyRiskTier.HIGH)
    assert request.required_approvals == 2

    first = client.post(f"/api/approvals/{request.id}/approve", headers={"X-Forge-User-Id": approver_id}, json={})
    assert first.status_code == 200
    assert first.json()["status"] == ApprovalStatus.PENDING

    for _ in range(2):
        again = client.post(
            f"/api/approvals/{request.id}/approve", headers={"X-Forge-User-Id": approver_id}, json={}
        )
        assert again.status_code == 400
        assert "already approved" in again.json()["detail"]

    detail = client.get(f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": approver_id}).json()
    assert detail["status"] == ApprovalStatus.PENDING
    assert [d["decision"] for d in detail["decisions"]] == ["approve"]


def test_a_duplicate_that_reaches_the_database_is_the_same_400(
    client, db_session, project_row, approver_id, monkeypatch
):
    """the first duplicate check is made blind (as it is in a race, where it runs before
    the other click commits), so the INSERT is what meets the unique index. The `IntegrityError` is
    reported as the same 400 'already approved', not a 500, and the session is still usable."""
    monkeypatch.setattr(settings, "policy_high_approvals_required", 2)
    _build, request = _make_request(db_session, project_row, risk_tier=PolicyRiskTier.HIGH)
    request_id = request.id
    headers = {"X-Forge-User-Id": approver_id}
    assert client.post(f"/api/approvals/{request_id}/approve", headers=headers, json={}).status_code == 200

    real = approval_service._has_approved
    answers = iter([False])  # the first check is blind; the one after the failed INSERT is the real one

    def blind_once(db, *, request_id, actor_id):
        blind = next(answers, None)
        return real(db, request_id=request_id, actor_id=actor_id) if blind is None else blind

    monkeypatch.setattr(approval_service, "_has_approved", blind_once)
    again = client.post(f"/api/approvals/{request_id}/approve", headers=headers, json={})

    assert again.status_code == 400
    assert "already approved" in again.json()["detail"]
    assert db_session.query(ApprovalDecision).filter_by(approval_request_id=request_id).count() == 1
    detail = client.get(f"/api/approvals/{request_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["status"] == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# POST /approvals/{id}/reject
# ---------------------------------------------------------------------------


def test_reject_happy_path_with_comment(client, db_session, project_row, approver_id):
    _build, request = _make_request(db_session, project_row)

    resp = client.post(
        f"/api/approvals/{request.id}/reject",
        headers={"X-Forge-User-Id": approver_id},
        json={"comment": "hardcoded credentials must be removed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == ApprovalStatus.REJECTED
    assert body["decisions"][0]["comment"] == "hardcoded credentials must be removed"


def test_reject_400_without_comment(client, db_session, project_row, approver_id):
    _build, request = _make_request(db_session, project_row)

    resp = client.post(
        f"/api/approvals/{request.id}/reject",
        headers={"X-Forge-User-Id": approver_id},
        json={},
    )
    assert resp.status_code == 400


def test_reject_403_for_non_approver_role(client, db_session, project_row, seeded_ids):
    _build, request = _make_request(db_session, project_row)
    resp = client.post(
        f"/api/approvals/{request.id}/reject",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"comment": "no"},
    )
    assert resp.status_code == 403


def test_rejected_build_stays_succeeded_and_not_deploy_eligible(
    client, db_session, project_row, approver_id
):
    build, request = _make_request(db_session, project_row)
    build.status = "SUCCEEDED"
    db_session.commit()

    resp = client.post(
        f"/api/approvals/{request.id}/reject",
        headers={"X-Forge-User-Id": approver_id},
        json={"comment": "not safe"},
    )
    assert resp.status_code == 200

    db_session.refresh(build)
    assert build.status == "SUCCEEDED"
    assert build.deploy_eligible is False


# ---------------------------------------------------------------------------
# Decision audit logging
# ---------------------------------------------------------------------------


def test_decisions_are_audit_logged_with_actor(client, db_session, project_row, approver_id):
    from app.models.audit import AuditLog

    _build, request = _make_request(db_session, project_row)
    client.post(
        f"/api/approvals/{request.id}/reject",
        headers={"X-Forge-User-Id": approver_id},
        json={"comment": "reason recorded"},
    )

    entries = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "approval_request.rejected",
            AuditLog.target == f"approval_request:{request.id}",
        )
        .all()
    )
    assert len(entries) == 1
    assert entries[0].actor_id == approver_id
    assert entries[0].metadata_json["comment"] == "reason recorded"


# ---------------------------------------------------------------------------
# Timeline surfacing (routers/projects.py)
# ---------------------------------------------------------------------------


def test_timeline_shows_approval_request_status(client, db_session, project_row, approver_id):
    _build, request = _make_request(db_session, project_row, risk_tier=PolicyRiskTier.HIGH)

    headers = {"X-Forge-User-Id": approver_id}
    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=headers)
    assert resp.status_code == 200
    approval_entries = [e for e in resp.json() if e["kind"] == "approval_request"]
    assert len(approval_entries) == 1
    entry = approval_entries[0]
    assert entry["data"]["id"] == request.id
    assert entry["data"]["status"] == ApprovalStatus.PENDING
    assert entry["data"]["risk_tier"] == PolicyRiskTier.HIGH

    client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": approver_id},
        json={},
    )

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=headers)
    approval_entries = [e for e in resp.json() if e["kind"] == "approval_request"]
    assert approval_entries[0]["data"]["status"] == ApprovalStatus.APPROVED
