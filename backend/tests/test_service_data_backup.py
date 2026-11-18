"""Unit tests for the pure filesystem logic
(`app/services/service_data_backup.py`, ADR 0023 §Backup) -- no real
Postgres, no real Docker. Real-Postgres/real-Docker coverage (the actual
`pg_dump` exec path) lives in `tests/test_service_data_backup_integration.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.services.service_data_backup import (
    DUMP_FILENAME_SUFFIX,
    PARTIAL_FILENAME_SUFFIX,
    _newest_dump_path,
    dump_database,
    dump_filename,
    ensure_backup_dir,
    is_due,
    project_backup_dir,
    sweep_backup_retention,
)
from tests.fake_admin_dsn import FAKE_ADMIN_DSN


# `dump_database` reads the admin DSN (`_admin_url()`) before it writes
# anything, and `settings` is built from `backend/.env` at import time -- so a
# test that reaches it passes on a dev box and fails on a CI box with no
# `.env`. Pin it per test (to `FAKE_ADMIN_DSN`, the one shared DSN that can
# never connect) instead of inheriting whatever the box has.


def _touch(path: str, *, age_hours: float = 0.0) -> None:
    """Creates an empty file at `path` and sets its mtime `age_hours` in
    the past -- the "controlled mtimes" technique the task's acceptance
    criteria (c1/c2/c4) explicitly asks for, instead of sleeping."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("-- fake dump\n")
    when = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (when, when))


# --- (c1) due gate -----------------------------------------------------


def test_is_due_when_no_dump_exists_yet(tmp_path):
    project_dir = str(tmp_path / "svc_never_backed_up")
    assert is_due(project_dir, interval_hours=24, now=datetime.now(timezone.utc)) is True


def test_is_due_false_when_newest_dump_is_within_the_interval(tmp_path):
    project_dir = str(tmp_path / "svc_fresh")
    _touch(os.path.join(project_dir, dump_filename(datetime.now(timezone.utc))), age_hours=1)
    assert is_due(project_dir, interval_hours=24, now=datetime.now(timezone.utc)) is False


def test_is_due_true_when_newest_dump_is_older_than_the_interval(tmp_path):
    project_dir = str(tmp_path / "svc_stale")
    _touch(os.path.join(project_dir, dump_filename(datetime.now(timezone.utc))), age_hours=25)
    assert is_due(project_dir, interval_hours=24, now=datetime.now(timezone.utc)) is True


def test_project_backup_dir_is_one_subdirectory_per_project(tmp_path):
    """(b4): one subdirectory per project underneath the base dir."""
    base = str(tmp_path)
    assert project_backup_dir(base, "svc_abc123") == os.path.join(base, "svc_abc123")


def test_dump_filename_sorts_chronologically():
    """(b4): the filename format carries a UTC timestamp and sorts
    chronologically as a plain string -- no timestamp parsing needed to
    find "the newest one"."""
    earlier = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    assert sorted([dump_filename(later), dump_filename(earlier)]) == [
        dump_filename(earlier),
        dump_filename(later),
    ]


# --- (c2/c4) retention ---------------------------------------------------


def test_retention_deletes_expired_dumps_and_keeps_fresh_ones(tmp_path):
    base_dir = str(tmp_path)
    project_dir = os.path.join(base_dir, "svc_proj1")
    now = datetime.now(timezone.utc)
    expired = os.path.join(project_dir, dump_filename(now - timedelta(days=10)))
    fresh = os.path.join(project_dir, dump_filename(now - timedelta(days=1)))
    _touch(expired, age_hours=10 * 24)
    _touch(fresh, age_hours=1 * 24)

    deleted = sweep_backup_retention(base_dir, retention_days=7, now=now)

    assert deleted == 1
    assert not os.path.exists(expired)
    assert os.path.exists(fresh)


def test_retention_never_deletes_the_newest_dump_even_when_it_is_expired(tmp_path):
    """(c4): losing the only copy you have because it aged is the wrong
    failure. Every dump for this project is older than the window, so the
    newest one is the ONLY thing that must survive."""
    base_dir = str(tmp_path)
    project_dir = os.path.join(base_dir, "svc_proj1")
    now = datetime.now(timezone.utc)
    oldest = os.path.join(project_dir, dump_filename(now - timedelta(days=30)))
    newest_but_still_expired = os.path.join(project_dir, dump_filename(now - timedelta(days=20)))
    _touch(oldest, age_hours=30 * 24)
    _touch(newest_but_still_expired, age_hours=20 * 24)

    deleted = sweep_backup_retention(base_dir, retention_days=7, now=now)

    assert deleted == 1
    assert not os.path.exists(oldest)
    assert os.path.exists(newest_but_still_expired)
    # (c2): the project directory itself survives, so the next run has
    # somewhere to write into.
    assert os.path.isdir(project_dir)


def test_retention_is_a_noop_on_a_directory_that_does_not_exist_yet(tmp_path):
    assert sweep_backup_retention(str(tmp_path / "never-created"), retention_days=7) == 0


def test_retention_ignores_non_directory_entries_at_the_base_level(tmp_path):
    """A stray file directly under the base dir (e.g. the base dir's own
    `.gitignore`) is not itself a project directory and must not be
    mistaken for one."""
    base_dir = str(tmp_path)
    ensure_backup_dir(base_dir)  # writes base_dir/.gitignore
    assert sweep_backup_retention(base_dir, retention_days=7) == 0


# --- (c3) never destructive ----------------------------------------------


def test_module_source_contains_no_drop_statement():
    """(c3): grep for DROP in the new module returns nothing outside a
    docstring -- retention only ever deletes FILES, never a database, a
    role, or a container. Docstrings are prose explaining the invariant
    (this module's own module docstring says "never DROP DATABASE...")
    and are excluded the same way `test_config.py::test_app_never_drops_
    a_database_or_a_role` excludes them -- only an executable string
    literal (an f-string or a plain string actually passed to something
    SQL-shaped) would trip this."""
    import ast

    path = Path(__file__).resolve().parents[1] / "app" / "services" / "service_data_backup.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_node_ids = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstring_node_ids:
            continue
        if "DROP" in node.value.upper():
            hits.append(node.value)
    assert hits == []


# --- (e1) the backup directory protects itself ---------------------------


def test_ensure_backup_dir_writes_a_gitignore_that_git_actually_honours():
    """(e1), decision (e): a file written into a freshly created backup
    directory must be ignored by git. Deliberately NOT `tmp_path` -- that
    fixture lives outside any git working tree, and `git check-ignore`
    cannot reason about a path git does not consider part of a repository.
    A scratch directory INSIDE this actual checkout (cleaned up
    afterward) exercises the real repository's git, not a simulation."""
    repo_root = Path(__file__).resolve().parents[2]
    scratch_root = tempfile.mkdtemp(dir=str(Path(__file__).resolve().parent))
    try:
        backup_dir = os.path.join(scratch_root, "service-data")
        ensure_backup_dir(backup_dir)
        assert os.path.isfile(os.path.join(backup_dir, ".gitignore"))

        probe_file = os.path.join(backup_dir, "svc_some_project", "20260909T000000Z.sql")
        os.makedirs(os.path.dirname(probe_file), exist_ok=True)
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("-- fake dump\n")

        result = subprocess.run(
            ["git", "check-ignore", "-q", probe_file],
            cwd=str(repo_root),
        )
        assert result.returncode == 0, "a file inside a fresh backup dir must be git-ignored"
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


def test_ensure_backup_dir_is_idempotent():
    """A second call must not error or clobber an existing .gitignore's
    content (the directory may already hold real dumps)."""
    scratch_root = tempfile.mkdtemp(dir=str(Path(__file__).resolve().parent))
    try:
        backup_dir = os.path.join(scratch_root, "service-data")
        ensure_backup_dir(backup_dir)
        ensure_backup_dir(backup_dir)
        with open(os.path.join(backup_dir, ".gitignore"), encoding="utf-8") as f:
            assert f.read() == "*\n"
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


def test_newest_dump_path_is_none_for_a_directory_with_no_dumps(tmp_path):
    project_dir = str(tmp_path / "svc_empty")
    os.makedirs(project_dir)
    assert _newest_dump_path(project_dir) is None


# --- (d) dump_database: atomic write --------------------------------------
#
# Fault injection drives `dump_database` itself (a fake Docker client is
# fine -- see the module docstring: `container.exec_run`'s combined-stream
# default was already measured to interleave stderr into stdout, so a fake
# that demuxes correctly is what makes these tests meaningful at all).
# Testing only a factored-out write helper would not reach the risk this
# task closes: the risk is specifically that `dump_database`'s own write
# path can leave a truncated `.sql` file at the FINAL name.


class _FakeExecApi:
    """Demuxing fake for `docker_client.api.exec_create`/`exec_start`/
    `exec_inspect`, matching `_exec_streamed_demuxed`'s own call shape
    (`exec_start(..., stream=True, demux=True)`, yielding
    `(stdout_chunk, stderr_chunk)` pairs) -- distinct from
    `test_sandbox_runner_unit.py`'s `FakeExecApi`, which fakes the
    non-demuxed shape `sandbox_runner.py` uses instead."""

    def __init__(self, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def exec_create(self, container_id, cmd, environment=None):
        return {"Id": "fake-exec-id"}

    def exec_start(self, exec_id, stream=True, demux=True):
        return iter([(self.stdout, self.stderr)])

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.exit_code}


class _FakeContainer:
    id = "fake-container-id"


class _FakeContainersCollection:
    def get(self, name):
        return _FakeContainer()


class _FakeDockerClient:
    def __init__(self, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.containers = _FakeContainersCollection()
        self.api = _FakeExecApi(exit_code=exit_code, stdout=stdout, stderr=stderr)


def test_dump_database_leaves_no_file_when_the_write_fails_partway(tmp_path, monkeypatch):
    """(d1): a write that fails after `pg_dump` has already exited 0 --
    fault-injected via `os.fsync` raising, standing in for a disk-full or
    interrupted write between the OS accepting the bytes and the file
    becoming durable -- must leave behind neither a `.sql` file at the
    final name NOR a leftover `.partial` temp file, and must not disturb
    an older, already-valid dump: `_newest_dump_path` must still return
    that older dump afterward, so a failed attempt does not suppress the
    next scheduled backup (the exact failure ADR 0023's retention design
    exists to prevent)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project_dir = tmp_path / "svc_atomic"
    project_dir.mkdir()

    older = project_dir / dump_filename(datetime(2020, 1, 1, tzinfo=timezone.utc))
    older.write_text("-- old real dump\n", encoding="utf-8")

    monkeypatch.setattr(
        "os.fsync",
        lambda fd: (_ for _ in ()).throw(OSError("simulated disk failure during fsync")),
    )

    dest_path = str(project_dir / dump_filename(datetime(2024, 1, 1, tzinfo=timezone.utc)))
    client = _FakeDockerClient(exit_code=0, stdout=b"-- new dump that must never land\n")

    with pytest.raises(OSError):
        dump_database("svc_atomic", docker_client=client, dest_path=dest_path)

    names = os.listdir(project_dir)
    assert [n for n in names if n.endswith(DUMP_FILENAME_SUFFIX)] == [older.name]
    assert [n for n in names if n.endswith(PARTIAL_FILENAME_SUFFIX)] == []
    assert _newest_dump_path(str(project_dir)) == str(older)


def test_dump_database_writes_the_full_dump_with_no_temp_file_left_on_success(tmp_path, monkeypatch):
    """(d3)'s unit-level half (the Docker-integration half lives in
    `test_service_data_backup_integration.py`): the success path lands the
    complete content at `dest_path` and leaves no `.partial` file behind."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project_dir = tmp_path / "svc_atomic_ok"
    project_dir.mkdir()
    dest_path = str(project_dir / dump_filename(datetime(2024, 1, 1, tzinfo=timezone.utc)))
    content = b"-- a real pg_dump body\nCREATE TABLE items (id int);\n"
    client = _FakeDockerClient(exit_code=0, stdout=content)

    dump_database("svc_atomic_ok", docker_client=client, dest_path=dest_path)

    with open(dest_path, "rb") as f:
        assert f.read() == content
    names = os.listdir(project_dir)
    assert names == [os.path.basename(dest_path)]
