"""a sandboxed generated page can call its own preview.

Everything here runs through the REAL app factory (`create_app`, so the real middleware order) with a real
route and a fake `PreviewManager` that records what the route would have sent to the container. No Docker.
The pieces under test:

- layer 3 lets an unsafe request with `Origin: null` through ONLY on the anchored preview shape, and the
  route's own token check still runs after it;
- the preview CORS layer answers a preflight and adds `Access-Control-Allow-Origin: null` to every answer
  on that shape, outside `CORSMiddleware`, never with credentials;
- the entry URL's 307 to the `~{token}` path form, and the `~{token}` segment itself;
- the preview route reads no cookie.

Contract strings (header names, status codes, the `~` segment, the Location shape) are literals on purpose.
`httpx` normalises literal `.` / `..` segments away, so dot segments are sent through a hand-built ASGI
scope (`_asgi`), and `%2f` / `%2e` are only visible in `raw_path`, which that helper fills in as a server does.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import preview_manager as preview_manager_module
from app.services.preview_manager import hash_token, issue_preview_token
from app.services.status import BuildStatus, ProjectStatus

LISTED_ORIGIN = "https://spa.example.com"
REFUSAL = "cross-site or same-site write refused"  # layer 3's own body, not the route's
UNSAFE = ["POST", "PUT", "PATCH", "DELETE"]


# ---------------------------------------------------------------------------
# rig
# ---------------------------------------------------------------------------


class FakeManager:
    """Records what the route would have sent to the container."""

    def __init__(self, result=(200, b'{"ok": true}', "application/json"), error=None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def proxy(self, build_id, **kwargs):
        self.calls.append({"build_id": build_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="caseaq-sandbox",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _store(db_session, build, token):
    build.preview_token_hash = hash_token(token)
    db_session.commit()


@pytest.fixture()
def preview_build(db_session, project_row):
    build = Build(id="caseaq-pv-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.commit()
    token, _expires = issue_preview_token(build.id)
    _store(db_session, build, token)
    return build, token


@pytest.fixture()
def manager(monkeypatch):
    import app.routers.preview_proxy as preview_router

    fake = FakeManager()
    monkeypatch.setattr(preview_router, "_preview_manager", lambda: fake)
    return fake


def _make_app(db_session, *, traefik_enabled=False):
    app = create_app(
        Settings(_env_file=None, forge_env="dev", cors_origins=LISTED_ORIGIN, traefik_enabled=traefik_enabled)
    )

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.fixture()
def app(db_session):
    return _make_app(db_session)


@pytest.fixture()
def http(app):
    # The 307 is the thing under test: never follow it.
    return TestClient(app, follow_redirects=False)


def _asgi(app, method, raw_path, *, headers=None, root_path="", query=b"", body=b""):
    """Drive the ASGI app with a hand-built scope: the one way to send a literal `..` segment."""
    sent: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "query_string": query,
        "root_path": root_path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    out_headers: dict[str, list[str]] = {}
    for key, value in start["headers"]:
        out_headers.setdefault(key.decode().lower(), []).append(value.decode())
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], out_headers, content


def _base(project, build) -> str:
    return f"/api/projects/{project.id}/preview/{build.id}"


def _sandboxed(**extra) -> dict[str, str]:
    """What a sandboxed document's own request carries."""
    return {"Origin": "null", "Sec-Fetch-Site": "cross-site", **extra}


def _json_write(method, url, http, **extra_headers):
    return http.request(
        method,
        url,
        content=b'{"name": "n"}',
        headers=_sandboxed(**{"Content-Type": "application/json"}, **extra_headers),
    )


def _is_layer_three_refusal(response) -> bool:
    """Layer 3's own 403: its body, and none of the route's isolation headers."""
    return (
        response.status_code == 403
        and response.json() == {"detail": REFUSAL}
        and "content-security-policy" not in response.headers
    )


def _is_route_refusal(response) -> bool:
    """The route's own 403: the isolation headers are only ever attached by the route."""
    return response.status_code == 403 and "content-security-policy" in response.headers


# ---------------------------------------------------------------------------
# layer 3: the exemption, both directions, through the real factory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", UNSAFE)
def test_null_origin_unsafe_write_on_the_shape_reaches_the_route_and_the_container(
    http, manager, project_row, preview_build, method
):
    build, token = preview_build
    response = _json_write(method, f"{_base(project_row, build)}/~{token}/items?x=1", http)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert [(c["method"], c["path"], c["query"]) for c in manager.calls] == [(method, "/items", "x=1")]
    assert manager.calls[0]["body"] == b'{"name": "n"}'


@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize("origin", ["http://evil.test", "http://127.0.0.1:5391", "https://evil.example.com"])
def test_any_other_origin_on_the_shape_is_still_refused_by_layer_three(
    http, manager, project_row, preview_build, method, origin
):
    build, token = preview_build
    response = http.request(
        method,
        f"{_base(project_row, build)}/~{token}/items",
        content=b"{}",
        headers={"Origin": origin, "Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
    )

    assert _is_layer_three_refusal(response)
    assert manager.calls == []


@pytest.mark.parametrize("method", UNSAFE)
def test_a_listed_origin_keeps_the_exemption_it_always_had_and_still_needs_the_token(
    http, manager, project_row, preview_build, method
):
    # Unchanged: an origin on CORS_ORIGINS is exempt from layer 3 everywhere. It reaches the
    # route, where the token check decides.
    build, token = preview_build
    headers = {"Origin": LISTED_ORIGIN, "Sec-Fetch-Site": "same-site", "Content-Type": "application/json"}

    ok = http.request(method, f"{_base(project_row, build)}/~{token}/items", content=b"{}", headers=headers)
    assert ok.status_code == 200 and len(manager.calls) == 1
    assert ok.headers["access-control-allow-origin"] == LISTED_ORIGIN  # the global layer, not the sandbox-access one

    no_token = http.request(method, f"{_base(project_row, build)}/items", content=b"{}", headers=headers)
    assert _is_route_refusal(no_token) and len(manager.calls) == 1


@pytest.mark.parametrize("method", UNSAFE)
def test_null_origin_off_the_preview_shape_is_still_refused(http, manager, project_row, preview_build, method):
    build, token = preview_build
    for url in (
        "/api/health",
        f"/api/projects/{project_row.id}/service/items",
        f"/api/projects/{project_row.id}/deployments/stop",
        f"/api/projects/{project_row.id}/preview-x/{build.id}/~{token}/items",
        f"/api/projects/{project_row.id}/previews/{build.id}/~{token}/items",
        f"/api/projects/{project_row.id}/preview",
        f"/api/projects/{project_row.id}/preview/",
    ):
        response = _json_write(method, url, http)
        assert _is_layer_three_refusal(response), url
    assert manager.calls == []


@pytest.mark.parametrize(
    "raw_path_template",
    [
        "{base}/../x",
        "{base}/./x",
        "{base}/~{token}/../x",
        "{base}//x",
        "{base}/a//b",
        "{base}/a%2fb",
        "{base}/a%2Fb",
        "{base}/%2e%2e/x",
        "{base}/%2E/x",
        "{base}/a%5cb",
    ],
    ids=["dotdot", "dot", "dotdot-after-token", "empty-after-build", "empty-in-the-middle", "slash-lower",
         "slash-upper", "encoded-dotdot", "encoded-dot-upper", "backslash"],
)
@pytest.mark.parametrize("method", UNSAFE)
def test_an_ambiguous_path_on_the_shape_is_never_exempt_even_with_a_valid_token(
    app, manager, project_row, preview_build, method, raw_path_template
):
    build, token = preview_build
    raw_path = raw_path_template.format(base=_base(project_row, build), token=token)

    status, headers, content = _asgi(
        app, method, raw_path, headers=_sandboxed(**{"Content-Type": "application/json"}), body=b"{}"
    )

    assert status == 403
    assert json.loads(content) == {"detail": REFUSAL}
    assert "content-security-policy" not in headers  # layer 3, not the route
    assert "access-control-allow-origin" not in headers  # and the CORS layer did not claim it either
    assert manager.calls == []


def test_the_clean_hand_built_scope_does_reach_the_route(app, manager, project_row, preview_build):
    # The control for the table above: same helper, same headers, a clean path -> exempt and proxied.
    build, token = preview_build
    status, _headers, _content = _asgi(
        app, "POST", f"{_base(project_row, build)}/~{token}/a/b", headers=_sandboxed(**{"Content-Type": "application/json"}), body=b"{}"
    )
    assert status == 200
    assert [c["path"] for c in manager.calls] == ["/a/b"]


# -- the token check still runs behind the exemption -------------------------------------------------


def _present(db_session, build, project, scenario):
    """The token a request presents under `scenario` (or None for no token at all), with the database
    arranged so the route has a live preview to compare against."""
    current, _ = issue_preview_token(build.id)
    _store(db_session, build, current)
    if scenario == "none":
        return None
    if scenario == "wrong":
        return current[:-1] + ("0" if current[-1] != "0" else "1")
    if scenario == "expired":
        expired, _ = issue_preview_token(build.id, ttl_minutes=-5)
        _store(db_session, build, expired)  # the stored hash matches: only the expiry is wrong
        return expired
    if scenario == "other-build":
        other, _ = issue_preview_token("some-other-build")
        return other
    if scenario == "superseded":
        _store(db_session, build, issue_preview_token(build.id)[0])  # a newer preview replaced it
        return current
    raise AssertionError(scenario)


SCENARIOS = ["none", "wrong", "expired", "other-build", "superseded"]


@pytest.mark.parametrize("carrier", ["query", "path"])
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("method", UNSAFE)
def test_null_origin_with_a_bad_token_is_403_for_every_unsafe_method_and_never_reaches_the_container(
    http, manager, db_session, project_row, preview_build, method, scenario, carrier
):
    build, _ = preview_build
    token = _present(db_session, build, project_row, scenario)
    base = _base(project_row, build)
    if token is None:
        url = f"{base}/items"
    elif carrier == "query":
        url = f"{base}/items?token={token}"
    else:
        url = f"{base}/~{token}/items"

    response = _json_write(method, url, http)

    assert _is_route_refusal(response)  # the route's 403: layer 3 let it in and the token check refused it
    assert "location" not in response.headers
    assert manager.calls == []


@pytest.mark.parametrize("method", UNSAFE)
def test_the_skipped_token_check_would_be_caught_by_the_good_control(http, manager, project_row, preview_build, method):
    # The control for the matrix above: the very same request with the CURRENT token passes, so the 403s
    # there are the token check's doing and not an artefact of the request shape.
    build, token = preview_build
    assert _json_write(method, f"{_base(project_row, build)}/~{token}/items", http).status_code == 200
    assert _json_write(method, f"{_base(project_row, build)}/items?token={token}", http).status_code == 200
    assert len(manager.calls) == 2


def test_traefik_mode_changes_nothing(db_session, project_row, preview_build):
    # The routes are not mounted there: layer 3 still refuses `null` on the shape (as it always did) and
    # the preview CORS layer is not installed, so the global layer answers a preflight with its own 400.
    build, token = preview_build
    http = TestClient(_make_app(db_session, traefik_enabled=True), follow_redirects=False)

    write = _json_write("POST", f"{_base(project_row, build)}/~{token}/items", http)
    assert _is_layer_three_refusal(write)

    preflight = http.options(
        f"{_base(project_row, build)}/items",
        headers={"Origin": "null", "Access-Control-Request-Method": "PUT"},
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers

    read = http.get(f"{_base(project_row, build)}/~{token}/", headers={"Origin": "null"})
    assert read.status_code == 404 and "access-control-allow-origin" not in read.headers


# ---------------------------------------------------------------------------
# the entry 307 and the `~{token}` segment
# ---------------------------------------------------------------------------


def test_the_entry_url_answers_307_to_the_path_only_token_form(http, manager, project_row, preview_build):
    build, token = preview_build
    for entry in (f"{_base(project_row, build)}?token={token}", f"{_base(project_row, build)}/?token={token}"):
        response = http.get(entry)
        assert response.status_code == 307, entry
        assert response.headers["location"] == f"/api/projects/{project_row.id}/preview/{build.id}/~{token}/"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"].startswith("sandbox ")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.content == b""
    assert manager.calls == []  # the redirect never reached the container


@pytest.mark.parametrize(
    ("query_template", "expected_query"),
    [
        ("token={t}", ""),
        ("token={t}&a=1&b=two%20words", "?a=1&b=two%20words"),
        ("a=1&token={t}&b=2", "?a=1&b=2"),
        ("a=1&b=2&token={t}", "?a=1&b=2"),
        ("token={t}&a=1&token={t}&b=2", "?a=1&b=2"),  # repeated
        ("tok%65n={t}&a=1", "?a=1"),  # an encoded spelling of the name
        ("a=1&TOKEN=keep&token={t}", "?a=1&TOKEN=keep"),  # another name is somebody else's
        ("a=b+c&token={t}&d=%27x%27", "?a=b+c&d=%27x%27"),
    ],
)
def test_the_307_keeps_the_other_query_pairs_and_drops_the_token(
    http, project_row, preview_build, query_template, expected_query
):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}?{query_template.format(t=token)}")

    assert response.status_code == 307
    location = response.headers["location"]
    assert location == f"/api/projects/{project_row.id}/preview/{build.id}/~{token}/{expected_query}"
    assert location.count(token) == 1  # the credential is in the path, never also in the query


def test_a_bad_token_on_the_entry_url_is_403_with_no_location(http, manager, project_row, preview_build):
    build, token = preview_build
    for query in ("token=garbage", "", f"token={token[:-1]}{'0' if token[-1] != '0' else '1'}", "token=%0d%0aLocation:%20http://evil.test", "token="):
        response = http.get(f"{_base(project_row, build)}?{query}")
        assert _is_route_refusal(response), query
        assert "location" not in response.headers, query
    assert manager.calls == []


def test_the_token_is_quoted_in_the_location_whatever_it_holds(monkeypatch, http, project_row, preview_build):
    # Defence in depth: a real token is hex and dots, so nothing needs quoting. With the verifier
    # replaced by one that accepts anything, a hostile value must still come out percent-encoded, in one
    # header line, and never end the header early.
    import app.routers.preview_proxy as preview_router

    monkeypatch.setattr(preview_router, "verify_preview_token", lambda *a, **k: None)
    build, _ = preview_build
    hostile = "a b/../c?d=e#f%g\r\nX-Injected: 1"
    from urllib.parse import quote

    response = http.get(f"{_base(project_row, build)}?token={quote(hostile, safe='')}&keep=1")

    assert response.status_code == 307
    location = response.headers["location"]
    assert location == (
        f"/api/projects/{project_row.id}/preview/{build.id}/~a%20b%2F..%2Fc%3Fd%3De%23f%25g%0D%0AX-Injected%3A%201/?keep=1"
    )
    assert "x-injected" not in response.headers


def test_the_location_is_built_from_the_routes_own_ids_never_from_the_host(http, project_row, preview_build):
    build, token = preview_build
    response = http.get(
        f"{_base(project_row, build)}?token={token}",
        headers={"Host": "evil.test", "X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "https", "Origin": "http://evil.test"},
    )

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("/api/projects/")  # path only: no scheme, no host
    assert "evil.test" not in location and "://" not in location


def test_the_location_carries_the_servers_root_path_and_nothing_the_client_chose(app, project_row, preview_build):
    build, token = preview_build
    status, headers, _content = _asgi(
        app, "GET", _base(project_row, build), query=f"token={token}&a=1".encode(), root_path="/forge",
        headers={"Host": "evil.test"},
    )

    assert status == 307
    assert headers["location"] == [f"/forge/api/projects/{project_row.id}/preview/{build.id}/~{token}/?a=1"]


def test_a_sub_path_with_a_token_query_is_proxied_not_redirected(http, manager, project_row, preview_build):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}/items?a=1&token={token}")

    assert response.status_code == 200 and "location" not in response.headers
    assert [(c["path"], c["query"]) for c in manager.calls] == [("/items", "a=1")]


def test_a_post_to_the_entry_url_is_proxied_not_redirected(http, manager, project_row, preview_build):
    build, token = preview_build
    response = http.post(f"{_base(project_row, build)}?token={token}", content=b"x", headers={"Content-Type": "text/plain"})

    assert response.status_code == 200 and "location" not in response.headers
    assert [(c["method"], c["path"]) for c in manager.calls] == [("POST", "/")]


@pytest.mark.parametrize(
    ("suffix", "expected_path"),
    [
        ("", "/"),
        ("/", "/"),
        ("/items", "/items"),
        ("/items/", "/items/"),
        ("/a/b/c.js", "/a/b/c.js"),
        ("/items/~keep", "/items/~keep"),  # a `~` further down is the app's own business
    ],
)
def test_the_token_segment_is_stripped_before_the_container(http, manager, project_row, preview_build, suffix, expected_path):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}/~{token}{suffix}?x=1")

    assert response.status_code == 200
    assert [(c["path"], c["query"]) for c in manager.calls] == [(expected_path, "x=1")]
    assert token not in json.dumps(manager.calls)  # not in the path, the query, nor anywhere else


@pytest.mark.parametrize("which", ["garbage", "empty", "wrong-signature", "other-build", "expired", "superseded", "query-token-valid"])
def test_a_bad_token_segment_is_403_and_never_reaches_the_container(
    http, manager, db_session, project_row, preview_build, which
):
    build, token = preview_build
    if which == "garbage":
        segment = "~garbage"
    elif which == "empty":
        segment = "~"
    elif which == "wrong-signature":
        segment = "~" + token[:-1] + ("0" if token[-1] != "0" else "1")
    elif which == "other-build":
        segment = "~" + issue_preview_token("some-other-build")[0]
    elif which == "expired":
        expired, _ = issue_preview_token(build.id, ttl_minutes=-5)
        _store(db_session, build, expired)
        segment = "~" + expired
    elif which == "superseded":
        _store(db_session, build, issue_preview_token(build.id)[0])
        segment = "~" + token
    else:
        segment = "~garbage"  # the `~` segment wins: a valid ?token= does not rescue it
    query = f"?token={token}" if which == "query-token-valid" else ""

    response = http.get(f"{_base(project_row, build)}/{segment}/items{query}")

    assert _is_route_refusal(response)
    assert "location" not in response.headers
    assert manager.calls == []


def test_when_both_carriers_are_present_the_segment_wins_and_the_query_token_is_gone(http, manager, project_row, preview_build):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}/~{token}/items?a=1&token=GARBAGE&b=2&tok%65n=GARBAGE2")

    assert response.status_code == 200
    assert [(c["path"], c["query"]) for c in manager.calls] == [("/items", "a=1&b=2")]


def test_a_dot_free_rule_is_not_in_force_the_token_has_two_dots(http, manager, project_row, preview_build):
    build, token = preview_build
    assert token.count(".") == 2
    assert http.get(f"{_base(project_row, build)}/~{token}/app.v2.js").status_code == 200
    assert manager.calls[0]["path"] == "/app.v2.js"


# ---------------------------------------------------------------------------
# CORS: only for `Origin: null` on the shape, outside the global layer, never with credentials
# ---------------------------------------------------------------------------

PREFLIGHT_METHODS = ["POST", "PUT", "PATCH", "DELETE", "GET"]


def _assert_null_cors(response, *, vary=("Origin",)):
    assert response.headers["access-control-allow-origin"] == "null"
    assert response.headers.get_list("vary") == list(vary)  # one header, `Origin` once
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("requested", PREFLIGHT_METHODS)
def test_a_preflight_on_the_shape_is_204_with_exactly_the_five_headers(http, manager, project_row, preview_build, requested):
    build, token = preview_build
    for url in (f"{_base(project_row, build)}/~{token}/items", f"{_base(project_row, build)}/items", _base(project_row, build)):
        response = http.options(
            url,
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": requested,
                "Access-Control-Request-Headers": "content-type",
                "Sec-Fetch-Site": "cross-site",
            },
        )

        assert response.status_code == 204, url
        assert response.content == b""
        cors = {k: v for k, v in response.headers.items() if k.startswith("access-control-") or k == "vary"}
        assert cors == {
            "access-control-allow-origin": "null",
            "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE",
            "access-control-allow-headers": "Content-Type",
            "access-control-max-age": "600",
            "vary": "Origin",
        }, url
    assert manager.calls == []


def test_a_preflight_is_answered_before_routing_and_before_the_database(project_row, preview_build):
    # The database dependency raises: a preflight that reached the route would be a 500.
    build, token = preview_build
    from app.config import Settings as _Settings

    app = create_app(_Settings(_env_file=None, forge_env="dev", cors_origins=LISTED_ORIGIN))

    def _broken():
        raise RuntimeError("the database must not be touched by a preflight")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = _broken
    for requested in ("PUT", "DELETE"):
        status, headers, _content = _asgi(
            app, "OPTIONS", f"{_base(project_row, build)}/~{token}/items",
            headers={"Origin": "null", "Access-Control-Request-Method": requested},
        )
        assert status == 204
        assert headers["access-control-allow-origin"] == ["null"]


def test_the_preflight_is_204_and_not_the_global_layers_400(http, project_row, preview_build):
    # `CORSMiddleware` answers a preflight from an origin it does not list with 400. Same request on a
    # route that is NOT on the shape shows what it does; on the shape the new layer, outside it, wins.
    build, _ = preview_build
    headers = {"Origin": "null", "Access-Control-Request-Method": "PUT"}

    off_the_shape = http.options(f"/api/projects/{project_row.id}/service/items", headers=headers)
    assert off_the_shape.status_code == 400
    assert "access-control-allow-origin" not in off_the_shape.headers

    on_the_shape = http.options(f"{_base(project_row, build)}/items", headers=headers)
    assert on_the_shape.status_code == 204


def test_options_without_null_origin_or_without_a_request_method_is_still_405(http, project_row, preview_build):
    build, token = preview_build
    url = f"{_base(project_row, build)}/~{token}/items"

    no_origin = http.options(url)
    no_origin_with_method = http.options(url, headers={"Access-Control-Request-Method": "PUT"})
    null_without_method = http.options(url, headers={"Origin": "null"})
    for response in (no_origin, no_origin_with_method, null_without_method):
        assert response.status_code == 405
        assert set(response.headers["allow"].split(", ")) == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert "access-control-allow-origin" not in no_origin.headers
    assert "access-control-allow-origin" not in no_origin_with_method.headers
    assert "access-control-allow-methods" not in null_without_method.headers  # an answer, not a preflight


@pytest.mark.parametrize("origin", ["http://evil.test", "http://127.0.0.1:5391", "NULL", "nul"])
def test_any_other_origin_gets_nothing_from_the_preview_layer(http, manager, project_row, preview_build, origin):
    build, token = preview_build
    headers = {"Origin": origin, "Access-Control-Request-Method": "PUT"}

    preflight = http.options(f"{_base(project_row, build)}/items", headers=headers)
    assert preflight.status_code == 400  # the global layer's own refusal, as before
    assert "access-control-allow-origin" not in preflight.headers

    real = http.get(f"{_base(project_row, build)}/~{token}/items", headers={"Origin": origin})
    assert real.status_code == 200
    assert "access-control-allow-origin" not in real.headers


def test_no_origin_gets_no_cors_headers(http, manager, project_row, preview_build):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}/~{token}/items")
    assert response.status_code == 200
    assert not [k for k in response.headers if k.startswith("access-control-")]
    # (`Vary: Origin` is the global layer's own, added to every answer: not this layer's business)


def test_the_service_route_gets_no_preview_cors(http, project_row, db_session):
    build = Build(id="caseaq-dep-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    db_session.add(
        Deployment(project_id=project_row.id, build_id=build.id, confirmed_by_id=project_row.owner_id, status=DeploymentStatus.ACTIVE)
    )
    db_session.commit()

    response = http.get(f"/api/projects/{project_row.id}/service/items", headers={"Origin": "null"})
    assert "access-control-allow-origin" not in response.headers


def _status_cases(http, manager, db_session, project_row, build, token, monkeypatch):
    """(status, response) for each real answer the shape can produce, all from `Origin: null`."""
    import app.routers.preview_proxy as preview_router

    base = _base(project_row, build)
    sandboxed = _sandboxed()
    out = {}
    out[200] = http.get(f"{base}/~{token}/items", headers=sandboxed)
    out[307] = http.get(f"{base}?token={token}", headers=sandboxed)
    out[403] = http.get(f"{base}/~garbage/items", headers=sandboxed)
    out[404] = http.get(f"/api/projects/{project_row.id}/preview/no-such-build/~{token}/items", headers=sandboxed)
    out[414] = http.get(f"{base}/~{token}/items?a={'x' * 8200}", headers=sandboxed)
    out[413] = http.post(
        f"{base}/~{token}/items", content=b"x" * 65537, headers=_sandboxed(**{"Content-Type": "text/plain"})
    )
    monkeypatch.setattr(
        preview_router,
        "_preview_manager",
        lambda: FakeManager(error=preview_manager_module.PreviewStartError("preview container is gone")),
    )
    out[502] = http.get(f"{base}/~{token}/items", headers=sandboxed)
    return out


def test_every_real_answer_on_the_shape_carries_acao_null_and_a_single_vary(
    http, manager, db_session, project_row, preview_build, monkeypatch
):
    build, token = preview_build
    cases = _status_cases(http, manager, db_session, project_row, build, token, monkeypatch)

    assert sorted(cases) == [200, 307, 403, 404, 413, 414, 502]
    for status, response in cases.items():
        assert response.status_code == status
        _assert_null_cors(response)
    # the route's own answers (not a layer's) are the ones carrying the isolation headers: the CORS
    # headers were added to answers the route produced, error and success alike
    assert all("content-security-policy" in r.headers for r in cases.values())


def test_a_listed_origin_keeps_its_own_cors_answer_and_not_null(http, manager, project_row, preview_build):
    build, token = preview_build
    response = http.get(f"{_base(project_row, build)}/~{token}/items", headers={"Origin": LISTED_ORIGIN})
    assert response.headers["access-control-allow-origin"] == LISTED_ORIGIN
    assert response.headers.get_list("vary") == ["Origin"]


def test_allow_credentials_is_never_sent_to_a_null_origin_though_the_global_layer_would(
    http, manager, project_row, preview_build
):
    # The global CORS layer is configured with credentials for the SPA and adds
    # `Access-Control-Allow-Credentials: true` to every answer that has an Origin. For `null` on the
    # shape it must not survive. The SAME request for a listed origin (not this layer's business) shows
    # the header IS being produced underneath, so its absence for `null` is this layer's doing.
    build, token = preview_build
    url = f"{_base(project_row, build)}/~{token}/items"
    listed = http.get(url, headers={"Origin": LISTED_ORIGIN})
    assert listed.headers["access-control-allow-credentials"] == "true"

    null = http.get(url, headers={"Origin": "null"})
    assert null.status_code == 200 and "access-control-allow-credentials" not in null.headers
    credentialed = http.get(url, headers={"Origin": "null", "Cookie": "forge_session=x"})
    assert "access-control-allow-credentials" not in credentialed.headers
    assert null.headers["access-control-allow-origin"] != "*"


# ---------------------------------------------------------------------------
# the isolation headers (Referrer-Policy) and the cookie
# ---------------------------------------------------------------------------


def test_referrer_policy_is_on_the_success_and_the_error_answers_of_the_route(
    http, manager, project_row, preview_build, monkeypatch
):
    build, token = preview_build
    cases = _status_cases(http, manager, None, project_row, build, token, monkeypatch)
    for status, response in cases.items():
        assert response.headers["referrer-policy"] == "no-referrer", status


def test_the_preview_route_reads_no_cookie_a_session_without_a_token_is_403(http, manager, project_row, preview_build):
    build, _ = preview_build
    response = http.get(
        f"{_base(project_row, build)}/items",
        headers={
            "Cookie": "forge_session=a-valid-looking-session; session=abc",
            "X-Forge-User-Id": project_row.owner_id,  # the dev identity of the project's owner
        },
    )
    assert _is_route_refusal(response)
    assert manager.calls == []

    navigation = http.get(
        f"{_base(project_row, build)}/",
        headers={"Cookie": "forge_session=a-valid-looking-session", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
    )
    assert _is_route_refusal(navigation) and manager.calls == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_cookie_with_a_valid_token_succeeds_and_the_container_receives_no_cookie(
    http, manager, project_row, preview_build, method
):
    build, token = preview_build
    response = http.request(
        method,
        f"{_base(project_row, build)}/~{token}/items",
        content=b"{}" if method == "POST" else None,
        headers={
            "Cookie": "forge_session=SECRET-COOKIE",
            "Authorization": "Bearer SECRET-AUTH",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    (call,) = manager.calls
    # the route hands the manager these values and nothing else: there is no header for a cookie to ride in
    assert set(call) == {"build_id", "method", "path", "query", "body", "content_type", "max_body_bytes"}
    assert "SECRET" not in json.dumps({k: str(v) for k, v in call.items()})
    assert "set-cookie" not in response.headers
