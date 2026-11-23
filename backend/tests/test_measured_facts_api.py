"""what the platform measured about a build reaches the people who judge it.

- the project timeline (read by every authenticated user) carries the COUNTS and the
  drift, never the file list;
- `GET /approvals/{id}` carries the request text, the full list, the drift and the
  findings;
- the pending list carries the counts only.
"""

from __future__ import annotations

import pytest

from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.services.approval import create_approval_request_for_verdict
from app.services.policy_checks import PolicyRiskTier
from app.services.status import ProjectStatus

PATH = "app/AGENT-CHOSEN-PATH-3c9a.py"
REQUEST = "Build me a small inventory tool, with a SECRET-REQUEST-TEXT."
CHANGED = {
    "entries": [
        {"path": PATH, "change": "added"},
        {"path": "app/app.py", "change": "modified"},
        {"path": "app/old.py", "change": "deleted"},
    ],
    "totals": {"added": 1, "modified": 1, "deleted": 1},
    "truncated": False,
}


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="Measured Facts Project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _build(db_session, project, seeded_ids, *, changed=CHANGED, drift=("Dockerfile",), with_request=True):
    build = Build(
        project_id=project.id,
        type="create",
        diff_ref="forge-build-x#1",
        changed_files=changed,
        protected_path_drift=list(drift) if drift is not None else None,
    )
    db_session.add(build)
    db_session.flush()
    if with_request:
        db_session.add(
            ChangeRequest(
                project_id=project.id,
                requester_id=seeded_ids["user_id"],
                nl_request=REQUEST,
                build_id=build.id,
            )
        )
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    db_session.add(
        PolicyCheckResult(
            verdict_id=verdict.id,
            check_name="secrets_pii",
            result="flag",
            summary="1 finding",
            findings=[{"path": PATH, "type": "aws_key", "match": "AKIA****"}],
        )
    )
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id, requester_id=project.owner_id
    )
    db_session.commit()
    return build, request


def _as(user_id):
    return {"X-Forge-User-Id": user_id}


# --- (c1) the timeline: counts and drift, no list ----------------------------------


def test_c1_the_timeline_build_entry_has_counts_and_drift_and_no_file_list(
    client, db_session, project_row, seeded_ids
):
    build, _ = _build(db_session, project_row, seeded_ids)

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"]))

    assert resp.status_code == 200
    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["changed_counts"] == {
        "totals": {"added": 1, "modified": 1, "deleted": 1},
        "truncated": False,
    }
    assert entry["data"]["protected_path_drift"] == ["Dockerfile"]
    # No path the agent chose, anywhere in the timeline, and no list-shaped key.
    assert PATH not in resp.text
    assert "entries" not in entry["data"] and "changed_files" not in entry["data"]


def test_c1_an_unmeasured_build_has_null_counts_and_drift_on_the_timeline(
    client, db_session, project_row, seeded_ids
):
    build, _ = _build(db_session, project_row, seeded_ids, changed=None, drift=None)

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"]))

    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["changed_counts"] is None
    assert entry["data"]["protected_path_drift"] is None


def test_c1_a_measured_empty_build_is_zero_counts_not_null(client, db_session, project_row, seeded_ids):
    empty = {"entries": [], "totals": {"added": 0, "modified": 0, "deleted": 0}, "truncated": False}
    build, _ = _build(db_session, project_row, seeded_ids, changed=empty, drift=())

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"]))

    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["changed_counts"]["totals"] == {"added": 0, "modified": 0, "deleted": 0}
    assert entry["data"]["protected_path_drift"] == []


def test_c1_the_truncated_flag_travels_with_the_counts(client, db_session, project_row, seeded_ids):
    big = {"entries": CHANGED["entries"], "totals": {"added": 900, "modified": 0, "deleted": 0}, "truncated": True}
    build, _ = _build(db_session, project_row, seeded_ids, changed=big)

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"]))

    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["changed_counts"] == {"totals": {"added": 900, "modified": 0, "deleted": 0}, "truncated": True}


# --- (c2) the approval detail ------------------------------------------------------


def test_c2_the_approval_detail_carries_the_request_the_full_list_the_drift_and_the_findings(
    client, db_session, project_row, seeded_ids
):
    _, request = _build(db_session, project_row, seeded_ids)

    resp = client.get(f"/api/approvals/{request.id}", headers=_as(seeded_ids["user_id"]))

    assert resp.status_code == 200
    body = resp.json()
    assert body["request_text"] == REQUEST
    assert body["changed_files"] == CHANGED
    assert body["protected_path_drift"] == ["Dockerfile"]
    (check,) = body["policy_verdict"]["checks"]
    assert check["findings"] == [{"path": PATH, "type": "aws_key", "match": "AKIA****"}]


def test_c2_an_unmeasured_build_and_a_missing_request_are_null_not_invented(
    client, db_session, project_row, seeded_ids
):
    _, request = _build(db_session, project_row, seeded_ids, changed=None, drift=None, with_request=False)

    body = client.get(f"/api/approvals/{request.id}", headers=_as(seeded_ids["user_id"])).json()

    assert body["request_text"] is None
    assert body["changed_files"] is None
    assert body["protected_path_drift"] is None


def test_c2_the_requester_who_made_the_request_reads_it_and_no_credentials_is_401(
    client, db_session, project_row, seeded_ids
):
    """ADR 0048: the detail is read by the request's requester (here also the
    project owner), an approver or an admin; an unrelated user is refused (covered
    in test_approvals and test_rbac). This case shows the "belongs to it" half
    and that no credentials is 401."""
    _, request = _build(db_session, project_row, seeded_ids)
    requester = db_session.query(User).filter_by(role="requester").first()

    assert client.get(f"/api/approvals/{request.id}", headers=_as(requester.id)).status_code == 200
    assert client.get(f"/api/approvals/{request.id}").status_code == 401


def test_c2_the_decision_is_still_approver_gated_and_the_decision_response_carries_the_facts(
    client, db_session, project_row, seeded_ids
):
    _, request = _build(db_session, project_row, seeded_ids)
    requester = db_session.query(User).filter_by(role="requester").first()
    approver = db_session.query(User).filter_by(role="approver").first()

    denied = client.post(f"/api/approvals/{request.id}/approve", json={}, headers=_as(requester.id))
    assert denied.status_code == 403

    decided = client.post(f"/api/approvals/{request.id}/approve", json={}, headers=_as(approver.id))
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"
    assert decided.json()["request_text"] == REQUEST
    assert decided.json()["changed_files"] == CHANGED


# --- (c3) the pending list: counts only --------------------------------------------


def test_c3_the_list_has_counts_only(client, db_session, project_row, seeded_ids):
    _build(db_session, project_row, seeded_ids)
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    resp = client.get("/api/approvals", headers=_as(approver.id))
    assert resp.status_code == 200

    (item,) = resp.json()
    assert item["changed_counts"] == {"totals": {"added": 1, "modified": 1, "deleted": 1}, "truncated": False}
    assert PATH not in resp.text
    assert "changed_files" not in item and "request_text" not in item


def test_c3_an_unmeasured_build_has_null_counts_in_the_list(client, db_session, project_row, seeded_ids):
    _build(db_session, project_row, seeded_ids, changed=None, drift=None)
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    (item,) = client.get("/api/approvals", headers=_as(approver.id)).json()

    assert item["changed_counts"] is None
