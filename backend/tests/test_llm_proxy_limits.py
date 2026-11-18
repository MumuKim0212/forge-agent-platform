"""the LLM proxy's request-body bound, the
separation of its admin routes from the listener the sandbox can reach, and the constant-time
admin-secret comparison.

No Docker. The proxy is driven in process, over a stub upstream that RECORDS what reaches it, so a
refusal test can say that nothing was forwarded and a bound test can count what the proxy read.
The packaged image (caps, user, the two real listeners) is `test_proxy_container_integration.py`.

The body tests go through httpx's ASGI transport, which hands the app its request body lazily,
chunk by chunk as the app awaits `receive()`; that is what makes "the proxy never read the rest" an
observable, where Starlette's `TestClient` buffers a whole body first.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app import proxy_server
from app.services import llm_proxy
from app.services.llm_proxy import TokenRegistry, create_app
from tests.openai_proxy_support import ADMIN_SECRET, REAL_KEY

ADMIN = {"X-Forge-Admin-Secret": ADMIN_SECRET}
KIB = 1024
MIB = 1024 * 1024

ANTHROPIC_ENV = {"FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET, "FORGE_PROXY_ANTHROPIC_API_KEY": REAL_KEY}
OPENAI_ENV = {
    "FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET,
    "FORGE_PROXY_DIALECT": "openai",
    "FORGE_PROXY_OPENAI_API_KEY": REAL_KEY,
}


class Upstream:
    """A stub Messages API that records each request it receives (path, body size)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.app = FastAPI()

        @self.app.api_route("/{path:path}", methods=["GET", "POST"])
        async def anything(path: str, request: Request) -> Response:
            self.calls.append(("/" + path, len(await request.body())))
            return Response(
                content=json.dumps(
                    {"id": "m", "model": "claude-opus-4-8", "usage": {"input_tokens": 1, "output_tokens": 1}}
                ),
                media_type="application/json",
            )


def _apps(*, serve="all", registry=None, upstream=None):
    """(app, registry, upstream): a proxy app whose upstream is the recording stub."""
    upstream = upstream or Upstream()
    registry = registry or TokenRegistry()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream.app), base_url="http://upstream.test")
    app = create_app(registry, real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET, http_client=http, serve=serve)
    return app, registry, upstream


def _token(registry: TokenRegistry) -> str:
    return registry.issue("build-1", token_budget=10**9, cost_budget_usd=10**6)


def _post(app, path, *, headers, content):
    """One POST through the in-process ASGI transport; `content` may be bytes or an async
    iterator (a streamed, chunked body whose consumption the test can count)."""

    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as client:
            return await client.post(path, headers=headers, content=content)

    return asyncio.run(go())


def _counted_stream(chunk: bytes, count: int):
    """An async body of `count` chunks, and the list that records each chunk the app pulled."""
    pulled: list[int] = []

    async def stream():
        for _ in range(count):
            pulled.append(1)
            yield chunk

    return stream(), pulled


# --- the request-body bound --------------------------------------------------------------------


def test_the_body_limit_is_pinned_at_4_mib():
    """A literal, so changing it is a deliberate edit. The comment above the constant says why:
    over three times what a 200,000-token conversation encodes to, and small enough that the
    worst-case parse of a hostile body fits the proxy container (it was 16 MiB, which did
    not). `tests/test_llm_proxy_request_surface.py` pins the memory arithmetic."""
    assert llm_proxy.MAX_REQUEST_BODY_BYTES == 4 * 1024 * 1024


def test_a_streamed_body_over_the_limit_is_refused_413_and_never_read_in_full(monkeypatch):
    """A 25 MiB chunked body against a 1 MiB limit. The proxy answers 413 having pulled about a
    MiB of it -- not 25. (Without the bound it reads every chunk, joins them, and forwards.)"""
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", 1 * MIB)
    app, registry, upstream = _apps()
    stream, pulled = _counted_stream(b"x" * (64 * KIB), 400)  # 25 MiB if it were all read

    response = _post(app, "/v1/messages", headers={"x-api-key": _token(registry)}, content=stream)

    assert response.status_code == 413
    assert response.text.startswith("REQUEST_TOO_LARGE")
    assert len(pulled) <= (1 * MIB) // (64 * KIB) + 2
    assert upstream.calls == []  # nothing was forwarded
    assert REAL_KEY not in response.text


def test_a_declared_content_length_over_the_limit_is_refused_without_reading_a_byte(monkeypatch):
    """The cheapest refusal: the client says how long the body is, and the proxy takes its word
    for the purpose of saying no. `receive()` is never called -- the app was not even asked for
    the body."""
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", 1 * MIB)
    app, registry, upstream = _apps()
    token = _token(registry)
    received: list[str] = []
    sent: list[dict] = []

    async def receive():
        received.append("receive() called")
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [(b"x-api-key", token.encode()), (b"content-length", str(2 * MIB).encode())],
        "client": ("172.20.0.5", 4000),
        "server": ("172.20.0.2", 8801),
    }

    asyncio.run(app(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert received == []
    assert upstream.calls == []


def test_a_body_exactly_at_the_limit_is_forwarded_and_one_byte_over_is_not(monkeypatch):
    """Both sides of the boundary around the same payload, so the 413 above is the limit and
    not a refusal of this body for some other reason."""
    payload = json.dumps({"model": "claude-opus-4-8", "system": "x" * 2000}).encode()
    app, registry, upstream = _apps()
    token = _token(registry)

    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", len(payload))
    at_limit = _post(app, "/v1/messages", headers={"x-api-key": token}, content=payload)
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", len(payload) - 1)
    over = _post(app, "/v1/messages", headers={"x-api-key": token}, content=payload)

    assert at_limit.status_code == 200
    assert over.status_code == 413
    assert upstream.calls == [("/v1/messages", len(payload))]  # the one that fit, whole


def test_the_limit_counts_a_chunked_body_with_no_declared_length_too(monkeypatch):
    payload = json.dumps({"model": "claude-opus-4-8", "system": "x" * 2000}).encode()
    app, registry, upstream = _apps()
    token = _token(registry)

    async def chunked(parts):
        for part in parts:
            yield part

    halves = [payload[: len(payload) // 2], payload[len(payload) // 2 :]]
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", len(payload))
    at_limit = _post(app, "/v1/messages", headers={"x-api-key": token}, content=chunked(halves))
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", len(payload) - 1)
    over = _post(app, "/v1/messages", headers={"x-api-key": token}, content=chunked(halves))

    assert at_limit.status_code == 200
    assert over.status_code == 413


def test_a_caller_without_a_live_token_gets_401_before_any_of_its_body_is_read(monkeypatch):
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", 1 * MIB)
    app, registry, upstream = _apps()
    stream, pulled = _counted_stream(b"x" * (64 * KIB), 400)

    unknown = _post(app, "/v1/messages", headers={"x-api-key": "not-a-token"}, content=stream)

    assert unknown.status_code == 401
    assert len(pulled) <= 1  # the transport may hand over one chunk; the proxy did not consume it


@pytest.mark.parametrize("env", [ANTHROPIC_ENV, OPENAI_ENV], ids=["anthropic", "openai"])
def test_the_limit_applies_on_both_dialects(monkeypatch, env):
    monkeypatch.setattr(llm_proxy, "MAX_REQUEST_BODY_BYTES", 64 * KIB)
    registry = TokenRegistry()
    app = proxy_server.build_app(env, serve="proxy", registry=registry)
    token = _token(registry)
    big = json.dumps({"model": "m", "pad": "x" * (128 * KIB)}).encode()

    response = _post(app, "/v1/messages" if env is ANTHROPIC_ENV else "/v1/responses", headers={"x-api-key": token}, content=big)

    assert response.status_code == 413


# --- the admin routes are not served on the sandbox-facing app -------------------------------------

ADMIN_ROUTES = [
    ("GET", "/_forge_admin/health"),
    ("POST", "/_forge_admin/tokens"),
    ("DELETE", "/_forge_admin/tokens/abc"),
    ("GET", "/_forge_admin/tokens/abc/usage"),
    ("GET", "/_forge_admin/tokens/abc/trace"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
@pytest.mark.parametrize("credential", ["admin secret", "live proxy token", "both", "nothing"])
def test_the_proxy_listener_serves_no_admin_route_whatever_the_caller_sends(method, path, credential):
    app, registry, upstream = _apps(serve="proxy")
    headers = {}
    if credential in ("admin secret", "both"):
        headers.update(ADMIN)
    if credential in ("live proxy token", "both"):
        headers["x-api-key"] = _token(registry)
    client = TestClient(app)

    response = client.request(method, path, headers=headers, json={"build_id": "b", "token_budget": 1, "cost_budget_usd": 1.0})

    # 404 where the catch-all takes the method; 405 for DELETE, which it never accepted on any path.
    # Either way: nothing answered as an admin route, and nothing was forwarded.
    assert response.status_code == (405 if method == "DELETE" else 404)
    assert upstream.calls == []  # an admin path is never forwarded upstream either


def test_a_proxy_listener_cannot_be_used_to_issue_a_token():
    """The consequence that matters, not just the status: with the admin secret in hand, a call
    on the proxy listener creates no token."""
    app, registry, _ = _apps(serve="proxy")
    client = TestClient(app)

    client.post("/_forge_admin/tokens", headers=ADMIN, json={"build_id": "b", "token_budget": 1, "cost_budget_usd": 1.0})

    assert registry._tokens == {}


def test_the_admin_listener_serves_the_control_plane_and_no_proxy_route():
    app, registry, upstream = _apps(serve="control")
    client = TestClient(app)
    token = client.post(
        "/_forge_admin/tokens", headers=ADMIN, json={"build_id": "b", "token_budget": 1000, "cost_budget_usd": 1.0}
    ).json()["token"]

    health = client.get("/_forge_admin/health")
    usage = client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN)
    messages = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": "m"})

    assert health.status_code == 200
    assert usage.status_code == 200 and usage.json()["tokens"] == 0
    assert messages.status_code == 404  # no catch-all here: nothing a sandbox sends is forwarded
    assert upstream.calls == []


def test_the_two_halves_share_one_registry_so_a_token_issued_on_one_works_on_the_other():
    registry = TokenRegistry()
    upstream = Upstream()
    admin_app, _, _ = _apps(serve="control", registry=registry, upstream=upstream)
    proxy_app, _, _ = _apps(serve="proxy", registry=registry, upstream=upstream)
    token = TestClient(admin_app).post(
        "/_forge_admin/tokens", headers=ADMIN, json={"build_id": "b", "token_budget": 1000, "cost_budget_usd": 1.0}
    ).json()["token"]

    response = TestClient(proxy_app).post("/v1/messages", headers={"x-api-key": token}, json={"model": "claude-opus-4-8"})

    assert response.status_code == 200
    assert upstream.calls == [("/v1/messages", len(b'{"model":"claude-opus-4-8"}'))]
    assert registry.usage_for(token).tokens == 2  # metered on the proxy half, readable through the registry


def test_an_unknown_serve_value_is_refused():
    with pytest.raises(ValueError, match="serve must be"):
        create_app(TokenRegistry(), real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET, serve="both")


def test_the_default_still_serves_everything_for_in_process_callers():
    app, registry, _ = _apps()  # serve="all"
    client = TestClient(app)

    assert client.get("/_forge_admin/health").status_code == 200
    assert client.post("/v1/messages", json={}).status_code == 401  # the catch-all is there too


# --- the admin secret is compared in constant time --------------------------------------------------


def test_the_admin_secret_is_compared_with_compare_digest_on_bytes(monkeypatch):
    calls: list[tuple] = []
    real = llm_proxy.secrets.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(llm_proxy.secrets, "compare_digest", spy)
    app, _, _ = _apps(serve="control")
    client = TestClient(app)

    right = client.get("/_forge_admin/tokens/abc/usage", headers=ADMIN)
    wrong = client.get("/_forge_admin/tokens/abc/usage", headers={"X-Forge-Admin-Secret": "wrong"})
    missing = client.get("/_forge_admin/tokens/abc/usage")

    assert right.status_code == 404  # authorised; the token is simply unknown
    assert wrong.status_code == 403 and missing.status_code == 403
    assert calls == [(bytes, bytes)] * 3


def test_a_non_ascii_admin_secret_header_is_a_403_not_a_500():
    """`secrets.compare_digest` on two `str`s raises TypeError for non-ASCII; the header is the
    attacker's, so that would be a 500 an attacker can provoke."""
    app, _, _ = _apps(serve="control")
    client = TestClient(app)

    response = client.get("/_forge_admin/tokens/abc/usage", headers=[(b"x-forge-admin-secret", "é".encode())])

    assert response.status_code == 403


# --- the entry point: two listeners, one registry ------------------------------------------------------


def _fake_uvicorn(monkeypatch):
    """A stand-in for the `uvicorn` module the entry point imports: records the two listeners."""
    record = SimpleNamespace(configs=[], runs=[], admin_started=threading.Event())

    class Config:
        def __init__(self, app, host, port, log_level):
            self.app, self.host, self.port = app, host, port
            record.configs.append(self)

    class Server:
        def __init__(self, config):
            self.config = config

        def run(self):
            record.admin_started.set()

    def run(app, host, port, log_level):
        record.runs.append(SimpleNamespace(app=app, host=host, port=port))

    monkeypatch.setattr(proxy_server, "uvicorn", SimpleNamespace(Config=Config, Server=Server, run=run))
    return record


def _env(monkeypatch, **values):
    for key in ("FORGE_PROXY_PORT", "FORGE_PROXY_ADMIN_PORT", "FORGE_PROXY_DIALECT", "FORGE_PROXY_OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FORGE_PROXY_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("FORGE_PROXY_ANTHROPIC_API_KEY", REAL_KEY)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_main_binds_the_admin_listener_to_loopback_and_the_proxy_listener_to_every_interface(monkeypatch):
    record = _fake_uvicorn(monkeypatch)
    _env(monkeypatch)

    proxy_server.main()

    (admin,) = record.configs
    (proxy,) = record.runs
    assert record.admin_started.wait(2)
    assert (admin.host, admin.port) == ("127.0.0.1", 8802)
    assert (proxy.host, proxy.port) == ("0.0.0.0", 8801)


def test_main_serves_admin_routes_on_the_admin_app_only_and_shares_one_registry(monkeypatch):
    record = _fake_uvicorn(monkeypatch)
    _env(monkeypatch)

    proxy_server.main()

    admin_client = TestClient(record.configs[0].app)
    proxy_client = TestClient(record.runs[0].app)
    assert admin_client.get("/_forge_admin/health").status_code == 200
    assert proxy_client.get("/_forge_admin/health").status_code == 404
    token = admin_client.post(
        "/_forge_admin/tokens", headers=ADMIN, json={"build_id": "b", "token_budget": 10, "cost_budget_usd": 1.0}
    ).json()["token"]
    # Same registry: the proxy half knows the token the admin half issued (it gets as far as
    # the endpoint policy, a 403, instead of the 401 for an unknown token).
    assert proxy_client.post("/v1/responses", headers={"x-api-key": token}, json={}).status_code == 403


def test_main_reads_both_ports_from_the_environment(monkeypatch):
    record = _fake_uvicorn(monkeypatch)
    _env(monkeypatch, FORGE_PROXY_PORT="9001", FORGE_PROXY_ADMIN_PORT="9002")

    proxy_server.main()

    assert (record.configs[0].port, record.runs[0].port) == (9002, 9001)


def test_main_refuses_one_port_for_both_listeners(monkeypatch):
    record = _fake_uvicorn(monkeypatch)
    _env(monkeypatch, FORGE_PROXY_PORT="8801", FORGE_PROXY_ADMIN_PORT="8801")

    with pytest.raises(SystemExit, match="must differ"):
        proxy_server.main()

    assert record.configs == [] and record.runs == []
