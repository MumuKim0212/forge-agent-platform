"""ADR 0028 layer 2: conditional mounting of the two
platform-origin proxy routers. `main.create_app` registers
`preview_proxy.router`/`service_proxy.router` only when
`settings.traefik_enabled` is false — see that function's own comment.

No Docker: builds throwaway apps via the `create_app` factory seam
with an in-memory-SQLite `get_db` override, the same "second app, second
Settings" idiom
`test_rbac.py::test_in_oidc_mode_the_bootstrap_route_does_not_exist_at_all`
uses for the dev-switcher's own conditional registration — except here the
proxy routes carry `include_in_schema=False`, so an OpenAPI-schema
absence check would be vacuous; the test instead drives real requests and
tells "never registered" apart from "registered, 404 for its own reasons"
by the presence of the ADR 0028 isolation header only a MOUNTED proxy
handler ever attaches (a plain Starlette 404 carries none)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.services.project_activity import service_traffic_is_observed
from app.services.project_lifecycle import live_services_exempt_from_idle_sweep

# Two preview paths (with and without a trailing sub-path) + two service
# paths, mirroring the four `api_route` registrations across
# `preview_proxy.py`/`service_proxy.py`.
PROXY_PATHS = [
    "/api/projects/does-not-exist/preview/does-not-exist",
    "/api/projects/does-not-exist/preview/does-not-exist/index.html",
    "/api/projects/does-not-exist/service",
    "/api/projects/does-not-exist/service/healthz",
]


def _client_for(settings_obj: Settings, db_session) -> TestClient:
    app = create_app(settings_obj)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def test_traefik_on_serves_none_of_the_four_proxy_paths(db_session):
    """(b1) All four proxy paths 404 when Traefik is handling routing —
    and none of the four carries the ADR 0028 isolation header, which only
    a MOUNTED proxy handler's own 404 ever attaches (decision 1: "the
    headers go on every response these two routes return, including their
    own 403/404s"). A Starlette route-not-found 404 carries neither."""
    client = _client_for(Settings(traefik_enabled=True), db_session)

    for path in PROXY_PATHS:
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert "content-security-policy" not in resp.headers, path


def test_traefik_on_still_serves_core_routes_and_forward_auth(db_session):
    """(b1) Every other route on `projects`/`deployments`, plus
    forward-auth, is unaffected by the conditional mounting above."""
    client = _client_for(Settings(traefik_enabled=True), db_session)

    # A representative projects route and a representative deployments
    # route: 401 (no identity) -- never 404 -- proves the surrounding
    # routers are still fully mounted, only the two proxy paths are gated.
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects/does-not-exist/deployments").status_code == 401

    # Forward-auth (routers/preview_auth.py) is a separate router,
    # untouched by ADR 0028 layer 2 -- identity-free, 403 on an unknown
    # build, never 404.
    resp = client.get("/api/internal/preview-auth/does-not-exist")
    assert resp.status_code == 403


def test_traefik_off_still_serves_the_proxies_with_isolation_headers(db_session):
    """(b2) The control, and today's actual posture (dev, or prod with
    Traefik off): the same four paths ARE handled by the platform's own
    proxy routes -- a 404 that DOES carry the ADR 0028 header, proving a
    real handler ran rather than Starlette's own route-not-found catch-all.
    The existing proxy test suites (`test_proxy_isolation.py`,
    `test_deployments_api.py`, the Docker integration modules) already
    cover behaviour in this mode in full; this test only pins that mounting
    itself is unaffected."""
    client = _client_for(Settings(traefik_enabled=False), db_session)

    for path in PROXY_PATHS:
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert "content-security-policy" in resp.headers, path


@pytest.mark.parametrize("traefik_enabled", [True, False])
def test_the_platform_observes_service_traffic_exactly_when_the_service_proxy_is_mounted(
    db_session, traefik_enabled
):
    """`last_active_at` carries traffic only because the
    platform-origin service proxy records it, so "is traffic observed?" is "is that proxy mounted?".
    The idle sweep exempts a live service exactly when it is not. The answer is read off a real app
    built from the same `Settings`, so a change to the mount decision that is not made in the
    predicate fails here, instead of the sweep quietly judging services on a signal that is not
    there (or exempting them with one that is)."""
    settings_obj = Settings(traefik_enabled=traefik_enabled)
    client = _client_for(settings_obj, db_session)

    resp = client.get("/api/projects/does-not-exist/service")
    proxy_is_mounted = "content-security-policy" in resp.headers

    assert proxy_is_mounted is (not traefik_enabled)  # literal, so this half cannot move with the code
    assert service_traffic_is_observed(settings_obj) is proxy_is_mounted
    assert live_services_exempt_from_idle_sweep(settings_obj) is (not proxy_is_mounted)
