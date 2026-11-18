"""Shared annotated types for response schemas.

``UtcDateTime``: every timestamp the API serialises carries a UTC offset.
SQLite drops tzinfo on reload, so a stored UTC value comes back naive; this
type treats a naive value as UTC and converts an aware value to UTC, both on
validation and on serialisation (so a model built without validation is
covered too). Every ``datetime`` field under ``app/schemas/`` must use it;
``tests/test_api_timestamps_carry_utc_offset.py`` fails otherwise.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[
    datetime,
    AfterValidator(to_utc),
    PlainSerializer(to_utc, return_type=datetime),
]
