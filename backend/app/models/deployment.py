"""Deployment lifecycle.

One row per L4 deployment attempt of a project's build. Durable history:
never rewritten, only appended to (a rollback creates a *new* row with
`rollback_of` set).
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin


class DeploymentStatus:
    """Deployment lifecycle.

    ```
    DEPLOYING -> ACTIVE | FAILED
    ACTIVE    -> SUPERSEDED | STOPPED
    ```
    `FAILED`/`SUPERSEDED`/`STOPPED` are terminal for that row (history is
    append-only — a new deployment attempt is always a new row, including
    rollback and restart-after-stop).
    """

    DEPLOYING = "DEPLOYING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    STOPPED = "STOPPED"

    TERMINAL = (FAILED, SUPERSEDED, STOPPED)


class DeploymentFailureReason:
    """Deployment failure taxonomy."""

    MERGE_FAILED = "MERGE_FAILED"
    IMAGE_MISSING = "IMAGE_MISSING"
    START_FAILED = "START_FAILED"
    HEALTH_TIMEOUT = "HEALTH_TIMEOUT"
    ROUTE_FAILED = "ROUTE_FAILED"
    # ADR 0023 §Data services: distinguished deliberately, per
    # the operator response to the first
    # is "set a value", to the second "investigate Postgres". Collapsing
    # either into INFRA_ERROR would make an unconfigured platform look
    # broken, or a real infra problem look like a config gap.
    PROVISION_UNCONFIGURED = "PROVISION_UNCONFIGURED"
    PROVISION_FAILED = "PROVISION_FAILED"
    INFRA_ERROR = "INFRA_ERROR"


class Deployment(IdMixin, CreatedAtMixin, Base):
    """"A Deployment records: project,
    build (and thereby image ref), who confirmed, rollback_of (nullable),
    state, timestamps, failure reason." `DEPLOYING` rows are this table's
    queue (mirrors `builds`, for the same durability reasons) — the worker
    claims and executes them; the API only ever creates a row in
    `DEPLOYING`."""

    __tablename__ = "deployments"

    # "At most one ACTIVE deployment
    # per project" — DB-level partial unique index (SQLite + Postgres),
    # belt-and-braces on top of the switchover marking old ACTIVE ->
    # SUPERSEDED in the same transaction that activates the new one.
    #
    # Per-project serialization:
    # "at most one DEPLOYING row per project" is enforced the same way. The
    # check-then-insert in `deploy.confirm_deploy`/`request_rollback` is
    # only a friendly early refusal; two concurrent requests can both pass
    # it, and this index is what makes the loser fail instead of leaving two
    # in-flight rows for one project (the worker would then run both).
    __table_args__ = (
        Index(
            "uq_deployments_one_active_per_project",
            "project_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "uq_deployments_one_deploying_per_project",
            "project_id",
            unique=True,
            sqlite_where=text("status = 'DEPLOYING'"),
            postgresql_where=text("status = 'DEPLOYING'"),
        ),
    )

    # also a plain index -- the two partial unique indexes above only cover ACTIVE and
    # DEPLOYING rows, and the timeline reads a project's whole history.
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"))
    # Who confirmed this deployment (owner/admin). Nullable would misrepresent
    # a human action as system-driven — every deployment is triggered by a
    # confirm/rollback/stop action with a real actor.
    confirmed_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # Set only when this row was created by a rollback
    # (which creates a new Deployment with rollback_of set). Points at
    # the SUPERSEDED deployment being rolled back to.
    rollback_of: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True)

    status: Mapped[str] = mapped_column(default=DeploymentStatus.DEPLOYING)
    # One of DeploymentFailureReason; set
    # only when status == FAILED.
    failure_reason: Mapped[str | None] = mapped_column(nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # the last value of this deployment's summed Traefik request counter
    # the worker saw (`services/traefik_traffic.py`). NULL: no series seen yet, which counts
    # from zero. A float because the exposition format's values are; counts stay exact far
    # beyond any real request volume.
    traffic_counter_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
