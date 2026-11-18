"""a QUEUED build on the timeline says why it is waiting, as numbers and nothing else.

- `queue` is `{ahead, at_cap}` for a QUEUED build and `None` for every other status;
- `ahead` counts QUEUED and RUNNING builds created earlier, across all projects, and never names them;
- `at_cap` is whether the RUNNING builds fill `settings.build_concurrency`;
- the timeline build entry has no key that could carry another project's identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.build import Build
from app.models.project import Project
from app.services.build_queue import queue_wait
from app.services.status import BuildStatus, ProjectStatus
from tests.test_measured_facts_api import _as, project_row  # noqa: F401 - a fixture, used by name

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _other_project(db_session, seeded_ids, name):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.BUILD_PENDING,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _build(db_session, project, status, minute):
    build = Build(project_id=project.id, type="create", status=status, created_at=T0 + timedelta(minutes=minute))
    db_session.add(build)
    db_session.flush()
    return build


def _timeline_build(client, project, build, user_id):
    resp = client.get(f"/api/projects/{project.id}/timeline", headers=_as(user_id))
    assert resp.status_code == 200
    (entry,) = [e for e in resp.json() if e["kind"] == "build" and e["data"]["id"] == build.id]
    return entry["data"]


@pytest.fixture
def cap(monkeypatch):
    def _set(n):
        monkeypatch.setattr(settings, "build_concurrency", n)

    _set(1)
    return _set


def test_nothing_ahead_is_zero_and_not_at_cap(client, db_session, project_row, seeded_ids, cap):  # noqa: F811
    build = _build(db_session, project_row, BuildStatus.QUEUED, 5)
    db_session.commit()

    data = _timeline_build(client, project_row, build, seeded_ids["user_id"])

    assert data["queue"] == {"ahead": 0, "at_cap": False}


def test_one_ahead_counts_one(client, db_session, project_row, seeded_ids, cap):  # noqa: F811
    _build(db_session, project_row, BuildStatus.QUEUED, 1)
    mine = _build(db_session, project_row, BuildStatus.QUEUED, 2)
    db_session.commit()

    data = _timeline_build(client, project_row, mine, seeded_ids["user_id"])

    assert data["queue"] == {"ahead": 1, "at_cap": False}


def test_many_ahead_counts_queued_and_running_of_other_projects_and_is_at_cap(
    client, db_session, project_row, seeded_ids, cap  # noqa: F811
):
    cap(2)
    other_a = _other_project(db_session, seeded_ids, "Secret Project Alpha")
    other_b = _other_project(db_session, seeded_ids, "Secret Project Beta")
    _build(db_session, other_a, BuildStatus.RUNNING, 1)
    _build(db_session, other_b, BuildStatus.RUNNING, 2)
    _build(db_session, other_a, BuildStatus.QUEUED, 3)
    _build(db_session, other_b, BuildStatus.QUEUED, 4)
    _build(db_session, project_row, BuildStatus.QUEUED, 5)
    mine = _build(db_session, project_row, BuildStatus.QUEUED, 6)
    db_session.commit()

    data = _timeline_build(client, project_row, mine, seeded_ids["user_id"])

    assert data["queue"] == {"ahead": 5, "at_cap": True}


def test_only_earlier_builds_count_and_finished_ones_do_not(client, db_session, project_row, seeded_ids, cap):  # noqa: F811
    other = _other_project(db_session, seeded_ids, "Other")
    _build(db_session, other, BuildStatus.SUCCEEDED, 1)
    _build(db_session, other, BuildStatus.FAILED, 2)
    _build(db_session, other, BuildStatus.NOT_IMPLEMENTED, 3)
    mine = _build(db_session, project_row, BuildStatus.QUEUED, 4)
    _build(db_session, other, BuildStatus.QUEUED, 5)  # later than mine: not ahead
    db_session.commit()

    data = _timeline_build(client, project_row, mine, seeded_ids["user_id"])

    assert data["queue"] == {"ahead": 0, "at_cap": False}


def test_a_running_build_ahead_counts(client, db_session, project_row, seeded_ids, cap):  # noqa: F811
    cap(3)
    other = _other_project(db_session, seeded_ids, "Other")
    _build(db_session, other, BuildStatus.RUNNING, 1)
    mine = _build(db_session, project_row, BuildStatus.QUEUED, 2)
    db_session.commit()

    data = _timeline_build(client, project_row, mine, seeded_ids["user_id"])

    assert data["queue"] == {"ahead": 1, "at_cap": False}


@pytest.mark.parametrize(
    "status",
    [BuildStatus.RUNNING, BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.NOT_IMPLEMENTED],
)
def test_a_build_that_is_not_queued_carries_no_queue_value(
    client, db_session, project_row, seeded_ids, cap, status  # noqa: F811
):
    other = _other_project(db_session, seeded_ids, "Other")
    _build(db_session, other, BuildStatus.QUEUED, 1)
    build = _build(db_session, project_row, status, 2)
    db_session.commit()

    data = _timeline_build(client, project_row, build, seeded_ids["user_id"])

    assert data["queue"] is None
    assert queue_wait(db_session, build, concurrency_cap=1) is None


def test_the_queue_value_names_no_other_project_build_or_owner(client, db_session, project_row, seeded_ids, cap):  # noqa: F811
    other = _other_project(db_session, seeded_ids, "Secret Project Alpha")
    theirs = _build(db_session, other, BuildStatus.RUNNING, 1)
    mine = _build(db_session, project_row, BuildStatus.QUEUED, 2)
    db_session.commit()

    resp = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"]))
    data = _timeline_build(client, project_row, mine, seeded_ids["user_id"])

    assert set(data["queue"]) == {"ahead", "at_cap"}
    assert all(type(v) in (int, bool) for v in data["queue"].values())
    body = resp.text
    assert "Secret Project Alpha" not in body
    assert theirs.id not in body
    assert other.id not in body
