"""The installation's own id (ADR 0037 §Decision 2).

One row, at most: the value stamped on every container and network this installation
creates as `forge.instance`. The reconciler touches only objects carrying it. It lives in
the platform database, and not in `.env` or a file next to the code, for three reasons:

* the reconciler's premise is "compare Docker objects with what the DATABASE says should
  exist", so the identity of the objects it may judge belongs to that database. A copy of
  the database restored elsewhere is the same installation; a fresh database on a host with
  old containers is a different one and cannot reap them (a file that outlived a deleted
  database would hand the new, empty database every old object to "reconcile" away);
* it is durable wherever the database is (a production Postgres, not a container's
  throwaway filesystem), so one installation keeps one id across restarts;
* a second checkout on the same Docker daemon has its own database, so it has its own id
  with nothing to configure and nothing to forget.

Same single-row discipline as `EngineSelection`: the primary key is a fixed literal that a
CHECK pins, so a second row cannot exist. No row means "not generated yet": the worker
creates it at its first start (`services/instance_id.get_or_create_instance_id`).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _utcnow

SINGLETON_ID = "platform"


class PlatformInstance(Base):
    __tablename__ = "platform_instance"
    __table_args__ = (CheckConstraint(f"id = '{SINGLETON_ID}'", name="ck_platform_instance_singleton"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=SINGLETON_ID)
    # A random uuid4 hex, generated once. Never derived from the hostname or a path.
    instance_id: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
