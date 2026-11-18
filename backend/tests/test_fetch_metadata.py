"""ADR 0028 layer 3: refuses an unsafe cross-site/same-site write
per Fetch Metadata (`Sec-Fetch-Site`). Two levels:

- Pure-function tests (`should_refuse`/`is_exempt_origin`) — the exact
  rule, decoupled from any transport.
- ASGI-level tests against a minimal Starlette app (own `FetchMetadataMiddleware`
  + `CORSMiddleware`, a counting handler) — (c1) the full method x site x
  origin matrix, each row asserting BOTH the status code and (for a pass)
  that the handler actually ran, via the counter, or (for OPTIONS) that
  CORS answered the preflight itself.

(c2) the real attack shapes end-to-end, against the REAL app + REAL DB
(the `client`/`db_session`/`seeded_ids` fixtures), asserting the action did
NOT happen — not merely a 403 status — lives at the bottom of this file.
(c3) is the rest of the suite: it is already green with this middleware
wired in and adds no fetch-metadata header anywhere (verified by running
the full non-integration suite; see the task's `## Result`)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import CorsOriginsError, Settings
from app.main import create_app
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.models.user import User
from app.services.status import BuildStatus, ProjectStatus
from app.services.fetch_metadata import (
    CHECKED_METHODS,
    REFUSAL_DETAIL,
    FetchMetadataMiddleware,
    is_exempt_origin,
    should_refuse,
)

LISTED_ORIGIN = "https://spa.example.com"
ALLOWED = [LISTED_ORIGIN]


# ---------------------------------------------------------------------------
# Pure-function matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(CHECKED_METHODS))
def test_checked_methods_refuse_cross_site_with_no_origin(method):
    assert should_refuse(method, "cross-site", None, ALLOWED) is True


def test_get_is_never_checked_even_cross_site_with_no_origin():
    assert should_refuse("GET", "cross-site", None, ALLOWED) is False


def test_head_is_never_checked():
    assert should_refuse("HEAD", "cross-site", None, ALLOWED) is False


def test_options_is_never_checked_by_this_function():
    """The ASGI-level test below additionally proves OPTIONS never even
    reaches this function in the real middleware chain (CORS answers it
    first) -- this pins the function's own contract in isolation."""
    assert should_refuse("OPTIONS", "cross-site", None, ALLOWED) is False


def test_same_site_non_listed_origin_refused():
    assert should_refuse("POST", "same-site", "https://evil.example.com", ALLOWED) is True


def test_same_site_listed_origin_passes():
    assert should_refuse("POST", "same-site", LISTED_ORIGIN, ALLOWED) is False


# The two tests below hand the function a list holding `null`/`*`. Startup refuses such a
# `CORS_ORIGINS` now (see `test_a_wildcard_cors_origins_is_no_longer_a_supported_
# configuration`), so a real `Settings` can no longer produce one -- but the middleware is
# constructed from a plain list and ADR 0028 §3 states the rule independently ("never an
# exemption, whatever the configuration says"), so the rule is pinned here as the second layer.


def test_cross_site_null_origin_refused_even_when_null_is_configured():
    assert should_refuse("POST", "cross-site", "null", ["null", *ALLOWED]) is True
    assert is_exempt_origin("null", ["null"]) is False


def test_cross_site_wildcard_configured_never_exempts_an_arbitrary_origin():
    assert should_refuse("POST", "cross-site", "https://evil.example.com", ["*"]) is True
    assert is_exempt_origin("https://evil.example.com", ["*"]) is False


def test_same_origin_passes():
    assert should_refuse("POST", "same-origin", None, ALLOWED) is False


def test_no_sec_fetch_site_header_passes():
    assert should_refuse("POST", None, None, ALLOWED) is False


def test_none_sec_fetch_site_value_passes():
    assert should_refuse("POST", "none", None, ALLOWED) is False


# ---------------------------------------------------------------------------
# ASGI-level: the full matrix through a real request/response cycle, each
# row asserting an observable that distinguishes it (status + a counter).
# ---------------------------------------------------------------------------


def _build_test_app(allowed_origins):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/target", handler, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ],
        middleware=[
            # Same ordering as `main.create_app`: FetchMetadataMiddleware
            # added first -> CORSMiddleware ends up outermost.
            Middleware(FetchMetadataMiddleware, allowed_origins=allowed_origins),
            Middleware(
                CORSMiddleware,
                allow_origins=allowed_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        ],
    )
    return app, calls


ROWS = [
    # (id, method, sec_fetch_site, origin, expect_refused)
    ("post-cross-site-no-origin", "POST", "cross-site", None, True),
    ("post-same-site-non-listed", "POST", "same-site", "https://evil.example.com", True),
    ("post-same-site-listed", "POST", "same-site", LISTED_ORIGIN, False),
    ("post-cross-site-null", "POST", "cross-site", "null", True),
    ("post-same-origin", "POST", "same-origin", None, False),
    ("post-no-sec-fetch-site", "POST", None, None, False),
    ("get-cross-site", "GET", "cross-site", None, False),
    ("put-cross-site", "PUT", "cross-site", "https://evil.example.com", True),
    ("patch-cross-site", "PATCH", "cross-site", "https://evil.example.com", True),
    ("delete-cross-site", "DELETE", "cross-site", "https://evil.example.com", True),
]


@pytest.mark.parametrize("case_id, method, sec_fetch_site, origin, expect_refused", ROWS, ids=[r[0] for r in ROWS])
def test_matrix_row(case_id, method, sec_fetch_site, origin, expect_refused):
    app, calls = _build_test_app(ALLOWED)
    client = TestClient(app)

    headers = {}
    if sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    if origin is not None:
        headers["Origin"] = origin

    resp = client.request(method, "/target", headers=headers)

    if expect_refused:
        assert resp.status_code == 403, case_id
        assert resp.json()["detail"] == REFUSAL_DETAIL
        assert calls["n"] == 0, case_id
    else:
        assert resp.status_code == 200, case_id
        assert calls["n"] == 1, case_id


def test_a_wildcard_cors_origins_is_no_longer_a_supported_configuration():
    """ADR 0028 §Amendment 2026-10-17. This test used to build the app with
    `*` configured and assert the write layer still refused an arbitrary origin --
    treating `*` as a supported setting. It is not one: the platform refuses to
    START with it (Starlette would answer `*` plus credentials by echoing whatever
    origin asks, and Fetch Metadata does not cover reads). So the middleware's own
    "never exempt `*`/`null`" rule above is now the second layer, and the first is
    that no `Settings` carrying either can be built."""
    with pytest.raises(CorsOriginsError):
        Settings(_env_file=None, forge_env="dev", cors_origins="*")
    with pytest.raises(CorsOriginsError):
        Settings(_env_file=None, forge_env="dev", cors_origins=f"{LISTED_ORIGIN},null")


def _factory_app(cors_origins: str):
    """The real app from the real factory, with a `Settings` built the way startup
    builds it -- so what runs is what validation let through."""
    return TestClient(create_app(Settings(_env_file=None, forge_env="dev", cors_origins=cors_origins)))


def test_a_legitimate_origin_keeps_working_through_the_real_factory():
    """Fetch Metadata for a LEGITIMATE configured origin, end to end through
    `create_app` (the part of the old wildcard test that still matters). `POST
    /api/health` has no POST handler: a request the write layer lets through
    reaches routing and gets 405, one it refuses never gets there and gets the
    layer's own 403 -- the status tells the two apart."""
    client = _factory_app(LISTED_ORIGIN)

    passed = client.post(
        "/api/health", headers={"Sec-Fetch-Site": "same-site", "Origin": LISTED_ORIGIN}
    )
    assert passed.status_code == 405  # exempt: reached routing

    refused = client.post(
        "/api/health",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == REFUSAL_DETAIL

    sandboxed = client.post("/api/health", headers={"Sec-Fetch-Site": "cross-site", "Origin": "null"})
    assert sandboxed.status_code == 403  # `null` is never an exemption


def test_cors_headers_go_to_the_listed_origin_only():
    client = _factory_app(LISTED_ORIGIN)

    listed = client.get("/api/health", headers={"Origin": LISTED_ORIGIN})
    assert listed.status_code == 200
    assert listed.headers["access-control-allow-origin"] == LISTED_ORIGIN
    assert listed.headers["access-control-allow-credentials"] == "true"

    other = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert other.status_code == 200
    assert "access-control-allow-origin" not in other.headers  # the response stays unreadable cross-origin


def test_options_preflight_from_a_listed_origin_still_gets_cors_headers():
    """(c1) OPTIONS is never checked, and — stronger — never even reaches
    `FetchMetadataMiddleware.__call__`'s refusal branch, because
    `CORSMiddleware` answers a genuine preflight (an OPTIONS carrying
    `Access-Control-Request-Method`) before forwarding anywhere."""
    app, calls = _build_test_app(ALLOWED)
    client = TestClient(app)

    resp = client.options(
        "/target",
        headers={
            "Origin": LISTED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == LISTED_ORIGIN
    assert "access-control-allow-methods" in resp.headers
    assert calls["n"] == 0  # a preflight never reaches the route handler either


# ---------------------------------------------------------------------------
# (c4) reads the SAME parsed CORS list CORSMiddleware receives
# ---------------------------------------------------------------------------


def test_fetch_metadata_and_cors_share_the_same_origin_list():
    """Builds two apps with two different `CORS_ORIGINS` values via the
    `create_app` factory and asserts BOTH middlewares' configured origin
    lists move together each time -- `main.create_app` computes the parsed
    list ONCE and hands the identical object to both `add_middleware`
    calls, so they cannot drift by re-parsing the setting twice."""
    for cors_origins, expected in (
        ("http://localhost:5173", ["http://localhost:5173"]),
        ("https://a.example.com,https://b.example.com", ["https://a.example.com", "https://b.example.com"]),
    ):
        app = create_app(Settings(cors_origins=cors_origins))
        fetch_metadata_mw = next(
            m for m in app.user_middleware if m.cls is FetchMetadataMiddleware
        )
        cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        assert list(fetch_metadata_mw.kwargs["allowed_origins"]) == expected
        assert list(cors_mw.kwargs["allow_origins"]) == expected


# ---------------------------------------------------------------------------
# (c2) the real attack shapes, refused end to end through the real app,
# with the action proven NOT to have happened.
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="fetch-metadata-attack-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def test_real_attack_stop_deployment_refused_and_unchanged(client, db_session, project_row):
    build = Build(project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    active = Deployment(
        project_id=project_row.id,
        build_id=build.id,
        confirmed_by_id=project_row.owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(active)
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project_row.id}/deployments/stop",
        headers={"X-Forge-User-Id": project_row.owner_id, "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403

    db_session.refresh(active)
    assert active.status == DeploymentStatus.ACTIVE  # unchanged, not merely a 403


def test_real_attack_rollback_deployment_refused_and_unchanged(client, db_session, project_row):
    build = Build(project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    superseded = Deployment(
        project_id=project_row.id,
        build_id=build.id,
        confirmed_by_id=project_row.owner_id,
        status=DeploymentStatus.SUPERSEDED,
    )
    db_session.add(superseded)
    db_session.commit()

    resp = client.post(
        f"/api/projects/{project_row.id}/deployments/rollback",
        headers={"X-Forge-User-Id": project_row.owner_id, "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403

    count_after = (
        db_session.query(Deployment).filter(Deployment.project_id == project_row.id).count()
    )
    assert count_after == 1  # no new (rollback) row created


def test_real_attack_approve_approval_refused_and_unchanged(client, db_session, project_row):
    approver = db_session.query(User).filter_by(role="approver").first()
    build = Build(project_id=project_row.id, type="create", diff_ref="forge-build-x#1")
    db_session.add(build)
    db_session.flush()
    request = ApprovalRequest(
        build_id=build.id,
        project_id=project_row.id,
        requester_id=project_row.owner_id,
        risk_tier="medium",
        status=ApprovalStatus.PENDING,
        required_approvals=1,
    )
    db_session.add(request)
    db_session.commit()

    resp = client.post(
        f"/api/approvals/{request.id}/approve",
        headers={"X-Forge-User-Id": approver.id, "Sec-Fetch-Site": "cross-site"},
        json={},
    )
    assert resp.status_code == 403

    db_session.refresh(request)
    assert request.status == ApprovalStatus.PENDING  # unchanged, not merely a 403
    assert len(request.decisions) == 0
