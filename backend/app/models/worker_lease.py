"""The worker lease (ADR 0038 §Decision 1): which worker process currently owns this
installation's destructive work (start-up recovery, the reconciler, timeout sweeps, claims).

One row, at most, beside `platform_instance`: the primary key is a fixed literal that a CHECK
pins, so a second row cannot exist. No row is "never held"; the first worker to start inserts
it (`services/worker_lease.py`), so the migration invents nothing. A released lease keeps its
row with the holder columns NULL.

It is taken, renewed and released by conditional `UPDATE`s whose row count says who won, which
means the same statements work on SQLite and on Postgres. The timestamps are written from the
holder's clock, compared on another worker's: the 60 s stale window is long against any
plausible skew, and a waiting worker decides "alive" by watching `heartbeat_at` change, not by
reading the clock.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SINGLETON_ID = "platform"


class WorkerLease(Base):
    __tablename__ = "worker_lease"
    __table_args__ = (CheckConstraint(f"id = '{SINGLETON_ID}'", name="ck_worker_lease_singleton"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=SINGLETON_ID)
    # A random uuid4 hex, per worker PROCESS. NULL: released, nobody holds it.
    holder_id: Mapped[str | None] = mapped_column(default=None)
    # For the operator's refusal message only; nothing is decided from them.
    holder_host: Mapped[str | None] = mapped_column(default=None)
    holder_pid: Mapped[int | None] = mapped_column(default=None)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
