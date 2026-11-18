"""the frontend's `local`-vs-`oidc` mode probe reads `GET
/openapi.json` directly and checks whether `/api/auth/signup` is present in
its `paths` (`api/auth.ts`'s `localAccountsEnabled`, replacing the
`GET /auth/signup` 405-vs-404 status-code inference).
`## Decision`).

This file pins the exact contract at the HTTP layer the frontend actually
talks to: a live `TestClient` GET to `/openapi.json`, not `app.openapi()`
called directly in Python.
`test_local_accounts.py::test_local_routes_exist_only_in_local_mode`
already proves the underlying route-registration fact (presence in `local`
mode, absence in `dev`/`oidc`, with a positive control) via the Python
method; this reproduces that same fact via HTTP, plus the two properties
that method never touches: that `/openapi.json` itself is reachable and
`openapi_url` stays enabled.

No frontend code runs here — that side is pinned by `frontend/src/api/
auth.test.ts` and `frontend/src/context/IdentityContext.test.ts`.

No Docker, no network, no real IdP — in-memory only, same posture as
`test_local_accounts.py` and `test_rbac.py`'s factory tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BOOTSTRAP_EMAIL = "mode-detection@example.com"
BOOTSTRAP_PASSWORD = "correct horse battery staple"  # 27 chars, not "admin"


# ---------------------------------------------------------------------------
# B-1: /api/auth/signup in local mode's schema, absent from dev/oidc's
# ---------------------------------------------------------------------------


def test_auth_signup_is_in_the_openapi_schema_only_in_local_mode():
    """Built through the `create_app` factory seam for each mode —
    a monkeypatch of the process-wide `settings` singleton cannot
    retro-register a conditionally-included router. Includes a positive
    control (a flat `app.routes` scan silently finds nothing
    under an included router and would pass forever, so the absence
    assertions below are checked against the same generated schema the
    presence assertion is, with a route known to exist in dev mode proving
    the schema itself is populated)."""
    local_settings = Settings(
        _env_file=None,
        forge_env="dev",
        auth_mode="local",
        bootstrap_admin_emails=BOOTSTRAP_EMAIL,
        bootstrap_admin_password=BOOTSTRAP_PASSWORD,
        session_signing_secret="test-session-secret",
    )
    local_paths = TestClient(create_app(local_settings)).get("/openapi.json").json()["paths"]
    assert "/api/auth/signup" in local_paths

    dev_paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    assert "/api/auth/signup" not in dev_paths
    # The positive control: dev mode really does register something else
    # conditionally, so the absence above is about the mode, not a typo'd
    # path string or an empty schema.
    assert "/api/dev/identities" in dev_paths

    oidc_settings = Settings(
        _env_file=None,
        forge_env="dev",
        auth_mode="oidc",
        oidc_issuer="https://idp.example.test",
        oidc_client_id="forge",
        oidc_client_secret="shh",
        session_signing_secret="test-session-secret",
    )
    oidc_paths = TestClient(create_app(oidc_settings)).get("/openapi.json").json()["paths"]
    assert "/api/auth/signup" not in oidc_paths


# ---------------------------------------------------------------------------
# B-2: /openapi.json itself is reachable and shaped the way the frontend
# consumer expects
# ---------------------------------------------------------------------------


def test_openapi_schema_endpoint_is_reachable_unauthenticated_and_returns_a_paths_object(
    client,
):
    """The contract the frontend now depends on — previously
    nothing asserted the schema endpoint exists at all. Reachable
    with **no** identity: the mode probe fires before any identity is known,
    so an auth requirement here would make mode detection unable to bootstrap
    in the first place."""
    response = client.get("/openapi.json")
    assert response.status_code == 200

    body = response.json()
    assert isinstance(body.get("paths"), dict)
    assert "/api/health" in body["paths"]  # sanity: a real, always-registered path


# ---------------------------------------------------------------------------
# B-3: what happens if openapi_url is ever disabled (decided in the Result)
# ---------------------------------------------------------------------------


def test_openapi_schema_endpoint_stays_enabled_for_local_mode_detection():
    """Reading the OpenAPI schema has a known cost:
    `FastAPI(openapi_url=None)` would remove `/openapi.json` entirely, and
    every consumer of this task's contract (`authApi.localAccountsEnabled`)
    would then resolve `oidc` — the safe direction, but a `local`-mode
    deployment that nobody could log into, discovered only by a bug report.

    `create_app` does not set `openapi_url` today, so FastAPI's own default
    ("/openapi.json") applies — nothing stops a future edit from passing
    `openapi_url=None` to the `FastAPI(...)` constructor there. Pinned
    mechanically (matching this codebase's existing preference for an
    assertion over a comment-only invariant, e.g. the role-literal `ast`
    scan in `test_rbac.py`) so that edit fails a test the moment it lands."""
    assert create_app().openapi_url == "/openapi.json"
