
from pydantic import BaseModel, ConfigDict

from app.schemas.types import UtcDateTime


class PolicyCheckResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    check_name: str
    result: str
    summary: str
    findings: list[dict] | None


class PolicyVerdictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    build_id: str
    risk_tier: str
    created_at: UtcDateTime
    checks: list[PolicyCheckResultOut] = []
