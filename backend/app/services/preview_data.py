"""Ephemeral preview data services (ADR 0023 §Amendment 2).

A preview never receives the service's own data services
(`services/service_data.py`) -- it gets a throwaway database + login role
on the SAME shared Postgres instance, plus its OWN Redis container, both
created with the preview and dropped when the preview is torn down. This
is the ONE exception ADR 0023 records to "never deleted implicitly" --
that rule protects SERVICE data; preview data is throwaway by
construction (a preview runs the same, unvetted L3 image a real service
would, per ADR 0016).

Kept in a module SEPARATE from `service_data.py` -- not because that file
is full, but so the `ast`-based "never drops a database or a role" pin
(`tests/test_config.py::test_app_never_drops_a_database_or_a_role`)
narrows to exactly this file, and stays a real pin on every other module
under `app/`, including `service_data.py` itself (that test also asserts
`service_data.py` remains in its scanned set). A later change
adds a SECOND exempt file, `services/service_data_deprovision.py`, for the
separate, admin-only, operator-initiated action that drops a SERVICE's own
`svc_`-prefixed database/role -- a distinct lifecycle from this module's
`pv_`-prefixed one, never reused between them.

Reuses `service_data`'s low-level admin-connection/quoting helpers
(`_admin_url`, `_connect_args`, `_qi`, `_ql`, `_database_url`, the
AUTOCOMMIT engine idiom, the `NOCREATEDB NOCREATEROLE NOSUPERUSER` role
shape, the CREATEROLE-not-SUPERUSER `GRANT ... WITH SET TRUE` workaround,
and the Redis hardening kwargs) -- the admin credential, the driver
choice, and the hardening posture are properties of the ONE shared
instance/host, not of who a database is provisioned for. Does NOT reuse
`_project_identifier` (hard-codes a `svc_` prefix a preview must never
carry) or `provision_project_data_services` (a SERVICE's combined
entry point, which never drops anything -- a preview's does).

**The guard**: every function that drops something takes an
ALREADY-COMPUTED name and refuses (raises, before touching the admin DSN
at all) anything that doesn't carry this module's own `pv_` prefix
(`_require_preview_identifier`) -- an executed check, not a comment. This
is the structural reason a bug in this module cannot reach a service's
own `svc_`-prefixed database or role.

Unlike `service_data.provision_project_postgres`, provisioning here is NOT
idempotent against an existing database/role -- a preview is always
freshly created: `PreviewManager.start_preview` calls `stop_preview` on
this exact build id (which drops any of its data services too)
immediately before provisioning ever runs, so there is never a row to
reuse. A residual leak from a crash BETWEEN provisioning and the preview
container actually starting is accepted, not solved here -- the general
orphan-sweep deferral (a sweep's
shape, not this one's).
"""

from __future__ import annotations

import logging
import secrets

from docker.errors import DockerException, NotFound
from sqlalchemy import create_engine, text

from app.config import settings
from app.services.platform_labels import LABEL_INSTANCE, Role, instance_id, platform_labels
from app.services.service_data import (
    _admin_url,
    _connect_args,
    _database_url,
    _qi,
    _ql,
    create_isolated_redis,
    generate_redis_password,
    redis_url_with_password,
)

logger = logging.getLogger("app.preview_data")

# `pv_` + a uuid4 build id (hyphens -> underscores, same substitution
# `service_data._project_identifier` makes) = 39 characters -- comfortably
# under Postgres's 63-byte identifier limit, and never collidable with a
# service's `svc_`-prefixed name (decision (c)).
PREVIEW_IDENTIFIER_PREFIX = "pv_"

# Distinct from `service_data.REDIS_CONTAINER_PREFIX` ("forge-service-redis")
# so the two lifecycles are never confusable at a glance, in a `docker ps`,
# or in a label.
PREVIEW_REDIS_CONTAINER_PREFIX = "forge-preview-redis"


class PreviewDataProvisionError(RuntimeError):
    """Any failure provisioning, reaching, or dropping a preview's
    database, role, or Redis container."""


# -- naming ----------------------------------------------------------------


def _preview_identifier(build_id: str) -> str:
    """Deterministic Postgres identifier for one preview's OWN throwaway
    database AND role (the same name in both, as `service_data.
    _project_identifier` also does -- roles and databases are separate
    object types, so reusing one name is not a collision). `build_id` is a
    uuid4 (`models.base.new_id`)."""
    return PREVIEW_IDENTIFIER_PREFIX + build_id.replace("-", "_")


def preview_redis_container_name(build_id: str) -> str:
    """Public: `PreviewManager`'s attach/detach bracket needs this exact
    name to connect/disconnect the container from a preview's network
    without importing this module's private Postgres naming scheme."""
    return f"{PREVIEW_REDIS_CONTAINER_PREFIX}-{build_id}"


def _instance_marker() -> str:
    """The text a preview's Postgres database and role carry as their `COMMENT` (ADR
    0037 §Decision 2): `forge.instance=<this installation's id>`, the Postgres counterpart of
    the `forge.instance` label a preview's Redis container carries. Postgres has no labels, and
    the shared instance can be reached by more than one installation (a second checkout, a
    test run), so a `pv_` name alone does not say whose it is. The orphan sweep lists and
    drops only what carries THIS value; an object with no comment, or another installation's,
    is never touched. Raises `InstanceIdUnsetError` when no id is bound, the same fail-loud
    rule as every create site."""
    return f"{LABEL_INSTANCE}={instance_id()}"


def _require_preview_identifier(name: str) -> None:
    """Executed guard, not a comment (decision (c)): refuses to touch
    anything that is not this module's own preview-prefixed identifier.
    Called BEFORE any admin connection is attempted, so calling this with
    a `svc_`-prefixed name fails with THIS message, distinctly, even when
    no admin DSN is configured at all (a connection attempt would fail
    differently, and never gets the chance to)."""
    if not name.startswith(PREVIEW_IDENTIFIER_PREFIX):
        raise PreviewDataProvisionError(
            f"refusing to drop {name!r}: not a preview identifier (expected "
            f"a {PREVIEW_IDENTIFIER_PREFIX!r}-prefixed name) -- this guard "
            "is what keeps a bug in this module from ever reaching a "
            "service's own database or role."
        )


# -- Postgres: provisioning -------------------------------------------------


def _provision_preview_postgres(build_id: str) -> str:
    """Creates a throwaway, EMPTY database + login role for one preview.
    No schema is created and nothing is copied from any service's
    database -- ADR 0023 §Amendment 2's "empty" is a provisioning-time
    property; a template's own `alembic upgrade head` is what
    would populate it after the container starts. The role is created
    WITHOUT `CREATEDB`/`CREATEROLE`/`SUPERUSER`, the same shape ADR 0023
    §Amendment 5 requires of a service's own role -- verified by a test
    reading `pg_roles` back, not by asserting the SQL string."""
    name = _preview_identifier(build_id)
    password = secrets.token_urlsafe(24)
    try:
        marker = _instance_marker()
        url = _admin_url()
        admin_username = url.username
        engine = create_engine(url, connect_args=_connect_args(url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        f"CREATE ROLE {_qi(name)} WITH LOGIN PASSWORD {_ql(password)} "
                        "NOCREATEDB NOCREATEROLE NOSUPERUSER"
                    )
                )
                # marked as this installation's the moment it exists, so the window in
                # which a crash could leave an unmarked (and so never swept) object is one
                # statement wide.
                conn.execute(text(f"COMMENT ON ROLE {_qi(name)} IS {_ql(marker)}"))
                if admin_username:
                    # Same CREATEROLE-not-SUPERUSER workaround
                    # service_data.provision_project_postgres documents in
                    # full: a CREATEROLE admin gets
                    # ADMIN OPTION on a role it just created but not SET,
                    # and CREATE DATABASE ... OWNER needs SET as of
                    # Postgres 16. Harmless no-op for a superuser admin.
                    conn.execute(text(f"GRANT {_qi(name)} TO {_qi(admin_username)} WITH SET TRUE"))
                conn.execute(text(f"CREATE DATABASE {_qi(name)} OWNER {_qi(name)}"))
                conn.execute(text(f"COMMENT ON DATABASE {_qi(name)} IS {_ql(marker)}"))
                conn.execute(text(f"REVOKE CONNECT ON DATABASE {_qi(name)} FROM PUBLIC"))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure (incl. an unconfigured admin DSN) is a provisioning failure
        # this half rolls ITSELF back. The four statements above are separate
        # autocommit transactions, so a failure after the first leaves what the first
        # created -- a role, or a role and an empty database that anything holding the
        # credentials could still reach -- and until now only a failing REDIS half
        # (`provision_preview_data_services`) ever dropped the Postgres half. The drop
        # is `IF EXISTS` on both objects, so it is right for whichever statement failed
        # and a no-op if the admin connection never opened; its own failure is logged
        # and never replaces this one.
        try:
            _drop_preview_postgres(name)
        except Exception:  # noqa: BLE001 - best-effort rollback of what this call created
            logger.exception(
                "preview %s: failed to roll back its postgres database/role after provisioning failed",
                build_id,
            )
        raise PreviewDataProvisionError(
            f"provisioning preview {build_id}'s Postgres database/role failed: {exc}"
        ) from exc

    return _database_url(role=name, password=password, database=name)


# -- Postgres: the drop, and its guard --------------------------------------


def _drop_preview_postgres(name: str) -> None:
    """Drops the database then the role, both `IF EXISTS` -- a repeat call
    (or a call for a build that never provisioned anything) is a silent
    no-op (b2). `DROP DATABASE ... WITH (FORCE)` disconnects any lingering
    session first: `PreviewManager.stop_preview` force-removes the preview
    CONTAINER (whose process may still hold an open connection) right
    before calling this, and the two events are not ordered by Postgres's
    own notion of "connection closed" without it.

    `name` is an already-computed identifier (not a build id) precisely so
    a test can call this directly with an arbitrary string and prove the
    guard fires before any admin connection is attempted at all -- see
    `_require_preview_identifier`.

    Also a no-op (not a failure) when no admin DSN is configured at all:
    nothing could have been provisioned without one either (the same gate
    guards `PreviewManager.start_preview`'s call into
    `provision_preview_data_services`), so there is nothing to drop for
    THIS build any more than for one that never had a preview."""
    _require_preview_identifier(name)
    if not settings.service_postgres_configured:
        return
    try:
        url = _admin_url()
        engine = create_engine(url, connect_args=_connect_args(url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {_qi(name)} WITH (FORCE)"))
                conn.execute(text(f"DROP ROLE IF EXISTS {_qi(name)}"))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure is a teardown failure
        raise PreviewDataProvisionError(
            f"dropping preview database/role {name!r} failed: {exc}"
        ) from exc


# -- Redis: provisioning and the drop ---------------------------------------


def _create_preview_redis(build_id: str, *, docker_client) -> str:
    """Creates this preview's OWN Redis container -- through the SAME
    `service_data.create_isolated_redis` a service's Redis uses (ADR
    0023 §Amendment 12), so a preview -- still unvetted L3 output -- gets no
    weaker a cage than a real service's own cache: mem/cpu/pids caps,
    `no-new-privileges`, `cap_drop=ALL`, `privileged=False`, no binds, no
    published port, `--save ""`, **never on the default bridge**, a
    generated password carried in the returned `REDIS_URL`, and the
    server-to-server commands disabled. Named and labelled distinctly from a
    service's (a4) so the two are never confusable. Unlike a service's
    Redis, never reused across calls -- `start_preview` always drops any
    previous container for this exact build id first -- and **no restart
    policy**: its consumer, the preview container, has none either, so a
    store that outlived the daemon would only be an orphan for the sweep."""
    if docker_client is None:
        raise PreviewDataProvisionError(
            "preview redis creation called with no Docker client -- this "
            "should be unreachable (PreviewManager always passes its own "
            "client); investigate the caller."
        )
    name = preview_redis_container_name(build_id)
    password = generate_redis_password()
    try:
        create_isolated_redis(
            docker_client,
            name=name,
            # the platform's instance and role beside the data-service marker the
            # orphan sweep and `list_preview_redis_containers` select by.
            labels=platform_labels(
                Role.PREVIEW_REDIS,
                build_id=build_id,
                extra={"forge.data_service": "preview-redis"},
            ),
            password=password,
            restart_policy=None,
        )
    except DockerException as exc:
        raise PreviewDataProvisionError(
            f"preview {build_id}'s Redis container failed to start: {exc}"
        ) from exc
    return _preview_redis_url(build_id, password)


def _preview_redis_url(build_id: str, password: str) -> str:
    return redis_url_with_password(preview_redis_container_name(build_id), password)


def _drop_preview_redis(build_id: str, *, docker_client) -> None:
    """Idempotent: removes this preview's Redis container if present. A
    missing container (never provisioned, or already removed) is a silent
    no-op, matching `_drop_preview_postgres`'s `IF EXISTS` shape. A `None`
    Docker client (a caller with no engine at all) is also a no-op rather
    than a raise -- `stop_preview`'s teardown must never crash over this."""
    if docker_client is None:
        return
    name = preview_redis_container_name(build_id)
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return
    except DockerException:
        return
    try:
        container.remove(v=True, force=True)
    except DockerException:
        pass


# -- combined entry points (what `PreviewManager` calls) --------------------


def provision_preview_data_services(build_id: str, *, docker_client=None) -> dict[str, str]:
    """ADR 0023 §Amendment 2: creates -- always fresh, never reused
    -- one preview's OWN throwaway Postgres database+role and Redis
    container, returning `{"DATABASE_URL": ..., "REDIS_URL": ...}`.
    `PreviewManager.start_preview` only calls this when
    `settings.service_postgres_configured` is true (mirroring
    `DeployManager._connect_data_services`'s own guard) -- unconfigured is
    "no data services for this preview", the same "today's behaviour"
    posture ADR 0023 §Amendment 3 describes for a service before that
    amendment made deployment refuse outright; a preview has no such
    refusal because nothing yet requires it to have a
    database to start at all.

    Raises `PreviewDataProvisionError` on any failure; `PreviewManager.
    start_preview` catches it and treats a provisioning failure like any
    other preview-start failure -- non-fatal to the build (decision (e):
    preview issuance is a post-success convenience, not a gate).

    If Redis creation fails after Postgres succeeded, the just-created
    database/role is dropped before raising -- a partially-provisioned
    preview is exactly as unwanted as a fully-provisioned one nobody ever
    uses, and both are equally cheap to throw away. The other
    direction too (`_provision_preview_postgres` drops what it created when
    ITS statements fail part-way), and for ANY exception out of the Redis
    half, not only `PreviewDataProvisionError` -- a container the Redis half
    created before it raised something else is removed with it."""
    database_url = _provision_preview_postgres(build_id)
    try:
        redis_url = _create_preview_redis(build_id, docker_client=docker_client)
    except Exception:  # noqa: BLE001 - roll back, then let the original exception through unchanged
        try:
            _drop_preview_postgres(_preview_identifier(build_id))
        except Exception:  # noqa: BLE001 - best-effort rollback of the half that DID succeed
            logger.exception(
                "preview %s: failed to roll back postgres after redis provisioning failed",
                build_id,
            )
        try:
            _drop_preview_redis(build_id, docker_client=docker_client)
        except Exception:  # noqa: BLE001 - best-effort: a container the failed create may have left
            logger.exception(
                "preview %s: failed to roll back redis after redis provisioning failed",
                build_id,
            )
        raise
    return {"DATABASE_URL": database_url, "REDIS_URL": redis_url}


def drop_preview_data_services(build_id: str, *, docker_client=None) -> None:
    """ADR 0023 §Amendment 2: drops one preview's database, role,
    and Redis container -- the ONE exception ADR 0023 records to "never
    deleted implicitly" (that rule protects SERVICE data; this data was
    throwaway from the moment it was created).

    Idempotent (b2): calling this twice, or for a build id that never had
    a preview, raises nothing. Called UNCONDITIONALLY by `PreviewManager.
    stop_preview`, even when the preview's own container is already gone
    (decision (a) -- that is exactly the case that used to leak: the
    container gone and the database not)."""
    name = _preview_identifier(build_id)
    _drop_preview_postgres(name)
    _drop_preview_redis(build_id, docker_client=docker_client)


# -- orphan reclamation ---------------------------------------
#
# Mechanics only -- enumeration and a narrow public drop-by-name/build_id
# wrapper around what already exists above. The ORPHAN RULE itself (which
# build states make a store reclaimable), the schedule, the per-orphan
# error-swallowing, and the audit logging all live in
# `worker.sweep_orphaned_preview_stores` -- the same split the backup
# sweep already established between this file's sibling
# `service_data_backup.py` (mechanics) and `worker.py` (orchestration).
# Worker code never reaches into this module's underscore-prefixed
# functions directly (matching every other worker.py sweep's own
# convention) -- `drop_preview_postgres_by_identifier`/
# `drop_preview_redis_by_build_id` below are the public seam for that.


def list_preview_databases() -> list[str]:
    """Enumerates THIS installation's `pv_`-prefixed database names straight from `pg_database`
    -- the same `service_data_backup.list_service_databases` shape (`left(datname, N) =
    '<prefix>_'`, sidestepping `LIKE`'s escape-character question entirely), with `N=3` for
    `pv_` where that function's `svc_` uses `N=4`. **Only those whose comment is this
    installation's marker** (`_instance_marker`): a `pv_` database another installation
    created, or one with no comment at all (made before the marker existed, or by hand), is not listed and
    so is never dropped. Returns `[]` (not an error) when there are none. Raises
    `service_data.ServiceDataProvisionError` if no admin DSN is configured -- `_admin_url()`
    itself raises that (not this module's own `PreviewDataProvisionError`) before any
    try/except here is even entered, the exact same shape `service_data_backup.
    list_service_databases` already has. Callers gate on `settings.service_postgres_configured`
    first, exactly like `service_data_backup`'s own caller does, so this is never reached
    unconfigured in production."""
    url = _admin_url()
    marker = _instance_marker()
    engine = create_engine(url, connect_args=_connect_args(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT datname FROM pg_database "
                    "WHERE left(datname, 3) = 'pv_' "
                    "AND shobj_description(oid, 'pg_database') = :marker ORDER BY datname"
                ),
                {"marker": marker},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure
        raise PreviewDataProvisionError(f"enumerating preview databases failed: {exc}") from exc
    finally:
        engine.dispose()
    return [row[0] for row in rows]


def list_preview_roles() -> list[str]:
    """Same shape (and the same instance scope) as `list_preview_databases`, over `pg_roles`
    instead of `pg_database` -- enumerated and reconciled SEPARATELY (decision 3): a role can
    outlive its database, or vice versa, so pairing the two enumerations would miss exactly
    the half-orphan case this sweep exists to catch."""
    url = _admin_url()
    marker = _instance_marker()
    engine = create_engine(url, connect_args=_connect_args(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rolname FROM pg_roles "
                    "WHERE left(rolname, 3) = 'pv_' "
                    "AND shobj_description(oid, 'pg_authid') = :marker ORDER BY rolname"
                ),
                {"marker": marker},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure
        raise PreviewDataProvisionError(f"enumerating preview roles failed: {exc}") from exc
    finally:
        engine.dispose()
    return [row[0] for row in rows]


def list_preview_redis_containers(*, docker_client) -> list[dict]:
    """Enumerates THIS installation's `forge.data_service=preview-redis` containers, returning
    `[{"name": ..., "build_id": ...}, ...]` -- `[]` (not an error) when there are none.

    **Instance-scoped (ADR 0037 §Decision 2):** the Docker filter asks for this
    installation's `forge.instance` as well, and each container's own labels are then checked
    again, because "never act on what is not positively identified as ours" must not rest on a
    filter's semantics (the reconciler's `_list_objects` rule). A container with no instance
    label, or another installation's, is not returned.

    `build_id` comes from the container's OWN `forge.build_id` label (decision 4: reconciled by
    that label, never by parsing the name back) and is `None` only for a container carrying
    this data-service label without also carrying `forge.build_id` -- nothing this module has
    ever created omits it, so the caller treats a `None` as unreconcilable rather than guessing
    at ownership from the name."""
    instance = instance_id()
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_INSTANCE}={instance}", "forge.data_service=preview-redis"]},
        )
    except DockerException as exc:
        raise PreviewDataProvisionError(
            f"enumerating preview redis containers failed: {exc}"
        ) from exc
    found: list[dict] = []
    for container in containers:
        labels = container.labels or {}
        if not _is_this_installations_preview_redis(labels, instance):
            continue
        found.append({"name": container.name, "build_id": labels.get("forge.build_id")})
    return found


def _is_this_installations_preview_redis(labels: dict, instance: str) -> bool:
    return (
        labels.get(LABEL_INSTANCE) == instance
        and labels.get("forge.data_service") == "preview-redis"
    )


def drop_preview_postgres_by_identifier(name: str) -> None:
    """Public seam for `worker.sweep_orphaned_preview_stores`: drops an already-enumerated
    Postgres identifier directly. There is no build id to call `drop_preview_data_services`
    with here -- decision 2 forbids reversing a `pv_` name back to one -- so this takes the
    identifier, with the exact same guard (`_require_preview_identifier`) and idempotency.

    **Re-checked per object:** the database is dropped only if its comment is this
    installation's marker, and the role only if its comment is; the two are checked and
    dropped separately, so a half-orphan whose other half belongs to someone else loses only
    its own. Anything else is left alone without an error (the listing never returned it, so
    this is the second check, not the first)."""
    _require_preview_identifier(name)
    if not settings.service_postgres_configured:
        return
    try:
        marker = _instance_marker()
        url = _admin_url()
        engine = create_engine(url, connect_args=_connect_args(url)).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                owns_database = conn.execute(
                    text(
                        "SELECT 1 FROM pg_database WHERE datname = :name "
                        "AND shobj_description(oid, 'pg_database') = :marker"
                    ),
                    {"name": name, "marker": marker},
                ).fetchone()
                if owns_database is not None:
                    conn.execute(text(f"DROP DATABASE IF EXISTS {_qi(name)} WITH (FORCE)"))
                owns_role = conn.execute(
                    text(
                        "SELECT 1 FROM pg_roles WHERE rolname = :name "
                        "AND shobj_description(oid, 'pg_authid') = :marker"
                    ),
                    {"name": name, "marker": marker},
                ).fetchone()
                if owns_role is not None:
                    conn.execute(text(f"DROP ROLE IF EXISTS {_qi(name)}"))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure is a teardown failure
        raise PreviewDataProvisionError(
            f"dropping preview database/role {name!r} failed: {exc}"
        ) from exc


def drop_preview_redis_by_build_id(build_id: str, *, docker_client) -> None:
    """Public seam for `worker.sweep_orphaned_preview_stores`: drops one preview-redis container
    by the build id read off its OWN `forge.build_id` label (decision 4). Deliberately does NOT
    also touch the Postgres half (that half is reconciled independently, by `list_preview_
    databases`/`list_preview_roles` above, per decision 3), so a caller that already knows this
    build's Postgres half is untouched here never gets a surprise second drop attempt from this
    call.

    **Re-checked per object:** the container found under that name is removed only if
    its labels say it is THIS installation's preview-redis for THIS build; a container of
    another installation (or none of the platform's) that happens to carry the name is left
    alone. A missing container, or a `None` client, is a silent no-op."""
    if docker_client is None:
        return
    name = preview_redis_container_name(build_id)
    try:
        container = docker_client.containers.get(name)
    except DockerException:
        return
    labels = container.labels or {}
    if not _is_this_installations_preview_redis(labels, instance_id()):
        return
    if labels.get("forge.build_id") != build_id:
        return
    # The one removal site stays `_drop_preview_redis` (it looks the same name up again; the
    # ownership decision above is on the same object, which holds that name).
    _drop_preview_redis(build_id, docker_client=docker_client)
