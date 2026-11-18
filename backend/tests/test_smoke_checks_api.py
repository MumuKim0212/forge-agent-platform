"""the platform's smoke check of a preview reaches the people who judge the build.

- `GET /approvals/{id}` and the project timeline carry `smoke_checks` as the same typed object;
- `null` means not measured, and is not rewritten into an empty result;
- a measured result with no routes stays an object with `routes: []`;
- the pending list does not carry it (detail view only).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyVerdict
from app.schemas.build import SmokeChecksOut
from app.services.approval import create_approval_request_for_verdict
from app.services.policy_checks import PolicyRiskTier
from tests.test_measured_facts_api import _as, project_row  # noqa: F401 - a fixture, used by name

MEASURED = {
    "routes": [
        {"path": "/", "status": 200, "outcome": "ok"},
        {"path": "/bookings", "status": 500, "outcome": "server_error"},
        {"path": "/slow", "status": None, "outcome": "no_answer"},
        {"path": "/big", "status": 200, "outcome": "too_large"},
    ],
    "skipped": 2,
    "truncated": True,
}
ZERO_ROUTES = {"routes": [], "skipped": 3, "truncated": False}


def _build(db_session, project, seeded_ids, *, smoke):
    build = Build(project_id=project.id, type="create", diff_ref="forge-build-x#1", smoke_checks=smoke)
    db_session.add(build)
    db_session.flush()
    db_session.add(
        ChangeRequest(
            project_id=project.id, requester_id=seeded_ids["user_id"], nl_request="x", build_id=build.id
        )
    )
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id, requester_id=project.owner_id
    )
    db_session.commit()
    return build, request


def _timeline_build(client, project, build, user_id):
    resp = client.get(f"/api/projects/{project.id}/timeline", headers=_as(user_id))
    assert resp.status_code == 200
    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    return entry["data"]


@pytest.mark.parametrize("smoke", [MEASURED, ZERO_ROUTES])
def test_a_measured_fact_reaches_the_approval_detail_and_the_timeline_as_the_same_object(
    client, db_session, project_row, seeded_ids, smoke  # noqa: F811
):
    build, request = _build(db_session, project_row, seeded_ids, smoke=smoke)
    user_id = seeded_ids["user_id"]

    detail = client.get(f"/api/approvals/{request.id}", headers=_as(user_id)).json()
    timeline = _timeline_build(client, project_row, build, user_id)

    assert detail["smoke_checks"] == smoke
    assert timeline["smoke_checks"] == smoke


def test_an_unmeasured_build_is_null_on_both_not_an_empty_result(client, db_session, project_row, seeded_ids):  # noqa: F811
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)
    user_id = seeded_ids["user_id"]

    detail = client.get(f"/api/approvals/{request.id}", headers=_as(user_id)).json()
    timeline = _timeline_build(client, project_row, build, user_id)

    assert "smoke_checks" in detail and detail["smoke_checks"] is None
    assert "smoke_checks" in timeline and timeline["smoke_checks"] is None


def test_the_pending_list_does_not_carry_the_routes(client, db_session, project_row, seeded_ids):  # noqa: F811
    _build(db_session, project_row, seeded_ids, smoke=MEASURED)

    resp = client.get("/api/approvals", headers=_as(seeded_ids["user_id"]))

    assert resp.status_code == 200 and "/bookings" not in resp.text


def test_reads_need_authentication(client, db_session, project_row, seeded_ids):  # noqa: F811
    _, request = _build(db_session, project_row, seeded_ids, smoke=MEASURED)

    assert client.get(f"/api/approvals/{request.id}").status_code == 401


def test_the_typed_shape_forbids_extra_keys_and_unknown_outcomes():
    assert SmokeChecksOut.model_validate(MEASURED).model_dump() == MEASURED
    with pytest.raises(ValidationError):
        SmokeChecksOut.model_validate({**MEASURED, "body": "x"})
    with pytest.raises(ValidationError):
        SmokeChecksOut.model_validate(
            {"routes": [{"path": "/", "status": 200, "outcome": "ok", "body": "x"}], "skipped": 0, "truncated": False}
        )
    with pytest.raises(ValidationError):
        SmokeChecksOut.model_validate(
            {"routes": [{"path": "/", "status": 200, "outcome": "fine"}], "skipped": 0, "truncated": False}
        )
