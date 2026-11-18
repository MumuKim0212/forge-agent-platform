
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.types import UtcDateTime


# every accepted request is placed
# verbatim in the build agent's prompt, and the LLM proxy checks the per-build budget
# only before a call, so nothing else bounds this text. The API rejects (422) rather
# than truncates -- a silently shortened request is a different request. Counted in
# characters as Python `len` does (code points); no normalisation is applied first.
# The frontend mirrors it (`frontend/src/api/requestLimits.ts`).
NL_REQUEST_MAX_CHARS = 8000


class ChangeRequestCreate(BaseModel):
    nl_request: str = Field(min_length=1, max_length=NL_REQUEST_MAX_CHARS)


class ChangeRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str | None
    requester_id: str
    nl_request: str
    intent_delta: dict | None
    build_id: str | None
    created_at: UtcDateTime
