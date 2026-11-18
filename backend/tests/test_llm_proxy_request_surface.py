"""What the proxy accepts of a request, for
BOTH dialects.

2. The top-level fields of a request body are an ALLOWLIST of what each pinned engine was measured to
   send (`ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS`, `OPENAI_ALLOWED_TOP_LEVEL_FIELDS`). An unknown field
   is a recorded policy refusal before the forward, spends nothing and is never sent. It replaced a
   denylist (`mcp_servers`, `container`; and four state fields on the OpenAI side) that forwarded any
   field it had not heard of. The measurements are re-derived by the engine-up tests
   (`test_agent_tool_capture_integration.py`, `test_openai_sdk_wire_integration.py`); THIS file needs
   no Docker.
3. A body's parsed size is bounded. `json.loads` expands a hostile body up to ~33x, so the proxy's
   body cap is the number that decides whether one request can exhaust the container. The tests pin
   the cap, pin the arithmetic that fits it under the container's memory cap, measure the parse's
   expansion on the real policy functions at a small scale, and prove the cap at its edge with a
   finite body.

Every test here is BOUNDED when the thing under test is removed: the largest
allocation is a body of cap+1 bytes of cheap content (~4 MiB) and its parse, and the expansion test
parses 256 KiB bodies. Nothing here builds a body that would take hundreds of MB to parse.
"""

from __future__ import annotations

import asyncio
import json
import tracemalloc

import httpx
import pytest

from app.services import llm_proxy
from app.services.llm_proxy import ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS, check_request_policy
from app.services.llm_proxy_openai import OPENAI_ALLOWED_TOP_LEVEL_FIELDS
from app.services.llm_proxy_openai import check_request_policy as openai_check_request_policy
from app.services.proxy_container import PROXY_MEMORY_LIMIT
from tests.test_llm_proxy_policy import (
    ANTHROPIC,
    KITS,
    OPENAI,
    PREFIX,
    WAIT,
    Stub,
    _build,
    _post,
    _proxy_client,
    _run,
    _stub_client,
)

MIB = 1024 * 1024

# =============================================================================================
# 2. The top-level surface is an allowlist
# =============================================================================================


def test_the_anthropic_allowlist_is_exactly_what_the_pinned_cli_was_measured_to_send():
    """A literal (measured on CLI 2.1.285 / agent-sdk 0.2.162: every request of the
    production path, clean and hostile workspace, 3 requests each; the default tool set's nested search
    request alone adds `tool_choice`, and is refused). Widening
    it is a deliberate edit, after the engine-up capture test is re-run."""
    assert ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS == frozenset(
        {
            "model",
            "messages",
            "system",
            "tools",
            "metadata",
            "max_tokens",
            "thinking",
            "context_management",
            "output_config",
            "stream",
        }
    )


def test_the_openai_allowlist_is_exactly_what_the_pinned_runner_was_measured_to_send():
    """A literal (measured on openai-agents 0.23.1 / openai 3.28.0: the tool round
    trip and the rich session, 5 requests; `test_openai_engine_up_integration.py` records the same
    seven keys)."""
    assert OPENAI_ALLOWED_TOP_LEVEL_FIELDS == frozenset(
        {"model", "input", "instructions", "tools", "store", "service_tier", "include"}
    )


# The shapes below are the measured requests with their bulky values (the system prompt, the six
# tool definitions) shortened; the KEYS and the scalar fields are as recorded.
_MEASURED_ANTHROPIC_MAIN_TURN = {
    "model": "claude-opus-5-5",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    "system": [{"type": "text", "text": "x-anthropic-billing-header"}],
    "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
    "metadata": {"user_id": "{}"},
    "max_tokens": 128000,
    "thinking": {"type": "adaptive"},
    "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    "output_config": {"effort": "medium"},
    "stream": True,
}
_MEASURED_ANTHROPIC_TITLE_CALL = {
    "model": "claude-opus-5-5",
    "messages": [{"role": "user", "content": "hi"}],
    "system": [{"type": "text", "text": "t"}],
    "tools": [],
    "metadata": {"user_id": "{}"},
    "max_tokens": 128000,
    "output_config": {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    },
    "stream": True,
}
_MEASURED_OPENAI = {
    "model": "gpt-5-mini",
    "service_tier": "default",
    "include": ["reasoning.encrypted_content"],
    "input": [{"role": "user", "content": "hi"}],
    "instructions": "be brief",
    "store": False,
    "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}, "strict": True}],
}


@pytest.mark.parametrize(
    "payload",
    [_MEASURED_ANTHROPIC_MAIN_TURN, _MEASURED_ANTHROPIC_TITLE_CALL],
    ids=["main_turn", "title_call"],
)
def test_the_measured_anthropic_requests_pass(payload):
    assert check_request_policy(json.dumps(payload).encode()) is None


def test_the_measured_openai_request_passes():
    assert openai_check_request_policy(json.dumps(_MEASURED_OPENAI).encode()) is None


# What each dialect must refuse. Fields the provider documents (and the pinned CLI's own cost code
# reads, for the ones that move the price) that are NOT measured on the wire, plus one invented.
_UNKNOWN_ANTHROPIC = [
    "mcp_servers",  # the MCP connector: the provider connects to a server the request names
    "container",  # the provider-side code-execution container
    "inference_geo",  # the CLI's own cost function prices `us` at 1.1x
    "service_tier",  # priority capacity
    "speed",  # `fast` is billed at 2x
    "tool_choice",
    "temperature",
    "top_k",
    "top_p",
    "stop_sequences",
    "safeguards",
    "thread",
    "diagnostics",
    "cache_control",
    "betas",  # a header in the SDK; in the body it would be a field the provider may read
    "requests",  # the Batches payload
    "a_field_the_provider_ships_next_year",
]
_UNKNOWN_OPENAI = [
    "previous_response_id",
    "conversation",
    "background",
    "prompt",
    "tool_choice",
    "stream",
    "parallel_tool_calls",
    "reasoning",
    "temperature",
    "max_output_tokens",
    "truncation",
    "metadata",
    "user",
    "text",
    "a_field_the_provider_ships_next_year",
]


def _unknown_cases():
    for field in _UNKNOWN_ANTHROPIC:
        yield pytest.param(ANTHROPIC, field, id=f"anthropic-{field}")
    for field in _UNKNOWN_OPENAI:
        yield pytest.param(OPENAI, field, id=f"openai-{field}")


@pytest.mark.parametrize(("kit", "field"), list(_unknown_cases()))
def test_an_unknown_top_level_field_is_refused_before_the_forward_and_recorded(kit, field):
    """Whatever it holds. It is a recorded policy refusal (the build fails closed and says which
    field), spends nothing, and is NEVER sent upstream; the next clean request still goes."""
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            refused = await asyncio.wait_for(_post(client, kit, token, **{field: {"anything": [1, 2]}}), WAIT)
            after = await asyncio.wait_for(_post(client, kit, token), WAIT)
        return refused, after, registry.usage_for(token)

    refused, after, usage = _run(scenario())

    detail = f"{PREFIX}request field '{field}' is not allowed"
    assert refused.status_code == 403
    assert detail in refused.text
    assert usage.policy_refusals == [detail]
    assert usage.tokens == 15  # only the clean request after it spent anything
    assert len(stub.calls) == 1  # the clean request; the refused one never reached the upstream
    assert after.status_code == 200


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize("value", [None, "", [], {}, 0, False], ids=["null", "empty_str", "empty_list", "empty_obj", "zero", "false"])
def test_an_unknown_field_is_refused_even_when_it_is_empty(kit, value):
    body = json.dumps(kit.body(a_field_the_provider_ships_next_year=value)).encode()
    check = check_request_policy if kit is ANTHROPIC else openai_check_request_policy

    assert check(body) == f"{PREFIX}request field 'a_field_the_provider_ships_next_year' is not allowed"


@pytest.mark.parametrize("kit", KITS)
def test_a_hostile_field_name_is_bounded_and_escaped_in_the_detail(kit):
    name = "x" * 5000 + "\n\x00'"
    body = json.dumps(kit.body(**{name: 1})).encode()
    check = check_request_policy if kit is ANTHROPIC else openai_check_request_policy

    detail = check(body)

    assert detail is not None and detail.startswith(f"{PREFIX}request field '" + "x" * 64 + "...")
    assert len(detail) < 200 and "\n" not in detail and "\x00" not in detail


@pytest.mark.parametrize("kit", KITS)
def test_the_allowlist_is_checked_before_the_values_it_admits(kit):
    """An unknown field and a bad tool in the same body: the field is what is named, so the
    refusal does not depend on which rule happens to run first on the rest."""
    body = kit.body(tools=[{"type": "web_search_20250305", "name": "web_search"}], a_field_the_provider_ships_next_year=1)
    check = check_request_policy if kit is ANTHROPIC else openai_check_request_policy

    assert check(json.dumps(body).encode()) == f"{PREFIX}request field 'a_field_the_provider_ships_next_year' is not allowed"


# =============================================================================================
# 3. A body's parsed size is bounded
# =============================================================================================

# MEASURED, as literals: the proxy image's own Python 3.14.8, `ru_maxrss` of the
# real policy parse, one body shape per process, 16 MiB each. The comment above
# `MAX_REQUEST_BODY_BYTES` carries the table; these are the three numbers the arithmetic needs.
PROCESS_BASELINE_BYTES = 50 * MIB  # the proxy process before any request (imports, uvicorn, FastAPI)
PARSE_EXPANSION = 35  # measured 33.2 on the worst shape; 35 leaves a little for another Python minor
BODY_COPIES_ON_THE_WAY_IN = 2  # the read buffer, and the decoded text `json.loads` makes of the bytes


def _container_cap_bytes() -> int:
    assert PROXY_MEMORY_LIMIT == "512m"  # a literal: moving the cap is a deliberate edit with this test
    return 512 * MIB


def test_the_worst_case_parse_beside_a_full_queue_fits_half_the_container_cap():
    """The audit's finding in one inequality: a hostile body is parsed (~35x its size) while up to
    `MAX_CONCURRENT_REQUESTS_PER_TOKEN` bodies wait on the slot. The cap is chosen so this stays
    under HALF of the container's memory (the rest is margin for what was not measured: the upstream
    response, the trace, allocator fragmentation). At the old 16 MiB the same sum is ~740 MiB."""
    body = llm_proxy.MAX_REQUEST_BODY_BYTES
    queue = llm_proxy.MAX_CONCURRENT_REQUESTS_PER_TOKEN

    worst = PROCESS_BASELINE_BYTES + (queue + PARSE_EXPANSION + BODY_COPIES_ON_THE_WAY_IN) * body

    assert worst <= _container_cap_bytes() // 2, f"{worst / MIB:.0f} MiB"


def test_the_arithmetic_would_have_caught_the_old_cap():
    """Non-vacuity: the same inequality at 16 MiB is false, by a wide margin."""
    worst = PROCESS_BASELINE_BYTES + (
        llm_proxy.MAX_CONCURRENT_REQUESTS_PER_TOKEN + PARSE_EXPANSION + BODY_COPIES_ON_THE_WAY_IN
    ) * (16 * MIB)

    assert worst > _container_cap_bytes()


def _rep(unit: bytes, head: bytes, tail: bytes, size: int) -> bytes:
    return head + unit * ((size - len(head) - len(tail)) // len(unit)) + tail


_HEAD = b'{"model":"x","messages":['
_WORST_SHAPES = {
    "empty_objects": (b"{},", b"{}]}"),
    "empty_lists": (b"[],", b"[]]}"),
    "one_key_objects": (b'{"":0},', b"{}]}"),
    "nested_one_key": (b'{"":{}},', b"{}]}"),  # the worst measured: 33.2x
    "one_key_lists": (b'{"":[]},', b"{}]}"),
}


@pytest.mark.parametrize("check", [check_request_policy, openai_check_request_policy], ids=["anthropic", "openai"])
@pytest.mark.parametrize("shape", list(_WORST_SHAPES))
def test_the_real_policy_parse_expands_a_hostile_body_by_no_more_than_the_pinned_factor(check, shape):
    """Measured on the real functions at 256 KiB (an 8 MiB allocation at the factor, not 130): the
    ratio of the parse's peak to the body's size is what `PARSE_EXPANSION` stands for, and it is
    scale-free. If a change to the parse (a hook that allocates more per object, a second pass)
    pushes it over, the arithmetic above is no longer true and this is the test that says so."""
    unit, tail = _WORST_SHAPES[shape]
    body = _rep(unit, _HEAD, tail, 256 * 1024)

    tracemalloc.start()
    try:
        check(body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak / len(body) <= PARSE_EXPANSION, f"{peak / len(body):.1f}x"
    assert peak / len(body) > 10  # non-vacuity: the shape really is an expanding one


def test_a_body_one_byte_over_the_real_cap_is_refused_413_and_one_at_the_cap_is_forwarded():
    """The cap at its real value, with a FINITE body (cap+1 bytes of cheap content: a long string,
    parsed at about its own size). Both sides of the edge around the same payload shape, so the 413
    is the cap and not a refusal for some other reason. Remove the cap and the over-sized body is
    forwarded: this goes red having allocated a few multiples of the cap, not hundreds of MB."""
    cap = llm_proxy.MAX_REQUEST_BODY_BYTES
    assert cap == 4 * MIB  # the number below is the real one, not a test-sized stand-in

    def payload(size: int) -> bytes:
        head = b'{"model":"claude-opus-4-8","max_tokens":1,"system":"'
        tail = b'"}'
        return head + b"x" * (size - len(head) - len(tail)) + tail

    at_cap, over = payload(cap), payload(cap + 1)
    assert len(at_cap) == cap and len(over) == cap + 1
    stub = Stub(ANTHROPIC.reply(10, 5))

    async def scenario():
        app, registry, token = _build(ANTHROPIC, _stub_client(stub))
        async with _proxy_client(app) as client:
            ok = await asyncio.wait_for(
                client.post(ANTHROPIC.path, headers=ANTHROPIC.auth(token), content=at_cap), 30
            )
            refused = await asyncio.wait_for(
                client.post(ANTHROPIC.path, headers=ANTHROPIC.auth(token), content=over), 30
            )
        return ok, refused

    ok, refused = _run(scenario())

    assert ok.status_code == 200
    assert refused.status_code == 413 and refused.text.startswith("REQUEST_TOO_LARGE")
    assert [len(body) for _, body in stub.calls] == [cap]  # the one at the cap, whole; the other never went out


def test_the_same_edge_holds_for_a_chunked_body_with_no_declared_length():
    """The other way the proxy learns a length: counting as it reads. Cap+1 bytes in 64 KiB chunks."""
    cap = llm_proxy.MAX_REQUEST_BODY_BYTES
    chunk = b"x" * (64 * 1024)
    pulled: list[int] = []

    async def over_the_cap():
        yield b'{"model":"claude-opus-4-8","max_tokens":1,"system":"'
        sent = 0
        while sent <= cap:  # one chunk past the cap, then stop: a finite body
            pulled.append(1)
            sent += len(chunk)
            yield chunk
        yield b'"}'

    stub = Stub(ANTHROPIC.reply(10, 5))

    async def scenario():
        app, registry, token = _build(ANTHROPIC, _stub_client(stub))
        async with _proxy_client(app) as client:
            return await asyncio.wait_for(
                client.post(ANTHROPIC.path, headers=ANTHROPIC.auth(token), content=over_the_cap()), 30
            )

    refused = _run(scenario())

    assert refused.status_code == 413
    assert stub.calls == []
    assert len(pulled) <= cap // len(chunk) + 2  # it stopped reading at the cap, not at the end of the body


def test_a_body_over_the_cap_is_refused_before_it_is_parsed(monkeypatch):
    """The bound sits IN FRONT of the parse, which is what costs the memory: `json.loads` is never
    called on a body over the cap. The body is cheap to parse on purpose (one long string, about its
    own size) so that this test, with the bound removed, parses ~4 MiB and not the ~140 MB a
    hostile shape would take; that expansion is measured separately, at 256 KiB, above."""
    cap = llm_proxy.MAX_REQUEST_BODY_BYTES
    head, tail = b'{"model":"claude-opus-4-8","max_tokens":1,"system":"', b'"}'
    body = head + b"x" * (cap + 8 - len(head) - len(tail)) + tail
    assert len(body) > cap
    parsed: list[int] = []
    real_loads = json.loads

    def spy(data, *args, **kwargs):
        if isinstance(data, (bytes, str)) and len(data) > MIB:  # the request body, not a small reply
            parsed.append(len(data))
        return real_loads(data, *args, **kwargs)

    monkeypatch.setattr(json, "loads", spy)
    stub = Stub(ANTHROPIC.reply(10, 5))

    async def scenario():
        app, registry, token = _build(ANTHROPIC, _stub_client(stub))
        async with _proxy_client(app) as client:
            return await asyncio.wait_for(
                client.post(ANTHROPIC.path, headers=ANTHROPIC.auth(token), content=body), 30
            )

    refused = _run(scenario())

    assert refused.status_code == 413
    assert parsed == [] and stub.calls == []
