"""the worker's host free-disk check and the console health read.

Thresholds are set per test from literals. An unmeasurable path is UNKNOWN, never OK. The hanging
reader releases itself after `HANG_CEILING_SECONDS`, so a missing bound fails an elapsed-time
assertion instead of hanging the run.
"""

from __future__ import annotations

import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.host_disk_check import HostDiskCheck
from app.models.worker_lease import WorkerLease
from app.services.host_disk import run_disk_check_if_due

GIB = 1024**3
Usage = namedtuple("Usage", "total used free")
HANG_CEILING_SECONDS = 6.0


class FakeDocker:
    def __init__(self, root=None, error=None):
        self._root, self._error = root, error

    def info(self):
        if self._error:
            raise self._error
        return {"DockerRootDir": self._root}


@pytest.fixture(autouse=True)
def _paths(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "local_git_root", str(tmp_path / "git"))
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path / "nope" / "backups"))
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@db.invalid/x")
    monkeypatch.setattr(settings, "host_disk_min_free_percent", 10.0)
    monkeypatch.setattr(settings, "host_disk_min_free_gb", 5.0)


def _rows(db):
    return {row.label: row for row in db.scalars(select(HostDiskCheck))}


def _plenty(_path):
    return Usage(1000 * GIB, 100 * GIB, 900 * GIB)


def test_every_location_ok_is_persisted(db_session, tmp_path):
    ran = run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=_plenty)
    rows = _rows(db_session)
    assert ran is True
    assert set(rows) == {"docker_data_root", "local_git_root", "backup_dir"}  # postgres: no db dir
    assert {row.state for row in rows.values()} == {"OK"}
    assert rows["docker_data_root"].free_bytes == 900 * GIB
    assert rows["docker_data_root"].free_percent == pytest.approx(90.0)


def test_sqlite_adds_the_database_directory(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=_plenty)
    assert "database_dir" in _rows(db_session)


def test_below_the_percent_threshold(db_session, tmp_path):
    run_disk_check_if_due(
        db_session,
        docker_client=FakeDocker(str(tmp_path)),
        usage_fn=lambda _p: Usage(1000 * GIB, 950 * GIB, 50 * GIB),  # 5% free, 50 GB free
    )
    assert {row.state for row in _rows(db_session).values()} == {"BELOW_THRESHOLD"}


def test_below_the_absolute_threshold_even_with_plenty_of_percent(db_session, tmp_path):
    run_disk_check_if_due(
        db_session,
        docker_client=FakeDocker(str(tmp_path)),
        usage_fn=lambda _p: Usage(20 * GIB, 16 * GIB, 4 * GIB),  # 20% free, 4 GB free
    )
    assert {row.state for row in _rows(db_session).values()} == {"BELOW_THRESHOLD"}


def test_docker_info_failing_is_unknown_not_ok(db_session):
    run_disk_check_if_due(
        db_session, docker_client=FakeDocker(error=RuntimeError("no daemon")), usage_fn=_plenty
    )
    row = _rows(db_session)["docker_data_root"]
    assert (row.state, row.path, row.free_bytes, row.total_bytes, row.free_percent) == (
        "UNKNOWN",
        None,
        None,
        None,
        None,
    )


def test_a_reader_that_raises_is_unknown(db_session, tmp_path):
    def boom(_path):
        raise PermissionError("denied")

    run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=boom)
    assert {row.state for row in _rows(db_session).values()} == {"UNKNOWN"}


def test_a_hanging_reader_is_unknown_within_the_bound(db_session, tmp_path):
    release = threading.Event()

    def hang(_path):
        release.wait(HANG_CEILING_SECONDS)
        return Usage(1000 * GIB, 100 * GIB, 900 * GIB)  # a late "OK" a missing bound would record

    started = time.monotonic()
    try:
        run_disk_check_if_due(
            db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=hang, timeout=0.2
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert {row.state for row in _rows(db_session).values()} == {"UNKNOWN"}
    assert elapsed < 3.0


def test_a_missing_directory_is_measured_at_its_nearest_existing_ancestor(db_session, tmp_path):
    seen = []

    def spy(path):
        seen.append(path)
        return Usage(1000 * GIB, 100 * GIB, 900 * GIB)

    run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=spy)
    backup = _rows(db_session)["backup_dir"]
    assert backup.state == "OK"
    assert backup.path.endswith("backups")
    assert str(tmp_path / "nope") not in seen and str(tmp_path / "nope" / "backups") not in seen
    assert str(tmp_path) in seen


def test_a_docker_root_missing_on_this_host_is_unknown_even_when_an_ancestor_exists(
    db_session, tmp_path
):
    ghost = str(tmp_path / "var" / "lib" / "docker")  # tmp_path exists, so a walk-up would read it
    seen = []

    def spy(path):
        seen.append(path)
        return Usage(1000 * GIB, 100 * GIB, 900 * GIB)

    run_disk_check_if_due(db_session, docker_client=FakeDocker(ghost), usage_fn=spy)
    row = _rows(db_session)["docker_data_root"]
    assert (row.state, row.path, row.free_bytes) == ("UNKNOWN", ghost, None)
    # the backup dir (missing too) is still measured at its ancestor: one side per rule
    assert _rows(db_session)["backup_dir"].state == "OK"


def test_an_existing_docker_root_is_measured(db_session, tmp_path):
    run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=_plenty)
    assert _rows(db_session)["docker_data_root"].state == "OK"


def test_cadence_and_overwrite_not_growth(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "host_disk_check_interval_seconds", 300.0)
    state: dict = {}
    t0 = datetime(2026, 11, 14, tzinfo=timezone.utc)
    kw = dict(docker_client=FakeDocker(str(tmp_path)), usage_fn=_plenty, scan_state=state)
    assert run_disk_check_if_due(db_session, now=t0, **kw) is True
    assert run_disk_check_if_due(db_session, now=t0 + timedelta(seconds=299), **kw) is False
    assert run_disk_check_if_due(db_session, now=t0 + timedelta(seconds=301), **kw) is True
    assert len(_rows(db_session)) == 3
    assert all(
        row.checked_at.replace(tzinfo=timezone.utc) == t0 + timedelta(seconds=301)
        for row in _rows(db_session).values()
    )


def test_a_label_no_longer_checked_is_removed(db_session, tmp_path):
    db_session.add(HostDiskCheck(label="gone", state="OK", checked_at=datetime.now(timezone.utc)))
    db_session.commit()
    run_disk_check_if_due(db_session, docker_client=FakeDocker(str(tmp_path)), usage_fn=_plenty)
    assert "gone" not in _rows(db_session)


def test_the_check_never_raises(db_session, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "execute", explode)
    assert run_disk_check_if_due(db_session, docker_client=FakeDocker("/x"), usage_fn=_plenty) is False


# -- the console read ------------------------------------------------------------------------


def _admin_headers(db_session):
    from app.models.user import User

    return {"X-Forge-User-Id": db_session.query(User).filter_by(role="admin").first().id}


def test_console_health_reports_lease_and_disks(client, db_session, tmp_path):
    now = datetime.now(timezone.utc)
    db_session.add(
        WorkerLease(
            id="platform",
            holder_id="h",
            holder_host="h",
            holder_pid=1,
            acquired_at=now,
            heartbeat_at=now - timedelta(seconds=7),
        )
    )
    db_session.commit()
    run_disk_check_if_due(
        db_session,
        docker_client=FakeDocker(str(tmp_path)),
        usage_fn=lambda _p: Usage(20 * GIB, 16 * GIB, 4 * GIB),
    )
    body = client.get("/api/console/health", headers=_admin_headers(db_session)).json()
    # The per-service section (`services`, `service_failure_threshold`) was added later; the original four
    # keys and their values are unchanged and still asserted below.
    assert set(body) == {
        "worker", "disk_min_free_percent", "disk_min_free_gb", "disks",
        "service_failure_threshold", "services", "service_checker",
        # the two notification channels and the test message's minimum interval.
        "notification_channels", "notification_test_min_interval_seconds",
    }
    assert body["worker"]["state"] == "HELD"
    assert 6.0 < body["worker"]["heartbeat_age_seconds"] < 30.0
    assert body["worker"]["stale_after_seconds"] == 60.0
    assert body["disk_min_free_percent"] == 10.0 and body["disk_min_free_gb"] == 5.0
    assert {d["state"] for d in body["disks"]} == {"BELOW_THRESHOLD"}
    assert set(body["disks"][0]) == {
        "label",
        "path",
        "free_bytes",
        "total_bytes",
        "free_percent",
        "state",
        "checked_at",
    }
    assert set(body["worker"]) == {"state", "heartbeat_age_seconds", "stale_after_seconds"}


def test_console_health_before_anything_ran(client, db_session):
    body = client.get("/api/console/health", headers=_admin_headers(db_session)).json()
    assert body["worker"] == {"state": "NONE", "heartbeat_age_seconds": None, "stale_after_seconds": 60.0}
    assert body["disks"] == []


# -- the worker loop wiring ------------------------------------------------------------------


def test_run_iteration_runs_the_disk_check_only_for_a_caller_with_a_cadence_dict(
    db_session, tmp_path
):
    from app import worker
    from app.services.build_executor import PlaceholderBuildExecutor

    worker.run_iteration(db_session, PlaceholderBuildExecutor())
    assert _rows(db_session) == {}  # no cadence dict: a unit-test tick never touches the host

    state: dict = {}
    worker.run_iteration(
        db_session,
        PlaceholderBuildExecutor(),
        disk_check_state=state,
        disk_check_client=FakeDocker(str(tmp_path)),
    )
    assert "docker_data_root" in _rows(db_session)
    assert "next_due_at" in state
