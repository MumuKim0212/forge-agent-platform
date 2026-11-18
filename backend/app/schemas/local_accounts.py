"""Request/response shapes for `local` auth mode.
Only reachable when the endpoints in
`routers/auth_local.py` are registered, i.e. `AUTH_MODE=local`.

Convention: separate `*Create`/`*Input` (input) vs `*Out` (output) models.
`password`/`current_password`/`new_password` are
input-only fields and never appear on an `*Out` model — login and the
admin/self password-change endpoints reuse `schemas.user.UserOut`, which
already excludes `password_hash` by construction (it lists fields by name
rather than serializing the ORM object wholesale).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.services.passwords import PASSWORD_MAX_BYTES, fits_hash_limit
from app.schemas.types import UtcDateTime

# Deliberately not `pydantic`'s `EmailStr` — that requires the
# `email-validator` package, a new dependency for one "looks like an
# address" check ("must look like
# an address", not RFC 5322 validation). A permissive shape check is
# enough; nothing here is a security boundary — an admin still has to look
# at the address before approving.
_EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_like(value: str) -> str:
    value = value.strip()
    if not _EMAIL_LIKE.match(value):
        raise ValueError("must look like an email address")
    return value


# every free-text field on the unauthenticated signup form is bounded,
# because an unauthenticated caller chooses these strings and an admin (and, formerly,
# every user) later reads them. 254 is the longest address SMTP allows.
EMAIL_MAX_CHARS = 254
NAME_MAX_CHARS = 100
DEPARTMENT_MAX_CHARS = 100


def _password_fits_the_hash_limit(value: str) -> str:
    """at most `PASSWORD_MAX_BYTES` (72)
    bytes in UTF-8, stated in characters-and-bytes terms because the user counts
    characters and the limit counts bytes (a Korean syllable is three bytes).
    Without it a 75-byte password reaches bcrypt and comes back as a 500."""
    if not fits_hash_limit(value):
        raise ValueError(
            f"비밀번호는 UTF-8 기준 {PASSWORD_MAX_BYTES}바이트 이하여야 합니다 "
            f"(영문·숫자는 {PASSWORD_MAX_BYTES}자까지, 한글은 한 글자가 3바이트이므로 "
            f"{PASSWORD_MAX_BYTES // 3}자까지)."
        )
    return value


# A password being SET: at least 8 characters, at most `PASSWORD_MAX_BYTES` bytes.
# One alias for the three set paths (signup, own change, admin set) so the rule
# cannot be applied to two of them and forgotten on the third. NOT used for a
# password being *presented* (`LocalLoginInput`, `current_password`): those are
# checked against a hash, and a too-long one simply fails to match.
NewPassword = Annotated[str, Field(min_length=8), AfterValidator(_password_fits_the_hash_limit)]


class SignupCreate(BaseModel):
    email: str = Field(max_length=EMAIL_MAX_CHARS)
    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    department: str = Field(min_length=1, max_length=DEPARTMENT_MAX_CHARS)
    password: NewPassword

    @field_validator("email")
    @classmethod
    def _email_looks_valid(cls, value: str) -> str:
        return _validate_email_like(value)


class SignupAck(BaseModel):
    """Deliberately the only shape a signup request ever returns — the
    router returns this literal ack whether or not the email already
    existed (spec rule 2), so there is nothing here that could vary."""

    detail: str


class LocalLoginInput(BaseModel):
    email: str = Field(max_length=EMAIL_MAX_CHARS)
    password: str

    @field_validator("email")
    @classmethod
    def _email_looks_valid(cls, value: str) -> str:
        return _validate_email_like(value)


class ChangePasswordInput(BaseModel):
    current_password: str
    new_password: NewPassword


class AdminSetPasswordInput(BaseModel):
    password: NewPassword


class SignupApproveInput(BaseModel):
    role: str | None = None


class SignupRejectInput(BaseModel):
    reason: str | None = None


class PendingSignupOut(BaseModel):
    """The admin approval queue's row shape:
    "email, name, department, and requested-at". Built by hand in the
    router from named `User` fields, not `ConfigDict(from_attributes=True)`
    — `requested_at` renames `User.created_at`, and a manual assembly is
    also what keeps `password_hash` from ever having a path into a
    response, the same discipline `schemas/console.py` documents for
    `Settings`."""

    id: str
    email: str
    name: str
    department: str | None
    requested_at: UtcDateTime
    status: str
