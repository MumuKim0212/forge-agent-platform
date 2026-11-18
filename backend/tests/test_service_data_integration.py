"""Integration tests for the Postgres/Redis provisioning
(`app/services/service_data.py`, ADR 0023) against a REAL Postgres and a
REAL Docker Engine — no fakes for the SQL or the container mechanics.

Uses the shared `real_service_postgres_admin_dsn` fixture (conftest.py): a
throwaway `postgres:18-alpine` container, one per module, published on
loopback only. Skips cleanly (not fails) when Docker is unavailable.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException, NotFound  # noqa: E402

from app.services.service_data import (  # noqa: E402
    ServiceDataProvisionError,
    ensure_project_redis,
    issue_role,
    provision_project_data_services,
    provision_project_postgres,
    redis_container_name,
)


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
def project_id():
    return str(uuid.uuid4())


def _connect(database_url: str) -> psycopg.Connection:
    # DATABASE_URL is built for a container reaching the shared instance by
    # NAME on the standard port (services/service_data.py) — unreachable
    # from this HOST process, which instead needs the admin DSN's own
    # loopback host/port. Swap host:port back onto 127.0.0.1:<published
    # port> while keeping the role/password/database this function issued.
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    issued = make_url(database_url)
    reachable = issued.set(
        drivername="postgresql", host=admin_url.host, port=admin_url.port
    )
    return psycopg.connect(reachable.render_as_string(hide_password=False))


def test_fixture_admin_credential_is_not_a_superuser(real_service_postgres_admin_dsn):
    """(i1): the step-7 gate (2026-09-04) found every test in this module
    passing against `postgres` -- the throwaway container's bootstrap
    SUPERUSER -- rather than the CREATEDB CREATEROLE, NOSUPERUSER role ADR
    0023 §Amendment 5 requires and `dev.ps1` actually creates. Pin the
    FIXTURE's own shape here so a future simplification back to `postgres`
    (e.g. "just reuse the bootstrap user, it's simpler") regresses visibly
    in this one place instead of silently in every other test in this
    module."""
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    assert admin_url.username == "forge_admin"

    with psycopg.connect(admin_url.render_as_string(hide_password=False)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin "
                "FROM pg_roles WHERE rolname = current_user"
            )
            rolsuper, rolcreatedb, rolcreaterole, rolcanlogin = cur.fetchone()
            assert rolsuper is False
            assert rolcreatedb is True
            assert rolcreaterole is True
            assert rolcanlogin is True


def test_provision_creates_database_and_role_without_elevated_privileges(
    real_service_postgres_admin_dsn, project_id
):
    """(b1) Creates a database + a login role with a generated password.
    The role holds NEITHER CREATEDB NOR CREATEROLE NOR SUPERUSER — asserted
    by reading `pg_roles` back out, not by asserting the SQL string."""
    database_url = provision_project_postgres(project_id)
    assert database_url

    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database()")
            current_user, current_database = cur.fetchone()
            assert current_user == current_database  # role name == database name

            cur.execute(
                "SELECT rolcreatedb, rolcreaterole, rolsuper FROM pg_roles WHERE rolname = %s",
                (current_user,),
            )
            rolcreatedb, rolcreaterole, rolsuper = cur.fetchone()
            assert rolcreatedb is False
            assert rolcreaterole is False
            assert rolsuper is False

            # The role can actually use its own database (owner privileges).
            cur.execute("CREATE TABLE proof (id integer)")
            cur.execute("INSERT INTO proof (id) VALUES (1)")
            cur.execute("SELECT id FROM proof")
            assert cur.fetchone() == (1,)


def test_provision_public_cannot_connect_to_the_database(
    real_service_postgres_admin_dsn, project_id
):
    """ADR 0023 §Decision: `REVOKE CONNECT ... FROM PUBLIC` -- a role with
    no grant on this specific database cannot connect to it sideways."""
    from sqlalchemy.engine import make_url

    from app.config import settings

    database_url = provision_project_postgres(project_id)
    issued = make_url(database_url)

    admin_url = make_url(settings.service_postgres_admin_dsn)
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin_conn:
        with admin_conn.cursor() as cur:
            other_role = f"svc_outsider_{uuid.uuid4().hex[:8]}"
            cur.execute(
                f'CREATE ROLE "{other_role}" WITH LOGIN PASSWORD \'outsider-pw\' '
                "NOCREATEDB NOCREATEROLE NOSUPERUSER"
            )

    outsider_url = issued.set(
        drivername="postgresql",
        host=admin_url.host,
        port=admin_url.port,
        username=other_role,
        password="outsider-pw",
    )
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(outsider_url.render_as_string(hide_password=False))


# -- provisioning converges to the hardened state on every call ------------------------


def _admin_connection():
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    return psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True)


def _public_may_connect(database: str) -> bool:
    """Read from the catalog, as PUBLIC itself (grantee 0): a NULL ACL means the default one,
    which gives PUBLIC `CONNECT`."""
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database d, "
            "aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a "
            "WHERE d.datname = %s AND a.grantee = 0 AND a.privilege_type = 'CONNECT')",
            (database,),
        )
        return cur.fetchone()[0]


def _admin_holds_connect_directly(database: str) -> bool:
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database d, aclexplode(d.datacl) a "
            "WHERE d.datname = %s AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
            "AND a.privilege_type = 'CONNECT')",
            (database,),
        )
        return cur.fetchone()[0]


def _make_outsider(database: str) -> str:
    """A login role with no grant on the database, and a URL that reaches that database as it."""
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    role = f"svc_outsider_{uuid.uuid4().hex[:8]}"
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f'CREATE ROLE "{role}" WITH LOGIN PASSWORD \'outsider-pw\' NOCREATEDB NOCREATEROLE NOSUPERUSER'
        )
    return admin_url.set(
        drivername="postgresql", database=database, username=role, password="outsider-pw"
    ).render_as_string(hide_password=False)


def test_a_first_attempt_that_died_before_the_revoke_is_hardened_by_the_retry(
    real_service_postgres_admin_dsn, project_id, monkeypatch
):
    """`CONNECT` revoked from `PUBLIC` on every service database.
    The real first attempt runs until the hardening step and dies there, so the database exists
    and PUBLIC can still connect (both asserted, so the retry below meets the state this test is
    named for). The retry used to see "the database exists", reset the password and return:
    every role on the instance kept `CONNECT` for good. Runs as the non-superuser admin role the
    platform holds (`test_fixture_admin_credential_is_not_a_superuser`)."""
    import app.services.service_data as service_data

    name = service_data._project_identifier(project_id)

    real_harden = service_data._harden_database

    def _dies_before_the_revoke(conn, database, admin_username):
        raise RuntimeError("simulated worker death after CREATE DATABASE")

    monkeypatch.setattr(service_data, "_harden_database", _dies_before_the_revoke)
    with pytest.raises(ServiceDataProvisionError):
        provision_project_postgres(project_id)
    monkeypatch.setattr(service_data, "_harden_database", real_harden)

    assert _public_may_connect(name) is True  # the state the retry has to repair
    outsider = _make_outsider(name)
    with psycopg.connect(outsider):  # positive control: the exposure is real, not a catalog reading
        pass

    database_url = provision_project_postgres(project_id)

    assert _public_may_connect(name) is False
    assert _admin_holds_connect_directly(name) is True  # the grant the revoke must never strand
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(outsider)
    with _connect(database_url) as conn, conn.cursor() as cur:  # the owner still gets in, with the new password
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_a_database_created_directly_is_hardened_by_the_first_call_that_finds_it(
    real_service_postgres_admin_dsn, project_id
):
    """The other way to reach "the database exists": it was made by hand (or by an older version)
    exactly as provisioning makes one, with the default ACL."""
    import app.services.service_data as service_data
    from app.config import settings
    from sqlalchemy.engine import make_url

    name = service_data._project_identifier(project_id)
    admin_username = make_url(settings.service_postgres_admin_dsn).username
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE ROLE "{name}" WITH LOGIN PASSWORD \'old-pw\' NOCREATEDB NOCREATEROLE NOSUPERUSER')
        cur.execute(f'GRANT "{name}" TO "{admin_username}" WITH SET TRUE')
        cur.execute(f'CREATE DATABASE "{name}" OWNER "{name}"')
    assert _public_may_connect(name) is True

    provision_project_postgres(project_id)

    assert _public_may_connect(name) is False
    assert _admin_holds_connect_directly(name) is True


def test_provision_converges_when_the_admin_lacks_membership_of_the_role_on_a_retry(
    real_service_postgres_admin_dsn, project_id
):
    """A retry also restores the admin's membership of the service role (a later `DROP DATABASE`
    needs it), which the early return used to skip as well."""
    import app.services.service_data as service_data
    from app.config import settings
    from sqlalchemy.engine import make_url

    name = service_data._project_identifier(project_id)
    admin_username = make_url(settings.service_postgres_admin_dsn).username
    provision_project_postgres(project_id)
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(f'REVOKE "{name}" FROM "{admin_username}"')
        # The role's creator keeps an implicit membership (admin option only), so MEMBER stays
        # true; what the revoke removes, and the retry has to restore, is the SET and the inherited
        # (USAGE) privilege that `GRANT ... WITH SET TRUE` added.
        cur.execute("SELECT pg_has_role(current_user, %s, 'SET'), pg_has_role(current_user, %s, 'USAGE')", (name, name))
        assert cur.fetchone() == (False, False)  # the state the retry has to repair

    provision_project_postgres(project_id)

    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_has_role(current_user, %s, 'SET'), pg_has_role(current_user, %s, 'USAGE')",
            (name, name),
        )
        assert cur.fetchone() == (True, True)


def test_a_database_with_no_role_of_its_name_is_refused_not_adopted(real_service_postgres_admin_dsn, project_id):
    """Only a database this function made is converged. One that exists with no role of the same
    name belongs to someone else: it is refused and left exactly as it was."""
    import app.services.service_data as service_data

    name = service_data._project_identifier(project_id)
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')

    with pytest.raises(ServiceDataProvisionError, match="no role of that name"):
        provision_project_postgres(project_id)

    assert _public_may_connect(name) is True  # untouched
    with _admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (name,))
        assert cur.fetchone() == (0,)


def test_provision_is_defensively_idempotent_against_an_existing_database(
    real_service_postgres_admin_dsn, project_id
):
    """(b1/b4's crash-retry backstop): calling `provision_project_postgres`
    again for a project whose database ALREADY exists does not error — it
    resets the role's password (via ALTER, never DROP/CREATE) rather than
    fail. This is the narrow "a previous attempt got this far but never
    persisted anywhere reachable" case described in the function's
    docstring, not the ordinary redeploy path (which never reaches this
    function twice at all -- see ensure_service_env's own caching, tested
    in tests/test_deploy_service.py)."""
    first_url = provision_project_postgres(project_id)
    second_url = provision_project_postgres(project_id)

    assert first_url != second_url  # password rotated on the retry path
    from app.services.service_data import _project_identifier

    assert _public_may_connect(_project_identifier(project_id)) is False  # and still hardened
    # The SECOND (latest) credential works.
    with _connect(second_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


def test_provision_failure_after_role_creation_converges_on_retry(
    real_service_postgres_admin_dsn, project_id, monkeypatch
):
    """(i4): the step-7 gate's failure (2026-09-04) created the role, then
    failed on `CREATE DATABASE`, and the orphan role survived with no
    cleanup and no retry path. Neither rollback nor rotation is built here
    ((b6) forbids `DROP ROLE`; (b3) is a different function) -- instead, a
    RETRY converges: induce the exact same failure shape (role created,
    database step raises) via a monkeypatch on `_create_database_owned_by`
    -- the one statement split out for this purpose, everything else in
    the call is real Postgres SQL -- then call `provision_project_postgres`
    again with the patch removed and assert it completes rather than
    erroring on "role already exists"."""
    import app.services.service_data as service_data

    real_create_database = service_data._create_database_owned_by

    def _boom(conn, name):
        raise RuntimeError("simulated failure between role and database creation")

    monkeypatch.setattr(service_data, "_create_database_owned_by", _boom)
    with pytest.raises(ServiceDataProvisionError):
        provision_project_postgres(project_id)
    monkeypatch.setattr(service_data, "_create_database_owned_by", real_create_database)

    name = service_data._project_identifier(project_id)
    from sqlalchemy.engine import make_url

    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            # The observable this test is FOR: the role from the failed
            # attempt survived (the gate's bug, restated as a fact rather
            # than fixed by deleting it) and no database exists yet.
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (name,))
            assert cur.fetchone() == (1,)
            cur.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() == (0,)

    # The retry converges without manual intervention.
    database_url = provision_project_postgres(project_id)
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)

    # Exactly ONE role with this project's name exists afterward -- the
    # retry reused it rather than leaving the orphan behind a second,
    # differently-named role.
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (name,))
            assert cur.fetchone() == (1,)


def test_create_database_runs_autocommit_against_a_real_postgres(
    real_service_postgres_admin_dsn, project_id
):
    """(b2) `CREATE DATABASE` cannot run inside a transaction block -- this
    whole module's provisioning succeeding at all against a REAL Postgres
    instance (not a fake) is the proof the connection used for it is
    genuinely autocommit."""
    database_url = provision_project_postgres(project_id)
    with _connect(database_url):
        pass  # connecting at all proves the database exists and is usable


def test_issue_role_is_rerunnable_and_adds_a_second_usable_credential(
    real_service_postgres_admin_dsn, project_id
):
    """(b3) Issuing a role is re-runnable against an
    existing database. Creates a table through credential #1, issues #2,
    and reads that table through #2 -- proving #2 is a genuinely separate,
    usable credential on the SAME data, not a rotation of #1."""
    first_url = provision_project_postgres(project_id)
    with _connect(first_url) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE shared_proof (id integer)")
            cur.execute("INSERT INTO shared_proof (id) VALUES (42)")
            conn.commit()

    second_role = f"svc_second_{uuid.uuid4().hex[:8]}"
    second_url = issue_role(project_id, role_name=second_role)
    assert second_url != first_url

    with _connect(second_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM shared_proof")
            assert cur.fetchone() == (42,)

    # credential #1 is untouched -- issuing #2 touched no data.
    with _connect(first_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM shared_proof")
            assert cur.fetchone() == (42,)


def test_issue_role_requires_an_already_provisioned_database(
    real_service_postgres_admin_dsn, project_id
):
    with pytest.raises(ServiceDataProvisionError, match="does not exist"):
        issue_role(project_id, role_name="orphan-role")


# The command line a project Redis must run with, written out as a literal
# (a test must not share its source of truth with the code under
# test).
def _expected_redis_command(password: str) -> list[str]:
    return [
        "redis-server",
        "--save",
        "",
        "--requirepass",
        password,
        "--rename-command",
        "REPLICAOF",
        "",
        "--rename-command",
        "SLAVEOF",
        "",
        "--rename-command",
        "MIGRATE",
        "",
        "--rename-command",
        "MODULE",
        "",
    ]


def _password_in(redis_url: str) -> str:
    from urllib.parse import urlsplit

    password = urlsplit(redis_url).password
    assert password, f"REDIS_URL carries no password: {redis_url!r}"
    return password


def _redis_cli(container, *args: str, password: str | None = None) -> str:
    """Runs `redis-cli` INSIDE the Redis container (the only place a Redis
    with no published port and no network is reachable) and returns what it
    printed, stderr included."""
    command = ["redis-cli"]
    if password is not None:
        command += ["-a", password, "--no-auth-warning"]
    _exit_code, output = container.exec_run(command + list(args))
    return output.decode(errors="replace")


def _remove_redis(docker_client, project_id: str) -> None:
    try:
        docker_client.containers.get(redis_container_name(project_id)).remove(v=True, force=True)
    except (NotFound, DockerException):
        pass


def test_ensure_project_redis_creates_a_hardened_container(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """(b5) The per-project Redis container carries the SAME hardening set
    as `DeployManager.start_service`'s L4 service containers, no published
    port, `--save ""`, and a password, the disabled server-to-server
    commands, the `unless-stopped` restart policy, and -- the point -- no
    network at all until a deployment attaches it: never the default bridge."""
    redis_url = ensure_project_redis(project_id, docker_client=docker_client)
    password = _password_in(redis_url)
    assert redis_url == f"redis://:{password}@forge-service-redis-{project_id}:6379/0"

    from app.config import settings

    container = docker_client.containers.get(redis_container_name(project_id))
    attrs = container.attrs
    host_config = attrs["HostConfig"]
    try:
        assert container.status == "running"
        assert host_config["Privileged"] is False
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["Memory"] == settings.sandbox_memory_limit_mb * 1024 * 1024
        assert host_config["PidsLimit"] == settings.sandbox_pids_limit
        assert not host_config.get("Binds")  # volumes={} -> no bind mounts
        assert not host_config.get("PortBindings")  # no published port at all
        assert attrs["Config"]["Labels"]["forge.project_id"] == project_id
        assert attrs["Args"] == _expected_redis_command(password)
        # restarts with the daemon, like the service container.
        assert host_config["RestartPolicy"]["Name"] == "unless-stopped"
        # not on the default bridge -- and on no network at all yet.
        assert "bridge" not in attrs["NetworkSettings"]["Networks"]
        assert attrs["NetworkSettings"]["Networks"] == {}
    finally:
        container.remove(v=True, force=True)


def test_redis_refuses_an_unauthenticated_client_and_the_server_to_server_commands(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """The auth criterion: a connection without the password is
    refused, and REPLICAOF / SLAVEOF / MIGRATE / MODULE are refused EVEN WITH
    it -- the generated service can use Redis, and cannot use it to copy data
    out. Measured against the pinned image, driven through `redis-cli` inside
    the container."""
    redis_url = ensure_project_redis(project_id, docker_client=docker_client)
    password = _password_in(redis_url)
    container = docker_client.containers.get(redis_container_name(project_id))
    try:
        # Without the password: nothing works.
        assert "NOAUTH" in _redis_cli(container, "PING")
        assert "NOAUTH" in _redis_cli(container, "SET", "k", "v")
        # With a wrong one: refused too.
        assert "WRONGPASS" in _redis_cli(
            container, "-a", "not-the-password", "--no-auth-warning", "PING"
        )

        # With the right one: a normal Redis. (The control that makes the
        # refusals below mean something: the connection itself is fine.)
        assert "PONG" in _redis_cli(container, "PING", password=password)
        assert "OK" in _redis_cli(container, "SET", "caseac", "value", password=password)
        assert "value" in _redis_cli(container, "GET", "caseac", password=password)

        # With the right one, the four commands are gone -- in any letter case.
        for command in (
            ("REPLICAOF", "10.255.255.1", "6379"),
            ("SLAVEOF", "10.255.255.1", "6379"),
            ("replicaof", "10.255.255.1", "6379"),
            ("MIGRATE", "10.255.255.1", "6379", "caseac", "0", "100"),
            ("MODULE", "LIST"),
            ("MODULE", "LOAD", "/tmp/x.so"),
        ):
            out = _redis_cli(container, *command, password=password)
            assert "unknown command" in out, f"{command!r} was not refused: {out!r}"
    finally:
        container.remove(v=True, force=True)


def test_ensure_project_redis_is_idempotent_per_project(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """(b4, Redis half): handed back the URL it issued, a second call reuses
    the existing container and the same password rather than recreating it.
    (the stored URL is what carries the password between calls, which
    is what `ensure_service_env` does on every deployment start.)"""
    first_url = ensure_project_redis(project_id, docker_client=docker_client)
    container = docker_client.containers.get(redis_container_name(project_id))
    first_id = container.id

    second_url = ensure_project_redis(project_id, docker_client=docker_client, redis_url=first_url)
    same_container = docker_client.containers.get(redis_container_name(project_id))

    try:
        assert same_container.id == first_id
        assert second_url == first_url
    finally:
        same_container.remove(v=True, force=True)


def test_ensure_project_redis_without_the_stored_url_cannot_reuse_a_container(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """With no stored URL the password of an existing container is unknown,
    so it cannot be handed to anyone: the container is replaced by one with a
    fresh password, never reused with a credential nobody holds."""
    first_url = ensure_project_redis(project_id, docker_client=docker_client)
    first_id = docker_client.containers.get(redis_container_name(project_id)).id
    try:
        second_url = ensure_project_redis(project_id, docker_client=docker_client)
        second = docker_client.containers.get(redis_container_name(project_id))
        assert second.id != first_id
        assert _password_in(second_url) != _password_in(first_url)
        assert second.attrs["Args"] == _expected_redis_command(_password_in(second_url))
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_starts_a_stopped_container_and_keeps_it(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """Re-ensure (a): a Redis that exists but is stopped is started
    again -- same container, same password."""
    url = ensure_project_redis(project_id, docker_client=docker_client)
    container = docker_client.containers.get(redis_container_name(project_id))
    original_id = container.id
    try:
        container.stop(timeout=2)
        container.reload()
        assert container.status != "running"

        again = ensure_project_redis(project_id, docker_client=docker_client, redis_url=url)

        container = docker_client.containers.get(redis_container_name(project_id))
        assert container.id == original_id
        assert container.status == "running"
        assert again == url
        assert "bridge" not in container.attrs["NetworkSettings"]["Networks"]
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_recreates_a_missing_container_with_the_stored_password(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """Re-ensure (b): a Redis that was removed is created again, and
    the service's stored `REDIS_URL` stays valid -- the password is the
    stored one, not a new one."""
    url = ensure_project_redis(project_id, docker_client=docker_client)
    first_id = docker_client.containers.get(redis_container_name(project_id)).id
    _remove_redis(docker_client, project_id)
    try:
        again = ensure_project_redis(project_id, docker_client=docker_client, redis_url=url)

        container = docker_client.containers.get(redis_container_name(project_id))
        assert container.id != first_id
        assert container.status == "running"
        assert again == url
        assert "PONG" in _redis_cli(container, "PING", password=_password_in(url))
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_starts_a_container_that_was_created_but_never_started(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """Re-ensure (a created-but-never-started container): `docker
    create` records an endpoint on the default bridge in the container's
    config, so simply starting it would put it ON the bridge. It must come up
    running, with the bridge endpoint gone -- the sequence this code exists to
    get right."""
    from app.config import settings

    password = "created-never-started-pw"
    url = f"redis://:{password}@{redis_container_name(project_id)}:6379/0"
    container = docker_client.containers.create(
        image=settings.service_redis_image,
        name=redis_container_name(project_id),
        command=_expected_redis_command(password),
        restart_policy={"Name": "unless-stopped"},
        labels={"forge.data_service": "redis", "forge.project_id": project_id},
    )
    try:
        container.reload()
        assert container.status == "created"
        assert "bridge" in container.attrs["NetworkSettings"]["Networks"]  # the trap

        again = ensure_project_redis(project_id, docker_client=docker_client, redis_url=url)

        container = docker_client.containers.get(redis_container_name(project_id))
        assert container.status == "running"
        assert again == url
        assert "bridge" not in container.attrs["NetworkSettings"]["Networks"]
        assert "PONG" in _redis_cli(container, "PING", password=password)
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_replaces_a_legacy_container(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """a Redis from before this task -- no password, on the default
    bridge, no restart policy -- is replaced by the hardened one, and the URL
    handed back carries a NEW password (the legacy URL had none)."""
    from app.config import settings

    legacy_url = f"redis://{redis_container_name(project_id)}:6379/0"
    legacy = docker_client.containers.run(
        image=settings.service_redis_image,
        name=redis_container_name(project_id),
        command=["redis-server", "--save", ""],
        detach=True,
        labels={"forge.data_service": "redis", "forge.project_id": project_id},
    )
    try:
        legacy.reload()
        assert "bridge" in legacy.attrs["NetworkSettings"]["Networks"]  # the legacy shape
        legacy_id = legacy.id

        upgraded = ensure_project_redis(
            project_id, docker_client=docker_client, redis_url=legacy_url
        )

        assert upgraded != legacy_url
        password = _password_in(upgraded)
        replacement = docker_client.containers.get(redis_container_name(project_id))
        assert replacement.id != legacy_id
        assert replacement.status == "running"
        assert replacement.attrs["Args"] == _expected_redis_command(password)
        assert replacement.attrs["NetworkSettings"]["Networks"] == {}
        assert replacement.attrs["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_never_removes_a_container_that_is_not_this_projects_redis(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """Teardown removes only what it positively owns: a container
    that merely has the Redis name -- no matching labels -- is neither reused
    nor removed. The call fails and the container is still there."""
    from app.config import settings

    stranger = docker_client.containers.run(
        image=settings.service_redis_image,
        name=redis_container_name(project_id),
        detach=True,
        labels={"owner": "somebody-else"},
    )
    try:
        with pytest.raises(ServiceDataProvisionError, match="not project .* Redis"):
            ensure_project_redis(project_id, docker_client=docker_client)
        assert docker_client.containers.get(redis_container_name(project_id)).id == stranger.id
    finally:
        _remove_redis(docker_client, project_id)


def test_ensure_project_redis_requires_a_docker_client(real_service_postgres_admin_dsn, project_id):
    with pytest.raises(ServiceDataProvisionError, match="Docker client"):
        ensure_project_redis(project_id, docker_client=None)


def test_provision_project_data_services_returns_both_urls(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """The combined entry point `ensure_service_env` actually calls."""
    result = provision_project_data_services(project_id, docker_client=docker_client)
    try:
        assert set(result) == {"DATABASE_URL", "REDIS_URL"}
        assert result["DATABASE_URL"]
        assert result["REDIS_URL"] == (
            f"redis://:{_password_in(result['REDIS_URL'])}@forge-service-redis-{project_id}:6379/0"
        )
        with _connect(result["DATABASE_URL"]):
            pass
    finally:
        try:
            docker_client.containers.get(redis_container_name(project_id)).remove(v=True, force=True)
        except (NotFound, DockerException):
            pass
