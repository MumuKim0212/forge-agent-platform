"""the requester's text has a length cap, and over it the API rejects (422) rather
than truncates. Every request is placed verbatim in the build agent's prompt, and the
LLM proxy checks the budget only before a call, so this bound is the only thing
between a huge request and the prompt.

Limits are written as literals on purpose: the constants are
pinned once below, and a one-sided change to either side must go red.

The change-request tests and the project-create tests are separate on purpose:
`ChangeRequestCreate` and `ProjectCreate` are distinct classes, so a bound removed from
one must not be coverable by a test of the other. The change-request tests therefore
create their project by direct insert, never through `POST /api/projects`."""

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.schemas.change_request import NL_REQUEST_MAX_CHARS
from app.schemas.project import PROJECT_NAME_MAX_CHARS
from app.services.status import ProjectStatus


def _row_counts(db):
    db.expire_all()
    return (
        db.query(Project).count(),
        db.query(ChangeRequest).count(),
        db.query(Build).count(),
        db.query(AuditLog).count(),
    )


def _make_project(db, seeded_ids):
    # `confirmed_ref` set: a modify request needs one; these tests are about the text limit.
    project = Project(
        confirmed_ref="c0ffee0",
        name="cap-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project.id


def _post_change_request(client, seeded_ids, project_id, text):
    return client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": text},
    )


def _post_project(client, seeded_ids, *, name="캡 테스트", nl_request="요청"):
    return client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": name,
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": nl_request,
        },
    )


def _assert_too_long(response, field):
    """The frontend recognises this shape (`api/requestLimits.ts`), so it is pinned."""
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(
        entry["type"] == "string_too_long" and entry["loc"][-1] == field for entry in detail
    ), detail


def test_the_limits_are_the_documented_values():
    assert NL_REQUEST_MAX_CHARS == 8000
    assert PROJECT_NAME_MAX_CHARS == 100


# -- POST /api/projects/{id}/change-requests: ChangeRequestCreate --


def test_change_request_of_exactly_the_limit_is_accepted_and_stored_whole(
    client, db_session, seeded_ids
):
    project_id = _make_project(db_session, seeded_ids)
    text = "가" * 8000

    response = _post_change_request(client, seeded_ids, project_id, text)

    assert response.status_code == 201
    assert response.json()["nl_request"] == text  # not truncated, not normalised


def test_change_request_one_over_the_limit_is_rejected_and_creates_nothing(
    client, db_session, seeded_ids
):
    project_id = _make_project(db_session, seeded_ids)
    before = _row_counts(db_session)

    response = _post_change_request(client, seeded_ids, project_id, "가" * 8001)

    _assert_too_long(response, "nl_request")
    assert _row_counts(db_session) == before


def test_change_request_limit_counts_code_points_not_utf16_units(client, db_session, seeded_ids):
    """8000 emoji are 8000 characters to Python `len` (16000 UTF-16 units to JS
    `.length`): the server accepts them, and 8001 are rejected."""
    project_id = _make_project(db_session, seeded_ids)

    accepted = _post_change_request(client, seeded_ids, project_id, "😀" * 8000)
    assert accepted.status_code == 201
    assert accepted.json()["nl_request"] == "😀" * 8000

    before = _row_counts(db_session)
    rejected = _post_change_request(client, seeded_ids, project_id, "😀" * 8001)
    _assert_too_long(rejected, "nl_request")
    assert _row_counts(db_session) == before


# -- POST /api/projects: ProjectCreate --


def test_project_nl_request_of_exactly_the_limit_is_accepted_and_stored_whole(
    client, db_session, seeded_ids
):
    text = "나" * 8000

    response = _post_project(client, seeded_ids, nl_request=text)

    assert response.status_code == 201
    stored = db_session.query(ChangeRequest).filter_by(project_id=response.json()["id"]).one()
    assert stored.nl_request == text


def test_project_nl_request_one_over_the_limit_is_rejected_and_creates_nothing(
    client, db_session, seeded_ids
):
    before = _row_counts(db_session)

    response = _post_project(client, seeded_ids, nl_request="나" * 8001)

    _assert_too_long(response, "nl_request")
    assert _row_counts(db_session) == before


def test_project_name_of_exactly_the_limit_is_accepted_and_stored_whole(
    client, db_session, seeded_ids
):
    name = "다" * 100

    response = _post_project(client, seeded_ids, name=name)

    assert response.status_code == 201
    assert response.json()["name"] == name


def test_project_name_one_over_the_limit_is_rejected_and_creates_nothing(
    client, db_session, seeded_ids
):
    before = _row_counts(db_session)

    response = _post_project(client, seeded_ids, name="다" * 101)

    _assert_too_long(response, "name")
    assert _row_counts(db_session) == before
