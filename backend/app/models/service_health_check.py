"""The worker's latest check of one ACTIVE service, one row per deployment.

The row is the persisted state of the check, so a worker restart resumes where it was: the failure
streak, the restart-count baseline and the state all live here, none in process memory. A row is
created by the first check of a deployment and deleted once the deployment is no longer ACTIVE, so
the table holds one row per live service and never grows.

* `state` is `HEALTHY` or `UNHEALTHY` (`services/service_health.ServiceState`): `UNHEALTHY` after
  `service_check_failure_threshold` consecutive failed checks, `HEALTHY` again after one pass. A
  deployment with no row has not been checked yet; the console shows that as such.
* `last_result` is `PASS`, `FAIL` or `UNKNOWN`: UNKNOWN is a check that could not be carried out
  (Docker unreachable), which neither counts as a failure nor as a pass.
* `last_failure_kind` is one of `FailureKind` and is kept after a recovery (it is the last one);
  `failing_since` is when the current streak began and is null while the last result is PASS.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ServiceHealthCheck(Base):
    __tablename__ = "service_health_checks"

    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    state: Mapped[str]
    last_result: Mapped[str]
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_kind: Mapped[str | None] = mapped_column(default=None)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_restart_count: Mapped[int | None] = mapped_column(Integer, default=None)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
