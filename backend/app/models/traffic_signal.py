"""The state of the traffic signal read from Traefik's counters.

One row, at most, beside `platform_instance` and `worker_lease`: the primary key is a fixed
literal that a CHECK pins. No row is "never polled"; the first successful poll inserts it, so
the migration invents nothing. The per-deployment half of the persisted state is
`Deployment.traffic_counter_seen`.

* `traefik_started_at` -- the Traefik process start time seen at the last successful poll
  (`process_start_time_seconds`); a change means Traefik restarted and its counters reset.
* `last_poll_at` -- the time of the last successful poll. A failed poll never moves it.
* `observed_since` -- the start of the current unbroken observation. The idle sweep may judge
  a live service only when this is at least one idle threshold in the past.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SINGLETON_ID = "platform"


class TrafficSignalState(Base):
    __tablename__ = "traffic_signal_state"
    __table_args__ = (CheckConstraint(f"id = '{SINGLETON_ID}'", name="ck_traffic_signal_state_singleton"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=SINGLETON_ID)
    traefik_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
