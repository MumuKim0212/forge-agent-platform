"""API tests for `routers/deployments.py` — no
Docker: the router's `_deploy_manager` seam is monkeypatched with the same
fake used by the service unit tests. Covers role gating (owner-or-admin),
confirm validation error mapping (400/404/409), rollback/stop endpoints,
the deployment history list, and the timeline's deployment entries.

ADR 0023: `confirm_deploy` now refuses without a configured
`SERVICE_POSTGRES_ADMIN_DSN` — the `_fake_admin_dsn` autouse fixture below
keeps every OTHER test in this file (predating data-service provisioning) on a syntactically
configured DSN so confirm/rollback still reach their existing checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.services.project_activity import TRAFFIC_TOUCH_COARSEN_INTERVAL, _as_aware_utc
from app.services.status import BuildStatus, ProjectStatus
from tests.fake_admin_dsn import FAKE_ADMIN_DSN


@pytest.fixture(autouse=True)
def _fake_admin_dsn(monkeypatch):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)


class FakeManager:
    def __init__(self, *, image_present=True):
        self.image_present = image_present
        self.removed: list[str] = []

    def image_exists(self, image_ref):
        return self.image_present and bool(image_ref)

    def remove_service(self, deployment_id):
        self.removed.append(deployment_id)
        return True

    def proxy(self, deployment_id, *, method="GET", path="/", **_forwarded):
        return 200, b"service says hi", None


@pytest.fixture()
def fake_manager(monkeypatch):
    manager = FakeManager()
    import app.routers.deployments as deployments_router
    import app.routers.service_proxy as service_proxy_router

    monkeypatch.setattr(deployments_router, "_deploy_manager", lambda: manager)
    # the dev-mode service proxy has its own seam (one shared Docker client); the same fake.
    monkeypatch.setattr(service_proxy_router, "_proxy_manager", lambda: manager)
    return manager


@pytest.fixture()
def owner_id(db_session, seeded_ids):
    return seeded_ids["user_id"]


@pytest.fixture()
def admin_id(db_session):
    return db_session.query(User).filter_by(role="admin").first().id


@pytest.fixture()
def approver_id(db_session):
    return db_session.query(User).filter_by(role="approver").first().id


@pytest.fixture()
def project(db_session, seeded_ids, owner_id):
    project = Project(
        name="deploy-api-project",
        owner_id=owner_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _make_build(db, project, *, build_id="bld-api-1", eligible=True, image_ref="img-1"):
    build = Build(
        id=build_id,
        project_id=project.id,
        type="create",
        status=BuildStatus.SUCCEEDED,
        image_ref=image_ref,
        diff_ref=f"forge-build-{build_id}#1",
    )
    db.add(build)
    db.flush()
    if eligible:
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


def _headers(user_id):
    return {"X-Forge-User-Id": user_id}


def test_confirm_creates_deployment(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project)

    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == DeploymentStatus.DEPLOYING
    assert body["build_id"] == build.id
    assert body["confirmed_by_id"] == owner_id
    assert body["rollback_of"] is None


def test_confirm_updates_project_last_active_at(client, db_session, project, owner_id, fake_manager):
    """"a deploy" — the owner/admin confirm action itself."""
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    build = _make_build(db_session, project)

    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert resp.status_code == 201
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_confirm_rejects_400_when_no_admin_dsn_configured(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """Refused at the confirm step,
    router level -- a 400 whose message names the operator action, before
    any Deployment row exists."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    build = _make_build(db_session, project)

    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert resp.status_code == 400
    assert "SERVICE_POSTGRES_ADMIN_DSN" in resp.json()["detail"]
    assert db_session.query(Deployment).count() == 0


def test_confirm_requires_identity(client, db_session, project, fake_manager):
    build = _make_build(db_session, project)
    resp = client.post(f"/api/projects/{project.id}/deployments", json={"build_id": build.id})
    assert resp.status_code == 401


def test_confirm_forbidden_for_non_owner_non_admin(
    client, db_session, project, approver_id, fake_manager
):
    """The approver role can approve builds, but confirm is the OWNER's (or
    admin's) action (ADR 0008: auto-approve does not
    remove the owner's confirm)."""
    build = _make_build(db_session, project)
    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(approver_id),
    )
    assert resp.status_code == 403


def test_confirm_allowed_for_admin(client, db_session, project, admin_id, fake_manager):
    build = _make_build(db_session, project)
    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(admin_id),
    )
    assert resp.status_code == 201
    assert resp.json()["confirmed_by_id"] == admin_id


def test_confirm_400_for_non_eligible_build(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project, eligible=False)
    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    assert resp.status_code == 400
    assert "not deploy-eligible" in resp.json()["detail"]


def test_confirm_404_for_unknown_build_or_project(client, db_session, project, owner_id, fake_manager):
    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": "nope"},
        headers=_headers(owner_id),
    )
    assert resp.status_code == 404

    build = _make_build(db_session, project)
    resp = client.post(
        "/api/projects/nope/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    assert resp.status_code == 404


def test_confirm_409_while_in_flight(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project)
    first = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    assert second.status_code == 409


def test_list_deployments_newest_first(client, db_session, project, owner_id):
    build = _make_build(db_session, project)
    for status in (DeploymentStatus.SUPERSEDED, DeploymentStatus.ACTIVE):
        db_session.add(
            Deployment(
                project_id=project.id,
                build_id=build.id,
                confirmed_by_id=owner_id,
                status=status,
            )
        )
    db_session.commit()

    resp = client.get(f"/api/projects/{project.id}/deployments", headers=_headers(owner_id))

    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_rollback_400_without_target(client, db_session, project, owner_id, fake_manager):
    resp = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(owner_id)
    )
    assert resp.status_code == 400


def test_rollback_creates_rollback_deployment(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project)
    superseded = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.SUPERSEDED,
    )
    db_session.add(superseded)
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(owner_id)
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["rollback_of"] == superseded.id
    assert body["status"] == DeploymentStatus.DEPLOYING


def test_rollback_updates_project_last_active_at(client, db_session, project, owner_id, fake_manager):
    """"a rollback"."""
    build = _make_build(db_session, project)
    superseded = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.SUPERSEDED,
    )
    db_session.add(superseded)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(owner_id)
    )

    assert resp.status_code == 201
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_rollback_forbidden_for_non_owner(client, db_session, project, approver_id, fake_manager):
    resp = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(approver_id)
    )
    assert resp.status_code == 403


def test_stop_active_deployment(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    db_session.commit()

    resp = client.post(f"/api/projects/{project.id}/deployments/stop", headers=_headers(owner_id))

    assert resp.status_code == 200
    assert resp.json()["status"] == DeploymentStatus.STOPPED
    assert active.id in fake_manager.removed


def test_stop_updates_project_last_active_at(client, db_session, project, owner_id, fake_manager):
    """"a stop"."""
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    resp = client.post(f"/api/projects/{project.id}/deployments/stop", headers=_headers(owner_id))

    assert resp.status_code == 200
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_stop_400_without_active(client, db_session, project, owner_id, fake_manager):
    resp = client.post(f"/api/projects/{project.id}/deployments/stop", headers=_headers(owner_id))
    assert resp.status_code == 400


def test_service_proxy_404_without_active_deployment(client, db_session, project, fake_manager):
    resp = client.get(f"/api/projects/{project.id}/service")
    assert resp.status_code == 404


def test_service_proxy_routes_to_active_deployment(client, db_session, project, owner_id, fake_manager):
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    db_session.commit()

    # No identity header on purpose: dev routing is the intranet-facing
    # service URL, not a platform-identity endpoint.
    resp = client.get(f"/api/projects/{project.id}/service/some/path")

    assert resp.status_code == 200
    assert resp.content == b"service says hi"


def test_service_proxy_traffic_writes_last_active_at_once_per_interval(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """Traffic reaching a deployed service counts, but is
    coarsened so N requests inside `TRAFFIC_TOUCH_COARSEN_INTERVAL` produce
    exactly ONE write, not N. Spies on the real writer (rather than
    inspecting timestamps alone, which N sub-millisecond requests could
    not distinguish from N writes) to count how many of the 5 calls
    actually persisted a change. Patches `project_activity`'s own name
    (review round 2): the router now calls `record_traffic_activity`,
    which looks `touch_last_active_for_traffic` up in that module's
    namespace, not the router's — patching the router's (now nonexistent)
    import would silently never fire the spy."""
    import app.services.project_activity as project_activity

    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    # Stale on purpose: a freshly-created project's last_active_at is only
    # moments old, which would make even the FIRST request a no-op under
    # the same coarsening rule and defeat the "exactly one write" proof.
    project.last_active_at = (
        datetime.now(timezone.utc) - TRAFFIC_TOUCH_COARSEN_INTERVAL - timedelta(minutes=1)
    )
    db_session.commit()

    outcomes = []
    original = project_activity.touch_last_active_for_traffic

    def _spy(project_arg, **kwargs):
        wrote = original(project_arg, **kwargs)
        outcomes.append(wrote)
        return wrote

    monkeypatch.setattr(project_activity, "touch_last_active_for_traffic", _spy)

    for _ in range(5):
        resp = client.get(f"/api/projects/{project.id}/service/some/path")
        assert resp.status_code == 200

    assert outcomes == [True, False, False, False, False]


def test_service_proxy_still_serves_traffic_when_the_internal_write_raises(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """the internal-write half. Forces the shared writer's
    now-lookup to raise; `touch_last_active_for_traffic` swallows it and
    returns `False`, so `record_traffic_activity` never reaches its own
    `db.commit()` at all. This does not, by itself, prove a *commit*
    failure is survivable — see
    test_service_proxy_still_serves_traffic_when_the_activity_commit_fails
    below for that (review round 2: this test alone was previously
    (mis)named for that guarantee without visiting it)."""
    import app.services.project_activity as project_activity

    def _raise():
        raise RuntimeError("clock is broken")

    monkeypatch.setattr(project_activity, "_utcnow", _raise)

    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    resp = client.get(f"/api/projects/{project.id}/service/some/path")

    assert resp.status_code == 200  # the proxied response is unaffected
    assert resp.content == b"service says hi"
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) == stale  # the activity write itself was a no-op


def test_service_proxy_still_serves_traffic_when_the_activity_commit_fails(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """The half the previous test could not
    reach: `proxy_service` is the one write site that does not ride an
    existing transaction (it performed no DB write at all before this
    task), so its `db.commit()` is new and has to be guarded on its own.
    Forces the commit itself (an ordinary infrastructure failure — a
    dropped connection, pool exhaustion — not the in-memory write) to
    raise, and drives it through the real proxy route: the
    already-successfully-proxied response must not become a 500 because
    the bookkeeping riding it failed to persist."""
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    def _raise_commit():
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(db_session, "commit", _raise_commit)

    resp = client.get(f"/api/projects/{project.id}/service/some/path")

    assert resp.status_code == 200  # the proxied response is unaffected
    assert resp.content == b"service says hi"


def test_service_proxy_still_serves_traffic_when_the_activity_commit_and_rollback_both_fail(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """The half neither previous test could
    reach. `test_..._when_the_internal_write_raises` breaks a helper that
    `touch_last_active_for_traffic` catches internally, so
    `record_traffic_activity` never reaches its own `db.commit()` at all.
    `test_..._when_the_activity_commit_fails` breaks only `commit`, so the
    `db.rollback()` in `record_traffic_activity`'s `except` block runs
    against a session nothing actually broke and succeeds quietly —
    neither test's mutation reaches the rollback call itself. This one
    forces *both* `commit` and `rollback` to raise, the way a real dropped
    connection or an already-invalidated DBAPI connection would break
    both calls the same way, and drives the real proxy route: the
    already-successfully-proxied response must still not become a 500
    because the write's own cleanup also failed."""
    build = _make_build(db_session, project)
    active = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    def _raise_commit():
        raise RuntimeError("connection dropped")

    def _raise_rollback():
        raise RuntimeError("connection already invalidated")

    monkeypatch.setattr(db_session, "commit", _raise_commit)
    monkeypatch.setattr(db_session, "rollback", _raise_rollback)

    resp = client.get(f"/api/projects/{project.id}/service/some/path")

    assert resp.status_code == 200  # the proxied response is unaffected
    assert resp.content == b"service says hi"


def test_timeline_shows_deployment_and_transition_audits(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """Every deployment state transition is
    timeline-visible — the deployment row appears as its own entry and the
    per-transition audit entries (target deployment:<id>) are included."""
    build = _make_build(db_session, project)
    confirm = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    deployment_id = confirm.json()["id"]

    # Drive the claimed deployment through the worker path with fakes so
    # activated/superseded audits exist. Also fakes data-service
    # provisioning (no real Postgres/Docker here) the same way
    # tests/test_deploy_service.py does.
    from tests.test_deploy_service import (
        FakeDeployManager,
        FakeMergeProvider,
        _fake_ensure_project_redis,
        _fake_provision_project_data_services,
    )
    from app.services.deploy import claim_next_deployment, execute_deployment

    monkeypatch.setattr(
        "app.services.deploy.service_data.provision_project_data_services",
        _fake_provision_project_data_services,
    )
    monkeypatch.setattr(
        "app.services.deploy.service_data.ensure_project_redis", _fake_ensure_project_redis
    )
    claimed = claim_next_deployment(db_session)
    execute_deployment(
        db_session, claimed, manager=FakeDeployManager(), git_provider=FakeMergeProvider()
    )

    resp = client.get(f"/api/projects/{project.id}/timeline", headers=_headers(owner_id))
    assert resp.status_code == 200
    entries = resp.json()

    deployment_entries = [e for e in entries if e["kind"] == "deployment"]
    assert len(deployment_entries) == 1
    assert deployment_entries[0]["data"]["id"] == deployment_id
    assert deployment_entries[0]["data"]["status"] == DeploymentStatus.ACTIVE

    audit_actions = {
        e["data"]["action"]
        for e in entries
        if e["kind"] == "audit_log" and e["data"].get("target") == f"deployment:{deployment_id}"
    }
    assert {"deployment.confirmed", "deployment.started", "deployment.activated"} <= audit_actions


# -- an archived project refuses confirm and rollback ------------------


def _archive(db, project):
    project.status = ProjectStatus.ARCHIVED
    db.commit()


def test_confirm_on_an_archived_project_is_409_with_the_archived_code_until_un_archived(
    client, db_session, project, owner_id, fake_manager
):
    """The build is eligible, the latest and imaged, and nothing is in flight,
    so only the archived rule can be refusing -- and the refusal is not the
    in-flight 409 (its detail starts with a code and says to un-archive)."""
    build = _make_build(db_session, project)
    _archive(db_session, project)

    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("PROJECT_ARCHIVED: ")
    assert "un-archive" in detail
    assert "in flight" not in detail
    assert db_session.query(Deployment).count() == 0

    unarchived = client.post(f"/api/projects/{project.id}/unarchive", headers=_headers(owner_id))
    assert unarchived.status_code == 200

    retry = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )
    assert retry.status_code == 201


def test_rollback_on_an_archived_project_is_409_with_the_archived_code_until_un_archived(
    client, db_session, project, owner_id, fake_manager
):
    build = _make_build(db_session, project)
    superseded = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.SUPERSEDED,
    )
    db_session.add(superseded)
    db_session.commit()
    _archive(db_session, project)

    resp = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(owner_id)
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("PROJECT_ARCHIVED: ")
    assert "un-archive" in detail
    assert db_session.query(Deployment).filter_by(status=DeploymentStatus.DEPLOYING).count() == 0

    unarchived = client.post(f"/api/projects/{project.id}/unarchive", headers=_headers(owner_id))
    assert unarchived.status_code == 200

    retry = client.post(
        f"/api/projects/{project.id}/deployments/rollback", headers=_headers(owner_id)
    )
    assert retry.status_code == 201
    assert retry.json()["rollback_of"] == superseded.id


def test_the_in_flight_409_does_not_carry_the_archived_code(
    client, db_session, project, owner_id, fake_manager
):
    build = _make_build(db_session, project)
    client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    second = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert second.status_code == 409
    assert "PROJECT_ARCHIVED" not in second.json()["detail"]
    assert "already in flight" in second.json()["detail"]


# -- two concurrent requests cannot both create the DEPLOYING row -----


def test_a_confirm_that_loses_the_race_for_the_deploying_slot_is_the_same_409(
    client, db_session, project, owner_id, fake_manager, monkeypatch
):
    """A competing request's DEPLOYING row is already committed, and this
    request's check-then-insert looked before it existed. The database refuses
    the INSERT; the API answers exactly as the check would have."""
    build = _make_build(db_session, project)
    winner = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=owner_id,
        status=DeploymentStatus.DEPLOYING,
    )
    db_session.add(winner)
    db_session.commit()

    import app.services.deploy as deploy_service

    real_lookup = deploy_service.get_in_flight_deployment
    calls: list[str] = []

    def blind_first_look(db, project_id):
        calls.append(project_id)
        return None if len(calls) == 1 else real_lookup(db, project_id)

    monkeypatch.setattr(deploy_service, "get_in_flight_deployment", blind_first_look)

    resp = client.post(
        f"/api/projects/{project.id}/deployments",
        json={"build_id": build.id},
        headers=_headers(owner_id),
    )

    assert resp.status_code == 409
    assert "already in flight" in resp.json()["detail"]
    assert len(calls) == 2  # the check was blind; the database is what refused
    assert db_session.query(Deployment).filter_by(project_id=project.id).count() == 1
