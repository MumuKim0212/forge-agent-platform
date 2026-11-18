"""The admin-only eval endpoints and
how eval projects stay out of service life.

Standard in-memory SQLite fixtures, dev auth mode (`X-Forge-User-Id`), no Docker. Paths, response
keys and status codes are literals here, never read back from the module under test.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.eval_run import EvalCase, EvalRun
from app.models.project import Project
from app.models.user import User
from app.services.status import BuildStatus, ProjectStatus
from tests.eval_request_set_support import pin_v1_request_set
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

RUNS = "/api/evals/runs"
STATUS = "/api/evals/status"


def _h(user: User) -> dict[str, str]:
    return {"X-Forge-User-Id": user.id}


@pytest.fixture()
def requester(db_session) -> User:
    return db_session.query(User).filter_by(role="requester").first()


@pytest.fixture()
def approver(db_session) -> User:
    return db_session.query(User).filter_by(role="approver").first()


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role="admin").first()


@pytest.fixture(autouse=True)
def frozen_request_set(monkeypatch, tmp_path):
    """The API tests count cases and read their ids; they run against a frozen set v1 so the
    shipped set can grow by version without rewriting them (`tests/test_eval_request_set.py`
    pins what the shipped file contains)."""
    pin_v1_request_set(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def engine_ready(monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "openai_api_key", "fake-key-never-used")
    monkeypatch.setattr(settings, "agent_engine_default", "openai")
    monkeypatch.setattr(settings, "openai_model", "gpt-5-mini")
    monkeypatch.setattr(settings, "eval_run_budget_usd", 1.0)


# --- access -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", RUNS), ("GET", RUNS), ("GET", RUNS + "/some-id"), ("GET", STATUS)],
)
def test_every_eval_endpoint_is_401_without_an_identity(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("who", ["requester", "approver"])
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", RUNS), ("GET", RUNS), ("GET", RUNS + "/some-id"), ("GET", STATUS)],
)
def test_every_eval_endpoint_is_403_for_a_requester_or_an_approver(
    client, db_session, requester, approver, who, method, path
):
    user = requester if who == "requester" else approver

    resp = client.request(method, path, headers=_h(user))

    assert resp.status_code == 403
    assert db_session.query(EvalRun).count() == 0  # and nothing was started by the refused POST


def test_the_eval_routes_are_exactly_these_four_and_none_is_under_the_read_only_console():
    from app.main import app

    paths = app.openapi()["paths"]
    assert {p: sorted(m) for p, m in paths.items() if "/evals" in p} == {
        "/api/evals/runs": ["get", "post"],
        "/api/evals/runs/{run_id}": ["get"],
        "/api/evals/status": ["get"],
    }
    assert not [p for p in paths if p.startswith("/api/console") and "eval" in p]


# --- the status read ---------------------------------------


def test_status_states_the_effective_settings_and_that_a_run_could_start(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "eval_run_budget_usd", 2.5)
    monkeypatch.setattr(settings, "eval_case_min_budget_usd", 0.2)

    resp = client.get(STATUS, headers=_h(admin))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "budget_usd",
        "case_min_budget_usd",
        "executor_active",
        "engine",
        "model",
        "engine_available",
        "run_in_progress",
        "request_set_version",
        "case_count",
    }
    assert body["budget_usd"] == 2.5 and body["case_min_budget_usd"] == 0.2
    assert body["executor_active"] is True
    assert (body["engine"], body["model"], body["engine_available"]) == ("openai", "gpt-5-mini", True)
    assert body["run_in_progress"] is False
    assert (body["request_set_version"], body["case_count"]) == (1, 3)


def test_status_says_the_executor_is_inactive_when_the_flag_is_off_even_with_a_key(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", False)

    body = client.get(STATUS, headers=_h(admin)).json()

    assert body["executor_active"] is False
    assert body["engine_available"] is True  # the key is there; it is the flag that is off


def test_status_says_the_executor_is_inactive_when_no_engine_has_a_key(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    body = client.get(STATUS, headers=_h(admin)).json()

    assert body["executor_active"] is False
    assert body["engine_available"] is False


def test_status_names_an_unavailable_selected_engine_and_its_model_without_calling_it_available(
    client, admin, monkeypatch
):
    monkeypatch.setattr(settings, "agent_engine_default", "anthropic")  # selected, but no key set

    body = client.get(STATUS, headers=_h(admin)).json()

    assert (body["engine"], body["engine_available"]) == ("anthropic", False)
    assert body["executor_active"] is True  # openai still has a key, so the executor runs


def test_status_follows_a_run_in_progress(client, admin):
    assert client.get(STATUS, headers=_h(admin)).json()["run_in_progress"] is False
    client.post(RUNS, headers=_h(admin))

    assert client.get(STATUS, headers=_h(admin)).json()["run_in_progress"] is True


def test_status_has_no_request_set_figures_when_the_set_cannot_be_loaded(client, admin, monkeypatch, tmp_path):
    from app.services import eval_request_set

    monkeypatch.setattr(eval_request_set, "REQUEST_SET_PATH", tmp_path / "missing.json")

    body = client.get(STATUS, headers=_h(admin)).json()

    assert (body["request_set_version"], body["case_count"]) == (None, None)


def test_status_never_carries_a_key(client, admin, monkeypatch):
    sentinels = ["sk-sentinel-openai-0123456789", "sk-ant-sentinel-0123456789"]
    monkeypatch.setattr(settings, "openai_api_key", sentinels[0])
    monkeypatch.setattr(settings, "anthropic_api_key", sentinels[1])

    resp = client.get(STATUS, headers=_h(admin))

    assert resp.status_code == 200
    for sentinel in sentinels:
        assert sentinel not in resp.text
    assert "key" not in " ".join(resp.json())  # no field is named for one


# --- start ------------------------------------------------------------------------------------


def test_start_returns_the_run_with_its_budget_spend_and_pending_cases(client, db_session, admin):
    resp = client.post(RUNS, headers=_h(admin))

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert (body["engine"], body["model"]) == ("openai", "gpt-5-mini")
    assert body["request_set_version"] == 1
    assert body["budget_usd"] == 1.0 and body["spend_usd"] == 0.0
    assert body["started_by_id"] == admin.id and body["started_by_name"] == admin.name
    assert (body["total"], body["passed"], body["failed"], body["not_run"]) == (3, 0, 0, 0)
    assert body["ended_at"] is None and body["duration_seconds"] is None
    assert [(c["case_id"], c["state"], c["result"], c["build_id"]) for c in body["cases"]] == [
        ("static-page", "PENDING", None, None),
        ("database-page", "PENDING", None, None),
        ("write-then-read-form", "PENDING", None, None),
    ]
    audit = db_session.query(AuditLog).filter_by(action="eval.run_started").one()
    assert audit.actor_id == admin.id and audit.target == f"eval_run:{body['id']}"


def test_the_runs_api_returns_the_prompt_fingerprint_and_null_for_an_old_run(client, db_session, admin):
    import hashlib

    from app.services.agent_prompt import compose_system_prompt

    started = client.post(RUNS, headers=_h(admin)).json()
    expected = hashlib.sha256(compose_system_prompt("create").encode("utf-8")).hexdigest()[:12]
    run = db_session.get(EvalRun, started["id"])
    run.status = "COMPLETED"
    db_session.commit()
    legacy = EvalRun(
        status="COMPLETED", engine="openai", model=None, request_set_version=1, budget_usd=1.0, started_by_id=admin.id
    )
    db_session.add(legacy)
    db_session.commit()

    listing = {r["id"]: r for r in client.get(RUNS, headers=_h(admin)).json()}
    detail = client.get(f"{RUNS}/{started['id']}", headers=_h(admin)).json()

    assert started["prompt_fingerprint"] == expected
    assert listing[started["id"]]["prompt_fingerprint"] == expected
    assert detail["prompt_fingerprint"] == expected
    assert listing[legacy.id]["prompt_fingerprint"] is None


@pytest.mark.parametrize("setting", [0, 2])
def test_the_runs_api_returns_the_repair_setting_the_run_recorded_and_null_for_an_old_run(
    client, db_session, admin, monkeypatch, setting
):
    monkeypatch.setattr(settings, "build_repair_max_attempts", setting)
    started = client.post(RUNS, headers=_h(admin)).json()
    db_session.get(EvalRun, started["id"]).status = "COMPLETED"
    legacy = EvalRun(
        status="COMPLETED", engine="openai", model=None, request_set_version=1, budget_usd=1.0, started_by_id=admin.id
    )
    db_session.add(legacy)
    db_session.commit()
    monkeypatch.setattr(settings, "build_repair_max_attempts", 3)  # the read serves the record, not the setting

    listing = {r["id"]: r for r in client.get(RUNS, headers=_h(admin)).json()}
    detail = client.get(f"{RUNS}/{started['id']}", headers=_h(admin)).json()
    audit = db_session.query(AuditLog).filter_by(action="eval.run_started").one()

    assert started["repair_max_attempts"] == setting  # 0 is served as 0
    assert detail["repair_max_attempts"] == setting
    assert listing[started["id"]]["repair_max_attempts"] == setting
    assert "repair_max_attempts" in listing[legacy.id] and listing[legacy.id]["repair_max_attempts"] is None
    assert audit.metadata_json["repair_max_attempts"] == setting


def test_a_case_serves_the_attempts_its_build_ran_and_null_when_not_measured(client, db_session, admin):
    run = client.post(RUNS, headers=_h(admin)).json()
    cases = db_session.query(EvalCase).filter_by(run_id=run["id"]).order_by(EvalCase.position).all()
    cases[0].state, cases[0].result, cases[0].repair_attempts = "DONE", "PASSED", 2
    cases[1].state, cases[1].result, cases[1].repair_attempts = "DONE", "PASSED", 0
    db_session.commit()

    body = client.get(f"{RUNS}/{run['id']}", headers=_h(admin)).json()

    assert [c["repair_attempts"] for c in body["cases"]] == [2, 0, None]
    assert all("repair_attempts" in c for c in body["cases"])


def test_the_budget_in_the_response_is_the_setting_at_the_time(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "eval_run_budget_usd", 0.75)

    assert client.post(RUNS, headers=_h(admin)).json()["budget_usd"] == 0.75


def test_a_second_start_while_one_is_running_is_409(client, db_session, admin):
    first = client.post(RUNS, headers=_h(admin))

    second = client.post(RUNS, headers=_h(admin))

    assert (first.status_code, second.status_code) == (201, 409)
    assert "already in progress" in second.json()["detail"]
    assert db_session.query(EvalRun).count() == 1
    assert db_session.query(AuditLog).filter_by(action="eval.run_started").count() == 1


def test_start_is_422_when_the_executor_is_inactive(client, db_session, admin, monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", False)

    resp = client.post(RUNS, headers=_h(admin))

    assert resp.status_code == 422 and "USE_SANDBOX_EXECUTOR" in resp.json()["detail"]
    assert db_session.query(EvalRun).count() == 0


def test_start_is_422_when_the_selected_engine_is_unavailable(client, db_session, admin, monkeypatch):
    monkeypatch.setattr(settings, "agent_engine_default", "anthropic")  # selected, but no key set

    resp = client.post(RUNS, headers=_h(admin))

    assert resp.status_code == 422 and "anthropic" in resp.json()["detail"]
    assert db_session.query(EvalRun).count() == 0


# --- read -------------------------------------------------------------------------------------


def test_list_is_newest_first_with_pass_counts_and_detail_carries_the_cases(client, db_session, admin):
    first = client.post(RUNS, headers=_h(admin)).json()
    run = db_session.get(EvalRun, first["id"])
    run.status = "COMPLETED"
    db_session.commit()
    second = client.post(RUNS, headers=_h(admin)).json()
    cases = db_session.query(EvalCase).filter_by(run_id=first["id"]).order_by(EvalCase.position).all()
    cases[0].state, cases[0].result, cases[0].cost_usd = "DONE", "PASSED", 0.12
    cases[1].state, cases[1].result, cases[1].failure_reason = "DONE", "FAILED", "CHECK_FAILED"
    cases[1].failed_check_index = 0
    cases[1].check_outcomes = [{"index": 0, "ok": False, "outcome": "expected status 200, got 500"}]
    cases[2].state, cases[2].result = "DONE", "NOT_RUN_BUDGET"
    db_session.commit()

    listing = client.get(RUNS, headers=_h(admin))
    detail = client.get(f"{RUNS}/{first['id']}", headers=_h(admin))

    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()] == [second["id"], first["id"]]
    older = listing.json()[1]
    assert (older["total"], older["passed"], older["failed"], older["not_run"]) == (3, 1, 1, 1)
    assert "cases" not in older  # the list is the summary; the cases come with the detail
    assert detail.status_code == 200
    body = detail.json()
    assert [c["result"] for c in body["cases"]] == ["PASSED", "FAILED", "NOT_RUN_BUDGET"]
    assert body["cases"][0]["cost_usd"] == 0.12
    assert body["cases"][1]["failed_check_index"] == 0
    assert body["cases"][1]["check_outcomes"][0]["outcome"] == "expected status 200, got 500"


def test_an_unknown_run_is_404(client, admin):
    assert client.get(f"{RUNS}/nope", headers=_h(admin)).status_code == 404


# --- eval projects are not service life -------------------------------------------------------


def _project(db, seeded_ids, owner_id, name, kind):
    project = Project(
        name=name,
        owner_id=owner_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
        kind=kind,
    )
    db.add(project)
    db.commit()
    return project


def test_the_registry_hides_eval_projects_unless_asked(client, db_session, admin, seeded_ids):
    normal = _project(db_session, seeded_ids, admin.id, "a-normal-service", "normal")
    evaluation = _project(db_session, seeded_ids, admin.id, "eval-1234abcd-static-page", "eval")

    default = client.get("/api/projects", headers=_h(admin)).json()
    only_eval = client.get("/api/projects?kind=eval", headers=_h(admin)).json()
    only_normal = client.get("/api/projects?kind=normal", headers=_h(admin)).json()

    assert [p["id"] for p in default] == [normal.id]
    assert [p["id"] for p in only_eval] == [evaluation.id]
    assert [p["id"] for p in only_normal] == [normal.id]
    assert only_eval[0]["kind"] == "eval" and default[0]["kind"] == "normal"
    # Hidden from the list, not from lookup: its builds and trace stay readable.
    assert client.get(f"/api/projects/{evaluation.id}", headers=_h(admin)).status_code == 200


class _Manager:
    def image_exists(self, image_ref):
        return True


def _deployable(db, project):
    build = Build(
        project_id=project.id,
        type="create",
        status=BuildStatus.SUCCEEDED,
        image_ref="img-1",
        diff_ref="forge-build#1",
    )
    db.add(build)
    db.flush()
    db.add(
        ApprovalRequest(
            build_id=build.id,
            project_id=project.id,
            requester_id=project.owner_id,
            risk_tier="low",
            status=ApprovalStatus.AUTO_APPROVED,
            required_approvals=1,
        )
    )
    db.commit()
    return build


def test_deploy_confirmation_is_409_for_an_eval_project_and_a_normal_one_in_the_same_state_is_not(
    client, db_session, admin, seeded_ids, monkeypatch
):
    import app.routers.deployments as deployments_router

    monkeypatch.setattr(deployments_router, "_deploy_manager", lambda: _Manager())
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    normal = _project(db_session, seeded_ids, admin.id, "deployable-normal", "normal")
    evaluation = _project(db_session, seeded_ids, admin.id, "eval-1234abcd-database-page", "eval")
    normal_build, eval_build = _deployable(db_session, normal), _deployable(db_session, evaluation)

    # An admin who owns it, a SUCCEEDED auto-approved build with an image: everything a deploy needs.
    refused = client.post(
        f"/api/projects/{evaluation.id}/deployments", json={"build_id": eval_build.id}, headers=_h(admin)
    )
    allowed = client.post(
        f"/api/projects/{normal.id}/deployments", json={"build_id": normal_build.id}, headers=_h(admin)
    )

    assert refused.status_code == 409 and "eval project" in refused.json()["detail"]
    assert allowed.status_code == 201
    from app.models.deployment import Deployment

    assert db_session.query(Deployment).filter_by(project_id=evaluation.id).count() == 0
