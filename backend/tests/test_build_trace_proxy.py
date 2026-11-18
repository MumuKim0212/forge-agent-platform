"""What the LLM proxy records per
forwarded Messages call, and that recording it never touches metering.

Unit seam, no Docker: the real proxy app over a stubbed upstream that answers each call with a
canned body. Expected values are literals (the bounds, the marker, the field names), not the
module's constants: a rename that moved both sides would otherwise stay green.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.services import llm_proxy
from app.services.llm_proxy import TokenRegistry, create_app

ADMIN = {"X-Forge-Admin-Secret": "test-admin-secret"}


class Upstream:
    """A stub Messages API: answers call N with `responses[N]` (the last one repeats)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.app = FastAPI()

        @self.app.post("/v1/messages")
        async def messages(request: Request) -> Response:  # noqa: ARG001
            body, content_type = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return Response(content=body, media_type=content_type)


def make(responses):
    upstream = Upstream(responses)
    registry = TokenRegistry()
    transport = httpx.ASGITransport(app=upstream.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test")
    app = create_app(registry, real_api_key="k", admin_secret="test-admin-secret", http_client=http)
    client = TestClient(app)
    token = client.post(
        "/_forge_admin/tokens",
        json={"build_id": "b", "token_budget": 10**9, "cost_budget_usd": 10**6},
        headers=ADMIN,
    ).json()["token"]
    return client, token, registry, upstream


def json_reply(content, *, model="claude-opus-5-5", stop_reason="end_turn", usage=None) -> tuple[bytes, str]:
    body = {
        "id": "msg",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }
    return json.dumps(body).encode(), "application/json"


def sse(events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def sse_reply(blocks, *, stop_reason="tool_use", usage=None, model="claude-opus-5-5") -> tuple[bytes, str]:
    """Each block is ('text', str) | ('thinking', str) | ('tool_use', name, input_dict)."""
    events = [
        {
            "type": "message_start",
            "message": {"id": "m", "model": model, "content": [], "usage": usage or {"input_tokens": 10, "output_tokens": 1}},
        }
    ]
    for index, block in enumerate(blocks):
        if block[0] == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block[1]}})
        elif block[0] == "thinking":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": block[1]}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": "sig"}})
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": "toolu_1", "name": block[1], "input": {}}})
            raw = json.dumps(block[2])
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": raw[:5]}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": raw[5:]}})
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}})
    events.append({"type": "message_stop"})
    return sse(events), "text/event-stream"


def send(client, token, messages=None, **extra):
    return client.post(
        "/v1/messages", headers={"x-api-key": token}, json={"model": "claude-opus-5-5", "messages": messages or [], **extra}
    )


def trace(client, token) -> dict:
    resp = client.get(f"/_forge_admin/tokens/{token}/trace", headers=ADMIN)
    assert resp.status_code == 200
    return resp.json()


def usage(client, token) -> dict:
    return client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN).json()


def tool_result(tool_use_id, content, **extra):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, **extra}


# --- what is recorded ---------------------------------------------------------------------------


def test_a_json_call_records_measured_fields_and_only_the_new_content():
    reply = json_reply(
        [
            {"type": "thinking", "thinking": "SECRET-REASONING-TEXT-should-not-be-stored", "signature": "s"},
            {"type": "text", "text": "I will read the file."},
            {"type": "tool_use", "id": "toolu_9", "name": "Read", "input": {"file_path": "/workspace/a.py"}},
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 1000, "output_tokens": 500},
    )
    client, token, _, _ = make([reply])
    history = [
        {"role": "user", "content": "build me a thing"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [tool_result("toolu_1", "HISTORY-RESULT-recorded-on-an-earlier-call")]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}}]},
        {"role": "user", "content": [tool_result("toolu_2", [{"type": "text", "text": "LAST-TURN-RESULT"}], is_error=True)]},
    ]

    assert send(client, token, history).status_code == 200

    record = trace(client, token)
    assert record["calls_total"] == 1 and len(record["steps"]) == 1
    step = record["steps"][0]
    assert step["ordinal"] == 1
    assert step["model"] == "claude-opus-5-5"
    assert (step["input_tokens"], step["output_tokens"], step["tokens"]) == (1000, 500, 1500)
    assert step["cost_usd"] == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    assert step["stop_reason"] == "tool_use"
    assert step["content"] == "recorded"
    assert step["started_at"] and step["ended_at"]
    assert step["thinking"] == [{"chars": len("SECRET-REASONING-TEXT-should-not-be-stored")}]
    blocks = [(b["kind"], b.get("name"), b["text"]) for b in step["blocks"]]
    assert blocks == [
        ("assistant_text", None, "I will read the file."),
        ("tool_use", "Read", '{"file_path": "/workspace/a.py"}'),
        ("tool_result", None, "LAST-TURN-RESULT"),
    ]
    assert step["blocks"][2]["is_error"] is True and step["blocks"][2]["tool_use_id"] == "toolu_2"
    dumped = json.dumps(record)
    assert "HISTORY-RESULT" not in dumped  # history is never re-recorded
    assert "SECRET-REASONING-TEXT" not in dumped  # thinking is presence and length only


def test_an_sse_call_is_parsed_for_content_the_same_way():
    reply = sse_reply(
        [
            ("thinking", "SECRET-STREAMED-THINKING"),
            ("text", "Looking."),
            ("tool_use", "Write", {"file_path": "/workspace/b.py", "content": "x = 1"}),
        ]
    )
    client, token, _, _ = make([reply])

    send(client, token, [{"role": "user", "content": [tool_result("t0", "streamed-turn result")]}])

    step = trace(client, token)["steps"][0]
    assert step["content"] == "recorded" and step["stop_reason"] == "tool_use"
    assert step["thinking"] == [{"chars": len("SECRET-STREAMED-THINKING")}]
    assert [(b["kind"], b["text"]) for b in step["blocks"]] == [
        ("assistant_text", "Looking."),
        ("tool_use", '{"file_path": "/workspace/b.py", "content": "x = 1"}'),
        ("tool_result", "streamed-turn result"),
    ]
    assert step["blocks"][1]["name"] == "Write"
    assert "SECRET-STREAMED-THINKING" not in json.dumps(trace(client, token))
    # Metered like any stream: 10 input + the cumulative 5 output.
    assert (step["input_tokens"], step["output_tokens"]) == (10, 5)


def test_a_first_call_whose_last_user_turn_is_a_prompt_records_no_request_content():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "hi"}])])

    send(client, token, [{"role": "user", "content": "the whole intent prompt, not a tool result"}])

    blocks = trace(client, token)["steps"][0]["blocks"]
    assert [b["kind"] for b in blocks] == ["assistant_text"]
    assert "intent prompt" not in json.dumps(trace(client, token))


def test_a_tool_result_with_a_block_list_keeps_text_and_names_the_rest():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "ok"}])])
    content = [{"type": "text", "text": "line one"}, {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]

    send(client, token, [{"role": "user", "content": [tool_result("t", content)]}])

    result = trace(client, token)["steps"][0]["blocks"][1]
    assert result["text"] == "line one\n[image block]"
    assert "AAAA" not in json.dumps(trace(client, token))


# --- the proxy token is redacted by value --------------------------------------------------------


def test_the_proxy_token_is_replaced_in_a_tool_result_and_a_tool_use_input():
    client, token, _, upstream = make([])  # the token exists only after make(): build the reply around it
    upstream.responses = [
        json_reply(
            [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": f"echo {token}"}},
                {"type": "text", "text": f"the key is {token}"},
            ]
        )
    ]

    send(client, token, [{"role": "user", "content": [tool_result("t", f"ANTHROPIC_API_KEY={token}")]}])

    record = trace(client, token)
    dumped = json.dumps(record)
    assert token not in dumped
    assert token[:12] not in dumped
    texts = [b["text"] for b in record["steps"][0]["blocks"]]
    assert texts == [
        '{"command": "echo [proxy-token-redacted]"}',
        "the key is [proxy-token-redacted]",
        "ANTHROPIC_API_KEY=[proxy-token-redacted]",
    ]


def test_the_token_is_replaced_before_a_block_is_cut_so_a_prefix_cannot_survive():
    client, token, _, upstream = make([])
    upstream.responses = [json_reply([{"type": "text", "text": "ok"}])]
    long_result = "x" * 7_988 + token  # 12 characters of the token sit inside the 8,000-character cut

    send(client, token, [{"role": "user", "content": [tool_result("t", long_result)]}])

    block = trace(client, token)["steps"][0]["blocks"][1]
    assert block["truncated"] is True and len(block["text"]) == 8_000
    assert token[:8] not in block["text"]  # unredacted, the first 12 characters of it would be here


# --- bounds, enforced in the proxy's memory ------------------------------------------------------


def test_more_than_400_calls_keep_content_for_400_and_measured_fields_only_after():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "step"}])])

    for _ in range(405):
        assert send(client, token).status_code == 200

    record = trace(client, token)
    assert record["calls_total"] == 405 and len(record["steps"]) == 405
    assert [s["content"] for s in record["steps"][:400]] == ["recorded"] * 400
    assert [s["content"] for s in record["steps"][400:]] == ["dropped"] * 5
    late = record["steps"][400]
    assert late["blocks"] == [] and late["ordinal"] == 401 and late["tokens"] == 15 and late["model"] == "claude-opus-5-5"
    assert record["content_truncated"] is True
    # Nothing was lost to the sums: every call, recorded or not, is in them.
    assert sum(s["tokens"] for s in record["steps"]) == usage(client, token)["tokens"] == 405 * 15
    assert sum(s["cost_usd"] for s in record["steps"]) == pytest.approx(usage(client, token)["cost_usd"])


def test_one_oversized_block_is_cut_and_marked():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "A" * 20_000}])])

    send(client, token, [{"role": "user", "content": [tool_result("t", "B" * 9_000)]}])

    blocks = trace(client, token)["steps"][0]["blocks"]
    assert [(len(b["text"]), b["chars"], b["truncated"]) for b in blocks] == [(8_000, 20_000, True), (8_000, 9_000, True)]


def test_a_block_exactly_at_the_limit_is_not_marked():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "A" * 8_000}])])

    send(client, token)

    block = trace(client, token)["steps"][0]["blocks"][0]
    assert (len(block["text"]), block["truncated"]) == (8_000, False)


def test_the_two_megabyte_total_stops_recording_content_before_the_call_limit():
    client, token, _, _ = make([json_reply([{"type": "text", "text": "z" * 8_000}])])

    for _ in range(300):  # ~8.2 KB each: the 2 MB total is spent near call 255, well before 400
        send(client, token)

    record = trace(client, token)
    recorded = [s for s in record["steps"] if s["content"] == "recorded"]
    dropped = [s for s in record["steps"] if s["content"] == "dropped"]
    assert dropped and recorded and len(recorded) < 300
    assert sum(len(json.dumps(s, ensure_ascii=False)) for s in recorded) <= 2 * 1024 * 1024
    assert [s["ordinal"] for s in dropped] == list(range(len(recorded) + 1, 301))  # once spent, always spent
    assert record["content_truncated"] is True


def test_calls_past_the_step_ceiling_fold_into_totals_so_sums_still_reconcile(monkeypatch):
    monkeypatch.setattr(llm_proxy, "TRACE_MAX_STEP_ENTRIES", 5)
    client, token, _, _ = make([json_reply([{"type": "text", "text": "s"}])])

    for _ in range(8):
        send(client, token)

    record = trace(client, token)
    assert (record["calls_total"], len(record["steps"]), record["omitted_calls"]) == (8, 5, 3)
    assert sum(s["tokens"] for s in record["steps"]) + record["omitted_tokens"] == usage(client, token)["tokens"]


# --- independence from metering ------------------------------------------------


def test_content_that_cannot_be_parsed_is_an_unparsed_step_and_the_call_is_metered_normally():
    # usage is readable, `content` is not a list.
    bad = (json.dumps({"model": "claude-opus-5-5", "content": "nope", "usage": {"input_tokens": 100, "output_tokens": 50}}).encode(), "application/json")
    client, token, _, _ = make([bad])

    resp = send(client, token)

    assert resp.status_code == 200
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"]) == (150, False)
    step = trace(client, token)["steps"][0]
    assert step["content"] == "unparsed" and step["blocks"] == []
    assert (step["tokens"], step["model"]) == (150, "claude-opus-5-5")  # its measured fields are still there


def test_a_parser_that_raises_is_an_unparsed_step_and_metering_is_untouched(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the trace parser exploded")

    monkeypatch.setattr(llm_proxy, "_record_blocks", boom)
    client, token, _, _ = make([json_reply([{"type": "text", "text": "hi"}])])

    resp = send(client, token)

    assert resp.status_code == 200
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"], metered["unmetered_reason"]) == (15, False, None)
    assert trace(client, token)["steps"][0]["content"] == "unparsed"


def test_a_capture_that_raises_outright_cannot_reach_metering_or_the_response(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the whole trace capture failed")

    monkeypatch.setattr(llm_proxy, "_capture_trace", boom)
    client, token, _, _ = make([json_reply([{"type": "text", "text": "hi"}])])

    first = send(client, token)
    second = send(client, token)  # and the token is not poisoned for the next call

    assert (first.status_code, second.status_code) == (200, 200)
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"]) == (30, False)
    assert trace(client, token)["steps"] == []  # nothing recorded, and nothing else changed


def test_an_unmeterable_response_still_fails_closed_and_the_trace_does_not_mask_it():
    no_usage = (json.dumps({"model": "claude-opus-5-5", "content": [{"type": "text", "text": "free lunch"}]}).encode(), "application/json")
    client, token, _, _ = make([no_usage])

    first = send(client, token)
    second = send(client, token)

    assert first.status_code == 200
    metered = usage(client, token)
    assert metered["unmetered"] is True and "no usage object" in metered["unmetered_reason"]
    assert second.status_code == 403 and second.text.startswith("USAGE_UNMETERED: ")
    steps = trace(client, token)["steps"]
    assert len(steps) == 1  # the refused second call was never forwarded, so it has no step
    assert steps[0]["content"] == "unparsed" and steps[0]["tokens"] is None and steps[0]["cost_usd"] is None


def test_an_unmeterable_response_stays_unmetered_even_when_the_trace_capture_also_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("trace failure")

    monkeypatch.setattr(llm_proxy, "_capture_trace", boom)
    client, token, _, _ = make([(b"not json at all", "application/json")])

    send(client, token)

    assert usage(client, token)["unmetered"] is True


# --- the admin read ------------------------------------------------------------------------------


def test_the_trace_read_needs_the_admin_secret_and_a_known_token():
    client, token, _, _ = make([json_reply([])])

    assert client.get(f"/_forge_admin/tokens/{token}/trace").status_code == 403
    assert client.get(f"/_forge_admin/tokens/{token}/trace", headers={"X-Forge-Admin-Secret": "wrong"}).status_code == 403
    assert client.get(f"/_forge_admin/tokens/{token}/trace", headers={"x-api-key": token}).status_code == 403
    assert client.get("/_forge_admin/tokens/no-such-token/trace", headers=ADMIN).status_code == 404
    assert trace(client, token) == {
        "steps": [],
        "calls_total": 0,
        "content_truncated": False,
        "omitted_calls": 0,
        "omitted_tokens": 0,
        "omitted_cost_usd": 0.0,
    }


def test_the_forwarded_endpoint_allowlist_is_unchanged():
    assert llm_proxy.ALLOWED_ENDPOINTS == {("POST", "v1/messages"): frozenset({"", "beta=true"})}
    client, token, _, upstream = make([json_reply([])])

    # The trace path is reachable only as an admin route; on the proxy path it is just
    # another endpoint, and an unmeasured one.
    resp = client.get("/v1/messages/trace", headers={"x-api-key": token})

    assert resp.status_code == 403 and "is not allowed" in resp.json()["detail"]
    assert upstream.calls == 0
