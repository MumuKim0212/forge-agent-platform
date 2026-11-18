"""The worker's backup sweep looks at a
sane rate, backs off per database after a failed dump, audits a failure once per backoff window, and
streams a dump to disk instead of holding it in memory.

Before: every 2-second worker tick opened an admin engine to enumerate the databases, and a dump
that kept failing was attempted -- and audit-logged -- on every tick (a failure leaves no file, so
the database stays "due"): about 43,000 rows a day per database.

The real `sweep_service_data_backups`, the real `is_due` (the filesystem), the real `dump_database`
and the real `write_audit_log` run; time is simulated by passing `now=` on 2-second ticks, as the
worker's loop advances. What is replaced is only the outside: the admin connection that enumerates
(`create_engine`, counted) and the Docker client that runs `pg_dump` (a fake whose exec exits
non-zero for the databases a test names, and counts the attempts). No Docker, no Postgres. Every
stream below is FINITE (`range(N)`): with a bound removed the test still ends.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest
from docker.errors import APIError

from app import worker
from app.config import settings
from app.models.audit import AuditLog
from app.services import service_data_backup
from app.services.service_data_backup import (
    PARTIAL_FILENAME_SUFFIX,
    ServiceDataBackupError,
    dump_database,
    retry_delay,
)
from tests.fake_admin_dsn import FAKE_ADMIN_DSN

# The sweeps `run_iteration` runs beside the backup one resolve the same DSN, so it must be one that
# can never connect (the shared fake; a DSN on the dev service Postgres's own port would make
# them knock on a real database).
ADMIN_DSN = FAKE_ADMIN_DSN
TICK = timedelta(seconds=2)
DAY_SECONDS = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _backup_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", ADMIN_DSN)
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 24)
    monkeypatch.setattr(settings, "service_data_backup_retention_days", 7)


# --- doubles ---------------------------------------------------------------------------------


class _AdminEngines:
    """Stands in for `create_engine` in `service_data_backup.list_service_databases`: counts how
    many times an admin engine is opened and answers the enumeration query."""

    def __init__(self, names: list[str]):
        self.names = names
        self.opened = 0

    def __call__(self, url, **kwargs):
        self.opened += 1
        return self

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        return self

    def fetchall(self):
        return [(name,) for name in self.names]

    def dispose(self):
        pass


class _PgDumpDocker:
    """A Docker client whose `pg_dump` exec exits 1 for the databases in `failing`, and otherwise
    writes a short dump. Counts every exec per database (the argument list ends with the name)."""

    def __init__(self, failing: set[str]):
        self.failing = set(failing)
        self.attempts: dict[str, list[int]] = {}
        self.clock = lambda: 0
        self._last: str | None = None
        self.containers = self
        self.api = self

    # containers.get(...)
    def get(self, name):
        return type("Container", (), {"id": "pg-container"})()

    def exec_create(self, container_id, cmd, environment=None):
        self._last = cmd[-1]
        self.attempts.setdefault(self._last, []).append(self.clock())
        return {"Id": "exec"}

    def exec_start(self, exec_id, stream=True, demux=True):
        return iter([(b"-- a dump\n", None)])

    def exec_inspect(self, exec_id):
        return {"ExitCode": 1 if self._last in self.failing else 0}


def _setup(monkeypatch, names: list[str]) -> _AdminEngines:
    engines = _AdminEngines(names)
    monkeypatch.setattr(service_data_backup, "create_engine", engines)
    return engines


def _run_ticks(db, docker, state, *, start: datetime, seconds: int, only_every: int = 1):
    """The worker's loop for `seconds` of simulated time, one sweep per 2-second tick."""
    ticks = seconds // 2
    for k in range(0, ticks, only_every):
        moment = start + TICK * k
        docker.clock = lambda moment=moment: int((moment - start).total_seconds())
        worker.sweep_service_data_backups(db, docker_client=docker, now=moment, scan_state=state)


def _failed_audits(db, database: str) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter_by(action="service_data_backup.failed", target=f"database:{database}")
        .order_by(AuditLog.ts, AuditLog.id)
        .all()
    )


# --- the backoff -----------------------------------------------------------------------------


def test_the_backoff_schedule_is_exponential_and_capped_at_the_cadence():
    assert [retry_delay(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 40)] == [
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(hours=1),
        timedelta(hours=2),
        timedelta(hours=4),
        timedelta(hours=8),
        timedelta(hours=16),
        timedelta(hours=24),  # the cadence: never retried more often than a healthy database is dumped
        timedelta(hours=24),
        timedelta(hours=24),
    ]


def test_a_dump_that_keeps_failing_is_attempted_once_per_backoff_window_and_audited_once_each(
    monkeypatch, db_session
):
    """A day of 2-second ticks (43,200 sweeps) against a database whose dump always fails.
    The attempts are at literal times derived from the schedule above: the first look at t=0, then
    after each failure the window doubles, and a retry waits for the next 15-minute look."""
    engines = _setup(monkeypatch, ["svc_failing"])
    docker = _PgDumpDocker(failing={"svc_failing"})
    start = datetime.now(timezone.utc)

    _run_ticks(db_session, docker, {}, start=start, seconds=DAY_SECONDS)

    assert docker.attempts["svc_failing"] == [0, 900, 2700, 6300, 13500, 27900, 56700]
    audits = _failed_audits(db_session, "svc_failing")
    assert len(audits) == 7  # one per window, not 43,200
    assert [a.metadata_json["consecutive_failures"] for a in audits] == [1, 2, 3, 4, 5, 6, 7]
    assert [a.metadata_json["next_attempt_not_before"] for a in audits] == [
        (start + timedelta(seconds=s)).isoformat() for s in (900, 2700, 6300, 13500, 27900, 56700, 114300)
    ]
    assert engines.opened == 96  # one look per 15 minutes, not one per tick


def test_the_ceiling_holds_after_the_schedule_has_run_out(monkeypatch, db_session):
    """Past the doubling: four days of ticks (only every 15th tick, to keep it quick -- the gates
    are all multiples of 15 minutes) settles at one attempt a day."""
    _setup(monkeypatch, ["svc_failing"])
    docker = _PgDumpDocker(failing={"svc_failing"})
    start = datetime.now(timezone.utc)

    _run_ticks(db_session, docker, {}, start=start, seconds=4 * DAY_SECONDS, only_every=450)

    times = docker.attempts["svc_failing"]
    assert times[:7] == [0, 900, 2700, 6300, 13500, 27900, 56700]
    assert times[7:] == [114300, 200700, 287100]  # 16 h, then a day apart: 57,600 s and 86,400 s


def test_a_database_that_fails_does_not_hold_back_one_that_does_not(monkeypatch, db_session):
    _setup(monkeypatch, ["svc_failing", "svc_healthy"])
    docker = _PgDumpDocker(failing={"svc_failing"})
    start = datetime.now(timezone.utc)

    _run_ticks(db_session, docker, {}, start=start, seconds=DAY_SECONDS - 600)

    assert docker.attempts["svc_healthy"] == [0]  # dumped on the first look, fresh for the day
    assert len(docker.attempts["svc_failing"]) == 7
    assert db_session.query(AuditLog).filter_by(action="service_data_backup.created", target="database:svc_healthy").count() == 1
    assert _failed_audits(db_session, "svc_healthy") == []
    # The healthy one's dump really is on disk, at the path the retention sweep reads.
    assert len(os.listdir(os.path.join(settings.service_data_backup_dir, "svc_healthy"))) == 1


def test_a_success_ends_the_backoff_and_the_next_failure_starts_the_schedule_again(monkeypatch, db_session):
    _setup(monkeypatch, ["svc_flaky"])
    docker = _PgDumpDocker(failing={"svc_flaky"})
    state: dict = {}
    start = datetime.now(timezone.utc)

    _run_ticks(db_session, docker, state, start=start, seconds=2 * 3600 + 2)  # attempts at 0, 900, 2700, 6300
    assert docker.attempts["svc_flaky"] == [0, 900, 2700, 6300]
    assert state["failures"]["svc_flaky"]["count"] == 4

    docker.failing.clear()  # the cause is fixed; the next attempt (6300 + 2 h = 13500) succeeds
    for k in range(3600 // 2, 4 * 3600 // 2):
        moment = start + TICK * k
        docker.clock = lambda moment=moment: int((moment - start).total_seconds())
        worker.sweep_service_data_backups(db_session, docker_client=docker, now=moment, scan_state=state)

    assert docker.attempts["svc_flaky"] == [0, 900, 2700, 6300, 13500]
    assert "svc_flaky" not in state["failures"]
    assert db_session.query(AuditLog).filter_by(action="service_data_backup.created", target="database:svc_flaky").count() == 1

    # It breaks again later: the count starts from 1, not from where it was.
    docker.failing.add("svc_flaky")
    later = start + timedelta(days=2)
    state["next_due_at"] = later
    worker.sweep_service_data_backups(db_session, docker_client=docker, now=later, scan_state=state)
    assert _failed_audits(db_session, "svc_flaky")[-1].metadata_json["consecutive_failures"] == 1


def test_a_database_that_no_longer_exists_is_forgotten(monkeypatch, db_session):
    engines = _setup(monkeypatch, ["svc_failing"])
    docker = _PgDumpDocker(failing={"svc_failing"})
    state: dict = {}
    start = datetime.now(timezone.utc)
    worker.sweep_service_data_backups(db_session, docker_client=docker, now=start, scan_state=state)
    assert "svc_failing" in state["failures"]

    engines.names = []  # dropped (deprovisioned) since
    worker.sweep_service_data_backups(
        db_session, docker_client=docker, now=start + timedelta(minutes=16), scan_state=state
    )

    assert state["failures"] == {}


# --- the look ----------------------------------------------------------------------------------


def test_a_tick_before_the_next_look_opens_no_engine_and_touches_nothing(monkeypatch, db_session):
    engines = _setup(monkeypatch, ["svc_a"])
    docker = _PgDumpDocker(failing=set())
    state: dict = {}
    start = datetime.now(timezone.utc)
    worker.sweep_service_data_backups(db_session, docker_client=docker, now=start, scan_state=state)
    assert engines.opened == 1 and docker.attempts == {"svc_a": [0]}
    audits_after_the_look = db_session.query(AuditLog).count()

    for k in range(1, 450):  # 2-second ticks up to 15 minutes short of one second
        result = worker.sweep_service_data_backups(
            db_session, docker_client=docker, now=start + TICK * k, scan_state=state
        )
        assert result == 0

    assert engines.opened == 1  # not one more admin connection
    assert db_session.query(AuditLog).count() == audits_after_the_look
    worker.sweep_service_data_backups(
        db_session, docker_client=docker, now=start + timedelta(minutes=15), scan_state=state
    )
    assert engines.opened == 2  # the next look


def test_the_look_is_advanced_before_enumerating_so_an_unreachable_postgres_is_not_retried_every_tick(
    monkeypatch, db_session
):
    opened = []

    def _unreachable(url, **kwargs):
        opened.append(1)
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(service_data_backup, "create_engine", _unreachable)
    state: dict = {}
    start = datetime.now(timezone.utc)

    for k in range(0, 450):
        worker.sweep_service_data_backups(db_session, docker_client=None, now=start + TICK * k, scan_state=state)

    assert len(opened) == 1


def test_a_cadence_shorter_than_the_look_interval_is_the_look_interval(monkeypatch):
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 0)
    assert service_data_backup.scan_interval() == timedelta(0)  # "every tick", as before
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 1)
    assert service_data_backup.scan_interval() == timedelta(minutes=15)
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 24)
    assert service_data_backup.scan_interval() == timedelta(minutes=15)


def test_the_look_interval_is_shorter_than_the_cadence_so_a_daily_backup_stays_daily(monkeypatch, db_session):
    """Why the interval is not the cadence: a look exactly one cadence after a dump finds the dump
    a few seconds too young. With the real filesystem check, dump at the first look, and the second
    dump is made at the first look AFTER the dump is a day old -- not two days later."""
    _setup(monkeypatch, ["svc_daily"])
    docker = _PgDumpDocker(failing=set())
    state: dict = {}
    start = datetime.now(timezone.utc)

    worker.sweep_service_data_backups(db_session, docker_client=docker, now=start, scan_state=state)
    (dump,) = os.listdir(os.path.join(settings.service_data_backup_dir, "svc_daily"))
    finished = (start + timedelta(seconds=1)).timestamp()  # the dump took a second, as the clock sees it
    os.utime(os.path.join(settings.service_data_backup_dir, "svc_daily", dump), (finished, finished))
    _run_ticks(db_session, docker, state, start=start, seconds=DAY_SECONDS + 1800, only_every=450)

    # At start + 24 h the dump is 86,399 s old -- a second too young. If the look interval were the
    # cadence, the next look would be 24 h later still. At 15 minutes it is the very next look.
    assert docker.attempts["svc_daily"] == [0, DAY_SECONDS + 900]


# --- streaming ----------------------------------------------------------------------------------

MIB = 1024 * 1024
CHUNK = bytes(MIB)  # one object, offered again and again: the source costs no memory itself
CHUNKS = 64  # a 64 MiB dump; a finite range, so a removed bound still ends


class _StreamingApi:
    """A `pg_dump` exec that yields `CHUNKS` chunks of 1 MiB and, each time the consumer comes
    back for the next, records how big the temp file in `dest_dir` is -- the consumer has by then
    handled the chunk it was given, so this is what has reached the disk so far."""

    def __init__(self, dest_dir, *, exit_code=0, stderr_chunks=0, fail_after: int | None = None):
        self.dest_dir = dest_dir
        self.exit_code = exit_code
        self.stderr_chunks = stderr_chunks
        self.fail_after = fail_after
        self.on_disk: list[int] = []
        self.sha = hashlib.sha256()

    def exec_create(self, container_id, cmd, environment=None):
        return {"Id": "exec"}

    def exec_start(self, exec_id, stream=True, demux=True):
        def _stream():
            for i in range(CHUNKS):
                if self.fail_after is not None and i == self.fail_after:
                    raise APIError("the docker stream broke")
                self.sha.update(CHUNK)
                yield (CHUNK, b"x" * 65536 if i < self.stderr_chunks else None)
                self.on_disk.append(
                    sum(
                        os.path.getsize(os.path.join(self.dest_dir, name))
                        for name in os.listdir(self.dest_dir)
                        if name.endswith(PARTIAL_FILENAME_SUFFIX)
                    )
                )

        return _stream()

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.exit_code}


class _StreamingDocker:
    def __init__(self, api):
        self.api = api
        self.containers = self

    def get(self, name):
        return type("Container", (), {"id": "pg-container"})()


def _sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(MIB):
            digest.update(block)
    return digest.hexdigest()


def test_a_large_dump_is_streamed_to_the_file_not_held_in_memory(tmp_path):
    dest_dir = tmp_path / "svc_big"
    dest_dir.mkdir()
    dest = str(dest_dir / "20300101T000000Z.sql")
    api = _StreamingApi(str(dest_dir))

    tracemalloc.start()
    try:
        dump_database("svc_big", docker_client=_StreamingDocker(api), dest_path=dest)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Bounded memory: one chunk at a time (a few MiB of headroom), not 64 MiB, not 128.
    assert peak < 8 * MIB, f"peak {peak / MIB:.1f} MiB while dumping {CHUNKS} MiB"
    # The bytes were reaching the disk while the dump was still arriving, not at the end: halfway
    # through the stream about half of it was already in the temp file.
    assert api.on_disk[CHUNKS // 2] >= (CHUNKS // 2) * MIB
    assert api.on_disk == sorted(api.on_disk) and api.on_disk[0] >= MIB
    # And nothing was lost or reordered on the way.
    assert os.path.getsize(dest) == CHUNKS * MIB
    assert _sha256_of(dest) == api.sha.hexdigest()
    assert os.listdir(dest_dir) == ["20300101T000000Z.sql"]  # the temp file is gone


def test_a_failed_dump_leaves_neither_the_final_file_nor_the_streamed_partial(tmp_path):
    dest_dir = tmp_path / "svc_big"
    dest_dir.mkdir()
    api = _StreamingApi(str(dest_dir), exit_code=2)

    with pytest.raises(ServiceDataBackupError, match="exited 2"):
        dump_database("svc_big", docker_client=_StreamingDocker(api), dest_path=str(dest_dir / "d.sql"))

    assert api.on_disk[-1] >= (CHUNKS - 1) * MIB  # a great deal had been streamed to the partial by then
    assert os.listdir(dest_dir) == []


def test_a_docker_failure_midway_leaves_nothing_behind_and_is_reported_as_a_backup_error(tmp_path):
    dest_dir = tmp_path / "svc_big"
    dest_dir.mkdir()
    api = _StreamingApi(str(dest_dir), fail_after=CHUNKS // 2)

    with pytest.raises(ServiceDataBackupError, match="pg_dump exec failed"):
        dump_database("svc_big", docker_client=_StreamingDocker(api), dest_path=str(dest_dir / "d.sql"))

    assert os.listdir(dest_dir) == []


def test_pg_dump_stderr_is_kept_only_up_to_a_bound_for_the_log(tmp_path, caplog):
    dest_dir = tmp_path / "svc_noisy"
    dest_dir.mkdir()
    api = _StreamingApi(str(dest_dir), stderr_chunks=16)  # 16 x 64 KiB = 1 MiB of stderr, exit 0

    with caplog.at_level(logging.WARNING, logger="app.service_data_backup"):
        dump_database("svc_noisy", docker_client=_StreamingDocker(api), dest_path=str(dest_dir / "d.sql"))

    (warning,) = [r for r in caplog.records if "wrote to stderr" in r.getMessage()]
    assert len(warning.getMessage()) < 70_000  # not 1 MiB
    assert os.path.getsize(dest_dir / "d.sql") == CHUNKS * MIB  # and none of it is in the dump


# --- wiring: the gate is only worth anything if the production loop passes its state -----------


def test_run_iteration_passes_the_backup_state_through_so_the_second_tick_does_not_look(monkeypatch, db_session):
    from app.services.build_executor import PlaceholderBuildExecutor

    engines = _setup(monkeypatch, ["svc_a"])
    monkeypatch.setattr(worker, "sweep_orphaned_preview_stores", lambda *a, **k: 0)  # not what this is about
    docker = _PgDumpDocker(failing=set())
    state: dict = {}

    for _ in range(3):
        worker.run_iteration(
            db_session, PlaceholderBuildExecutor(), backup_docker_client=docker, backup_scan_state=state
        )

    assert engines.opened == 1  # three ticks, one look


def test_main_hands_every_iteration_the_same_backup_state(monkeypatch):
    from tests.test_worker import _StopLoop, _drive_main

    seen: list = []

    def iteration(db, executor, **kwargs):
        seen.append(kwargs.get("backup_scan_state"))
        if len(seen) == 3:
            raise _StopLoop
        return object()  # something ran: no sleep, straight to the next tick

    _drive_main(monkeypatch, iteration)

    with pytest.raises(_StopLoop):
        worker.main()

    assert isinstance(seen[0], dict)
    assert seen[0] is seen[1] is seen[2]  # one dict for the life of the loop, or it gates nothing


# --- the sweep never raises, including while it prepares ---------------------------------


def _from_env_that_cannot_build_a_client(monkeypatch) -> list[int]:
    calls: list[int] = []

    def refuse():
        calls.append(1)
        raise RuntimeError("no docker daemon")

    monkeypatch.setattr("docker.from_env", refuse)
    return calls


def test_a_docker_client_that_cannot_be_built_is_recorded_per_due_database_and_not_raised(
    monkeypatch, db_session
):
    """Earlier, `ensure_backup_dir` and `docker.from_env()` sat outside any `try`: the sweep raised
    (against its own "never raises" docstring), no `service_data_backup.failed` row was written, and no
    backoff was applied. Now each due database is recorded the way a failed dump is."""
    _setup(monkeypatch, ["svc_one", "svc_two"])
    calls = _from_env_that_cannot_build_a_client(monkeypatch)
    start = datetime.now(timezone.utc)
    state: dict = {}

    result = worker.sweep_service_data_backups(db_session, docker_client=None, now=start, scan_state=state)

    assert result == 0
    assert len(calls) == 1  # one attempt to build the client for the whole sweep
    for name in ("svc_one", "svc_two"):
        [audit] = _failed_audits(db_session, name)
        assert audit.metadata_json["consecutive_failures"] == 1
        assert audit.metadata_json["next_attempt_not_before"] == (start + retry_delay(1)).isoformat()
        assert state["failures"][name] == {"count": 1, "retry_at": start + retry_delay(1)}


def test_a_failed_preparation_is_backed_off_like_a_failed_dump(monkeypatch, db_session):
    _setup(monkeypatch, ["svc_one"])
    calls = _from_env_that_cannot_build_a_client(monkeypatch)
    start = datetime.now(timezone.utc)
    state: dict = {}
    worker.sweep_service_data_backups(db_session, docker_client=None, now=start, scan_state=state)

    # One second before the retry time: the scan is let through, and only the backoff may stop it.
    state["next_due_at"] = start  # let the scan through; only the backoff may stop it
    worker.sweep_service_data_backups(
        db_session, docker_client=None, now=start + retry_delay(1) - timedelta(seconds=1), scan_state=state
    )

    assert len(calls) == 1
    assert len(_failed_audits(db_session, "svc_one")) == 1


def test_an_unwritable_backup_directory_is_recorded_and_not_raised(monkeypatch, db_session):
    _setup(monkeypatch, ["svc_one"])

    def refuse(base_dir):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(service_data_backup, "ensure_backup_dir", refuse)
    state: dict = {}

    result = worker.sweep_service_data_backups(
        db_session, docker_client=_PgDumpDocker(failing=set()), now=datetime.now(timezone.utc), scan_state=state
    )

    assert result == 0
    assert len(_failed_audits(db_session, "svc_one")) == 1
    assert state["failures"]["svc_one"]["count"] == 1


def test_retention_still_runs_when_preparation_failed(monkeypatch, db_session):
    _setup(monkeypatch, ["svc_one"])
    _from_env_that_cannot_build_a_client(monkeypatch)
    swept: list[int] = []
    monkeypatch.setattr(service_data_backup, "sweep_backup_retention", lambda *a, **k: swept.append(1))

    worker.sweep_service_data_backups(db_session, docker_client=None, now=datetime.now(timezone.utc), scan_state={})

    assert swept == [1]


def test_the_sweep_does_not_raise_whatever_its_body_raises(monkeypatch, db_session, caplog):
    """The guarantee is at the boundary: even a failure the body does not record itself (here the audit
    write of a failed dump) is logged and the tick counts as 0, instead of reaching `run_iteration`."""
    _setup(monkeypatch, ["svc_one"])

    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(worker, "write_audit_log", broken_audit)

    with caplog.at_level(logging.ERROR):
        result = worker.sweep_service_data_backups(
            db_session,
            docker_client=_PgDumpDocker(failing={"svc_one"}),
            now=datetime.now(timezone.utc),
            scan_state={},
        )

    assert result == 0
    assert "unexpected failure" in caplog.text
