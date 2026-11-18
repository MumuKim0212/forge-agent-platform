"""Per-project data-service provisioning (ADR 0023).

Owns the platform's ADMIN-side half of ADR 0023: creating (once, lazily, at
first deploy) one project's own Postgres database + login role on the
shared instance, and one project's own Redis container. `services.deploy.
ensure_service_env` is the only production caller — provisioning is part of
the service env, not a second lifecycle (ADR 0023 §Provisioning lifecycle).

**Postgres** is operator-provisioned (ADR 0023 §Amendment 8): this module
never creates or destroys the shared INSTANCE, only a database and a role
inside it, reached over `settings.service_postgres_admin_dsn`. `CREATE
DATABASE` cannot run inside a transaction block, so every admin connection
here is explicit AUTOCOMMIT.

**Redis** is platform-owned end-to-end (ADR 0023 §Amendment 8's second
reason: a cache costs nothing to recreate): this module creates the
container itself, via the Docker client `ensure_service_env`'s caller
already holds (the `DeployManager`'s own client — see that module's
docstring for why `getattr(manager, "_client", None)` is the shape).

**ADR 0023 §Amendment 12 -- what a Redis is, and how it is kept so.**
Never on Docker's default bridge at any moment it runs; password-protected,
with `REPLICAOF`/`SLAVEOF`/`MIGRATE`/`MODULE` disabled; `unless-stopped`; and
**re-ensured at every deployment start** rather than trusted from the stored
env (`ensure_project_redis` is called on the short-circuit path of
`ensure_service_env` too). The one container-creation path is
`create_isolated_redis`, shared with the preview store (`preview_data.py`).

Nothing in this module ever runs `DROP DATABASE` or `DROP ROLE` (ADR 0023:
"never deleted implicitly"). It removes a Redis container in exactly one
case: `ensure_project_redis` replacing the project's OWN container that is
not the hardened one (a container created before the hardening) or will not start -- a cache
recreated, not data deleted (ADR 0023 §Amendment 8, §Amendment 12), and
never a container that lacks this project's labels. The staged zombie
path's automatic stages stop at flag/
stop/archive and never delete; actual destruction is a separate, explicit,
admin-only operator action (`services/service_data_deprovision.py`)
that this module does not implement and is never called by.

**Read this before "fixing" either half separately.**
`provision_project_postgres` MAY reset a role's password (via `ALTER ROLE`,
never `DROP`/`CREATE`) when it is actually called a second time for the same
project — a role whose password the platform never persisted anywhere
cannot be recovered any other way, and that is exactly what lets a retry
after a crash (i4) converge without a human dropping anything first. This
does NOT make an ordinary REDEPLOY reissue a credential: `ensure_service_env`
(`services/deploy.py`) never calls this module a second time once BOTH
`DATABASE_URL`/`REDIS_URL` are already stored in a project's encrypted
service env, so the "second deploy keeps the same database, role and
password" guarantee lives one layer up, in `ensure_service_env`'s own
caching — not in this module refusing to reset a password. The two
properties look like they contradict (it is easy to
treat them as if they did); they don't, because they are enforced at
different layers. Fixing one without checking the other breaks it: making
this module refuse to ever reset a password would strand a crash-orphaned
role forever (breaks i4); making `ensure_service_env` re-provision on every
deploy would reissue credentials that TEMPLATE code has already
written to disk (breaks the redeploy guarantee this paragraph exists to
name). The redeploy-side guarantee is pinned by
`tests/test_deploy_service.py::test_redeploy_reuses_data_services_and_does_not_regenerate_password`,
not by anything in this file.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from urllib.parse import urlsplit

from docker.errors import DockerException, NotFound
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from app.config import settings
from app.services.platform_labels import Role, platform_labels

logger = logging.getLogger("app.service_data")

# ADR 0023 §Amendment 6: the STANDARD Postgres port the shared
# instance listens on INSIDE the Docker network every deployment attaches it
# to — distinct from whatever host port the admin DSN publishes for the
# platform's own loopback-only connection (ADR 0023 §Amendment 10), which is
# operator/dev.ps1 configuration and can be anything. A deployed SERVICE
# reaches the shared instance by CONTAINER NAME on this port, never through
# the admin DSN's host/port — see `_database_url` below.
_SERVICE_POSTGRES_PORT = 5432
# Standard Redis port inside the Docker network — no host port is ever
# published for a project's Redis container (ADR 0023 §Redis).
_SERVICE_REDIS_PORT = 6379

# ADR 0023 §Amendment 6: forced regardless of what scheme the
# operator writes in SERVICE_POSTGRES_ADMIN_DSN — bare `postgresql://`
# resolves to psycopg2 in SQLAlchemy, which this platform does not depend on
# (see pyproject.toml).
_ADMIN_DRIVER = "postgresql+psycopg"

REDIS_CONTAINER_PREFIX = "forge-service-redis"

# ADR 0023 §Amendment 12: the commands a generated service has no use
# for and that turn a cache into a copy channel or an exit -- replication
# (`REPLICAOF`/`SLAVEOF`), key migration to another server (`MIGRATE`), and
# loading native code (`MODULE`). Disabled with `--rename-command <name> ""`,
# which is the mechanism chosen by measurement on the pinned image (see
# `redis_server_command`), not by assumption.
_REDIS_DISABLED_COMMANDS = ("REPLICAOF", "SLAVEOF", "MIGRATE", "MODULE")

# The service Redis restarts with the Docker daemon, the way the service
# container does. A PREVIEW's Redis does not
# (`preview_data.py`): its consumer, the preview container, has no restart
# policy either, so a surviving preview store would be an orphan the sweep has
# to reap.
SERVICE_REDIS_RESTART_POLICY = {"Name": "unless-stopped"}

# Docker's fixed name for the default bridge network. It has egress NAT and
# reaches every other container on it, so no Redis may be attached to it at
# any moment it runs (ADR 0023 §Amendment 12).
_DEFAULT_BRIDGE = "bridge"

# An unreachable admin DSN (host down, wrong port, firewalled) must fail
# loudly rather than hang the worker process indefinitely — the worker is
# single-threaded per its queue model, so a stuck connect attempt
# here would silently stop every OTHER project's deploys from being
# claimed too. Only applied when the DSN itself does not already specify
# one (`_connect_args` below), so an operator can still choose their own.
_ADMIN_CONNECT_TIMEOUT_SECONDS = 10


class ServiceDataProvisionError(RuntimeError):
    """Any failure creating or reaching a project's database, role, or
    Redis container: PROVISION_FAILED.
    `services.deploy.execute_deployment` catches exactly this type (not a
    bare `Exception`) so an unrelated failure (e.g. a service-env
    decryption error) is not misclassified as a provisioning failure."""


# -- naming ------------------------------------------------------------


def _project_identifier(project_id: str) -> str:
    """Deterministic Postgres identifier for a project's OWN database AND
    role (the same name in both — roles and databases are separate object
    types in Postgres, so reusing one name across them is not a collision).
    `project_id` is a uuid4 (`models.base.new_id`) — hyphens become
    underscores for a safe bare identifier; `svc_` + 36 chars is
    comfortably under Postgres's 63-byte identifier limit."""
    return "svc_" + project_id.replace("-", "_")


def redis_container_name(project_id: str) -> str:
    """Public: `deploy_manager.py`'s attach/detach bracket (ADR 0023
    §Amendment 1) needs this same name to connect/disconnect the container
    from a deployment's network without importing this module's private
    Postgres naming scheme."""
    return f"{REDIS_CONTAINER_PREFIX}-{project_id}"


# -- SQL literal/identifier quoting -------------------------------------
#
# Every value quoted here is platform-generated (a uuid4-derived identifier
# or a `secrets.token_urlsafe` password) — never end-user input — so hand
# quoting is safe. `text()` bind parameters are deliberately NOT used for
# CREATE/ALTER ROLE's PASSWORD clause: PostgreSQL's grammar accepts only a
# string constant there (`Sconst`), not a bind-able expression, so a
# parameter placeholder in that position is a syntax error, not a safer
# substitute.


def _qi(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _ql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# -- admin connection -----------------------------------------------------


def _admin_url() -> URL:
    """The admin DSN as a SQLAlchemy URL, forced onto the psycopg (v3)
    driver. Raises `ServiceDataProvisionError` if unconfigured — this
    should be unreachable in production, since `services.deploy` checks
    `settings.service_postgres_configured` before ever calling into this
    module (refusal happens at confirm/execute, before any
    provisioning is attempted)."""
    if not settings.service_postgres_configured:
        raise ServiceDataProvisionError(
            "SERVICE_POSTGRES_ADMIN_DSN is not configured -- this should be "
            "unreachable in production (services.deploy checks "
            "settings.service_postgres_configured before calling this "
            "module); investigate the caller."
        )
    return make_url(settings.service_postgres_admin_dsn).set(drivername=_ADMIN_DRIVER)


def _connect_args(url: URL) -> dict:
    """`connect_timeout` unless the DSN already specifies its own (an
    explicit operator choice always wins)."""
    if url.query and "connect_timeout" in url.query:
        return {}
    return {"connect_timeout": _ADMIN_CONNECT_TIMEOUT_SECONDS}


def _database_url(*, role: str, password: str, database: str) -> str:
    """The `DATABASE_URL` handed to the DEPLOYED SERVICE — deliberately
    driver-agnostic (`postgresql://`, no `+driver` suffix): the template
    that will consume this picks its own driver, and this platform
    must not choose for it. Host is the shared instance's CONTAINER NAME
    (`settings.service_postgres_container_name`) at the standard internal
    port — the service reaches it by Docker network attachment
    (`deploy_manager.py`), never through the admin DSN's loopback endpoint.
    `password` is `secrets.token_urlsafe` output (URL-safe base64: letters,
    digits, `-`, `_` only), so it needs no percent-encoding here."""
    return (
        f"postgresql://{role}:{password}@"
        f"{settings.service_postgres_container_name}:{_SERVICE_POSTGRES_PORT}/{database}"
    )


# -- Postgres provisioning -------------------------------------------------


def _create_database_owned_by(conn, name: str) -> None:
    """Split out from `provision_project_postgres` as its own statement —
    both a readability seam and the exact point a
    test monkeypatches to induce "role created, database creation failed"
    without faking anything else in the function: role creation/granting
    above and the connection itself both stay real Postgres SQL either
    way."""
    conn.execute(text(f"CREATE DATABASE {_qi(name)} OWNER {_qi(name)}"))


def _harden_database(conn, name: str, admin_username: str | None) -> None:
    """The statements that make a service database what ADR 0023 says it is:
    the admin keeps `CONNECT` for its own housekeeping, then `PUBLIC` loses it,
    so no OTHER role can reach the database sideways.

    **Idempotent, and the last thing `provision_project_postgres` does on every
    call -- never behind an early return.** Postgres gives `PUBLIC`
    `CONNECT` on every new database, and `CREATE DATABASE` cannot be made to
    leave it out, so there is always a window between the create and the
    revoke. A first attempt that died inside that window used to leave the
    database open to every role on the instance for good: the retry saw "the
    database exists", reset the password and returned. Both statements are safe
    to repeat (a grant that is already held and a revoke of a privilege that is
    already gone are no-ops), and the grant comes first so the revoke can never
    leave the admin without a way in. Split out as its own function, as
    `_create_database_owned_by` is, so a test can make the first attempt die
    right here."""
    if admin_username:
        # The admin role itself needs to retain CONNECT for its OWN future
        # housekeeping on this database (issue_role's schema-level grants) —
        # ownership grants the SERVICE role's own access, not the admin's;
        # database privilege ACLs are independent of who issued CREATE DATABASE.
        conn.execute(text(f"GRANT CONNECT ON DATABASE {_qi(name)} TO {_qi(admin_username)}"))
    conn.execute(text(f"REVOKE CONNECT ON DATABASE {_qi(name)} FROM PUBLIC"))


def provision_project_postgres(project_id: str) -> str:
    """ADR 0023 §Decision (Postgres), §Amendment 5: creates — for a project
    that has none — a database and a login role with a generated password,
    makes the role the database's OWNER (Postgres grants an owner every
    privilege on its own database at creation, including `pg_database_
    owner` membership as of Postgres 15+, which is what lets the role
    `CREATE` inside its own `public` schema too — see `issue_role`'s
    docstring for the second-credential case), and revokes `PUBLIC`'s
    ability to connect to it (`_harden_database`), so no OTHER role can reach
    it sideways. The
    role is created WITHOUT `CREATEDB`, `CREATEROLE` or `SUPERUSER` —
    a test proves this by reading the role's attributes back out of
    `pg_roles`, not by asserting the SQL string.

    `CREATE DATABASE` cannot run inside a transaction block, so the
    connection used here is explicit AUTOCOMMIT.

    The admin connection is a `CREATEDB
    CREATEROLE` role, never `SUPERUSER` — a run against a real Postgres
    proved that credential hits `InsufficientPrivilege: must be able to SET
    ROLE "<service role>"` on plain `CREATE DATABASE ... OWNER <role>`. From
    Postgres 16, a `CREATEROLE` admin automatically gets `ADMIN OPTION` on
    a role it just created, but NOT `SET` — those became separate grantable
    options, and `CREATE DATABASE ... OWNER` needs `SET`. The fix, proven
    live against the dev instance during the gate: `GRANT "<role>" TO
    "<admin>" WITH SET TRUE` — the admin granting itself the missing option
    on the role it already administers — immediately before `CREATE
    DATABASE`. A superuser admin (never used in production) already
    bypasses this check, so the extra `GRANT` is a harmless no-op there.

    Idempotent against a database that already exists — the ordinary
    "second deploy reuses the database and role" case never reaches this
    function at all (`ensure_service_env` only calls it while `DATABASE_URL`
    is still absent from the stored, encrypted env; see that function's
    docstring). This existence check exists for the narrower crash-retry
    case: a previous provisioning attempt created the database/role but
    never got as far as persisting `DATABASE_URL` anywhere reachable (e.g.
    Redis creation failed right after). Nothing depends on that unrecorded
    password, so the only recoverable move is a FRESH one on the SAME
    role — via `ALTER ROLE`, not `DROP`/`CREATE` — touching no data.

    **one path, no early return.** Whether the database was already
    there or not, the call ends in the same statements, in the same order: the
    role is created or given a fresh password, the admin is granted the role,
    the database is created only if it is missing, and `_harden_database` runs
    LAST. It used to return as soon as it saw the database, which skipped the
    revoke for an attempt that had died between `CREATE DATABASE` and it and
    left `PUBLIC` with `CONNECT` on a production database indefinitely
. A database that exists with no role of
    this name is not provisioned over: it is not one this function made.

    A failed provision must leave nothing behind
    that blocks a retry. This function never runs `DROP ROLE` (b6), so
    "roll back" is not the shape chosen here — instead, a role that exists
    but has no matching database (the step-7 gate's own failure: role
    created, then `CREATE DATABASE` failed and the role survived) is
    treated exactly like the existing-database case one level down: reused
    with a fresh password via `ALTER ROLE`, not re-created. A retry after
    ANY failure between role creation and the final `REVOKE` therefore
    converges to a working database + role without a human dropping
    anything first.
    """
    name = _project_identifier(project_id)
    password = _secrets.token_urlsafe(24)
    url = _admin_url()
    admin_username = url.username

    try:
        engine = create_engine(url, connect_args=_connect_args(url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                database_exists = (
                    conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                    ).first()
                    is not None
                )
                role_exists = (
                    conn.execute(
                        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}
                    ).first()
                    is not None
                )
                if database_exists and not role_exists:
                    raise ServiceDataProvisionError(
                        f"database {name!r} exists but no role of that name does -- "
                        "this function only converges a database it created itself"
                    )
                if role_exists:
                    # (i4) retry-converge case: an earlier attempt created
                    # the role and then failed before CREATE DATABASE.
                    conn.execute(text(f"ALTER ROLE {_qi(name)} WITH PASSWORD {_ql(password)}"))
                else:
                    conn.execute(
                        text(
                            f"CREATE ROLE {_qi(name)} WITH LOGIN PASSWORD {_ql(password)} "
                            "NOCREATEDB NOCREATEROLE NOSUPERUSER"
                        )
                    )

                if admin_username:
                    # (i2) — see docstring above: required for a CREATEROLE
                    # (non-superuser) admin; harmless no-op for a superuser
                    # one. Idempotent on the retry-converge path too — a
                    # role already granted WITH SET TRUE can be granted it
                    # again with no error.
                    #
                    # this grant is a dependency for
                    # `service_data_deprovision._drop_service_postgres`'s
                    # later `DROP DATABASE` too, not just for the `CREATE
                    # DATABASE ... OWNER` below — but for a DIFFERENT half
                    # of it. `SET` (the option this GRANT adds) is what
                    # `CREATE DATABASE ... OWNER` needs; the plain
                    # MEMBERSHIP the same GRANT confers is what `DROP
                    # DATABASE` needs later (Postgres requires the
                    # executing role to own the database, or hold the
                    # owner's privileges via membership). Verified by
                    # REVOKE/GRANT against the real fixture, one grant at a
                    # time: revoking only
                    # this membership makes deprovisioning's `DROP
                    # DATABASE` fail; revoking only `SET` does not. Narrow
                    # or remove this grant only with that in mind.
                    conn.execute(
                        text(f"GRANT {_qi(name)} TO {_qi(admin_username)} WITH SET TRUE")
                    )
                if not database_exists:
                    _create_database_owned_by(conn, name)
                # Last, and on every call: see `_harden_database`.
                _harden_database(conn, name, admin_username)
        finally:
            engine.dispose()
    except ServiceDataProvisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure is PROVISION_FAILED
        raise ServiceDataProvisionError(
            f"provisioning project {project_id}'s Postgres database/role failed: {exc}"
        ) from exc

    return _database_url(role=name, password=password, database=name)


def issue_role(project_id: str, *, role_name: str) -> str:
    """re-runnable against an EXISTING
    database — calling this again with a NEW `role_name` adds a SECOND
    usable credential on the same database and touches no existing data.
    This is NOT rotation (nothing here
    implements the health-gated switchover) — it only proves the door
    stays open: a future value-rotation flow would call this to mint the
    new role before anything else moves.

    Requires the project's database to already exist — raises
    `ServiceDataProvisionError` otherwise. This is not the function that
    creates a project's FIRST role (`provision_project_postgres` is)."""
    database = _project_identifier(project_id)
    password = _secrets.token_urlsafe(24)
    admin_url = _admin_url()

    try:
        engine = create_engine(admin_url, connect_args=_connect_args(admin_url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
                ).first()
                if exists is None:
                    raise ServiceDataProvisionError(
                        f"project {project_id}'s database {database!r} does not exist -- "
                        "issue_role only adds a credential to an ALREADY-provisioned "
                        "database (provision_project_postgres creates the first one)."
                    )
                conn.execute(
                    text(
                        f"CREATE ROLE {_qi(role_name)} WITH LOGIN PASSWORD {_ql(password)} "
                        "NOCREATEDB NOCREATEROLE NOSUPERUSER"
                    )
                )
                conn.execute(text(f"GRANT CONNECT ON DATABASE {_qi(database)} TO {_qi(role_name)}"))
        finally:
            engine.dispose()

        # Schema-level grants run ON the project's own database, not the
        # admin's maintenance database — GRANT ON SCHEMA / ALL TABLES IN
        # SCHEMA are database-local, so this needs its own connection.
        db_url = admin_url.set(database=database)
        engine2 = create_engine(db_url, connect_args=_connect_args(db_url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine2.connect() as conn:
                conn.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {_qi(role_name)}"))
                conn.execute(
                    text(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {_qi(role_name)}")
                )
                conn.execute(
                    text(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        f"GRANT ALL ON TABLES TO {_qi(role_name)}"
                    )
                )
        finally:
            engine2.dispose()
    except ServiceDataProvisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure is PROVISION_FAILED
        raise ServiceDataProvisionError(
            f"issuing role {role_name!r} on project {project_id}'s database failed: {exc}"
        ) from exc

    return _database_url(role=role_name, password=password, database=database)


# -- Redis provisioning -----------------------------------------------------


class RedisNotIsolatedError(DockerException):
    """A Redis container is, or would be, attached to Docker's default
    bridge. A `DockerException` subclass on purpose: every caller already
    wraps `DockerException` into its own provisioning error, so "could not
    keep it off the default bridge" fails a provision exactly like "could
    not start it" -- there is no state in which it is tolerated."""


def generate_redis_password() -> str:
    """`secrets.token_urlsafe` output: letters, digits, `-` and `_` only, so
    it needs no percent-encoding inside a `redis://` URL."""
    return _secrets.token_urlsafe(24)


def redis_url_with_password(container_name: str, password: str) -> str:
    """The `REDIS_URL` handed to a service or preview. The empty username
    (`redis://:<password>@...`) is what redis-py and every other client read
    as "AUTH with the default user's password"."""
    return f"redis://:{password}@{container_name}:{_SERVICE_REDIS_PORT}/0"


def redis_password_from_url(redis_url: str | None) -> str | None:
    """The password carried by a stored `REDIS_URL`, or `None` for a
    password-less one (issued before Redis was password-protected) or no URL at all."""
    if not redis_url:
        return None
    try:
        return urlsplit(redis_url).password or None
    except ValueError:
        return None


def redis_server_command(password: str) -> list[str]:
    """The `redis-server` command line for a password-protected Redis with
    server-to-server commands disabled.

    **Mechanism chosen by measurement on the pinned image (`redis:8-alpine`,
    redis 8.10.1), not assumed:** `--requirepass` + `--rename-command <name>
    ""` on the command line. A client without the password gets `NOAUTH`; with
    it, `REPLICAOF`/`SLAVEOF`/`MIGRATE`/`MODULE` (including `MODULE LIST` /
    `MODULE LOAD`) answer `ERR unknown command`, in any letter case and from
    inside a Lua script too. An ACL (`-replicaof ...`) was rejected: a client
    holding the password could undo it with `ACL SETUSER default +replicaof`,
    whereas a rename is static configuration with no runtime way back.

    `redis-server` stays the first argument so the image's entrypoint takes
    the same branch it always has under `cap_drop=ALL`."""
    command = ["redis-server", "--save", "", "--requirepass", password]
    for name in _REDIS_DISABLED_COMMANDS:
        command += ["--rename-command", name, ""]
    return command


def _networks_of(container) -> dict:
    return (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}


def _detach_default_bridge(docker_client, container) -> None:
    """Drops the container's endpoint on the default bridge, if it has one.
    On a freshly `create`d, not-yet-started container this is what keeps it
    from ever being on the bridge while running: `docker create` records a
    bridge endpoint in the container's config, and `network disconnect`
    removes it before `start` allocates anything (measured: after
    create -> disconnect -> start the container has only `lo`, and a later
    `network connect` to a project network works as usual)."""
    container.reload()
    if _DEFAULT_BRIDGE in _networks_of(container):
        docker_client.networks.get(_DEFAULT_BRIDGE).disconnect(container)


def _require_off_default_bridge(container) -> None:
    """Executed boundary check (not a comment): after every create/start the
    container is re-read and refused if it is on the default bridge."""
    container.reload()
    if _DEFAULT_BRIDGE in _networks_of(container):
        raise RedisNotIsolatedError(
            f"Redis container {container.name!r} is attached to the default bridge"
        )


def create_isolated_redis(
    docker_client,
    *,
    name: str,
    labels: dict[str, str],
    password: str,
    restart_policy: dict | None = None,
):
    """Creates and starts one Redis container that has **never been on the
    default bridge while running**, is password-protected, and has the
    server-to-server commands disabled. Shared by the service Redis
    (`ensure_project_redis`) and the preview Redis (`preview_data`): the
    hardening is one code path, so it cannot drift between them.

    The container starts with no network at all and is attached to a project
    deployment's (or a preview's) private network afterwards, by
    `DeployManager`/`PreviewManager` -- the only networks it may be on. No
    network of its own is created for it (a per-project network would worsen
    Docker's address-pool limit). Same cap/limit hardening set as an L4
    service container; no published port; `--save ""` (a cache).

    Anything that goes wrong after the container exists removes it again, so
    a half-made Redis is never left behind. Raises `DockerException`
    (`RedisNotIsolatedError` included)."""
    kwargs: dict = {
        "image": settings.service_redis_image,
        "name": name,
        "command": redis_server_command(password),
        "mem_limit": f"{settings.sandbox_memory_limit_mb}m",
        "nano_cpus": int(settings.sandbox_cpu_limit_cores * 1_000_000_000),
        "pids_limit": settings.sandbox_pids_limit,
        "security_opt": ["no-new-privileges"],
        "cap_drop": ["ALL"],
        "privileged": False,
        "volumes": {},
        "labels": labels,
    }
    if restart_policy:
        kwargs["restart_policy"] = restart_policy
    container = docker_client.containers.create(**kwargs)
    try:
        _detach_default_bridge(docker_client, container)
        container.start()
        _require_off_default_bridge(container)
    except BaseException:
        try:
            container.remove(v=True, force=True)
        except DockerException:
            pass
        raise
    return container


def _redis_matches_spec(container, password: str) -> bool:
    """Whether an existing container is the Redis this module would create
    now: the exact command line (so the same password and the same disabled
    commands) and the restart policy. A container from before the hardening has none
    of that and fails this check, which is what makes it get replaced."""
    config = container.attrs.get("Config") or {}
    restart = ((container.attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
    return (
        config.get("Cmd") == redis_server_command(password)
        and restart == SERVICE_REDIS_RESTART_POLICY["Name"]
    )


def _is_this_projects_redis(container, project_id: str) -> bool:
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    return (
        labels.get("forge.data_service") == "redis" and labels.get("forge.project_id") == project_id
    )


def ensure_project_redis(project_id: str, *, docker_client, redis_url: str | None = None) -> str:
    """ADR 0023 §Redis, §Amendment 8, §Amendment 12:
    makes sure the project's own Redis container exists, is
    running, and is the hardened one, and returns the `REDIS_URL` to store.

    Called on **every deployment start** (`services.deploy.ensure_service_env`),
    not only at first issuance -- the stored env is not evidence that the
    container exists. `redis_url` is the value currently stored (or `None`
    on first issuance); its password is reused, so a redeploy never changes
    the credential a running service holds. With no usable password in it
    (first issuance, or a URL issued before passwords were added) a new one is generated and
    the returned URL differs from `redis_url` -- the caller stores it.

    - absent -> created (never on the default bridge, see
      `create_isolated_redis`);
    - present and exactly the container this module would create now ->
      kept as is, started if it is stopped or was never started, and taken
      off the default bridge if it is somehow on it;
    - present but not that (an older container: no password, on the
      default bridge, no restart policy), or one that will not start ->
      removed with its volumes and recreated. Redis is a cache the platform
      promises nothing durable about (ADR 0023 §Amendment 8), so recreation
      loses nothing it owes anyone.

    A container with this name that does not carry this project's
    `forge.data_service=redis` + `forge.project_id` labels is never removed:
    the call fails instead (teardown removes only what it positively owns).

    Raises `ServiceDataProvisionError` on any failure."""
    if docker_client is None:
        raise ServiceDataProvisionError(
            "ensure_project_redis called with no Docker client -- this "
            "should be unreachable in production (services.deploy passes "
            "the DeployManager's own client); investigate the caller."
        )
    name = redis_container_name(project_id)
    password = redis_password_from_url(redis_url) or generate_redis_password()
    # the platform's instance and role beside the two labels `_is_this_projects_redis`
    # reads. The reconciler never removes this container; it re-ensures it (it reaches this
    # function) for every project with an ACTIVE deployment.
    labels = platform_labels(
        Role.SERVICE_REDIS,
        project_id=project_id,
        extra={"forge.data_service": "redis"},
    )

    try:
        try:
            existing = docker_client.containers.get(name)
        except NotFound:
            existing = None

        if existing is not None:
            if not _is_this_projects_redis(existing, project_id):
                raise ServiceDataProvisionError(
                    f"a container named {name!r} exists but is not project {project_id}'s "
                    "Redis (its forge.data_service/forge.project_id labels do not match) "
                    "-- refusing to remove or reuse it."
                )
            if _redis_matches_spec(existing, password):
                try:
                    _detach_default_bridge(docker_client, existing)
                    if existing.status != "running":
                        existing.start()
                    _require_off_default_bridge(existing)
                    return redis_url_with_password(name, password)
                except DockerException as exc:
                    logger.warning(
                        "project %s: Redis container could not be (re)started (%s); recreating it",
                        project_id,
                        exc,
                    )
            existing.remove(v=True, force=True)

        create_isolated_redis(
            docker_client,
            name=name,
            labels=labels,
            password=password,
            restart_policy=SERVICE_REDIS_RESTART_POLICY,
        )
    except ServiceDataProvisionError:
        raise
    except DockerException as exc:
        raise ServiceDataProvisionError(
            f"ensuring project {project_id}'s Redis container failed: {exc}"
        ) from exc

    return redis_url_with_password(name, password)


# -- combined entry point (the one `services.deploy` calls) ---------------


def provision_project_data_services(project_id: str, *, docker_client=None) -> dict[str, str]:
    """ADR 0023 §Provisioning lifecycle: issues (or reuses) one
    project's OWN Postgres database + login role and its OWN Redis
    container, and returns `{"DATABASE_URL": ..., "REDIS_URL": ...}`.
    `services.deploy.ensure_service_env` is the only caller — provisioning
    is part of the service env, not a separate lifecycle. Only for a
    project with no usable stored env: the Redis half takes no stored URL,
    so it issues a fresh password (an already-issued env goes through
    `ensure_project_redis(..., redis_url=<stored>)` instead).

    Raises `ServiceDataProvisionError` on any failure — the caller maps
    that (and only that) to `DeploymentFailureReason.PROVISION_FAILED`."""
    database_url = provision_project_postgres(project_id)
    redis_url = ensure_project_redis(project_id, docker_client=docker_client)
    return {"DATABASE_URL": database_url, "REDIS_URL": redis_url}
