"""Unit tests for the LLM egress proxy (ADR 0014) against a stubbed
upstream Anthropic API — no real key, no network. Covers: token issuance
and revocation (401/403 on revoked/unknown tokens), real-key injection
(never forwarded from the client), budget enforcement (BUDGET_EXCEEDED),
usage accounting, and upstream-unreachable -> ENGINE_UNREACHABLE.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.services.llm_proxy import TokenRegistry, create_app

REAL_KEY = "sk-ant-REAL-KEY-never-leaves-the-proxy"
ADMIN_SECRET = "test-admin-secret"
# A model with a price row (a request naming one without a row is refused).
PRICED_MODEL = "claude-opus-4-8"


def _stub_upstream(
    *, model: str = "claude-opus-4-8", input_tokens=100, output_tokens=50, received: list | None = None
):
    """A minimal fake Anthropic API: it returns a Messages-API-shaped response with usage,
    echoes the received x-api-key back in a header, and appends every request's
    headers to `received`, so a test can assert what the proxy forwarded without relying on
    a response header, which the proxy's outbound allowlist no longer relays."""
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        received_key = request.headers.get("x-api-key")
        if received is not None:
            received.append(dict(request.headers))
        import json

        body = json.dumps(
            {
                "id": "msg_stub",
                "model": model,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            }
        )
        return Response(
            content=body,
            media_type="application/json",
            headers={"x-received-api-key": received_key or ""},
        )

    return app


def _make_client(upstream_app: FastAPI, *, real_api_key: str = REAL_KEY) -> tuple[TestClient, TokenRegistry]:
    registry = TokenRegistry()
    async_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app), base_url="http://upstream.test"
    )
    proxy_app = create_app(
        registry, real_api_key=real_api_key, admin_secret=ADMIN_SECRET, http_client=async_client
    )
    return TestClient(proxy_app), registry


def _issue_token(client: TestClient, *, token_budget=1000, cost_budget_usd=10.0) -> str:
    resp = client.post(
        "/_forge_admin/tokens",
        json={"build_id": "build-1", "token_budget": token_budget, "cost_budget_usd": cost_budget_usd},
        headers={"X-Forge-Admin-Secret": ADMIN_SECRET},
    )
    assert resp.status_code == 200
    return resp.json()["token"]


def test_health_endpoint():
    client, _ = _make_client(_stub_upstream())
    resp = client.get("/_forge_admin/health")
    assert resp.status_code == 200


def test_admin_endpoints_require_correct_secret():
    client, _ = _make_client(_stub_upstream())
    resp = client.post(
        "/_forge_admin/tokens",
        json={"build_id": "b1", "token_budget": 100, "cost_budget_usd": 1.0},
        headers={"X-Forge-Admin-Secret": "wrong-secret"},
    )
    assert resp.status_code == 403


def test_valid_token_is_forwarded_and_gets_response():
    client, _ = _make_client(_stub_upstream())
    token = _issue_token(client)

    resp = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": "claude-opus-4-8"})

    assert resp.status_code == 200


def test_real_key_is_swapped_in_never_the_client_token():
    received: list[dict] = []
    client, _ = _make_client(_stub_upstream(received=received))
    token = _issue_token(client)

    resp = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})

    assert resp.status_code == 200
    # What the upstream actually received, read from the stub (the echo header it also sends
    # is an upstream-only header the proxy does not relay, tested in test_llm_proxy_policy.py).
    assert [h["x-api-key"] for h in received] == [REAL_KEY]
    assert token not in json.dumps(received)


def test_missing_token_is_401():
    client, _ = _make_client(_stub_upstream())
    resp = client.post("/v1/messages", json={})
    assert resp.status_code == 401


def test_unknown_token_is_401():
    client, _ = _make_client(_stub_upstream())
    resp = client.post("/v1/messages", headers={"x-api-key": "not-a-real-token"}, json={})
    assert resp.status_code == 401


def test_revoked_token_is_rejected():
    client, _ = _make_client(_stub_upstream())
    token = _issue_token(client)

    revoke_resp = client.delete(
        f"/_forge_admin/tokens/{token}", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    )
    assert revoke_resp.status_code == 200

    resp = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})
    assert resp.status_code == 401


def test_usage_is_recorded_from_upstream_response():
    client, _ = _make_client(_stub_upstream(input_tokens=1000, output_tokens=500))
    token = _issue_token(client, token_budget=10_000)

    client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})

    usage_resp = client.get(
        f"/_forge_admin/tokens/{token}/usage", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    )
    usage = usage_resp.json()
    assert usage["tokens"] == 1500
    assert usage["cost_usd"] > 0
    assert usage["budget_exceeded"] is False


def test_budget_exceeded_rejects_further_calls():
    # opus pricing: (5+25)/1e6 -> 1000 in + 1000 out tokens costs well over
    # a tiny budget, and 2000 tokens exceeds a 100-token budget outright.
    client, _ = _make_client(_stub_upstream(input_tokens=1000, output_tokens=1000))
    token = _issue_token(client, token_budget=100, cost_budget_usd=10.0)

    first = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})
    assert first.status_code == 200  # first call always allowed; budget is checked pre-call

    second = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})
    assert second.status_code == 403
    assert second.text == "BUDGET_EXCEEDED"


def test_usage_endpoint_reports_budget_exceeded_after_spend():
    client, _ = _make_client(_stub_upstream(input_tokens=1000, output_tokens=1000))
    token = _issue_token(client, token_budget=100, cost_budget_usd=10.0)

    client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})

    usage_resp = client.get(
        f"/_forge_admin/tokens/{token}/usage", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    )
    assert usage_resp.json()["budget_exceeded"] is True


def test_usage_lookup_for_unknown_token_is_404():
    client, _ = _make_client(_stub_upstream())
    resp = client.get(
        "/_forge_admin/tokens/does-not-exist/usage", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    )
    assert resp.status_code == 404


def test_upstream_unreachable_returns_engine_unreachable():
    registry = TokenRegistry()

    class _RaisingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    async_client = httpx.AsyncClient(transport=_RaisingTransport(), base_url="http://upstream.test")
    proxy_app = create_app(
        registry, real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET, http_client=async_client
    )
    client = TestClient(proxy_app)
    token = _issue_token(client)

    resp = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": PRICED_MODEL})

    assert resp.status_code == 502
    assert resp.text == "ENGINE_UNREACHABLE"


def test_different_builds_get_independent_budgets():
    client, _ = _make_client(_stub_upstream(input_tokens=10, output_tokens=10))
    token_a = _issue_token(client, token_budget=15, cost_budget_usd=10.0)
    token_b = _issue_token(client, token_budget=1000, cost_budget_usd=10.0)

    client.post("/v1/messages", headers={"x-api-key": token_a}, json={"model": PRICED_MODEL})
    over_budget = client.post("/v1/messages", headers={"x-api-key": token_a}, json={"model": PRICED_MODEL})
    still_ok = client.post("/v1/messages", headers={"x-api-key": token_b}, json={"model": PRICED_MODEL})

    assert over_budget.status_code == 403
    assert still_ok.status_code == 200


# ---------------------------------------------------------------------------
# ADR 0029 §2: the proxy refuses any request that asks the provider to
# act outside the sandbox. An allowlist of shapes; a refused request is never
# forwarded, spends nothing, and is recorded against the token.
# ---------------------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402


def _counting_upstream(*, input_tokens=100, output_tokens=50):
    """A stub upstream that answers ANY path/method and records each call, so a
    test can assert a refused request never reached it."""
    app = FastAPI()
    calls: list[dict] = []

    @app.api_route("/{path:path}", methods=["POST", "GET"])
    async def anything(path: str, request: Request) -> Response:
        calls.append({"path": path, "body": await request.body()})
        body = json.dumps(
            {
                "id": "msg_stub",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            }
        )
        return Response(content=body, media_type="application/json")

    return app, calls


def _usage(client: TestClient, token: str) -> dict:
    return client.get(
        f"/_forge_admin/tokens/{token}/usage", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    ).json()


def _post(client: TestClient, token: str, body, *, path: str = "/v1/messages"):
    """`body` is sent verbatim if bytes, JSON-encoded otherwise."""
    if isinstance(body, bytes):
        return client.post(
            path, headers={"x-api-key": token, "content-type": "application/json"}, content=body
        )
    return client.post(path, headers={"x-api-key": token}, json=body)


_URL_IMAGE = {"type": "image", "source": {"type": "url", "url": "https://evil.example/?q=secret"}}
_URL_DOC = {"type": "document", "source": {"type": "url", "url": "https://evil.example/d.pdf"}}
_FILE_DOC = {"type": "document", "source": {"type": "file", "file_id": "file_1"}}

# (case id, request body, expected `detail`) -- status is always 403
_REFUSED_CASES = [
    (
        "server_tool_web_search",
        {"model": PRICED_MODEL, "tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        "tool policy: tool type 'web_search_20250305' is not allowed",
    ),
    (
        "invented_future_server_tool",
        {"model": PRICED_MODEL, "tools": [{"type": "quantum_fetch_20991231", "name": "qf"}]},
        "tool policy: tool type 'quantum_fetch_20991231' is not allowed",
    ),
    (
        "server_tool_after_allowed_ones",
        {"model": PRICED_MODEL, "tools": [{"name": "Bash"}, {"type": "custom", "name": "x"}, {"type": "web_fetch_20250910"}]},
        "tool policy: tool type 'web_fetch_20250910' is not allowed",
    ),
    (
        "type_is_null",
        {"model": PRICED_MODEL, "tools": [{"type": None, "name": "x"}]},
        "tool policy: tool type 'NoneType' is not allowed",
    ),
    (
        "type_is_not_a_string",
        {"model": PRICED_MODEL, "tools": [{"type": ["custom"], "name": "x"}]},
        "tool policy: tool type 'list' is not allowed",
    ),
    (
        "mcp_servers_present",
        {"model": PRICED_MODEL, "mcp_servers": [{"type": "url", "url": "https://evil.example", "name": "e"}]},
        "tool policy: request field 'mcp_servers' is not allowed",
    ),
    (
        "mcp_servers_present_but_empty",
        {"model": PRICED_MODEL, "mcp_servers": []},
        "tool policy: request field 'mcp_servers' is not allowed",
    ),
    (
        "container_present",
        {"model": PRICED_MODEL, "container": "container_abc"},
        "tool policy: request field 'container' is not allowed",
    ),
    (
        "tools_is_not_a_list",
        {"model": PRICED_MODEL, "tools": {"type": "web_search_20250305"}},
        "tool policy: 'tools' is not a list",
    ),
    (
        "a_tools_entry_is_not_an_object",
        {"model": PRICED_MODEL, "tools": ["web_search"]},
        "tool policy: a 'tools' entry is not an object",
    ),
    (
        "url_image_source",
        {"model": PRICED_MODEL, "messages": [{"role": "user", "content": [_URL_IMAGE]}]},
        "tool policy: content source type 'url' is not allowed",
    ),
    (
        "url_document_nested_in_a_tool_result",
        {
            "model": PRICED_MODEL,
            "messages": [
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [_URL_DOC]}]}
            ],
        },
        "tool policy: content source type 'url' is not allowed",
    ),
    (
        "file_document_source",
        {"model": PRICED_MODEL, "messages": [{"role": "user", "content": [_FILE_DOC]}]},
        "tool policy: content source type 'file' is not allowed",
    ),
]

_NOT_JSON = "tool policy: request body is not valid JSON"
_NOT_OBJECT = "tool policy: request body is not a JSON object"
_REFUSED_RAW_CASES = [
    ("not_json", b"this is not json", _NOT_JSON),
    ("truncated_json", b'{"model": PRICED_MODEL, "tools": [', _NOT_JSON),
    ("gzip_bytes", b"\x1f\x8b\x08\x00\x00\x00\x00\x00", _NOT_JSON),
    ("json_array", b'[{"type": "web_search_20250305"}]', _NOT_OBJECT),
    ("json_string", b'"tools"', _NOT_OBJECT),
    ("json_number", b"42", _NOT_OBJECT),
    (
        "duplicate_tools_key_hides_a_server_tool",
        b'{"tools": [{"type": "web_search_20250305"}], "tools": []}',
        "tool policy: request body repeats the key 'tools'",
    ),
]


@pytest.mark.parametrize("case_id,body,detail", _REFUSED_CASES, ids=[c[0] for c in _REFUSED_CASES])
def test_refused_shape_is_403_with_its_own_detail_and_never_forwarded(case_id, body, detail):
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    before = _usage(client, token)

    resp = _post(client, token, body)

    assert resp.status_code == 403
    assert resp.json()["detail"] == detail
    assert resp.json()["error"]["message"] == detail  # the agent sees the reason too
    assert calls == []  # never forwarded
    after = _usage(client, token)
    assert (after["tokens"], after["cost_usd"], after["budget_exceeded"]) == (
        before["tokens"],
        before["cost_usd"],
        before["budget_exceeded"],
    )
    assert after["policy_refusals"] == [detail]  # recorded against the token


@pytest.mark.parametrize("case_id,body,detail", _REFUSED_RAW_CASES, ids=[c[0] for c in _REFUSED_RAW_CASES])
def test_unreadable_or_non_object_body_is_refused_never_forwarded(case_id, body, detail):
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _post(client, token, body)

    assert resp.status_code == 403
    assert resp.json()["detail"] == detail
    assert calls == []
    after = _usage(client, token)
    assert after["tokens"] == 0 and after["cost_usd"] == 0.0
    assert after["policy_refusals"] == [detail]


def test_the_distinct_refusal_conditions_carry_distinct_details():
    """A test that could not tell two conditions apart could not tell which one
    fired. The 'NoneType'/'list' cases share the tool-type branch by design and
    differ only in what they name."""
    details = {c[2] for c in _REFUSED_CASES} | {c[2] for c in _REFUSED_RAW_CASES}
    # tool type (x4 named), mcp_servers, container, tools-not-list, entry-not-object,
    # url source, file source, invalid JSON, not-an-object, repeated key
    assert len(details) >= 14


_ALLOWED_CASES = [
    ("no_tools", {"model": PRICED_MODEL, "messages": [{"role": "user", "content": "hi"}]}),
    ("empty_tools", {"model": PRICED_MODEL, "tools": []}),
    ("no_type", {"model": PRICED_MODEL, "tools": [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}]}),
    (
        "no_type_and_custom",
        {
            "model": PRICED_MODEL,
            "tools": [
                {"name": "Read", "input_schema": {"type": "object"}},
                {"type": "custom", "name": "Edit", "input_schema": {"type": "object"}},
            ],
        },
    ),
    (
        "base64_image",
        {
            "model": PRICED_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
                    ],
                }
            ],
        },
    ),
    (
        "lookalike_data_inside_inputs",  # a schema property or a tool_use input that merely looks like a type/source
        {
            "model": PRICED_MODEL,
            "tools": [{"name": "Write", "input_schema": {"type": "object", "properties": {"source": {"type": "url"}}}}],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t",
                            "name": "Write",
                            "input": {
                                "source": {"type": "url", "url": "https://example.com"},
                                "type": "web_search_20250305",
                            },
                        }
                    ],
                }
            ],
        },
    ),
]


@pytest.mark.parametrize("case_id,body", _ALLOWED_CASES, ids=[c[0] for c in _ALLOWED_CASES])
def test_client_executed_shapes_are_forwarded_unchanged(case_id, body):
    upstream, calls = _counting_upstream(input_tokens=10, output_tokens=5)
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _post(client, token, body)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert json.loads(calls[0]["body"]) == body  # forwarded, not rewritten
    usage = _usage(client, token)
    assert usage["tokens"] == 15
    assert usage["policy_refusals"] == []


def test_a_bodyless_request_to_an_allowlisted_endpoint_is_forwarded():
    """No body means nothing for the body rule to inspect. (Was a bodyless GET
    /v1/models before the endpoint allowlist: that path is not measured CLI
    traffic and is now refused -- see the endpoint tests below.)"""
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = client.post("/v1/messages", headers={"x-api-key": token})

    assert resp.status_code == 200
    assert [c["path"] for c in calls] == ["v1/messages"]


def test_the_body_rule_applies_on_the_measured_query_form_too():
    """The CLI posts to `/v1/messages?beta=true`; a server tool there is refused."""
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _post(client, token, {"tools": [{"type": "web_search_20250305"}]}, path="/v1/messages?beta=true")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool policy: tool type 'web_search_20250305' is not allowed"
    assert calls == []


def test_a_refusal_leaves_later_allowed_requests_unchanged():
    """(d2) forwarding and accounting for an allowed request are the same
    after a refusal on the same token."""
    upstream, calls = _counting_upstream(input_tokens=1000, output_tokens=500)
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=10_000)

    _post(client, token, {"tools": [{"type": "web_search_20250305"}]})
    ok = _post(client, token, {"model": PRICED_MODEL})

    assert ok.status_code == 200
    assert len(calls) == 1
    usage = _usage(client, token)
    assert usage["tokens"] == 1500
    assert usage["policy_refusals"] == ["tool policy: tool type 'web_search_20250305' is not allowed"]


def test_an_unknown_token_is_still_401_before_the_rule_is_evaluated():
    """An unauthenticated caller learns nothing about the rule."""
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)

    resp = client.post(
        "/v1/messages", headers={"x-api-key": "not-a-token"}, json={"tools": [{"type": "web_search_20250305"}]}
    )

    assert resp.status_code == 401
    assert calls == []


def test_a_refusal_is_reported_even_when_the_budget_is_already_spent():
    """The rule runs ahead of the budget check: a boundary event is never
    masked as an ordinary BUDGET_EXCEEDED."""
    upstream, calls = _counting_upstream(input_tokens=1000, output_tokens=1000)
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=100)
    _post(client, token, {"model": PRICED_MODEL})  # spends the whole budget

    resp = _post(client, token, {"mcp_servers": []})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool policy: request field 'mcp_servers' is not allowed"
    assert len(calls) == 1


def test_recorded_refusals_are_capped():
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    for _i in range(30):
        _post(client, token, {"mcp_servers": []})

    assert len(_usage(client, token)["policy_refusals"]) == 20
    assert calls == []


# ---------------------------------------------------------------------------
# ADR 0029 §Amendment 2026-09-30: only the (method, path) pairs the
# pinned CLI is MEASURED to use are forwarded. Everything else is refused before
# the body is read, never forwarded, spends nothing, and is recorded.
# ---------------------------------------------------------------------------

# The gate's probe: a server tool + mcp_servers nested where the top-level body
# rule cannot see them. It returned 200 and reached upstream before (h).
_BATCH_PROBE = {
    "requests": [
        {
            "custom_id": "x",
            "params": {
                "model": PRICED_MODEL,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "mcp_servers": [{"type": "url", "url": "https://evil.example", "name": "e"}],
            },
        }
    ]
}

# (case id, method, path, query, expected `detail`)
_REFUSED_ENDPOINTS = [
    ("batches_probe", "POST", "/v1/messages/batches", "", "tool policy: endpoint POST /v1/messages/batches is not allowed"),
    ("files_upload", "POST", "/v1/files", "", "tool policy: endpoint POST /v1/files is not allowed"),
    ("get_on_an_unlisted_path", "GET", "/v1/models", "", "tool policy: endpoint GET /v1/models is not allowed"),
    ("get_on_the_allowed_path", "GET", "/v1/messages", "", "tool policy: endpoint GET /v1/messages is not allowed"),
    ("invented_path", "POST", "/v1/quantum", "", "tool policy: endpoint POST /v1/quantum is not allowed"),
    ("count_tokens", "POST", "/v1/messages/count_tokens", "", "tool policy: endpoint POST /v1/messages/count_tokens is not allowed"),
    ("trailing_slash", "POST", "/v1/messages/", "", "tool policy: endpoint POST /v1/messages/ is not allowed"),
    ("prefix_only", "POST", "/v1", "", "tool policy: endpoint POST /v1 is not allowed"),
    ("root", "POST", "/", "", "tool policy: endpoint POST / is not allowed"),
    ("unknown_admin_looking_path", "POST", "/_forge_admin/nope", "", "tool policy: endpoint POST /_forge_admin/nope is not allowed"),
    ("query_on_allowed_path", "POST", "/v1/messages", "beta=true&x=1", "tool policy: query string 'beta=true&x=1' is not allowed on POST /v1/messages"),
    ("unmeasured_query", "POST", "/v1/messages", "foo=bar", "tool policy: query string 'foo=bar' is not allowed on POST /v1/messages"),
]


@pytest.mark.parametrize(
    "case_id,method,path,query,detail", _REFUSED_ENDPOINTS, ids=[c[0] for c in _REFUSED_ENDPOINTS]
)
def test_unmeasured_endpoint_is_refused_with_its_own_detail_and_never_forwarded(case_id, method, path, query, detail):
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    before = _usage(client, token)
    url = path + (f"?{query}" if query else "")

    resp = client.request(method, url, headers={"x-api-key": token}, json={"model": PRICED_MODEL} if method == "POST" else None)

    assert resp.status_code == 403
    assert resp.json()["detail"] == detail
    assert calls == []  # never forwarded
    after = _usage(client, token)
    assert (after["tokens"], after["cost_usd"], after["budget_exceeded"]) == (
        before["tokens"],
        before["cost_usd"],
        before["budget_exceeded"],
    )
    assert after["policy_refusals"] == [detail]


def test_the_batches_probe_that_reached_upstream_is_now_refused():
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _post(client, token, _BATCH_PROBE, path="/v1/messages/batches")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool policy: endpoint POST /v1/messages/batches is not allowed"
    assert calls == []


def test_the_same_nested_payload_is_refused_by_the_body_rule_only_as_an_unknown_field():
    """The refusal above is the ENDPOINT rule. The body rule alone never read the nested payload
    (the `mcp_servers` and the server tool inside `requests[0].params`), which is the hole the
    endpoint amendment closed: it used to have nothing to object to at all. Now the
    top-level allowlist refuses the body, for the field `requests` and not for anything inside it:
    the endpoint rule is still what keeps this payload off the provider's Batches endpoint, and
    the allowlist is a second layer that does not depend on the path."""
    from app.services.llm_proxy import check_request_policy

    detail = check_request_policy(json.dumps({**_BATCH_PROBE, "model": PRICED_MODEL}).encode())

    assert detail == "tool policy: request field 'requests' is not allowed"


_ALLOWED_ENDPOINTS = [
    ("bare", "/v1/messages"),
    ("beta_true_as_the_cli_sends_it", "/v1/messages?beta=true"),
]


@pytest.mark.parametrize("case_id,url", _ALLOWED_ENDPOINTS, ids=[c[0] for c in _ALLOWED_ENDPOINTS])
def test_each_allowlisted_endpoint_still_forwards(case_id, url):
    upstream, calls = _counting_upstream(input_tokens=10, output_tokens=5)
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _post(client, token, {"model": PRICED_MODEL, "tools": [{"name": "Bash"}]}, path=url)

    assert resp.status_code == 200
    assert len(calls) == 1 and calls[0]["path"] == "v1/messages"
    usage = _usage(client, token)
    assert usage["tokens"] == 15 and usage["policy_refusals"] == []


def test_the_allowlist_is_exactly_the_measured_pair():
    """Pins the set literally: widening it is an explicit change (ADR 0029
    amendment), and must be re-measured against the capture."""
    from app.services.llm_proxy import ALLOWED_ENDPOINTS

    assert ALLOWED_ENDPOINTS == {("POST", "v1/messages"): frozenset({"", "beta=true"})}


def test_the_endpoint_rule_runs_before_the_body_rule_and_the_budget():
    """An unmeasured endpoint with an unreadable body reports the ENDPOINT, and
    a spent budget does not mask it."""
    upstream, calls = _counting_upstream(input_tokens=1000, output_tokens=1000)
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=100)
    _post(client, token, {"model": PRICED_MODEL})  # spends the whole budget

    resp = _post(client, token, b"not json", path="/v1/files")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool policy: endpoint POST /v1/files is not allowed"
    assert len(calls) == 1


def test_a_hostile_path_is_bounded_and_escaped_in_the_detail():
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = client.post("/v1/" + "a" * 500, headers={"x-api-key": token}, json={})

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert len(detail) < 200
    assert "..." in detail
    assert calls == []


def test_an_unknown_token_is_still_401_on_an_unmeasured_endpoint():
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)

    resp = client.post("/v1/files", headers={"x-api-key": "nope"}, json={})

    assert resp.status_code == 401 and calls == []


def test_the_admin_routes_are_unaffected():
    upstream, calls = _counting_upstream()
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    assert client.get("/_forge_admin/health").status_code == 200
    assert _usage(client, token)["policy_refusals"] == []
    assert client.delete(
        f"/_forge_admin/tokens/{token}", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    ).status_code == 200
    assert calls == []


# ---------------------------------------------------------------------------
# ADR 0014 §Amendment 2026-10-01: the proxy meters what the real engine
# spends. The pinned CLI streams (SSE); every real call used to be
# recorded as 0 tokens, so the pre-call budget check could never trip. These
# tests drive the proxy with SSE and JSON bodies and assert recorded tokens and
# cost EXACTLY (floats to a tolerance), never "> 0".
# ---------------------------------------------------------------------------


def _sse(events: list[dict], *, eol: str = "\n", with_event_lines: bool = True) -> bytes:
    """A `text/event-stream` body: each dict is one event, `type` doubling as
    the `event:` line the API sends."""
    chunks = []
    for event in events:
        head = f"event: {event['type']}{eol}" if with_event_lines else ""
        chunks.append(f"{head}data: {json.dumps(event, ensure_ascii=False)}{eol}{eol}")
    return "".join(chunks).encode("utf-8")


def _message_start(model="claude-opus-5-5", **usage) -> dict:
    return {
        "type": "message_start",
        "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "usage": usage},
    }


def _message_delta(**usage) -> dict:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": usage,
    }


_BLOCK = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
_STOP = {"type": "message_stop"}


def _recorded_sse_stream(model="claude-opus-5-5") -> list[dict]:
    """The shape the capture fake emits: 5 input / 1 output at the start,
    a cumulative output of 2 in the delta."""
    return [
        _message_start(model, input_tokens=5, output_tokens=1),
        _BLOCK,
        _message_delta(output_tokens=2),
        _STOP,
    ]


def _scripted_upstream(*responses):
    """A stub upstream answering `POST /v1/messages` with the given
    `(status, body_bytes, content_type)` in order (the last one repeats), and
    recording every call, so a test can assert a refused request never reached
    it."""
    app = FastAPI()
    calls: list[dict] = []

    @app.api_route("/{path:path}", methods=["POST", "GET"])
    async def anything(path: str, request: Request) -> Response:
        calls.append({"path": path})
        status, content, content_type = responses[min(len(calls), len(responses)) - 1]
        return Response(content=content, status_code=status, media_type=content_type)

    return app, calls


def _sse_response(events, **kw):
    return (200, _sse(events, **kw), "text/event-stream")


def _json_response(payload, status=200):
    return (status, json.dumps(payload).encode(), "application/json")


def _call(client: TestClient, token: str, url: str = "/v1/messages"):
    return client.post(url, headers={"x-api-key": token}, json={"model": PRICED_MODEL})


def _metered(responses, *, token_budget=1_000_000, cost_budget_usd=100.0):
    """One call through the proxy; returns the recorded usage."""
    upstream, calls = _scripted_upstream(*responses)
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=token_budget, cost_budget_usd=cost_budget_usd)
    assert _call(client, token).status_code == responses[0][0]
    assert len(calls) == 1
    return _usage(client, token)


def _assert_metered(usage: dict, *, tokens: int, cost_usd: float):
    assert usage["unmetered"] is False and usage["unmetered_reason"] is None
    assert usage["tokens"] == tokens
    assert usage["cost_usd"] == pytest.approx(cost_usd, rel=1e-9, abs=1e-12)


def test_sse_with_the_recorded_shape_meters_seven_tokens_not_eight():
    """message_delta's output_tokens (2) is CUMULATIVE: it supersedes
    message_start's (1). A naive sum gives 5 + 1 + 2 = 8."""
    usage = _metered([_sse_response(_recorded_sse_stream())])

    # claude-opus-5-5 is 4 / 20 USD per Mtok: (5 * 4 + 2 * 20) / 1e6.
    _assert_metered(usage, tokens=7, cost_usd=6.0e-5)


def test_sse_is_metered_on_the_cli_s_beta_query_form_too():
    upstream, calls = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    assert _call(client, token, "/v1/messages?beta=true").status_code == 200

    _assert_metered(_usage(client, token), tokens=7, cost_usd=6.0e-5)


def test_the_metered_response_reaches_the_client_unchanged():
    body = _sse(_recorded_sse_stream())
    upstream, _ = _scripted_upstream((200, body, "text/event-stream"))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    resp = _call(client, token)

    assert resp.content == body
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_a_later_message_delta_supersedes_input_and_cache_fields_too():
    """Cumulative means every field a delta carries replaces the start value;
    a field it omits keeps it."""
    stream = [
        _message_start(input_tokens=10, output_tokens=1, cache_read_input_tokens=100),
        _message_delta(input_tokens=30, output_tokens=7, cache_read_input_tokens=400),
        _message_delta(output_tokens=9),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    # input 30 (delta 1), output 9 (delta 2), cache read 400 (delta 1, kept).
    # opus-5-5: 30 * 4 + 9 * 20 + 400 * 0.20 = 380 -> per 1e6.
    _assert_metered(usage, tokens=39, cost_usd=3.8e-4)


def test_a_null_in_a_delta_does_not_erase_the_start_value():
    stream = [
        _message_start(input_tokens=10, output_tokens=1),
        _message_delta(input_tokens=None, output_tokens=4),
        _STOP,
    ]

    _assert_metered(_metered([_sse_response(stream)]), tokens=14, cost_usd=(10 * 4 + 4 * 20) / 1e6)


def test_sse_parsing_tolerates_crlf_no_event_lines_and_no_space_after_the_colon():
    body = _sse(_recorded_sse_stream(), eol="\r\n", with_event_lines=False).replace(b"data: ", b"data:")

    usage = _metered([(200, body, "text/event-stream")])

    _assert_metered(usage, tokens=7, cost_usd=6.0e-5)


def test_sse_parsing_does_not_split_on_a_unicode_line_separator_inside_json():
    """`str.splitlines()` would cut a data line at U+2028, lose the
    message_start and mark the build unmetered."""
    start = _message_start(input_tokens=5, output_tokens=1)
    start["message"]["note"] = "a b\x85c"
    stream = [start, _message_delta(output_tokens=2), _STOP]

    _assert_metered(_metered([_sse_response(stream)]), tokens=7, cost_usd=6.0e-5)


def test_a_multi_line_data_field_is_joined_before_parsing():
    start = json.dumps(_message_start(input_tokens=5, output_tokens=1), indent=1).splitlines()
    body = "event: message_start\n" + "".join(f"data: {line}\n" for line in start) + "\n"

    usage = _metered([(200, body.encode(), "text/event-stream")])

    _assert_metered(usage, tokens=6, cost_usd=(5 * 4 + 1 * 20) / 1e6)


def test_cache_reads_are_priced_at_the_models_cache_read_rate_and_are_not_tokens():
    stream = [
        _message_start(input_tokens=10, output_tokens=1, cache_read_input_tokens=1000),
        _message_delta(output_tokens=20),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    # opus-5-5 cache read is 0.20 USD/Mtok: 10 * 4 + 20 * 20 + 1000 * 0.20 = 640.
    # The 1000 re-read tokens are NOT counted: 10 + 20.
    _assert_metered(usage, tokens=30, cost_usd=6.4e-4)


def test_cache_creation_breakdown_is_priced_1_25x_for_5m_and_2x_for_1h():
    stream = [
        _message_start(
            input_tokens=10,
            output_tokens=1,
            cache_creation_input_tokens=1000,
            cache_creation={"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 400},
        ),
        _message_delta(output_tokens=20),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    # opus-5-5 input 4: 600 * 4 * 1.25 + 400 * 4 * 2 = 6200, plus 10 * 4 + 20 * 20 = 440.
    # Cache creation counts as tokens: 10 + 1000 + 20.
    _assert_metered(usage, tokens=1030, cost_usd=6.64e-3)


def test_cache_creation_without_a_breakdown_is_priced_at_2x():
    stream = [
        _message_start(input_tokens=10, output_tokens=1, cache_creation_input_tokens=1000),
        _message_delta(output_tokens=20),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    # 1000 * 4 * 2 = 8000, plus 440.
    _assert_metered(usage, tokens=1030, cost_usd=8.44e-3)


def test_cache_creation_the_breakdown_does_not_account_for_is_charged_at_2x():
    """A total larger than its parts: the 400 tokens the breakdown leaves
    unexplained take the dearer TTL, not the cheaper one."""
    stream = [
        _message_start(
            input_tokens=10,
            output_tokens=1,
            cache_creation_input_tokens=1000,
            cache_creation={"ephemeral_5m_input_tokens": 600, "ephemeral_1h_input_tokens": 0},
        ),
        _message_delta(output_tokens=20),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    # 600 * 4 * 1.25 + 400 * 4 * 2 = 6200, plus 440.
    _assert_metered(usage, tokens=1030, cost_usd=6.64e-3)


def test_a_json_response_prices_cache_fields_too():
    usage = _metered(
        [
            _json_response(
                {
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 1000,
                    },
                }
            )
        ]
    )

    # 40 + 400 + 1000 * 0.20 + 1000 * 4 * 2 = 8640; tokens 10 + 1000 + 20.
    _assert_metered(usage, tokens=1030, cost_usd=8.64e-3)


# (model id, USD per Mtok input / output / cache read) -- literals, the whole
# table, so a changed rate is a red test. Cost of 1000 input + 1000 output +
# 1000 cache-read tokens = (in + out + read) / 1000 USD.
_PRICE_ROWS = [
    ("claude-fable-5-1", 0.06025),
    ("claude-mythos-5-1", 0.06025),
    ("claude-fable-5", 0.061),
    ("claude-mythos-5", 0.061),
    ("claude-opus-5-5", 0.02420),
    ("claude-opus-5", 0.03050),
    ("claude-opus-4-8", 0.03050),
    ("claude-opus-4-7", 0.03050),
    ("claude-opus-4-6", 0.03050),
    ("claude-sonnet-5-5", 0.01220),
    ("claude-sonnet-5", 0.01220),
    ("claude-sonnet-4-6", 0.01830),
    ("claude-haiku-4-5", 0.00610),
]


@pytest.mark.parametrize("model,expected", _PRICE_ROWS, ids=[r[0] for r in _PRICE_ROWS])
def test_every_known_model_is_priced_from_its_own_row(model, expected):
    stream = [
        _message_start(model, input_tokens=1000, output_tokens=1000, cache_read_input_tokens=1000),
        _STOP,
    ]

    usage = _metered([_sse_response(stream)])

    _assert_metered(usage, tokens=2000, cost_usd=expected)


def test_claude_opus_5_5_has_its_own_row_not_the_opus_5_row():
    cheaper = _metered([_sse_response([_message_start("claude-opus-5-5", input_tokens=1000, output_tokens=1000)])])
    dearer = _metered([_sse_response([_message_start("claude-opus-5", input_tokens=1000, output_tokens=1000)])])

    _assert_metered(cheaper, tokens=2000, cost_usd=0.024)  # 4 + 20 per 1000 each
    _assert_metered(dearer, tokens=2000, cost_usd=0.030)  # 5 + 25


# ADR 0014 §Amendment 2026-10-17 point 2: a model with no price row is not priced at a
# "ceiling" (there is none): a request naming one is refused (test_llm_proxy_policy.py), and a
# 2xx response that names one is UNMETERABLE, so the token is marked and refused from then on.
_UNPRICED_MODELS = [
    "claude-invented-9",
    "claude-opus-5-5-x",  # a prefix match would price this as opus-5-5 (0.024)
    "claude-opus",
    "CLAUDE-OPUS-5-5",
    "claude-opus-5-5-2026",  # not a dated snapshot: four digits, not eight
    "claude-opus-5-5-202601011",  # nine digits
    "claude-opus-4-5-20251101",  # a dated snapshot of a model that has no row of its own
    "",
    None,
]


@pytest.mark.parametrize("model", _UNPRICED_MODELS)
def test_a_streamed_response_naming_an_unpriced_model_is_unmetered_not_priced_at_a_ceiling(model):
    start = _message_start(model, input_tokens=1000, output_tokens=1000)
    if model is None:
        del start["message"]["model"]
    upstream, calls = _scripted_upstream(_sse_response([start, _STOP]))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    first = _call(client, token)
    metered = _usage(client, token)
    second = _call(client, token)

    assert first.status_code == 200  # the call already happened; it is passed through
    assert metered["unmetered"] is True
    assert "has no price row" in metered["unmetered_reason"]
    assert (metered["tokens"], metered["cost_usd"]) == (0, 0.0)  # no figure was invented
    assert second.status_code == 403 and second.text.startswith("USAGE_UNMETERED: ")
    assert len(calls) == 1  # the unmetered token is never forwarded for again


@pytest.mark.parametrize("model", [m for m in _UNPRICED_MODELS if m is not None])
def test_a_json_response_naming_an_unpriced_model_is_unmetered_not_priced_at_a_ceiling(model):
    upstream, _ = _scripted_upstream(
        _json_response({"model": model, "usage": {"input_tokens": 1000, "output_tokens": 1000}})
    )
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    _call(client, token)
    metered = _usage(client, token)

    assert metered["unmetered"] is True and "has no price row" in metered["unmetered_reason"]
    assert (metered["tokens"], metered["cost_usd"]) == (0, 0.0)


def test_a_json_response_with_no_model_at_all_and_real_usage_is_unmetered():
    usage = _metered([_json_response({"usage": {"input_tokens": 1000, "output_tokens": 1000}})])

    assert usage["unmetered"] is True and "(none)" in usage["unmetered_reason"]


def test_a_response_that_billed_nothing_needs_no_price_row():
    """Zero tokens cost 0 at any price, so a usage with no model and no tokens (the provider's own
    `error` event carries none) is a measurement, not an unmeterable response."""
    usage = _metered([_sse_response([{"type": "error", "error": {"type": "overloaded_error", "message": "x"}}])])

    _assert_metered(usage, tokens=0, cost_usd=0.0)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5-20260301", 0.024),  # opus-5-5's row, 4 / 20: (1000*4 + 1000*20) / 1e6
        ("claude-haiku-4-5-20251001", 0.006),  # haiku-4-5's row, 1 / 5
        ("claude-sonnet-5-20260101", 0.012),  # sonnet-5's row, 2 / 10, NOT sonnet-5-5's
    ],
)
def test_a_dated_snapshot_of_a_priced_model_is_priced_at_its_base_row(model, expected):
    usage = _metered([_sse_response([_message_start(model, input_tokens=1000, output_tokens=1000), _STOP])])

    _assert_metered(usage, tokens=2000, cost_usd=expected)


def test_speed_fast_doubles_every_rate():
    normal = [_message_start(input_tokens=5, output_tokens=1, cache_read_input_tokens=1000), _message_delta(output_tokens=2), _STOP]
    fast = [
        _message_start(input_tokens=5, output_tokens=1, cache_read_input_tokens=1000, speed="fast"),
        _message_delta(output_tokens=2),
        _STOP,
    ]

    base = _metered([_sse_response(normal)])
    doubled = _metered([_sse_response(fast)])

    # 5 * 4 + 2 * 20 + 1000 * 0.20 = 260 per 1e6.
    _assert_metered(base, tokens=7, cost_usd=2.6e-4)
    _assert_metered(doubled, tokens=7, cost_usd=5.2e-4)


def test_speed_fast_on_a_json_response_doubles_it_too():
    usage = _metered(
        [
            _json_response(
                {"model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 1000, "speed": "fast"}}
            )
        ]
    )

    _assert_metered(usage, tokens=2000, cost_usd=0.048)


def test_a_json_response_is_still_metered_and_sonnet_5_is_two_and_ten_dollars():
    """The `claude-sonnet-5` row was 3/15, wrong against the list price (2/10).
    No earlier test pinned the old rate."""
    usage = _metered(
        [_json_response({"model": "claude-sonnet-5", "usage": {"input_tokens": 1000, "output_tokens": 1000}})]
    )

    _assert_metered(usage, tokens=2000, cost_usd=0.012)


# ---- an unmeterable 2xx fails closed -------------------------------------------

_UNMETERABLE = [
    (
        "json_without_usage",
        _json_response({"id": "msg_1", "model": "claude-opus-5-5"}),
        "response JSON has no usage object",
    ),
    (
        "json_usage_not_an_object",
        _json_response({"model": "claude-opus-5-5", "usage": 12}),
        "response JSON has no usage object",
    ),
    ("json_not_an_object", _json_response([{"usage": {"input_tokens": 1, "output_tokens": 1}}]), "response JSON is not an object"),
    (
        "sse_without_message_start",
        _sse_response([_BLOCK, _message_delta(output_tokens=2), _STOP]),
        "SSE stream has no message_start usage",
    ),
    (
        "sse_message_start_without_usage",
        _sse_response([{"type": "message_start", "message": {"model": "claude-opus-5-5"}}, _STOP]),
        "SSE stream has no message_start usage",
    ),
    ("sse_empty", (200, b"", "text/event-stream"), "SSE stream has no message_start usage"),
    ("undecodable_json", (200, b"\xff\xfe\x00 not json", "application/json"), "response body could not be decoded"),
    ("empty_json_body", (200, b"", "application/json"), "response body could not be decoded"),
    ("undecodable_sse", (200, b"data: \xff\xfe\n\n", "text/event-stream"), "response body could not be decoded"),
    (
        "usage_without_token_counts",
        _json_response({"model": "claude-opus-5-5", "usage": {}}),
        "usage field 'input_tokens' is missing",
    ),
    (
        "usage_count_is_not_an_integer",
        _json_response({"model": "claude-opus-5-5", "usage": {"input_tokens": "5", "output_tokens": 1}}),
        "usage field 'input_tokens' is not a non-negative integer",
    ),
    (
        "usage_count_is_negative",
        _json_response({"model": "claude-opus-5-5", "usage": {"input_tokens": 5, "output_tokens": -1}}),
        "usage field 'output_tokens' is not a non-negative integer",
    ),
    (
        "json_content_type_over_an_sse_body",
        (200, _sse(_recorded_sse_stream()), "application/json"),
        "response body could not be decoded",
    ),
]


@pytest.mark.parametrize(
    "case_id,response,reason", _UNMETERABLE, ids=[c[0] for c in _UNMETERABLE]
)
def test_an_unmeterable_success_marks_the_token_and_refuses_the_next_request(case_id, response, reason):
    upstream, calls = _scripted_upstream(response, _json_response({"usage": {"input_tokens": 1, "output_tokens": 1}}))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    first = _call(client, token)
    assert first.status_code == 200  # the call already happened; it is passed through
    after_first = _usage(client, token)
    assert (after_first["unmetered"], after_first["unmetered_reason"]) == (True, reason)
    assert (after_first["tokens"], after_first["cost_usd"], after_first["budget_exceeded"]) == (0, 0.0, False)

    second = _call(client, token)

    assert second.status_code == 403
    assert second.text == f"USAGE_UNMETERED: {reason}"
    assert second.text != "BUDGET_EXCEEDED"
    assert len(calls) == 1  # never forwarded
    assert _usage(client, token)["policy_refusals"] == []


def test_the_unmetered_reasons_are_distinct_per_condition():
    reasons = {
        "no_usage": "response JSON has no usage object",
        "not_an_object": "response JSON is not an object",
        "no_message_start": "SSE stream has no message_start usage",
        "undecodable": "response body could not be decoded",
    }
    assert len(set(reasons.values())) == 4
    assert set(reasons.values()) <= {c[2] for c in _UNMETERABLE}


def test_an_unmetered_token_stays_unmetered_after_the_first_reason():
    upstream, calls = _scripted_upstream(_json_response({"model": PRICED_MODEL}))
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    _call(client, token)

    for _ in range(3):
        assert _call(client, token).text == "USAGE_UNMETERED: response JSON has no usage object"

    assert len(calls) == 1
    assert _usage(client, token)["unmetered_reason"] == "response JSON has no usage object"


def test_an_unmetered_token_does_not_affect_another_build():
    upstream, calls = _scripted_upstream(
        _json_response({"model": PRICED_MODEL}),
        _json_response({"model": "claude-opus-5-5", "usage": {"input_tokens": 5, "output_tokens": 2}}),
    )
    client, _ = _make_client(upstream)
    bad = _issue_token(client)
    good = _issue_token(client)
    _call(client, bad)

    assert _call(client, good).status_code == 200
    assert _usage(client, good)["unmetered"] is False


def test_a_policy_refusal_still_outranks_the_unmetered_refusal():
    """The order stays: token, endpoint, body policy, then unmetered / budget.
    A boundary event is recorded as a policy refusal even on an unmetered
    token."""
    upstream, calls = _scripted_upstream(_json_response({"model": PRICED_MODEL}))
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    _call(client, token)

    resp = _post(client, token, {"model": PRICED_MODEL, "mcp_servers": []})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "tool policy: request field 'mcp_servers' is not allowed"
    assert _usage(client, token)["policy_refusals"] == [resp.json()["detail"]]
    assert len(calls) == 1


def test_an_sse_stream_with_a_message_start_then_an_error_event_is_metered_not_unmetered():
    stream = [
        _message_start(input_tokens=5, output_tokens=1),
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]

    usage = _metered([_sse_response(stream)])

    # No message_delta ever arrived: what the stream carried is what is metered.
    _assert_metered(usage, tokens=6, cost_usd=(5 * 4 + 1 * 20) / 1e6)


def test_a_2xx_without_a_body_is_unmetered():
    upstream, calls = _scripted_upstream((204, b"", "application/json"))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    _call(client, token)

    assert _usage(client, token)["unmetered"] is True


# ---- the guardrail: the budget trips through SSE ---------------------------------


def test_the_token_budget_trips_through_an_sse_response():
    """The reason this test exists: with a streamed response the counter used to
    stay at 0, so the second call was never refused."""
    upstream, calls = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=5, cost_budget_usd=100.0)

    first = _call(client, token)
    assert first.status_code == 200
    assert _usage(client, token)["tokens"] == 7
    assert _usage(client, token)["budget_exceeded"] is True

    second = _call(client, token)

    assert second.status_code == 403
    assert second.text == "BUDGET_EXCEEDED"
    assert len(calls) == 1  # zero further upstream calls


def test_the_cost_budget_trips_through_an_sse_response():
    upstream, calls = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=1_000_000, cost_budget_usd=5.0e-5)  # one call costs 6e-5

    assert _call(client, token).status_code == 200
    second = _call(client, token)

    assert second.status_code == 403 and second.text == "BUDGET_EXCEEDED"
    assert len(calls) == 1
    assert _usage(client, token)["budget_exceeded"] is True


def test_a_budget_not_yet_reached_through_sse_keeps_forwarding():
    upstream, calls = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client, token_budget=14, cost_budget_usd=100.0)  # 7 + 7 = 14

    assert _call(client, token).status_code == 200  # 7 < 14
    assert _call(client, token).status_code == 200  # forwarded; now 14 >= 14
    assert _call(client, token).status_code == 403
    assert len(calls) == 2


# ---- only a 2xx is metered ----------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 304, 307, 400, 401, 404, 429, 500, 529])
def test_a_non_2xx_response_is_not_accounted_and_does_not_unmeter(status):
    valid = {"model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 1000}}
    upstream, calls = _scripted_upstream(_json_response(valid, status=status))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    _call(client, token)

    usage = _usage(client, token)
    assert (usage["tokens"], usage["cost_usd"]) == (0, 0.0)
    assert usage["unmetered"] is False and usage["budget_exceeded"] is False


@pytest.mark.parametrize("status", [400, 500])
def test_a_non_2xx_with_an_unreadable_body_is_not_unmetered(status):
    upstream, _ = _scripted_upstream((status, b"upstream exploded", "text/plain"))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    _call(client, token)

    assert _usage(client, token)["unmetered"] is False
    assert _call(client, token).status_code == status  # still forwarded


def test_a_201_success_is_metered():
    valid = {"model": "claude-opus-5-5", "usage": {"input_tokens": 5, "output_tokens": 2}}
    usage = _metered([_json_response(valid, status=201)])

    _assert_metered(usage, tokens=7, cost_usd=6.0e-5)


def test_usage_across_calls_accumulates():
    upstream, _ = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    for _ in range(3):
        _call(client, token)

    _assert_metered(_usage(client, token), tokens=21, cost_usd=1.8e-4)


def test_the_usage_response_keeps_its_earlier_fields():
    upstream, _ = _scripted_upstream(_sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    _call(client, token)

    assert set(_usage(client, token)) == {
        "tokens",
        "cost_usd",
        "budget_exceeded",
        "policy_refusals",
        "unmetered",
        "unmetered_reason",
    }


# ---- (f2) a provider-reported error is measured zero; a garbage body is unmetered ----

_OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


@pytest.mark.parametrize(
    "case_id,events",
    [
        ("error_only", [_OVERLOADED]),
        ("ping_then_error", [{"type": "ping"}, _OVERLOADED]),
    ],
    ids=["error_only", "ping_then_error"],
)
def test_a_2xx_stream_whose_first_event_is_error_is_measured_zero_and_not_unmetered(case_id, events):
    """The provider's explicit report that nothing was generated (e.g.
    `overloaded_error`). Unmetered here would turn a transient upstream hiccup
    into a dead build with BUDGET_EXCEEDED."""
    upstream, calls = _scripted_upstream(_sse_response(events), _sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    assert _call(client, token).status_code == 200
    usage = _usage(client, token)
    _assert_metered(usage, tokens=0, cost_usd=0.0)
    assert usage["budget_exceeded"] is False

    second = _call(client, token)

    assert second.status_code == 200  # forwarded: the token is not unmetered
    assert len(calls) == 2
    _assert_metered(_usage(client, token), tokens=7, cost_usd=6.0e-5)


_NOT_A_PROVIDER_ERROR = [
    ("plain_text_under_sse_content_type", (200, b"upstream exploded", "text/event-stream")),
    ("json_error_body_under_sse_content_type", (200, b'{"type": "error", "error": {}}', "text/event-stream")),
    ("error_after_a_content_block_with_no_message_start", _sse_response([_BLOCK, _OVERLOADED])),
    ("error_event_with_invalid_json", (200, b"event: error\ndata: {not json\n\n", "text/event-stream")),
]


@pytest.mark.parametrize(
    "case_id,response", _NOT_A_PROVIDER_ERROR, ids=[c[0] for c in _NOT_A_PROVIDER_ERROR]
)
def test_a_body_that_is_not_a_provider_reported_error_stays_unmetered(case_id, response):
    """The distinction (f2) turns on: an `error` event FIRST is a reading; anything
    else the proxy cannot read is unmetered, and the next request is refused."""
    upstream, calls = _scripted_upstream(response, _sse_response(_recorded_sse_stream()))
    client, _ = _make_client(upstream)
    token = _issue_token(client)

    _call(client, token)

    assert _usage(client, token)["unmetered"] is True
    assert _call(client, token).status_code == 403
    assert len(calls) == 1


# --- an unmetered reason is never built from upstream or exception text ----------
#
# The reason is read by the build executor into `Build.failure_detail`, which every
# authenticated reader of the timeline sees. So it must be authored by the proxy
# (one of a closed set of literals), however hostile the upstream body or whatever
# a parsing library raises.

_HOSTILE_MARKER = "UPSTREAM-MARKER-5c1e"


@pytest.mark.parametrize(
    "response",
    [
        _json_response({_HOSTILE_MARKER: 1, "model": _HOSTILE_MARKER, "usage": _HOSTILE_MARKER}),
        _json_response({"model": _HOSTILE_MARKER, "usage": {"input_tokens": _HOSTILE_MARKER, "output_tokens": 1}}),
        (200, f"data: {{\"type\": \"{_HOSTILE_MARKER}\"}}\n\n".encode(), "text/event-stream"),
    ],
    ids=["marker_as_key_and_value", "marker_as_count", "marker_as_sse_type"],
)
def test_an_unmetered_reason_never_echoes_upstream_content(response):
    upstream, _ = _scripted_upstream(response)
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    _call(client, token)

    usage = _usage(client, token)

    assert usage["unmetered"] is True
    assert _HOSTILE_MARKER not in usage["unmetered_reason"]
    assert usage["unmetered_reason"] in {c[2] for c in _UNMETERABLE}  # one of the closed set


def test_an_unexpected_parsing_error_records_a_fixed_reason_not_its_text(monkeypatch):
    """The free-form case: whatever a library raises mid-parse is logged, and the
    recorded reason is the constant -- the exception's message never reaches it."""
    from app.services import llm_proxy

    def _explode(model, usage):
        raise RuntimeError(f"library says {_HOSTILE_MARKER}")

    monkeypatch.setattr(llm_proxy, "_price_usage", _explode)
    upstream, _ = _scripted_upstream(
        _json_response({"model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )
    client, _ = _make_client(upstream)
    token = _issue_token(client)
    _call(client, token)

    usage = _usage(client, token)

    assert usage["unmetered"] is True
    assert usage["unmetered_reason"] == "unexpected error while reading usage"
    assert _HOSTILE_MARKER not in usage["unmetered_reason"]
