"""the per-service section of `GET /console/health` (admin only, read-only).

Reads what the worker persisted; nothing is checked when it is called. An ACTIVE service the worker
has not checked yet reads UNCHECKED, never healthy.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.service_health_check import ServiceHealthCheck
from app.models.user import User
from app.services.roles import Role
from app.services.status import BuildStatus, ProjectStatus

PATH = "/api/console/health"
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def _admin(db_session) -> dict[str, str]:
    return {"X-Forge-User-Id": db_session.query(User).filter_by(role=Role.ADMIN).first().id}


def _service(db_session, seeded_ids, name, *, status=DeploymentStatus.ACTIVE, check=None):
    owner = db_session.query(User).filter_by(role=Role.REQUESTER).first()
    project = Project(
        name=name, owner_id=owner.id, team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"], status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    deployment = Deployment(project_id=project.id, build_id=build.id, confirmed_by_id=owner.id, status=status)
    db_session.add(deployment)
    db_session.flush()
    if check is not None:
        db_session.add(ServiceHealthCheck(deployment_id=deployment.id, project_id=project.id, last_checked_at=T0,
                                          state_changed_at=T0, **check))
    db_session.commit()
    return deployment


def test_an_active_service_with_no_check_is_unchecked_and_a_stopped_one_is_absent(client, db_session, seeded_ids):
    active = _service(db_session, seeded_ids, "alpha")
    _service(db_session, seeded_ids, "stopped", status=DeploymentStatus.STOPPED)
    body = client.get(PATH, headers=_admin(db_session)).json()
    assert [(s["project_name"], s["deployment_id"], s["state"]) for s in body["services"]] == [
        ("alpha", active.id, "UNCHECKED")
    ]
    only = body["services"][0]
    assert (only["last_result"], only["last_checked_at"], only["failing_since"], only["last_failure_kind"]) == (
        None, None, None, None,
    )
    assert body["service_failure_threshold"] == settings.service_check_failure_threshold


def test_each_service_carries_its_state_failure_kind_and_since_when_unhealthy_first(client, db_session, seeded_ids):
    _service(db_session, seeded_ids, "b-fine", check=dict(state="HEALTHY", last_result="PASS", consecutive_failures=0))
    _service(db_session, seeded_ids, "z-unchecked")
    sick = _service(
        db_session, seeded_ids, "a-sick",
        check=dict(state="UNHEALTHY", last_result="FAIL", consecutive_failures=4,
                   last_failure_kind="RESTARTED", last_failure_at=T0, failing_since=T0),
    )
    body = client.get(PATH, headers=_admin(db_session)).json()
    assert [(s["project_name"], s["state"]) for s in body["services"]] == [
        ("a-sick", "UNHEALTHY"), ("z-unchecked", "UNCHECKED"), ("b-fine", "HEALTHY"),
    ]
    first = body["services"][0]
    assert first["deployment_id"] == sick.id
    assert (first["last_failure_kind"], first["consecutive_failures"]) == ("RESTARTED", 4)
    assert first["failing_since"] == "2026-11-14T10:00:00Z" or first["failing_since"].startswith("2026-11-14T10:00:00")
    assert first["failing_since"].endswith(("Z", "+00:00"))  # SQLite drops the offset; the route puts it back


def test_the_service_section_is_admin_only(client, db_session):
    requester = db_session.query(User).filter_by(role=Role.REQUESTER).first()
    assert client.get(PATH).status_code == 401
    assert client.get(PATH, headers={"X-Forge-User-Id": requester.id}).status_code == 403


def test_the_payload_names_no_container_id_env_or_url(client, db_session, seeded_ids):
    _service(db_session, seeded_ids, "alpha", check=dict(state="HEALTHY", last_result="PASS", consecutive_failures=0))
    service = client.get(PATH, headers=_admin(db_session)).json()["services"][0]
    assert set(service) == {
        "deployment_id", "project_id", "project_name", "state", "last_result", "consecutive_failures",
        "last_failure_kind", "last_failure_at", "failing_since", "last_checked_at", "state_changed_at", "stale",
    }


# -- staleness and checker liveness ----------------------------------------------------


def _checker_row(db_session, started, finished=None):
    from app.models.service_checker_status import ServiceCheckerStatus

    db_session.add(ServiceCheckerStatus(id="singleton", last_pass_started_at=started, last_pass_finished_at=finished))
    db_session.commit()


@pytest.fixture()
def _intervals(monkeypatch):
    monkeypatch.setattr(settings, "service_check_interval_seconds", 60.0)
    monkeypatch.setattr(settings, "service_check_cycle_timeout_seconds", 120.0)


def test_a_row_older_than_the_stale_window_is_flagged_stale_and_a_fresh_one_is_not(
    client, db_session, seeded_ids, _intervals
):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    for name, age in (("old", timedelta(seconds=181)), ("fresh", timedelta(seconds=170))):
        dep = _service(db_session, seeded_ids, name, check=dict(state="HEALTHY", last_result="PASS", consecutive_failures=0))
        row = db_session.get(ServiceHealthCheck, dep.id)
        row.last_checked_at = now - age
        db_session.commit()
    body = client.get(PATH, headers=_admin(db_session)).json()
    assert {s["project_name"]: s["stale"] for s in body["services"]} == {"old": True, "fresh": False}
    assert body["service_checker"]["stale_after_seconds"] == 180.0  # max(3 x 60, 60 + 120)


def test_the_stale_window_is_never_shorter_than_an_interval_plus_the_cycle_bound(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "service_check_interval_seconds", 10.0)
    monkeypatch.setattr(settings, "service_check_cycle_timeout_seconds", 300.0)
    body = client.get(PATH, headers=_admin(db_session)).json()
    assert body["service_checker"]["stale_after_seconds"] == 310.0


@pytest.mark.parametrize(
    "age_seconds, expected",
    [(None, "NONE"), (30, "ALIVE"), (179, "ALIVE"), (200, "STALE")],
)
def test_the_checker_is_none_alive_or_stale_by_when_a_pass_last_started(
    client, db_session, _intervals, age_seconds, expected
):
    from datetime import timedelta

    if age_seconds is not None:
        _checker_row(db_session, datetime.now(timezone.utc) - timedelta(seconds=age_seconds))
    checker = client.get(PATH, headers=_admin(db_session)).json()["service_checker"]
    assert checker["state"] == expected
    assert checker["interval_seconds"] == 60.0
    if age_seconds is None:
        assert checker["last_pass_started_at"] is None
    else:
        assert checker["last_pass_started_at"].endswith(("Z", "+00:00"))
