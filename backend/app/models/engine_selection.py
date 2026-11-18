"""The platform-level agent engine selection (ADR 0035 §Decision 5).

One row, at most: which engine builds run on, who set it and when. It is DB state and not
a `Settings` field because an admin changes it at will, without a restart or a deploy.
Credentials, models and images stay in `.env`; only the CHOICE lives here.

A single-row table is enforced by the database, not by habit: the primary key is a fixed
literal that a CHECK pins, so a second row cannot exist. No row means "no one has chosen":
`services/engines` then falls back to the environment default, or the only available engine.
The row is read by `engines.resolve_engine` on every claim and cached nowhere.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _utcnow

SINGLETON_ID = "agent_engine"


class EngineSelection(Base):
    __tablename__ = "engine_selection"
    __table_args__ = (CheckConstraint(f"id = '{SINGLETON_ID}'", name="ck_engine_selection_singleton"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=SINGLETON_ID)
    # `anthropic` | `openai` (`services/engines.ENGINES`); validated by the service that
    # writes it, a plain string column like every other status-like value here.
    engine: Mapped[str]
    set_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
