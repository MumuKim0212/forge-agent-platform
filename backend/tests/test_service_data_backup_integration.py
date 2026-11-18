"""Integration tests for the Postgres backup sweep
(`app/services/service_data_backup.py`, `app/worker.py`, ADR 0023 §Backup)
against a REAL Postgres and a REAL Docker Engine -- no fakes for the
`pg_dump` exec or the SQL it reads back.

Uses the shared `real_service_postgres_admin_dsn` fixture (conftest.py): a
throwaway `postgres:18-alpine` container, one per module, published on
loopback only, with `settings.service_postgres_admin_dsn` /
`settings.service_postgres_container_name` pointed at it -- which is
exactly the container `pg_dump` runs INSIDE of for these tests, since
`app/services/service_data_backup.py` looks up the shared Postgres
container by that same setting. Skips cleanly (not fails) when Docker is
unavailable.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("docker")
pytest.importorskip("psycopg")

import docker  # noqa: E402
import psycopg  # noqa: E402
from docker.errors import DockerException  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app.worker as worker  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.services.service_data import _project_identifier, provision_project_postgres  # noqa: E402
from app.services.service_data_backup import (  # noqa: E402
    ServiceDataBackupError,
    _exec_streamed_to_file,
    dump_database,
    is_due,
    list_service_databases,
    project_backup_dir,
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
    # Same rewrite `test_service_data_integration.py` uses: DATABASE_URL is
    # built for a container reaching the shared instance by NAME on the
    # standard port -- unreachable from this HOST process, which instead
    # needs the admin DSN's own loopback host/port.
    from app.config import settings

    admin_url = make_url(settings.service_postgres_admin_dsn)
    issued = make_url(database_url)
    reachable = issued.set(drivername="postgresql", host=admin_url.host, port=admin_url.port)
    return psycopg.connect(reachable.render_as_string(hide_password=False))


def _admin_password(dsn: str) -> str:
    return make_url(dsn).password or ""


def test_list_service_databases_is_empty_with_none_provisioned(real_service_postgres_admin_dsn):
    """Runs FIRST in this module (test order = file order, and this
    module-scoped fixture's throwaway container is shared by every other
    test below, each of which provisions at least one project) -- an
    empty result here is only meaningful before anything else has run."""
    assert list_service_databases() == []


def test_list_service_databases_finds_a_provisioned_project(
    real_service_postgres_admin_dsn, project_id
):
    """(b) enumerates from pg_database, never from service_env_json."""
    provision_project_postgres(project_id)
    database_name = _project_identifier(project_id)

    assert database_name in list_service_databases()


def test_dump_technique_keeps_stderr_out_of_the_dump_file(
    real_service_postgres_admin_dsn, docker_client, project_id
):
    """(b1): proves the low-level streamed+demuxed exec technique --
    exactly what `dump_database` uses -- does not let stderr reach the
    dump content, using `--verbose` specifically because ITS stderr
    output is LARGER than its stdout on a small database (measured during
    this task's pre-dispatch probe: 1195 stdout bytes vs 2150 stderr bytes
    on a two-row database). `container.exec_run`'s combined default would
    interleave the two; this must not."""
    from app.config import settings
    from app.services.service_data import _admin_url

    database_url = provision_project_postgres(project_id)
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE proof (id integer)")
            cur.execute("INSERT INTO proof (id) VALUES (1)")
            conn.commit()

    database_name = _project_identifier(project_id)
    admin_url = _admin_url()
    container = docker_client.containers.get(settings.service_postgres_container_name)
    cmd = [
        "pg_dump",
        "--no-password",
        "--verbose",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        admin_url.username,
        database_name,
    ]
    sink = io.BytesIO()
    exit_code, stderr = _exec_streamed_to_file(
        docker_client, container.id, cmd, out_file=sink, environment={"PGPASSWORD": admin_url.password or ""}
    )
    stdout = sink.getvalue()

    assert exit_code == 0
    assert len(stderr) > 0  # --verbose guarantees SOME stderr on a real run
    assert stdout.startswith(b"--")
    assert b"pg_dump:" not in stdout


def test_dump_database_writes_a_real_dump_with_the_seeded_content(
    real_service_postgres_admin_dsn, docker_client, project_id, tmp_path
):
    """(b3): the dump is real -- seed a table+row, take a backup, and
    assert the dumped TEXT contains both the CREATE TABLE and the row."""
    database_url = provision_project_postgres(project_id)
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE proof (id integer, note text)")
            cur.execute("INSERT INTO proof (id, note) VALUES (42, 'forge-proof-marker')")
            conn.commit()

    database_name = _project_identifier(project_id)
    dest_path = str(tmp_path / "dump.sql")

    dump_database(database_name, docker_client=docker_client, dest_path=dest_path)

    text = open(dest_path, "r", encoding="utf-8", errors="replace").read()
    assert "CREATE TABLE" in text
    assert "proof" in text
    assert "forge-proof-marker" in text


def test_dump_database_leaves_no_file_behind_on_failure(
    real_service_postgres_admin_dsn, docker_client, tmp_path
):
    """(b2): a non-zero exit leaves NO file behind that could be mistaken
    for a backup -- induced against a database that does not exist."""
    dest_path = str(tmp_path / "dump.sql")

    with pytest.raises(ServiceDataBackupError):
        dump_database("svc_this_database_does_not_exist", docker_client=docker_client, dest_path=dest_path)

    assert not os.path.exists(dest_path)


def test_dump_database_sets_restrictive_mode(
    real_service_postgres_admin_dsn, docker_client, project_id, tmp_path
):
    """(b5): mode 0600 is set on every dump. Windows' `os.chmod` only
    toggles the read-only attribute (measured during this task's
    pre-dispatch probe: `os.chmod(path, 0o600)` left mode 0o666 on this
    box) -- the assertion is gated on `os.name != "nt"` and this comment
    is that "state in a comment why" the acceptance criterion asks for."""
    database_url = provision_project_postgres(project_id)
    database_name = _project_identifier(project_id)
    dest_path = str(tmp_path / "dump.sql")

    dump_database(database_name, docker_client=docker_client, dest_path=dest_path)

    if os.name != "nt":
        mode = os.stat(dest_path).st_mode & 0o777
        assert mode == 0o600
    # else: os.chmod is a documented no-op on Windows -- see decision (d)
    # in the task file. The call still runs (proven by not raising above);
    # it just cannot be observed as a mode change on this platform.


def test_full_backup_sweep_creates_one_subdirectory_per_project_with_a_sortable_timestamped_dump(
    real_service_postgres_admin_dsn, docker_client, db_session, project_id, tmp_path, monkeypatch
):
    """(b4): one subdirectory per project, filename carries a UTC
    timestamp and sorts chronologically. No credential, DSN or password
    appears in any path or in audit metadata (b4/e2). Also exercises the
    full worker-level sweep end-to-end against a real admin DSN."""
    from app.config import settings

    provision_project_postgres(project_id)
    database_name = _project_identifier(project_id)

    base_dir = str(tmp_path / "backups")
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_data_backup_dir", base_dir)
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 24)

    backed_up = worker.sweep_service_data_backups(db_session, docker_client=docker_client)

    assert backed_up >= 1
    project_dir = project_backup_dir(base_dir, database_name)
    assert os.path.isdir(project_dir)

    dumps = sorted(name for name in os.listdir(project_dir) if name.endswith(".sql"))
    assert len(dumps) == 1
    assert dumps[0][:8].isdigit()  # YYYYMMDD... -- a plain UTC timestamp

    admin_dsn = settings.service_postgres_admin_dsn
    admin_password = _admin_password(admin_dsn)
    assert admin_dsn not in project_dir
    assert admin_password not in project_dir
    assert "postgresql://" not in project_dir

    # This module-scoped fixture's throwaway container accumulates every
    # project OTHER tests in this file provision too, and the sweep backs
    # ALL of them up in one pass -- filter by THIS test's own target
    # rather than assume the most recent audit entry is this project's.
    entry = (
        db_session.query(AuditLog)
        .filter_by(action="service_data_backup.created", target=f"database:{database_name}")
        .order_by(AuditLog.ts.desc())
        .first()
    )
    assert entry is not None
    assert "postgresql://" not in entry.target
    assert admin_password not in entry.target
    if entry.metadata_json:
        serialized_metadata = str(entry.metadata_json)
        assert "postgresql://" not in serialized_metadata
        assert admin_password not in serialized_metadata

    # A second sweep in the same run is a no-op (the dump just written is
    # fresh) -- (c1)'s due gate, exercised through the real filesystem
    # mtime this time rather than a hand-set one.
    second_pass = worker.sweep_service_data_backups(db_session, docker_client=docker_client)
    assert second_pass == 0
    assert is_due(project_dir, interval_hours=24, now=datetime.now(timezone.utc)) is False
