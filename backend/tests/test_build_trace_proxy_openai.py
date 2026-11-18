"""ADR 0035 §Decision 3: what the `openai` dialect records per forwarded Responses
call, and that recording it never touches metering. Mirrors `test_build_trace_proxy.py`.

Unit seam, no Docker: the real proxy app over a stub upstream that answers each call with a canned
body. Expected values are literals (the bounds, the marker, the field names), not the modules'
constants: a rename that moved both sides would otherwise stay green.
"""

from __future__ import annotations

import json

import pytest

from app.services import llm_proxy, llm_proxy_openai
from tests.openai_proxy_support import (
    ADMIN,
    body,
    call_item,
    json_reply,
    make,
    message_item,
    reasoning_item,
    send,
    sse,
    sse_reply,
    trace,
    usage,
    usage_obj,
)


def output_item(call_id, output, **extra) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output, **extra}


# --- what is recorded ---------------------------------------------------------------------------


def test_a_json_call_records_measured_fields_and_only_the_new_content():
    reply = json_reply(
        [
            reasoning_item("SECRET-REASONING-SUMMARY-should-not-be-stored", encrypted="ENCRYPTED-BLOB-should-not-be-stored"),
            message_item("I will read the file."),
            call_item("Read", {"file_path": "/workspace/a.py"}, call_id="call_9"),
        ],
        model="gpt-5-mini",
        usage=usage_obj(1000, 500, cached=200, written=30),
    )
    client, token, _, _ = make([reply])
    history = [
        {"role": "user", "content": "build me a thing"},
        {"type": "function_call", "name": "Bash", "arguments": '{"command": "ls"}', "call_id": "call_1"},
        output_item("call_1", "HISTORY-RESULT-recorded-on-an-earlier-call"),
        {"type": "function_call", "name": "Read", "arguments": "{}", "call_id": "call_2"},
        {"type": "function_call", "name": "Read", "arguments": "{}", "call_id": "call_3"},
        output_item("call_2", [{"type": "input_text", "text": "LAST-TURN-RESULT"}]),
        output_item("call_3", "SECOND-PARALLEL-RESULT"),
    ]

    assert send(client, token, body(input=history)).status_code == 200

    record = trace(client, token)
    assert record["calls_total"] == 1 and len(record["steps"]) == 1
    step = record["steps"][0]
    assert step["ordinal"] == 1
    assert step["model"] == "gpt-5-mini"
    # input_tokens is the uncached part, cache_read the cached part, cache_write as reported.
    assert (step["input_tokens"], step["output_tokens"], step["cache_read_tokens"], step["cache_write_tokens"]) == (
        800,
        500,
        200,
        30,
    )
    assert step["tokens"] == 1300
    assert step["cost_usd"] == pytest.approx((800 * 0.25 + 200 * 0.025 + 500 * 2.00) / 1e6)
    assert step["stop_reason"] == "completed"
    assert step["content"] == "recorded"
    assert step["started_at"] and step["ended_at"]
    assert step["thinking"] == [{"chars": len("SECRET-REASONING-SUMMARY-should-not-be-stored")}]
    blocks = [(b["kind"], b.get("name"), b["text"]) for b in step["blocks"]]
    assert blocks == [
        ("assistant_text", None, "I will read the file."),
        ("tool_use", "Read", '{"file_path": "/workspace/a.py"}'),
        ("tool_result", None, "LAST-TURN-RESULT"),
        ("tool_result", None, "SECOND-PARALLEL-RESULT"),
    ]
    assert step["blocks"][1]["id"] == "call_9"
    assert [b["tool_use_id"] for b in step["blocks"][2:]] == ["call_2", "call_3"]
    assert step["blocks"][2]["is_error"] is None
    dumped = json.dumps(record)
    assert "HISTORY-RESULT" not in dumped  # history is never re-recorded
    assert "SECRET-REASONING-SUMMARY" not in dumped  # reasoning is presence and length only
    assert "ENCRYPTED-BLOB" not in dumped  # and its opaque payload never


def test_reasoning_with_only_an_encrypted_payload_records_its_length_and_nothing_else():
    client, token, _, _ = make([json_reply([reasoning_item(encrypted="E" * 321), message_item("ok")])])

    send(client, token)

    record = trace(client, token)
    assert record["steps"][0]["thinking"] == [{"chars": 321}]
    assert "EEEEE" not in json.dumps(record)


def test_a_reasoning_item_with_neither_summary_nor_payload_is_still_a_presence_marker():
    client, token, _, _ = make([json_reply([reasoning_item(), message_item("ok")])])

    send(client, token)

    assert trace(client, token)["steps"][0]["thinking"] == [{"chars": 0}]


def test_an_sse_call_is_parsed_for_content_the_same_way():
    reply = sse_reply(
        [
            reasoning_item("SECRET-STREAMED-REASONING"),
            message_item("Looking."),
            call_item("Write", {"file_path": "/workspace/b.py", "content": "x = 1"}),
        ],
        usage=usage_obj(10, 5),
    )
    client, token, _, _ = make([reply])

    send(client, token, body(input=[output_item("t0", "streamed-turn result")]))

    step = trace(client, token)["steps"][0]
    assert step["content"] == "recorded" and step["stop_reason"] == "completed"
    assert step["thinking"] == [{"chars": len("SECRET-STREAMED-REASONING")}]
    assert [(b["kind"], b["text"]) for b in step["blocks"]] == [
        ("assistant_text", "Looking."),
        ("tool_use", '{"file_path": "/workspace/b.py", "content": "x = 1"}'),
        ("tool_result", "streamed-turn result"),
    ]
    assert step["blocks"][1]["name"] == "Write"
    assert "SECRET-STREAMED-REASONING" not in json.dumps(trace(client, token))
    assert (step["input_tokens"], step["output_tokens"]) == (10, 5)


def test_an_sse_terminal_event_without_output_falls_back_to_the_per_item_done_events():
    reply = sse_reply([message_item("from the item events")], include_output=False)
    client, token, _, _ = make([reply])

    send(client, token, body())

    step = trace(client, token)["steps"][0]
    assert step["content"] == "recorded"
    assert [b["text"] for b in step["blocks"]] == ["from the item events"]


def test_an_incomplete_response_records_the_reason_it_gave():
    client, token, _, _ = make([json_reply([message_item("cut")], status="incomplete", reason="max_output_tokens")])

    send(client, token)

    assert trace(client, token)["steps"][0]["stop_reason"] == "incomplete:max_output_tokens"


def test_a_first_call_whose_input_is_a_prompt_records_no_request_content():
    client, token, _, _ = make([json_reply([message_item("hi")])])

    send(client, token, body(input="the whole intent prompt, as a plain string"))
    send(client, token, body(input=[{"role": "user", "content": "the whole intent prompt, as a message"}]))

    record = trace(client, token)
    assert [[b["kind"] for b in s["blocks"]] for s in record["steps"]] == [["assistant_text"], ["assistant_text"]]
    assert "intent prompt" not in json.dumps(record)


def test_a_function_output_followed_by_a_user_message_is_still_last_turn():
    client, token, _, _ = make([json_reply([message_item("ok")])])
    history = [
        {"role": "user", "content": "start"},
        {"type": "function_call", "name": "Read", "arguments": "{}", "call_id": "c1"},
        output_item("c1", "RESULT-OF-C1"),
        {"role": "user", "content": "and then this"},
    ]

    send(client, token, body(input=history))

    assert [b["text"] for b in trace(client, token)["steps"][0]["blocks"]] == ["ok", "RESULT-OF-C1"]


def test_an_assistant_message_ends_the_last_turn():
    client, token, _, _ = make([json_reply([message_item("ok")])])
    history = [
        output_item("c0", "OLD-RESULT"),
        {"role": "assistant", "content": [{"type": "output_text", "text": "earlier"}]},
        {"role": "user", "content": "again"},
    ]

    send(client, token, body(input=history))

    assert "OLD-RESULT" not in json.dumps(trace(client, token))


def test_a_function_output_with_a_part_list_keeps_text_and_names_the_rest():
    client, token, _, _ = make([json_reply([message_item("ok")])])
    parts = [{"type": "input_text", "text": "line one"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]

    send(client, token, body(input=[output_item("t", parts)]))

    result = trace(client, token)["steps"][0]["blocks"][1]
    assert result["text"] == "line one\n[input_image block]"
    assert "AAAA" not in json.dumps(trace(client, token))


def test_an_unknown_output_item_is_named_by_its_type_only():
    client, token, _, _ = make([json_reply([{"type": "web_search_call", "id": "ws_1", "action": {"query": "SECRET-QUERY"}}])])

    send(client, token)

    blocks = trace(client, token)["steps"][0]["blocks"]
    assert blocks == [{"kind": "other", "type": "web_search_call"}]
    assert "SECRET-QUERY" not in json.dumps(trace(client, token))


# --- the proxy token is redacted by value --------------------------------------------------------


def test_the_proxy_token_is_replaced_in_a_function_output_and_a_function_call_and_a_message():
    client, token, _, upstream = make([])  # the token exists only after make(): build the reply around it
    upstream.responses = [
        json_reply(
            [
                call_item("Bash", {"command": f"echo {token}"}),
                message_item(f"the key is {token}"),
            ]
        )
    ]

    send(client, token, body(input=[output_item("t", f"OPENAI_API_KEY={token}")]))

    record = trace(client, token)
    dumped = json.dumps(record)
    assert token not in dumped
    assert token[:12] not in dumped
    texts = [b["text"] for b in record["steps"][0]["blocks"]]
    assert texts == [
        '{"command": "echo [proxy-token-redacted]"}',
        "the key is [proxy-token-redacted]",
        "OPENAI_API_KEY=[proxy-token-redacted]",
    ]


def test_the_token_is_replaced_before_a_block_is_cut_so_a_prefix_cannot_survive():
    client, token, _, upstream = make([])
    upstream.responses = [json_reply([message_item("ok")])]
    long_result = "x" * 7_988 + token  # 12 characters of the token sit inside the 8,000-character cut

    send(client, token, body(input=[output_item("t", long_result)]))

    block = trace(client, token)["steps"][0]["blocks"][1]
    assert block["truncated"] is True and len(block["text"]) == 8_000
    assert token[:8] not in block["text"]


def test_the_token_in_a_call_id_and_a_tool_name_is_redacted_too():
    client, token, _, upstream = make([])
    upstream.responses = [json_reply([{"type": "function_call", "name": f"n-{token}", "arguments": "{}", "call_id": f"c-{token}"}])]

    send(client, token)

    assert token not in json.dumps(trace(client, token))


# --- bounds, enforced in the proxy's memory ------------------------------------------------------


def test_more_than_400_calls_keep_content_for_400_and_measured_fields_only_after():
    client, token, _, _ = make([json_reply([message_item("step")], usage=usage_obj(10, 5))])

    for _ in range(405):
        assert send(client, token).status_code == 200

    record = trace(client, token)
    assert record["calls_total"] == 405 and len(record["steps"]) == 405
    assert [s["content"] for s in record["steps"][:400]] == ["recorded"] * 400
    assert [s["content"] for s in record["steps"][400:]] == ["dropped"] * 5
    late = record["steps"][400]
    assert late["blocks"] == [] and late["ordinal"] == 401 and late["tokens"] == 15 and late["model"] == "gpt-5-mini"
    assert record["content_truncated"] is True
    assert sum(s["tokens"] for s in record["steps"]) == usage(client, token)["tokens"] == 405 * 15


def test_one_oversized_block_is_cut_and_marked():
    client, token, _, _ = make([json_reply([message_item("A" * 20_000)])])

    send(client, token, body(input=[output_item("t", "B" * 9_000)]))

    blocks = trace(client, token)["steps"][0]["blocks"]
    assert [(len(b["text"]), b["chars"], b["truncated"]) for b in blocks] == [(8_000, 20_000, True), (8_000, 9_000, True)]


def test_a_block_exactly_at_the_limit_is_not_marked():
    client, token, _, _ = make([json_reply([message_item("A" * 8_000)])])

    send(client, token)

    block = trace(client, token)["steps"][0]["blocks"][0]
    assert (len(block["text"]), block["truncated"]) == (8_000, False)


def test_a_large_function_call_argument_string_is_cut_like_any_block():
    client, token, _, _ = make([json_reply([{"type": "function_call", "name": "Write", "arguments": "C" * 10_000, "call_id": "c"}])])

    send(client, token)

    block = trace(client, token)["steps"][0]["blocks"][0]
    assert (len(block["text"]), block["chars"], block["truncated"]) == (8_000, 10_000, True)


def test_the_two_megabyte_total_stops_recording_content_before_the_call_limit():
    client, token, _, _ = make([json_reply([message_item("z" * 8_000)])])

    for _ in range(300):  # ~8.2 KB each: the 2 MB total is spent near call 255, well before 400
        send(client, token)

    record = trace(client, token)
    recorded = [s for s in record["steps"] if s["content"] == "recorded"]
    dropped = [s for s in record["steps"] if s["content"] == "dropped"]
    assert dropped and recorded and len(recorded) < 300
    assert sum(len(json.dumps(s, ensure_ascii=False)) for s in recorded) <= 2 * 1024 * 1024
    assert [s["ordinal"] for s in dropped] == list(range(len(recorded) + 1, 301))
    assert record["content_truncated"] is True


def test_calls_past_the_step_ceiling_fold_into_totals_so_sums_still_reconcile(monkeypatch):
    monkeypatch.setattr(llm_proxy, "TRACE_MAX_STEP_ENTRIES", 5)
    client, token, _, _ = make([json_reply([message_item("s")])])

    for _ in range(8):
        send(client, token)

    record = trace(client, token)
    assert (record["calls_total"], len(record["steps"]), record["omitted_calls"]) == (8, 5, 3)
    assert sum(s["tokens"] for s in record["steps"]) + record["omitted_tokens"] == usage(client, token)["tokens"]


# --- independence from metering ------------------------------------------------


def test_content_that_cannot_be_parsed_is_an_unparsed_step_and_the_call_is_metered_normally():
    bad = (200, json.dumps({"model": "gpt-5-mini", "output": "nope", "usage": usage_obj(100, 50)}).encode(), "application/json")
    client, token, _, _ = make([bad])

    resp = send(client, token)

    assert resp.status_code == 200
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"]) == (150, False)
    step = trace(client, token)["steps"][0]
    assert step["content"] == "unparsed" and step["blocks"] == []
    assert (step["tokens"], step["model"]) == (150, "gpt-5-mini")


def test_an_output_with_a_non_object_item_is_an_unparsed_step():
    client, token, _, _ = make([json_reply(["not an object"])])

    send(client, token)

    assert trace(client, token)["steps"][0]["content"] == "unparsed"
    assert usage(client, token)["tokens"] == 15


def test_a_parser_that_raises_is_an_unparsed_step_and_metering_is_untouched(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the trace parser exploded")

    monkeypatch.setattr(llm_proxy_openai, "_record_items", boom)
    client, token, _, _ = make([json_reply([message_item("hi")])])

    resp = send(client, token)

    assert resp.status_code == 200
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"], metered["unmetered_reason"]) == (15, False, None)
    assert trace(client, token)["steps"][0]["content"] == "unparsed"


def test_a_capture_that_raises_outright_cannot_reach_metering_or_the_response(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the whole trace capture failed")

    monkeypatch.setattr(llm_proxy_openai, "_capture_trace", boom)
    client, token, _, _ = make([json_reply([message_item("hi")])])

    first = send(client, token)
    second = send(client, token)  # and the token is not poisoned for the next call

    assert (first.status_code, second.status_code) == (200, 200)
    metered = usage(client, token)
    assert (metered["tokens"], metered["unmetered"]) == (30, False)
    assert trace(client, token)["steps"] == []


def test_an_unmeterable_response_still_fails_closed_and_the_trace_does_not_mask_it():
    no_usage = json_reply([message_item("free lunch")], usage=None)
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

    monkeypatch.setattr(llm_proxy_openai, "_capture_trace", boom)
    client, token, _, _ = make([(200, b"not json at all", "application/json")])

    send(client, token)

    assert usage(client, token)["unmetered"] is True


def test_an_sse_stream_with_no_terminal_event_is_unmetered_and_has_an_unparsed_step():
    client, token, _, _ = make([(200, sse([{"type": "response.output_text.delta", "delta": "x"}]), "text/event-stream")])

    send(client, token, body())

    assert usage(client, token)["unmetered"] is True
    assert trace(client, token)["steps"][0]["content"] == "unparsed"


# --- the admin read ------------------------------------------------------------------------------


def test_the_trace_read_needs_the_admin_secret_and_a_known_token():
    client, token, _, _ = make([json_reply()])

    assert client.get(f"/_forge_admin/tokens/{token}/trace").status_code == 403
    assert client.get(f"/_forge_admin/tokens/{token}/trace", headers={"X-Forge-Admin-Secret": "wrong"}).status_code == 403
    assert client.get(f"/_forge_admin/tokens/{token}/trace", headers={"authorization": f"Bearer {token}"}).status_code == 403
    assert client.get("/_forge_admin/tokens/no-such-token/trace", headers=ADMIN).status_code == 404
    assert trace(client, token) == {
        "steps": [],
        "calls_total": 0,
        "content_truncated": False,
        "omitted_calls": 0,
        "omitted_tokens": 0,
        "omitted_cost_usd": 0.0,
    }


def test_the_trace_path_is_just_another_refused_endpoint_on_the_proxy_path():
    client, token, _, upstream = make([json_reply()])

    resp = send(client, token, method="GET", path="/v1/responses/trace")

    assert resp.status_code == 403 and "is not allowed" in resp.json()["error"]["message"]
    assert upstream.calls == []
