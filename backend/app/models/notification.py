"""Persisted state of operator notifications.

`NotificationState`: one row per (family, subject) -- `worker`/`worker`, `service`/`service:<project id>`,
`disk`/`<location label>`. `state` is `UP` or `DOWN`. The row is what makes a notice fire once: a
transition is won by a CONDITIONAL update on `state` (rows matched = 1), so with several API
processes or a restarted worker exactly one caller announces it. `down_notified` records whether
the current DOWN episode was announced (the cooldown can suppress it) and so whether a recovery is.

`NotificationChannelStatus`: one row per channel (`webhook`, `email`): the last attempt, its result
and the exception CLASS name that ended it. Never a message body, URL or response.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NotificationState(Base):
    __tablename__ = "notification_states"

    family: Mapped[str] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(primary_key=True)
    state: Mapped[str]
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    down_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_down_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class NotificationChannelStatus(Base):
    __tablename__ = "notification_channel_status"

    channel: Mapped[str] = mapped_column(primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_result: Mapped[str | None] = mapped_column(default=None)
    last_error_class: Mapped[str | None] = mapped_column(default=None)
