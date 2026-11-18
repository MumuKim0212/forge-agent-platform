"""The agent engine selection (ADR 0035 §Decision 5).

**Not under `/api/console`**: the console router stays read-only by construction
(`tests/test_console.py` pins its paths and methods), and the one write the console makes
here lives, like quota and role changes, on its own admin-only audited endpoint.

`GET` and `PATCH` both require `admin` as a dependency, so the role check runs before the
body is read. A change applies to builds claimed after it; one already running keeps the
engine it recorded (`worker.assign_engine` writes it at claim).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_role
from app.models.user import User
from app.schemas.engine import EngineSelectionOut, EngineSelectionUpdate, EngineStatusOut
from app.services import engines
from app.services.roles import Role

router = APIRouter(prefix="/platform/agent-engine", tags=["platform"])


def _response(db: Session) -> EngineSelectionOut:
    overview = engines.engine_overview(db, settings)
    return EngineSelectionOut(
        engines=[
            EngineStatusOut(
                engine=e.engine, available=e.available, model=e.model, image=e.image, selected=e.selected
            )
            for e in overview.engines
        ],
        selected=overview.selected,
        source=overview.source,
        set_by_id=overview.set_by_id,
        set_by_name=overview.set_by_name,
        set_at=overview.set_at,
    )


@router.get("", response_model=EngineSelectionOut)
def get_agent_engine(
    db: Session = Depends(get_db), _admin: User = Depends(require_role(Role.ADMIN))
) -> EngineSelectionOut:
    return _response(db)


@router.patch("", response_model=EngineSelectionOut)
def set_agent_engine(
    body: EngineSelectionUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.ADMIN)),
) -> EngineSelectionOut:
    try:
        engines.set_engine_selection(db, settings, actor_id=admin.id, engine=body.engine)
    except engines.EngineSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _response(db)
