"""Integration tests for the deploy slice against a real Docker
Engine: confirm -> worker claim -> merge (faked git host)
-> health-gated switchover -> ACTIVE, health-timeout failure leaving the
old deployment routed, rollback, stop, secret-env injection into the real
container, preview teardown on promotion, dev-mode platform-proxy routing
through the real API route, and zero orphaned containers/networks.

Builds the REAL generic-webtool template skeleton image once per module —
doubling as the test that the template seed app actually serves
`GET /healthz` -> 200 (the template acceptance criterion). Skips
cleanly (not fails) when Docker is unavailable.

ADR 0023: `confirm_deploy`/`execute_deployment` now provision real
data services on every call in this module — the `_real_data_services`
autouse fixture below points `settings.service_postgres_admin_dsn` /
`service_postgres_container_name` at the module's own throwaway Postgres
(`tests/conftest.py`'s `real_service_postgres_admin_dsn`), so every
confirm->execute flow here provisions a genuine per-project database, role,
and Redis container, exactly as production does.

the template image built by `template_image` now also runs
`alembic upgrade head` (its own Dockerfile CMD) against the real per-
project database before serving, and exposes `/items` (the seed's one
table, read+write) and `/cache-roundtrip` (a real Redis write+read) --
`test_migration_creates_table_before_first_request` and
`test_redis_roundtrip_against_real_data_services` below exercise both
against the real containers this module already provisions.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.approval import ApprovalRequest, ApprovalStatus  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.deployment import (  # noqa: E402
    Deployment,
    DeploymentFailureReason,
    DeploymentStatus,
)
from app.models.project import Project  # noqa: E402
from app.services.deploy import (  # noqa: E402
    claim_next_deployment,
    confirm_deploy,
    execute_deployment,
    request_rollback,
    stop_active_deployment,
)
from app.services.deploy_manager import DeployManager  # noqa: E402
from app.services.service_env_crypto import decrypt_service_env  # noqa: E402
from app.services.status import BuildStatus, ProjectStatus  # noqa: E402
from tests.test_deploy_service import FakeMergeProvider, MERGE_SHA  # noqa: E402

TEMPLATE_APP_IMAGE = "forge-deploy-test-app:latest"
NEVER_HEALTHY_IMAGE = "forge-deploy-test-sleep:latest"
ROOT_WRITER_IMAGE = "forge-deploy-test-root-writer:latest"


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine unavailable"),
]


@pytest.fixture(scope="module")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="module", autouse=True)
def _real_data_services(real_service_postgres_admin_dsn):
    """every `confirm_deploy` call in this module now requires a
    configured admin DSN (PROVISION_UNCONFIGURED) -- point
    it at the module's real throwaway Postgres so the existing happy-path
    tests keep exercising a REAL confirm -> execute -> ACTIVE flow, now
    provisioning real per-project Postgres roles/databases and Redis
    containers along the way."""
    yield


@pytest.fixture(scope="module")
def template_image(docker_client):
    """The REAL template skeleton image (backend/templates/generic-webtool)
    — proves the seed app is a real FastAPI service with /healthz."""
    docker_client.images.build(
        path="templates/generic-webtool", tag=TEMPLATE_APP_IMAGE, rm=True
    )
    yield TEMPLATE_APP_IMAGE
    try:
        docker_client.images.get(TEMPLATE_APP_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture(scope="module")
def never_healthy_image(docker_client):
    """An image that runs but never serves HTTP — the HEALTH_TIMEOUT case."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(
            'FROM python:3.11-slim\nCMD ["sleep", "infinity"]\n'
        )
        docker_client.images.build(path=tmp, tag=NEVER_HEALTHY_IMAGE, rm=True)
    yield NEVER_HEALTHY_IMAGE
    try:
        docker_client.images.get(NEVER_HEALTHY_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture(scope="module")
def root_writer_image(docker_client):
    """an app that, like one written against the root-owned `/app`, writes there at
    startup and only then serves `/healthz`. As root it is healthy; as nobody it dies at its
    first line."""
    code = (
        "open('/app/state.txt','w').write('x')\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "HTTPServer(('0.0.0.0', 8000), H).serve_forever()\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "app.py").write_text(code)
        Path(tmp, "Dockerfile").write_text(
            "FROM python:3.11-slim\nWORKDIR /app\nCOPY app.py /srv/app.py\n"
            'CMD ["python", "/srv/app.py"]\n'
        )
        docker_client.images.build(path=tmp, tag=ROOT_WRITER_IMAGE, rm=True)
    yield ROOT_WRITER_IMAGE
    try:
        docker_client.images.get(ROOT_WRITER_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture()
def manager(docker_client):
    return DeployManager(client=docker_client)


@pytest.fixture()
def project(db_session, seeded_ids):
    project = Project(
        name="deploy-it-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _make_build(db, project, *, build_id, image_ref):
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
    db.refresh(build)
    return build


def _confirm_and_execute(db, project, build, manager, *, provider=None):
    deployment = confirm_deploy(
        db,
        project=project,
        build=build,
        actor_id=project.owner_id,
        image_exists=manager.image_exists,
    )
    db.commit()
    claimed = claim_next_deployment(db)
    assert claimed.id == deployment.id
    execute_deployment(db, claimed, manager=manager, git_provider=provider or FakeMergeProvider())
    db.refresh(deployment)
    return deployment


def _cleanup_deployments(manager, db):
    for deployment in db.query(Deployment).all():
        manager.remove_service(deployment.id)


def test_confirm_to_healthy_activation_end_to_end(
    docker_client, db_session, project, template_image, manager, client
):
    build = _make_build(db_session, project, build_id="dep-happy", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)

        assert deployment.status == DeploymentStatus.ACTIVE
        db_session.refresh(project)
        assert project.confirmed_ref == MERGE_SHA  # merge advanced the confirmed ref

        # L4 runtime rules: hardened, capped, restartable,
        # labeled, no Docker socket, no published ports, internal network.
        container = docker_client.containers.get(f"forge-deploy-{deployment.id}")
        attrs = container.attrs
        host_config = attrs["HostConfig"]
        assert container.status == "running"
        assert host_config["Privileged"] is False
        assert attrs["Config"]["User"] == "65534:65534"  # as its preview runs
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["RestartPolicy"]["Name"] == "unless-stopped"
        assert host_config["Memory"] == settings.sandbox_memory_limit_mb * 1024 * 1024
        assert host_config["PidsLimit"] == settings.sandbox_pids_limit
        assert not host_config.get("Binds")  # no volumes -> no Docker socket
        assert not host_config.get("PortBindings")
        network_names = list(attrs["NetworkSettings"]["Networks"].keys())
        assert len(network_names) == 1
        assert docker_client.networks.get(network_names[0]).attrs["Internal"] is True
        assert attrs["Config"]["Labels"]["forge.project_id"] == project.id
        assert attrs["Config"]["Labels"]["forge.build_id"] == build.id

        # Secret injection: the stored service env is the container's env,
        # secret key included; issued values never appear in audit rows.
        env_pairs = dict(e.split("=", 1) for e in attrs["Config"]["Env"] if "=" in e)
        service_env = decrypt_service_env(project.service_env_json)
        assert service_env["FORGE_APP_SECRET_KEY"]
        for key, value in service_env.items():
            assert env_pairs[key] == value
        from app.models.audit import AuditLog

        for row in db_session.query(AuditLog).all():
            assert service_env["FORGE_APP_SECRET_KEY"] not in str(row.metadata_json or "")

        # Dev routing: reachable through the platform-proxy route, no host
        # port published, no identity header.
        resp = client.get(f"/api/projects/{project.id}/service/healthz")
        assert resp.status_code == 200
        assert b"ok" in resp.content
        # ADR 0028 (a4): the real proxy path, not a stub -- all three
        # isolation headers, with the upstream Content-Type genuinely
        # forwarded (the template's JSON endpoints answer application/json).
        assert resp.headers["content-security-policy"] == (
            "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        )
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-type"].startswith("application/json")
    finally:
        _cleanup_deployments(manager, db_session)


def test_redeploy_supersedes_old_and_service_env_stays_stable(
    docker_client, db_session, project, template_image, manager
):
    build1 = _make_build(db_session, project, build_id="dep-v1", image_ref=template_image)
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        env_after_first = decrypt_service_env(project.service_env_json)
        build2 = _make_build(db_session, project, build_id="dep-v2", image_ref=template_image)

        # build1 is no longer the latest successful build -> only build2 is
        # confirmable now.
        from app.services.deploy import DeployValidationError

        with pytest.raises(DeployValidationError, match="latest successful"):
            confirm_deploy(
                db_session,
                project=project,
                build=build1,
                actor_id=project.owner_id,
                image_exists=manager.image_exists,
            )
        db_session.rollback()

        second = _confirm_and_execute(db_session, project, build2, manager)

        db_session.refresh(first)
        assert second.status == DeploymentStatus.ACTIVE
        assert first.status == DeploymentStatus.SUPERSEDED
        with pytest.raises(NotFound):
            docker_client.containers.get(f"forge-deploy-{first.id}")  # old container removed

        # Secret stability across redeploys.
        db_session.refresh(project)
        assert decrypt_service_env(project.service_env_json) == env_after_first
        new_container = docker_client.containers.get(f"forge-deploy-{second.id}")
        env_pairs = dict(
            e.split("=", 1) for e in new_container.attrs["Config"]["Env"] if "=" in e
        )
        assert env_pairs["FORGE_APP_SECRET_KEY"] == env_after_first["FORGE_APP_SECRET_KEY"]
    finally:
        _cleanup_deployments(manager, db_session)


def test_health_timeout_leaves_old_deployment_active_and_routed(
    docker_client, db_session, project, template_image, never_healthy_image, manager, monkeypatch
):
    build1 = _make_build(db_session, project, build_id="dep-good", image_ref=template_image)
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        assert first.status == DeploymentStatus.ACTIVE
        build2 = _make_build(db_session, project, build_id="dep-bad", image_ref=never_healthy_image)

        monkeypatch.setattr(settings, "deploy_health_check_timeout_seconds", 4.0)
        second = _confirm_and_execute(db_session, project, build2, manager)

        assert second.status == DeploymentStatus.FAILED
        assert second.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
        with pytest.raises(NotFound):
            docker_client.containers.get(f"forge-deploy-{second.id}")  # new container removed

        # Old deployment untouched and STILL routed (spec: "the old
        # deployment stays ACTIVE and routed, untouched").
        db_session.refresh(first)
        assert first.status == DeploymentStatus.ACTIVE
        status, body, _content_type = manager.proxy(first.id, path="/healthz")
        assert status == 200
    finally:
        _cleanup_deployments(manager, db_session)


def test_an_app_that_writes_under_app_at_startup_fails_the_health_gate_and_the_old_deployment_stays(
    docker_client, db_session, project, template_image, root_writer_image, manager, monkeypatch
):
    """deploy containers run as nobody, so a service that relied on writing under the
    root-owned `/app` fails its health gate (HEALTH_TIMEOUT, named, bounded by the 4 s window)
    and the previous deployment stays ACTIVE and routed. Not a hang: if the user were dropped the
    app would start and the deployment would be ACTIVE, failing this test at once."""
    build1 = _make_build(db_session, project, build_id="dep-ok", image_ref=template_image)
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        assert first.status == DeploymentStatus.ACTIVE
        build2 = _make_build(db_session, project, build_id="dep-rootw", image_ref=root_writer_image)

        monkeypatch.setattr(settings, "deploy_health_check_timeout_seconds", 4.0)
        second = _confirm_and_execute(db_session, project, build2, manager)

        assert second.status == DeploymentStatus.FAILED
        assert second.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
        db_session.refresh(first)
        assert first.status == DeploymentStatus.ACTIVE
        status, _body, _content_type = manager.proxy(first.id, path="/healthz")
        assert status == 200
    finally:
        _cleanup_deployments(manager, db_session)


def test_rollback_restores_previous_deployment_runtime_only(
    docker_client, db_session, project, template_image, manager
):
    build1 = _make_build(db_session, project, build_id="rb-v1", image_ref=template_image)
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        build2 = _make_build(db_session, project, build_id="rb-v2", image_ref=template_image)
        second = _confirm_and_execute(db_session, project, build2, manager)
        db_session.refresh(project)
        confirmed_ref_before = project.confirmed_ref

        rollback = request_rollback(
            db_session,
            project=project,
            actor_id=project.owner_id,
            image_exists=manager.image_exists,
        )
        db_session.commit()
        claimed = claim_next_deployment(db_session)
        provider = FakeMergeProvider()
        execute_deployment(db_session, claimed, manager=manager, git_provider=provider)

        db_session.refresh(rollback)
        db_session.refresh(second)
        db_session.refresh(project)
        assert rollback.status == DeploymentStatus.ACTIVE
        assert rollback.rollback_of == first.id
        assert rollback.build_id == build1.id
        assert second.status == DeploymentStatus.SUPERSEDED
        assert provider.any_calls == 0  # runtime-only: no git operation at all
        assert project.confirmed_ref == confirmed_ref_before  # ref does not move

        # The rolled-back service really serves again, health-gated.
        status, _body, _content_type = manager.proxy(rollback.id, path="/healthz")
        assert status == 200
    finally:
        _cleanup_deployments(manager, db_session)


def test_stop_removes_container_and_route(
    docker_client, db_session, project, template_image, manager, client
):
    build = _make_build(db_session, project, build_id="dep-stop", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)

        stopped = stop_active_deployment(
            db_session, project=project, actor_id=project.owner_id, manager=manager
        )
        db_session.commit()

        assert stopped.status == DeploymentStatus.STOPPED
        with pytest.raises(NotFound):
            docker_client.containers.get(f"forge-deploy-{deployment.id}")
        # Route gone: the platform-proxy route 404s with no ACTIVE deployment.
        resp = client.get(f"/api/projects/{project.id}/service/healthz")
        assert resp.status_code == 404
    finally:
        _cleanup_deployments(manager, db_session)


def test_preview_teardown_on_promotion(
    docker_client, db_session, project, template_image, manager
):
    """ADR 0016 teardown trigger: promotion — the confirmed build's live
    preview container is torn down and its preview columns cleared on
    successful activation. (The "promotion" teardown trigger,
    pinned separately from "TTL expiry" and "newer build"): the preview's
    OWN Postgres database/role and Redis container are dropped too."""
    import psycopg

    from app.services.preview_data import _preview_identifier, preview_redis_container_name
    from app.services.preview_manager import PreviewManager

    preview_manager = PreviewManager(client=docker_client)
    build = _make_build(db_session, project, build_id="dep-promote", image_ref=template_image)
    try:
        issued = preview_manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()
        preview_container = docker_client.containers.get(f"forge-preview-{build.id}")
        assert preview_container.status == "running"

        deployment = _confirm_and_execute(db_session, project, build, manager)

        assert deployment.status == DeploymentStatus.ACTIVE
        with pytest.raises(NotFound):
            docker_client.containers.get(f"forge-preview-{build.id}")
        db_session.refresh(build)
        assert build.preview_url is None
        assert build.preview_expires_at is None
        assert build.preview_token_hash is None

        preview_name = _preview_identifier(build.id)
        admin_conn = psycopg.connect(settings.service_postgres_admin_dsn)
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (preview_name,))
                assert cur.fetchone() is None
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (preview_name,))
                assert cur.fetchone() is None
        finally:
            admin_conn.close()
        with pytest.raises(NotFound):
            docker_client.containers.get(preview_redis_container_name(build.id))
    finally:
        preview_manager.stop_preview(build.id)
        _cleanup_deployments(manager, db_session)


def test_no_orphaned_containers_or_networks_after_full_lifecycle(
    docker_client, db_session, project, template_image, manager
):
    """Every per-DEPLOYMENT container/network is gone after deploy ->
    redeploy -> stop. ADR 0023: the project's OWN Redis container is
    the ONE deliberate exception -- "never deleted implicitly" means a stop
    (or a redeploy) leaves data services intact, so it is asserted present
    by name rather than counted as a leak."""
    from app.services.service_data import redis_container_name

    before_containers = {c.id for c in docker_client.containers.list(all=True)}
    before_networks = {n.id for n in docker_client.networks.list()}

    build1 = _make_build(db_session, project, build_id="lc-v1", image_ref=template_image)
    _confirm_and_execute(db_session, project, build1, manager)  # deploy
    build2 = _make_build(db_session, project, build_id="lc-v2", image_ref=template_image)
    _confirm_and_execute(db_session, project, build2, manager)  # redeploy (supersede)
    stop_active_deployment(
        db_session, project=project, actor_id=project.owner_id, manager=manager
    )
    db_session.commit()

    after_containers = {c.id for c in docker_client.containers.list(all=True)}
    after_networks = {n.id for n in docker_client.networks.list()}

    redis_container = docker_client.containers.get(redis_container_name(project.id))
    try:
        assert after_containers - before_containers == {redis_container.id}
        assert after_networks - before_networks == set()
    finally:
        redis_container.remove(v=True, force=True)


# -- ADR 0023 §Amendment 1: data-service attach/detach bracket -----


def test_data_services_attach_on_deploy_and_detach_on_teardown(
    docker_client, db_session, project, template_image, manager
):
    """(e1, e2, e4) The full bracket, proven against a REAL Docker Engine:
    deploy -> both the shared Postgres and this project's OWN Redis are
    attached to the deployment's network; redeploy -> the OLD network is
    fully gone (a leaked attachment would make Docker refuse to remove it,
    so "the network was removed" IS the proof the detach happened) and the
    NEW one is attached instead; stop -> that network is gone too, and
    neither Postgres nor Redis is left attached to (i.e. never leaked onto)
    any deployment network at all."""
    from app.services.service_data import redis_container_name

    postgres = docker_client.containers.get(settings.service_postgres_container_name)
    redis_name = redis_container_name(project.id)

    build1 = _make_build(db_session, project, build_id="ds-v1", image_ref=template_image)
    deployment1 = _confirm_and_execute(db_session, project, build1, manager)
    try:
        deploy_container1 = docker_client.containers.get(f"forge-deploy-{deployment1.id}")
        network1_name = deploy_container1.labels["forge.network"]

        postgres.reload()
        redis = docker_client.containers.get(redis_name)
        assert network1_name in postgres.attrs["NetworkSettings"]["Networks"]
        assert network1_name in redis.attrs["NetworkSettings"]["Networks"]

        # Redeploy: the switchover removes the old container + its
        # network, attaching Postgres/Redis to the NEW one instead.
        build2 = _make_build(db_session, project, build_id="ds-v2", image_ref=template_image)
        deployment2 = _confirm_and_execute(db_session, project, build2, manager)
        deploy_container2 = docker_client.containers.get(f"forge-deploy-{deployment2.id}")
        network2_name = deploy_container2.labels["forge.network"]
        assert network2_name != network1_name

        with pytest.raises(NotFound):
            docker_client.networks.get(network1_name)  # old network actually removed

        postgres.reload()
        redis.reload()
        assert network2_name in postgres.attrs["NetworkSettings"]["Networks"]
        assert network2_name in redis.attrs["NetworkSettings"]["Networks"]
        assert network1_name not in postgres.attrs["NetworkSettings"]["Networks"]
        assert network1_name not in redis.attrs["NetworkSettings"]["Networks"]

        stop_active_deployment(
            db_session, project=project, actor_id=project.owner_id, manager=manager
        )
        db_session.commit()

        with pytest.raises(NotFound):
            docker_client.networks.get(network2_name)  # new network removed too

        postgres.reload()
        redis.reload()
        assert network2_name not in postgres.attrs["NetworkSettings"]["Networks"]
        assert network2_name not in redis.attrs["NetworkSettings"]["Networks"]
    finally:
        _cleanup_deployments(manager, db_session)
        try:
            docker_client.containers.get(redis_name).remove(v=True, force=True)
        except NotFound:
            pass


def test_data_services_attach_to_both_networks_during_a_concurrent_switchover(
    docker_client, db_session, project, template_image, manager
):
    """(e3) ADR 0023 §Amendment 1 says explicitly: both deployments are
    attached simultaneously during the health-gated switchover window, and
    that is correct, not an error. Driven directly at the DeployManager
    level (bypassing deploy.py's state machine, which removes the OLD
    container immediately after a successful flip and so never leaves two
    ACTIVE-shaped deployments observable at once) to hold BOTH networks
    attached at the same time and prove neither Postgres nor Redis rejects
    the second attachment."""
    from app.services.service_data import redis_container_name

    postgres = docker_client.containers.get(settings.service_postgres_container_name)
    redis_name = redis_container_name(project.id)
    # Provision the project's data services (normally ensure_service_env's
    # job) directly, so start_service has a Redis container to attach.
    from app.services.service_data import provision_project_data_services

    env = {
        "FORGE_SERVICE_ID": project.id,
        "FORGE_SERVICE_NAME": project.name,
        "FORGE_SERVICE_BASE_URL": "/unused",
        "FORGE_APP_SECRET_KEY": "unused",
    }
    data_services = provision_project_data_services(project.id, docker_client=docker_client)
    env.update(data_services)

    deployment_id_a = "concurrent-a"
    deployment_id_b = "concurrent-b"
    try:
        manager.start_service(
            project_id=project.id,
            deployment_id=deployment_id_a,
            build_id="bld-a",
            image_ref=template_image,
            env=env,
        )
        manager.start_service(
            project_id=project.id,
            deployment_id=deployment_id_b,
            build_id="bld-b",
            image_ref=template_image,
            env=env,
        )

        network_a = docker_client.containers.get(f"forge-deploy-{deployment_id_a}").labels[
            "forge.network"
        ]
        network_b = docker_client.containers.get(f"forge-deploy-{deployment_id_b}").labels[
            "forge.network"
        ]
        assert network_a != network_b

        postgres.reload()
        redis = docker_client.containers.get(redis_name)
        assert network_a in postgres.attrs["NetworkSettings"]["Networks"]
        assert network_b in postgres.attrs["NetworkSettings"]["Networks"]
        assert network_a in redis.attrs["NetworkSettings"]["Networks"]
        assert network_b in redis.attrs["NetworkSettings"]["Networks"]
    finally:
        manager.remove_service(deployment_id_a)
        manager.remove_service(deployment_id_b)
        try:
            docker_client.containers.get(redis_name).remove(v=True, force=True)
        except NotFound:
            pass


def test_migration_creates_table_before_first_request(
    docker_client, db_session, project, template_image, manager
):
    """Proves the ORDERING -- that `alembic upgrade
    head` (the template's Dockerfile CMD) ran, and created the `items`
    table, before uvicorn ever answered a request -- by observing the
    table exists at the very first request against it, not by reading the
    Dockerfile. If the migration had not run (or ran after, or against
    the wrong database), `GET /items` would 500 on an undefined-table
    error from Postgres instead of returning an empty list."""
    build = _make_build(db_session, project, build_id="dep-migration-order", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE

        status, body, content_type = manager.proxy(deployment.id, method="GET", path="/items")
        assert status == 200
        assert body == b"[]"
        # ADR 0028 (a4): the real proxy path forwards the upstream
        # Content-Type -- the template's JSON endpoints answer
        # application/json, not something this test invents.
        assert content_type is not None and content_type.startswith("application/json")

        # And the same endpoint's write half actually persists against
        # that same table (the migration's table, not some other one).
        status, body, _content_type = manager.proxy(
            deployment.id, method="POST", path="/items?value=caseaa-order-check"
        )
        assert status == 200
        assert b"caseaa-order-check" in body

        status, body, _content_type = manager.proxy(deployment.id, method="GET", path="/items")
        assert status == 200
        assert b"caseaa-order-check" in body
    finally:
        _cleanup_deployments(manager, db_session)


def test_redis_roundtrip_against_real_data_services(
    docker_client, db_session, project, template_image, manager
):
    """A Redis round-trip (write a key, read it
    back) against the REAL, per-project Redis container this module's
    `_real_data_services` fixture already provisions -- not a fake/stub
    Redis."""
    build = _make_build(db_session, project, build_id="dep-redis-roundtrip", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE

        status, body, _content_type = manager.proxy(deployment.id, method="POST", path="/cache-roundtrip")
        assert status == 200
        assert b'"roundtrip_ok":true' in body or b'"roundtrip_ok": true' in body
    finally:
        _cleanup_deployments(manager, db_session)


# -- ADR 0023 §Amendment 12: a project Redis is isolated, ------------
# -- authenticated, and re-ensured at every deployment start ------------------


def _redis_container(docker_client, project):
    from app.services.service_data import redis_container_name

    return docker_client.containers.get(redis_container_name(project.id))


def _remove_project_redis(docker_client, project) -> None:
    try:
        _redis_container(docker_client, project).remove(v=True, force=True)
    except (NotFound, DockerException):
        pass


def _start_preview_for(preview_manager, db, project, build):
    """The real `start_preview` path, with the Build row updated the way
    `sandbox_build_executor._start_preview` does it."""
    issued = preview_manager.start_preview(project_id=project.id, build=build)
    build.preview_url = issued.url
    build.preview_expires_at = issued.expires_at
    build.preview_token_hash = issued.token_hash
    db.commit()
    return issued


def _cache_roundtrip_ok(manager, deployment) -> bool:
    status, body, _content_type = manager.proxy(
        deployment.id, method="POST", path="/cache-roundtrip"
    )
    return status == 200 and b'"roundtrip_ok":true' in body.replace(b" ", b"")


def test_a_preview_of_a_later_build_does_not_remove_the_projects_production_redis(
    docker_client, db_session, project, template_image, manager
):
    """`stop_preview_for_project` (called
    by every `start_preview`) removed every container labelled with the
    project id that had no build id -- which is the project's production
    Redis. So the first successful modify build of a DEPLOYED project deleted
    its live Redis, and every later deploy or rollback failed PROVISION_FAILED.

    The order here is the one the old suite never exercised (the preview used
    to start BEFORE the first deploy): deploy first, then a preview of a later
    build of the same project through the real `start_preview`; then a
    redeploy of that build must still succeed against the same Redis."""
    from app.services.preview_manager import PreviewManager

    preview_manager = PreviewManager(client=docker_client)
    build1 = _make_build(db_session, project, build_id="p1-v1", image_ref=template_image)
    build2 = None
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        assert first.status == DeploymentStatus.ACTIVE
        redis_id = _redis_container(docker_client, project).id
        build2 = _make_build(db_session, project, build_id="p1-v2", image_ref=template_image)

        _start_preview_for(preview_manager, db_session, project, build2)

        assert docker_client.containers.get(f"forge-preview-{build2.id}").status == "running"
        survivor = _redis_container(docker_client, project)  # NotFound here = the old defect
        assert survivor.id == redis_id
        assert survivor.status == "running"

        second = _confirm_and_execute(db_session, project, build2, manager)
        assert second.status == DeploymentStatus.ACTIVE
        assert _redis_container(docker_client, project).id == redis_id
        assert _cache_roundtrip_ok(manager, second)
    finally:
        if build2 is not None:
            preview_manager.stop_preview(build2.id)
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_no_redis_is_ever_on_the_default_bridge_and_each_sits_only_on_its_own_networks(
    docker_client, db_session, project, template_image, manager
):
    """The default bridge has egress NAT
    and reaches every other container on it. After a deploy and after a
    preview start, neither Redis has `bridge`, and each has exactly the
    network the spec allows: the service Redis its project's active
    deployment network, the preview Redis its own preview's network -- never
    the other's. Restart policy asserted on both."""
    from app.services.preview_data import preview_redis_container_name
    from app.services.preview_manager import PreviewManager

    preview_manager = PreviewManager(client=docker_client)
    build1 = _make_build(db_session, project, build_id="br-v1", image_ref=template_image)
    build2 = None
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        deployment_network = docker_client.containers.get(f"forge-deploy-{first.id}").labels[
            "forge.network"
        ]

        service_redis = _redis_container(docker_client, project)
        assert set(service_redis.attrs["NetworkSettings"]["Networks"]) == {deployment_network}
        assert service_redis.attrs["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"

        build2 = _make_build(db_session, project, build_id="br-v2", image_ref=template_image)
        _start_preview_for(preview_manager, db_session, project, build2)
        preview_network = docker_client.containers.get(f"forge-preview-{build2.id}").labels[
            "forge.network"
        ]
        assert preview_network != deployment_network

        preview_redis = docker_client.containers.get(preview_redis_container_name(build2.id))
        assert set(preview_redis.attrs["NetworkSettings"]["Networks"]) == {preview_network}
        # Its consumer has no restart policy either, so neither does it.
        assert preview_redis.attrs["HostConfig"]["RestartPolicy"]["Name"] in ("", "no")

        # Nothing was handed to the wrong side: the preview did not pick up
        # the service Redis, and the service Redis did not pick up the preview.
        service_redis.reload()
        assert set(service_redis.attrs["NetworkSettings"]["Networks"]) == {deployment_network}
        for redis in (service_redis, preview_redis):
            assert "bridge" not in redis.attrs["NetworkSettings"]["Networks"]
    finally:
        if build2 is not None:
            preview_manager.stop_preview(build2.id)
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_a_stopped_redis_is_started_again_at_the_next_deployment_start(
    docker_client, db_session, project, template_image, manager
):
    """The stored env says a Redis exists; the container is
    stopped. The next deployment start must start it, not fail or run a
    service against a dead cache."""
    build1 = _make_build(db_session, project, build_id="st-v1", image_ref=template_image)
    try:
        _confirm_and_execute(db_session, project, build1, manager)
        redis = _redis_container(docker_client, project)
        redis_id = redis.id
        redis.stop(timeout=2)
        redis.reload()
        assert redis.status != "running"

        build2 = _make_build(db_session, project, build_id="st-v2", image_ref=template_image)
        second = _confirm_and_execute(db_session, project, build2, manager)

        assert second.status == DeploymentStatus.ACTIVE
        redis = _redis_container(docker_client, project)
        assert redis.id == redis_id
        assert redis.status == "running"
        assert _cache_roundtrip_ok(manager, second)
    finally:
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_a_missing_redis_is_recreated_at_the_next_deployment_start_with_the_stored_password(
    docker_client, db_session, project, template_image, manager
):
    """The demo's actual state: a deployed service whose
    Redis container is gone. Before, `ensure_service_env` saw both keys stored
    and returned, `_connect_data_services` raised NotFound, and every
    redeploy or rollback failed PROVISION_FAILED forever. Now the next
    deployment start recreates the Redis, with the password already in the
    stored `REDIS_URL` (a running service keeps a working credential)."""
    build1 = _make_build(db_session, project, build_id="ms-v1", image_ref=template_image)
    try:
        _confirm_and_execute(db_session, project, build1, manager)
        stored_url = decrypt_service_env(project.service_env_json)["REDIS_URL"]
        removed_id = _redis_container(docker_client, project).id
        _remove_project_redis(docker_client, project)
        with pytest.raises(NotFound):
            _redis_container(docker_client, project)

        build2 = _make_build(db_session, project, build_id="ms-v2", image_ref=template_image)
        second = _confirm_and_execute(db_session, project, build2, manager)

        assert second.status == DeploymentStatus.ACTIVE, second.failure_reason
        redis = _redis_container(docker_client, project)
        assert redis.id != removed_id
        assert redis.status == "running"
        db_session.refresh(project)
        assert decrypt_service_env(project.service_env_json)["REDIS_URL"] == stored_url
        assert _cache_roundtrip_ok(manager, second)
    finally:
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_a_rollback_also_re_ensures_a_missing_redis(
    docker_client, db_session, project, template_image, manager
):
    """Confirm and rollback alike: both go through
    `execute_deployment`, but this pins it for the path where nothing else
    re-provisions anything."""
    build1 = _make_build(db_session, project, build_id="rr-v1", image_ref=template_image)
    try:
        _confirm_and_execute(db_session, project, build1, manager)
        build2 = _make_build(db_session, project, build_id="rr-v2", image_ref=template_image)
        _confirm_and_execute(db_session, project, build2, manager)
        _remove_project_redis(docker_client, project)

        rollback = request_rollback(
            db_session,
            project=project,
            actor_id=project.owner_id,
            image_exists=manager.image_exists,
        )
        db_session.commit()
        claimed = claim_next_deployment(db_session)
        execute_deployment(
            db_session, claimed, manager=manager, git_provider=FakeMergeProvider()
        )
        db_session.refresh(rollback)

        assert rollback.status == DeploymentStatus.ACTIVE, rollback.failure_reason
        assert _redis_container(docker_client, project).status == "running"
        assert _cache_roundtrip_ok(manager, rollback)
    finally:
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_a_legacy_password_less_redis_url_is_upgraded_once_and_stored_encrypted(
    docker_client, db_session, project, template_image, manager
):
    """A service issued before this change holds a
    password-less `REDIS_URL` and a Redis container on the default bridge with
    no password. At the first deployment start the container is replaced by
    the hardened one, the stored env is updated through the same encrypted
    path with the new URL, and that new value is what the service gets --
    proven by the real service reaching its Redis with it."""
    import re

    from app.models.audit import AuditLog
    from app.services.service_data import redis_container_name
    from app.services.service_env_crypto import encrypt_service_env

    build1 = _make_build(db_session, project, build_id="lg-v1", image_ref=template_image)
    try:
        _confirm_and_execute(db_session, project, build1, manager)

        # Put the project into the old state.
        env_before = decrypt_service_env(project.service_env_json)
        legacy_url = f"redis://{redis_container_name(project.id)}:6379/0"
        env_before["REDIS_URL"] = legacy_url
        project.service_env_json = encrypt_service_env(env_before)
        db_session.commit()
        _remove_project_redis(docker_client, project)
        legacy = docker_client.containers.run(
            image=settings.service_redis_image,
            name=redis_container_name(project.id),
            command=["redis-server", "--save", ""],
            detach=True,
            labels={"forge.data_service": "redis", "forge.project_id": project.id},
        )
        legacy_id = legacy.id

        build2 = _make_build(db_session, project, build_id="lg-v2", image_ref=template_image)
        second = _confirm_and_execute(db_session, project, build2, manager)

        assert second.status == DeploymentStatus.ACTIVE, second.failure_reason
        db_session.refresh(project)
        env_after = decrypt_service_env(project.service_env_json)
        new_url = env_after["REDIS_URL"]
        match = re.fullmatch(
            rf"redis://:([A-Za-z0-9_-]+)@forge-service-redis-{project.id}:6379/0", new_url
        )
        assert match, f"the upgraded URL has no password: {new_url!r}"
        password = match.group(1)
        # Only REDIS_URL moved: every other issued value is what it was.
        assert {k: v for k, v in env_after.items() if k != "REDIS_URL"} == {
            k: v for k, v in env_before.items() if k != "REDIS_URL"
        }
        # Encrypted at rest, like every other value in the env.
        assert password not in project.service_env_json
        # The container is the hardened replacement, not the legacy one.
        redis = _redis_container(docker_client, project)
        assert redis.id != legacy_id
        assert f"--requirepass {password}" in " ".join(redis.attrs["Args"])
        # The service was started with the NEW value and can use it.
        deploy_env = dict(
            e.split("=", 1)
            for e in docker_client.containers.get(f"forge-deploy-{second.id}").attrs["Config"]["Env"]
            if "=" in e
        )
        assert deploy_env["REDIS_URL"] == new_url
        assert _cache_roundtrip_ok(manager, second)
        # Audit: the upgrade is recorded by key name; no value anywhere.
        rows = db_session.query(AuditLog).filter_by(action="deployment.service_env_upgraded").all()
        assert len(rows) == 1
        assert rows[0].metadata_json == {"keys": ["REDIS_URL"]}
        for row in db_session.query(AuditLog).all():
            assert password not in str(row.metadata_json or "")

        # "Once": the next deployment start finds a current URL and changes nothing.
        build3 = _make_build(db_session, project, build_id="lg-v3", image_ref=template_image)
        third = _confirm_and_execute(db_session, project, build3, manager)
        assert third.status == DeploymentStatus.ACTIVE, third.failure_reason
        db_session.refresh(project)
        assert decrypt_service_env(project.service_env_json)["REDIS_URL"] == new_url
        assert (
            db_session.query(AuditLog).filter_by(action="deployment.service_env_upgraded").count()
            == 1
        )
    finally:
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


# -- a failed deployment leaves no network and no container behind ------
#
# "Observed: two deployments that failed
# PROVISION_FAILED at the data-service attach step each left an empty
# forge-deploy-net-<id> network." Each test below drives a failure the real way
# and then asks Docker -- by the deployment id, the way `docker network ls
# --filter name=<id>` / `docker ps -a --filter name=<id>` would -- what is left.


CANNOT_START_IMAGE = "forge-deploy-test-cannot-start:latest"


@pytest.fixture(scope="module")
def cannot_start_image(docker_client):
    """An image whose container is created but can never be started (the
    entrypoint does not exist): `containers.run` creates it, then the start
    fails, which leaves a `Created` container behind unless something removes it."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(
            'FROM python:3.11-slim\nCMD ["/no/such/binary"]\n'
        )
        docker_client.images.build(path=tmp, tag=CANNOT_START_IMAGE, rm=True)
    yield CANNOT_START_IMAGE
    try:
        docker_client.images.get(CANNOT_START_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


def _networks_of(docker_client, deployment_id):
    return sorted(n.name for n in docker_client.networks.list(filters={"name": deployment_id}))


def _containers_of(docker_client, deployment_id):
    """Every container (any state, `Created` included) named for the deployment."""
    return sorted(
        c.name for c in docker_client.containers.list(all=True, filters={"name": deployment_id})
    )


def _sweep_leftovers(docker_client, deployment_id):
    """Whatever a (failing) test left for this deployment, so a red test does
    not also litter the host. Never the assertion -- that is made before."""
    for container in docker_client.containers.list(all=True, filters={"name": deployment_id}):
        try:
            container.remove(v=True, force=True)
        except (NotFound, DockerException):
            pass
    for network in docker_client.networks.list(filters={"name": deployment_id}):
        try:
            network.reload()  # `list` returns the summary, without the attached endpoints
        except (NotFound, DockerException):
            continue
        for container_id in list((network.attrs.get("Containers") or {}).keys()):
            try:
                network.disconnect(container_id, force=True)
            except (NotFound, DockerException):
                pass
        try:
            network.remove()
        except (NotFound, DockerException):
            pass


def test_a_provision_failed_at_the_data_service_attach_step_leaves_no_network(
    docker_client, db_session, project, template_image, manager, monkeypatch
):
    """The observed leak, end to end. The shared Postgres attaches to the new
    deployment network and this project's Redis does not exist, so the attach
    fails (`PROVISION_FAILED`) with Postgres still on the network -- which is
    what stopped Docker removing it."""
    import app.services.deploy as deploy_service

    # The env is issued without provisioning anything, so there is no Redis for
    # `_connect_data_services` to find after it has attached the Postgres.
    monkeypatch.setattr(
        deploy_service,
        "ensure_service_env",
        lambda db, project, docker_client=None: {"FORGE_SERVICE_ID": project.id},
    )
    build = _make_build(db_session, project, build_id="leak-attach", image_ref=template_image)
    deployment = None
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)

        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_reason == DeploymentFailureReason.PROVISION_FAILED
        assert _networks_of(docker_client, deployment.id) == []
        assert _containers_of(docker_client, deployment.id) == []
        postgres = docker_client.containers.get(settings.service_postgres_container_name)
        postgres.reload()
        assert not [
            name for name in postgres.attrs["NetworkSettings"]["Networks"] if deployment.id in name
        ], "the shared Postgres is still attached to the failed deployment's network"
    finally:
        if deployment is not None:
            _sweep_leftovers(docker_client, deployment.id)
        _cleanup_deployments(manager, db_session)


def test_a_health_timeout_leaves_no_network_and_no_container(
    docker_client, db_session, project, never_healthy_image, manager, monkeypatch
):
    monkeypatch.setattr(settings, "deploy_health_check_timeout_seconds", 4.0)
    build = _make_build(db_session, project, build_id="leak-health", image_ref=never_healthy_image)
    deployment = None
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)

        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
        assert _networks_of(docker_client, deployment.id) == []
        assert _containers_of(docker_client, deployment.id) == []
    finally:
        if deployment is not None:
            _sweep_leftovers(docker_client, deployment.id)
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_a_start_failure_leaves_no_created_container_and_no_network(
    docker_client, db_session, project, cannot_start_image, manager
):
    """`containers.run` creates the container and then fails to start it. The
    `Created` container is what used to stay behind (and, being attached to the
    deployment network, kept the network from being removed too)."""
    build = _make_build(db_session, project, build_id="leak-start", image_ref=cannot_start_image)
    deployment = None
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)

        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_reason == DeploymentFailureReason.START_FAILED
        assert _containers_of(docker_client, deployment.id) == []
        assert _networks_of(docker_client, deployment.id) == []
    finally:
        if deployment is not None:
            _sweep_leftovers(docker_client, deployment.id)
        _cleanup_deployments(manager, db_session)
        _remove_project_redis(docker_client, project)


def test_remove_service_empties_and_removes_a_network_that_has_no_container(
    docker_client, manager
):
    """A network found by its `forge.deployment_id` label alone (no container
    carries the `forge.network` label that is the other way to it), with the
    shared Postgres still attached -- the shape a failed attach used to leave."""
    deployment_id = f"casead-orphan-{uuid.uuid4().hex[:8]}"
    postgres = docker_client.containers.get(settings.service_postgres_container_name)
    network = docker_client.networks.create(
        f"{settings.deploy_network_name}-{deployment_id}-aaaa",
        driver="bridge",
        internal=True,
        labels={"forge.deploy": "true", "forge.deployment_id": deployment_id},
    )
    try:
        network.connect(postgres)
        assert _networks_of(docker_client, deployment_id) == [network.name]

        assert manager.remove_service(deployment_id) is True

        assert _networks_of(docker_client, deployment_id) == []
        postgres.reload()
        assert network.name not in postgres.attrs["NetworkSettings"]["Networks"]
    finally:
        _sweep_leftovers(docker_client, deployment_id)


def test_a_post_with_a_json_body_and_a_query_reaches_the_service_through_the_route(
    docker_client, db_session, project, template_image, manager, client
):
    """Against a real container: the platform-origin service proxy forwards
    a POST's query string and its allowlisted `Content-Type` + body, and the answer comes back.
    The template's `POST /items?value=` takes its value from the query, so the query is what proves
    delivery here; the body rides along (the template ignores it) and must not break the request."""
    build = _make_build(db_session, project, build_id="dep-caseap-route", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE

        created = client.post(
            f"/api/projects/{project.id}/service/items?value=caseap-through-route",
            json={"ignored": "by the template"},
        )
        assert created.status_code == 200, created.text
        assert "caseap-through-route" in created.text
        assert created.headers["content-type"].startswith("application/json")
        assert created.headers["x-content-type-options"] == "nosniff"

        listed = client.get(f"/api/projects/{project.id}/service/items")
        assert listed.status_code == 200
        assert "caseap-through-route" in listed.text

        # a query over the limit never reaches the container
        too_long = client.get(f"/api/projects/{project.id}/service/items?a=" + "x" * 9000)
        assert too_long.status_code == 414
        assert too_long.headers["x-content-type-options"] == "nosniff"
    finally:
        _cleanup_deployments(manager, db_session)
