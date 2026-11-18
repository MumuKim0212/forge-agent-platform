"""ADR 0038: two real worker PROCESSES on one throwaway SQLite database.

`test_worker_lease_worker.py` drives `worker.main()` in the test process; this starts
`python -c <driver>` children that run `app.worker.main()` against a migrated scratch database
(`test_worker_sigterm_subprocess.database`, which runs `alembic upgrade head`, the new migration
included), the way an operator's second `python -m app.worker` would:

* a second worker against a live one exits with status 1 and a message naming the first one's host,
  pid and heartbeat age, while the first goes on running -- and the stop signal then frees the lease;
* a worker killed without a chance to release leaves a lease that a newcomer waits out and then takes.

The only change to the code under test is in the child's driver: the intervals are module constants
(ADR 0038 §Decision 3), shortened here (beat 0.3 s, stale window 5 s) so the test needs seconds,
not minutes. No Docker, engine or network: the reconciler is not built.
"""

from __future__ import annotations

import os
import signal
import socket
import time

import pytest
from sqlalchemy import select

from app.models.worker_lease import WorkerLease as WorkerLeaseRow
from tests.test_worker_sigterm_subprocess import IS_WINDOWS, _Child, database  # noqa: F401 - `database` is the fixture

_DRIVER = """
import os
print("DRIVER-PID", os.getpid(), flush=True)
import app.services.worker_lease as lease
lease.HEARTBEAT_INTERVAL_SECONDS = 0.3
lease.STALE_AFTER_SECONDS = 5.0
lease.WAIT_POLL_SECONDS = 0.2
import app.worker as w

w._new_reconciler = lambda: None   # no Docker in this process

# `_serve` installs the SIGTERM handler AFTER the lease is taken; a stop sent in between kills the
# process (-15) instead of ending it cleanly. `_recover_at_start` is the first call after the install.
_recover = w._recover_at_start
def _recover_after_install(*args, **kwargs):
    print("SIGTERM-HANDLER-INSTALLED", flush=True)
    return _recover(*args, **kwargs)
w._recover_at_start = _recover_after_install
w.main()
print("MAIN-RETURNED", flush=True)
"""


def _lease_row(make_session):
    with make_session() as db:
        row = db.scalars(select(WorkerLeaseRow)).first()
        if row is not None:
            db.expunge(row)
        return row


class _Worker(_Child):
    """On Windows the venv's `python.exe` is a launcher that starts the real interpreter as ITS child,
    so `proc.pid` is not the worker's pid (the pid the lease row and the message carry) and killing
    `proc` would leave the worker running. The driver prints its own pid first; this reads it."""

    def __init__(self, tmp_path, db_file):
        super().__init__(tmp_path, db_file, driver=_DRIVER)
        self.pid = 0

    def started(self) -> "_Worker":
        self.wait_for("DRIVER-PID", timeout=60)
        self.pid = int(self.lines[-1].split()[-1])
        return self

    def hard_kill(self) -> None:
        """No chance to release: the real interpreter is terminated, then its launcher is reaped."""
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM if IS_WINDOWS else signal.SIGKILL)  # Windows: TerminateProcess
            except OSError:
                pass
        self.kill()


@pytest.fixture()
def children(tmp_path, database):
    db_file, _ = database
    started: list[_Worker] = []

    def start() -> _Worker:
        child = _Worker(tmp_path, db_file).started()
        started.append(child)
        return child

    yield start
    for child in started:
        child.hard_kill()  # whatever a failed test left running


def test_a_second_real_worker_refuses_with_the_holders_host_pid_and_heartbeat_age_and_the_stop_frees_the_lease(
    children, database
):
    _, make_session = database
    first = children()
    first.wait_for("worker lease: acquired")

    second = children()
    code = second.finish(timeout=60)
    output = "\n".join(second.lines)

    assert code == 1, output
    assert "worker refuses to start" in output
    assert f"host {socket.gethostname()}" in output
    assert f"pid {first.pid}" in output
    assert "last heartbeat" in output and "s ago" in output
    assert "MAIN-RETURNED" not in output
    # it did nothing a worker does: no start-up recovery, no executor selection, no loop
    assert "using PlaceholderBuildExecutor" not in output
    assert "worker starting" in output  # (the banner precedes the lease; nothing after it ran)
    # and the first worker was not disturbed
    assert first.proc.poll() is None
    row = _lease_row(make_session)
    assert row.holder_pid == first.pid

    first.stop()
    assert first.finish() == 0
    freed = _lease_row(make_session)
    assert (freed.holder_id, freed.holder_host, freed.holder_pid) == (None, None, None)


def test_a_worker_killed_without_releasing_is_waited_out_then_replaced(children, database):
    _, make_session = database
    doomed = children()
    doomed.wait_for("worker lease: acquired")
    doomed.hard_kill()
    killed_at = time.monotonic()
    assert _lease_row(make_session).holder_pid == doomed.pid  # it never let go

    successor = children()
    successor.wait_for("worker lease: acquired", timeout=60)
    waited = time.monotonic() - killed_at

    assert waited >= 4.5, "the successor took a lease whose holder's last beat was under 5 s old"
    assert "waiting to see whether it is alive" in "\n".join(successor.lines)
    assert _lease_row(make_session).holder_pid == successor.pid
    successor.wait_for("SIGTERM-HANDLER-INSTALLED")  # not before: the signal would arrive ahead of the handler
    successor.stop()
    assert successor.finish() == 0
