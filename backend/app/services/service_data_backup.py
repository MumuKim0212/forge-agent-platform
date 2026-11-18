"""Scheduled Postgres backups of per-project service databases, with
retention (ADR 0023 §Backup, §Amendment 4, §Amendment 9).

**Enumerates what to back up from Postgres itself**, never from
`service_env_json`: every `svc_`-prefixed database in `pg_database`, the
same check `services.service_data.provision_project_postgres` already makes
before creating one. This needs no credential beyond the admin DSN already
used to provision (never touches the encrypted service-env column), and it
is robust to a `Project` row whose env cannot be decrypted. A `svc_`
database with no matching `Project` row still gets backed up -- that is
the honest behaviour: stopping,
superseding, a failed deploy and a rollback all leave data intact, so
filtering on `Project.status` would silently skip exactly what ADR 0023
promises to protect.

**The dump runs INSIDE the shared Postgres container**, via the low-level
Docker exec API (`exec_create` / `exec_start` / `exec_inspect`), streamed
and demuxed, with the exit code read from `exec_inspect` -- never
`container.exec_run`, whose default combines stdout and stderr (measured
during this task's pre-dispatch probe: `pg_dump --verbose`'s OWN stderr
output was *larger* than its stdout on a two-row database -- interleaving
it into the dump file is silent corruption) and whose `exit_code` is
always `None` when `stream=True` (a naive `if exit_code != 0` never fires;
a failed dump would otherwise be written to disk and recorded as a
success). `pg_dump` exists only inside the container (measured: not on
this host), which has the useful side effect of making the client and
server versions identical by construction -- an operator on a different
Postgres major version can never hit pg_dump's refusal to dump a newer
server.

The dump authenticates as the SAME admin credential that provisions every
project's database (`SERVICE_POSTGRES_ADMIN_DSN`), over TCP to the
server's own standard port on its own loopback -- not via local
Unix-socket trust, which this module does not assume is configured.
`provision_project_postgres` already grants that admin role `CONNECT` on
every project database it creates (needed for `issue_role`'s later
schema-level grants), so no new grant is required here.

**Redis is never backed up** (ADR 0023 §Backup): it is a cache by default
(`--save ""`), and backing it up would imply a durability guarantee the
platform does not make. Do not add it here thinking this was an
oversight.

**This module never touches a database, a role, or a container beyond
reading `pg_database` and exec'ing `pg_dump`** -- ADR 0023: "never deleted
implicitly" protects SERVICE data, and ``tests/test_config.py::
test_app_never_drops_a_database_or_a_role`` scans this file too (it is not
the one exemption -- that is `preview_data.py`). The retention sweep below
deletes FILES only, inside the platform's own backup directory, and never
issues a single statement against Postgres.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from docker.errors import DockerException, NotFound
from sqlalchemy import create_engine, text

from app.config import settings
from app.services.service_data import _admin_url, _connect_args

logger = logging.getLogger("app.service_data_backup")

# Filesystem-safe, UTC, and sorts chronologically as a plain string (ISO
# 8601 basic format, no colons) -- so "the newest dump" is always just
# `sorted(os.listdir(project_dir))[-1]`, no timestamp parsing required.
_DUMP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DUMP_FILENAME_SUFFIX = ".sql"

# `dump_database`'s in-progress write lands under this suffix, in
# the same directory as the final dump, via `tempfile.mkstemp`. Deliberately
# NOT `.sql` -- `_newest_dump_path` and `sweep_backup_retention` both select
# by `DUMP_FILENAME_SUFFIX` alone, so a name ending in `.partial` is inert to
# both (never picked up as "the newest dump", never deleted by retention). A
# `.partial` left by a hard kill mid-write is out of scope (task's Out of
# scope) -- it costs disk space, never correctness, and is removed wholesale
# by deprovisioning along with everything else under the project directory.
PARTIAL_FILENAME_SUFFIX = ".partial"

# The standard port the Postgres SERVER binds to inside its own container --
# reached here over the container's own loopback, from a process (pg_dump)
# execed INTO that same container. Not the admin DSN's published host/port
# (ADR 0023 §Amendment 10: loopback-only, and a DIFFERENT port in dev -- that
# is how the PLATFORM, a host process, reaches the instance; irrelevant once
# already inside the container's own network namespace). Numerically the
# same value as `service_data._SERVICE_POSTGRES_PORT` (the port a DEPLOYED
# SERVICE reaches the instance on via Docker network attachment) because
# both name the one port the server actually listens on -- kept as a
# separate literal since the two call sites reach it for unrelated reasons.
_POSTGRES_PORT_IN_CONTAINER = 5432


# how often the worker's sweep LOOKS (enumerates the
# databases and asks which are due), as opposed to how often a database is dumped (the cadence,
# `service_data_backup_interval_hours`). Strictly shorter than the cadence, and that is not a
# preference: "due" is `age of the newest dump >= cadence`, so a look exactly one cadence after a
# dump finds it a few seconds too young and the database waits a second whole cadence -- a daily
# backup would quietly become a two-day one. Looking every `SCAN_INTERVAL` also bounds how long a
# newly provisioned database waits for its first dump. The sweep used to look on every 2-second
# worker tick, opening an admin engine each time.
SCAN_INTERVAL = timedelta(minutes=15)

# Retry backoff after a failed dump, per database: `RETRY_BACKOFF_BASE * 2 ** (failures - 1)`,
# capped at one cadence (the schedule a healthy database keeps), so an outage is retried 15 min,
# 30 min, 1 h, 2 h ... then once per cadence and never more often. Exponential with the cadence as
# the ceiling, not the cadence itself: a transient failure (the container restarting) is retried
# within minutes, and a persistent one costs one attempt and one audit row per day, not one per tick.
RETRY_BACKOFF_BASE = timedelta(minutes=15)

# How much of pg_dump's stderr is kept for the log. Its stdout goes to the file as it arrives; its
# stderr is only ever read by a person, and a runaway stream of it must not grow in memory either.
STDERR_KEEP_BYTES = 64 * 1024


def scan_interval() -> timedelta:
    """How long the sweep waits between looks: `SCAN_INTERVAL`, or the cadence when an operator
    configured one shorter than that (so a cadence of 0 still means "every tick")."""
    return min(SCAN_INTERVAL, timedelta(hours=settings.service_data_backup_interval_hours))


def retry_delay(consecutive_failures: int) -> timedelta:
    """The backoff window after the Nth consecutive failed dump of one database (N >= 1)."""
    cap = max(timedelta(hours=settings.service_data_backup_interval_hours), RETRY_BACKOFF_BASE)
    # Bounded before it is multiplied: a database that has failed for a month must not build a
    # huge integer just to be clamped by `min`.
    exponent = min(max(consecutive_failures - 1, 0), 32)
    return min(RETRY_BACKOFF_BASE * (2**exponent), cap)


class ServiceDataBackupError(RuntimeError):
    """Any failure enumerating service databases, dumping one, or reading
    a dump back. Never raised for a destructive failure, because nothing
    in this module issues a destructive statement."""


def ensure_backup_dir(directory: str) -> None:
    """Creates `directory` if missing, together with a `.gitignore`
    containing `*` inside it (decision (e)) -- the default path
    lands inside the working tree, and the failure this prevents is a
    complete plaintext copy of a service's data being committed to a
    repository that pushes off-host. A `.gitignore` with `*` at this
    level covers every file AND subdirectory git would otherwise see
    underneath it (git's ignore rules apply recursively), so a per-project
    subdirectory needs none of its own."""
    os.makedirs(directory, exist_ok=True)
    gitignore_path = os.path.join(directory, ".gitignore")
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("*\n")


def project_backup_dir(base_dir: str, database_name: str) -> str:
    """ADR 0023 §Amendment 4: one subdirectory per project. `database_name`
    (`svc_<project id, "-" -> "_">`) is already a deterministic, 1:1 name
    for the project -- reusing it as the subdirectory name needs no
    reverse mapping back to a project id, and the id substitution
    (`services.service_data._project_identifier`) is not perfectly
    reversible anyway."""
    return os.path.join(base_dir, database_name)


def list_service_databases() -> list[str]:
    """(b) Enumerates `svc_`-prefixed database names straight from
    `pg_database` -- never from `service_env_json`. `left(datname, 4)`
    rather than `LIKE 'svc\\_%'` sidesteps having to think about LIKE's
    escape-character default at all, mirroring `provision_project_postgres`'s
    own exact-match style over `pg_database`. Returns `[]` (not an error)
    when there are none."""
    url = _admin_url()
    engine = create_engine(url, connect_args=_connect_args(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT datname FROM pg_database "
                    "WHERE left(datname, 4) = 'svc_' ORDER BY datname"
                )
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - any admin-connection/SQL failure
        raise ServiceDataBackupError(f"enumerating service databases failed: {exc}") from exc
    finally:
        engine.dispose()
    return [row[0] for row in rows]


def _newest_dump_path(project_dir: str) -> str | None:
    """The lexicographically-last `*.sql` filename in `project_dir`, which
    is also the chronologically newest given the filename format above.
    `None` if the directory does not exist or holds no dump yet."""
    if not os.path.isdir(project_dir):
        return None
    dumps = sorted(name for name in os.listdir(project_dir) if name.endswith(DUMP_FILENAME_SUFFIX))
    if not dumps:
        return None
    return os.path.join(project_dir, dumps[-1])


def dump_filename(now: datetime) -> str:
    return now.strftime(_DUMP_TIMESTAMP_FORMAT) + DUMP_FILENAME_SUFFIX


def is_due(project_dir: str, *, interval_hours: int, now: datetime) -> bool:
    """(c1) Due means: no dump exists yet, or the newest one is older than
    `interval_hours`. Reads the FILESYSTEM (the newest dump's mtime), never
    an in-process clock: an in-memory deadline resets on every worker
    restart (never backing up, or backing up on every start, in a restart
    loop), and the worker loop's own sleep is not a fixed amount per tick,
    so wall-clock reasoning held inside the process is unreliable. The
    retention sweep below already has to stat this same directory, so this
    adds no new source of truth."""
    newest = _newest_dump_path(project_dir)
    if newest is None:
        return True
    age_seconds = now.timestamp() - os.path.getmtime(newest)
    return age_seconds >= interval_hours * 3600


def _exec_streamed_to_file(
    docker_client,
    container_id: str,
    cmd: list[str],
    *,
    out_file,
    environment: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """(a) The low-level exec path proven during this task's pre-dispatch
    probe: streamed and demuxed, with the exit code read from
    `exec_inspect` (a STREAMING exec's own `exit_code` is always `None`).
    Never `container.exec_run` -- see module docstring.

    **stdout is written to `out_file` chunk by chunk as it arrives, never collected.** This
    used to gather every chunk and `b"".join` them, so a dump held its whole size in the worker's
    memory (twice, for the moment of the join) -- a large service database is a large worker.
    Now the most this holds is one chunk, whatever the dump's size. stderr is kept only up to
    `STDERR_KEEP_BYTES`, for the log.

    Returns `(exit_code, stderr)`, never raising for a non-zero exit (that is the caller's call
    to make: by then `out_file` holds whatever was written, and the caller discards it);
    raises `DockerException` only for an actual Docker-API-level failure."""
    exec_id = docker_client.api.exec_create(container_id, cmd, environment=environment)["Id"]
    stderr_chunks: list[bytes] = []
    stderr_kept = 0
    exec_stream = docker_client.api.exec_start(exec_id, stream=True, demux=True)
    for stdout_chunk, stderr_chunk in exec_stream:
        if stdout_chunk:
            out_file.write(stdout_chunk)
        if stderr_chunk and stderr_kept < STDERR_KEEP_BYTES:
            kept = stderr_chunk[: STDERR_KEEP_BYTES - stderr_kept]
            stderr_chunks.append(kept)
            stderr_kept += len(kept)
    exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]
    return exit_code, b"".join(stderr_chunks)


def dump_database(database_name: str, *, docker_client, dest_path: str) -> None:
    """(b1-b5) Dumps `database_name` to `dest_path`. Leaves NO file behind
    on a non-zero exit (b2): `dest_path` itself is only ever produced by the final
    rename, after the exit code is known to be zero.

    **the dump is streamed straight into the temp file below** (the exit code is
    only known once the stream ends, so the bytes land in `.partial` first and are removed
    on a non-zero exit, a Docker failure or any other exception), instead of being held in
    memory and written afterwards.

    `dest_path` itself is reached only via `tempfile.mkstemp` +
    write + `fsync` + `os.replace`, never a direct `open(dest_path, "wb")`.
    Without this, a truncated write (disk full, process killed mid-write)
    used to leave a partial file at the FINAL `.sql` name -- and both
    `_newest_dump_path` and `sweep_backup_retention` select by suffix alone,
    so a truncated dump would be treated as the newest valid one: it would
    suppress the next scheduled backup for a full interval and become the
    one file retention always keeps, which is the exact failure this
    feature exists to prevent. `mkstemp` is used (not a fixed `.partial`
    name) because two sweeps racing on the same one-second-resolution
    filename is an unsupported-but-not-prevented case (no lock anywhere in
    `app/`, docs assume one worker process) -- a fixed name would collide
    there too. Same directory as `dest_path`, so `os.replace` is a
    same-filesystem atomic rename. `fsync` happens before the rename (not
    a directory fsync too) -- without it the failure mode is a MISSING
    dump, not a truncated one that looks valid, which is the property that
    matters here. On any failure between the temp file's creation and the
    rename, the temp file is removed on a best-effort basis and the
    exception re-raised unwrapped -- the failure contract (`OSError`
    propagates to `worker.py`'s catch-all, `ServiceDataBackupError` for the
    Docker/pg_dump half above) is otherwise unchanged.

    `mkstemp` also creates the file at mode `0600` under `O_EXCL`, which is
    what closes the earlier umask window (the file used to be briefly
    world-readable between creation and a separate `os.chmod` call) --
    real and effective on the single Linux host production runs on (ADR
    0002); a documented no-op on Windows, where `mkstemp`'s mode is not the
    same POSIX guarantee -- callers/tests gate any assertion on the
    resulting mode by `os.name`, never this function."""
    try:
        container = docker_client.containers.get(settings.service_postgres_container_name)
    except NotFound as exc:
        raise ServiceDataBackupError(
            f"shared Postgres container {settings.service_postgres_container_name!r} not found: {exc}"
        ) from exc
    except DockerException as exc:
        raise ServiceDataBackupError(f"looking up the shared Postgres container failed: {exc}") from exc

    admin_url = _admin_url()
    cmd = [
        "pg_dump",
        "--no-password",
        "-h",
        "127.0.0.1",
        "-p",
        str(_POSTGRES_PORT_IN_CONTAINER),
        "-U",
        admin_url.username or "",
        database_name,
    ]
    environment = {"PGPASSWORD": admin_url.password or ""}

    dest_dir = os.path.dirname(dest_path) or "."
    fd, tmp_path = tempfile.mkstemp(suffix=PARTIAL_FILENAME_SUFFIX, dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            try:
                exit_code, stderr = _exec_streamed_to_file(
                    docker_client, container.id, cmd, out_file=tmp_file, environment=environment
                )
            except DockerException as exc:
                raise ServiceDataBackupError(f"pg_dump exec failed for {database_name}: {exc}") from exc

            if exit_code != 0:
                logger.error(
                    "pg_dump for %s exited %s: %s",
                    database_name,
                    exit_code,
                    stderr.decode("utf-8", errors="replace"),
                )
                raise ServiceDataBackupError(f"pg_dump for {database_name} exited {exit_code}")

            if stderr:
                # Exit 0 with SOME stderr output is not itself a failure (pg_dump
                # can warn on exit 0) -- logged, never raised, and never mixed into
                # the dump file (that mixing is exactly what demux=True prevents).
                logger.warning(
                    "pg_dump for %s wrote to stderr despite exit 0: %s",
                    database_name,
                    stderr.decode("utf-8", errors="replace"),
                )

            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, dest_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _sweep_retention_for_project(project_dir: str, *, retention_days: int, now: datetime) -> int:
    """(c2-c4) Deletes every dump in `project_dir` older than
    `retention_days` -- EXCEPT the newest one, unconditionally, even when
    it is older than the window (c4): losing the only copy you have
    because it aged is the wrong failure. Never removes `project_dir`
    itself, so the next backup for this project has nowhere new to be
    created (c2)."""
    if not os.path.isdir(project_dir):
        return 0
    dumps = sorted(name for name in os.listdir(project_dir) if name.endswith(DUMP_FILENAME_SUFFIX))
    if not dumps:
        return 0
    newest = dumps[-1]
    cutoff_seconds = retention_days * 86400
    deleted = 0
    for name in dumps[:-1]:
        path = os.path.join(project_dir, name)
        age_seconds = now.timestamp() - os.path.getmtime(path)
        if age_seconds >= cutoff_seconds:
            os.remove(path)
            deleted += 1
    return deleted


def sweep_backup_retention(base_dir: str, *, retention_days: int, now: datetime | None = None) -> int:
    """(c2-c4) Deletes expired dumps across every project subdirectory of
    `base_dir` -- FILES only, never a database, a role, or a container
    (pinned by `tests/test_config.py::test_app_never_drops_a_database_or_
    a_role`, which scans this module too). A no-op, not an error, when
    `base_dir` does not exist yet."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not os.path.isdir(base_dir):
        return 0
    deleted_total = 0
    for entry in os.listdir(base_dir):
        project_dir = os.path.join(base_dir, entry)
        if not os.path.isdir(project_dir):
            continue  # e.g. the base directory's own .gitignore
        deleted_total += _sweep_retention_for_project(
            project_dir, retention_days=retention_days, now=now
        )
    return deleted_total
