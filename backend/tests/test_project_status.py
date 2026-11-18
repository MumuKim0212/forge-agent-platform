"""Project status forward transitions.

`Project.status` moves forward through the pipeline, through ONE function
(`services/project_status.advance`), at four events. Every transition test here
drives the code that really fires it -- the API for a created build, the worker's
`run_one_build` for a succeeded one, `create_approval_request_for_verdict` for a
routed one, `execute_deployment` for an activated one -- never `advance` itself
(that is what the unit tests at the top are for). No Docker, no network.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.eval_run import EvalRunStatus
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.services import project_lifecycle
from app.services.account_status import AccountStatus
from app.services.approval import create_approval_request_for_verdict
from app.services.build_executor import ExecutionResult
from app.services.build_queue import claim_next_build, mark_succeeded
from app.services.deploy import (
    DeployProjectArchivedError,
    confirm_deploy,
    execute_deployment,
    request_rollback,
    stop_active_deployment,
)
from app.services.policy_checks import PolicyRiskTier
from app.services.project_status import FORWARD_ORDER, STATUS_CHANGED_ACTION, advance
from app.services.sandbox_build_executor import SandboxBuildExecutor
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import run_one_build
from tests.fake_admin_dsn import FAKE_ADMIN_DSN
from tests.test_deploy_service import (
    FakeDeployManager,
    FakeMergeProvider,
    _fake_ensure_project_redis,
    _fake_provision_project_data_services,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# The literal order, written out here rather than read from FORWARD_ORDER: the spec's table.
ORDER = ["DRAFT", "SUBMITTED", "BUILD_PENDING", "PREVIEW_READY", "AWAITING_APPROVAL", "ACTIVE"]


@pytest.fixture(autouse=True)
def _fake_data_services(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        _fake_provision_project_data_services,
    )
    monkeypatch.setattr(
        "app.services.deploy.service_data.ensure_project_redis", _fake_ensure_project_redis
    )
    # Promotion tears a build's preview down through a real `PreviewManager` (Docker + the
    # preview data stores). Status does not depend on it and this suite must not reach either.
    monkeypatch.setattr("app.services.deploy._teardown_preview_on_promotion", lambda db, build, manager: None)


@pytest.fixture()
def executor_active(monkeypatch):
    """Builds are created QUEUED (the same opt-in as tests/test_pipeline_flip.py)."""
    monkeypatch.setattr(settings, "use_sandbox_executor", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key-not-real")


def _headers(seeded_ids):
    return {"X-Forge-User-Id": seeded_ids["user_id"]}


def _create_project(client, seeded_ids, name="status-project") -> str:
    resp = client.post(
        "/api/projects",
        headers=_headers(seeded_ids),
        json={
            "name": name,
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "make a tool",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _submit_change_request(client, seeded_ids, project_id) -> None:
    resp = client.post(
        f"/api/projects/{project_id}/change-requests",
        headers=_headers(seeded_ids),
        json={"nl_request": "add a search box"},
    )
    assert resp.status_code == 201


def _status(db, project_id) -> str:
    db.expire_all()
    return db.get(Project, project_id).status


def _transitions(db, project_id) -> list[tuple[str, str, str]]:
    db.expire_all()
    rows = (
        db.query(AuditLog)
        .filter_by(action=STATUS_CHANGED_ACTION, target=f"project:{project_id}")
        .order_by(AuditLog.ts)
        .all()
    )
    return [(r.metadata_json["old"], r.metadata_json["new"], r.metadata_json["cause"]) for r in rows]


class _Executor:
    """What the worker calls. A real executor sets `build.preview_url` inside `execute`
    (`SandboxBuildExecutor._start_preview`) and returns ok; this does the same."""

    def __init__(self, *, ok=True, preview=True):
        self.ok = ok
        self.preview = preview

    def execute(self, build):
        if not self.ok:
            return ExecutionResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary="failed")
        build.image_ref = "forge-build-img"
        build.diff_ref = f"forge-build-{build.id}#1"
        if self.preview:
            build.preview_url = f"http://preview.test/{build.id}"
        return ExecutionResult(ok=True, agent_summary="done")


def _run_next_build(db, *, ok=True, preview=True) -> Build:
    build = claim_next_build(db, concurrency_cap=5)
    assert build is not None
    return run_one_build(db, build, _Executor(ok=ok, preview=preview))


def _route(db, build, tier) -> ApprovalRequest:
    verdict = PolicyVerdict(build_id=build.id, risk_tier=tier)
    db.add(verdict)
    db.flush()
    request = create_approval_request_for_verdict(
        db,
        build=build,
        verdict=verdict,
        project_id=build.project_id,
        requester_id=db.get(Project, build.project_id).owner_id,
    )
    db.commit()
    return request


def _approve(db, request) -> None:
    request.status = ApprovalStatus.APPROVED
    db.commit()


def _deploy(db, project, build) -> Deployment:
    deployment = confirm_deploy(
        db, project=project, build=build, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db.commit()
    execute_deployment(db, deployment, manager=FakeDeployManager(), git_provider=FakeMergeProvider())
    db.refresh(deployment)
    return deployment


def _deployable_build(db, project_id, *, build_id) -> Build:
    """A SUCCEEDED, auto-approved build with an image and a PR ref, as a confirm needs."""
    build = Build(
        id=build_id,
        project_id=project_id,
        type="create",
        status=BuildStatus.SUCCEEDED,
        image_ref="forge-build-img",
        diff_ref=f"{build_id}#1",
    )
    db.add(build)
    db.flush()
    db.add(
        ApprovalRequest(
            build_id=build.id,
            project_id=project_id,
            requester_id=db.get(Project, project_id).owner_id,
            risk_tier="low",
            status=ApprovalStatus.AUTO_APPROVED,
            required_approvals=1,
        )
    )
    db.commit()
    return build


def _project_at(db, seeded_ids, status, name="direct-project") -> Project:
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=status,
    )
    db.add(project)
    db.commit()
    return project


# ---------------------------------------------------------------------------
# advance(): the rules, in isolation
# ---------------------------------------------------------------------------


def test_the_forward_order_is_the_specs_table():
    assert list(FORWARD_ORDER) == ORDER


@pytest.mark.parametrize("current", ORDER)
@pytest.mark.parametrize("target", ORDER)
def test_advance_applies_exactly_the_strictly_forward_moves(db_session, seeded_ids, current, target):
    project = _project_at(db_session, seeded_ids, current)

    moved = advance(db_session, project, target, cause="build:x")
    db_session.commit()

    forward = ORDER.index(target) > ORDER.index(current)
    assert moved is forward
    assert project.status == (target if forward else current)
    assert len(_transitions(db_session, project.id)) == (1 if forward else 0)


def test_advance_never_touches_an_archived_project(db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.ARCHIVED)

    for target in ORDER:
        assert advance(db_session, project, target, cause="build:x") is False
    db_session.commit()

    assert project.status == ProjectStatus.ARCHIVED
    assert _transitions(db_session, project.id) == []


def test_advance_refuses_archived_as_a_target(db_session, seeded_ids):
    """Archiving is not a forward transition: it has its own writer."""
    project = _project_at(db_session, seeded_ids, ProjectStatus.ACTIVE)

    with pytest.raises(ValueError):
        advance(db_session, project, ProjectStatus.ARCHIVED, cause="build:x")

    assert project.status == ProjectStatus.ACTIVE


def test_advance_audits_old_new_cause_and_actor(db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.SUBMITTED)

    advance(db_session, project, ProjectStatus.BUILD_PENDING, cause="build:b-1", actor_id=seeded_ids["user_id"])
    db_session.commit()

    entry = db_session.query(AuditLog).filter_by(action="project.status_changed").one()
    assert entry.target == f"project:{project.id}"
    assert entry.actor_id == seeded_ids["user_id"]
    assert entry.metadata_json == {"old": "SUBMITTED", "new": "BUILD_PENDING", "cause": "build:b-1"}


def test_the_audit_entry_is_on_the_project_timeline(client, db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.SUBMITTED)
    advance(db_session, project, ProjectStatus.BUILD_PENDING, cause="build:b-1")
    db_session.commit()

    timeline = client.get(f"/api/projects/{project.id}/timeline", headers=_headers(seeded_ids)).json()

    assert "project.status_changed" in [e["data"].get("action") for e in timeline if e["kind"] == "audit_log"]


# ---------------------------------------------------------------------------
# The four transitions, each through the code path that fires it
# ---------------------------------------------------------------------------


def test_a_queued_build_moves_the_project_to_build_pending(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)

    assert _status(db_session, project_id) == "BUILD_PENDING"
    build = db_session.query(Build).filter_by(project_id=project_id).one()
    assert _transitions(db_session, project_id) == [("SUBMITTED", "BUILD_PENDING", f"build:{build.id}")]


def test_a_stub_build_that_will_never_run_advances_nothing(client, db_session, seeded_ids, monkeypatch):
    monkeypatch.setattr(settings, "use_sandbox_executor", False)

    project_id = _create_project(client, seeded_ids)

    assert db_session.query(Build).filter_by(project_id=project_id).one().status == "NOT_IMPLEMENTED"
    assert _status(db_session, project_id) == "SUBMITTED"
    assert _transitions(db_session, project_id) == []


def test_a_succeeded_build_with_a_preview_moves_the_project_to_preview_ready(
    client, db_session, seeded_ids, executor_active
):
    project_id = _create_project(client, seeded_ids)

    build = _run_next_build(db_session)

    assert build.status == BuildStatus.SUCCEEDED
    assert _status(db_session, project_id) == "PREVIEW_READY"
    assert _transitions(db_session, project_id)[-1] == ("BUILD_PENDING", "PREVIEW_READY", f"build:{build.id}")


def test_the_real_preview_writer_feeds_the_transition(client, db_session, seeded_ids, executor_active):
    """`SandboxBuildExecutor._start_preview` is what really sets `Build.preview_url`; the
    transition must read what it writes, not a name the fake executor above happens to use."""
    project_id = _create_project(client, seeded_ids)
    build = claim_next_build(db_session, concurrency_cap=5)
    preview_manager = MagicMock()
    preview_manager.start_preview.return_value = SimpleNamespace(
        url="http://preview.test/real",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_hash="h",
    )
    executor = SandboxBuildExecutor(docker_client=MagicMock(), adapter=MagicMock(), preview_manager=preview_manager)

    executor._start_preview(build)
    mark_succeeded(db_session, build)

    assert _status(db_session, project_id) == "PREVIEW_READY"


def test_a_succeeded_build_with_no_preview_changes_nothing(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)

    _run_next_build(db_session, preview=False)

    assert _status(db_session, project_id) == "BUILD_PENDING"


def test_a_failed_build_changes_nothing(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)

    build = _run_next_build(db_session, ok=False)

    assert build.status == BuildStatus.FAILED
    assert _status(db_session, project_id) == "BUILD_PENDING"
    assert len(_transitions(db_session, project_id)) == 1  # only the build-created one


@pytest.mark.parametrize("tier", [PolicyRiskTier.MEDIUM, PolicyRiskTier.HIGH])
def test_routing_that_leaves_an_approval_pending_moves_the_project_to_awaiting_approval(
    client, db_session, seeded_ids, executor_active, tier
):
    project_id = _create_project(client, seeded_ids)
    build = _run_next_build(db_session)
    assert _status(db_session, project_id) == "PREVIEW_READY"

    request = _route(db_session, build, tier)

    assert request.status == ApprovalStatus.PENDING
    assert _status(db_session, project_id) == "AWAITING_APPROVAL"
    assert _transitions(db_session, project_id)[-1] == ("PREVIEW_READY", "AWAITING_APPROVAL", f"build:{build.id}")


def test_an_auto_approved_low_risk_build_skips_awaiting_approval(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)
    build = _run_next_build(db_session)

    request = _route(db_session, build, PolicyRiskTier.LOW)

    assert request.status == ApprovalStatus.AUTO_APPROVED
    assert _status(db_session, project_id) == "PREVIEW_READY"
    assert [new for _old, new, _cause in _transitions(db_session, project_id)] == ["BUILD_PENDING", "PREVIEW_READY"]


def test_an_activated_deployment_moves_the_project_to_active(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)
    build = _run_next_build(db_session)
    _approve(db_session, _route(db_session, build, PolicyRiskTier.MEDIUM))
    project = db_session.get(Project, project_id)

    deployment = _deploy(db_session, project, build)

    assert deployment.status == DeploymentStatus.ACTIVE
    assert _status(db_session, project_id) == "ACTIVE"
    assert [new for _old, new, _cause in _transitions(db_session, project_id)] == [
        "BUILD_PENDING",
        "PREVIEW_READY",
        "AWAITING_APPROVAL",
        "ACTIVE",
    ]
    assert _transitions(db_session, project_id)[-1] == ("AWAITING_APPROVAL", "ACTIVE", f"deployment:{deployment.id}")


def test_a_failed_deployment_does_not_make_the_project_active(db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.AWAITING_APPROVAL)
    build = _deployable_build(db_session, project.id, build_id="bld-fail")
    deployment = confirm_deploy(
        db_session, project=project, build=build, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()

    execute_deployment(
        db_session, deployment, manager=FakeDeployManager(healthy=False), git_provider=FakeMergeProvider()
    )

    assert deployment.status == DeploymentStatus.FAILED
    assert _status(db_session, project.id) == "AWAITING_APPROVAL"


# ---------------------------------------------------------------------------
# Forward only
# ---------------------------------------------------------------------------


def test_a_second_build_on_a_preview_ready_project_does_not_regress_it(
    client, db_session, seeded_ids, executor_active
):
    project_id = _create_project(client, seeded_ids)
    _run_next_build(db_session)
    assert _status(db_session, project_id) == "PREVIEW_READY"
    # a modify request needs a confirmed ref; this test is about the status, so set one.
    db_session.get(Project, project_id).confirmed_ref = "c0ffee0"
    db_session.commit()

    _submit_change_request(client, seeded_ids, project_id)

    assert _status(db_session, project_id) == "PREVIEW_READY"


def test_a_second_build_on_an_active_project_does_not_regress_it(client, db_session, seeded_ids, executor_active):
    project_id = _create_project(client, seeded_ids)
    build = _run_next_build(db_session)
    _approve(db_session, _route(db_session, build, PolicyRiskTier.LOW))
    _deploy(db_session, db_session.get(Project, project_id), build)
    assert _status(db_session, project_id) == "ACTIVE"

    _submit_change_request(client, seeded_ids, project_id)
    second = _run_next_build(db_session)  # its preview succeeds ...
    _route(db_session, second, PolicyRiskTier.HIGH)  # ... and it routes to a human

    assert second.status == BuildStatus.SUCCEEDED
    assert _status(db_session, project_id) == "ACTIVE"


def test_a_late_preview_does_not_pull_an_awaiting_approval_project_back(
    client, db_session, seeded_ids, executor_active
):
    """The executor routes the approval inside `execute`, before the worker marks the build
    succeeded -- so for a medium/high build AWAITING_APPROVAL comes first, and the
    PREVIEW_READY the same build then earns must be a no-op, not a regression."""
    project_id = _create_project(client, seeded_ids)
    build = claim_next_build(db_session, concurrency_cap=5)
    _route(db_session, build, PolicyRiskTier.MEDIUM)
    assert _status(db_session, project_id) == "AWAITING_APPROVAL"
    build.preview_url = "http://preview.test/late"

    mark_succeeded(db_session, build)

    assert _status(db_session, project_id) == "AWAITING_APPROVAL"


def test_rollback_and_stop_leave_the_project_active(db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.SUBMITTED)
    first = _deployable_build(db_session, project.id, build_id="bld-1")
    manager = FakeDeployManager()
    _deploy(db_session, project, first)
    second = _deployable_build(db_session, project.id, build_id="bld-2")
    _deploy(db_session, project, second)  # supersedes the first
    assert _status(db_session, project.id) == "ACTIVE"

    rollback = request_rollback(
        db_session, project=project, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()
    execute_deployment(db_session, rollback, manager=manager, git_provider=FakeMergeProvider())
    assert rollback.status == DeploymentStatus.ACTIVE
    assert _status(db_session, project.id) == "ACTIVE"

    stop_active_deployment(db_session, project=project, actor_id=project.owner_id, manager=manager)
    db_session.commit()

    assert db_session.query(Deployment).filter_by(project_id=project.id, status=DeploymentStatus.ACTIVE).count() == 0
    assert _status(db_session, project.id) == "ACTIVE"
    # one transition into ACTIVE, ever: the superseding deploy and the rollback were no-ops
    assert [new for _old, new, _cause in _transitions(db_session, project.id)].count("ACTIVE") == 1


def test_an_archived_project_stays_archived_through_new_builds_previews_routing_and_deploys(
    client, db_session, seeded_ids, executor_active
):
    project_id = _create_project(client, seeded_ids)
    project = db_session.get(Project, project_id)
    project.status = ProjectStatus.ARCHIVED  # test setup: archived by whatever means
    project.confirmed_ref = "c0ffee0"  # a modify request needs one
    db_session.commit()

    _submit_change_request(client, seeded_ids, project_id)
    assert _status(db_session, project_id) == "ARCHIVED"  # a queued build

    build = _run_next_build(db_session)
    assert build.preview_url is not None
    assert _status(db_session, project_id) == "ARCHIVED"  # a succeeded build with a preview

    _route(db_session, build, PolicyRiskTier.MEDIUM)
    assert _status(db_session, project_id) == "ARCHIVED"  # an approval left pending

    deployable = _deployable_build(db_session, project_id, build_id="bld-arch")
    # An archived project refuses the confirm itself, so
    # no new deployment can be started on it ...
    with pytest.raises(DeployProjectArchivedError):
        confirm_deploy(
            db_session,
            project=db_session.get(Project, project_id),
            build=deployable,
            actor_id=db_session.get(Project, project_id).owner_id,
            image_exists=lambda ref: True,
        )
    db_session.rollback()
    assert _status(db_session, project_id) == "ARCHIVED"
    assert db_session.query(Deployment).filter_by(project_id=project_id).count() == 0

    # ... but a deployment confirmed BEFORE the archive and activated after it (the worker runs it
    # whenever it gets to it) still leaves the project ARCHIVED: only un-archive moves it. The status
    # is set directly here, as the test setup above does, so no transition entry is written.
    project = db_session.get(Project, project_id)
    project.status = ProjectStatus.PREVIEW_READY
    db_session.commit()
    deployment = confirm_deploy(
        db_session, project=project, build=deployable, actor_id=project.owner_id, image_exists=lambda ref: True
    )
    db_session.commit()
    project.status = ProjectStatus.ARCHIVED
    db_session.commit()
    execute_deployment(db_session, deployment, manager=FakeDeployManager(), git_provider=FakeMergeProvider())
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert _status(db_session, project_id) == "ARCHIVED"  # a deployment activated

    # the queued build on a project that was still SUBMITTED moved it once; nothing after the archive did
    assert [(old, new) for old, new, _cause in _transitions(db_session, project_id)] == [
        ("SUBMITTED", "BUILD_PENDING")
    ]


# ---------------------------------------------------------------------------
# End to end: archive, un-archive, registry
# ---------------------------------------------------------------------------


def test_archive_then_unarchive_of_a_deployed_project_restores_active(
    client, db_session, seeded_ids, executor_active
):
    """Through the real transitions, not a seeded status, and through the real archive writer."""
    project_id = _create_project(client, seeded_ids)
    build = _run_next_build(db_session)
    _approve(db_session, _route(db_session, build, PolicyRiskTier.MEDIUM))
    _deploy(db_session, db_session.get(Project, project_id), build)
    assert _status(db_session, project_id) == "ACTIVE"

    project_lifecycle.process_idle_candidate(
        db_session, db_session.get(Project, project_id), deploy_manager=FakeDeployManager()
    )
    db_session.commit()
    assert _status(db_session, project_id) == "ARCHIVED"

    resp = client.post(f"/api/projects/{project_id}/unarchive", headers=_headers(seeded_ids))

    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"
    assert _status(db_session, project_id) == "ACTIVE"


def test_the_registry_status_filter_finds_a_deployed_project(client, db_session, seeded_ids, executor_active):
    deployed_id = _create_project(client, seeded_ids, name="deployed")
    build = _run_next_build(db_session)
    _approve(db_session, _route(db_session, build, PolicyRiskTier.LOW))
    _deploy(db_session, db_session.get(Project, deployed_id), build)
    other_id = _create_project(client, seeded_ids, name="not-deployed")

    found = client.get("/api/projects", params={"status": "ACTIVE"}, headers=_headers(seeded_ids)).json()

    assert [p["id"] for p in found] == [deployed_id]
    assert other_id not in [p["id"] for p in found]


# ---------------------------------------------------------------------------
# The zombie path does not read Project.status
# ---------------------------------------------------------------------------


def test_a_deployed_project_with_a_legacy_submitted_status_is_still_deployed_then_idle(db_session, seeded_ids):
    """A legacy row: deployed, but its status never moved. The population (what
    cleanup may do to it) is decided by the Deployment row, never by the status column."""
    project = _project_at(db_session, seeded_ids, ProjectStatus.SUBMITTED, name="legacy")
    build = _deployable_build(db_session, project.id, build_id="bld-legacy")
    db_session.add(
        Deployment(
            project_id=project.id,
            build_id=build.id,
            confirmed_by_id=project.owner_id,
            status=DeploymentStatus.STOPPED,
        )
    )
    db_session.commit()

    assert project_lifecycle.population_of(db_session, project.id) == "deployed_then_idle"


def test_a_never_deployed_project_that_claims_to_be_active_is_still_never_deployed(db_session, seeded_ids):
    project = _project_at(db_session, seeded_ids, ProjectStatus.ACTIVE, name="claims-active")

    assert project_lifecycle.population_of(db_session, project.id) == "never_deployed"


def _project_status_reads(tree: ast.AST) -> list[tuple[str, str]]:
    """(enclosing function, source) of every `Project.status` / `project.status` read and every
    `ProjectStatus` mention in a module."""
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "status"
            and isinstance(node.value, ast.Name)
            and node.value.id in ("Project", "project")
        ):
            found.append((function, ast.unparse(node)))
        if isinstance(node, ast.Name) and node.id == "ProjectStatus":
            found.append((function, "ProjectStatus"))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_project_lifecycle_reads_the_status_column_only_to_exclude_archived_and_to_archive():
    """The zombie path chooses a population (`population_of`) and so what may be destroyed; that
    must never involve `Project.status`. The module reads it in exactly two places: the candidate
    query's `!= ARCHIVED` and `_archive`'s record of what it overwrites."""
    tree = ast.parse((APP_DIR / "services" / "project_lifecycle.py").read_text(encoding="utf-8"))

    reads = _project_status_reads(tree)

    assert {function for function, _src in reads} == {"idle_candidates", "_archive"}, reads
    # And the named function itself, flatly: no status in it at all.
    population_of = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "population_of")
    assert not [n for n in ast.walk(population_of) if isinstance(n, ast.Attribute) and n.attr == "status"]
    assert not [n for n in ast.walk(population_of) if isinstance(n, ast.Name) and n.id == "ProjectStatus"]


# ---------------------------------------------------------------------------
# One writer
# ---------------------------------------------------------------------------

# (file relative to app/, enclosing function). `demo_data.py` is seed data: it builds demo
# `Project` rows with a hand-chosen `status=` and is the one file allowed to, and only that way
# (a constructor keyword, never an assignment) -- see `test_demo_data.py`, which pins that each
# demo project's status matches the pipeline position its own rows show.
ALLOWED_PROJECT_STATUS_ASSIGNMENTS = {
    ("services/project_status.py", "advance"),  # forward transitions: the one writer
    ("services/project_lifecycle.py", "_archive"),  # archive
    ("routers/projects.py", "unarchive_project"),  # un-archive
}
ALLOWED_PROJECT_STATUS_CONSTRUCTIONS = {
    ("routers/projects.py", "create_project"),  # creation
    ("demo_data.py", "<seed>"),  # seed data, any function in the file
}


def _root_name(node: ast.AST) -> str | None:
    """`DeploymentStatus` for `DeploymentStatus.ACTIVE`; None for anything not a dotted name."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


# Status classes of the OTHER entities. An assignment `<anything>.status = <value>` is cleared as
# "not a project's" only if its value is a constant of one of these; every other `.status =` is
# treated as possibly a project's, whatever the receiver is called and however the value is spelled
# (a string literal, a variable, a ProjectStatus constant).
OTHER_STATUS_CLASSES = {"BuildStatus", "DeploymentStatus", "ApprovalStatus", "AccountStatus", "EvalRunStatus"}

# ...but the status values are plain strings and two of those classes share a member with
# `ProjectStatus` (`DeploymentStatus.ACTIVE` and `AccountStatus.ACTIVE` are both "ACTIVE", a legal
# project status), so a class name alone clears nothing that could be a project's: a constant whose
# VALUE is a `ProjectStatus` value is cleared only at the (file, function) sites listed below,
# which really do write a deployment's or an account's status.
_OTHER_CLASSES = {
    "BuildStatus": BuildStatus,
    "DeploymentStatus": DeploymentStatus,
    "ApprovalStatus": ApprovalStatus,
    "AccountStatus": AccountStatus,
    "EvalRunStatus": EvalRunStatus,  # an eval run's status, none of its values is a project status
}
_PROJECT_STATUS_VALUES = {v for k, v in vars(ProjectStatus).items() if k.isupper()}
ALLOWED_OVERLAPPING_ASSIGNMENTS = {
    ("services/deploy.py", "_activate"),  # deployment.status = DeploymentStatus.ACTIVE
    ("services/signup.py", "approve_signup"),  # user.status = AccountStatus.ACTIVE
}

_PROJECT_NAME = re.compile(r"\bProject\b")
_RAW_PROJECT_WRITE = re.compile(r"\b(UPDATE\s+projects|INSERT\s+INTO\s+projects)\b", re.IGNORECASE)


def _clears_as_another_entitys(target: ast.Attribute, value: ast.AST | None, rel: str, function: str) -> bool:
    """True iff `<x>.status = value` can be shown not to be a project's. Two signals, each covering
    the other's blind spot: a receiver whose text mentions `project` is never cleared; and a value
    is cleared only if it is a constant of another entity's status class whose string is NOT also a
    `ProjectStatus` value -- or, if it is (`DeploymentStatus.ACTIVE`), only at a listed site."""
    if "project" in ast.unparse(target.value).lower():
        return False
    if not isinstance(value, ast.Attribute) or not isinstance(value.value, ast.Name):
        return False
    cls = _OTHER_CLASSES.get(value.value.id)
    if cls is None:
        return False
    member = getattr(cls, value.attr, None)
    if member is None:
        return False
    if member in _PROJECT_STATUS_VALUES:
        return (rel, function) in ALLOWED_OVERLAPPING_ASSIGNMENTS
    return True


def _scan_status_writes(tree: ast.AST, rel: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Every place in one module that could write `Project.status`, found without relying on what a
    variable is called. Returns (writes, constructions) as (file, enclosing function).

    writes: any `<x>.status = v` (also augmented/annotated) unless `v` is a constant of another
    entity's status class; `setattr(x, k, v)` with `k` the literal "status" or not a literal at
    all; a call ending in `update`/`insert`/`values` whose whole chain (receiver included) names
    `Project` -- `update(Project)`, `sa.update(Project)`, `update(Project.__table__)`,
    `update(models.Project)`, `db.query(Project).filter(..).update({..})`; and any string that is
    raw SQL `UPDATE projects` / `INSERT INTO projects`.
    constructions: `Project(..., status=...)` and `Project(**fields)`."""
    writes: set[tuple[str, str]] = set()
    constructed: set[tuple[str, str]] = set()

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        targets = []
        if isinstance(node, ast.Assign):
            targets = [(t, node.value) for t in node.targets]
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets = [(node.target, node.value)]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [(node.target, None)]
        for target, value in targets:
            # A tuple/list/starred target (`a.status, b = ...`, `for p.status in ...`) has no single
            # value to look at: unpack it and treat each `.status` inside as written with an unknown one.
            leaves = [(target, value)]
            if isinstance(target, (ast.Tuple, ast.List, ast.Starred)):
                leaves = [(leaf, None) for leaf in ast.walk(target) if isinstance(leaf, ast.Attribute)]
            for leaf, leaf_value in leaves:
                if isinstance(leaf, ast.Attribute) and leaf.attr == "status":
                    if not _clears_as_another_entitys(leaf, leaf_value, rel, function):
                        writes.add((rel, function))
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if callee == "Project" and any(k.arg in ("status", None) for k in node.keywords):
                constructed.add((rel, "<seed>" if rel == "demo_data.py" else function))
            if callee in ("setattr", "__setattr__") and len(node.args) >= (2 if callee == "setattr" else 1):
                key = node.args[1] if callee == "setattr" else node.args[0]
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)) or key.value == "status":
                    writes.add((rel, function))
            if callee in ("update", "insert", "values") and _PROJECT_NAME.search(ast.unparse(node)):
                writes.add((rel, function))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _RAW_PROJECT_WRITE.search(node.value):
            writes.add((rel, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return writes, constructed


def _status_writes() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    assigned: set[tuple[str, str]] = set()
    constructed: set[tuple[str, str]] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        a, c = _scan_status_writes(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(APP_DIR).as_posix())
        assigned |= a
        constructed |= c
    return assigned, constructed


def test_nothing_but_the_named_writers_assigns_project_status():
    assigned, constructed = _status_writes()

    assert assigned == ALLOWED_PROJECT_STATUS_ASSIGNMENTS
    assert constructed == ALLOWED_PROJECT_STATUS_CONSTRUCTIONS


@pytest.mark.parametrize(
    "line",
    [
        # assignments: receiver name and value spelling are irrelevant
        "project.status = 'ACTIVE'",
        "self.owner_project.status = x",
        "p.status = ProjectStatus.ACTIVE",
        "p.status = 'ACTIVE'",
        "row.status = 'ACTIVE'",
        "row.status = new_status",
        "row.status = lookup['x']",
        "p.status: str = 'ACTIVE'",
        "p.status += 'x'",
        "p.status, other = 'ACTIVE', 1",
        "for p.status in values: pass",
        # another entity's constant whose string is also a legal project status
        "project.status = DeploymentStatus.ACTIVE",
        "project.status = AccountStatus.ACTIVE",
        "row.status = DeploymentStatus.ACTIVE",
        "row.status = AccountStatus.ACTIVE",
        "p.__setattr__('status', v)",
        "p.__setattr__(name, v)",
        # setattr: literal key, and a key that is not a literal
        "setattr(p, 'status', 'ACTIVE')",
        "setattr(p, name, v)",
        # bulk SQL, however the table is reached
        "db.execute(update(Project).values(status='ACTIVE'))",
        "db.execute(sa.update(Project).values(status='ACTIVE'))",
        "db.execute(update(Project.__table__).values(status='ACTIVE'))",
        "db.execute(update(models.Project).values(status='ACTIVE'))",
        "db.execute(insert(Project).values(status='ACTIVE'))",
        "db.query(Project).filter(Project.id == pid).update({'status': 'ACTIVE'})",
        "db.query(Project).update({Project.status: 'ACTIVE'})",
        "db.execute(text('UPDATE projects SET status = :s'))",
        "db.execute(text('update   projects set status = :s'))",
        "db.execute(text('INSERT INTO projects (id, status) VALUES (1, 2)'))",
        # construction
        "Project(name='x', status='ACTIVE')",
        "Project(**fields)",
    ],
)
def test_the_writer_scan_sees_each_shape_it_claims_to_catch(line):
    """The scan must be able to reach what it names: run the real scanner over each shape."""
    tree = ast.parse("def sneaky(project, p, db):\n    " + line + "\n")

    assigned, constructed = _scan_status_writes(tree, "stray.py")

    assert assigned | constructed == {("stray.py", "sneaky")}


@pytest.mark.parametrize(
    "line",
    [
        "build.status = BuildStatus.RUNNING",
        "x.status = DeploymentStatus.SUPERSEDED",
        "x.status = DeploymentStatus.STOPPED",
        "x.status = AccountStatus.PENDING",
        "user.status = AccountStatus.PENDING",
        "request.status = ApprovalStatus.APPROVED",
        "db.query(Deployment).filter(Deployment.id.in_(ids)).update({'status': 'X'})",
        "db.execute(update(Build).values(status='X'))",
        "db.execute(text('UPDATE builds SET status = :s'))",
        "db.execute(text('SELECT * FROM projects'))",
        "setattr(obj, 'name', v)",
        "Project(name='x')",
    ],
)
def test_the_writer_scan_does_not_flag_other_writes(line):
    tree = ast.parse("def fine(build, deployment, db):\n    " + line + "\n")

    assert _scan_status_writes(tree, "fine.py") == (set(), set())


@pytest.mark.parametrize(
    ("rel", "function", "line"),
    [
        ("services/deploy.py", "_activate", "deployment.status = DeploymentStatus.ACTIVE"),
        ("services/signup.py", "approve_signup", "target.status = AccountStatus.ACTIVE"),
    ],
)
def test_the_writer_scan_clears_the_listed_overlapping_sites(rel, function, line):
    """`ACTIVE` is also a project status, so these two real writes are cleared by (file, function),
    not by the class name: the same line anywhere else is flagged (see the shapes test)."""
    tree = ast.parse(f"def {function}(deployment, target):\n    {line}\n")

    assert _scan_status_writes(tree, rel) == (set(), set())


def test_a_project_write_inside_a_listed_overlapping_site_is_still_flagged():
    """Listing `_activate` must not hide a project write placed next to the deployment's own."""
    tree = ast.parse(
        "def _activate(deployment, project):\n"
        "    deployment.status = DeploymentStatus.ACTIVE\n"
        "    project.status = DeploymentStatus.ACTIVE\n"
    )

    assert _scan_status_writes(tree, "services/deploy.py") == ({("services/deploy.py", "_activate")}, set())
