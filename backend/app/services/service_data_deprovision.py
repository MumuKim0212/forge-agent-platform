"""Operator-initiated deprovisioning of a SERVICE's data.

The only route to destroying a SERVICE's Postgres database, its login
role, and its Redis container. The rule is: "the
automatic path may stop and archive; it may not delete" -- the sweep
stops at `ARCHIVED`. This module is what the admin-only `POST
/projects/{id}/deprovision` action (`routers/projects.py`) calls once that
precondition, and the confirmation-echo precondition, both already hold;
neither precondition is re-checked here -- this module trusts its caller
the same way `preview_data.py` trusts `PreviewManager`.

Kept in a module SEPARATE from `service_data.py` -- not because that file
is full, but so `tests/test_config.py::test_app_never_drops_a_database_or_
a_role`'s `ast`-based "never drops a database or a role" pin narrows to
EXACTLY TWO exempt files (`preview_data.py` for PREVIEW stores,
this one for SERVICE stores), and stays a real pin on every other module
under `app/`, including `service_data.py` itself.

**(T1) A `svc_` role is NOT materially harder to drop than a `pv_` one,
once tested against the real credential -- and this took proving TWICE:
the first published account of the mechanism (below) was itself wrong,
caught by running against a real database, not by reading.** A service
database carries two things a preview database does not: `GRANT CONNECT ON
DATABASE ... TO "<admin>"` (`service_data.py:313-321`) and the admin's own
`GRANT "<role>" TO "<admin>" WITH SET TRUE` membership (`service_data.py`'s
(i2), granted there so `CREATE DATABASE ... OWNER` -- an operation
requiring the SET option specifically -- succeeds under `CREATEDB
CREATEROLE NOSUPERUSER`).

**`DROP DATABASE` is authorized by the admin's ORDINARY MEMBERSHIP (with
`INHERIT`) in the `svc_` role that owns the database -- not by `CREATEROLE`,
and not by `SET`.** Postgres requires the executing role to own the
database, or to hold the owning role's privileges via membership; the gate
proved this directly against the real fixture, by varying exactly one
grant at a time: `REVOKE`ing only the admin's membership in the `svc_`
role (leaving `CREATEDB CREATEROLE` untouched) made `DROP DATABASE` fail
with `InsufficientPrivilege: must be owner of database`; re-`GRANT`ing that
SAME membership made the identical drop succeed again; and removing only
the `SET` option (`GRANT ... WITH SET FALSE`, membership itself left
standing) changed nothing -- the drop still succeeded. So `service_data.py`'s
`GRANT "<role>" TO "<admin>" WITH SET TRUE` (`service_data.py:310`) is
doing double duty it does not advertise: the `SET` half is what `CREATE
DATABASE ... OWNER` needs at provision time, but the plain MEMBERSHIP that
same grant confers is what THIS module's `DROP DATABASE` depends on later
-- flagged at the grant site itself (`service_data.py:287-311`) so a future
change narrowing or revoking it is an informed choice, not an accident.

`DROP ROLE` is unrelated to any of this: it succeeds on the admin's own
`CREATEROLE` attribute alone -- the same attribute that let it `CREATE
ROLE` in the first place -- independent of membership entirely.

`preview_data.py`'s exact two-statement shape (`DROP DATABASE ... WITH
(FORCE)` then `DROP ROLE IF EXISTS`, in that order) transfers unchanged to
a SERVICE role, but only because that membership grant is still standing
when this runs -- see `tests/test_service_data_deprovision_integration.py`
for the proof, including against a database with an OPEN session and a
SECOND role (`service_data.issue_role`) still attached.

**Roles minted by `service_data.issue_role` are explicitly out of scope**
because the platform records their names nowhere
and has no production caller, so this module does not hunt `pg_roles` for
them. A project's second credential from a manual `issue_role` call
survives a deprovision as an orphan -- a known, named limitation, not a
bug this module is expected to close.

**Backups are deleted only when the caller explicitly asks** --
`delete_service_backups` below, never called by
`deprovision_service_data` unless `delete_backups=True`. Reuses
`service_data_backup.project_backup_dir`'s naming (the directory name IS
the database name) rather than re-deriving it.

**The `ast` pin matches only the two SQL substrings it greps for** --
Redis container removal is not, and never was, covered by it.
The guarantee for the Redis half is this module's own guard
clause and review, the same as `preview_data.py`'s.
"""

from __future__ import annotations

import logging
import os
import shutil

from docker.errors import DockerException, NotFound
from sqlalchemy import create_engine, text

from app.config import settings
from app.services.service_data import (
    _admin_url,
    _connect_args,
    _project_identifier,
    _qi,
    redis_container_name,
)
from app.services.service_data_backup import project_backup_dir

logger = logging.getLogger("app.service_data_deprovision")

# Matches service_data.py's own hardcoded "svc_" -- this module does not
# import a shared constant because service_data.py exports none; both
# modules independently agree on the literal, the same way preview_data.py
# independently agrees on "pv_" rather than sharing a constant across an
# unrelated concern.
SERVICE_IDENTIFIER_PREFIX = "svc_"


class ServiceDataDeprovisionError(RuntimeError):
    """Any failure dropping a project's database or role. The Redis half
    never raises (mirrors `preview_data._drop_preview_redis`'s shape), and
    neither does the backup-deletion half."""


def _require_service_identifier(name: str) -> None:
    """Executed guard, not a comment (a2, mirroring preview_data._require_
    preview_identifier's shape and placement): refuses to touch anything
    that is not a `svc_`-prefixed identifier, BEFORE any admin connection
    is attempted -- so calling this with, say, a `pv_`-prefixed name fails
    with THIS message even when no admin DSN is configured at all."""
    if not name.startswith(SERVICE_IDENTIFIER_PREFIX):
        raise ServiceDataDeprovisionError(
            f"refusing to drop {name!r}: not a service identifier (expected "
            f"a {SERVICE_IDENTIFIER_PREFIX!r}-prefixed name) -- this guard "
            "is what keeps a bug in this module from ever reaching a "
            "preview's own database or role."
        )


def _drop_service_postgres(name: str) -> None:
    """(a1, a3, a4) Drops the database then the role, both `IF EXISTS` -- a
    repeat call, or a call for a project that never provisioned, is a
    silent no-op. `DROP DATABASE ... WITH (FORCE)` (Postgres 13+; the
    shared instance runs `postgres:18-alpine`) disconnects any lingering
    session first -- a deployed service's own container may still hold an
    open connection at the moment an admin deprovisions it, and nothing
    else in this call path stops that container the way `PreviewManager.
    stop_preview` force-removes a preview's container before its drop. See
    `preview_data.py:204`'s comment for the fuller explanation of why
    `WITH (FORCE)` is there; the (T1) proof in this module's own docstring
    is what confirms the role half needs no extra statement despite the
    extra grants a service role carries.

    A no-op (not a failure) when no admin DSN is configured at all --
    nothing could have been provisioned for it either."""
    _require_service_identifier(name)
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
        raise ServiceDataDeprovisionError(
            f"dropping service database/role {name!r} failed: {exc}"
        ) from exc


def _drop_service_redis(project_id: str, *, docker_client) -> None:
    """(a1, a3) Idempotent: removes a project's Redis container if present.
    A missing container (never provisioned, or already removed) is a
    silent no-op, matching `_drop_service_postgres`'s `IF EXISTS` shape. A
    `None` Docker client is also a no-op rather than a raise, mirroring
    `preview_data._drop_preview_redis` -- the caller decides whether a
    missing client is itself an error (`routers/projects.py` does, at 503,
    before this function is ever reached)."""
    if docker_client is None:
        return
    name = redis_container_name(project_id)
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


def delete_service_backups(project_id: str) -> bool:
    """Removes a project's ENTIRE backup
    directory -- every dump, including the newest one the scheduled
    retention sweep (`service_data_backup.sweep_backup_retention`) never
    touches, and the directory itself. Only ever called when the caller
    explicitly asked (`delete_backups=True` below); the default path never
    reaches this function. Idempotent: a project with no backup directory
    (never backed up, or already deleted) returns `False` rather than
    raising."""
    name = _project_identifier(project_id)
    directory = project_backup_dir(settings.service_data_backup_dir, name)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory)
    return True


def deprovision_service_data(
    project_id: str, *, docker_client=None, delete_backups: bool = False
) -> dict[str, bool]:
    """the one entry point `routers/projects.py`'s
    deprovision action calls. Drops, in the order that works (a1): the
    project's database, then its login role, then its Redis container --
    and, only when `delete_backups` is `True`, its entire backup
    directory. Returns `{"backups_deleted": bool}` so the caller's audit
    entry can record what actually happened.

    Raises `ServiceDataDeprovisionError` only for the Postgres half -- the
    Redis half never raises (matches `preview_data.drop_preview_data_
    services`'s own shape), and a partial failure (database/role dropped,
    Redis or backups not yet reached, e.g. because the caller's Docker
    client construction failed) is safe to retry: every step here is
    idempotent."""
    name = _project_identifier(project_id)
    _drop_service_postgres(name)
    _drop_service_redis(project_id, docker_client=docker_client)
    backups_deleted = delete_service_backups(project_id) if delete_backups else False
    return {"backups_deleted": backups_deleted}
