"""`builds.repair_attempts` reaches the people who judge the build.

- the project timeline's `build` entry and the approval detail carry it as stored: an int, and 0 is
  served as 0 (it means "the first attempt shipped, or the loop was off"), never as null;
- the approval list summary is not changed (detail view only).

`repair_routes` sits beside it: the failing routes each repair attempt was started for, served as
stored on the detail, the timeline entry and `BuildOut`; NULL stays null; a stored value that is not
the shape the executor writes is served as null, never passed through; the list summary and the eval
schemas do not carry it.

Same fixtures as `test_smoke_checks_api.py` / `test_preview_start_failure.py`. The value is written
straight onto the build row, so each assertion is about the read path alone.
"""

from __future__ import annotations

import pytest

from app.models.user import User
from app.schemas.build import BuildOut
from app.schemas.eval_run import EvalCaseOut, EvalRunDetailOut, EvalRunOut
from tests.test_measured_facts_api import _as, project_row  # noqa: F401 - a fixture, used by name
from tests.test_smoke_checks_api import _build, _timeline_build


@pytest.mark.parametrize("attempts", [0, 1, 3])
def test_the_count_reaches_the_approval_detail_and_the_timeline_as_stored(
    client, db_session, project_row, seeded_ids, attempts  # noqa: F811
):
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)
    build.repair_attempts = attempts
    db_session.commit()
    uid = seeded_ids["user_id"]

    detail = client.get(f"/api/approvals/{request.id}", headers=_as(uid)).json()
    timeline = _timeline_build(client, project_row, build, uid)

    assert detail["repair_attempts"] == attempts
    assert isinstance(detail["repair_attempts"], int)
    assert timeline["repair_attempts"] == attempts
    assert isinstance(timeline["repair_attempts"], int)


def test_a_build_that_never_repaired_serves_zero_not_null_on_both(client, db_session, project_row, seeded_ids):  # noqa: F811
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)  # the column default
    uid = seeded_ids["user_id"]

    detail = client.get(f"/api/approvals/{request.id}", headers=_as(uid)).json()
    timeline = _timeline_build(client, project_row, build, uid)

    assert "repair_attempts" in detail and detail["repair_attempts"] == 0
    assert "repair_attempts" in timeline and timeline["repair_attempts"] == 0


def test_the_pending_list_summary_does_not_carry_the_count(client, db_session, project_row, seeded_ids):  # noqa: F811
    build, _ = _build(db_session, project_row, seeded_ids, smoke=None)
    build.repair_attempts = 2
    db_session.commit()
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    resp = client.get("/api/approvals", headers=_as(approver.id))

    assert resp.status_code == 200
    assert resp.json() and all("repair_attempts" not in row for row in resp.json())


def test_the_detail_read_still_needs_authentication(client, db_session, project_row, seeded_ids):  # noqa: F811
    _, request = _build(db_session, project_row, seeded_ids, smoke=None)

    assert client.get(f"/api/approvals/{request.id}").status_code == 401


# --- builds.repair_routes -----------------------------------------------------------------------

STORED = [
    {"attempt": 1, "routes": [{"path": "/boom", "status": 500, "outcome": "server_error"}]},
    {
        "attempt": 2,
        "routes": [
            {"path": "/boom", "status": 503, "outcome": "server_error"},
            {"path": "/slow", "status": None, "outcome": "no_answer"},
        ],
    },
]


def _served_three_ways(client, db_session, project_row, seeded_ids, stored):  # noqa: F811
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)
    build.repair_routes = stored
    db_session.commit()
    uid = seeded_ids["user_id"]
    detail = client.get(f"/api/approvals/{request.id}", headers=_as(uid)).json()
    timeline = _timeline_build(client, project_row, build, uid)
    db_session.refresh(build)
    return detail, timeline, BuildOut.model_validate(build).model_dump(mode="json")


def test_the_routes_reach_the_detail_the_timeline_and_the_build_schema_as_stored(
    client, db_session, project_row, seeded_ids  # noqa: F811
):
    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, STORED)

    assert detail["repair_routes"] == STORED
    assert timeline["repair_routes"] == STORED
    assert schema["repair_routes"] == STORED


def test_a_build_with_no_repair_serves_null_on_all_three_not_an_empty_list(
    client, db_session, project_row, seeded_ids  # noqa: F811
):
    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, None)

    assert "repair_routes" in detail and detail["repair_routes"] is None
    assert "repair_routes" in timeline and timeline["repair_routes"] is None
    assert "repair_routes" in schema and schema["repair_routes"] is None


_GOOD_ROUTE = {"path": "/boom", "status": 500, "outcome": "server_error"}


@pytest.mark.parametrize(
    "malformed",
    [
        "not a list",
        {"attempt": 1, "routes": [_GOOD_ROUTE]},  # one entry, not a list of them
        [],  # a started repair is never an empty list
        [{"attempt": 1, "routes": []}],
        [{"attempt": 1}],
        [{"attempt": "1", "routes": [_GOOD_ROUTE]}],
        [{"attempt": 1, "routes": [{"path": "/boom", "status": 500, "outcome": "kaboom"}]}],
        [{"attempt": 1, "routes": [{"path": "/boom", "status": "500", "outcome": "server_error"}]}],
        [{"attempt": 1, "routes": [{"path": "/boom", "status": 500}]}],
        [{"attempt": 1, "routes": [{**_GOOD_ROUTE, "body": "BODY-MARKER"}]}],
        [{"attempt": 1, "routes": [_GOOD_ROUTE], "summary": "SUMMARY-MARKER"}],
        [{"attempt": 1, "routes": [_GOOD_ROUTE]}, "second entry is not an object"],
        ["/boom"],
    ],
)
def test_a_stored_value_that_is_not_the_written_shape_is_served_as_null_never_passed_through(
    client, db_session, project_row, seeded_ids, malformed  # noqa: F811
):
    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, malformed)

    assert detail["repair_routes"] is None
    assert timeline["repair_routes"] is None
    assert schema["repair_routes"] is None
    assert "MARKER" not in str((detail, timeline, schema))


def test_the_pending_list_summary_does_not_carry_the_routes(client, db_session, project_row, seeded_ids):  # noqa: F811
    build, _ = _build(db_session, project_row, seeded_ids, smoke=None)
    build.repair_routes = STORED
    db_session.commit()
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    resp = client.get("/api/approvals", headers=_as(approver.id))

    assert resp.status_code == 200
    assert resp.json() and all("repair_routes" not in row for row in resp.json())


def test_the_eval_run_and_case_schemas_do_not_carry_the_routes():
    for model in (EvalCaseOut, EvalRunOut, EvalRunDetailOut):
        assert "repair_routes" not in model.model_fields, model.__name__


# --- SQL findings (ADR 0047 §Amendment 2026-11-25): the second thing a repair may have been started for ---

_SQL = {"path": "app/app.py", "line": 116, "error": 'syntax error at or near "end"'}


def test_an_entry_with_sql_findings_is_served_as_stored_including_one_with_no_routes(
    client, db_session, project_row, seeded_ids  # noqa: F811
):
    stored = [
        {"attempt": 1, "routes": [_GOOD_ROUTE], "sql": [_SQL]},
        {"attempt": 2, "routes": [], "sql": [_SQL, {**_SQL, "line": 125}]},
    ]

    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, stored)

    assert detail["repair_routes"] == stored
    assert timeline["repair_routes"] == stored
    assert schema["repair_routes"] == stored


def test_an_entry_without_findings_is_still_served_without_a_sql_key(client, db_session, project_row, seeded_ids):  # noqa: F811
    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, STORED)

    for served in (detail, timeline, schema):
        assert all("sql" not in entry for entry in served["repair_routes"])


@pytest.mark.parametrize(
    "malformed",
    [
        [{"attempt": 1, "routes": [], "sql": []}],  # neither a route nor a finding
        [{"attempt": 1, "routes": [], "sql": "syntax error"}],
        [{"attempt": 1, "routes": [], "sql": [{"path": "app/app.py", "line": "3", "error": "syntax error"}]}],
        [{"attempt": 1, "routes": [], "sql": [{"path": "app/app.py", "line": 3}]}],
        [{"attempt": 1, "routes": [], "sql": [{**_SQL, "statement": "SELECT end FROM STATEMENT-MARKER"}]}],
        [{"attempt": 1, "routes": [_GOOD_ROUTE], "sql": [_SQL, "second finding is not an object"]}],
    ],
)
def test_a_malformed_sql_part_serves_the_whole_value_as_null(client, db_session, project_row, seeded_ids, malformed):  # noqa: F811
    detail, timeline, schema = _served_three_ways(client, db_session, project_row, seeded_ids, malformed)

    assert detail["repair_routes"] is None
    assert timeline["repair_routes"] is None
    assert schema["repair_routes"] is None
    assert "MARKER" not in str((detail, timeline, schema))
