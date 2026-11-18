"""ADR 0038 §Decision 6: `deploy._activate` is a compare-and-set.

The new row moves to ACTIVE only if the DATABASE still has it DEPLOYING. When another process has
resolved it in the meantime (a second worker's start-up recovery, the stale sweep) the activation is
refused: the row is left exactly as that process left it, the old ACTIVE row keeps serving (the
supersede is rolled back), and the container this worker started for it is removed.

In-memory SQLite, no Docker (`FakeDeployManager`). The other process is simulated by a committed
UPDATE; the object this worker holds is then put back to the status it believed (`set_committed_value`),
because the point is a stale in-memory view.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from app.models.audit import AuditLog
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.project import Project
from app.services import deploy as deploy_module
from app.services.deploy import execute_deployment
from app.services.status import ProjectStatus
from tests.test_deploy_service import FakeDeployManager, FakeMergeProvider, _confirm, _make_build
from tests.test_worker_recovery import _data_services_are_fakes  # noqa: F401 - autouse: no real data services
from tests.test_worker_recovery import project  # noqa: F401 - the project fixture


class _OtherWorkerFailsTheRow(FakeDeployManager):
    """While this worker's container passes its health gate, another process fails the row (a
    second worker's start-up recovery would): written and committed by it, and the object this
    worker holds keeps believing the row is still DEPLOYING."""

    def __init__(self, db):
        super().__init__()
        self._db = db

    def connect_to_traefik(self, deployment_id):
        super().connect_to_traefik(deployment_id)
        db = self._db
        db.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id)
            .values(status=DeploymentStatus.FAILED, failure_reason=DeploymentFailureReason.INFRA_ERROR),
            execution_options={"synchronize_session": False},
        )
        db.commit()
        row = db.get(Deployment, deployment_id)
        set_committed_value(row, "status", DeploymentStatus.DEPLOYING)  # stale, as in the other process's view


def _audit_actions(db):
    return [a.action for a in db.query(AuditLog)]


def test_activating_a_row_that_is_no_longer_deploying_leaves_it_alone_and_removes_the_container(
    db_session, project
):
    first_build = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, first_build)
    execute_deployment(db_session, first, manager=FakeDeployManager(), git_provider=FakeMergeProvider())
    assert first.status == DeploymentStatus.ACTIVE
    second_build = _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2")
    second = _confirm(db_session, project, second_build)
    second_id, first_id = second.id, first.id
    manager = _OtherWorkerFailsTheRow(db_session)
    actions_before = _audit_actions(db_session)

    result = execute_deployment(db_session, second, manager=manager, git_provider=FakeMergeProvider())

    db_session.expire_all()
    row = db_session.get(Deployment, second_id)
    assert result.id == second_id
    assert row.status == DeploymentStatus.FAILED  # what the other process wrote, untouched
    assert row.failure_reason == DeploymentFailureReason.INFRA_ERROR
    assert manager.started and manager.started[-1]["deployment_id"] == second_id
    assert manager.removed == [second_id]  # the container this worker started is gone
    old = db_session.get(Deployment, first_id)
    assert old.status == DeploymentStatus.ACTIVE  # still serving: the supersede was rolled back
    assert old.ended_at is None
    new_actions = Counter(_audit_actions(db_session)) - Counter(actions_before)
    assert "deployment.activated" not in new_actions
    assert "deployment.superseded" not in new_actions
    assert "deployment.failed" not in new_actions  # this worker wrote nothing about the row


def test_activate_returns_false_and_changes_nothing_for_a_row_that_is_not_deploying(db_session, project):
    build = _make_build(db_session, project)
    deployment = _confirm(db_session, project, build)
    db_session.execute(
        update(Deployment).where(Deployment.id == deployment.id).values(status=DeploymentStatus.FAILED),
        execution_options={"synchronize_session": False},
    )
    db_session.commit()
    set_committed_value(deployment, "status", DeploymentStatus.DEPLOYING)

    activated = deploy_module._activate(db_session, deployment, None)

    assert activated is False
    db_session.expire_all()
    assert db_session.get(Deployment, deployment.id).status == DeploymentStatus.FAILED
    assert "deployment.activated" not in _audit_actions(db_session)
    assert db_session.get(Project, project.id).status != ProjectStatus.ACTIVE


def test_activate_still_activates_a_deploying_row_and_supersedes_the_old_one(db_session, project):
    first_build = _make_build(db_session, project, build_id="bld-1", diff_ref="b1#1")
    first = _confirm(db_session, project, first_build)
    execute_deployment(db_session, first, manager=FakeDeployManager(), git_provider=FakeMergeProvider())
    second = _confirm(db_session, project, _make_build(db_session, project, build_id="bld-2", diff_ref="b2#2"))

    activated = deploy_module._activate(db_session, second, first)

    assert activated is True
    db_session.expire_all()
    assert db_session.get(Deployment, second.id).status == DeploymentStatus.ACTIVE
    assert db_session.get(Deployment, first.id).status == DeploymentStatus.SUPERSEDED
    assert Counter(_audit_actions(db_session))["deployment.activated"] == 2  # the first one's, and this one
