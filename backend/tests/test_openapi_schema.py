"""The four catch-all proxy routes (`routers/projects.py::
proxy_preview`, `routers/deployments.py::proxy_service`, each stacking two
`@router.api_route(...)` decorators) generated duplicate OpenAPI operation
ids — FastAPI assigns one id per *route*, not per method, and reuses it for
every method on the route (measured 2026-09-27: 16 warnings across 4 ids,
nondeterministic because the id suffix comes from `list(route.methods)[0]`
of a `set`, and string hashing is randomised per process).

The fix is `include_in_schema=False` on all four decorators — the routes'
"operations" document a generated service's own behaviour, not the
platform's, so excluding them removes the warnings and the nondeterministic
ids at the source. Splitting into one route per method was measured and
rejected (breaks `OPTIONS`/`HEAD`'s `405 Allow` down to a single method,
an RFC 9110 violation) — see this task's Result for the measurement. This
file exists to pin both halves: no more duplicate ids anywhere in the
schema (b1/b4), and the routes' own HTTP behaviour is unchanged (b3).
"""

from __future__ import annotations

import warnings

import pytest

from app.main import app

# The four route paths as FastAPI/Starlette render them in a compiled route
# object's `.path` — curly-brace path-parameter syntax, matching the
# decorators in routers/projects.py and routers/deployments.py exactly.
PROXY_ROUTE_PATHS = [
    "/api/projects/{project_id}/preview/{build_id}",
    "/api/projects/{project_id}/preview/{build_id}/{path}",
    "/api/projects/{project_id}/service",
    "/api/projects/{project_id}/service/{path}",
]

PROXY_METHOD_SET = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Concrete (non-existent-id) URLs for driving the routes through the real
# ASGI stack — routing decides 405/Allow before any dependency or handler
# runs, so these ids never need to resolve to a real project/build.
PROXY_REQUEST_URLS = [
    "/api/projects/does-not-exist/preview/does-not-exist",
    "/api/projects/does-not-exist/preview/does-not-exist/some/path",
    "/api/projects/does-not-exist/service",
    "/api/projects/does-not-exist/service/some/path",
]


def test_openapi_schema_has_no_duplicate_operation_ids_and_hides_the_proxy_routes():
    """(b1) Regenerated fresh (`openapi_schema` reset first) so this does not
    silently pass off a schema some earlier test already cached. Warnings
    raised as errors: if a duplicate id ever reappears (a new stacked route
    added elsewhere, for instance), this test fails loudly rather than
    quietly accepting a captured warning list."""
    app.openapi_schema = None
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        schema = app.openapi()

    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert operation_ids  # non-vacuous: the schema really has operations
    assert len(operation_ids) == len(set(operation_ids))

    for proxy_path in PROXY_ROUTE_PATHS:
        assert proxy_path not in schema["paths"]


def test_operation_ids_are_stable_across_hash_seeds():
    """(b4)'s in-process half: the nondeterministic suffix (established
    fact B) came from `list(route.methods)[0]` of a `set`, which is no
    longer reachable once each route has exactly one id shared by every
    method rather than a per-method id. Two fresh schema builds in the same
    process must already agree; the task's Result additionally reports two
    full runs under different `PYTHONHASHSEED` values from the shell."""
    app.openapi_schema = None
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        first = sorted(
            operation["operationId"]
            for path_item in app.openapi()["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        )

    app.openapi_schema = None
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        second = sorted(
            operation["operationId"]
            for path_item in app.openapi()["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        )

    assert first == second


@pytest.mark.parametrize("url", PROXY_REQUEST_URLS)
def test_proxy_routes_still_accept_every_method_in_the_set(client, url):
    """(b3) first half, driven live through the real ASGI stack rather than
    by introspecting `app.routes` — this FastAPI version nests an included
    router behind a private wrapper object, so a flat scan silently finds
    nothing (`test_console.py`'s own comment names this exact trap for the
    `/api/console` paths; excluding the proxy routes from the schema now
    makes the schema itself unusable for this check too). Each of the five
    methods the route was given must still reach the handler rather than
    being rejected at the routing layer: every one 404s here (unknown
    project/build), a value **only** the handler can produce, since a
    routing-layer rejection is 405, never 404 — so 404 on all five is
    non-vacuous proof the request was actually routed to `proxy_preview`/
    `proxy_service`, not a coincidence of a generic error path."""
    for method in sorted(PROXY_METHOD_SET):
        response = client.request(method, url)
        assert response.status_code != 405, f"{method} {url} was rejected at routing"
        assert response.status_code == 404


@pytest.mark.parametrize("url", PROXY_REQUEST_URLS)
def test_options_to_proxy_routes_returns_405_with_the_full_allow_set(client, url):
    """(b3) second half, pinned as a live HTTP assertion rather than only a
    route-object inspection: a method the route was never given (`OPTIONS`)
    still 405s with every one of the five real methods in `Allow`, compared
    as a set since dict/set ordering across methods is not part of the
    contract (established fact B: the ORDER was observed to vary by
    process). This is exactly the shape the rejected per-method-route split
    would have broken (measured: `Allow` down to a single method)."""
    response = client.options(url)
    assert response.status_code == 405
    allow = {method.strip() for method in response.headers["allow"].split(",")}
    assert allow == PROXY_METHOD_SET
