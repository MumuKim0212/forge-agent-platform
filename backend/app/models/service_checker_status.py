"""When the worker's service checker last started and finished a pass.

One row (`id` = `singleton`), written by `services/service_health` at the start and end of every pass,
even a pass with nothing to check. It is how the API process, which is not the worker, can tell a
checker that is running from one that has died: a dead checker leaves every service row looking as
it did, so the rows alone cannot say. No row = the checker never ran.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SINGLETON_ID = "singleton"


class ServiceCheckerStatus(Base):
    __tablename__ = "service_checker_status"

    id: Mapped[str] = mapped_column(primary_key=True)
    last_pass_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_pass_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
