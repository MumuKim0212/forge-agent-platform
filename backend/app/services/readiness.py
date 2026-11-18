"""Readiness: is the platform database reachable and the worker lease fresh.

`failing_checks` returns only the names `database` and/or `worker` -- the whole of what the
unauthenticated endpoint may reveal. If the database cannot be read, the lease cannot be judged
either, so only `database` is named. A lease is fresh when a row exists, a holder is set, and its
heartbeat is within `worker_lease.STALE_AFTER_SECONDS` (read at call time). No row (never held) and
a released row (graceful stop) are both "no live worker".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.worker_lease import SINGLETON_ID, WorkerLease as WorkerLeaseRow
from app.services import worker_lease

logger = logging.getLogger("app.readiness")

DATABASE = "database"
WORKER = "worker"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class LeaseView:
    """What the lease row says right now; `state` is NONE / RELEASED / HELD / STALE."""

    def __init__(self, state: str, heartbeat_age_seconds: float | None) -> None:
        self.state = state
        self.heartbeat_age_seconds = heartbeat_age_seconds


def read_lease(db: Session, now: datetime | None = None) -> LeaseView:
    row = db.get(WorkerLeaseRow, SINGLETON_ID)
    if row is None:
        return LeaseView("NONE", None)
    moment = now or datetime.now(timezone.utc)
    age = max(0.0, (moment - _aware(row.heartbeat_at)).total_seconds())
    if row.holder_id is None:
        return LeaseView("RELEASED", age)
    return LeaseView("STALE" if age > worker_lease.STALE_AFTER_SECONDS else "HELD", age)


def failing_checks(session_factory: Callable[[], Session]) -> list[str]:
    """Blocking; the caller bounds it with a timeout. Never raises."""
    try:
        with session_factory() as db:
            db.execute(select(1))
            view = read_lease(db)
    except Exception:  # noqa: BLE001 - any failure to read is "database"; detail stays in the log
        logger.warning("readiness: the database check failed", exc_info=True)
        return [DATABASE]
    return [] if view.state == "HELD" else [WORKER]
