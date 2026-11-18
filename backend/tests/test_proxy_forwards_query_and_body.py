"""the platform-origin proxies forward a bounded query, a bounded body and
one allowlisted header, and read a bounded answer back. No Docker anywhere in this file.

The end-to-end tests run the REAL chain -- route -> real `PreviewManager` / `DeployManager` -> the
real exec script -- against a local echo server standing in for the generated service. Only
`container.exec_run` is replaced: it feeds the script to a host `python` over stdin (an argv of 64 KiB
of base64 would not fit a Windows command line) and returns what a Docker exec would. The echo server
records the request line, the headers and the body exactly as the container's app would see them, so
every "never reaches the container" assertion is read off the server, not off the route.

Contract strings (header names, the `token` parameter, the status codes and the byte limits) are
spelled as literals on purpose: a one-sided rename must turn a test red."""

from __future__ import annotations

import ast
import base64
import http.server
import json
import socketserver
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit

import pytest

from app.config import settings
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import deploy_manager as deploy_manager_module
from app.services import preview_manager as preview_manager_module
from app.services import proxy_request
from app.services.deploy_manager import DeployManager
from app.services.preview_manager import PreviewManager, hash_token, issue_preview_token
from app.services.proxy_request import (
    ProxyRequestError,
    forwardable_content_type,
    prepare_query,
    quote_path,
    read_bounded_body,
)
from app.services.status import BuildStatus, ProjectStatus

EXPECTED_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
TWO_MIB = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# the limits are the ADR's, pinned once by literal
# ---------------------------------------------------------------------------


def test_the_limits_are_the_adrs_numbers():
    assert proxy_request.MAX_QUERY_BYTES == 8192
    assert proxy_request.MAX_BODY_BYTES == 65536
    assert proxy_request.MAX_RESPONSE_BYTES == 2097152
    assert proxy_request.FORWARDED_MEDIA_TYPES == {
        "application/json",
        "application/x-www-form-urlencoded",
        "text/plain",
        "multipart/form-data",
    }
    assert proxy_request.BODY_METHODS == {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# query: pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"token=T", ""),
        (b"token=T&a=1", "a=1"),
        (b"a=1&token=T", "a=1"),
        (b"a=1&token=T&b=2", "a=1&b=2"),
        (b"token=T1&a=1&token=T2&b=2&token=T3", "a=1&b=2"),
        (b"tok%65n=T&a=1", "a=1"),  # an encoded spelling of the name is still the token
        (b"a=1&TOKEN=keep", "a=1&TOKEN=keep"),  # a different name is somebody else's parameter
        (b"a=1&mytoken=keep&token2=keep", "a=1&mytoken=keep&token2=keep"),
        (b"token", ""),  # a bare name
        (b"a=token&b=2", "a=token&b=2"),  # the word as a VALUE is not the parameter
    ],
)
def test_preview_query_loses_every_token_parameter(raw, expected):
    assert prepare_query(raw, strip_token=True) == expected


def test_service_query_keeps_a_parameter_called_token():
    assert prepare_query(b"token=mine&a=1", strip_token=False) == "token=mine&a=1"


def test_query_is_verbatim_for_what_a_url_may_carry():
    raw = b"q=%27%22%0A%3B%60%00&r='x';y&s=a+b&t=1,2/3?4:5@6!$()*[]~-._"
    assert prepare_query(raw, strip_token=False) == raw.decode()


def test_query_is_encoded_where_a_url_could_not_carry_it():
    # A space ends a request line, `#` ends the URL, a control character or a byte over 0x7f is not
    # URL text: each is percent-encoded (byte for byte, not as a character), never passed on raw.
    assert prepare_query(b"a=b c#d\x00e\n\xc3\xa9", strip_token=False) == "a=b%20c%23d%00e%0A%C3%A9"


def test_query_exactly_at_the_limit_passes_and_one_over_is_414():
    at_limit = b"a=" + b"x" * (8192 - 2)
    assert len(at_limit) == 8192
    assert prepare_query(at_limit, strip_token=False) == at_limit.decode()
    with pytest.raises(ProxyRequestError) as excinfo:
        prepare_query(at_limit + b"x", strip_token=False)
    assert excinfo.value.status_code == 414


def test_query_limit_is_measured_before_the_token_is_removed():
    # 8193 bytes on the wire, 8 of them the token: still over the limit.
    raw = b"token=T&a=" + b"x" * (8193 - len(b"token=T&a="))
    with pytest.raises(ProxyRequestError) as excinfo:
        prepare_query(raw, strip_token=True)
    assert excinfo.value.status_code == 414


# ---------------------------------------------------------------------------
# path: re-quoted
# ---------------------------------------------------------------------------


def test_quote_path_encodes_everything_that_could_end_or_split_a_url():
    decoded = "/a'b\"c\nd;e`f\x00g?h#i%j k"
    quoted = quote_path(decoded)
    assert quoted == "/a%27b%22c%0Ad%3Be%60f%00g%3Fh%23i%25j%20k"
    assert unquote(quoted) == decoded  # and it round-trips: the container decodes it back
    assert "?" not in quoted and "#" not in quoted


def test_quote_path_keeps_slashes_and_unreserved_characters():
    assert quote_path("/items/a-b_c.d~e/9") == "/items/a-b_c.d~e/9"


# ---------------------------------------------------------------------------
# Content-Type: allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "  text/plain ;charset=UTF-8",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=----WebKitFormBoundaryAbC123",
        "TEXT/PLAIN",
    ],
)
def test_allowed_content_type_is_forwarded_whole(value):
    assert forwardable_content_type(value) == value  # full value: boundary and charset kept


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "text/html",
        "application/xml",
        "application/octet-stream",
        "application/jsonx",
        "text/plain, text/html",
        "xapplication/json",
        "application/json\r\nX-Injected: 1",
        "application/json\x00",
        "application/json\x7f",
        "application/json; x=" + "a" * 1100,
    ],
)
def test_any_other_content_type_is_dropped(value):
    assert forwardable_content_type(value) is None


# ---------------------------------------------------------------------------
# body: bounded read, counted on a finite source
# ---------------------------------------------------------------------------


class _CountingSource:
    """A FINITE async source of `total_chunks` chunks that counts how many it handed out. Finite on
    purpose: with the bound removed the reader drains it and `delivered` shows it; an endless source
    would hang the test instead of failing it."""

    def __init__(self, chunk: bytes, total_chunks: int) -> None:
        self._chunk = chunk
        self._total = total_chunks
        self.delivered = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for _ in range(self._total):
            self.delivered += 1
            yield self._chunk


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_an_undeclared_stream_over_the_cap_is_refused_and_never_drained():
    source = _CountingSource(b"x" * 1024, total_chunks=300)  # 300 KiB on offer, cap is 64 KiB
    with pytest.raises(ProxyRequestError) as excinfo:
        _run(read_bounded_body(source, declared_length=None))
    assert excinfo.value.status_code == 413
    # 64 chunks fit; the 65th is the one that passes the cap and is pulled, not kept: cap + one chunk.
    assert source.delivered == 65
    assert source.delivered * 1024 <= 64 * 1024 + 1024


def test_a_stream_that_lies_about_its_length_is_still_counted():
    source = _CountingSource(b"x" * 1024, total_chunks=300)
    with pytest.raises(ProxyRequestError) as excinfo:
        _run(read_bounded_body(source, declared_length=10))
    assert excinfo.value.status_code == 413
    assert source.delivered == 65


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read():
    source = _CountingSource(b"x" * 1024, total_chunks=300)
    with pytest.raises(ProxyRequestError) as excinfo:
        _run(read_bounded_body(source, declared_length=65537))
    assert excinfo.value.status_code == 413
    assert source.delivered == 0


def test_a_body_of_exactly_the_cap_is_read_whole():
    source = _CountingSource(b"y" * 1024, total_chunks=64)
    body = _run(read_bounded_body(source, declared_length=65536))
    assert len(body) == 65536
    assert source.delivered == 64


def test_one_byte_over_the_cap_is_refused():
    source = _CountingSource(b"y" * 4096, total_chunks=16)
    assert len(_run(read_bounded_body(source, declared_length=None))) == 65536
    over = _CountingSource(b"y" * 65537, total_chunks=1)
    with pytest.raises(ProxyRequestError):
        _run(read_bounded_body(over, declared_length=None))


# ---------------------------------------------------------------------------
# the exec script: request data only through repr / base64
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [preview_manager_module, deploy_manager_module], ids=["preview", "deploy"])
def test_the_script_carries_the_body_as_base64_never_as_text(module):
    body = b"'''; import os; os.system('x') #\x00\"\"\"\n"
    script = module._build_proxy_exec_script(
        "http://127.0.0.1:1/x", "POST", 5.0, None, body=body, content_type="text/plain"
    )
    assert base64.b64encode(body).decode() in script
    assert "os.system" not in script
    assert "\x00" not in script
    compile(script, "<script>", "exec")  # still valid Python, whatever the body was


@pytest.mark.parametrize("module", [preview_manager_module, deploy_manager_module], ids=["preview", "deploy"])
def test_a_hostile_url_and_header_value_stay_string_literals(module):
    url = "http://127.0.0.1:1/a'\"\n;`\\" + "x"
    script = module._build_proxy_exec_script(url, "GET", 5.0, content_type="text/plain; x='\"\\")
    tree = ast.parse(script)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    request_call = next(c for c in calls if getattr(c.func, "attr", "") == "Request")
    assert ast.literal_eval(request_call.args[0]) == url
    headers = next(k.value for k in request_call.keywords if k.arg == "headers")
    assert ast.literal_eval(headers) == {"Content-Type": "text/plain; x='\"\\"}


@pytest.mark.parametrize("module", [preview_manager_module, deploy_manager_module], ids=["preview", "deploy"])
def test_the_script_without_a_body_or_header_is_the_old_one(module):
    script = module._build_proxy_exec_script("http://127.0.0.1:1/", "GET", 5.0)
    assert script.splitlines()[0] == "import urllib.request, sys"
    assert script.splitlines()[1] == "req = urllib.request.Request('http://127.0.0.1:1/', method='GET')"
    assert "resp.read()" in script and "e.read()" in script


# ---------------------------------------------------------------------------
# the end-to-end rig: route -> real manager -> real script -> echo server
# ---------------------------------------------------------------------------


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _answer(self):
        server = self.server
        server.requests_seen += 1  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        if parts.path.startswith("/big/"):
            size = int(parts.path.split("/")[2])
            self.send_response(404 if parts.query == "error" else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"z" * size)
            return
        # the headers exactly as the generated app would receive them
        received = {name.lower(): value for name, value in self.headers.items()}
        payload = json.dumps(
            {
                "method": self.command,
                "raw_target": self.path,
                "path": parts.path,
                "query": parts.query,
                "headers": received,
                "body_b64": base64.b64encode(body).decode(),
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "session=leak-me-not")
        self.send_header("X-Upstream-Secret", "leak-me-not")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer  # noqa: N815

    def log_message(self, format, *args):  # noqa: A002 - silence
        pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    requests_seen = 0


class _Container:
    status = "running"

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.argv_lengths: list[int] = []

    def exec_run(self, cmd):
        assert cmd[:2] == ["python", "-c"] and len(cmd) == 3  # an argv, never a shell string
        self.scripts.append(cmd[2])
        self.argv_lengths.append(len(cmd[2].encode()))
        proc = subprocess.run(
            [sys.executable, "-"], input=cmd[2].encode(), capture_output=True, timeout=60
        )
        return proc.returncode, proc.stdout + proc.stderr


class _Containers:
    def __init__(self, container) -> None:
        self._container = container

    def get(self, name):
        return self._container


class _Client:
    def __init__(self, container) -> None:
        self.containers = _Containers(container)


@pytest.fixture()
def echo(monkeypatch):
    server = _Server(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "preview_container_port", server.server_address[1])
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def container():
    return _Container()


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="caseap-proxy",
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
    build = Build(id="caseap-pv-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.commit()
    token, _expires = issue_preview_token(build.id)
    build.preview_token_hash = hash_token(token)
    db_session.commit()
    return build, token


@pytest.fixture()
def active_deployment(db_session, project_row):
    build = Build(id="caseap-dep-1", project_id=project_row.id, type="create", status=BuildStatus.SUCCEEDED)
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


class _Rig:
    """One call shape for both routes: `rig.call(method, suffix, query=..., content=..., headers=...)`."""

    def __init__(self, client, kind, project, build_token, container):
        self.client = client
        self.kind = kind
        self.project = project
        self.build_token = build_token
        self.container = container

    def url(self, suffix: str, query: str) -> str:
        if self.kind == "preview":
            build, token = self.build_token
            base = f"/api/projects/{self.project.id}/preview/{build.id}/{suffix}"
            query = f"token={token}" + (f"&{query}" if query else "")
        else:
            base = f"/api/projects/{self.project.id}/service/{suffix}"
        return base + (f"?{query}" if query else "")

    def call(self, method, suffix="items", *, query="", content=None, headers=None):
        return self.client.request(method, self.url(suffix, query), content=content, headers=headers)

    @staticmethod
    def echoed(response) -> dict:
        assert response.status_code == 201, response.text
        return json.loads(response.content)


@pytest.fixture(params=["preview", "service"])
def rig(request, client, echo, container, project_row, monkeypatch, db_session):
    import app.routers.preview_proxy as preview_router
    import app.routers.service_proxy as service_router

    fake = _Client(container)
    if request.param == "preview":
        build_token = request.getfixturevalue("preview_build")
        monkeypatch.setattr(preview_router, "_preview_manager", lambda: PreviewManager(client=fake))
    else:
        build_token = None
        request.getfixturevalue("active_deployment")
        monkeypatch.setattr(service_router, "_proxy_manager", lambda: DeployManager(client=fake))
    return _Rig(client, request.param, project_row, build_token, container)


def _assert_isolation(response, status: int):
    assert response.status_code == status
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers


# -- query ------------------------------------------------------------------


def test_the_query_reaches_the_container_verbatim(rig, echo):
    query = "a=1&b=two%20words&c=&d=%27%22%0A%3B%60%00&e='x';y&f=a+b"
    echoed = rig.echoed(rig.call("GET", "items", query=query))
    assert echoed["query"] == query
    assert echoed["path"] == "/items"


def test_a_post_with_a_json_body_and_a_query_reaches_the_service_and_comes_back(rig, echo):
    response = rig.call(
        "POST",
        "items",
        query="value=7&tag=x",
        content=b'{"name": "n", "n": [1, 2]}',
        headers={"Content-Type": "application/json"},
    )
    echoed = rig.echoed(response)
    assert echoed["method"] == "POST"
    assert echoed["query"] == "value=7&tag=x"
    assert base64.b64decode(echoed["body_b64"]) == b'{"name": "n", "n": [1, 2]}'
    assert echoed["headers"]["content-type"] == "application/json"
    assert response.headers["content-type"] == "application/json"


def test_a_query_over_the_limit_is_414_with_the_isolation_headers_and_never_reaches_the_container(rig, echo):
    over = "a=" + "x" * 8192  # preview adds its token on top: over either way
    response = rig.call("GET", "items", query=over)
    _assert_isolation(response, 414)
    assert echo.requests_seen == 0
    assert rig.container.scripts == []


def test_a_query_at_the_limit_passes(rig, echo):
    prefix = len(rig.url("items", "").split("?", 1)[1]) if rig.kind == "preview" else 0
    # preview: the raw query on the wire is `token=<T>&<ours>`; make the WHOLE of it exactly 8192
    pad = 8192 - prefix - (1 if rig.kind == "preview" else 0) - len("a=")
    echoed = rig.echoed(rig.call("GET", "items", query="a=" + "x" * pad))
    assert echoed["query"] == "a=" + "x" * pad


def test_the_preview_token_never_reaches_the_container(client, echo, container, preview_build, project_row, monkeypatch):
    import app.routers.preview_proxy as preview_router

    monkeypatch.setattr(
        preview_router, "_preview_manager", lambda: PreviewManager(client=_Client(container))
    )
    build, token = preview_build
    base = f"/api/projects/{project_row.id}/preview/{build.id}/items"
    cases = {
        f"?token={token}": "",
        f"?token={token}&a=1&b=2": "a=1&b=2",
        f"?a=1&token={token}&b=2": "a=1&b=2",
        f"?a=1&b=2&token={token}": "a=1&b=2",
        f"?token={token}&a=1&token={token}&b=2&token={token}": "a=1&b=2",
    }
    for suffix, expected_query in cases.items():
        echoed = json.loads(client.get(base + suffix).content)
        assert echoed["query"] == expected_query, suffix
        assert echoed["raw_target"] == "/items" + (f"?{expected_query}" if expected_query else "")
    # and nowhere else in what the container was sent
    assert token not in "".join(container.scripts)


def test_a_service_parameter_called_token_is_forwarded(client, echo, container, project_row, active_deployment, monkeypatch):
    import app.routers.service_proxy as service_router

    monkeypatch.setattr(
        service_router, "_proxy_manager", lambda: DeployManager(client=_Client(container))
    )
    echoed = json.loads(client.get(f"/api/projects/{project_row.id}/service/items?token=mine&a=1").content)
    assert echoed["query"] == "token=mine&a=1"


# -- path -------------------------------------------------------------------


def test_a_hostile_path_segment_never_becomes_a_query_or_splits_the_request(rig, echo):
    decoded = "a'b\"c\td;e`f\x00g?h#i%j k"
    encoded_by_the_client = "a%27b%22c%09d%3Be%60f%00g%3Fh%23i%25j%20k"
    echoed = rig.echoed(rig.call("GET", encoded_by_the_client, query="real=1"))
    assert echoed["query"] == "real=1"  # the `?` inside the segment did not start a query
    assert unquote(echoed["path"]) == f"/{decoded}"  # the container decodes the segment it was sent
    assert echo.requests_seen == 1
    script = rig.container.scripts[0]
    compile(script, "<script>", "exec")


def test_a_newline_in_a_path_segment_never_reaches_the_container(rig, echo):
    # Starlette's `{path:path}` does not match a decoded newline: the route answers 404 itself.
    response = rig.call("GET", "a%0Ab")
    assert response.status_code == 404
    assert echo.requests_seen == 0 and rig.container.scripts == []


def test_a_hostile_path_with_no_query_has_no_query(rig, echo):
    echoed = rig.echoed(rig.call("GET", "x%3Fy%3Dz%23frag"))
    assert echoed["query"] == ""
    assert unquote(echoed["path"]) == "/x?y=z#frag"


# -- body -------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_these_methods_forward_their_body(rig, echo, method):
    echoed = rig.echoed(
        rig.call(method, "items", content=b'{"a": 1}', headers={"Content-Type": "application/json"})
    )
    assert echoed["method"] == method
    assert base64.b64decode(echoed["body_b64"]) == b'{"a": 1}'


def test_a_get_forwards_no_body_even_if_one_is_sent(rig, echo):
    echoed = rig.echoed(
        rig.call("GET", "items", content=b'{"a": 1}', headers={"Content-Type": "application/json"})
    )
    assert base64.b64decode(echoed["body_b64"]) == b""
    assert "content-length" not in echoed["headers"]
    assert "content-type" not in echoed["headers"]
    assert "base64" not in rig.container.scripts[0]


def test_a_body_with_triple_quotes_and_a_nul_byte_arrives_intact(rig, echo):
    body = b"'''\"\"\"; import os; os.system('echo pwned') #\x00\x01\xff\r\n```"
    echoed = rig.echoed(rig.call("POST", "items", content=body, headers={"Content-Type": "text/plain"}))
    assert base64.b64decode(echoed["body_b64"]) == body
    assert "pwned" not in rig.container.scripts[0]


def test_a_body_of_exactly_the_cap_passes_and_fits_one_argument(rig, echo):
    body = bytes(range(256)) * 256  # 65536 bytes
    assert len(body) == 65536
    echoed = rig.echoed(rig.call("POST", "items", content=body, headers={"Content-Type": "text/plain"}))
    assert base64.b64decode(echoed["body_b64"]) == body
    assert rig.container.argv_lengths[0] < 128 * 1024  # the kernel's single-argument limit


def test_a_declared_body_over_the_cap_is_413_and_never_reaches_the_container(rig, echo):
    response = rig.call("POST", "items", content=b"x" * 65537, headers={"Content-Type": "text/plain"})
    _assert_isolation(response, 413)
    assert echo.requests_seen == 0 and rig.container.scripts == []


def test_an_undeclared_chunked_body_over_the_cap_is_413_and_never_reaches_the_container(rig, echo):
    delivered = []

    def chunks():
        for _ in range(200):  # 200 KiB on offer, finite
            delivered.append(1)
            yield b"x" * 1024

    response = rig.call("POST", "items", content=chunks(), headers={"Content-Type": "text/plain"})
    _assert_isolation(response, 413)
    assert echo.requests_seen == 0 and rig.container.scripts == []


def test_a_chunked_body_under_the_cap_is_forwarded(rig, echo):
    def chunks():
        for _ in range(10):
            yield b"abc" * 100

    echoed = rig.echoed(rig.call("POST", "items", content=chunks(), headers={"Content-Type": "text/plain"}))
    assert base64.b64decode(echoed["body_b64"]) == b"abc" * 1000


# -- headers in -------------------------------------------------------------


HOSTILE_HEADERS = {
    "Cookie": "forge_session=SECRET-COOKIE",
    "Authorization": "Bearer SECRET-AUTH",
    "Origin": "https://secret-origin.example",
    "Referer": "https://secret-referer.example/?token=SECRET-REF",
    "X-Forwarded-For": "203.0.113.9",
    "X-Forwarded-Host": "secret-fwd.example",
    "X-Forge-User-Id": "SECRET-USER",
    "X-Custom-Header": "SECRET-CUSTOM",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
}


def test_no_request_header_but_content_type_reaches_the_container(rig, echo):
    headers = dict(HOSTILE_HEADERS) | {"Host": "secret-host.example"}
    echoed = rig.echoed(rig.call("POST", "items", content=b"{}", headers=headers))
    received = echoed["headers"]
    # what the container saw is urllib's own plus the one forwarded header: nothing else
    assert set(received) == {
        "accept-encoding",
        "connection",
        "content-length",
        "content-type",
        "host",
        "user-agent",
    }
    assert received["content-type"] == "application/json"
    assert received["host"] == f"127.0.0.1:{echo.server_address[1]}"
    dump = json.dumps(received) + rig.container.scripts[0]
    for secret in ("SECRET-", "secret-", "203.0.113.9"):
        assert secret not in dump


def test_the_full_content_type_value_is_forwarded(rig, echo):
    value = "multipart/form-data; boundary=----WebKitFormBoundaryAbC123"
    echoed = rig.echoed(rig.call("POST", "items", content=b"--x--", headers={"Content-Type": value}))
    assert echoed["headers"]["content-type"] == value
    value = "Application/JSON; charset=utf-8"
    echoed = rig.echoed(rig.call("POST", "items", content=b"{}", headers={"Content-Type": value}))
    assert echoed["headers"]["content-type"] == value


def test_a_content_type_off_the_allowlist_is_dropped(rig, echo):
    echoed = rig.echoed(rig.call("POST", "items", content=b"<x/>", headers={"Content-Type": "text/html"}))
    # urllib supplies its own default for a body with no type; the request's value is gone
    assert echoed["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "text/html" not in json.dumps(echoed["headers"])
    assert "headers=" not in rig.container.scripts[0]


# -- response ---------------------------------------------------------------


def test_status_and_content_type_come_back_and_nothing_else(rig, echo):
    response = rig.call("GET", "items")
    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert "set-cookie" not in response.headers
    assert "x-upstream-secret" not in response.headers
    assert "leak-me-not" not in str(response.headers)


def test_a_response_of_exactly_two_mib_passes(rig, echo):
    response = rig.call("GET", f"big/{TWO_MIB}")
    _assert_isolation(response, 200)
    assert len(response.content) == TWO_MIB


def test_a_response_one_byte_over_two_mib_is_502_with_a_platform_authored_detail(rig, echo):
    response = rig.call("GET", f"big/{TWO_MIB + 1}")
    _assert_isolation(response, 502)
    assert "too large" in response.json()["detail"]
    assert "zzzz" not in response.text


def test_an_error_response_over_two_mib_is_502_too(rig, echo):
    response = rig.call("GET", f"big/{TWO_MIB + 500}", query="error")
    _assert_isolation(response, 502)


def test_the_script_never_reads_more_than_the_cap_plus_one_byte(echo):
    # the bound lives inside the container: the exec output never exceeds cap + 1 + the two lines
    url = f"http://127.0.0.1:{echo.server_address[1]}/big/{TWO_MIB + 100000}"
    script = preview_manager_module._build_proxy_exec_script(url, "GET", 20.0, TWO_MIB + 1)
    result = subprocess.run([sys.executable, "-"], input=script.encode(), capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout) <= TWO_MIB + 1 + len(b"200\napplication/octet-stream\n")
    assert len(result.stdout) > TWO_MIB  # and it is not shorter than the cap either


def test_the_routes_ask_for_cap_plus_one(client, project_row, preview_build, active_deployment, monkeypatch):
    import app.routers.preview_proxy as preview_router
    import app.routers.service_proxy as service_router

    seen = []

    class _Recorder:
        def proxy(self, _id, **kwargs):
            seen.append(kwargs)
            return 200, b"ok", "text/plain"

    monkeypatch.setattr(preview_router, "_preview_manager", _Recorder)
    monkeypatch.setattr(service_router, "_proxy_manager", _Recorder)
    build, token = preview_build
    assert client.get(f"/api/projects/{project_row.id}/preview/{build.id}/page?token={token}").status_code == 200
    assert client.get(f"/api/projects/{project_row.id}/service/").status_code == 200
    assert [call["max_body_bytes"] for call in seen] == [2097153, 2097153]


# -- unchanged on purpose ---------------------------------------------------


@pytest.mark.parametrize("kind", ["preview", "service"])
def test_options_is_still_405(client, project_row, preview_build, active_deployment, kind):
    build, token = preview_build
    url = (
        f"/api/projects/{project_row.id}/preview/{build.id}?token={token}"
        if kind == "preview"
        else f"/api/projects/{project_row.id}/service/"
    )
    assert client.options(url).status_code == 405


def test_the_access_checks_still_come_first(client, echo, container, project_row, preview_build, monkeypatch):
    import app.routers.preview_proxy as preview_router

    monkeypatch.setattr(
        preview_router, "_preview_manager", lambda: PreviewManager(client=_Client(container))
    )
    build, _token = preview_build
    bad = client.post(
        f"/api/projects/{project_row.id}/preview/{build.id}/items?token=garbage", content=b"x" * 70000
    )
    _assert_isolation(bad, 403)  # not 413: the body was never read for an unauthorised caller
    assert echo.requests_seen == 0
