"""`GET /api/builds/{id}/trace`.

Two labelled halves, never mixed: `measured` (what the platform observed or computed)
and `claims` (what the agent, or the sandbox on its behalf, said). Every string in either
is agent-influenced data: a surface shows it as plain text, with invisible characters made
visible, never linkified and never as HTML. Tool use is a REQUEST the model made and a
tool result is what L3 REPORTED -- the proxy never sees whether a tool ran.
"""


from pydantic import BaseModel

from app.schemas.types import UtcDateTime


class TraceThinkingOut(BaseModel):
    """A thinking block: that it was there and how long it was, never its text."""

    chars: int


class TraceStepMeasured(BaseModel):
    ordinal: int
    started_at: str | None
    ended_at: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    tokens: int | None
    cost_usd: float | None
    stop_reason: str | None
    # "recorded" | "unparsed" (content could not be read) | "dropped" (a bound was hit).
    content: str
    thinking: list[TraceThinkingOut]


class TraceCallsMeasured(BaseModel):
    # "captured" | "unavailable" (the proxy's record could not be read back).
    state: str
    steps: list[TraceStepMeasured]
    calls_total: int
    content_truncated: bool
    # Calls past the record's step ceiling, folded into totals so the sums still reconcile.
    omitted_calls: int
    omitted_tokens: int
    omitted_cost_usd: float


class DiffFileOut(BaseModel):
    """One changed file. `path` and the characters of `diff` were chosen by the agent."""

    path: str
    change: str  # "added" | "modified" | "deleted"
    # "text" | "binary" | "too_large" | "total_limit": why `diff` is, or is not, present.
    content: str
    diff: str | None
    truncated: bool


class TraceDiffOut(BaseModel):
    # "computed" | "not_reached" (the build ended before publish) | "unavailable".
    state: str
    files: list[DiffFileOut]
    files_total: int
    truncated: bool


class TraceMeasuredOut(BaseModel):
    calls: TraceCallsMeasured
    diff: TraceDiffOut


class TraceBlockClaim(BaseModel):
    """A content block. `assistant_text`, `tool_use` (REQUESTED: `name` + `text` is its
    input as JSON) and `tool_result` (REPORTED by the sandbox), or `other` (type only)."""

    kind: str
    text: str | None = None
    chars: int | None = None
    truncated: bool = False
    id: str | None = None
    name: str | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    type: str | None = None


class TraceStepClaims(BaseModel):
    ordinal: int
    blocks: list[TraceBlockClaim]


class TraceLogOut(BaseModel):
    text: str | None
    truncated: bool


class TraceClaimsOut(BaseModel):
    calls: list[TraceStepClaims]
    log: TraceLogOut


class BuildTraceOut(BaseModel):
    build_id: str
    captured_at: UtcDateTime
    measured: TraceMeasuredOut
    claims: TraceClaimsOut
