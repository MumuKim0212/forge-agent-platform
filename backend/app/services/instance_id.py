"""Where the installation's id comes from (ADR 0037 §Decision 2): one row in the
platform database (`models/platform_instance.py`), generated the first time it is asked for
and read ever after.

`bind_instance_id` is what the worker calls at start: it reads (or creates) the row and
hands the value to `platform_labels.set_instance_id`, so every create site labels with it.
The API process never creates a container or a network, so it never binds one -- and a
future create site in it would raise rather than create an object nobody can find.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.platform_instance import SINGLETON_ID, PlatformInstance
from app.services import platform_labels


def get_or_create_instance_id(db: Session) -> str:
    """The stored id; the row is created on first use. Two processes starting together can
    both find no row: the loser's insert hits the singleton primary key, it rolls back and
    reads the winner's, so both end with the same value."""
    row = db.get(PlatformInstance, SINGLETON_ID)
    if row is None:
        db.add(PlatformInstance(id=SINGLETON_ID, instance_id=uuid.uuid4().hex))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        row = db.get(PlatformInstance, SINGLETON_ID)
        if row is None:  # the insert failed for a reason that is not the race
            raise RuntimeError("the platform instance id could not be created")
    return row.instance_id


def bind_instance_id(db: Session) -> str:
    """Reads (or creates) the stored id and binds it for this process."""
    value = get_or_create_instance_id(db)
    platform_labels.set_instance_id(value)
    return value
