# `backend/.env` now documents `AUTH_MODE=local` (+ its bootstrap
# admin pair) as the platform owner's dev-run credentials (ADR 0022) — the
# right thing for `dev.ps1`, but the whole suite below is written against
# dev-mode behaviour (`X-Forge-User-Id` header auth) and this module's own
# fixtures already document the invariant every mode-sensitive test relies
# on: "that [process-wide `app.config.settings`] singleton is already fixed
# at import time to dev mode for the rest of the suite" (see
# test_config.py's module docstring, unchanged by this note). `.env`
# gaining a live `AUTH_MODE` value is new and would otherwise
# silently break that invariant, so this suite pins it back explicitly,
# before `app.main`/`app.config` are ever imported (its own module-level
# `settings = Settings()` runs at import time — a fixture is too late).
#
# Two steps, in order:
#  1. Force AUTH_MODE=dev in the process environment for just long enough
#     to trigger `app.config`'s module-level singleton construction. Real
#     env vars outrank the dotenv file in pydantic-settings' source order,
#     so this reliably wins over `.env` regardless of its contents.
#  2. Once the `Settings` class exists, permanently disable dotenv loading
#     for the rest of the process (`model_config["env_file"] = None`) and
#     restore the environment variable to whatever it was before step 1.
#     Without this, step 1's override would leak into every *other*
#     `Settings(...)` construction for the rest of the session too — even
#     ones built with `_env_file=None` (that flag disables only the dotenv
#     source; a real env var is a separate, still-active source) — which
#     would make every mode-sensitive test in test_config.py that omits
#     `auth_mode` see "dev" instead of its expected forge_env-derived
#     default. Disabling dotenv here instead means every `Settings()`
#     built anywhere in the suite, with or without `_env_file=None`, now
#     behaves exactly as if `backend/.env` did not exist — restoring
#     dev-mode-by-default without leaving a live env var lying around to
#     surprise a test that does not expect one.
#
# `FORGE_ENV` has no default any more
# -- building a `Settings` without it raises `ForgeEnvNotSetError`, and
# importing `app.config` builds one. So the suite sets it EXPLICITLY, and
# permanently (unlike `AUTH_MODE`, which is put back): it is what the removed
# "dev" default used to supply for every `Settings(_env_file=None)` that does not
# name a mode, and subprocess-based tests inherit it. A test that wants the
# unset path removes it itself (`monkeypatch.delenv`); the autouse
# `_forge_env_is_set` below puts it back for every test that does not.
import os

os.environ["FORGE_ENV"] = "dev"

_PRIOR_AUTH_MODE = os.environ.get("AUTH_MODE")
os.environ["AUTH_MODE"] = "dev"
try:
    from app.config import Settings as _Settings  # noqa: E402  (see note above)
finally:
    Settings_model_config = _Settings.model_config
    Settings_model_config["env_file"] = None
    if _PRIOR_AUTH_MODE is None:
        os.environ.pop("AUTH_MODE", None)
    else:
        os.environ["AUTH_MODE"] = _PRIOR_AUTH_MODE

import functools
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# every non-credential setting is put back to what a `.env`-free `Settings` reads BEFORE `app.main`
# (and the modules after it) are imported, because some of them read the singleton at import:
# `app.main` mounts the preview/service proxy routes only when `traefik_enabled` is false, so an owner
# `.env` with `TRAEFIK_ENABLED=true` left 26 tests on a 404 (measured with a `.env` setting every
# key of `.env.example`). A fixture is too late for that, so this runs here, like the AUTH_MODE pin above;
# `_session_import_time_pins` undoes it at session end. The credential fields wait for
# `_session_credentials_are_unset`, which must first read the ambient DSN's port.
from app.config import settings as _ambient_settings  # noqa: E402
from tests.ambient_credentials import neutralise_other_settings, strip_credential_env  # noqa: E402

_IMPORT_TIME_PINS = pytest.MonkeyPatch()
strip_credential_env(_IMPORT_TIME_PINS, _Settings)
neutralise_other_settings(_IMPORT_TIME_PINS, _Settings, _ambient_settings)

import app.db as app_db
import app.demo_data as app_demo_data
import app.main as app_main
import app.seed as app_seed
import app.worker as app_worker
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.base import Base
from app.seed import seed

TEST_DATABASE_URL = "sqlite:///:memory:"


def _point_everything_at(mp: pytest.MonkeyPatch, sentinel_file) -> "object":
    """Rebind the ambient URL and every engine/session alias to `sentinel_file`;
    returns the throwaway engine so the caller can dispose it."""
    url = f"sqlite:///{sentinel_file.as_posix()}"
    guard_engine = create_engine(url, connect_args={"check_same_thread": False})
    guard_sessions = sessionmaker(autocommit=False, autoflush=False, bind=guard_engine)

    mp.setattr(settings, "database_url", url)
    for module in (app_db, app_main, app_worker):
        mp.setattr(module, "engine", guard_engine)
    for module in (app_db, app_main, app_worker, app_demo_data, app_seed):
        mp.setattr(module, "SessionLocal", guard_sessions)
    return guard_engine


@pytest.fixture(autouse=True, scope="session")
def _session_db_guard(tmp_path_factory):
    """Nothing in the suite may reach the platform owner's dev DB.

    The ambient `.env` points `settings.database_url` at `./forge_dev.db`, and
    `app.db` builds its `engine`/`SessionLocal` from it at import time, before
    any fixture can run -- and `app.main`, `app.worker`, `app.demo_data` and
    `app.seed` hold aliases to those. So the whole session starts with BOTH the
    URL (what `migrations/env.py` falls back to when no URL is supplied) and
    every such alias pointing at a throwaway, empty file.

    This one is **session**-scoped on purpose: pytest sets up higher-scoped
    fixtures first, so a module-scoped fixture (the traefik module's
    `backend_server_port` runs `alembic upgrade` at module-fixture time) would
    otherwise run before any function-scoped guard exists. Undone explicitly at
    session end. `_suite_db_guard` below re-points everything per test."""
    mp = pytest.MonkeyPatch()
    engine = _point_everything_at(mp, tmp_path_factory.mktemp("db-guard-session") / "must-not-be-used.db")
    try:
        yield
    finally:
        mp.undo()
        engine.dispose()


@pytest.fixture(autouse=True, scope="session")
def _session_service_postgres_guard():
    """nothing in the suite reaches the developer's dev service Postgres (`dev.ps1` starts it on a
    loopback port; it holds the demo service's live database).

    Two things, for the whole session (session scope for the reason `_session_db_guard` gives: a
    module-scoped fixture runs before any function-scoped one):

    1. **The ambient DSN starts as None.** `settings` is built from `backend/.env` at import, before the
       dotenv source is switched off above, so `settings.service_postgres_admin_dsn` is the developer's REAL
       credential (observed: the dev instance's port, the real admin role and its real password). A test
       that reached provisioning code without setting the DSN itself would have connected to the live
       instance and authenticated. A test that needs a DSN sets one: the shared fake that never connects
       (`tests/fake_admin_dsn.py`) or the throwaway container's (`real_service_postgres_admin_dsn`). This
       cannot be a per-test layer like `_engine_settings_are_not_the_owners`: that module-scoped fixture
       sets the DSN before any function-scoped fixture runs, and a per-test reset would erase it.
    2. **Every route to a DSN that reaches the dev instance fails loudly.** `Settings.__setattr__`
       (`monkeypatch.setattr`, a direct assignment) and `Settings.__init__` (a constructor argument or an
       environment variable) raise `DevInstanceReached`, naming the rule. The port is read from `dev.ps1`
       (`tests/dev_instance.py`), never written here, plus the port of the `.env` DSN neutralised in step 1.
       No test legitimately needs the dev instance (the Docker tests use throwaway containers), so there is
       no opt-out marker; add one only together with such a test.

    `tests/test_dev_instance_guard.py` proves both; the static scan in `test_config.py` is the third layer."""
    from tests.dev_instance import guarded_ports, load_dev_instance_or_none, refuse_dev_instance

    # None only in a backend-only container (no dev.ps1, and CI's FORGE_REQUIRE_REPO_GUARD unset): then
    # no dev instance is defined and only the `.env` DSN's port is guarded. A dev.ps1 that has changed
    # shape raises, and so does a missing one on CI.
    ports = guarded_ports(load_dev_instance_or_none(), settings.service_postgres_admin_dsn)
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "service_postgres_admin_dsn", None)

    original_setattr = _Settings.__setattr__
    original_init = _Settings.__init__

    def guarded_setattr(self, name, value):
        if name == "service_postgres_admin_dsn":
            refuse_dev_instance(value, ports, where="assignment to Settings")
        original_setattr(self, name, value)

    @functools.wraps(original_init)
    def guarded_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        refuse_dev_instance(self.service_postgres_admin_dsn, ports, where="Settings construction")

    # Installed after step 1, so undoing in reverse order removes the hooks before the ambient value is put back.
    mp.setattr(_Settings, "__setattr__", guarded_setattr)
    mp.setattr(_Settings, "__init__", guarded_init)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True, scope="session")
def _session_import_time_pins():
    """puts back what the module-level pin above changed (settings and the process environment)
    when the session ends."""
    try:
        yield
    finally:
        _IMPORT_TIME_PINS.undo()


@pytest.fixture(autouse=True, scope="session")
def _session_credentials_are_unset(_session_service_postgres_guard):
    """no credential the developer or CI holds reaches a test, whichever field it is in.

    Neutralising only one field (the admin DSN) is not enough. The ambient `settings` is built at import from
    `backend/.env` AND the process environment, so EVERY credential-valued field it carries is the
    operator's real one -- observed: an owner-set `SERVICE_ENV_ENCRYPTION_KEY` turned three
    tests red on the developer's machine while CI stayed green, because they assert the key was not
    configured. This starts every field `tests/credential_fields.py` discovers (by rule, not a
    list kept here) at its UNSET state, and removes the credential variables from the process
    environment, so `Settings(_env_file=None)` and a subprocess inherit none either. The unset state
    is what a credential-free `Settings` reads (`tests/ambient_credentials.py`): `None` for most, a
    per-process random value for the signing secrets and the encryption key in dev.

    The rest of `settings` is reset the same way, at conftest import (see the module-level pin above), to
    what a `.env`-free `Settings` reads: measured with a
    `.env` that sets every key of `.env.example`, 241 tests failed (an unpullable image name, a 4 MB memory
    limit, a proxy port that collides with its admin port, a changed URL scheme) while CI, which has no
    `.env`, was green. What `app.db` took at import (the engine, from `database_url`) is the DB guard's.

    Depends on `_session_service_postgres_guard` by name: that one reads the ambient DSN's port before
    this one erases the DSN. `database_url` is left to `_session_db_guard`. A test that needs a value
    sets it itself. `tests/test_ambient_credentials_guard.py` proves it, including with a value in the
    environment at import."""
    from tests.ambient_credentials import neutralise_ambient_credentials

    mp = pytest.MonkeyPatch()
    neutralise_ambient_credentials(mp, _Settings, settings)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True, scope="session")
def _session_platform_instance():
    """the whole session labels every container and network it creates with its own
    instance id (`tests/instance_scope.py`), never one a real installation could hold. Session
    scope for the same reason as the DB guard: a module-scoped fixture runs before any
    function-scoped one."""
    from app.services import platform_labels
    from tests.instance_scope import TEST_INSTANCE_ID

    platform_labels.set_instance_id(TEST_INSTANCE_ID)
    try:
        yield
    finally:
        platform_labels.clear_instance_id()


@pytest.fixture(autouse=True)
def _platform_instance_is_the_tests(monkeypatch):
    """per-test layer over the session pin. A test that binds another id (`worker.main`
    binds the one it reads from the database) is undone afterwards, so no later test labels or
    reconciles under an id it did not choose. Also clears the worker's shutdown request, which
    is process state a signal test sets."""
    from app.services import platform_labels, worker_shutdown
    from tests.instance_scope import TEST_INSTANCE_ID

    monkeypatch.setattr(platform_labels, "_instance_id", TEST_INSTANCE_ID)
    worker_shutdown.SHUTDOWN.reset()
    yield
    worker_shutdown.SHUTDOWN.reset()


class _NoLease:
    """The worker lease as the tests that run `worker.main()` for some other reason see it: held
    from the start, never lost, nothing written. The lease's own tests (`test_worker_lease*.py`) use
    the real `services.worker_lease.WorkerLease` against a database, which is what proves it."""

    lost = False

    def require(self) -> None:
        return None

    def release(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _worker_lease_is_a_stub(monkeypatch):
    """`worker.main()` takes the installation's lease from the database and
    would otherwise make every test that drives it (against a fake session, an in-memory schema
    with no lease row, or a schema-refusal) about the lease. They are not; this keeps them about
    what they are. A test of the lease replaces `worker._acquire_worker_lease` with one that builds
    a real `WorkerLease` over its own database."""
    import app.worker as worker

    monkeypatch.setattr(worker, "_acquire_worker_lease", lambda: _NoLease())


@pytest.fixture(autouse=True)
def _service_checker_is_off_in_worker_tests(monkeypatch):
    """`worker._serve` starts the service checker on a thread of its own. A test that drives
    `main()` / `_serve` against a fake or shared session (`db_session` is ONE connection) would have
    that thread use and close the session from outside the test's own flow, so by default no checker
    starts. `tests/test_service_health.py` puts the real one back for the tests that are about it."""
    import app.worker as worker

    monkeypatch.setattr(worker, "_start_service_checks", lambda lease=None: None)


@pytest.fixture(autouse=True)
def _notification_channels_are_not_the_owners(monkeypatch):
    """the ambient `backend/.env` may configure a real webhook or SMTP relay. A suite that
    drives service checks, disk checks and the lifespan must never reach either, so every test starts
    with NO channel; a test that wants one sets it (pointing at a receiver it started itself)."""
    from app.config import settings

    monkeypatch.setattr(settings, "notify_webhook_url", None)
    monkeypatch.setattr(settings, "notify_smtp_host", None)
    monkeypatch.setattr(settings, "notify_smtp_password", None)


@pytest.fixture(autouse=True)
def _forge_env_is_set(monkeypatch):
    """every test starts with `FORGE_ENV=dev` in the process environment,
    set explicitly. The module-level assignment at the top of this file covers
    import time and collection; this per-test layer covers a test that removed
    it by hand (`os.environ.pop`) and would otherwise leave every later test to
    meet `ForgeEnvNotSetError`. `tests/test_config.py` and
    `tests/test_startup_refuses_without_forge_env.py` remove it deliberately,
    through this same `monkeypatch`, which puts it back."""
    monkeypatch.setenv("FORGE_ENV", "dev")


@pytest.fixture(autouse=True)
def _repair_loop_is_off_unless_a_test_turns_it_on(monkeypatch):
    """The repair loop is ON by default in production (`BUILD_REPAIR_MAX_ATTEMPTS=1`),
    and with it on the executor runs a different order (publish last, once) over a different seam than
    the one the older executor tests drive (`_publish`, `publish_build` before `assemble_image`). So
    every test starts with the loop OFF, which is exactly the single-attempt flow, and a test of the loop
    sets `build_repair_max_attempts` itself (`tests/test_sandbox_build_executor_repair.py`). That the
    production default is 1 is pinned in `tests/test_repair_setting.py`, not by the ambient value here."""
    from app.config import settings

    monkeypatch.setattr(settings, "build_repair_max_attempts", 0)


@pytest.fixture(autouse=True)
def _engine_settings_are_not_the_owners(monkeypatch):
    """which engines count as available and which one is selected are decided from
    `Settings`, and the ambient `backend/.env` may hold an engine key (the developer's OpenAI key)
    that has nothing to do with the test in hand -- a build would flip to QUEUED, or an
    executor would be selected, depending on whose machine runs the suite. So every test
    starts with NEITHER engine key and no engine default; a test that wants one sets it
    (`anthropic_api_key` joins the nulling -- it was only "already None" because the
    owner's `.env` happened not to hold one, and an owner `ANTHROPIC_API_KEY` there would
    otherwise flip the opt-in ambiently, the same defect the OpenAI key had)."""
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "agent_engine_default", None)


@pytest.fixture(autouse=True)
def _suite_db_guard(tmp_path_factory, monkeypatch):
    """Per-test layer over `_session_db_guard`: a fresh, empty throwaway file for
    every test, so one test's leftovers never reach the next. A test that forgets
    its own redirect then meets an empty database ("EMPTY DATABASE", no tables)
    instead of someone's data, and an alembic call with no URL builds the
    throwaway file. Tests that need a database bring their own (`db_session`,
    `redirect_startup`/`worker_on`, the traefik module's temp file) and rebind on
    top; monkeypatch undoes in reverse order, so the traefik module-scoped
    save-and-restore of `original_engine` is unaffected.
    `tests/test_db_guard.py` proves both layers are in effect."""
    engine = _point_everything_at(monkeypatch, tmp_path_factory.mktemp("db-guard") / "must-not-be-used.db")
    yield
    engine.dispose()


@pytest.fixture()
def db_session():
    engine = create_engine(
        TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # scoped to THIS fixture's own engine, not the `Engine` class —
    # `event.listens_for(Engine, "connect")` (no specific engine) subscribes
    # process-wide, so once a test using `db_session` had run, every LATER
    # engine created anywhere in the same pytest session — including a real
    # Postgres admin connection (services/service_data.py) — got a SQLite
    # `PRAGMA` fired at it too. Never manifested before this task because
    # nothing else in the suite created a second, non-SQLite engine
    # in-process.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    seed(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    # Not used as a context manager: that would trigger FastAPI's startup
    # event, which creates tables/seeds via app.db's module-level engine
    # (the real dev sqlite file), bypassing this fixture's isolated db_session.
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_ids(db_session):
    from app.models.template import Template
    from app.models.user import Team, User

    template = db_session.query(Template).first()
    team = db_session.query(Team).first()
    requester = db_session.query(User).filter_by(role="requester").first()
    return {"template_id": template.id, "team_id": team.id, "user_id": requester.id}


@pytest.fixture(scope="module")
def real_service_postgres_admin_dsn():
    """ADR 0023: a THROWAWAY `postgres:18-alpine` container, one per
    test module, published on LOOPBACK ONLY (ADR 0023 §Amendment 10: the
    container's own IP is unreachable from this host — verified on this box
    by probe; 127.0.0.1 is), with
    `settings.service_postgres_admin_dsn` AND
    `settings.service_postgres_container_name` pointed at it for the
    module's tests and both restored afterward. Opt-in (not autouse) — only
    the handful of modules that exercise real confirm -> execute
    provisioning request this by name; every other test in the suite pays
    nothing for it.

    The DSN handed to tests points at a
    `forge_admin` role — `CREATEDB CREATEROLE`, `NOSUPERUSER` — created
    inside the container the SAME way `dev.ps1` creates it, not at the
    image's bootstrap `postgres` superuser. The step-7 gate (2026-09-04)
    found that every test in this suite had been passing against a
    credential the platform is specified (ADR 0023 §Amendment 5) never to
    hold: `postgres` bypasses the exact privilege check
    `provision_project_postgres` needs to get right. The bootstrap
    superuser still exists (it is how the image starts at all) but is used
    ONLY here, to create `forge_admin`, and is never hidden by
    `settings.service_postgres_admin_dsn`.

    Readiness is `pg_isready -h 127.0.0.1` inside the container, not a bare
    TCP connect (probe finding: the published port accepts a connection a
    full second before Postgres can actually answer) and not a
    socket-default `pg_isready` either: the official image's entrypoint
    starts a TEMPORARY server for init scripts that listens on the Unix
    socket ONLY, then stops it and starts the real one listening on TCP —
    confirmed against this image's own startup log. Plain
    `pg_isready` (no `-h`) defaults to the Unix socket and reports ready
    during that temporary phase, moments before the real server replaces
    it; forcing TCP means it can only succeed once the FINAL server is the
    one answering.

    Also removes the `forge-service-redis-*` and `forge-preview-redis-*`
    containers the module's own tests created — real containers on the REAL
    host daemon (unlike the throwaway Postgres above, which is destroyed as
    a whole), so nothing else cleans them up. **Only the ones this run
    created:** `tests/docker_cleanup.py` tags every data-service
    container made while the fixture is active with a run id and removes by
    that. This used to remove every container on the host labelled
    `forge.data_service=redis`/`=preview-redis` — the labels the platform
    itself puts on a project's production Redis — so one Docker test module
    deleted the Redis of a service the developer was running on the same host.
    """
    docker = pytest.importorskip("docker")
    from docker.errors import DockerException, NotFound

    try:
        client = docker.from_env()
        client.ping()
    except DockerException:
        pytest.skip("Docker Engine unavailable")

    from app.config import settings
    from tests.docker_cleanup import DataServiceContainerTracker, remove_strays

    # Containers a killed earlier run left behind (tagged by the tracker, so
    # never anything the platform made).
    remove_strays(client)

    container_name = f"forge-test-admin-postgres-{uuid.uuid4().hex[:8]}"
    bootstrap_password = "forge-test-bootstrap-pw"
    admin_password = "forge-test-admin-pw"
    container = client.containers.run(
        image="postgres:18-alpine",
        name=container_name,
        detach=True,
        environment={"POSTGRES_PASSWORD": bootstrap_password, "POSTGRES_USER": "postgres"},
        ports={"5432/tcp": ("127.0.0.1", None)},
    )
    tracker = DataServiceContainerTracker()
    tracker.__enter__()
    try:
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            exit_code, _ = container.exec_run(
                ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
            )
            if exit_code == 0:
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            pytest.fail("throwaway admin postgres never became ready (pg_isready)")

        # (i1): create the non-superuser admin role the platform actually
        # holds — same shape as dev.ps1's own `forge_admin` role (ADR 0023
        # §Amendment 5), via the bootstrap superuser above.
        exit_code, output = container.exec_run(
            [
                "psql",
                "-U",
                "postgres",
                "-c",
                f"CREATE ROLE forge_admin WITH LOGIN PASSWORD '{admin_password}' "
                "CREATEDB CREATEROLE NOSUPERUSER",
            ]
        )
        if exit_code != 0:
            pytest.fail(
                f"failed to create forge_admin role on throwaway admin postgres: {output!r}"
            )

        container.reload()
        host_port = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        dsn = f"postgresql://forge_admin:{admin_password}@127.0.0.1:{host_port}/postgres"

        # deploy_manager.py attaches the shared Postgres to a deployment's
        # network by CONTAINER NAME (ADR 0023 §Amendment 6) — a caller that
        # drives a real confirm -> execute flow against this fixture needs
        # that name to resolve to THIS throwaway container, not the
        # production default.
        prior_dsn = settings.service_postgres_admin_dsn
        prior_container_name = settings.service_postgres_container_name
        settings.service_postgres_admin_dsn = dsn
        settings.service_postgres_container_name = container_name
        try:
            yield dsn
        finally:
            settings.service_postgres_admin_dsn = prior_dsn
            settings.service_postgres_container_name = prior_container_name
    finally:
        # exactly the data-service containers this run created (a
        # service's own redis and a preview's own) -- by run id, never
        # by the `forge.data_service` label the platform owns.
        tracker.remove_created(client)
        tracker.__exit__(None, None, None)
        try:
            container.remove(v=True, force=True)
        except (NotFound, DockerException):
            pass


@pytest.fixture()
def scratch_postgres_url(real_service_postgres_admin_dsn):
    """a fresh, EMPTY database on the module's throwaway Postgres, as an SQLAlchemy URL
    (`postgresql+psycopg://...`), dropped (with `FORCE`, so a leaked connection cannot keep it)
    when the test ends. The platform-schema Docker tests (migration parity, the approval
    concurrency tests) build the schema on it with `alembic upgrade`, never `create_all`, so
    they exercise what production runs. The container itself, and its volume, belong to
    `real_service_postgres_admin_dsn` (removed with `v=True`)."""
    import psycopg
    from sqlalchemy.engine import make_url

    name = f"scratch_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(real_service_postgres_admin_dsn, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        url = make_url(real_service_postgres_admin_dsn).set(drivername="postgresql+psycopg", database=name)
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def make_build_with_request(db_session, seeded_ids):
    """a `Build` the way the API makes one -- with the `ChangeRequest`
    that carries its request, linked by `ChangeRequest.build_id`. The executor
    now refuses a build with no request, so any test that drives
    `SandboxBuildExecutor.execute` needs a real one rather than a bare row.

    Returns a factory; pass `project=` to add a build to an existing project,
    `created_at=` to order its change request against others, and
    `with_request=False` for a bare build (the fail-closed tests). The
    executor also refuses a build with no recorded engine (the worker records it
    at claim), so a build here carries `engine="anthropic"` unless a test passes
    another, or `engine=None` for the fail-closed case."""
    from datetime import datetime, timezone

    from app.models.build import Build
    from app.models.change_request import ChangeRequest
    from app.models.project import Project

    def _make(
        *,
        build_type: str = "create",
        nl_request: str = "Build a small to-do list service.",
        project=None,
        name: str = "request-project",
        build_id: str | None = None,
        created_at: datetime | None = None,
        with_request: bool = True,
        engine: str | None = "anthropic",
    ):
        if project is None:
            project = Project(
                name=name,
                owner_id=seeded_ids["user_id"],
                team_id=seeded_ids["team_id"],
                template_id=seeded_ids["template_id"],
            )
            db_session.add(project)
            db_session.flush()
        build = Build(project_id=project.id, type=build_type, engine=engine)
        if build_id is not None:
            build.id = build_id
        db_session.add(build)
        db_session.flush()
        if with_request:
            db_session.add(
                ChangeRequest(
                    project_id=project.id,
                    requester_id=seeded_ids["user_id"],
                    nl_request=nl_request,
                    build_id=build.id,
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
        db_session.commit()
        return build

    return _make


@pytest.fixture()
def silent_git_remote():
    """an HTTP "git host" that accepts every connection and never answers,
    which is how a hung host looks to `git fetch`/`git push`. Yields its base URL
    (`http://127.0.0.1:<port>`, no credentials: a test adds its own). Real git
    runs against it, so the timeout tests prove that a stalled remote is
    abandoned, not merely that a `timeout=` argument was passed. Connections are
    held open until teardown, then closed."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []

    def _accept_forever():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)  # never read, never answered

    threading.Thread(target=_accept_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        server.close()
        for connection in held:
            connection.close()


@pytest.fixture()
def returns_within():
    """`returns_within(seconds, fn)` runs `fn()` and returns its result, or
    raises `AssertionError` if it has not returned in that time. A test of "a hung
    git is abandoned" must FAIL, not hang the suite, when the property is broken:
    `fn` runs in a daemon thread, so a call that blocks forever is reported and left
    behind rather than waited on."""
    import threading

    def _call(seconds: float, fn):
        box: dict = {}

        def _run():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
                box["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(seconds)
        assert not worker.is_alive(), f"still blocked after {seconds}s"
        if "error" in box:
            raise box["error"]
        return box["result"]

    return _call


@pytest.fixture()
def pre_constraint_db(tmp_path):
    """a session on a file DB migrated to `a50f1d3c7e92` -- the last
    revision BEFORE `uq_change_requests_build_id` -- so a test can put two
    `ChangeRequest` rows on one build and watch a resolver refuse them. The
    suite's normal in-memory schema has the constraint, which makes that state
    unreachable there; this is the seam that reaches it without monkeypatching
    the query the code under test runs.

    Returns a namespace: `.session`, and `.make_build(...)` -- a build with
    `requesters` change requests (each by a distinct seeded user, starting `offset` into
    the email-sorted users, with one `nl_request` marker per row), returning `(build, [change_request_ids])`."""
    from types import SimpleNamespace

    from alembic import command

    from app.models.build import Build
    from app.models.change_request import ChangeRequest
    from app.models.project import Project
    from app.models.template import Template
    from app.models.user import Team, User
    from app.schema_check import alembic_config

    url = f"sqlite:///{(tmp_path / 'pre-constraint.db').as_posix()}"
    command.upgrade(alembic_config(url), "a50f1d3c7e92")
    engine = create_engine(url)
    # The ORM `Build` is the CURRENT model, so every `builds` column added after the
    # constraint revision has to exist for any query on it (the measured-change
    # columns). Added the way their migration adds them; a later `builds` column goes here too.
    with engine.begin() as conn:
        for column in ("changed_files", "protected_path_drift"):
            conn.exec_driver_sql(f"ALTER TABLE builds ADD COLUMN {column} JSON")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN agent_summary TEXT")
        for column in ("engine", "model"):
            conn.exec_driver_sql(f"ALTER TABLE builds ADD COLUMN {column} VARCHAR")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN cost_budget_usd FLOAT")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN smoke_checks JSON")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN preview_start_failure VARCHAR")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_routes JSON")
        # `projects.kind` is NOT NULL, so a column added after the fact needs a default.
        conn.exec_driver_sql("ALTER TABLE projects ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'normal'")
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    seed(session)
    users = list(session.query(User).order_by(User.email))

    def _make_build(*, requesters: int = 2, name: str = "ambiguous-project", offset: int = 0):
        project = Project(
            name=name,
            owner_id=session.query(User).filter_by(role="requester").first().id,
            team_id=session.query(Team).first().id,
            template_id=session.query(Template).first().id,
        )
        session.add(project)
        session.flush()
        build = Build(project_id=project.id, type="create", engine="anthropic")
        session.add(build)
        session.flush()
        crs = [
            ChangeRequest(
                project_id=project.id,
                requester_id=users[(offset + i) % len(users)].id,
                nl_request=f"SECRET-REQUEST-{i}",
                build_id=build.id,
            )
            for i in range(requesters)
        ]
        session.add_all(crs)
        session.commit()
        return build, [c.id for c in crs]

    try:
        yield SimpleNamespace(session=session, make_build=_make_build, users=users)
    finally:
        session.close()
        engine.dispose()
