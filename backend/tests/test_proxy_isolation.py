"""ADR 0028 layers 1 and 2 unit tests: the isolation header set every
platform-origin proxy response carries (`services/proxy_headers.py`,
`routers/preview_proxy.py::proxy_preview`, `routers/service_proxy.py::
proxy_service`). No Docker anywhere in this file — the manager seam
(`routers.preview_proxy._preview_manager` / `routers.service_proxy._proxy_manager`, the
two per-route seams -- see `routers/service_proxy.py`'s own docstring)
is monkeypatched, the same pattern `test_project_activity.py`/
`test_deployments_api.py` already use for these two routes. The REAL proxy
path against a real container (a4) is proven separately in
`test_preview_manager_integration.py` and `test_deploy_integration.py`.
Layer 2's own conditional-mounting proof (b1-b3) lives in
`test_main_proxy_mounting.py`.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading

import pytest

from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import deploy_manager as deploy_manager_module
from app.services import preview_manager as preview_manager_module
from app.services.preview_manager import hash_token, issue_preview_token
from app.services.proxy_headers import (
    CONTENT_SECURITY_POLICY,
    DEFAULT_CONTENT_TYPE,
    build_isolation_headers,
)
from app.services.status import BuildStatus, ProjectStatus

EXPECTED_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"

# ADR 0028 §Decision 1: these must NEVER appear in the CSP sandbox value --
# each one hands the sandboxed document (or a window it opens) some slice
# of the platform's own origin authority.
FORBIDDEN_SANDBOX_TOKENS = (
    "allow-same-origin",
    "allow-top-navigation",
    "allow-top-navigation-by-user-activation",
    "allow-top-navigation-to-custom-protocols",
    "allow-popups-to-escape-sandbox",
)


# ---------------------------------------------------------------------------
# (a1) build_isolation_headers -- the one named header function
# ---------------------------------------------------------------------------


def test_csp_is_the_exact_pinned_value():
    headers = build_isolation_headers("text/html")
    assert headers["Content-Security-Policy"] == EXPECTED_CSP
    assert CONTENT_SECURITY_POLICY == EXPECTED_CSP


def test_csp_carries_all_five_required_tokens():
    tokens = build_isolation_headers(None)["Content-Security-Policy"].split()
    assert tokens[0] == "sandbox"
    for required in ("allow-scripts", "allow-forms", "allow-popups", "allow-modals", "allow-downloads"):
        assert required in tokens
    assert len(tokens) == 6  # "sandbox" + exactly the five above, nothing more


@pytest.mark.parametrize("forbidden", FORBIDDEN_SANDBOX_TOKENS)
def test_csp_never_carries_a_forbidden_token(forbidden):
    csp = build_isolation_headers("text/html")["Content-Security-Policy"]
    assert forbidden not in csp.split()


def test_nosniff_present_regardless_of_content_type():
    assert build_isolation_headers("text/html")["X-Content-Type-Options"] == "nosniff"
    assert build_isolation_headers(None)["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("content_type", [None, "", "   "])
def test_content_type_defaults_when_none_blank_or_whitespace(content_type):
    assert build_isolation_headers(content_type)["Content-Type"] == DEFAULT_CONTENT_TYPE
    assert DEFAULT_CONTENT_TYPE == "text/plain; charset=utf-8"


def test_content_type_forwarded_when_upstream_sent_one():
    headers = build_isolation_headers("application/json; charset=utf-8")
    assert headers["Content-Type"] == "application/json; charset=utf-8"


def test_headers_dict_has_exactly_these_keys():
    """No other upstream header (Set-Cookie included) has anywhere to ride
    along — the function's own return shape is the guarantee, not a filter
    that could be bypassed. `Referrer-Policy` was added:
    the set is now four."""
    assert set(build_isolation_headers("text/html").keys()) == {
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Content-Type",
    }


def test_referrer_policy_is_no_referrer_whatever_the_content_type():
    assert build_isolation_headers("text/html")["Referrer-Policy"] == "no-referrer"
    assert build_isolation_headers(None)["Referrer-Policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# (a5) the new parser degrades on an old-format / truncated response,
# instead of raising -- proven directly against both modules' private
# `_parse_proxy_output`, which the health gate does NOT go through (it
# keeps its own untouched `output.partition(b"\\n")` for the status line
# only, unaffected either way).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_parser_degrades_on_a_status_line_with_no_header_line(module):
    # "An older container, a truncated read": only ever a status line and a
    # body -- no Content-Type line at all.
    status, body, content_type = module._parse_proxy_output(b"200\nplain body, no header line")
    assert status == 200
    assert content_type is None
    assert body == b"plain body, no header line"


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_parser_degrades_on_just_a_status_line(module):
    status, body, content_type = module._parse_proxy_output(b"204\n")
    assert status == 204
    assert content_type is None
    assert body == b""


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_parser_reads_the_new_format_content_type_line(module):
    status, body, content_type = module._parse_proxy_output(b"200\napplication/json\n{\"a\": 1}")
    assert status == 200
    assert content_type == "application/json"
    assert body == b'{"a": 1}'


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_parser_blank_content_type_line_resolves_to_none(module):
    status, body, content_type = module._parse_proxy_output(b"200\n\nbody bytes")
    assert status == 200
    assert content_type is None
    assert body == b"body bytes"


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_parser_still_raises_on_a_genuinely_unparseable_status(module):
    with pytest.raises(ValueError):
        module._parse_proxy_output(b"not-a-status\nsomething\nelse")


# ---------------------------------------------------------------------------
# (a2, the Set-Cookie case) -- runs the REAL exec script text as a plain
# host subprocess (no Docker) against a local HTTP server that sends
# Set-Cookie, proving the wire format itself never carries it through: not
# a router-level assumption, the actual script both proxy managers hand to
# `docker exec`.
# ---------------------------------------------------------------------------


class _SetCookieHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib method name
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "session=leak-me-not")
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, format, *args):  # noqa: A002 - silence test server logs
        pass


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize(
    "module", [preview_manager_module, deploy_manager_module], ids=["preview_manager", "deploy_manager"]
)
def test_exec_script_never_carries_set_cookie_through(module):
    port = _free_loopback_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _SetCookieHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = module._build_proxy_exec_script(f"http://127.0.0.1:{port}/", "GET", 5.0)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert b"Set-Cookie" not in result.stdout
        assert b"leak-me-not" not in result.stdout

        status, body, content_type = module._parse_proxy_output(result.stdout)
        assert status == 200
        assert content_type == "text/html"
        assert body == b"hello"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# (a2) router-level: both routes, stubbed manager, asserting all three
# headers each time -- text/html forwarded, and no-content-type -> default.
# (a3) the routes' own error responses carry the same headers.
# ---------------------------------------------------------------------------


def _assert_isolation_headers(resp, *, content_type: str) -> None:
    assert resp.headers["content-security-policy"] == EXPECTED_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"  # success and error answers, both routes
    assert resp.headers["content-type"] == content_type
    assert "set-cookie" not in resp.headers


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="proxy-isolation-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


@pytest.fixture()
def preview_build(db_session, project_row):
    build = Build(
        id="pv-isolation-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED
    )
    db_session.add(build)
    db_session.commit()
    token, _expires_at = issue_preview_token(build.id)
    build.preview_token_hash = hash_token(token)
    db_session.commit()
    return build, token


class _FakePreviewManager:
    """Stands in for the real `PreviewManager` — the manager-level Set-Cookie
    proof lives above; this fixture only needs a controlled `content_type`."""

    def __init__(self, content_type):
        self._content_type = content_type

    def proxy(self, build_id, *, method="GET", path="/", **_forwarded):
        return 200, b"<html>preview</html>", self._content_type


class _RaisingPreviewManager:
    def proxy(self, build_id, *, method="GET", path="/", **_forwarded):
        raise preview_manager_module.PreviewStartError("preview container is gone")


def test_preview_route_forwards_upstream_content_type(client, db_session, preview_build, monkeypatch):
    import app.routers.preview_proxy as preview_proxy_router

    build, token = preview_build
    monkeypatch.setattr(preview_proxy_router, "_preview_manager", lambda: _FakePreviewManager("text/html"))

    resp = client.get(f"/api/projects/{build.project_id}/preview/{build.id}/page?token={token}")
    assert resp.status_code == 200
    _assert_isolation_headers(resp, content_type="text/html")


def test_preview_route_defaults_content_type_when_upstream_sends_none(
    client, db_session, preview_build, monkeypatch
):
    import app.routers.preview_proxy as preview_proxy_router

    build, token = preview_build
    monkeypatch.setattr(preview_proxy_router, "_preview_manager", lambda: _FakePreviewManager(None))

    resp = client.get(f"/api/projects/{build.project_id}/preview/{build.id}/page?token={token}")
    assert resp.status_code == 200
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_preview_route_403_bad_token_carries_isolation_headers(client, db_session, preview_build):
    build, _token = preview_build
    resp = client.get(f"/api/projects/{build.project_id}/preview/{build.id}?token=garbage")
    assert resp.status_code == 403
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_preview_route_non_ascii_signature_is_a_403_with_isolation_headers_not_a_500(
    client, db_session, preview_build
):
    # `compare_digest` on a non-ASCII str raised TypeError -> a 500 with none of the headers.
    build, token = preview_build
    nonce, expires_ts, _signature = token.split(".")
    resp = client.get(
        f"/api/projects/{build.project_id}/preview/{build.id}?token={nonce}.{expires_ts}.%C3%A9%E2%82%AC"
    )
    assert resp.status_code == 403
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_preview_route_404_wrong_project_carries_isolation_headers(client, db_session, preview_build):
    build, token = preview_build
    resp = client.get(f"/api/projects/not-the-real-project/preview/{build.id}?token={token}")
    assert resp.status_code == 404
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_preview_route_502_carries_isolation_headers(client, db_session, preview_build, monkeypatch):
    import app.routers.preview_proxy as preview_proxy_router

    build, token = preview_build
    monkeypatch.setattr(preview_proxy_router, "_preview_manager", _RaisingPreviewManager)

    resp = client.get(f"/api/projects/{build.project_id}/preview/{build.id}/page?token={token}")
    assert resp.status_code == 502
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


@pytest.fixture()
def active_deployment(db_session, project_row):
    build = Build(
        id="dep-isolation-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED
    )
    db_session.add(build)
    db_session.flush()
    deployment = Deployment(
        project_id=project_row.id,
        build_id=build.id,
        confirmed_by_id=project_row.owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db_session.add(deployment)
    db_session.commit()
    return deployment


class _FakeDeployManager:
    def __init__(self, content_type):
        self._content_type = content_type

    def proxy(self, deployment_id, *, method="GET", path="/", **_forwarded):
        return 200, b'{"ok": true}', self._content_type


def test_service_route_forwards_upstream_content_type(
    client, db_session, project_row, active_deployment, monkeypatch
):
    import app.routers.service_proxy as service_proxy_router

    monkeypatch.setattr(
        service_proxy_router, "_proxy_manager", lambda: _FakeDeployManager("application/json")
    )

    resp = client.get(f"/api/projects/{project_row.id}/service/healthz")
    assert resp.status_code == 200
    _assert_isolation_headers(resp, content_type="application/json")


def test_service_route_defaults_content_type_when_upstream_sends_none(
    client, db_session, project_row, active_deployment, monkeypatch
):
    import app.routers.service_proxy as service_proxy_router

    monkeypatch.setattr(service_proxy_router, "_proxy_manager", lambda: _FakeDeployManager(None))

    resp = client.get(f"/api/projects/{project_row.id}/service/healthz")
    assert resp.status_code == 200
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_service_route_404_no_active_deployment_carries_isolation_headers(
    client, db_session, project_row
):
    resp = client.get(f"/api/projects/{project_row.id}/service/healthz")
    assert resp.status_code == 404
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)


def test_service_route_404_unknown_project_carries_isolation_headers(client, db_session):
    resp = client.get("/api/projects/does-not-exist/service/healthz")
    assert resp.status_code == 404
    _assert_isolation_headers(resp, content_type=DEFAULT_CONTENT_TYPE)
