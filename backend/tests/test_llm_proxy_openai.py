"""ADR 0035 §Decision 3-4: the `openai` dialect of the LLM proxy against a stub Responses
API -- endpoint allowlist, request policy, upstream auth, metering and fail-closed behaviour.

No Docker, no network, no real key. Expected values are literals: the refusal strings, the
header names, the prices and the arithmetic written out, never read back from the module.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import llm_proxy_openai
from tests.openai_proxy_support import (
    ADMIN,
    REAL_KEY,
    bearer,
    body,
    call_item,
    function_tool,
    json_reply,
    make,
    message_item,
    send,
    sse,
    sse_reply,
    usage,
    usage_obj,
)

# --- the endpoint allowlist ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/responses"),
        ("POST", "/v1/responses/compact"),
        ("POST", "/v1/responses/"),
        ("POST", "/v1/responses/resp_123"),
        ("GET", "/v1/responses/resp_123"),
        ("POST", "/v1/responses/resp_123/cancel"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/completions"),
        ("POST", "/v1/files"),
        ("POST", "/v1/conversations"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/responses/../files"),
        ("POST", "/responses"),
    ],
)
def test_an_endpoint_other_than_post_v1_responses_is_refused_and_never_forwarded(method, path):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, method=method, path=path)

    assert resp.status_code == 403
    assert resp.json()["error"]["message"].startswith("tool policy: endpoint ")
    assert "is not allowed" in resp.json()["error"]["message"]
    assert upstream.calls == []


@pytest.mark.parametrize("query", ["beta=true", "foo=bar", "stream=true", "a=1&b=2"])
def test_any_query_string_on_v1_responses_is_refused(query):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, path=f"/v1/responses?{query}")

    assert resp.status_code == 403
    assert "query string" in resp.json()["error"]["message"]
    assert upstream.calls == []


def test_the_allowed_shape_is_forwarded_to_v1_responses():
    client, token, _, upstream = make([json_reply([message_item("hello")])])

    resp = send(client, token)

    assert resp.status_code == 200
    assert [(m, p) for m, p, _, _ in upstream.calls] == [("POST", "/v1/responses")]


def test_the_endpoint_allowlist_is_exactly_one_pair_with_no_query():
    assert llm_proxy_openai.ALLOWED_ENDPOINTS == {("POST", "v1/responses"): frozenset({""})}


# --- the request policy: one test per refused shape ---------------------------------------------

_NON_FUNCTION_TOOL_TYPES = [
    "web_search",
    "web_search_2025_08_26",
    "web_search_preview",
    "web_search_preview_2025_03_11",
    "file_search",
    "code_interpreter",
    "image_generation",
    "mcp",
    "computer",
    "computer_use_preview",
    "programmatic_tool_calling",
    "shell",
    "tool_search",
    "custom",
    "namespace",
    "apply_patch",
    "local_shell",
    "some_tool_shipped_next_year",
]


@pytest.mark.parametrize("tool_type", _NON_FUNCTION_TOOL_TYPES)
def test_every_non_function_tool_type_is_refused(tool_type):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(tools=[function_tool(), {"type": tool_type}]))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == f"tool policy: tool type {tool_type!r} is not allowed"
    assert upstream.calls == []


@pytest.mark.parametrize(
    "tool",
    [{"name": "Read"}, {"type": None}, {"type": 7}, {"type": ["function"]}, {"type": "Function"}, {"type": "function "}],
    ids=["no_type", "null_type", "int_type", "list_type", "wrong_case", "trailing_space"],
)
def test_a_tool_without_the_exact_function_type_is_refused(tool):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(tools=[tool]))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"].startswith("tool policy: tool type ")
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("tools", "detail"),
    [
        ("web_search", "tool policy: 'tools' is not a list"),
        ({"type": "function"}, "tool policy: 'tools' is not a list"),
        (["function"], "tool policy: a 'tools' entry is not an object"),
        ([None], "tool policy: a 'tools' entry is not an object"),
    ],
)
def test_a_malformed_tools_field_is_refused(tools, detail):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(tools=tools))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == detail
    assert upstream.calls == []


@pytest.mark.parametrize("field", ["previous_response_id", "conversation", "background", "prompt"])
@pytest.mark.parametrize("value", ["resp_abc", None, "", False, {}], ids=["string", "null", "empty", "false", "object"])
def test_each_server_state_field_is_refused_by_presence(field, value):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(**{field: value}))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == f"tool policy: request field {field!r} is not allowed"
    assert upstream.calls == []


def test_an_include_of_only_the_allowlisted_value_is_forwarded():
    """the one value the runner sends (so a store=false second turn can carry its
    reasoning item back) passes, and the request reaches the upstream."""
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(include=["reasoning.encrypted_content"]))

    assert resp.status_code == 200
    assert len(upstream.calls) == 1


def test_a_request_with_no_include_is_forwarded():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(drop=("include",)))

    assert resp.status_code == 200
    assert len(upstream.calls) == 1


@pytest.mark.parametrize(
    "include",
    [
        ["web_search_call.action.sources"],
        ["file_search_call.results"],
        ["code_interpreter_call.outputs"],
        ["message.input_image.image_url"],
        ["message.output_text.logprobs"],
        ["computer_call_output.output.image_url"],
        ["some_value_shipped_next_year"],
        ["reasoning.encrypted_content", "file_search_call.results"],  # one bad value among good ones
        ["reasoning.encrypted_content ", "REASONING.ENCRYPTED_CONTENT", ""],  # near misses are not the value
    ],
    ids=repr,
)
def test_an_include_value_outside_the_allowlist_is_refused_and_never_forwarded(include):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(include=include))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"].startswith("tool policy: include value ")
    assert "is not allowed" in resp.json()["error"]["message"]
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("include", "detail"),
    [
        ("reasoning.encrypted_content", "tool policy: 'include' is not a list"),  # a bare string, not a list
        (None, "tool policy: 'include' is not a list"),  # present but null
        ({"reasoning.encrypted_content": True}, "tool policy: 'include' is not a list"),
        ([None], "tool policy: include value 'NoneType' is not allowed"),
        ([["reasoning.encrypted_content"]], "tool policy: include value 'list' is not allowed"),
        ([7], "tool policy: include value 'int' is not allowed"),
    ],
    ids=repr,
)
def test_an_include_that_is_not_a_list_of_strings_is_refused(include, detail):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(include=include))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == detail
    assert upstream.calls == []


def test_a_request_without_store_is_refused():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(drop=("store",)))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "tool policy: request field 'store' must be present and false"
    assert upstream.calls == []


@pytest.mark.parametrize("store", [True, None, "false", 0, 1, [], {}], ids=repr)
def test_a_store_that_is_not_the_boolean_false_is_refused(store):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(store=store))

    assert resp.status_code == 403
    assert "'store' must be present and false" in resp.json()["error"]["message"]
    assert upstream.calls == []


def test_a_request_without_service_tier_is_refused():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(drop=("service_tier",)))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "tool policy: request field 'service_tier' must be present and 'default'"
    assert upstream.calls == []


@pytest.mark.parametrize("tier", ["priority", "flex", "auto", "scale", "Default", "", None, 1])
def test_a_service_tier_other_than_default_is_refused(tier):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(service_tier=tier))

    assert resp.status_code == 403
    assert "'service_tier' must be present and 'default'" in resp.json()["error"]["message"]
    assert upstream.calls == []


def test_a_repeated_key_is_refused_because_two_parsers_could_disagree_on_it():
    client, token, _, upstream = make([json_reply()])
    # `tools` appears twice: a parser that keeps the first reads a hosted tool, one that keeps the last does not.
    raw = (
        b'{"model": "gpt-5-mini", "store": false, "service_tier": "default", '
        b'"tools": [{"type": "web_search"}], "tools": [{"type": "function", "name": "Read"}]}'
    )

    resp = send(client, token, raw=raw)

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "tool policy: request body repeats the key 'tools'"
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (b"[]", "tool policy: request body is not a JSON object"),
        (b'"a string"', "tool policy: request body is not a JSON object"),
        (b"7", "tool policy: request body is not a JSON object"),
        (b"null", "tool policy: request body is not a JSON object"),
        (b"not json", "tool policy: request body is not valid JSON"),
        (b"\x1f\x8b\x08\x00 gzip-looking", "tool policy: request body is not valid JSON"),
        (b"", "tool policy: request body is empty"),
    ],
    ids=["list", "string", "int", "null", "garbage", "compressed", "empty"],
)
def test_a_body_that_is_not_one_json_object_is_refused(raw, detail):
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, raw=raw)

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == detail
    assert upstream.calls == []


@pytest.mark.parametrize(
    "choice",
    [
        {"type": "web_search_preview"},
        {"type": "mcp", "server_label": "x"},
        {"type": "allowed_tools", "tools": []},
        {"name": "Read"},
        "auto",
        "required",
        "none",
        {"type": "function", "name": "Read"},
    ],
    ids=["web_search", "mcp", "allowed_tools", "nameless", "auto", "required", "none", "function"],
)
def test_a_tool_choice_is_refused_because_the_pinned_runner_does_not_send_one(choice):
    """`tool_choice` is not on the measured top-level allowlist, so it is refused whatever
    it holds (it used to be admitted when it named a function: the runner never sends one, and a
    hosted-tool choice is refused with it). The `type` check an edit that admits it would need is
    named in `OPENAI_ALLOWED_TOP_LEVEL_FIELDS`'s comment."""
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(tool_choice=choice))

    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "tool policy: request field 'tool_choice' is not allowed"
    assert upstream.calls == []


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": "https://attacker.example/x.png?leak=1"},
        {"type": "input_image", "image_url": "http://attacker.example/x.png"},
        {"type": "input_image", "file_id": "file-abc"},
        {"type": "input_file", "file_url": "https://attacker.example/x.pdf"},
        {"type": "input_file", "file_id": "file-abc"},
    ],
    ids=["https_image", "http_image", "image_file_id", "file_url", "file_file_id"],
)
@pytest.mark.parametrize("where", ["message", "tool_output"])
def test_an_input_part_that_makes_the_provider_fetch_or_open_a_file_is_refused(part, where):
    client, token, _, upstream = make([json_reply()])
    item = (
        {"role": "user", "content": [part]}
        if where == "message"
        else {"type": "function_call_output", "call_id": "c1", "output": [part]}
    )

    resp = send(client, token, body(input=[item]))

    assert resp.status_code == 403
    assert "may not reference a URL or a file" in resp.json()["error"]["message"]
    assert upstream.calls == []


def test_an_inline_data_image_and_plain_text_parts_are_forwarded():
    client, token, _, upstream = make([json_reply()])
    parts = [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "input_file", "filename": "a.txt", "file_data": "data:text/plain;base64,QQ=="},
    ]

    resp = send(client, token, body(input=[{"role": "user", "content": parts}]))

    assert resp.status_code == 200
    assert len(upstream.calls) == 1


def test_a_refusal_has_the_openai_envelope_and_the_policy_prefix():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, body(tools=[{"type": "web_search"}]))

    assert resp.status_code == 403
    payload = resp.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"message", "type", "code"}
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == "forge_policy_refusal"
    assert payload["error"]["message"] == "tool policy: tool type 'web_search' is not allowed"
    assert upstream.calls == []


def test_a_refusal_is_recorded_against_the_token_and_spends_nothing():
    client, token, _, _ = make([json_reply()])

    send(client, token, body(tools=[{"type": "web_search"}]))
    send(client, token, body(store=True))

    metered = usage(client, token)
    assert metered["policy_refusals"] == [
        "tool policy: tool type 'web_search' is not allowed",
        "tool policy: request field 'store' must be present and false",
    ]
    assert (metered["tokens"], metered["cost_usd"], metered["unmetered"]) == (0, 0.0, False)


def test_a_refusal_is_not_masked_by_an_exhausted_budget():
    client, token, _, upstream = make([json_reply(usage=usage_obj(1000, 1000))], token_budget=100)
    assert send(client, token).status_code == 200  # spends the budget

    refused = send(client, token, body(tools=[{"type": "web_search"}]))
    plain = send(client, token)

    assert refused.status_code == 403 and "tool policy:" in refused.json()["error"]["message"]
    assert (plain.status_code, plain.text) == (403, "BUDGET_EXCEEDED")
    assert len(upstream.calls) == 1


# --- upstream auth ------------------------------------------------------------------------------


def test_the_real_key_goes_upstream_as_a_bearer_token_and_the_proxy_token_never_does():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, headers={"x-api-key": token})  # a client sending the token both ways

    assert resp.status_code == 200
    _, _, headers, forwarded_body = upstream.calls[0]
    assert headers["authorization"] == f"Bearer {REAL_KEY}"
    assert "x-api-key" not in headers
    assert token not in json.dumps(headers)
    assert token.encode() not in forwarded_body
    assert len(upstream.calls) == 1


def test_an_x_api_key_credential_is_also_replaced_not_forwarded():
    client, token, _, upstream = make([json_reply()])

    resp = client.post("/v1/responses", headers={"x-api-key": token}, json=body())

    assert resp.status_code == 200
    _, _, headers, _ = upstream.calls[0]
    assert headers["authorization"] == f"Bearer {REAL_KEY}"
    assert "x-api-key" not in headers


def test_a_missing_or_unknown_or_revoked_token_is_401_and_nothing_is_forwarded():
    client, token, _, upstream = make([json_reply()])

    assert client.post("/v1/responses", json=body()).status_code == 401
    assert client.post("/v1/responses", headers=bearer("not-a-real-token"), json=body()).status_code == 401
    assert client.delete(f"/_forge_admin/tokens/{token}", headers=ADMIN).status_code == 200
    assert send(client, token).status_code == 401
    assert upstream.calls == []


def test_the_real_key_is_not_in_any_response_to_the_sandbox():
    client, token, _, _ = make([json_reply([message_item("hi")])])

    resp = send(client, token)

    assert REAL_KEY not in resp.text
    assert REAL_KEY not in json.dumps(dict(resp.headers))


# --- metering: JSON ----------------------------------------------------------------------------


def test_json_cost_is_uncached_input_plus_cached_plus_output_and_tokens_exclude_cached_reads():
    # gpt-5-mini 0.25 / 0.025 / 2.00. input 1000 of which 400 cached -> 600 uncached; output 500
    # of which 200 is reasoning (inside output_tokens, so it is not added again).
    reply = json_reply(
        model="gpt-5-mini", usage=usage_obj(1000, 500, cached=400, written=50, reasoning=200), tier="default"
    )
    client, token, _, _ = make([reply])

    send(client, token)

    metered = usage(client, token)
    assert metered["tokens"] == 1100  # 600 + 500
    assert metered["cost_usd"] == pytest.approx(0.00116)  # (600*0.25 + 400*0.025 + 500*2.00) / 1e6
    assert metered["unmetered"] is False


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("default", 0.00116),
        ("priority", 0.00232),
        ("fast", 0.00232),
        ("flex", 0.00058),
        ("scale", 0.00232),  # any other tier at the dearest multiplier
        ("", 0.00232),
        (None, 0.00232),  # no tier reported: nothing to vouch for the cheap one
    ],
)
def test_the_tier_the_response_reports_scales_the_cost(tier, expected):
    client, token, _, _ = make([json_reply(model="gpt-5-mini", usage=usage_obj(1000, 500, cached=400), tier=tier)])

    send(client, token)

    metered = usage(client, token)
    assert metered["cost_usd"] == pytest.approx(expected)
    assert metered["tokens"] == 1100  # the tier never changes the token count


def test_the_tier_that_counts_is_the_responses_not_the_requests():
    client, token, _, _ = make([json_reply(model="gpt-5-mini", usage=usage_obj(1000, 500, cached=400), tier="priority")])

    send(client, token, body(service_tier="default"))  # asked for default, billed priority

    assert usage(client, token)["cost_usd"] == pytest.approx(0.00232)


@pytest.mark.parametrize(
    ("model", "in_rate", "cached_rate", "out_rate"),
    [
        ("gpt-5-nano", 0.05, 0.005, 0.40),
        ("gpt-5-mini", 0.25, 0.025, 2.00),
        ("gpt-5", 1.25, 0.125, 10.00),
        ("gpt-5.1", 1.25, 0.125, 10.00),
        ("gpt-5.4-mini", 0.75, 0.075, 4.50),
        ("gpt-5.3-codex", 1.75, 0.175, 14.00),
    ],
)
def test_the_price_table_rows_are_pinned(model, in_rate, cached_rate, out_rate):
    # One million of each component in turn, so each rate is read off the cost alone.
    for reply, expected in [
        (json_reply(model=model, usage=usage_obj(1_000_000, 0)), in_rate),
        (json_reply(model=model, usage=usage_obj(1_000_000, 0, cached=1_000_000)), cached_rate),
        (json_reply(model=model, usage=usage_obj(0, 1_000_000)), out_rate),
    ]:
        client, token, _, _ = make([reply])
        send(client, token)
        assert usage(client, token)["cost_usd"] == pytest.approx(expected)


def test_a_dated_snapshot_of_a_priced_model_is_priced_as_that_model():
    # gpt-5 1.25 / 0.125 / 10.00 -- its base row, not a dearer or a cheaper one.
    client, token, _, _ = make([json_reply(model="gpt-5-2025-08-07", usage=usage_obj(1000, 100))])

    send(client, token)

    assert usage(client, token)["cost_usd"] == pytest.approx(0.00225)  # (1000*1.25 + 100*10.00) / 1e6


@pytest.mark.parametrize(
    "model", ["gpt-9", "gpt-5-mini-high", "gpt-5-mini-2025-08", "gpt-5.2", "gpt-5-mini-2025-08-07-x", "GPT-5", None, 7]
)
def test_a_response_naming_an_unpriced_model_is_unmetered_not_priced_at_a_ceiling(model):
    # ADR 0014 §Amendment 2026-10-17 point 2: there is no ceiling row. The model has no
    # price, so the response cannot be metered: the token is marked unmetered (fail closed) and
    # no figure is invented. (A REQUEST naming such a model never gets this far: it is refused,
    # see test_llm_proxy_policy.py.)
    client, token, _, upstream = make([json_reply(model=model, usage=usage_obj(1000, 500, cached=400))])

    first = send(client, token)
    metered = usage(client, token)
    second = send(client, token)

    assert first.status_code == 200  # the call already happened
    assert metered["unmetered"] is True
    assert "has no price row" in metered["unmetered_reason"]
    assert (metered["tokens"], metered["cost_usd"]) == (0, 0.0)
    assert second.status_code == 403 and second.text.startswith("USAGE_UNMETERED: ")
    assert len(upstream.calls) == 1


def test_a_response_that_billed_nothing_needs_no_price_row():
    client, token, _, _ = make([json_reply(model=None, usage=usage_obj(0, 0))])

    send(client, token)

    metered = usage(client, token)
    assert (metered["unmetered"], metered["tokens"], metered["cost_usd"]) == (False, 0, 0.0)


def test_a_budget_that_the_cost_exhausts_refuses_the_next_call():
    client, token, _, upstream = make(
        [json_reply(model="gpt-5.3-codex", usage=usage_obj(0, 1000))], cost_budget_usd=0.01
    )

    first = send(client, token)  # 1000 * 14.00 / 1e6 = 0.014 > 0.01
    second = send(client, token)

    assert first.status_code == 200
    assert (second.status_code, second.text) == (403, "BUDGET_EXCEEDED")
    assert usage(client, token)["budget_exceeded"] is True
    assert len(upstream.calls) == 1


def test_a_budget_that_the_token_count_exhausts_refuses_the_next_call():
    client, token, _, upstream = make([json_reply(usage=usage_obj(1000, 1000, cached=900))], token_budget=1000)

    first = send(client, token)  # counted: 100 uncached + 1000 output = 1100 (cached reads do not count)
    second = send(client, token)

    assert first.status_code == 200
    assert (second.status_code, second.text) == (403, "BUDGET_EXCEEDED")
    assert len(upstream.calls) == 1


def test_cached_reads_alone_do_not_spend_the_token_budget():
    client, token, _, _ = make([json_reply(usage=usage_obj(10_000, 0, cached=10_000))], token_budget=100)

    first = send(client, token)
    second = send(client, token)

    assert (first.status_code, second.status_code) == (200, 200)
    assert usage(client, token)["tokens"] == 0


def test_an_http_error_from_upstream_is_passed_through_and_not_metered():
    error = (429, json.dumps({"error": {"message": "slow down", "type": "rate_limit"}}).encode(), "application/json")
    client, token, _, _ = make([error])

    resp = send(client, token)

    assert resp.status_code == 429
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"]) == (0, False)


def test_an_unreachable_upstream_is_engine_unreachable():
    class _Raising(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    client, token, _, _ = make([], http_client=httpx.AsyncClient(transport=_Raising(), base_url="http://upstream.test"))

    resp = send(client, token)

    assert (resp.status_code, resp.text) == (502, "ENGINE_UNREACHABLE")


# --- metering: SSE -----------------------------------------------------------------------------


def test_sse_completed_is_metered_from_the_terminal_event():
    # gpt-5-mini snapshot: input 2000 of which 1000 cached -> 1000 uncached; output 300.
    reply = sse_reply(
        [message_item("hi")], terminal="response.completed", model="gpt-5-mini-2025-08-07",
        usage=usage_obj(2000, 300, cached=1000), tier="default",
    )
    client, token, _, _ = make([reply])

    send(client, token, body())

    metered = usage(client, token)
    assert metered["tokens"] == 1300
    assert metered["cost_usd"] == pytest.approx(0.000875)  # (1000*0.25 + 1000*0.025 + 300*2.00) / 1e6
    assert metered["unmetered"] is False


def test_sse_incomplete_is_metered_at_its_own_tier():
    # gpt-5-nano 0.05 / 0.005 / 0.40, flex (0.5x): (100*0.05 + 50*0.40) = 25 -> 12.5 / 1e6.
    reply = sse_reply(
        [], terminal="response.incomplete", model="gpt-5-nano", usage=usage_obj(100, 50), tier="flex",
        status="incomplete", reason="max_output_tokens",
    )
    client, token, _, _ = make([reply])

    send(client, token, body())

    metered = usage(client, token)
    assert metered["tokens"] == 150
    assert metered["cost_usd"] == pytest.approx(0.0000125)


def test_sse_failed_with_usage_is_metered():
    # gpt-5.1 1.25 input: 500 input tokens, no output -> 625 / 1e6.
    reply = sse_reply([], terminal="response.failed", model="gpt-5.1", usage=usage_obj(500, 0), status="failed")
    client, token, _, _ = make([reply])

    send(client, token, body())

    metered = usage(client, token)
    assert metered["tokens"] == 500
    assert metered["cost_usd"] == pytest.approx(0.000625)
    assert metered["unmetered"] is False


def test_sse_priority_tier_is_read_from_the_terminal_response():
    reply = sse_reply([], model="gpt-5-mini", usage=usage_obj(1000, 500, cached=400), tier="priority")
    client, token, _, _ = make([reply])

    send(client, token, body())

    assert usage(client, token)["cost_usd"] == pytest.approx(0.00232)


def test_the_created_event_usage_is_not_what_is_metered():
    # `response.created` carries no usage (null); only the terminal event's figure counts.
    client, token, _, _ = make([sse_reply([message_item("x")], usage=usage_obj(10, 5))])

    send(client, token, body())

    assert usage(client, token)["tokens"] == 15


# --- an unmeterable 2xx fails closed -----------------------------------------------------------

_HOSTILE = "HOSTILE-UPSTREAM-TEXT-do-not-echo"

_UNMETERABLE = [
    (json_reply(usage=None), "response has no usage object"),
    ((200, b'{"model": "gpt-5-mini", "output": []}', "application/json"), "response has no usage object"),
    ((200, b'{"usage": "lots"}', "application/json"), "response has no usage object"),
    ((200, b'{"usage": {"output_tokens": 5}}', "application/json"), "usage field 'input_tokens' is missing"),
    ((200, b'{"usage": {"input_tokens": 5}}', "application/json"), "usage field 'output_tokens' is missing"),
    ((200, b'{"usage": {"input_tokens": -1, "output_tokens": 5}}', "application/json"), "usage field 'input_tokens' is not a non-negative integer"),
    ((200, b'{"usage": {"input_tokens": true, "output_tokens": 5}}', "application/json"), "usage field 'input_tokens' is not a non-negative integer"),
    ((200, b'{"usage": {"input_tokens": 1.5, "output_tokens": 5}}', "application/json"), "usage field 'input_tokens' is not a non-negative integer"),
    ((200, b'{"usage": {"input_tokens": "9", "output_tokens": 5}}', "application/json"), "usage field 'input_tokens' is not a non-negative integer"),
    (
        (200, b'{"usage": {"input_tokens": 5, "output_tokens": 5, "input_tokens_details": {"cached_tokens": "x"}}}', "application/json"),
        "usage field 'cached_tokens' is not a non-negative integer",
    ),
    (
        (200, b'{"usage": {"input_tokens": 5, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 6}}}', "application/json"),
        "usage field 'cached_tokens' exceeds 'input_tokens'",
    ),
    ((200, b"not json at all", "application/json"), "response body could not be decoded"),
    ((200, b"[1, 2]", "application/json"), "response JSON is not an object"),
    ((200, b"\xff\xfe", "text/event-stream"), "response body could not be decoded"),
    ((200, b"", "text/event-stream"), "SSE stream has no terminal response event"),
    (
        (200, sse([{"type": "response.created", "response": {"model": "gpt-5-mini"}}]), "text/event-stream"),
        "SSE stream has no terminal response event",
    ),
    (  # truncated: output arrived, the terminal event never did
        (200, sse([{"type": "response.output_text.delta", "delta": "hi"}]), "text/event-stream"),
        "SSE stream has no terminal response event",
    ),
    (  # response.failed that reports no usage: nothing to vouch for
        (200, sse([{"type": "response.failed", "response": {"model": "gpt-5-mini", "usage": None}}]), "text/event-stream"),
        "response has no usage object",
    ),
    (  # an `error` event is not a carve-out
        (200, sse([{"type": "error", "message": "boom"}]), "text/event-stream"),
        "SSE stream has no terminal response event",
    ),
    ((200, b'{"usage": {"input_tokens": 5, "output_tokens": 5}}', "text/event-stream"), "SSE stream has no terminal response event"),
]


@pytest.mark.parametrize(("reply", "reason"), _UNMETERABLE, ids=[r for _, r in _UNMETERABLE])
def test_an_unmeterable_2xx_marks_the_token_unmetered_and_refuses_every_later_request(reply, reason):
    client, token, _, upstream = make([reply, json_reply()])

    first = send(client, token)
    second = send(client, token)

    assert first.status_code == 200  # the response itself is still returned
    metered = usage(client, token)
    assert metered["unmetered"] is True
    assert metered["unmetered_reason"] == reason
    assert (metered["tokens"], metered["cost_usd"]) == (0, 0.0)  # nothing is invented
    assert second.status_code == 403
    assert second.text == f"USAGE_UNMETERED: {reason}"
    assert len(upstream.calls) == 1  # the second request was never forwarded


@pytest.mark.parametrize(
    "reply",
    [
        (200, json.dumps({_HOSTILE: 1, "model": _HOSTILE, "usage": _HOSTILE}).encode(), "application/json"),
        (200, json.dumps({"model": _HOSTILE, "usage": {"input_tokens": _HOSTILE, "output_tokens": 1}}).encode(), "application/json"),
        (200, sse([{"type": _HOSTILE}]), "text/event-stream"),
        (200, sse([{"type": "response.completed", "response": {"model": _HOSTILE, "usage": {"input_tokens": _HOSTILE}}}]), "text/event-stream"),
    ],
    ids=["marker_as_key_and_value", "marker_as_count", "marker_as_sse_type", "marker_in_terminal"],
)
def test_an_unmetered_reason_never_echoes_upstream_content(reply):
    client, token, _, _ = make([reply])

    send(client, token)

    metered = usage(client, token)
    assert metered["unmetered"] is True
    assert _HOSTILE not in metered["unmetered_reason"]


def test_an_unexpected_error_while_pricing_records_a_fixed_reason_not_its_text(monkeypatch):
    def _explode(model, tier, usage_):
        raise RuntimeError(f"library says {_HOSTILE}")

    monkeypatch.setattr(llm_proxy_openai, "_price_usage", _explode)
    client, token, _, _ = make([json_reply()])

    send(client, token)

    metered = usage(client, token)
    assert metered["unmetered"] is True
    assert metered["unmetered_reason"] == "unexpected error while reading usage"
    assert _HOSTILE not in metered["unmetered_reason"]


def test_a_stream_is_not_metered_twice_by_a_second_terminal_event():
    # Only the first terminal response counts, as a stream carries one.
    final = sse(
        [
            {"type": "response.completed", "response": {"model": "gpt-5-mini", "usage": usage_obj(10, 5)}},
            {"type": "response.completed", "response": {"model": "gpt-5-mini", "usage": usage_obj(1000, 500)}},
        ]
    )
    client, token, _, _ = make([(200, final, "text/event-stream")])

    send(client, token, body())

    assert usage(client, token)["tokens"] == 15


def test_a_function_call_response_is_metered_like_any_other():
    client, token, _, _ = make([json_reply([call_item("Read", {"path": "/workspace/a.py"})], usage=usage_obj(100, 20))])

    send(client, token)

    assert usage(client, token)["tokens"] == 120
