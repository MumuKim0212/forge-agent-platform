import pytest

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.services.status import BuildStatus, ProjectStatus


def test_create_project_creates_change_request_and_not_implemented_build(client, seeded_ids):
    resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "휴가 신청 트래커",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "팀원들이 휴가를 신청하고 승인 현황을 볼 수 있는 도구를 만들어줘",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "SUBMITTED"
    assert body["owner_name"]
    assert body["template_name"]

    timeline = client.get(
        f"/api/projects/{body['id']}/timeline",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
    ).json()
    kinds = {entry["kind"] for entry in timeline}
    assert kinds == {"change_request", "build", "audit_log"}

    build_entries = [e for e in timeline if e["kind"] == "build"]
    assert len(build_entries) == 1
    assert build_entries[0]["data"]["status"] == "NOT_IMPLEMENTED"


def test_create_project_requires_identity_header(client, seeded_ids):
    resp = client.post(
        "/api/projects",
        json={
            "name": "No identity",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "test",
        },
    )
    assert resp.status_code == 401


def test_project_registry_lists_created_projects(client, seeded_ids):
    create_resp = client.post(
        "/api/projects",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
        json={
            "name": "Registry Test Project",
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "test request",
        },
    )
    project_id = create_resp.json()["id"]

    registry = client.get(
        "/api/projects", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    ).json()
    assert any(p["id"] == project_id for p in registry)


# -- unarchive (ARCHIVED is reversible in code, not only spec) --


def _make_project(db, seeded_ids, *, owner_id=None, status=ProjectStatus.SUBMITTED, name="unarchive-project"):
    project = Project(
        name=name,
        owner_id=owner_id or seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=status,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_unarchive_restores_the_status_recorded_by_the_archive_audit_entry(
    client, db_session, seeded_ids
):
    """(d1): the pre-archive status is recovered from the archive audit
    entry's own `previous_status` metadata, not a column. Pinned to a
    value other than `ACTIVE` (the fallback default below) so this test
    cannot pass merely because the fallback happens to agree."""
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ARCHIVED)
    db_session.add(
        AuditLog(
            actor_id=None,
            action="project.archived",
            target=f"project:{project.id}",
            metadata_json={
                "previous_status": ProjectStatus.SUBMITTED,
                "population": "never_deployed",
            },
        )
    )
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project.id}/unarchive",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == ProjectStatus.SUBMITTED

    db_session.refresh(project)
    assert project.status == ProjectStatus.SUBMITTED

    audit = (
        db_session.query(AuditLog)
        .filter_by(action="project.unarchived", target=f"project:{project.id}")
        .one()
    )
    assert audit.actor_id == seeded_ids["user_id"]
    assert audit.metadata_json == {"restored_status": ProjectStatus.SUBMITTED}


def test_unarchive_falls_back_to_active_with_no_archive_audit_entry(
    client, db_session, seeded_ids
):
    """Defensive fallback for an `ARCHIVED` row this task's own automatic
    path did not produce (see `routers/projects.py::_status_before_
    archival`'s own docstring) -- not expected in practice, but must still
    resolve to something rather than 500."""
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ARCHIVED)

    resp = client.post(
        f"/api/projects/{project.id}/unarchive",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == ProjectStatus.ACTIVE


def test_unarchive_is_refused_to_neither_owner_nor_admin(client, db_session, seeded_ids):
    """The refused direction. `approver` does not qualify:
    an approver does not override ownership."""
    approver = db_session.query(User).filter_by(role="approver").first()
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ARCHIVED)

    resp = client.post(
        f"/api/projects/{project.id}/unarchive",
        headers={"X-Forge-User-Id": approver.id},
    )
    assert resp.status_code == 403

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


def test_unarchive_is_permitted_for_admin_even_when_not_the_owner(client, db_session, seeded_ids):
    """(d2): the permitted direction via the role half, not the ownership
    half — an admin who does not own the project may still act."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ARCHIVED)
    assert project.owner_id != admin.id

    resp = client.post(
        f"/api/projects/{project.id}/unarchive",
        headers={"X-Forge-User-Id": admin.id},
    )
    assert resp.status_code == 200


def test_unarchive_a_project_that_is_not_archived_is_a_clean_4xx(client, db_session, seeded_ids):
    """(d3): not a silent no-op."""
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ACTIVE)

    resp = client.post(
        f"/api/projects/{project.id}/unarchive",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
    )
    assert resp.status_code == 400

    db_session.refresh(project)
    assert project.status == ProjectStatus.ACTIVE


def test_unarchive_unknown_project_is_404(client, seeded_ids):
    resp = client.post(
        "/api/projects/does-not-exist/unarchive",
        headers={"X-Forge-User-Id": seeded_ids["user_id"]},
    )
    assert resp.status_code == 404


# -- deprovision (the only route to destroying service data) --
#
# Router-level: no real Docker, no real Postgres. `settings.service_postgres_
# admin_dsn` is forced unset for this group so `deprovision_service_data`'s
# own Postgres half is a guaranteed no-op regardless of whether a real dev
# Postgres happens to be reachable in the environment running these tests --
# the drop mechanics themselves (against a REAL credential) are proven in
# `test_service_data_deprovision_integration.py`, not here. `_docker_client`
# is monkeypatched the same way `routers/deployments.py::_deploy_manager` is
# in `test_deployments_api.py`.


class _FakeContainers:
    def get(self, name):
        from docker.errors import NotFound

        raise NotFound("no such container")


class FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainers()


@pytest.fixture(autouse=True)
def _no_admin_dsn(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)


@pytest.fixture()
def fake_docker_client(monkeypatch):
    import app.routers.projects as projects_router

    fake_client = FakeDockerClient()
    monkeypatch.setattr(projects_router, "_docker_client", lambda: fake_client)
    return fake_client


def _make_archived_project_with_history(db, seeded_ids, *, owner_id=None):
    """An `ARCHIVED` project carrying the full history a real archival
    leaves behind (e4's fixture): a `project.archived` audit entry (so
    `_population_at_archival` resolves to something other than the
    "unknown" fallback), a `Build`, and a terminal `Deployment` row."""
    project = _make_project(
        db, seeded_ids, owner_id=owner_id, status=ProjectStatus.ARCHIVED, name="deprovision-project"
    )
    build = Build(project_id=project.id, type="create", status=BuildStatus.NOT_IMPLEMENTED)
    db.add(build)
    db.flush()
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=seeded_ids["user_id"],
        status=DeploymentStatus.STOPPED,
    )
    db.add(deployment)
    db.add(
        AuditLog(
            actor_id=None,
            action="project.archived",
            target=f"project:{project.id}",
            metadata_json={"previous_status": ProjectStatus.ACTIVE, "population": "deployed_then_idle"},
        )
    )
    db.commit()
    db.refresh(project)
    db.refresh(build)
    db.refresh(deployment)
    return project, build, deployment


def _deprovision(client, project_id, *, actor_id, body=None):
    return client.post(
        f"/api/projects/{project_id}/deprovision",
        headers={"X-Forge-User-Id": actor_id},
        json=body if body is not None else {"project_id": project_id},
    )


def test_deprovision_is_refused_to_the_owner_without_admin(
    client, db_session, seeded_ids, fake_docker_client
):
    """Ownership grants no bypass here, unlike `unarchive_project`'s
    owner-or-admin -- "operator" is the `admin` role."""
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)
    resp = _deprovision(client, project.id, actor_id=project.owner_id)
    assert resp.status_code == 403

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


def test_deprovision_is_refused_to_an_approver(client, db_session, seeded_ids, fake_docker_client):
    """(d1): `approver` does not qualify either -- only `admin` does."""
    approver = db_session.query(User).filter_by(role="approver").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)
    resp = _deprovision(client, project.id, actor_id=approver.id)
    assert resp.status_code == 403


def test_deprovision_is_permitted_for_admin_even_when_not_the_owner(
    client, db_session, seeded_ids, fake_docker_client
):
    admin = db_session.query(User).filter_by(role="admin").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)
    assert project.owner_id != admin.id

    resp = _deprovision(client, project.id, actor_id=admin.id)
    assert resp.status_code == 200


def test_deprovision_unknown_project_is_404(client, db_session, seeded_ids, fake_docker_client):
    """Role gating runs first (a plain requester is refused with 403 before
    the project lookup) so this asserts the 404 path as an `admin`, the
    only actor who could reach it."""
    admin = db_session.query(User).filter_by(role="admin").first()
    resp = client.post(
        "/api/projects/does-not-exist/deprovision",
        headers={"X-Forge-User-Id": admin.id},
        json={"project_id": "does-not-exist"},
    )
    assert resp.status_code == 404


def test_deprovision_refuses_a_project_that_is_not_archived(
    client, db_session, seeded_ids, fake_docker_client
):
    """(d2): a clean 4xx naming the actual status, not a silent no-op."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project = _make_project(db_session, seeded_ids, status=ProjectStatus.ACTIVE)

    resp = _deprovision(client, project.id, actor_id=admin.id)
    assert resp.status_code == 400
    assert "ACTIVE" in resp.json()["detail"]

    db_session.refresh(project)
    assert project.status == ProjectStatus.ACTIVE


def test_deprovision_refuses_a_confirmation_mismatch(
    client, db_session, seeded_ids, fake_docker_client
):
    """(d3): refused before any destructive call -- proven by the project
    surviving untouched (status still ARCHIVED, service_env_json untouched)
    and no `project.deprovisioned` audit entry appearing."""
    from app.services.service_env_crypto import encrypt_service_env

    admin = db_session.query(User).filter_by(role="admin").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)
    project.service_env_json = encrypt_service_env({"DATABASE_URL": "postgresql://still-here"})
    db_session.commit()

    resp = _deprovision(
        client, project.id, actor_id=admin.id, body={"project_id": "some-other-id"}
    )
    assert resp.status_code == 400

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED
    assert project.service_env_json is not None
    assert (
        db_session.query(AuditLog)
        .filter_by(action="project.deprovisioned", target=f"project:{project.id}")
        .count()
        == 0
    )


def test_deprovision_clears_service_env_and_audits_kept_backups_by_default(
    client, db_session, seeded_ids, fake_docker_client
):
    """(a5, d4, d5): the stored service env is cleared, the audit entry
    names the population recorded at archival time and that backups were
    kept (the default -- `delete_backups` omitted from the request body)."""
    from app.services.service_env_crypto import encrypt_service_env

    admin = db_session.query(User).filter_by(role="admin").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)
    project.service_env_json = encrypt_service_env({"DATABASE_URL": "postgresql://dead"})
    db_session.commit()

    resp = _deprovision(client, project.id, actor_id=admin.id)
    assert resp.status_code == 200

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED  # deprovision never changes status
    assert project.service_env_json is None

    audit = (
        db_session.query(AuditLog)
        .filter_by(action="project.deprovisioned", target=f"project:{project.id}")
        .one()
    )
    assert audit.actor_id == admin.id
    assert audit.metadata_json == {"population": "deployed_then_idle", "backups": "kept"}


def test_deprovision_audits_deleted_backups_when_requested(
    client, db_session, seeded_ids, fake_docker_client
):
    """(d4): the other branch, named distinguishably in the same audit
    entry's metadata."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)

    resp = _deprovision(
        client, project.id, actor_id=admin.id, body={"project_id": project.id, "delete_backups": True}
    )
    assert resp.status_code == 200

    audit = (
        db_session.query(AuditLog)
        .filter_by(action="project.deprovisioned", target=f"project:{project.id}")
        .one()
    )
    assert audit.metadata_json["backups"] == "deleted"


def test_deprovision_does_not_delete_the_project_row_or_its_history(
    client, db_session, seeded_ids, fake_docker_client
):
    """(e4): no `db.delete(` is introduced anywhere -- the project row, its
    build, its deployment and its pre-existing audit entry all survive."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project, build, deployment = _make_archived_project_with_history(db_session, seeded_ids)
    archive_audit_id = (
        db_session.query(AuditLog).filter_by(action="project.archived", target=f"project:{project.id}").one().id
    )

    resp = _deprovision(client, project.id, actor_id=admin.id)
    assert resp.status_code == 200

    assert db_session.get(Project, project.id) is not None
    assert db_session.get(Build, build.id) is not None
    assert db_session.get(Deployment, deployment.id) is not None
    assert db_session.get(AuditLog, archive_audit_id) is not None


def test_deprovision_is_refused_when_docker_is_unreachable(
    client, db_session, seeded_ids, monkeypatch
):
    """Docker-unreachable refuses the whole action before any destructive
    call, rather than partially tearing a project down."""
    import app.routers.projects as projects_router

    def _boom():
        raise RuntimeError("Docker daemon unreachable")

    monkeypatch.setattr(projects_router, "_docker_client", _boom)
    admin = db_session.query(User).filter_by(role="admin").first()
    project, _, _ = _make_archived_project_with_history(db_session, seeded_ids)

    resp = _deprovision(client, project.id, actor_id=admin.id)
    assert resp.status_code == 503

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


# -- no deprovision under a live deployment --


@pytest.fixture()
def drop_calls(monkeypatch):
    """Replaces the one function that issues the DROPs with a recorder, so a
    refused request can be shown never to have reached it."""
    import app.routers.projects as projects_router

    calls: list[str] = []

    def recorder(project_id, *, docker_client, delete_backups=False):
        calls.append(project_id)
        return {"backups_deleted": delete_backups}

    monkeypatch.setattr(projects_router, "deprovision_service_data", recorder)
    return calls


def _add_deployment(db, project, build, status):
    row = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _assert_nothing_destroyed(db, project, drop_calls):
    from app.services.service_env_crypto import decrypt_service_env

    assert drop_calls == []  # the DROPs were never reached
    db.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED
    assert decrypt_service_env(project.service_env_json) == {"DATABASE_URL": "postgresql://still-here"}
    assert (
        db.query(AuditLog)
        .filter_by(action="project.deprovisioned", target=f"project:{project.id}")
        .count()
        == 0
    )


def _archived_project_with_stored_env(db, seeded_ids):
    from app.services.service_env_crypto import encrypt_service_env

    project, build, stopped = _make_archived_project_with_history(db, seeded_ids)
    project.service_env_json = encrypt_service_env({"DATABASE_URL": "postgresql://still-here"})
    db.commit()
    return project, build, stopped


@pytest.mark.parametrize("live_status", [DeploymentStatus.ACTIVE, DeploymentStatus.DEPLOYING])
def test_deprovision_is_refused_with_409_naming_a_live_deployment(
    client, db_session, seeded_ids, fake_docker_client, drop_calls, live_status
):
    """The project IS archived and the confirmation IS right, so only the
    live-deployment guard can be refusing -- and it names the deployment and
    leaves everything as it was."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project, build, _stopped = _archived_project_with_stored_env(db_session, seeded_ids)
    live = _add_deployment(db_session, project, build, live_status)

    resp = _deprovision(client, project.id, actor_id=admin.id)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("DEPLOYMENT_LIVE: ")
    assert live.id in detail
    assert live_status in detail
    assert "stop it" in detail
    _assert_nothing_destroyed(db_session, project, drop_calls)


def test_deprovision_names_every_live_deployment(
    client, db_session, seeded_ids, fake_docker_client, drop_calls
):
    """A redeploy in flight is an ACTIVE row plus a DEPLOYING one."""
    admin = db_session.query(User).filter_by(role="admin").first()
    project, build, _stopped = _archived_project_with_stored_env(db_session, seeded_ids)
    active = _add_deployment(db_session, project, build, DeploymentStatus.ACTIVE)
    deploying = _add_deployment(db_session, project, build, DeploymentStatus.DEPLOYING)

    resp = _deprovision(client, project.id, actor_id=admin.id)

    assert resp.status_code == 409
    assert active.id in resp.json()["detail"]
    assert deploying.id in resp.json()["detail"]
    _assert_nothing_destroyed(db_session, project, drop_calls)


def test_deprovision_proceeds_once_the_live_deployment_is_stopped(
    client, db_session, seeded_ids, fake_docker_client, drop_calls
):
    admin = db_session.query(User).filter_by(role="admin").first()
    project, build, _stopped = _archived_project_with_stored_env(db_session, seeded_ids)
    live = _add_deployment(db_session, project, build, DeploymentStatus.ACTIVE)
    assert _deprovision(client, project.id, actor_id=admin.id).status_code == 409

    live.status = DeploymentStatus.STOPPED
    db_session.commit()
    resp = _deprovision(client, project.id, actor_id=admin.id)

    assert resp.status_code == 200
    assert drop_calls == [project.id]


@pytest.mark.parametrize(
    "terminal_status",
    [DeploymentStatus.STOPPED, DeploymentStatus.SUPERSEDED, DeploymentStatus.FAILED],
)
def test_an_archived_project_with_only_finished_deployments_still_deprovisions(
    client, db_session, seeded_ids, fake_docker_client, drop_calls, terminal_status
):
    admin = db_session.query(User).filter_by(role="admin").first()
    project, build, stopped = _make_archived_project_with_history(db_session, seeded_ids)
    stopped.status = terminal_status
    _add_deployment(db_session, project, build, DeploymentStatus.SUPERSEDED)
    _add_deployment(db_session, project, build, DeploymentStatus.FAILED)
    db_session.commit()

    resp = _deprovision(client, project.id, actor_id=admin.id)

    assert resp.status_code == 200
    assert drop_calls == [project.id]
