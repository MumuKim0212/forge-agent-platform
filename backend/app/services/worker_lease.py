"""One worker per installation, held by a database lease (ADR 0038).

A worker's START is destructive: it fails every claimed build and deployment it finds
(`worker.recover_orphaned_rows`) and its reconciler then removes their containers. That is sound
only if it is the one worker. This module makes that true: a worker takes the lease
(`models/worker_lease.py`, one row) before recovery, the first reconcile and the first claim, keeps
it alive from a thread of its own, re-checks it before each destructive step, and releases it on
a graceful stop.

**Taking it (`WorkerLease.acquire`).** Free (no row, or released) or stale (no heartbeat for
`STALE_AFTER_SECONDS`) -> take it with a conditional `UPDATE`, whose row count says whether this
process won. Held and fresh -> watch the heartbeat: if it CHANGES while we wait, the holder is a
live worker and `LeaseHeld` is raised (the worker prints it and exits 1); if it does not, the holder
was killed and the wait ends at the stale window, then the lease is taken. "Alive" is therefore
decided by watching the heartbeat move, never by comparing two machines' clocks; the clock is
read only for the stale cut-off, one window long.

**Sessions (the thread separation).** `WorkerLease` takes a session FACTORY and never a session.
Every operation opens its own short session, runs one statement, commits and closes. The worker
loop's session (`worker._run_loop_tick`) is a different object, never handed to the lease, and the
heartbeat thread has no way to reach it: the API has no parameter for one. The only shared
resource is the database itself, where a renewal is one single-row statement; if SQLite makes it
wait on the loop's write lock and the busy timeout expires, that beat is logged and missed (the
window is 60 s against a 10 s beat), and it is never read as "lost".

**Losing it.** Only a definite answer is a loss: the row names another holder (a takeover), or
our own heartbeat is older than the stale window (we could be taken over at any moment, so we do
not proceed). `require()` raises `LeaseLost`, a `SystemExit` with status 1, so nothing in the loop
that swallows `Exception` (`worker._run_loop_tick`) can swallow it. A database error while
checking is an ordinary exception: that step is skipped and tried again, never run unverified.

The intervals are constants (ADR 0038 §Decision 3), read at call time through this module so a
test's subprocess can shorten them.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.worker_lease import SINGLETON_ID, WorkerLease as WorkerLeaseRow

logger = logging.getLogger("app.worker_lease")

HEARTBEAT_INTERVAL_SECONDS = 10.0
STALE_AFTER_SECONDS = 60.0
# How often a waiting worker re-reads the row. Short against the beat, so a live holder is
# noticed within about one beat of its next renewal.
WAIT_POLL_SECONDS = 1.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes for a `timezone=True` column; every value written here
    is UTC, so a naive one is UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class LeaseHeld(Exception):
    """Another worker holds the lease and is alive. Carries what the operator needs to find it."""

    def __init__(self, host: str | None, pid: int | None, heartbeat_age_seconds: float) -> None:
        self.host = host
        self.pid = pid
        self.heartbeat_age_seconds = heartbeat_age_seconds
        super().__init__(
            f"another worker already holds this installation's lease: host {host or 'unknown'}, "
            f"pid {pid if pid is not None else 'unknown'}, last heartbeat "
            f"{heartbeat_age_seconds:.0f}s ago. Only one worker may run per installation "
            "(ADR 0038); stop that worker first, then start this one."
        )


class LeaseLost(SystemExit):
    """This worker no longer holds the lease. A `SystemExit` with status 1 (a lost lease is a
    failure, unlike a requested stop), and deliberately not an `Exception`: the worker loop
    survives every `Exception` an iteration raises, and this one must end the process."""

    def __init__(self, detail: str) -> None:
        super().__init__(1)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class WorkerLease:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
        host: str | None = None,
        pid: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._sleep = sleep
        self.holder_id = uuid.uuid4().hex
        self.host = host if host is not None else socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- one statement per session ---------------------------------------------------------

    def _read(self) -> WorkerLeaseRow | None:
        db = self._session_factory()
        try:
            row = db.scalars(select(WorkerLeaseRow).where(WorkerLeaseRow.id == SINGLETON_ID)).first()
            if row is not None:
                db.expunge(row)  # a plain snapshot: nothing lazy, nothing tied to this session
            return row
        finally:
            db.close()

    def _execute(self, statement) -> int:
        db = self._session_factory()
        try:
            result = db.execute(statement)
            db.commit()
            return result.rowcount
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _insert(self, now: datetime) -> bool:
        db = self._session_factory()
        try:
            db.add(
                WorkerLeaseRow(
                    id=SINGLETON_ID,
                    holder_id=self.holder_id,
                    holder_host=self.host,
                    holder_pid=self.pid,
                    acquired_at=now,
                    heartbeat_at=now,
                )
            )
            try:
                db.commit()
            except IntegrityError:  # another worker inserted the first row a moment before us
                db.rollback()
                return False
            return True
        finally:
            db.close()

    # -- acquire ---------------------------------------------------------------------------

    def acquire(self) -> None:
        """Takes the lease, waiting out a dead holder; raises `LeaseHeld` for a live one."""
        watching: tuple[str, datetime] | None = None  # (holder, heartbeat) first seen held
        announced = False
        while True:
            now = self._clock()
            row = self._read()
            if row is None:
                if self._insert(now):
                    return self._acquired()
                continue  # lost the race to insert; read what the winner wrote

            heartbeat = _aware(row.heartbeat_at)
            age = (now - heartbeat).total_seconds()
            stale_before = now - timedelta(seconds=STALE_AFTER_SECONDS)
            if row.holder_id is None or row.holder_id == self.holder_id or age >= STALE_AFTER_SECONDS:
                won = self._execute(
                    update(WorkerLeaseRow)
                    .where(
                        WorkerLeaseRow.id == SINGLETON_ID,
                        (WorkerLeaseRow.holder_id.is_(None))
                        | (WorkerLeaseRow.holder_id == self.holder_id)
                        | (WorkerLeaseRow.heartbeat_at <= stale_before),
                    )
                    .values(
                        holder_id=self.holder_id,
                        holder_host=self.host,
                        holder_pid=self.pid,
                        acquired_at=now,
                        heartbeat_at=now,
                    )
                )
                if won == 1:
                    return self._acquired()
                self._sleep(WAIT_POLL_SECONDS)  # never a hot loop on a row we keep losing
                continue  # someone else won between our read and our write; look again

            # Held, and fresh as of the last beat. A live worker's beat moves within one beat
            # interval; a killed one's never does.
            if watching is None:
                watching = (row.holder_id, heartbeat)
            elif (row.holder_id, heartbeat) != watching:
                raise LeaseHeld(row.holder_host, row.holder_pid, max(age, 0.0))
            if not announced:
                announced = True
                logger.warning(
                    "worker lease: held by host %s pid %s (last heartbeat %.0fs ago); waiting to see "
                    "whether it is alive -- a dead holder is waited out for up to %.0fs from its last beat",
                    row.holder_host,
                    row.holder_pid,
                    max(age, 0.0),
                    STALE_AFTER_SECONDS,
                )
            self._sleep(WAIT_POLL_SECONDS)

    def _acquired(self) -> None:
        logger.info("worker lease: acquired (holder %s, host %s, pid %s)", self.holder_id, self.host, self.pid)

    # -- keep it ---------------------------------------------------------------------------

    def renew(self) -> bool:
        """Moves our heartbeat to now. False means the row no longer names us: the lease was
        taken over. A database error is raised, not answered: it says nothing about ownership."""
        now = self._clock()
        renewed = self._execute(
            update(WorkerLeaseRow)
            .where(WorkerLeaseRow.id == SINGLETON_ID, WorkerLeaseRow.holder_id == self.holder_id)
            .values(heartbeat_at=now)
        )
        return renewed == 1

    def start_heartbeat(self) -> None:
        """Renews from a thread of its own, because a build blocks the worker loop for minutes."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._heartbeat_loop, name="worker-lease-heartbeat", daemon=True)
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                if not self.renew():
                    self._lost.set()
                    logger.error("worker lease: another worker has taken the lease; this worker will stop")
                    return
            except Exception:  # noqa: BLE001 - a missed beat is not a lost lease
                logger.warning("worker lease: could not renew the heartbeat; trying again", exc_info=True)

    def require(self) -> None:
        """Called before each destructive step (a timeout sweep, a reconcile pass, a claim).
        Returns when the lease is still ours; raises `LeaseLost` when it is not."""
        if self._lost.is_set():
            raise LeaseLost("the worker lease was taken over by another worker")
        row = self._read()
        if row is None or row.holder_id != self.holder_id:
            self._lost.set()
            raise LeaseLost("the worker lease is held by another worker")
        age = (self._clock() - _aware(row.heartbeat_at)).total_seconds()
        if age >= STALE_AFTER_SECONDS:
            # Nobody has taken it yet, but anybody now may; do not act on it.
            self._lost.set()
            raise LeaseLost(
                f"this worker's lease heartbeat is {age:.0f}s old (stale after {STALE_AFTER_SECONDS:.0f}s)"
            )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    # -- let go ----------------------------------------------------------------------------

    def release(self) -> bool:
        """Graceful stop: ends the heartbeat, then frees the row so the next worker starts at
        once. True when the row was ours and is now free. Never raises: a worker that cannot
        release is stopping anyway, and its lease goes stale."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        try:
            freed = self._execute(
                update(WorkerLeaseRow)
                .where(WorkerLeaseRow.id == SINGLETON_ID, WorkerLeaseRow.holder_id == self.holder_id)
                .values(holder_id=None, holder_host=None, holder_pid=None)
            )
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("worker lease: could not release it; it goes stale instead", exc_info=True)
            return False
        if freed == 1:
            logger.info("worker lease: released")
        return freed == 1
