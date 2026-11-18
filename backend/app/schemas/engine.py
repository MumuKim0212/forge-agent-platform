"""ADR 0035 §Decision 5: the agent engine selection's request and response shapes.

Hand-built from named values (no `from_attributes`), because these sit next to the
credential-bearing `Settings`: there is no path in them for a key, and no "is set" flag
beyond `available`, which is the one fact the console may state about a key
.
"""


from pydantic import BaseModel

from app.schemas.types import UtcDateTime


class EngineStatusOut(BaseModel):
    engine: str
    available: bool
    model: str | None  # the configured model; None for an engine that runs its own default
    image: str
    selected: bool


class EngineSelectionOut(BaseModel):
    engines: list[EngineStatusOut]
    selected: str
    source: str  # "stored" (an admin's choice) | "default" (nobody has chosen)
    set_by_id: str | None
    set_by_name: str | None
    set_at: UtcDateTime | None


class EngineSelectionUpdate(BaseModel):
    engine: str
