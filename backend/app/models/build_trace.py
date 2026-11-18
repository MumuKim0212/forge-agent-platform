"""A build's stored trace.

One row per build that reached the agent: the LLM proxy's call record, the platform's
diff and the sandbox log tail. It is a DISPLAY record: nothing decides anything from
it (`tests/test_build_trace_capture.py` pins which files may
name this model), and it is stored beside the build and goes with it.

Every column that can be empty says WHY it is empty in a `*_state` column, so a
missing part is shown as unavailable rather than looking like nothing happened.
The content inside the JSON columns is bounded when it is written
(`services/build_trace.py`) and is agent-influenced: a surface renders it as plain
text, never as HTML.
"""

from sqlalchemy import JSON, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin


class BuildTrace(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "build_traces"

    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"), unique=True)
    # "captured" | "unavailable" (the proxy's record could not be read back).
    calls_state: Mapped[str]
    # The proxy's call record, normalised: `{"steps": [...], "calls_total",
    # "content_truncated", "omitted_calls", "omitted_tokens", "omitted_cost_usd"}`.
    calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "computed" | "not_reached" (the build ended before publish: nothing to diff) |
    # "unavailable" (the diff could not be computed).
    diff_state: Mapped[str]
    # `{"files": [{"path", "change", "content", "diff", "truncated"}], "files_total",
    # "truncated"}` -- see `build_trace.compute_diff`.
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The tail of the sandbox's combined output (an agent claim), None when the
    # adapter returned none.
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
