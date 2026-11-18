
from pydantic import BaseModel, ConfigDict

from app.schemas.types import UtcDateTime


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    build_id: str
    confirmed_by_id: str
    rollback_of: str | None
    status: str
    failure_reason: str | None
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    ended_at: UtcDateTime | None


class DeployConfirmInput(BaseModel):
    # Explicit build id (not "whatever is latest right now") so the owner
    # confirms exactly the build they reviewed — a race with a newer build
    # finishing mid-confirm is rejected by the latest-successful check
    # rather than silently deploying something else.
    build_id: str
