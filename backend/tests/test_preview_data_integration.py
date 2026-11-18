"""Integration tests for the ephemeral preview data services
(`app/services/preview_data.py`, ADR 0023 §Amendment 2) against a REAL
Postgres and a REAL Docker Engine -- no fakes for the SQL or the container
mechanics.

Uses the shared `real_service_postgres_admin_dsn` fixture (conftest.py): a
throwaway `postgres:18-alpine` container, one per module, published on
loopback only, exposing the non-superuser `forge_admin` credential ADR
0023 §Amendment 5 actually specifies -- so every test here also proves
(a3) provisioning works against that credential, not a superuser one.
Skips cleanly (not fails) when Docker is unavailable.

Naming/guard unit tests that need neither Docker nor Postgres live in
`test_preview_data.py`.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException, NotFound  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import preview_data  # noqa: E402
from app.services.preview_data import (  # noqa: E402
    drop_preview_data_services,
    preview_redis_container_name,
    provision_preview_data_services,
)
from tests.docker_volumes import anonymous_volume_names, volume_exists  # noqa: E402


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


@pytest.fixture()
def docker_client():
    return docker.from_env()


@pytest.fixture()
def build_id():
    return str(uuid.uuid4())


def _connect(database_url: str) -> psycopg.Connection:
    # DATABASE_URL is built (service_data._database_url, reused here) for a
    # container reaching the shared instance by NAME -- unreachable from
    # this HOST process, which instead needs the admin DSN's own loopback
    # host/port. Swap host:port back onto 127.0.0.1:<published port> while
    # keeping the role/password/database this function issued. Mirrors
    # test_service_data_integration.py's own `_connect` helper exactly.
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    issued = make_url(database_url)
    reachable = issued.set(drivername="postgresql", host=admin_url.host, port=admin_url.port)
    return psycopg.connect(reachable.render_as_string(hide_password=False))


def test_provision_preview_postgres_role_has_no_elevated_privileges(
    real_service_postgres_admin_dsn, build_id, docker_client
):
    """(a1): read `pg_roles` back rather than assert the SQL string."""
    try:
        issued = provision_preview_data_services(build_id, docker_client=docker_client)
        conn = _connect(issued["DATABASE_URL"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rolcreatedb, rolcreaterole, rolsuper FROM pg_roles WHERE rolname = %s",
                    (preview_data._preview_identifier(build_id),),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row == (False, False, False)
    finally:
        drop_preview_data_services(build_id, docker_client=docker_client)


def test_provision_preview_postgres_database_is_empty(
    real_service_postgres_admin_dsn, build_id, docker_client
):
    """(a2): no schema is created -- a fresh preview database has no user
    tables at all."""
    try:
        issued = provision_preview_data_services(build_id, docker_client=docker_client)
        conn = _connect(issued["DATABASE_URL"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
                )
                (count,) = cur.fetchone()
        finally:
            conn.close()
        assert count == 0
    finally:
        drop_preview_data_services(build_id, docker_client=docker_client)


def test_ensure_preview_redis_has_the_same_hardening_set_and_distinct_labels(
    real_service_postgres_admin_dsn, build_id, docker_client
):
    """(a4): same hardening posture as `service_data.ensure_project_redis`'s
    container, labelled/named distinctly from a service's own."""
    try:
        issued = provision_preview_data_services(build_id, docker_client=docker_client)
        name = preview_redis_container_name(build_id)
        assert not name.startswith("forge-service-redis")

        container = docker_client.containers.get(name)
        attrs = container.attrs
        host_config = attrs["HostConfig"]
        assert host_config["Privileged"] is False
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["Memory"] > 0
        assert host_config["NanoCpus"] > 0
        assert host_config["PidsLimit"] > 0
        assert not host_config.get("PortBindings")
        assert not host_config.get("Binds")
        labels = attrs["Config"]["Labels"]
        assert labels["forge.data_service"] == "preview-redis"
        assert labels["forge.data_service"] != "redis"
        assert labels["forge.build_id"] == build_id

        # ADR 0023 §Amendment 12: the same isolation as a service's
        # Redis -- never on the default bridge (and on no network at all
        # until `PreviewManager` attaches it to its own preview's), a
        # password in the REDIS_URL it hands out, the server-to-server
        # commands refused. No restart policy: its consumer has none.
        assert "bridge" not in attrs["NetworkSettings"]["Networks"]
        assert attrs["NetworkSettings"]["Networks"] == {}
        assert host_config["RestartPolicy"]["Name"] in ("", "no")
        from urllib.parse import urlsplit

        password = urlsplit(issued["REDIS_URL"]).password
        assert password
        assert issued["REDIS_URL"] == (
            f"redis://:{password}@forge-preview-redis-{build_id}:6379/0"
        )
        assert "NOAUTH" in container.exec_run(["redis-cli", "PING"])[1].decode()
        assert "PONG" in container.exec_run(
            ["redis-cli", "-a", password, "--no-auth-warning", "PING"]
        )[1].decode()
        for command in (
            ["REPLICAOF", "10.255.255.1", "6379"],
            ["MIGRATE", "10.255.255.1", "6379", "k", "0", "100"],
            ["MODULE", "LIST"],
        ):
            out = container.exec_run(
                ["redis-cli", "-a", password, "--no-auth-warning", *command]
            )[1].decode()
            assert "unknown command" in out, f"{command!r} was not refused: {out!r}"
    finally:
        drop_preview_data_services(build_id, docker_client=docker_client)


def test_drop_preview_data_services_removes_the_redis_anonymous_volume(
    real_service_postgres_admin_dsn, build_id, docker_client, monkeypatch
):
    """a preview's Redis container gets an anonymous volume from the
    image's `VOLUME /data`; reaping the preview must take it too, or every
    preview leaks one. Names are captured before the drop and must be gone
    after it.

    The default `redis:8-alpine` declares no `VOLUME` (measured 2026-10-07),
    so it has no anonymous volume to leak; `redis:7-alpine` does (`VOLUME
    /data`), and `SERVICE_REDIS_IMAGE` is operator-configurable, so the test
    pins the image that makes the leak possible."""
    monkeypatch.setattr(settings, "service_redis_image", "redis:7-alpine")
    provision_preview_data_services(build_id, docker_client=docker_client)
    container = docker_client.containers.get(preview_redis_container_name(build_id))
    volumes = anonymous_volume_names(container)
    assert volumes, "the Redis container must hold an anonymous volume, or this test proves nothing"
    assert all(volume_exists(docker_client, v) for v in volumes)

    try:
        drop_preview_data_services(build_id, docker_client=docker_client)

        leaked = [v for v in volumes if volume_exists(docker_client, v)]
        assert leaked == [], f"anonymous volumes outlived the container: {leaked}"
    finally:
        for v in volumes:  # a regression must not also leak a volume per run
            try:
                docker_client.volumes.get(v).remove(force=True)
            except DockerException:
                pass


def test_drop_preview_data_services_is_idempotent(
    real_service_postgres_admin_dsn, build_id, docker_client
):
    """(b2): dropping twice, or for a build id that never provisioned
    anything, is a silent no-op."""
    from app.config import settings

    provision_preview_data_services(build_id, docker_client=docker_client)
    drop_preview_data_services(build_id, docker_client=docker_client)
    drop_preview_data_services(build_id, docker_client=docker_client)  # second call: nothing left
    drop_preview_data_services(str(uuid.uuid4()), docker_client=docker_client)  # never had one

    name = preview_data._preview_identifier(build_id)
    admin_conn = psycopg.connect(settings.service_postgres_admin_dsn)
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() is None
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            assert cur.fetchone() is None
    finally:
        admin_conn.close()
    with pytest.raises(NotFound):
        docker_client.containers.get(preview_redis_container_name(build_id))


# -- the Postgres half rolls itself back on its OWN failure -----------------------------------


def _preview_role_and_database_exist(build_id: str) -> tuple[bool, bool]:
    """(role exists, database exists), read off the module's throwaway admin DSN."""
    name = preview_data._preview_identifier(build_id)
    conn = psycopg.connect(settings.service_postgres_admin_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            role = cur.fetchone() is not None
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            database = cur.fetchone() is not None
    finally:
        conn.close()
    return role, database


@pytest.mark.parametrize(
    "failing_statement",
    ["GRANT", "CREATE DATABASE", "REVOKE CONNECT"],
    ids=["after-the-role", "after-the-role-and-grant", "after-the-database"],
)
def test_a_failure_part_way_through_the_postgres_half_leaves_no_role_and_no_database(
    real_service_postgres_admin_dsn, build_id, monkeypatch, failing_statement
):
    """The four statements are separate autocommit transactions. A failure at the 2nd, 3rd or 4th used
    to leave what the earlier ones made: a login role, or a role and an EMPTY database whose credentials
    nobody holds but which stays reachable by name until the hourly orphan sweep. Each statement is made
    to fail in turn, against a real Postgres, and the role and database are read back."""
    real_text = preview_data.text

    def text_that_fails_on(statement, *args, **kwargs):
        if str(statement).startswith(failing_statement):
            raise RuntimeError(f"forced failure at {failing_statement}")
        return real_text(statement, *args, **kwargs)

    monkeypatch.setattr(preview_data, "text", text_that_fails_on)

    with pytest.raises(preview_data.PreviewDataProvisionError, match="Postgres database/role failed"):
        preview_data._provision_preview_postgres(build_id)
    monkeypatch.undo()

    assert _preview_role_and_database_exist(build_id) == (False, False)


def test_the_non_vacuity_of_the_test_above_a_successful_provision_does_leave_both(
    real_service_postgres_admin_dsn, build_id, docker_client
):
    """Positive control: with nothing forced to fail, the same call leaves a role AND a database -- so
    the (False, False) above is the rollback, not a provisioning that never got as far as creating them."""
    try:
        preview_data._provision_preview_postgres(build_id)

        assert _preview_role_and_database_exist(build_id) == (True, True)
    finally:
        drop_preview_data_services(build_id, docker_client=docker_client)
