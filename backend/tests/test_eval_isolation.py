"""Eval projects never enter human
approval, are not part of the console's deployment health, and have a defined forward status.

Real policy gate over a workspace that plants a secret (a HIGH-tier flag), driven through
`SandboxBuildExecutor._run_policy_gate`, the one place that routes a verdict to approval; the
control is the same build on a normal project, which DOES get a PENDING approval request, so the
eval assertion cannot pass merely because the gate never reached routing. No Docker.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.models.approval import ApprovalRequest
from app.models.eval_run import EvalCase, EvalRun
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.services import sandbox_build_executor as sbe
from app.services.build_queue import mark_succeeded
from app.services.status import ProjectKind, ProjectStatus


def _h(user):
    return {"X-Forge-User-Id": user.id}


@pytest.fixture()
def admin(db_session):
    return db_session.query(User).filter_by(role="admin").first()


@pytest.fixture()
def approver(db_session):
    return db_session.query(User).filter_by(role="approver").first()


@pytest.fixture()
def executor():
    return sbe.SandboxBuildExecutor(docker_client=SimpleNamespace(), adapter=object())


@pytest.fixture()
def secret_workspace(tmp_path):
    path = tmp_path / "ws" / "app"
    os.makedirs(path)
    (path / "config.py").write_text("AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n", encoding="utf-8")
    return str(tmp_path / "ws")


def _project(db, seeded_ids, owner_id, name, kind):
    project = Project(
        name=name,
        owner_id=owner_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        kind=kind,
    )
    db.add(project)
    db.commit()
    return project


def _gated_build(db, make_build_with_request, project, executor, workspace):
    """A build that went QUEUED -> succeeded with a live preview (project PREVIEW_READY), then the gate."""
    build = make_build_with_request(project=project)
    build.status = "RUNNING"
    build.preview_url = f"/api/projects/{project.id}/preview/{build.id}?token=t"
    db.commit()
    mark_succeeded(db, build)
    result = executor._run_policy_gate(build, workspace)
    db.commit()
    assert result is None
    return build


def test_an_eval_build_is_gated_but_creates_no_approval_request_and_a_normal_one_does(
    client, db_session, seeded_ids, admin, approver, executor, secret_workspace, make_build_with_request
):
    eval_project = _project(db_session, seeded_ids, admin.id, "eval-1234abcd-static-page", ProjectKind.EVAL)
    normal_project = _project(db_session, seeded_ids, admin.id, "a-normal-service", ProjectKind.NORMAL)

    eval_build = _gated_build(db_session, make_build_with_request, eval_project, executor, secret_workspace)
    normal_build = _gated_build(db_session, make_build_with_request, normal_project, executor, secret_workspace)

    # Both were gated and flagged HIGH: the verdict exists for the eval build too.
    for build in (eval_build, normal_build):
        verdict = db_session.query(PolicyVerdict).filter_by(build_id=build.id).one()
        assert verdict.risk_tier == "high"
    # The control: the normal build is waiting on a human.
    assert db_session.query(ApprovalRequest).filter_by(build_id=normal_build.id).one().status == "PENDING"
    # The eval build created none, so an approver's queue holds the normal one only.
    assert db_session.query(ApprovalRequest).filter_by(build_id=eval_build.id).count() == 0
    queue = client.get("/api/approvals", headers=_h(approver)).json()
    assert [row["build_id"] for row in queue] == [normal_build.id]


def test_an_eval_projects_status_ends_at_preview_ready_and_never_awaits_approval(
    db_session, seeded_ids, admin, executor, secret_workspace, make_build_with_request
):
    """Forward transitions apply to an eval project up to PREVIEW_READY: queued -> BUILD_PENDING,
    a build that succeeded with a preview -> PREVIEW_READY. With no approval request it never reaches
    AWAITING_APPROVAL (and, with deploy refused, never ACTIVE); the normal control does reach it."""
    eval_project = _project(db_session, seeded_ids, admin.id, "eval-1234abcd-database-page", ProjectKind.EVAL)
    normal_project = _project(db_session, seeded_ids, admin.id, "another-normal", ProjectKind.NORMAL)

    _gated_build(db_session, make_build_with_request, eval_project, executor, secret_workspace)
    _gated_build(db_session, make_build_with_request, normal_project, executor, secret_workspace)

    db_session.refresh(eval_project)
    db_session.refresh(normal_project)
    assert eval_project.status == ProjectStatus.PREVIEW_READY
    assert normal_project.status == ProjectStatus.AWAITING_APPROVAL


def test_the_case_shows_the_builds_policy_verdict(
    client, db_session, seeded_ids, admin, executor, secret_workspace, make_build_with_request
):
    eval_project = _project(db_session, seeded_ids, admin.id, "eval-1234abcd-static-page", ProjectKind.EVAL)
    build = _gated_build(db_session, make_build_with_request, eval_project, executor, secret_workspace)
    run = EvalRun(
        status="COMPLETED", engine="openai", model=None, request_set_version=1, budget_usd=1.0, started_by_id=admin.id
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        EvalCase(run_id=run.id, case_id="static-page", position=0, state="DONE", result="PASSED",
                 project_id=eval_project.id, build_id=build.id)
    )
    db_session.add(EvalCase(run_id=run.id, case_id="database-page", position=1, state="PENDING"))
    db_session.commit()

    cases = client.get(f"/api/evals/runs/{run.id}", headers=_h(admin)).json()["cases"]

    assert cases[0]["policy_risk_tier"] == "high"
    assert "secrets_pii" in cases[0]["policy_flagged_checks"]  # (the tiny workspace trips other checks too)
    assert (cases[1]["policy_risk_tier"], cases[1]["policy_flagged_checks"]) == (None, [])  # not run: no verdict


def test_the_console_deployment_health_view_excludes_eval_projects(client, db_session, seeded_ids, admin):
    normal = _project(db_session, seeded_ids, admin.id, "a-normal-service", ProjectKind.NORMAL)
    _project(db_session, seeded_ids, admin.id, "eval-1234abcd-static-page", ProjectKind.EVAL)

    body = client.get("/api/console/deployments", headers=_h(admin)).json()

    assert [p["project_id"] for p in body["projects"]] == [normal.id]
