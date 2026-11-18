
from pydantic import BaseModel, ConfigDict

from app.schemas.types import UtcDateTime


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class DevIdentityOut(BaseModel):
    """The dev switcher bootstrap's response shape: **id
    and display name only.** `UserOut` carries email and role, and this is
    the one endpoint that serves unauthenticated requests, so it must not
    reuse it — a narrower shape costs nothing and there is no reason to ship
    a credential-free directory dump even in dev."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class UserRoleUpdate(BaseModel):
    """Input for `PATCH /users/{id}/role`.
    The value is validated against `services/roles.ALL_ROLES`
    in the service, not by a Pydantic enum, so the role set has exactly one
    declaration site."""

    role: str


class UsableAdminCountOut(BaseModel):
    """Response of `GET /users/usable-admin-count`: how many admins the
    last-admin guard counts -- the length of `services/roles.usable_admin_ids`, so
    the number an operator reads is the number the 409 is decided on. A count and
    nothing else: no ids, and no status or credential flag per user (`UserOut`
    deliberately carries none)."""

    count: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    role: str
    team_id: str | None
    created_at: UtcDateTime
