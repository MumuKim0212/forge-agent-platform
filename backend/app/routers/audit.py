from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_role
from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.audit import AuditLogOut
from app.services.roles import Role

router = APIRouter(prefix="/audit-log", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
def list_audit_log(
    actor_id: str | None = None,
    target: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.ADMIN)),
) -> list[AuditLog]:
    """"Read the audit log —
    `admin`". Closes a pre-existing gap: this endpoint had no
    authentication at all and accepted an arbitrary `actor_id` filter, so
    anyone who could reach the API could read any user's activity."""
    stmt = select(AuditLog)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if target:
        stmt = stmt.where(AuditLog.target == target)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.order_by(AuditLog.ts.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))
