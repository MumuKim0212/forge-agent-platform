"""Real-Traefik integration tests (prod
shape): runs an actual Traefik container against the real Docker Engine and
verifies host-header routing to an activated deployment, that a pre-health /
failed container is never reachable through Traefik, route removal on
stop/supersede, preview forward-auth accept/reject through the REAL platform
verification endpoint, preview-teardown route removal, and per-service
isolation (services cannot reach each other or the platform).

Skips cleanly (not fails) when Docker is unavailable.

Mechanism under test (per-service networks — the task's alternative flavor,
chosen over the suggested shared-network flavor for empirically-observed
reasons documented in deploy_manager.py's module docstring): each service
container stays on its own `internal=True` network and carries its Traefik
labels from creation; the route flip is connecting the TRAEFIK container
into that network after the health gate. Router priorities strictly decrease
per deployment so the old deployment deterministically serves during the
dual-router window.

Uses `traefik:v3.6` deliberately: earlier Traefik v3.x releases pin Docker
API version 1.24 in their Docker-provider client, which recent Docker
Engines (min supported API >= 1.40) reject outright — a known upstream bug
(traefik/traefik#12253, fixed in v3.6.1+). Reproduced directly against this
engine: `traefik:v3.1`/`v3.3`'s provider dies retrying
`Error response from daemon: ""` and never sees any labeled container.

The platform's forward-auth endpoint (`routers/preview_auth.py`) is served
for real: a uvicorn thread runs the actual FastAPI app (dependency-
overridden onto each test's DB session) on 0.0.0.0, reachable from the
Traefik container via `host.docker.internal` — the same topology as the
config default (`traefik_forward_auth_host`). That name is NOT provided by a
plain Linux Docker engine (only Docker Desktop injects it), so the
`traefik_container` fixture maps it to `host-gateway` itself, exactly as an
operator-provisioned Traefik on Linux must.
"""

from __future__ import annotations

import logging
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
import uvicorn  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402
from alembic import command  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as app_main  # noqa: E402  (module, not `from ... import app`: the
# lifespan patch below reassigns `app_main.engine`/`app_main.SessionLocal`, the
# exact names `app.main.lifespan` closes over via `from app.db import
# SessionLocal, engine` — see `backend_server_port`'s docstring)
from app.config import settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.approval import ApprovalRequest, ApprovalStatus  # noqa: E402
from app.models.build import Build  # noqa: E402
from app.models.deployment import (  # noqa: E402
    Deployment,
    DeploymentFailureReason,
    DeploymentStatus,
)
from app.models.project import Project  # noqa: E402
from app.schema_check import alembic_config  # noqa: E402
from app.services.deploy import (  # noqa: E402
    claim_next_deployment,
    confirm_deploy,
    execute_deployment,
    stop_active_deployment,
)
from app.services.deploy_manager import DeployManager  # noqa: E402
from app.services.preview_manager import PreviewManager  # noqa: E402
from app.services.status import BuildStatus, ProjectStatus  # noqa: E402
from app.services.traefik_traffic import (  # noqa: E402
    METRICS_READ_CAP_BYTES,
    TrafficSignalPoller,
    docker_exec_read,
    listens_beyond_loopback,
)
from tests.test_deploy_service import FakeMergeProvider  # noqa: E402

TRAEFIK_IMAGE = "traefik:v3.6"
# the loopback-only metrics entrypoint every Traefik here starts with.
METRICS_PORT = 8082
METRICS_FLAGS = [
    f"--entrypoints.forgemetrics.address=127.0.0.1:{METRICS_PORT}",
    "--metrics.prometheus=true",
    "--metrics.prometheus.entryPoint=forgemetrics",
]
TEMPLATE_APP_IMAGE = "forge-traefik-test-app:latest"
NO_HEALTHZ_IMAGE = "forge-traefik-test-nohealthz:latest"

# The uvicorn thread (module-scoped) resolves the CURRENT test's DB session
# through this holder — set/cleared per test by `bind_backend_db`.
_current_db_session = None


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


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="module", autouse=True)
def _real_data_services(real_service_postgres_admin_dsn):
    """every `confirm_deploy` call in this module now requires a
    configured admin DSN (PROVISION_UNCONFIGURED) -- point
    it at the module's real throwaway Postgres so the existing Traefik
    happy-path tests keep exercising a REAL confirm -> execute -> ACTIVE
    flow, now provisioning real per-project Postgres roles/databases and
    Redis containers along the way."""
    yield


@pytest.fixture(scope="module")
def traefik_image(docker_client):
    try:
        docker_client.images.get(TRAEFIK_IMAGE)
    except ImageNotFound:
        try:
            docker_client.images.pull(TRAEFIK_IMAGE)
        except DockerException:
            pytest.skip(f"{TRAEFIK_IMAGE} not available locally and could not be pulled")
    return TRAEFIK_IMAGE


@pytest.fixture(scope="module")
def template_image(docker_client):
    """The real generic-webtool template image (same as
    test_deploy_integration.py) — the deployed-service and preview image
    for this module."""
    docker_client.images.build(path="templates/generic-webtool", tag=TEMPLATE_APP_IMAGE, rm=True)
    yield TEMPLATE_APP_IMAGE
    try:
        docker_client.images.get(TEMPLATE_APP_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture(scope="module")
def no_healthz_image(docker_client):
    """Serves real HTTP on the app port but has no /healthz — the health
    gate fails while the app itself would happily answer, which is exactly
    what must never leak through Traefik pre-health."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "WORKDIR /srv\n"
            "RUN echo NO-HEALTHZ-APP > index.html\n"
            f'CMD ["python", "-m", "http.server", "{settings.preview_container_port}"]\n'
        )
        docker_client.images.build(path=tmp, tag=NO_HEALTHZ_IMAGE, rm=True)
    yield NO_HEALTHZ_IMAGE
    try:
        docker_client.images.get(NO_HEALTHZ_IMAGE).remove(force=True)
    except (ImageNotFound, DockerException):
        pass


@pytest.fixture(scope="module")
def traefik_web_port() -> int:
    return _free_port()


class _UvicornErrorCapture(logging.Handler):
    """Collects formatted `uvicorn.error` records (including tracebacks) so
    a lifespan-startup failure's actual cause can be folded into
    `backend_server_port`'s own assertion message instead of only reaching
    pytest's captured stderr."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


@pytest.fixture(scope="module")
def backend_server_port():
    """The REAL FastAPI app (actual routers/preview_auth.py code) served by
    uvicorn in a background thread on 0.0.0.0, so the Traefik container can
    call the forward-auth endpoint via host.docker.internal (which the
    `traefik_container` fixture maps to host-gateway). get_db is
    overridden to the current test's session via `_current_db_session` for
    every REQUEST this backend serves.

    That override does NOT cover STARTUP. `app.main.lifespan` runs
    `Base.metadata.create_all(bind=engine)` + `seed(db)` through
    `app.db.engine`/`SessionLocal` directly — those are resolved before a
    single request (and therefore before `get_db`) is ever involved, so
    they always hit whatever `DATABASE_URL` the process started with: the
    ambient dev DB (`sqlite:///./forge_dev.db`) for anyone running the
    suite locally. That made this fixture's success depend on the
    developer's dev DB happening to be migrated to head — three migrations
    (`oidc_sub`, `monthly_quota_usd`, `password_hash`/`department`/
    `status`) behind is enough to make `seed()` raise
    `sqlite3.OperationalError: no such column: users.oidc_sub` inside the
    ASGI lifespan, which uvicorn only ever surfaces as
    `server.started` staying `False` — a failure that reads as "the
    forward-auth backend is broken" and buries the real cause in captured
    stderr.

    Nothing else in this module reads through `app.db.engine` — every
    actual HTTP request this backend serves resolves `get_db` to
    `_current_db_session` (the per-test in-memory DB from `conftest.py`)
    via the override above, set up regardless of what engine startup used.
    So the fixture does not need the ambient `DATABASE_URL` AT ALL: give
    startup its own private, disposable SQLite file, freshly built by
    `alembic upgrade head` on every run (the lifespan no
    longer creates tables, it refuses a DB that is not at the head, so the
    fixture builds the DB the way every real one is built). That makes the dependency on the developer's dev-db
    migration state structurally unreachable rather than merely diagnosed
    with a better message — restoring a pre-migration `forge_dev.db` (see
    `## Result` for the reproduction) now has NO effect on this fixture at
    all, which is the sharpest proof the dependency is actually gone.

    Belt-and-suspenders for whatever startup failure mode is left (a code
    defect unrelated to the dev DB, say): capture `uvicorn.error`'s log
    records for the duration of the startup wait and fold them into the
    assertion message on failure, so the NEXT person to hit a startup
    failure here — of any cause — gets the cause on the first line instead
    of having to go spelunking in captured stderr.
    """

    def _override_get_db():
        assert _current_db_session is not None, "bind_backend_db fixture not active"
        yield _current_db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db

    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_db_path = Path(tmp_dir, "traefik-it-backend.db").as_posix()
        temp_engine = create_engine(
            f"sqlite:///{temp_db_path}", connect_args={"check_same_thread": False}
        )
        temp_session_local = sessionmaker(autocommit=False, autoflush=False, bind=temp_engine)
        # the lifespan only CHECKS the schema; build it by migrations.
        command.upgrade(alembic_config(f"sqlite:///{temp_db_path}"), "head")

        # `app.main.lifespan` resolves `engine`/`SessionLocal` as names in
        # `app.main`'s own namespace (bound there by `from app.db import
        # SessionLocal, engine`) — reassigning them here redirects THIS
        # module's startup without touching the shared `app.db` module or
        # any other test.
        original_engine = app_main.engine
        original_session_local = app_main.SessionLocal
        app_main.engine = temp_engine
        app_main.SessionLocal = temp_session_local

        port = _free_port()
        # Constructing `Config` runs uvicorn's own `configure_logging()`,
        # which calls `logging.config.dictConfig(...)` and — because
        # "uvicorn.error" is one of the loggers that config names —
        # REPLACES that logger's handler list wholesale. A handler added
        # before this point is silently discarded the moment `Config()` is
        # constructed (measured via mutation testing: a handler
        # added first never received a single record, even for a genuine
        # startup failure). So the capture handler is attached AFTER.
        config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, log_level="warning")

        log_capture = _UvicornErrorCapture()
        uvicorn_error_logger = logging.getLogger("uvicorn.error")
        uvicorn_error_logger.addHandler(log_capture)

        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        # Cleanup must run on EVERY exit path, including the startup-
        # failure `pytest.fail` below — otherwise a failed run leaks the
        # `app_main.engine`/`SessionLocal` patch onto every later test in
        # the process AND leaves `temp_engine` holding an open handle on a
        # file inside `tmp_dir`, which then fails to delete on Windows when
        # the `with tempfile.TemporaryDirectory()` block above exits
        # (measured via mutation testing: without this `try/finally`,
        # a planted lifespan failure left a `PermissionError` out of
        # `TemporaryDirectory.__exit__` instead of the intended message).
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not server.started:
                time.sleep(0.1)

            if not server.started:
                captured = "\n".join(log_capture.records) or "(no uvicorn.error output captured)"
                pytest.fail(
                    "uvicorn backend for forward-auth did not start (lifespan startup failed).\n"
                    f"Captured uvicorn.error output naming the cause:\n{captured}"
                )

            yield port
        finally:
            uvicorn_error_logger.removeHandler(log_capture)
            server.should_exit = True
            thread.join(timeout=5)
            fastapi_app.dependency_overrides.pop(get_db, None)
            app_main.engine = original_engine
            app_main.SessionLocal = original_session_local
            temp_engine.dispose()


@pytest.fixture(scope="module")
def traefik_container(docker_client, traefik_image, traefik_web_port):
    """A real Traefik instance with its Docker provider enabled, on its own
    bridge network (its published entrypoint is how tests reach routed
    services). Per-container `traefik.docker.network` labels point at each
    service's own network — no global provider network.

    `extra_hosts={"host.docker.internal": "host-gateway"}` is what makes the
    ForwardAuth address resolvable from inside Traefik on a plain Linux
    engine (CI, and ADR 0002's production host). Docker Desktop injects the
    name on its own, which is why this was invisible on a dev box; without
    the entry Traefik logs `lookup host.docker.internal ... no such host` and
    answers every guarded request 500 (observed). This is Traefik's
    entry only: routed services get no such entry (see the isolation test)."""
    container = docker_client.containers.run(
        image=traefik_image,
        name=f"forge-traefik-it-{uuid.uuid4().hex[:8]}",
        detach=True,
        command=[
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            "--entrypoints.web.address=:80",
            *METRICS_FLAGS,
        ],
        ports={"80/tcp": traefik_web_port},
        volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"}},
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    time.sleep(2)  # let the Docker provider come up
    yield container
    try:
        container.remove(v=True, force=True)
    except DockerException:
        pass


@pytest.fixture()
def bind_backend_db(db_session):
    global _current_db_session
    _current_db_session = db_session
    yield
    _current_db_session = None


@pytest.fixture()
def traefik_settings(monkeypatch, traefik_container, backend_server_port, bind_backend_db):
    """Turns Traefik routing ON for one test (monkeypatch reverts after),
    pointed at this module's Traefik container and forward-auth backend."""
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "apps_base_domain", "traefik-it.local")
    monkeypatch.setattr(settings, "traefik_container_name", traefik_container.name)
    monkeypatch.setattr(
        settings, "traefik_forward_auth_host", f"host.docker.internal:{backend_server_port}"
    )


@pytest.fixture()
def manager(docker_client):
    return DeployManager(client=docker_client)


def _make_project(db, seeded_ids, name):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.commit()
    return project


@pytest.fixture()
def project(db_session, seeded_ids):
    return _make_project(db_session, seeded_ids, "traefik-it-project")


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


def _confirm_and_execute(db, project, build, manager):
    deployment = confirm_deploy(
        db, project=project, build=build, actor_id=project.owner_id, image_exists=manager.image_exists
    )
    db.commit()
    claimed = claim_next_deployment(db)
    execute_deployment(db, claimed, manager=manager, git_provider=FakeMergeProvider())
    db.refresh(deployment)
    return deployment


def _cleanup_deployments(manager, db):
    for deployment in db.query(Deployment).all():
        manager.remove_service(deployment.id)


def _via_traefik(traefik_web_port: int, host: str, *, path: str = "/", timeout: float = 4.0):
    """One host-header request through Traefik's published entrypoint.
    Returns (status, body) — (None, reason) on connection-level failure
    (e.g. Traefik dialing an unreachable backend until our client timeout,
    which is exactly the pre-health posture)."""
    url = f"http://127.0.0.1:{traefik_web_port}{path}"
    req = urllib.request.Request(url, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001 - timeout/connection failures are a valid observation here
        return None, str(exc).encode()


def _wait_for(traefik_web_port, host, predicate, *, path="/", attempts=20, delay=0.5):
    """Polls until predicate((status, body)) is true (Traefik's provider
    needs a moment to observe container create/remove events); returns the
    last observation either way."""
    last = (None, b"")
    for _ in range(attempts):
        last = _via_traefik(traefik_web_port, host, path=path)
        if predicate(last):
            return last
        time.sleep(delay)
    return last


def test_activated_deployment_routed_by_host_header(
    db_session, project, template_image, manager, traefik_settings, traefik_web_port
):
    build = _make_build(db_session, project, build_id="tk-dep-1", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE

        host = f"{project.id}.{settings.apps_base_domain}"
        status, body = _wait_for(traefik_web_port, host, lambda r: r[0] == 200, path="/healthz")
        assert status == 200
        assert b"ok" in body
    finally:
        _cleanup_deployments(manager, db_session)


def test_pre_health_and_failed_deployment_never_reachable_through_traefik(
    db_session, project, no_healthz_image, manager, traefik_settings, traefik_web_port, monkeypatch
):
    """The health-gate criterion under Traefik: a container whose app IS
    serving HTTP (but fails /healthz) must never answer through Traefik —
    polled continuously DURING the health window (execute_deployment runs
    in this thread, the poller in another) and asserted absent after the
    deployment fails."""
    build = _make_build(db_session, project, build_id="tk-dep-nohz", image_ref=no_healthz_image)
    monkeypatch.setattr(settings, "deploy_health_check_timeout_seconds", 6.0)
    host = f"{project.id}.{settings.apps_base_domain}"

    deployment = confirm_deploy(
        db_session,
        project=project,
        build=build,
        actor_id=project.owner_id,
        image_exists=manager.image_exists,
    )
    db_session.commit()
    claimed = claim_next_deployment(db_session)

    leaked: list[tuple] = []
    stop_polling = threading.Event()

    def _poll_for_leak():
        while not stop_polling.is_set():
            status, body = _via_traefik(traefik_web_port, host, path="/", timeout=2.0)
            if status == 200 and b"NO-HEALTHZ-APP" in body:
                leaked.append((status, body))
            time.sleep(0.2)

    poller = threading.Thread(target=_poll_for_leak)
    poller.start()
    try:
        execute_deployment(db_session, claimed, manager=manager, git_provider=FakeMergeProvider())
    finally:
        stop_polling.set()
        poller.join(timeout=5)

    try:
        db_session.refresh(deployment)
        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_reason == DeploymentFailureReason.HEALTH_TIMEOUT
        # Never served through Traefik at any point during the window.
        assert leaked == []
        # And after the failure the router is gone entirely (container
        # removed): Traefik's own 404, not anything from the app.
        status, body = _via_traefik(traefik_web_port, host, path="/")
        assert status == 404
        assert b"NO-HEALTHZ-APP" not in body
    finally:
        _cleanup_deployments(manager, db_session)


def test_route_follows_active_deployment_on_supersede_and_stop(
    db_session, project, template_image, manager, traefik_settings, traefik_web_port
):
    build1 = _make_build(db_session, project, build_id="tk-dep-v1", image_ref=template_image)
    try:
        first = _confirm_and_execute(db_session, project, build1, manager)
        assert first.status == DeploymentStatus.ACTIVE
        host = f"{project.id}.{settings.apps_base_domain}"
        assert _wait_for(traefik_web_port, host, lambda r: r[0] == 200, path="/healthz")[0] == 200

        # Supersede: same host now serves via the new deployment's
        # container; the old container (and its router) is gone.
        build2 = _make_build(db_session, project, build_id="tk-dep-v2", image_ref=template_image)
        second = _confirm_and_execute(db_session, project, build2, manager)
        db_session.refresh(first)
        assert second.status == DeploymentStatus.ACTIVE
        assert first.status == DeploymentStatus.SUPERSEDED
        assert _wait_for(traefik_web_port, host, lambda r: r[0] == 200, path="/healthz")[0] == 200

        # Stop: the route disappears entirely (Traefik 404 — no router).
        stop_active_deployment(
            db_session, project=project, actor_id=project.owner_id, manager=manager
        )
        db_session.commit()
        status, _ = _wait_for(traefik_web_port, host, lambda r: r[0] == 404, path="/healthz")
        assert status == 404
    finally:
        _cleanup_deployments(manager, db_session)


def test_preview_forward_auth_accept_and_reject_paths(
    docker_client, db_session, project, template_image, traefik_settings, traefik_web_port
):
    """Preview prod-shape routing end-to-end through the REAL verification
    endpoint: valid signed token passes forward-auth and reaches the
    preview container; tampered/missing tokens are rejected 403 by the
    platform endpoint via Traefik's ForwardAuth (token travels in
    X-Forwarded-Uri); teardown removes the route."""
    preview_manager = PreviewManager(client=docker_client)
    build = _make_build(db_session, project, build_id="tk-preview-1", image_ref=template_image)
    try:
        issued = preview_manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()

        host = f"preview--{project.id}.{settings.apps_base_domain}"
        assert host in issued.url  # prod-shape URL points at the Traefik host

        # Valid token -> 200 from the real app inside the preview container.
        status, body = _wait_for(
            traefik_web_port,
            host,
            lambda r: r[0] == 200,
            path=f"/healthz?token={issued.token}",
        )
        assert status == 200
        assert b"ok" in body

        # Tampered token -> 403 from the forward-auth endpoint; the
        # container's healthz body never comes through.
        status, body = _via_traefik(traefik_web_port, host, path="/healthz?token=aaaa.1111.bbbb")
        assert status == 403
        assert b"status" not in body  # not the app's {"status": "ok"} payload

        # Missing token -> 403 as well.
        status, _ = _via_traefik(traefik_web_port, host, path="/healthz")
        assert status == 403

        # Teardown removes the route (ADR 0016: route removed at teardown).
        preview_manager.stop_preview(build.id)
        status, _ = _wait_for(
            traefik_web_port, host, lambda r: r[0] == 404, path=f"/healthz?token={issued.token}"
        )
        assert status == 404
    finally:
        preview_manager.stop_preview(build.id)


def test_isolation_between_routed_services_and_platform(
    docker_client, db_session, seeded_ids, template_image, manager, traefik_settings,
    traefik_web_port, backend_server_port, traefik_container,
):
    """Isolation under Traefik: two projects both live-routed, yet (a)
    service A cannot reach service B's container directly, and (b) a
    service cannot reach the platform API (the forward-auth backend on the
    host) — per-service internal networks hold even with the Traefik
    container multi-homed onto each."""
    project_a = _make_project(db_session, seeded_ids, "traefik-iso-a")
    project_b = _make_project(db_session, seeded_ids, "traefik-iso-b")
    build_a = _make_build(db_session, project_a, build_id="tk-iso-a", image_ref=template_image)
    build_b = _make_build(db_session, project_b, build_id="tk-iso-b", image_ref=template_image)
    try:
        dep_a = _confirm_and_execute(db_session, project_a, build_a, manager)
        dep_b = _confirm_and_execute(db_session, project_b, build_b, manager)
        assert dep_a.status == DeploymentStatus.ACTIVE
        assert dep_b.status == DeploymentStatus.ACTIVE
        # Both are really routed (the Traefik container is attached to both
        # services' networks).
        for proj in (project_a, project_b):
            host = f"{proj.id}.{settings.apps_base_domain}"
            assert _wait_for(traefik_web_port, host, lambda r: r[0] == 200, path="/healthz")[0] == 200

        container_a = docker_client.containers.get(f"forge-deploy-{dep_a.id}")
        container_b = docker_client.containers.get(f"forge-deploy-{dep_b.id}")
        container_b.reload()
        networks_b = container_b.attrs["NetworkSettings"]["Networks"]
        ip_b = next(iter(networks_b.values()))["IPAddress"]

        probe = (
            "import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(({target!r}, {port}))\n"
            "    print('CONNECTED')\n"
            "except Exception as exc:\n"
            "    print('BLOCKED', exc)\n"
        )

        # (a) A -> B directly: blocked (separate internal networks).
        exit_code, output = container_a.exec_run(
            ["python", "-c", probe.format(target=ip_b, port=settings.preview_container_port)]
        )
        assert b"BLOCKED" in output

        # (b) A -> platform API (host): blocked (internal network — no
        # gateway, no host DNS).
        #
        # "blocked" alone is satisfied by a name that simply does not
        # resolve, which is what a plain Linux engine does with
        # `host.docker.internal` for ANY container — so probing the name
        # only would pass vacuously there. Probe the host-gateway IP too
        # (read from the Traefik container, which has the alias), and prove
        # with a positive control that this exact address:port IS reachable
        # from a container that is not isolated. Then BLOCKED from A can only
        # mean the network blocked it. (Routed services get no extra_hosts
        # entry; the name is unresolvable from A on Linux, and the IP probe
        # covers that case on every engine.)
        hosts_out = traefik_container.exec_run(["cat", "/etc/hosts"]).output.decode()
        gateway_ips = [
            line.split()[0]
            for line in hosts_out.splitlines()
            if "host.docker.internal" in line.split()[1:]
        ]
        assert gateway_ips, f"Traefik has no host.docker.internal entry: {hosts_out!r}"
        gateway_ip = gateway_ips[0]

        control = docker_client.containers.run(
            image=template_image,
            entrypoint=["python", "-c"],
            command=[probe.format(target=gateway_ip, port=backend_server_port)],
            extra_hosts={"host.docker.internal": "host-gateway"},
            remove=True,
        )
        assert b"CONNECTED" in control, (
            f"positive control failed: {gateway_ip}:{backend_server_port} is not "
            f"reachable even from a non-isolated container: {control!r}"
        )

        for target in ("host.docker.internal", gateway_ip):
            exit_code, output = container_a.exec_run(
                ["python", "-c", probe.format(target=target, port=backend_server_port)]
            )
            assert b"BLOCKED" in output, f"service A reached the platform via {target}: {output!r}"
            assert b"CONNECTED" not in output
    finally:
        _cleanup_deployments(manager, db_session)


def test_no_orphaned_networks_or_traefik_attachments_after_lifecycle(
    docker_client, db_session, project, template_image, manager, traefik_settings,
    traefik_web_port, traefik_container,
):
    """After deploy -> redeploy -> stop, no service containers/networks
    remain and the Traefik container is back to only its original networks
    (every per-service attach got disconnected). ADR 0023: the
    project's OWN Redis container is the one deliberate exception --
    "never deleted implicitly" -- asserted present by name, not counted as
    a leak (mirrors tests/test_deploy_integration.py's identical test)."""
    from app.services.service_data import redis_container_name

    traefik_container.reload()
    nets_before = set(traefik_container.attrs["NetworkSettings"]["Networks"].keys())
    containers_before = {c.id for c in docker_client.containers.list(all=True)}
    networks_before = {n.id for n in docker_client.networks.list()}

    build1 = _make_build(db_session, project, build_id="tk-lc-v1", image_ref=template_image)
    _confirm_and_execute(db_session, project, build1, manager)
    build2 = _make_build(db_session, project, build_id="tk-lc-v2", image_ref=template_image)
    _confirm_and_execute(db_session, project, build2, manager)
    stop_active_deployment(db_session, project=project, actor_id=project.owner_id, manager=manager)
    db_session.commit()

    traefik_container.reload()
    nets_after = set(traefik_container.attrs["NetworkSettings"]["Networks"].keys())
    containers_after = {c.id for c in docker_client.containers.list(all=True)}
    networks_after = {n.id for n in docker_client.networks.list()}

    redis_container = docker_client.containers.get(redis_container_name(project.id))
    try:
        assert nets_after == nets_before
        assert containers_after - containers_before == {redis_container.id}
        assert networks_after - networks_before == set()
    finally:
        redis_container.remove(v=True, force=True)


# -- the traffic signal read from this Traefik's own counters ---------------------


@pytest.fixture()
def metrics_settings(monkeypatch, traefik_settings):
    monkeypatch.setattr(settings, "traefik_metrics_port", METRICS_PORT)


def _stale(db, project):
    project.last_active_at = datetime.now(timezone.utc) - timedelta(days=30)
    db.commit()
    db.refresh(project)
    return project.last_active_at


def _traefik_metrics_text(traefik_container) -> str:
    return docker_exec_read(
        traefik_container.name,
        ["wget", "-q", "-T", "10", "-O", "-", f"http://127.0.0.1:{METRICS_PORT}/metrics"],
        METRICS_READ_CAP_BYTES,
    )


def test_a_request_through_traefik_advances_last_active_after_one_poll(
    db_session, project, template_image, manager, metrics_settings, traefik_web_port, traefik_container
):
    build = _make_build(db_session, project, build_id="tk-sig-1", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE
        poller = TrafficSignalPoller()
        assert poller.poll(db_session) is True  # the baseline; nothing is traffic yet

        stale = _stale(db_session, project)
        host = f"{project.id}.{settings.apps_base_domain}"
        assert _wait_for(traefik_web_port, host, lambda r: r[0] == 200, path="/healthz")[0] == 200

        assert poller.poll(db_session) is True
        db_session.refresh(project)
        assert project.last_active_at > stale
        assert f'service="forge-deploy-{deployment.id}@docker"' in _traefik_metrics_text(traefik_container)
    finally:
        _cleanup_deployments(manager, db_session)


def test_a_preview_request_through_traefik_does_not_advance_last_active(
    docker_client, db_session, project, template_image, metrics_settings, traefik_web_port, traefik_container
):
    preview_manager = PreviewManager(client=docker_client)
    build = _make_build(db_session, project, build_id="tk-sig-prev", image_ref=template_image)
    try:
        issued = preview_manager.start_preview(project_id=project.id, build=build)
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash
        db_session.commit()
        poller = TrafficSignalPoller()
        assert poller.poll(db_session) is True

        stale = _stale(db_session, project)
        host = f"preview--{project.id}.{settings.apps_base_domain}"
        status, _ = _wait_for(
            traefik_web_port, host, lambda r: r[0] == 200, path=f"/healthz?token={issued.token}"
        )
        assert status == 200

        # The test can reach its risk: Traefik really counted the preview, under a forge-preview name.
        assert 'service="forge-preview-' in _traefik_metrics_text(traefik_container)

        assert poller.poll(db_session) is True
        db_session.refresh(project)
        assert project.last_active_at == stale
    finally:
        preview_manager.stop_preview(build.id)


def test_a_service_container_cannot_reach_the_metrics_port_on_traefiks_address(
    docker_client, db_session, project, template_image, manager, traefik_settings, traefik_container
):
    build = _make_build(db_session, project, build_id="tk-sig-iso", image_ref=template_image)
    try:
        deployment = _confirm_and_execute(db_session, project, build, manager)
        assert deployment.status == DeploymentStatus.ACTIVE
        service = docker_client.containers.get(f"forge-deploy-{deployment.id}")
        service.reload()
        service_networks = set(service.attrs["NetworkSettings"]["Networks"])
        traefik_container.reload()
        shared = {
            name: cfg["IPAddress"]
            for name, cfg in traefik_container.attrs["NetworkSettings"]["Networks"].items()
            if name in service_networks
        }
        assert shared, "Traefik is not attached to the service's network"
        traefik_ip = next(iter(shared.values()))

        probe = (
            "import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(({target!r}, {port}))\n"
            "    print('CONNECTED')\n"
            "except Exception as exc:\n"
            "    print('BLOCKED', exc)\n"
        )
        # Positive control: Traefik's address on this network is reachable at all (its web
        # entrypoint listens on every address), so BLOCKED below can only mean the metrics
        # entrypoint is not listening there.
        _, control = service.exec_run(["python", "-c", probe.format(target=traefik_ip, port=80)])
        assert b"CONNECTED" in control, control
        _, output = service.exec_run(["python", "-c", probe.format(target=traefik_ip, port=METRICS_PORT)])
        assert b"BLOCKED" in output and b"CONNECTED" not in output, output
    finally:
        _cleanup_deployments(manager, db_session)


def test_the_loopback_check_passes_on_this_traefik_and_refuses_one_listening_on_all_addresses(
    docker_client, traefik_image, db_session, monkeypatch, traefik_container, caplog
):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_metrics_port", METRICS_PORT)

    # The measured method: the container's own /proc/net/tcp{,6}, read through docker exec.
    def listens_beyond(container) -> bool:
        tcp = docker_exec_read(container.name, ["cat", "/proc/net/tcp"], 1024 * 1024)
        tcp6 = docker_exec_read(
            container.name, ["sh", "-c", "cat /proc/net/tcp6 2>/dev/null || true"], 1024 * 1024
        )
        return listens_beyond_loopback(tcp, tcp6, METRICS_PORT)

    assert listens_beyond(traefik_container) is False
    monkeypatch.setattr(settings, "traefik_container_name", traefik_container.name)
    assert TrafficSignalPoller().poll(db_session) is True

    exposed = docker_client.containers.run(
        image=traefik_image,
        name=f"forge-traefik-it-exposed-{uuid.uuid4().hex[:8]}",
        detach=True,
        command=[
            f"--entrypoints.forgemetrics.address=:{METRICS_PORT}",
            "--metrics.prometheus=true",
            "--metrics.prometheus.entryPoint=forgemetrics",
        ],
    )
    try:
        time.sleep(2)
        assert listens_beyond(exposed) is True
        monkeypatch.setattr(settings, "traefik_container_name", exposed.name)
        with caplog.at_level(logging.ERROR, logger="app.traefik_traffic"):
            assert TrafficSignalPoller().poll(db_session) is False
        assert any("loopback" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    finally:
        exposed.remove(v=True, force=True)
