"""The admin console's "send test message".

**Not under `/api/console`**: the console router stays read-only by construction
(`tests/test_console.py` pins its paths and methods). Like the agent engine selection and the quota
and role changes, the console's write lives on its own admin-only, audited endpoint
(this is a fourth narrow action, and it
changes no configuration and no entity -- it asks the platform to say one sentence to the channels
the operator already configured). The channel STATE (configured, last attempt, last result, last
error class) is read from `GET /console/health`, not here.

* admin only, at `require_role(Role.ADMIN)` -- before the body is read;
* written like every other unsafe request: `FetchMetadataMiddleware` (ADR 0028 layer 3) refuses a
  cross-site `POST` before routing, and the session cookie is `SameSite`;
* rate-limited by ONE conditional UPDATE on a gate row (`notification_states`, family `test_gate`),
  so two simultaneous clicks, in one process or several, send once and the other gets 429;
* audit-logged (`notification.test_sent`): the actor and the per-channel result, never a URL.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_role
from app.models.notification import NotificationState
from app.models.user import User
from app.services import notifications
from app.services.audit import write_audit_log
from app.services.roles import Role

router = APIRouter(prefix="/platform/notifications", tags=["platform"])

GATE_FAMILY = "test_gate"
GATE_SUBJECT = "test"
AUDIT_TEST_SENT = "notification.test_sent"


class ChannelResultOut(BaseModel):
    channel: str
    ok: bool
    error_class: str | None


class TestMessageOut(BaseModel):
    __test__ = False  # not a pytest class
    results: list[ChannelResultOut]


def _take_gate(db: Session, now: datetime) -> bool:
    """True when this request may send: no test was sent within the minimum interval. Atomic."""
    interval = timedelta(seconds=settings.notify_test_min_interval_seconds)
    won = db.execute(
        update(NotificationState)
        .where(
            NotificationState.family == GATE_FAMILY,
            NotificationState.subject == GATE_SUBJECT,
            NotificationState.changed_at <= now - interval,
        )
        .values(changed_at=now)
    ).rowcount
    if won == 1:
        db.commit()
        return True
    db.rollback()
    if db.get(NotificationState, (GATE_FAMILY, GATE_SUBJECT)) is not None:
        return False  # the row exists and is too recent
    db.add(NotificationState(family=GATE_FAMILY, subject=GATE_SUBJECT, state="UP", changed_at=now, down_notified=False))
    try:
        db.commit()
    except IntegrityError:  # a simultaneous request created it first
        db.rollback()
        return False
    return True


@router.post("/test", response_model=TestMessageOut)
def send_test_message(
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.ADMIN)),
) -> TestMessageOut:
    if not notifications.any_channel_configured():
        raise HTTPException(status_code=409, detail="No notification channel is configured.")
    if not _take_gate(db, datetime.now(timezone.utc)):
        raise HTTPException(
            status_code=429,
            detail="A test message was sent a moment ago; wait before sending another.",
            headers={"Retry-After": str(int(settings.notify_test_min_interval_seconds) or 1)},
        )
    results = notifications.send_test_message(notifications.session_factory_for(db))
    write_audit_log(
        db,
        actor_id=admin.id,
        action=AUDIT_TEST_SENT,
        target="platform:notifications",
        metadata={"results": {r.channel: ("OK" if r.ok else r.error_class) for r in results}},
    )
    db.commit()
    return TestMessageOut(results=[ChannelResultOut(channel=r.channel, ok=r.ok, error_class=r.error_class) for r in results])
