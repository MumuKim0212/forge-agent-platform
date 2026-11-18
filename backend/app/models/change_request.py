from sqlalchemy import ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin


class ChangeRequest(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "change_requests"
    # at most one change request per
    # build, so "who requested this build" has exactly one answer and the
    # self-approval ban cannot be handed an ambiguous one. NULLs stay distinct.
    __table_args__ = (UniqueConstraint("build_id", name="uq_change_requests_build_id"),)

    # Null project_id would represent a brand-new request before a project
    # exists; this slice always creates Project + ChangeRequest together, so
    # it is non-null in practice but kept nullable for forward-compat.
    # indexed -- the timeline and the change-request list filter on it.
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    requester_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    nl_request: Mapped[str]
    intent_delta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    build_id: Mapped[str | None] = mapped_column(ForeignKey("builds.id"), nullable=True)
