"""The dev switcher bootstrap endpoint.

`GET /users` is authenticated in both modes, which creates a chicken-and-egg
in `dev` mode: the identity switcher has to list users *before* any identity
exists. This router is the answer — and the whole of its access control is
that **it is not registered at all outside dev mode**.

Two consequences worth stating, because both are easy to undo by accident:

- **There is deliberately no mode check in the handler.** Registration in
  `main.create_app` is conditional on `not settings.oidc_enabled`, so in OIDC
  mode the path is absent from the app's schema and unreachable — a
  misconfiguration, a middleware ordering bug, or a refactor that drops a
  check cannot serve a route that does not exist. This is the opposite idiom
  from `routers/auth.py`'s login endpoints, which use a runtime
  `_require_oidc_mode()` 404; the design
  says not to harmonize them in the direction of a runtime check, because
  this route must be reachable before any request identity exists, so there
  is nothing request-scoped to check.
- **It returns id and display name only** (requirement 3). The authenticated
  `/users` endpoint may expose emails and roles; an unauthenticated one must
  not, even in dev.

Prod safety is two-layered, not one: `FORGE_ENV=prod` with `AUTH_MODE=dev`
cannot start at all (`ProdDevAuthModeError`).
Conditional registration is the second layer.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.schemas.user import DevIdentityOut

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/identities", response_model=list[DevIdentityOut])
def list_dev_identities(db: Session = Depends(get_db)) -> list[User]:
    """The seeded identities the dev switcher offers, id + display name only.

    The path is named `/dev/...` so that its *absence* from a prod OpenAPI
    schema is obvious at a glance rather than something you have to know to
    look for."""
    return list(db.scalars(select(User).order_by(User.name)))
