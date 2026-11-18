from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(IdMixin, Base):
    """Append-only. Never updated or deleted by application code. (The one deliberate exception
    is dev tooling, not the application: `demo_data --reset` deletes the rows that name only the
    fabricated demo dataset -- actor nobody or a demo user, target a demo entity -- and keeps
    every row a real person wrote.)"""

    __tablename__ = "audit_log"

    # Nullable: worker-driven build state transitions have no
    # human actor, so they write actor_id=None rather than a fabricated
    # "system user" row (keeps the users table free of non-human identities).
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str]
    # indexed -- every project timeline reads `target = 'project:<id>'` and
    # `target IN ('deployment:<id>', ...)`, and the un-archive lookup filters on it too.
    target: Mapped[str] = mapped_column(index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # indexed -- `GET /audit-log` pages newest first (`ORDER BY ts DESC LIMIT n`) over
    # the one table that only grows.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
