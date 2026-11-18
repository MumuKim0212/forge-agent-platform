
from pydantic import BaseModel, ConfigDict

from app.schemas.types import UtcDateTime


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str | None
    action: str
    target: str
    metadata_json: dict | None
    ts: UtcDateTime
