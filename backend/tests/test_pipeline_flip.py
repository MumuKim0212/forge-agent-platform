"""Pipeline flip: when the sandbox executor is active (the same
opt-in as worker._select_executor — `USE_SANDBOX_EXECUTOR` *and* at least one
engine's key, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`: engine-neutral,
ADR 0035 §Decision 5), the API creates builds as QUEUED
; otherwise behavior stays
exactly as before (`NOT_IMPLEMENTED`, ADR 0001: no fake pipeline states).

Configuration is driven directly via `settings` monkeypatching — no real
key or Docker is needed (the API only *creates* the row; the worker, not
under test here, is what would execute it).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.build import Build
from app.models.project import Project


def _create_project(client, seeded_ids) -> str:
    resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "flip-test",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "make a tool",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _mark_confirmed(db_session, project_id: str) -> None:
    """a modify request needs a confirmed ref; these tests are about the build it creates."""
    db_session.get(Project, project_id).confirmed_ref = "c0ffee0"
    db_session.commit()


def _build_statuses(db_session, project_id: str) -> list[str]:
    rows = db_session.query(Build).filter(Build.project_id == project_id).all()
    return [b.status for b in rows]


@pytest.fixture()
def executor_active(monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key-not-real")


@pytest.fixture()
def executor_inactive(monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", False)
    monkeypatch.setattr(settings, "anthropic_api_key", None)


def test_project_create_queues_build_when_executor_active(
    client, db_session, seeded_ids, executor_active
):
    project_id = _create_project(client, seeded_ids)
    assert _build_statuses(db_session, project_id) == ["QUEUED"]


def test_change_request_queues_build_when_executor_active(
    client, db_session, seeded_ids, executor_active
):
    project_id = _create_project(client, seeded_ids)
    _mark_confirmed(db_session, project_id)
    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "add a search box"},
    )
    assert resp.status_code == 201
    assert _build_statuses(db_session, project_id) == ["QUEUED", "QUEUED"]


def test_project_create_stays_not_implemented_when_executor_inactive(
    client, db_session, seeded_ids, executor_inactive
):
    project_id = _create_project(client, seeded_ids)
    assert _build_statuses(db_session, project_id) == ["NOT_IMPLEMENTED"]


def test_change_request_stays_not_implemented_when_executor_inactive(
    client, db_session, seeded_ids, executor_inactive
):
    project_id = _create_project(client, seeded_ids)
    _mark_confirmed(db_session, project_id)
    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={"nl_request": "add a search box"},
    )
    assert resp.status_code == 201
    assert _build_statuses(db_session, project_id) == ["NOT_IMPLEMENTED", "NOT_IMPLEMENTED"]


@pytest.mark.parametrize(
    ("use_sandbox", "anthropic_key", "openai_key"),
    [
        (True, None, None),
        (False, "test-key-not-real", None),
        (False, None, "test-openai-key-not-real"),
        (False, "test-key-not-real", "test-openai-key-not-real"),
    ],
)
def test_one_setting_alone_never_flips(
    client, db_session, seeded_ids, monkeypatch, use_sandbox, anthropic_key, openai_key
):
    """Same opt-in as worker._select_executor (engine-neutral): the flag
    without a key, or a key (either engine's) without the flag, must not start queueing builds."""
    monkeypatch.setattr(settings, "use_sandbox_executor", use_sandbox)
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic_key)
    monkeypatch.setattr(settings, "openai_api_key", openai_key)
    project_id = _create_project(client, seeded_ids)
    assert _build_statuses(db_session, project_id) == ["NOT_IMPLEMENTED"]


@pytest.mark.parametrize(
    ("anthropic_key", "openai_key"),
    [(None, "test-openai-key-not-real"), ("test-key-not-real", None), ("test-key-not-real", "test-openai-key-not-real")],
)
def test_the_flag_with_either_engine_s_key_queues_builds(
    client, db_session, seeded_ids, monkeypatch, anthropic_key, openai_key
):
    """an OpenAI-only deployment is as active as an Anthropic-only one."""
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic_key)
    monkeypatch.setattr(settings, "openai_api_key", openai_key)
    project_id = _create_project(client, seeded_ids)
    assert _build_statuses(db_session, project_id) == ["QUEUED"]
