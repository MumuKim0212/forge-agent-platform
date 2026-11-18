"""ADR 0035 §Decision 5: the agent
engine selection is DB state, set by an admin, audited, and read per build claim.

Standard in-memory SQLite fixtures, dev auth mode (`X-Forge-User-Id`), no Docker. Every
expected value (paths, response keys, audit action and metadata keys, status codes) is a
literal here, never read back from the module under test.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.engine_selection import EngineSelection
from app.models.project import Project
from app.models.user import User
from app.services import engines
from app.services.build_executor import ExecutionResult
from app.services.roles import Role
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import run_iteration

PATH = "/api/platform/agent-engine"

ANTHROPIC_SENTINEL = "sk-ant-SENTINEL-key-value-0001-do-not-show"
OPENAI_SENTINEL = "sk-proj-SENTINEL-key-value-0002-do-not-show-abcdef"


def _h(user: User) -> dict[str, str]:
    return {"X-Forge-User-Id": user.id}


@pytest.fixture()
def requester(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.REQUESTER).first()


@pytest.fixture()
def approver(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.APPROVER).first()


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.ADMIN).first()


@pytest.fixture()
def keys(monkeypatch):
    """Both engines available, with sentinel key values the responses must never carry."""

    def _set(anthropic=ANTHROPIC_SENTINEL, openai=OPENAI_SENTINEL, default=None, model="gpt-5-mini"):
        monkeypatch.setattr(settings, "anthropic_api_key", anthropic)
        monkeypatch.setattr(settings, "openai_api_key", openai)
        monkeypatch.setattr(settings, "agent_engine_default", default)
        monkeypatch.setattr(settings, "openai_model", model)

    _set()
    return _set


def _audit(db_session) -> list[AuditLog]:
    return db_session.query(AuditLog).filter_by(action="platform.engine_changed").all()


# --- access: admin only, outside /api/console ---------------------------------------------------


@pytest.mark.parametrize("method", ["get", "patch"])
def test_an_anonymous_request_is_401(client, keys, method):
    resp = client.request(method.upper(), PATH, json={"engine": "openai"} if method == "patch" else None)

    assert resp.status_code == 401


@pytest.mark.parametrize("who", ["requester", "approver"])
def test_get_is_403_for_a_non_admin(client, keys, who, requester, approver):
    user = {"requester": requester, "approver": approver}[who]

    assert client.get(PATH, headers=_h(user)).status_code == 403


@pytest.mark.parametrize("who", ["requester", "approver"])
def test_patch_is_403_for_a_non_admin_and_changes_nothing(client, db_session, keys, who, requester, approver):
    user = {"requester": requester, "approver": approver}[who]

    resp = client.patch(PATH, headers=_h(user), json={"engine": "openai"})

    assert resp.status_code == 403
    assert db_session.query(EngineSelection).count() == 0
    assert _audit(db_session) == []


def test_the_role_check_runs_before_the_body_is_read(client, keys, requester):
    """A dependency, so a non-admin with a malformed body gets 403, not 422."""
    resp = client.patch(PATH, headers=_h(requester), json={"nonsense": 1})

    assert resp.status_code == 403


def test_the_selection_endpoint_is_not_under_the_read_only_console_router(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert sorted(paths[PATH]) == ["get", "patch"]
    assert [p for p in paths if p.startswith("/api/console") and "engine" in p] == []


# --- GET ----------------------------------------------------------------------------------------


def test_get_reports_each_engine_with_availability_model_image_and_selection(client, admin, keys):
    keys(default="openai", model="gpt-5-nano")

    body = client.get(PATH, headers=_h(admin)).json()

    assert set(body) == {"engines", "selected", "source", "set_by_id", "set_by_name", "set_at"}
    assert [e["engine"] for e in body["engines"]] == ["anthropic", "openai"]
    for entry in body["engines"]:
        assert set(entry) == {"engine", "available", "model", "image", "selected"}
    by_name = {e["engine"]: e for e in body["engines"]}
    assert by_name["openai"]["available"] is True and by_name["openai"]["selected"] is True
    assert by_name["openai"]["model"] == "gpt-5-nano"
    assert by_name["openai"]["image"] == settings.openai_agent_sandbox_image
    assert by_name["anthropic"]["available"] is True and by_name["anthropic"]["selected"] is False
    assert by_name["anthropic"]["model"] is None  # the Claude runner passes no model
    assert body["selected"] == "openai"
    # nothing stored: the selection is the environment default, and nobody set it
    assert (body["source"], body["set_by_id"], body["set_by_name"], body["set_at"]) == ("default", None, None, None)


def test_get_never_carries_key_material_nor_an_is_set_flag_beyond_availability(client, admin, keys):
    resp = client.get(PATH, headers=_h(admin))

    assert ANTHROPIC_SENTINEL not in resp.text and OPENAI_SENTINEL not in resp.text
    assert "sk-" not in resp.text
    for entry in resp.json()["engines"]:
        assert not [k for k in entry if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower()]


def test_an_engine_without_its_key_is_reported_unavailable(client, admin, keys):
    keys(anthropic=None, openai=OPENAI_SENTINEL)

    body = client.get(PATH, headers=_h(admin)).json()

    by_name = {e["engine"]: e for e in body["engines"]}
    assert by_name["anthropic"]["available"] is False
    assert by_name["openai"]["available"] is True
    assert body["selected"] == "openai"  # the only available engine is the default


def test_a_blank_key_is_not_available(client, admin, keys):
    keys(anthropic="   ", openai=OPENAI_SENTINEL)

    by_name = {e["engine"]: e for e in client.get(PATH, headers=_h(admin)).json()["engines"]}

    assert by_name["anthropic"]["available"] is False


# --- PATCH --------------------------------------------------------------------------------------


def test_an_admin_selects_an_available_engine_and_the_response_says_who_and_when(client, db_session, admin, keys):
    resp = client.patch(PATH, headers=_h(admin), json={"engine": "openai"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"] == "openai" and body["source"] == "stored"
    assert body["set_by_id"] == admin.id and body["set_by_name"] == admin.name
    assert body["set_at"] is not None
    assert {e["engine"]: e["selected"] for e in body["engines"]} == {"anthropic": False, "openai": True}
    row = db_session.get(EngineSelection, "agent_engine")
    assert (row.engine, row.set_by_id) == ("openai", admin.id)
    # and a later GET reads the same stored state
    assert client.get(PATH, headers=_h(admin)).json()["selected"] == "openai"


def test_the_selection_is_audited_with_actor_old_and_new(client, db_session, admin, keys):
    client.patch(PATH, headers=_h(admin), json={"engine": "openai"})
    client.patch(PATH, headers=_h(admin), json={"engine": "anthropic"})

    first, second = sorted(_audit(db_session), key=lambda e: e.ts)
    assert (first.actor_id, first.target) == (admin.id, "platform:agent_engine")
    # nothing stored before: the old engine is the effective default (both available -> anthropic)
    assert first.metadata_json == {"old_engine": "anthropic", "new_engine": "openai", "old_was_default": True}
    assert second.metadata_json == {"old_engine": "openai", "new_engine": "anthropic", "old_was_default": False}


def test_a_second_change_updates_the_one_row_rather_than_adding_another(client, db_session, admin, keys):
    client.patch(PATH, headers=_h(admin), json={"engine": "openai"})
    client.patch(PATH, headers=_h(admin), json={"engine": "anthropic"})

    rows = db_session.query(EngineSelection).all()

    assert [(r.id, r.engine) for r in rows] == [("agent_engine", "anthropic")]


def test_the_database_itself_refuses_a_second_row(db_session, admin):
    db_session.add(EngineSelection(id="agent_engine", engine="openai", set_by_id=admin.id))
    db_session.commit()
    db_session.add(EngineSelection(id="another", engine="anthropic", set_by_id=admin.id))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_an_unavailable_engine_is_422_and_nothing_is_stored_or_audited(client, db_session, admin, keys):
    keys(anthropic=ANTHROPIC_SENTINEL, openai=None)

    resp = client.patch(PATH, headers=_h(admin), json={"engine": "openai"})

    assert resp.status_code == 422
    assert "not available" in resp.json()["detail"] and "openai" in resp.json()["detail"]
    assert OPENAI_SENTINEL not in resp.text and ANTHROPIC_SENTINEL not in resp.text
    assert db_session.query(EngineSelection).count() == 0
    assert _audit(db_session) == []


@pytest.mark.parametrize("engine", ["gemini", "", "OpenAI", "anthropic ", "agent_engine"])
def test_an_unknown_engine_is_422(client, db_session, admin, keys, engine):
    resp = client.patch(PATH, headers=_h(admin), json={"engine": engine})

    assert resp.status_code == 422
    assert db_session.query(EngineSelection).count() == 0
    assert _audit(db_session) == []


@pytest.mark.parametrize("payload", [{}, {"engine": None}, {"engine": 7}, {"engine": ["openai"]}])
def test_a_malformed_body_is_422(client, db_session, admin, keys, payload):
    assert client.patch(PATH, headers=_h(admin), json=payload).status_code == 422
    assert db_session.query(EngineSelection).count() == 0


# --- resolve_engine reads the row, per call, and the row wins -----------------------------------


def _store(db_session, admin, engine):
    row = db_session.get(EngineSelection, "agent_engine")
    if row is None:
        db_session.add(EngineSelection(id="agent_engine", engine=engine, set_by_id=admin.id))
    else:
        row.engine = engine
    db_session.commit()


def test_the_admins_row_wins_over_the_environment_default(db_session, admin, keys):
    keys(default="anthropic")
    _store(db_session, admin, "openai")

    choice = engines.resolve_engine(db_session, settings)

    assert isinstance(choice, engines.EngineChoice) and choice.engine == "openai"


def test_with_no_row_the_environment_default_still_applies(db_session, keys):
    keys(default="openai")

    assert engines.resolve_engine(db_session, settings).engine == "openai"


def test_a_stored_engine_whose_key_was_removed_is_unavailable_never_the_other_engine(db_session, admin, keys):
    _store(db_session, admin, "openai")
    keys(anthropic=ANTHROPIC_SENTINEL, openai=None)  # the key goes away after the choice

    result = engines.resolve_engine(db_session, settings)

    assert isinstance(result, engines.Unavailable) and result.engine == "openai"
    assert "OPENAI_API_KEY" in result.detail and "not run on another engine" in result.detail


def test_a_stored_value_outside_the_closed_set_is_unavailable_and_names_no_key(db_session, admin, keys):
    _store(db_session, admin, "gemini")

    result = engines.resolve_engine(db_session, settings)

    assert isinstance(result, engines.Unavailable)
    assert result.engine == "gemini" and "not one of the known engines" in result.detail


# --- the worker: a change applies to the next claim, never to a claimed build --------------------


class RecordingExecutor:
    uses_engine = True

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []

    def execute(self, build: Build) -> ExecutionResult:
        self.seen.append((build.id, build.engine))
        return ExecutionResult(ok=True, summary="ran")


def _queue(db_session, seeded_ids, name):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.QUEUED)
    db_session.add(build)
    db_session.commit()
    return build


def test_a_change_between_two_claims_puts_the_second_build_on_the_new_engine(
    client, db_session, seeded_ids, admin, keys
):
    """Through the real endpoint, with ONE executor held across both claims as the worker holds
    it: the first build is claimed under the default (anthropic), the admin switches, the second is
    claimed under the new selection. The first build keeps the engine it recorded."""
    executor = RecordingExecutor()
    first = _queue(db_session, seeded_ids, "first")
    run_iteration(db_session, executor)

    assert client.patch(PATH, headers=_h(admin), json={"engine": "openai"}).status_code == 200
    second = _queue(db_session, seeded_ids, "second")
    run_iteration(db_session, executor)

    assert executor.seen == [(first.id, "anthropic"), (second.id, "openai")]
    db_session.refresh(first)
    db_session.refresh(second)
    assert (first.engine, second.engine) == ("anthropic", "openai")

    # and back again: the earlier two are untouched by a later switch
    assert client.patch(PATH, headers=_h(admin), json={"engine": "anthropic"}).status_code == 200
    third = _queue(db_session, seeded_ids, "third")
    run_iteration(db_session, executor)
    db_session.refresh(first)
    db_session.refresh(second)
    db_session.refresh(third)
    assert (first.engine, second.engine, third.engine) == ("anthropic", "openai", "anthropic")


def test_a_build_already_claimed_keeps_its_engine_when_the_selection_changes(
    client, db_session, seeded_ids, admin, keys
):
    """The switch lands while a build is RUNNING (claimed, engine recorded): it is not moved."""
    _store(db_session, admin, "anthropic")
    build = _queue(db_session, seeded_ids, "running")

    class SwitchesMidRun(RecordingExecutor):
        def execute(self, b: Build) -> ExecutionResult:
            result = super().execute(b)
            # the admin switches while this build is executing
            assert client.patch(PATH, headers=_h(admin), json={"engine": "openai"}).status_code == 200
            return result

    run_iteration(db_session, SwitchesMidRun())

    db_session.refresh(build)
    assert build.engine == "anthropic" and build.status == BuildStatus.SUCCEEDED


def test_the_executor_path_still_fails_closed_on_an_unavailable_stored_selection(
    db_session, seeded_ids, admin, keys
):
    """The fail-closed test, now driven by the DB row: openai is stored, its key is gone, only
    anthropic has one. The build fails ENGINE_UNREACHABLE before the executor and never runs on anthropic."""
    _store(db_session, admin, "openai")
    keys(anthropic=ANTHROPIC_SENTINEL, openai=None)
    build = _queue(db_session, seeded_ids, "closed")
    executor = RecordingExecutor()

    run_iteration(db_session, executor)

    db_session.refresh(build)
    assert executor.seen == []
    assert build.status == BuildStatus.FAILED and build.failure_reason == BuildFailureReason.ENGINE_UNREACHABLE
    assert build.engine == "openai"
    assert "not run on another engine" in build.failure_detail
    assert ANTHROPIC_SENTINEL not in build.failure_detail
