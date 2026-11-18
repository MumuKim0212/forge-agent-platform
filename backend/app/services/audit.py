from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def write_audit_log(
    db: Session, *, actor_id: str | None, action: str, target: str, metadata: dict | None = None
) -> AuditLog:
    entry = AuditLog(actor_id=actor_id, action=action, target=target, metadata_json=metadata)
    db.add(entry)
    return entry
