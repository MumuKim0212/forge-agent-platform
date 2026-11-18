from datetime import datetime, timedelta, timezone

import pytest

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.services.project_activity import _as_aware_utc


def _create_project(client, seeded_ids):
    resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "기존 프로젝트",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "최초 요청",
        },
    )
    return resp.json()["id"]


def _mark_confirmed(db_session, project_id):
    """a modify request needs a confirmed ref; these tests are about what a modify request
    does, so the project has had a first deployment confirmed."""
    db_session.get(Project, project_id).confirmed_ref = "c0ffee0"
    db_session.commit()


def test_submit_change_request_adds_second_build_without_regressing_status(client, db_session, seeded_ids):
    project_id = _create_project(client, seeded_ids)
    _mark_confirmed(db_session, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "목록에 검색창 추가해줘"},
    )
    assert resp.status_code == 201
    assert resp.json()["build_id"] is not None

    headers = {"X-Forge-User-Id": seeded_ids["user_id"]}
    change_requests = client.get(
        f"/api/projects/{project_id}/change-requests", headers=headers
    ).json()
    assert len(change_requests) == 2  # initial + follow-up

    project = client.get(f"/api/projects/{project_id}", headers=headers).json()
    # No executor is configured in this test, so neither build is QUEUED and the
    # status stays where creation put it (the forward transitions
    # themselves, and that a later build never regresses, are in test_project_status.py).
    assert project["status"] == "SUBMITTED"

    timeline = client.get(f"/api/projects/{project_id}/timeline", headers=headers).json()
    builds = [e for e in timeline if e["kind"] == "build"]
    assert len(builds) == 2
    assert all(b["data"]["status"] == "NOT_IMPLEMENTED" for b in builds)


def test_submit_change_request_updates_project_last_active_at(client, db_session, seeded_ids):
    """already counted before this task (the one pre-existing
    write site), but untested until now — it now goes through the shared
    `services.project_activity.touch_last_active` helper rather than its
    own inline `datetime.now(timezone.utc)` assignment."""
    project_id = _create_project(client, seeded_ids)
    _mark_confirmed(db_session, project_id)
    project = db_session.get(Project, project_id)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "설명 변경"},
    )
    assert resp.status_code == 201

    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_submit_change_request_on_missing_project_returns_404(client, seeded_ids):
    resp = client.post(
        "/api/projects/does-not-exist/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "x"},
    )
    assert resp.status_code == 404


def _row_counts(db_session):
    db_session.expire_all()
    return {
        "change_requests": db_session.query(ChangeRequest).count(),
        "builds": db_session.query(Build).count(),
        "audit": db_session.query(AuditLog).filter(AuditLog.action == "change_request.submitted").count(),
    }


@pytest.mark.parametrize(
    ("confirmed_ref", "expected_status", "new_rows"),
    [
        (None, 409, 0),
        ("", 409, 0),
        ("c0ffee0", 201, 1),
    ],
    ids=["never-confirmed", "empty-ref", "confirmed"],
)
def test_a_modify_request_needs_a_confirmed_ref(
    client, db_session, seeded_ids, confirmed_ref, expected_status, new_rows
):
    """refused with 409 and a coded detail BEFORE
    any change request or build exists; with a confirmed ref the same request still creates its
    modify build. Both directions are read off the row counts and the build's type, not only the
    status code."""
    project_id = _create_project(client, seeded_ids)
    project = db_session.get(Project, project_id)
    project.confirmed_ref = confirmed_ref
    db_session.commit()
    before = _row_counts(db_session)

    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "목록에 검색창 추가해줘"},
    )

    assert resp.status_code == expected_status
    after = _row_counts(db_session)
    assert {k: after[k] - before[k] for k in before} == {
        "change_requests": new_rows,
        "builds": new_rows,
        "audit": new_rows,
    }
    if expected_status == 409:
        detail = resp.json()["detail"]
        assert detail.startswith("NO_CONFIRMED_REF: ")
        assert "confirm a first deployment" in detail and "create request" in detail
    else:
        modify_builds = db_session.query(Build).filter_by(project_id=project_id, type="modify").all()
        assert len(modify_builds) == 1
        assert resp.json()["build_id"] == modify_builds[0].id


def test_a_refused_modify_request_does_not_count_as_activity(client, db_session, seeded_ids):
    """The refusal changes nothing on the project (no activity touch, no status move)."""
    project_id = _create_project(client, seeded_ids)
    project = db_session.get(Project, project_id)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    status_before = project.status

    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "x"},
    )

    assert resp.status_code == 409
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) == stale
    assert project.status == status_before


def test_a_create_request_on_a_never_deployed_project_is_unaffected(client, db_session, seeded_ids):
    """The NO_CONFIRMED_REF refusal covers only the modify path: creating a project (a create-type build) with no
    confirmed ref anywhere still works."""
    resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "새 프로젝트",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "할 일 목록",
        },
    )

    assert resp.status_code == 201
    project_id = resp.json()["id"]
    assert resp.json()["confirmed_ref"] is None
    builds = db_session.query(Build).filter_by(project_id=project_id).all()
    assert [b.type for b in builds] == ["create"]
