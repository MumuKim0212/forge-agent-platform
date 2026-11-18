"""Integration tests for the TTL preview container lifecycle
(`services/preview_manager.py`) against a real Docker Engine.

Builds a tiny real HTTP-serving image (python:3.11-slim + http.server on
the preview port) once per module and exercises: start from a real image,
hardening posture (no published ports, internal network, caps, dropped
capabilities), signed-URL accept/reject through the real API route,
replacement (at most one live preview per project), the worker's expiry
sweep, and no orphaned containers/networks after teardown.

Skips cleanly (not fails) when Docker is unavailable.

ADR 0023 §Amendment 2: `start_preview` now provisions a REAL
per-preview Postgres database/role + Redis container on every call in
this module -- the `_real_data_services` autouse fixture below points
`settings.service_postgres_admin_dsn` / `service_postgres_container_name`
at the module's own throwaway Postgres (`tests/conftest.py`'s
`real_service_postgres_admin_dsn`), the same pattern
`test_deploy_integration.py` already uses, so the suite's outcome does
not depend on whether the developer's own `backend/.env` happens to hold
`SERVICE_POSTGRES_ADMIN_DSN` (a standing rule).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException, ImageNotFound, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from tests.fake_admin_dsn import FAKE_ADMIN_DSN  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.services.preview_data import (  # noqa: E402
    _preview_identifier,
    preview_redis_container_name,
)
from app.services.preview_manager import (  # noqa: E402
    PreviewManager,
    PreviewStartError,
    hash_token,
    issue_preview_token,
)
from app.services.status import BuildStatus, ProjectStatus  # noqa: E402

PREVIEW_TEST_IMAGE = "forge-preview-test:latest"


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
    """every `start_preview` call in this module now provisions
    real per-preview data services -- point it at the module's own
    throwaway Postgres so the existing tests keep exercising a REAL
    provision -> attach -> drop cycle rather than silently depending on
    whatever `backend/.env` happens to hold."""
    yield


def _preview_role_or_database_exists(name: str) -> bool:
    """Reads `pg_database`/`pg_roles` directly off the module's throwaway
    admin DSN -- used to prove a preview's Postgres database/role are
    genuinely gone after teardown (b1, b4), not merely that the container
    is gone."""
    conn = psycopg.connect(settings.service_postgres_admin_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is not None:
                return True
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


@pytest.fixture(scope="module")
def preview_image(docker_client):
    """A real, tiny image whose default CMD serves HTTP on the preview
    port — what a real built service image would look like from the
    preview container's perspective."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "WORKDIR /srv\n"
            "RUN echo forge-preview-test-page > index.html\n"
            f'CMD ["python", "-m", "http.server", "{settings.preview_container_port}"]\n'
        )
        docker_client.images.build(path=tmp, tag=PREVIEW_TEST_IMAGE, rm=True)
    yield PREVIEW_TEST_IMAGE
    try:
        docker_client.images.get(PREVIEW_TEST_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture()
def manager(docker_client):
    return PreviewManager(client=docker_client)


def _fake_build(build_id: str, image_ref: str):
    return SimpleNamespace(id=build_id, image_ref=image_ref)


def _cleanup(manager: PreviewManager, *build_ids: str) -> None:
    for build_id in build_ids:
        manager.stop_preview(build_id)


def test_start_preview_runs_hardened_container(docker_client, preview_image, manager):
    build = _fake_build("pv-hardened", preview_image)
    try:
        issued = manager.start_preview(project_id="proj-hardened", build=build)

        container = docker_client.containers.get("forge-preview-pv-hardened")
        container.reload()
        assert container.status == "running"

        attrs = container.attrs
        host_config = attrs["HostConfig"]
        # No published host ports at all (routing is exec-based; nothing on
        # the host network can reach the container directly).
        assert not host_config.get("PortBindings")
        assert not attrs["NetworkSettings"].get("Ports") or all(
            v is None for v in attrs["NetworkSettings"]["Ports"].values()
        )
        # Same hardening posture as build sandboxes (ADR 0003 invariants 2-4).
        assert host_config["Privileged"] is False
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["Memory"] == settings.sandbox_memory_limit_mb * 1024 * 1024
        assert host_config["NanoCpus"] == int(settings.sandbox_cpu_limit_cores * 1_000_000_000)
        assert host_config["PidsLimit"] == settings.sandbox_pids_limit
        # Docker socket not mounted.
        assert not host_config.get("Binds")
        # Its (only) network is internal=True — default-deny egress.
        network_names = list(attrs["NetworkSettings"]["Networks"].keys())
        assert len(network_names) == 1
        network = docker_client.networks.get(network_names[0])
        assert network.attrs["Internal"] is True

        # Signed URL shape + expiry within TTL.
        assert issued.url.startswith("/api/projects/proj-hardened/preview/pv-hardened?token=")
        assert issued.expires_at > datetime.now(timezone.utc)
        assert issued.token_hash == hash_token(issued.token)
    finally:
        _cleanup(manager, build.id)


def test_preview_container_receives_no_data_service_env(docker_client, preview_image, manager):
    """ADR 0023 §Amendment 2 regression pin, now inverted:
    a preview used to receive none of these six keys at all. It now
    has all six -- but they are its OWN preview-scoped, throwaway values,
    never the SERVICE's (that stays pinned separately, by
    `test_preview_database_url_differs_from_the_services_own` below,
    which provisions a real service env and a real preview for the same
    project and diffs the two `DATABASE_URL`s). Asserted against the REAL
    container's actual environment, so this fails the moment
    `start_preview`'s `environment=` argument ever stops carrying one of
    these keys."""
    build = _fake_build("pv-no-data-services", preview_image)
    try:
        manager.start_preview(project_id="proj-no-data-services", build=build)

        container = docker_client.containers.get("forge-preview-pv-no-data-services")
        env = dict(e.split("=", 1) for e in container.attrs["Config"]["Env"] if "=" in e)
        for service_env_key in (
            "DATABASE_URL",
            "REDIS_URL",
            "FORGE_SERVICE_ID",
            "FORGE_SERVICE_NAME",
            "FORGE_SERVICE_BASE_URL",
            "FORGE_APP_SECRET_KEY",
        ):
            assert service_env_key in env and env[service_env_key]
        assert env["FORGE_SERVICE_ID"] == "proj-no-data-services"
        assert env["DATABASE_URL"].startswith("postgresql://")
        assert env["REDIS_URL"].startswith("redis://")
    finally:
        _cleanup(manager, build.id)


def test_preview_database_url_differs_from_the_services_own(
    docker_client, preview_image, manager
):
    """(d2): a preview never gets the SERVICE's own `DATABASE_URL`, even
    once the service itself has real data services provisioned.
    Provisions the project's OWN data services first
    (`service_data.provision_project_data_services` -- the module this
    task must never touch the drop side of), then starts a preview for a
    build of that SAME project, and asserts the two URLs differ in
    database name, role, AND password -- not merely "not byte-identical",
    which a shared database with only a different password would still
    satisfy."""
    from sqlalchemy.engine import make_url

    from app.services.service_data import provision_project_data_services

    project_id = "proj-own-data-vs-preview"
    service_env = provision_project_data_services(project_id, docker_client=docker_client)
    service_url = make_url(service_env["DATABASE_URL"])

    build = _fake_build("pv-own-data-vs-preview", preview_image)
    try:
        manager.start_preview(project_id=project_id, build=build)
        container = docker_client.containers.get("forge-preview-pv-own-data-vs-preview")
        env = dict(e.split("=", 1) for e in container.attrs["Config"]["Env"] if "=" in e)
        preview_url = make_url(env["DATABASE_URL"])

        assert preview_url.database != service_url.database
        assert preview_url.username != service_url.username
        assert preview_url.password != service_url.password
    finally:
        _cleanup(manager, build.id)


def test_default_deny_egress_from_preview(docker_client, preview_image, manager):
    build = _fake_build("pv-egress", preview_image)
    try:
        manager.start_preview(project_id="proj-egress", build=build)
        container = docker_client.containers.get("forge-preview-pv-egress")
        exit_code, output = container.exec_run(
            [
                "python",
                "-c",
                "import socket; s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 443))",
            ]
        )
        assert exit_code != 0, f"egress unexpectedly allowed: {output!r}"
    finally:
        _cleanup(manager, build.id)


def test_proxy_fetches_page_from_preview(preview_image, manager):
    build = _fake_build("pv-proxy", preview_image)
    try:
        manager.start_preview(project_id="proj-proxy", build=build)
        status, body, content_type = manager.proxy(build.id, path="/index.html")
        assert status == 200
        assert b"forge-preview-test-page" in body
        # ADR 0028 (a4): the real proxy path forwards the upstream
        # Content-Type -- `python -m http.server` labels a `.html` file
        # `text/html`, not something this test invents.
        assert content_type is not None and content_type.startswith("text/html")
    finally:
        _cleanup(manager, build.id)


def test_newer_preview_replaces_older_for_same_project(docker_client, preview_image, manager):
    """ADR 0016: at most one live preview per project — a newer successful
    build's preview tears the previous one (container + network) down.
    The "newer build" teardown trigger: the OLD build's own
    Postgres database/role and Redis container are dropped too, pinned
    separately from the other two triggers (TTL expiry, promotion), because
    subsumed branches are invisible."""
    old_build = _fake_build("pv-old", preview_image)
    new_build = _fake_build("pv-new", preview_image)
    try:
        manager.start_preview(project_id="proj-replace", build=old_build)
        old_container = docker_client.containers.get("forge-preview-pv-old")
        old_network = old_container.labels["forge.network"]

        manager.start_preview(project_id="proj-replace", build=new_build)

        with pytest.raises(NotFound):
            docker_client.containers.get("forge-preview-pv-old")
        with pytest.raises(NotFound):
            docker_client.networks.get(old_network)
        assert docker_client.containers.get("forge-preview-pv-new").status == "running"

        assert _preview_role_or_database_exists(_preview_identifier(old_build.id)) is False
        with pytest.raises(NotFound):
            docker_client.containers.get(preview_redis_container_name(old_build.id))
    finally:
        _cleanup(manager, old_build.id, new_build.id)


def test_signed_url_accept_and_reject_through_api(
    docker_client, preview_image, manager, client, db_session, seeded_ids
):
    """The real route (`GET /api/projects/{pid}/preview/{bid}?token=...`):
    valid token -> the previewed page; missing/invalid/expired/superseded
    token -> 403; no identity header needed (the signed token is the access
    control, ADR 0016)."""
    project = Project(
        name="preview-api-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(
        id="pv-api", project_id=project.id, type="create", status=BuildStatus.SUCCEEDED,
        image_ref=preview_image,
    )
    db_session.add(build)
    db_session.commit()

    try:
        issued = manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()

        base = f"/api/projects/{project.id}/preview/{build.id}"

        # Valid token (no X-Forge-User-Id header on purpose).
        resp = client.get(f"{base}/index.html?token={issued.token}")
        assert resp.status_code == 200
        assert "forge-preview-test-page" in resp.text
        # ADR 0028 (a4): the real proxy path, not a stub -- all three
        # isolation headers, with the upstream Content-Type genuinely
        # forwarded (`python -m http.server` labels `index.html` text/html).
        assert resp.headers["content-security-policy"] == (
            "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        )
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-type"].startswith("text/html")

        # Missing token -- the route's own 403 carries the same headers too.
        missing_resp = client.get(base)
        assert missing_resp.status_code == 403
        assert missing_resp.headers["content-security-policy"] == (
            "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
        )
        assert missing_resp.headers["x-content-type-options"] == "nosniff"
        # Garbage token.
        assert client.get(f"{base}?token=garbage").status_code == 403
        # Structurally valid but expired token.
        expired_token, _ = issue_preview_token(build.id, ttl_minutes=-1)
        build.preview_token_hash = hash_token(expired_token)
        db_session.commit()
        assert client.get(f"{base}?token={expired_token}").status_code == 403
        # Wrong build id in the path.
        other_build = Build(
            id="pv-api-other", project_id=project.id, type="create",
            status=BuildStatus.SUCCEEDED, preview_token_hash=issued.token_hash,
        )
        db_session.add(other_build)
        db_session.commit()
        assert (
            client.get(
                f"/api/projects/{project.id}/preview/{other_build.id}?token={issued.token}"
            ).status_code
            == 403
        )
    finally:
        _cleanup(manager, build.id)


def test_expiry_sweep_tears_down_and_clears_fields(
    docker_client, preview_image, manager, db_session, seeded_ids
):
    """The worker sweep (worker.sweep_expired_previews): an expired preview's
    container + network are removed and the build's preview columns cleared."""
    from app.worker import sweep_expired_previews

    project = Project(
        name="preview-sweep-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(
        id="pv-sweep", project_id=project.id, type="create", status=BuildStatus.SUCCEEDED,
        image_ref=preview_image,
    )
    db_session.add(build)
    db_session.commit()

    try:
        issued = manager.start_preview(project_id=project.id, build=build)
        container = docker_client.containers.get("forge-preview-pv-sweep")
        network_name = container.labels["forge.network"]

        # Not yet expired -> sweep is a no-op.
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()
        assert sweep_expired_previews(db_session, preview_manager=manager) == 0
        assert docker_client.containers.get("forge-preview-pv-sweep").status == "running"

        # Force-expire, then sweep.
        build.preview_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()
        assert sweep_expired_previews(db_session, preview_manager=manager) == 1

        with pytest.raises(NotFound):
            docker_client.containers.get("forge-preview-pv-sweep")
        with pytest.raises(NotFound):
            docker_client.networks.get(network_name)
        db_session.refresh(build)
        assert build.preview_url is None
        assert build.preview_expires_at is None
        assert build.preview_token_hash is None

        # The "TTL expiry" teardown trigger, pinned separately from
        # the "newer build" and "promotion" triggers): the swept build's
        # own Postgres database/role and Redis container are gone too.
        assert _preview_role_or_database_exists(_preview_identifier(build.id)) is False
        with pytest.raises(NotFound):
            docker_client.containers.get(preview_redis_container_name(build.id))
    finally:
        _cleanup(manager, build.id)


def test_no_orphaned_resources_after_lifecycle(docker_client, preview_image, manager):
    before_containers = {c.id for c in docker_client.containers.list(all=True)}
    before_networks = {n.id for n in docker_client.networks.list()}

    build = _fake_build("pv-orphan", preview_image)
    manager.start_preview(project_id="proj-orphan", build=build)
    assert manager.stop_preview(build.id) is True

    after_containers = {c.id for c in docker_client.containers.list(all=True)}
    after_networks = {n.id for n in docker_client.networks.list()}
    assert after_containers - before_containers == set()
    assert after_networks - before_networks == set()


def test_stop_preview_drops_data_services_even_when_the_container_is_already_gone(
    docker_client, preview_image, manager
):
    """(b1), decision (a): the container-not-found early return `stop_
    preview`'s docstring used to describe was exactly the leaking case --
    the container gone and the database not. Removes the preview's own
    APP container OUT OF BAND (simulating an operator/crash cleanup that
    never went through this module), then calls `stop_preview`, and
    asserts the preview's database, role, AND Redis container are all
    gone anyway."""
    build = _fake_build("pv-container-gone", preview_image)
    manager.start_preview(project_id="proj-container-gone", build=build)
    container = docker_client.containers.get("forge-preview-pv-container-gone")
    network_name = container.labels["forge.network"]
    container.remove(v=True, force=True)  # out-of-band removal this module never drove

    try:
        assert manager.stop_preview(build.id) is False  # nothing left to remove -- container already gone
        assert _preview_role_or_database_exists(_preview_identifier(build.id)) is False
        with pytest.raises(NotFound):
            docker_client.containers.get(preview_redis_container_name(build.id))
    finally:
        # The network's name carries a random suffix known only via the
        # container's OWN label (module docstring) -- once the container
        # is gone out of band, nothing in this module can rediscover the
        # network either (a pre-existing, accepted gap distinct from what
        # is guaranteed: only the DATA is ever promised to be dropped in
        # this scenario). Clean it up here so this test does not itself
        # leave a leaked network for the suite's own check (e3).
        try:
            network = docker_client.networks.get(network_name)
        except (NotFound, DockerException):
            network = None
        if network is not None:
            try:
                network.disconnect(settings.service_postgres_container_name)
            except (NotFound, DockerException):
                pass
            try:
                network.remove()
            except DockerException:
                pass


def test_start_preview_without_image_ref_raises(manager):
    build = _fake_build("pv-no-image", image_ref=None)
    with pytest.raises(PreviewStartError, match="no image_ref"):
        manager.start_preview(project_id="proj-no-image", build=build)


def test_stop_preview_is_idempotent(manager):
    assert manager.stop_preview("pv-never-existed") is False


# -- a preview start that raises leaves nothing of that attempt behind ---------------------
#
# The same guarantee `DeployManager.start_service` has, for the preview path. Each test forces
# the failure at a different point between "the network exists" and "the preview is live", and then
# asserts ALL FOUR resources of the attempt are gone -- the container, the network, the preview's
# Postgres database and role, and its Redis container -- and that the shared Postgres (which the attach
# step had joined to the network) is still running and detached.


def _attempt_leftovers(docker_client, build_id: str) -> dict:
    """What of one preview attempt exists right now."""
    containers = [c.name for c in docker_client.containers.list(all=True) if build_id in c.name]
    networks = [n.name for n in docker_client.networks.list() if build_id in n.name]
    return {
        "containers": containers,  # forge-preview-<id> and forge-preview-redis-<id>
        "networks": networks,
        "postgres": _preview_role_or_database_exists(_preview_identifier(build_id)),
    }


NOTHING_LEFT = {"containers": [], "networks": [], "postgres": False}


def _shared_postgres_is_up_and_attached_to_nothing_of_ours(docker_client, build_id: str) -> bool:
    postgres = docker_client.containers.get(settings.service_postgres_container_name)
    postgres.reload()
    attached = list(postgres.attrs["NetworkSettings"]["Networks"])
    return postgres.status == "running" and not [n for n in attached if build_id in n]


def test_a_failure_at_the_second_data_store_attach_leaves_nothing_behind(
    docker_client, preview_image, manager, monkeypatch
):
    """THE leak: the attach joins the shared Postgres to the preview's network, then fails on the
    Redis (here: removed out from under it right after provisioning). The old code removed the network
    without detaching the Postgres, Docker refused, the refusal was swallowed, and the network stayed."""
    from app.services import preview_data as preview_data_module

    build_id = "pv-caseae-attach"
    real_provision = preview_data_module.provision_preview_data_services

    def provision_then_lose_the_redis(bid, *, docker_client=None):
        issued = real_provision(bid, docker_client=docker_client)
        docker_client.containers.get(preview_redis_container_name(bid)).remove(v=True, force=True)
        return issued

    monkeypatch.setattr(preview_data_module, "provision_preview_data_services", provision_then_lose_the_redis)

    try:
        with pytest.raises(PreviewStartError, match="failed to attach data services"):
            manager.start_preview(project_id="proj-caseae-attach", build=_fake_build(build_id, preview_image))

        assert _attempt_leftovers(docker_client, build_id) == NOTHING_LEFT
        assert _shared_postgres_is_up_and_attached_to_nothing_of_ours(docker_client, build_id)
    finally:
        _cleanup(manager, build_id)


def test_a_failure_after_the_container_started_leaves_nothing_behind(
    docker_client, preview_image, manager, monkeypatch
):
    """The old code cleaned up the failures it knew of. This one (the token signer raising, as it does
    for a missing signing secret) comes AFTER the container is running and the data services exist: it left
    the container, the database, the Redis and the network."""
    from app.services import preview_manager as preview_manager_module

    build_id = "pv-caseae-late"

    def refuse(*args, **kwargs):
        raise RuntimeError("signing is unavailable")

    monkeypatch.setattr(preview_manager_module, "issue_preview_token", refuse)

    try:
        with pytest.raises(RuntimeError, match="signing is unavailable"):  # the original, unchanged
            manager.start_preview(project_id="proj-caseae-late", build=_fake_build(build_id, preview_image))

        assert _attempt_leftovers(docker_client, build_id) == NOTHING_LEFT
        assert _shared_postgres_is_up_and_attached_to_nothing_of_ours(docker_client, build_id)
    finally:
        _cleanup(manager, build_id)


def test_a_container_that_cannot_be_started_leaves_nothing_behind(docker_client, manager):
    build_id = "pv-caseae-noimage"

    try:
        with pytest.raises(PreviewStartError, match="preview container failed to start"):
            manager.start_preview(
                project_id="proj-caseae-noimage",
                build=_fake_build(build_id, "forge-preview-image-that-does-not-exist:latest"),
            )

        assert _attempt_leftovers(docker_client, build_id) == NOTHING_LEFT
        assert _shared_postgres_is_up_and_attached_to_nothing_of_ours(docker_client, build_id)
    finally:
        _cleanup(manager, build_id)


def test_a_data_service_provisioning_failure_leaves_nothing_behind(
    docker_client, preview_image, manager, monkeypatch
):
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    build_id = "pv-caseae-provision"

    with pytest.raises(PreviewStartError, match="data services provisioning failed"):
        manager.start_preview(project_id="proj-caseae-provision", build=_fake_build(build_id, preview_image))
    monkeypatch.undo()  # the module's real admin DSN again, so the Postgres half can be asked about

    try:
        assert _attempt_leftovers(docker_client, build_id) == NOTHING_LEFT
    finally:
        _cleanup(manager, build_id)


ECHO_APP = """
import base64, http.server, json


class Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        payload = json.dumps(
            {
                "method": self.command,
                "target": self.path,
                "content_type": self.headers.get("Content-Type"),
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "body_b64": base64.b64encode(body).decode(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer


http.server.HTTPServer(("0.0.0.0", __PORT__), Handler).serve_forever()
"""

ECHO_IMAGE = "forge-preview-echo-test:latest"


@pytest.fixture(scope="module")
def echo_image(docker_client):
    """a test-local app that answers every request with what it received (the generic
    template is not touched). The preview image above is `http.server`, which refuses a POST."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "echo.py").write_text(ECHO_APP.replace("__PORT__", str(settings.preview_container_port)))
        Path(tmp, "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "WORKDIR /srv\n"
            "COPY echo.py .\n"
            'CMD ["python", "echo.py"]\n'
        )
        docker_client.images.build(path=tmp, tag=ECHO_IMAGE, rm=True)
    yield ECHO_IMAGE
    try:
        docker_client.images.get(ECHO_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


def test_a_post_with_a_json_body_and_a_query_reaches_the_preview_through_the_route(
    docker_client, echo_image, manager, client, db_session, seeded_ids
):
    """Against a real container: the preview proxy forwards the query (with
    the `token` parameter removed), the body and the allowlisted `Content-Type`, and no other header."""
    import json

    project = Project(
        name="preview-caseap-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(
        id="pv-caseap", project_id=project.id, type="create", status=BuildStatus.SUCCEEDED,
        image_ref=echo_image,
    )
    db_session.add(build)
    db_session.commit()

    try:
        issued = manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()

        resp = client.post(
            f"/api/projects/{project.id}/preview/{build.id}/items?a=1&token={issued.token}&b=2",
            content=b'{"name": "n"}',
            headers={
                "Content-Type": "application/json",
                "Cookie": "forge_session=SECRET-COOKIE",
                "Authorization": "Bearer SECRET-AUTH",
            },
        )
        assert resp.status_code == 200, resp.text
        echoed = json.loads(resp.content)
        assert echoed["method"] == "POST"
        assert echoed["target"] == "/items?a=1&b=2"  # no token
        assert echoed["content_type"] == "application/json"
        assert echoed["cookie"] is None and echoed["authorization"] is None
        import base64

        assert base64.b64decode(echoed["body_b64"]) == b'{"name": "n"}'
        assert issued.token not in resp.text
        assert resp.headers["x-content-type-options"] == "nosniff"
    finally:
        _cleanup(manager, build.id)


def test_a_post_through_the_token_path_form_reaches_the_container_without_the_token_segment(
    docker_client, echo_image, manager, client, db_session, seeded_ids
):
    """Against a real container: a JSON `POST` addressed with the `~{token}`
    path segment (how a sandboxed page's relative URLs resolve) is verified, the segment is removed, and
    the container receives the body and the path WITHOUT it."""
    import base64
    import json

    project = Project(
        name="preview-caseaq-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(
        id="pv-caseaq", project_id=project.id, type="create", status=BuildStatus.SUCCEEDED,
        image_ref=echo_image,
    )
    db_session.add(build)
    db_session.commit()

    try:
        issued = manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()

        resp = client.post(
            f"/api/projects/{project.id}/preview/{build.id}/~{issued.token}/items?a=1",
            content=b'{"name": "n"}',
            headers={"Content-Type": "application/json", "Origin": "null", "Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200, resp.text
        echoed = json.loads(resp.content)
        assert echoed["method"] == "POST"
        assert echoed["target"] == "/items?a=1"  # no `~token` segment, and no token anywhere
        assert issued.token not in resp.text
        assert base64.b64decode(echoed["body_b64"]) == b'{"name": "n"}'
        assert resp.headers["access-control-allow-origin"] == "null"
        assert "access-control-allow-credentials" not in resp.headers

        # and a `~` segment that does not verify never reaches the container
        refused = client.post(
            f"/api/projects/{project.id}/preview/{build.id}/~not-the-token/items",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert refused.status_code == 403
    finally:
        _cleanup(manager, build.id)
