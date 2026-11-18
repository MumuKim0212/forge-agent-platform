"""The `openai` dialect of the LLM egress proxy (ADR 0035 §Decision 3-4).

The engine-neutral core (token registry, budget, unmetered fail-closed, the
admin endpoints, the trace bounds) is `llm_proxy.py`. This module supplies only
what is specific to the OpenAI Responses API, as an `llm_proxy.Dialect`:

- Endpoint allowlist: `POST v1/responses`, no query string. Nothing else is
  forwarded (not `/v1/responses/compact`, not conversations, not files, not chat
  completions). Reference design: OpenAI's own `codex-responses-api-proxy`.
- Request policy, an allowlist of shapes (ADR 0029 §2 for this engine): every
  tool is `type: "function"`, the request holds no server-side state
  (`previous_response_id`, `conversation`, `background`, `prompt` are refused,
  `store` must be `false`, `service_tier` must be `"default"`), every input ITEM
  is of a type the pinned runner is measured to send (so an `item_reference`,
  which reads a stored item by id, is refused), and no input content part makes the
  provider fetch an address or open provider-side storage.
- Upstream auth: `Authorization: Bearer <real key>`. The inbound proxy token is
  stripped by the core and never forwarded.
- Metering and the price table (the proxy is the only cost source, ADR 0014
  §Amendment): `input_tokens` INCLUDES cached tokens, reasoning is inside
  `output_tokens`; the model and the processing tier are read from the response.
- Trace capture (ADR 0035 §Decision 3): `message` text, `function_call`
  as requested, `reasoning` as presence and length only (never its text and never
  `encrypted_content`), and the last turn's `function_call_output` as reported.

Imports only the core and the standard library: this file runs inside the standalone
proxy container, which ships `app/` but not the platform's configuration.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import httpx
from fastapi import Response

from app.services.llm_proxy import (
    CONTENT_RECORDED,
    CONTENT_UNPARSED,
    POLICY_REFUSAL_PREFIX,
    Dialect,
    TokenRegistry,
    _bounded_field,
    _bounded_text,
    _count,
    _DuplicateKeyError,
    _iso,
    _reject_duplicate_keys,
    _shown,
    _sse_events,
    _UnmeterableUsage,
    _UnparseableContent,
    endpoint_refusal,
    model_refusal,
    unknown_field_refusal,
)

logger = logging.getLogger("app.llm_proxy.openai")

_DEFAULT_UPSTREAM_BASE_URL = "https://api.openai.com"

# ---------------------------------------------------------------------------
# Endpoint allowlist
# ---------------------------------------------------------------------------

# The ONLY (method, path) pair forwarded, with the raw query strings allowed on
# it: none. `path` has no leading slash and matching is exact, so
# `v1/responses/compact`, `v1/responses/resp_1`, `v1/responses/` and
# `v1/chat/completions` are all different paths and all refused.
ALLOWED_ENDPOINTS: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "v1/responses"): frozenset({""}),
}


def check_endpoint_policy(method: str, path: str, query: str) -> str | None:
    return endpoint_refusal(ALLOWED_ENDPOINTS, method, path, query)


# ---------------------------------------------------------------------------
# Request policy
# ---------------------------------------------------------------------------

# The only tool `type` forwarded: a function the CLIENT executes. Every other
# value is hosted by the provider (web search, file search, code interpreter,
# image generation, computer use, an MCP connector, ...) or ambiguous about where
# it runs (`shell`, `custom`, `namespace`, `apply_patch`, `local_shell`,
# `tool_search`): a channel that leaves the sandbox over the one path default-deny
# egress does not cover. An allowlist, not a denylist -- the next hosted tool the
# provider ships is refused by default, and so is a tool with no `type` at all.
_ALLOWED_TOOL_TYPES = frozenset({"function"})

# The top-level request fields the `openai` dialect forwards: an ALLOWLIST, and exactly the fields
# the pinned runner (openai-agents 0.23.1 / openai 3.28.0) was MEASURED to send (2026-10-28:
# the real staged `agent_runner_openai.py` in the pinned `forge-agent-sandbox-openai` image against a
# recording server, the simple tool round trip and the rich session with reasoning items and parallel
# calls, 5 requests; `test_openai_engine_up_integration.py` records the same set from the engine-up
# run). Any other top-level field is refused, even null or empty. It replaces a denylist of the four
# that keep or read state on the provider's side (`previous_response_id`, `conversation`,
# `background`, `prompt`), which would have forwarded the fifth. NOT on the list, so refused, among
# the many the Responses API has: those four, `stream`, `tool_choice`, `parallel_tool_calls`,
# `reasoning`, `temperature`, `max_output_tokens`, `truncation`, `metadata`, `user`, `text`.
# `store` and `service_tier` are on it but their VALUE is checked below (`false`, `"default"`), as is
# every value of `include` and every `tools` entry. A `tool_choice` is not admitted: the runner does
# not send one, and an edit that admits it must also check its `type` (it can name a hosted tool the
# `tools` list never declared). Adding a field is a deliberate edit here, after the wire test is
# re-run against the pinned runner.
OPENAI_ALLOWED_TOP_LEVEL_FIELDS = frozenset(
    {"model", "input", "instructions", "tools", "store", "service_tier", "include"}
)

# `include` asks the provider to expand extra data into its OUTPUT. It is admitted by an
# explicit allowlist, not by omission: if present it must be a
# list whose every value is one of these, and anything else is refused and never forwarded.
# Today the one value is `reasoning.encrypted_content`, which the OpenAI runner sends so a
# `store=false` second turn can carry the reasoning item back statelessly; the trace records
# a reasoning item as its length only, never this blob (ADR 0035 §Decision 3). Adding a value
# is a deliberate edit here, with a test; the values that expand hosted-tool results or
# fetched images (`web_search_call.action.sources`, `file_search_call.results`,
# `code_interpreter_call.outputs`, `message.input_image.image_url`, ...) are not on it.
_ALLOWED_INCLUDE_VALUES = frozenset({"reasoning.encrypted_content"})

# `input` content parts that make the provider fetch an address the agent chose
# (an exfiltration channel through the query string, an injection channel through
# the response) or open provider-side storage. An inline `data:` image is the one
# image form a build could need.
_FILE_REFERENCE_FIELDS = {"input_image": ("file_id",), "input_file": ("file_id", "file_url")}

# How a refusal names the part (the article is part of the literal the tests pin). A
# `computer_screenshot` is what a `computer_call_output` carries, and it has an `image_url`
# and a `file_id` exactly as an `input_image` does.
_PART_NAMES = {"input_image": "an 'input_image'", "computer_screenshot": "a 'computer_screenshot'"}

# The `type` of every item of `input` that is forwarded. An ALLOWLIST,
# not a refusal of `item_reference` by name: `input` is an open set (the Responses API has about
# thirty item types, and ships more), and the one that matters here -- an `item_reference`, which
# makes the provider read an already-stored item by id into the model's context -- is the same class
# as `previous_response_id` and `conversation` (server-side state, ADR 0029 / 0035). The four types
# are MEASURED, not guessed: the pinned runner (openai-agents 0.23.1 / openai 3.28.0) replays exactly
# `reasoning`, `message` (the assistant's own, from an earlier turn), `function_call` and
# `function_call_output`, over a session in which the model answers with a reasoning item, text and
# two parallel function calls (`tests/test_openai_sdk_wire_integration.py` re-derives it, so an SDK
# bump that replays another type is a red test before it is a refused build). The first item of
# every request is the task text as `{"role": "user", "content": ...}` with NO `type`; that shape
# (a role message) is the one typeless item admitted, see `_ROLES`. Adding a type is a deliberate
# edit here, with a test; it is a type the runner is measured to send, never one it could.
_ALLOWED_INPUT_ITEM_TYPES = frozenset({"message", "reasoning", "function_call", "function_call_output"})

# The `role` of an item that carries no `type` (the Responses API reads it as a message). A typeless
# item with no role is not a shape the API defines, and is refused rather than guessed at.
_ROLES = frozenset({"user", "assistant", "system", "developer"})


def check_request_policy(body: bytes) -> str | None:
    """Returns the refusal `detail` for a request body that asks the provider to
    act outside the sandbox, or `None` if it may be forwarded. Every condition has
    its own `detail`, each starting `POLICY_REFUSAL_PREFIX`.

    - the body must be one JSON object with no repeated key (a body the proxy
      cannot read is a body it cannot vouch for -- including an empty or a
      compressed one);
    - every top-level field is on `OPENAI_ALLOWED_TOP_LEVEL_FIELDS` (what the pinned runner
      was measured to send), so `previous_response_id`, `conversation`, `background`, `prompt`,
      `tool_choice` and every other field are refused;
    - `store` present and `false`; `service_tier` present and `"default"`;
    - `include`, if present, a list of values each in `_ALLOWED_INCLUDE_VALUES`;
    - `tools`, if present, a list of objects each `type: "function"`;
    - every item of `input` is one the runner is measured to send (`_ALLOWED_INPUT_ITEM_TYPES`, or a
      typeless role message), which refuses an `item_reference` among the rest;
    - no `input_image` / `input_file` / `computer_screenshot` part references a file id or a URL,
      at any depth of `input`;
    - `model` is a model with a price row (checked last, so a body refused for another
      reason keeps naming that reason).
    """
    if not body:
        return f"{POLICY_REFUSAL_PREFIX}request body is empty"

    try:
        parsed = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        return f"{POLICY_REFUSAL_PREFIX}request body repeats the key {str(exc)[:64]!r}"
    except (ValueError, RecursionError):
        return f"{POLICY_REFUSAL_PREFIX}request body is not valid JSON"

    if not isinstance(parsed, dict):
        return f"{POLICY_REFUSAL_PREFIX}request body is not a JSON object"

    unknown = unknown_field_refusal(parsed, OPENAI_ALLOWED_TOP_LEVEL_FIELDS)
    if unknown is not None:
        return unknown

    if parsed.get("store") is not False:
        return f"{POLICY_REFUSAL_PREFIX}request field 'store' must be present and false"
    if parsed.get("service_tier") != "default":
        return f"{POLICY_REFUSAL_PREFIX}request field 'service_tier' must be present and 'default'"

    if "include" in parsed:
        include = parsed["include"]
        if not isinstance(include, list):
            return f"{POLICY_REFUSAL_PREFIX}'include' is not a list"
        for value in include:
            if not isinstance(value, str) or value not in _ALLOWED_INCLUDE_VALUES:
                shown = value[:64] if isinstance(value, str) else type(value).__name__
                return f"{POLICY_REFUSAL_PREFIX}include value {shown!r} is not allowed"

    if "tools" in parsed:
        tools = parsed["tools"]
        if not isinstance(tools, list):
            return f"{POLICY_REFUSAL_PREFIX}'tools' is not a list"
        for tool in tools:
            if not isinstance(tool, dict):
                return f"{POLICY_REFUSAL_PREFIX}a 'tools' entry is not an object"
            tool_type = tool.get("type")
            if not isinstance(tool_type, str) or tool_type not in _ALLOWED_TOOL_TYPES:
                shown = tool_type[:64] if isinstance(tool_type, str) else type(tool_type).__name__
                return f"{POLICY_REFUSAL_PREFIX}tool type {shown!r} is not allowed"

    return (
        _check_input_items(parsed.get("input"))
        or _check_input_references(parsed.get("input"))
        or model_refusal(parsed.get("model"), is_priced_model)
    )


def _check_input_items(items: object) -> str | None:
    """every item of an `input` LIST is an object whose `type` is on
    `_ALLOWED_INPUT_ITEM_TYPES`, or a role message with no `type`. The refusal names the type
    (bounded to 64 characters, like the tool types above), so an `item_reference` -- a read of an
    item the provider stored, by id -- reads `input item type 'item_reference' is not allowed`.
    A string `input` is the task text itself and is not an item list."""
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            return f"{POLICY_REFUSAL_PREFIX}an 'input' entry is not an object"
        if "type" not in item:
            if isinstance(item.get("role"), str) and item["role"] in _ROLES:
                continue
            return f"{POLICY_REFUSAL_PREFIX}an 'input' item has no type and is not a role message"
        item_type = item["type"]
        if not isinstance(item_type, str) or item_type not in _ALLOWED_INPUT_ITEM_TYPES:
            shown = item_type[:64] if isinstance(item_type, str) else type(item_type).__name__
            return f"{POLICY_REFUSAL_PREFIX}input item type {shown!r} is not allowed"
    return None


def _check_input_references(items: object) -> str | None:
    """`input_image` / `input_file` / `computer_screenshot` parts anywhere in `input` -- in a
    message's `content`, in a `function_call_output`'s `output` list, or in a
    `computer_call_output`'s `output` object -- may carry only inline data. Does not descend
    into a `function_call`'s `arguments`, which is an agent-authored string and not a part
    the provider interprets.

    This walk follows the Responses input grammar. A message's `content` and
    a `*_call_output`'s `output` are lists of FLAT parts (`input_text`, `input_image`,
    `input_file`), so the first version's depth of one reached every list. Two things it
    missed, both closed here: a `computer_call_output`'s `output` is a single OBJECT (a
    `computer_screenshot` with an `image_url` and a `file_id`), which the list-only walk
    skipped; and nothing stopped it being one level shallow the day a part carries parts of
    its own. So the walk now follows every `content` / `output` that is a list (or, for
    `output`, an object) on an item or on any part, as a worklist (no recursion; the body is
    bounded). Only the item types the runner is measured to send reach this walk
    (`_check_input_items` runs first), so a `computer_call_output` is refused by its type before
    it is walked; the object-valued `output` branch stays as the second layer for the day the
    allowlist widens, and still covers a part that carries an object `output` of its own."""
    if not isinstance(items, list):
        return None
    pending: list[object] = []

    def descend(node: dict) -> None:
        for name in ("content", "output"):
            value = node.get(name)
            if isinstance(value, list):
                pending.extend(value)
            elif name == "output" and isinstance(value, dict):
                pending.append(value)

    for item in items:
        if isinstance(item, dict):
            descend(item)
    while pending:
        part = pending.pop()
        if not isinstance(part, dict):
            continue
        descend(part)
        part_type = part.get("type")
        if part_type in ("input_image", "computer_screenshot"):
            url = part.get("image_url")
            if part.get("file_id") is not None or (
                isinstance(url, str) and not url.lower().startswith("data:")
            ):
                return f"{POLICY_REFUSAL_PREFIX}{_PART_NAMES[part_type]} part may not reference a URL or a file"
            if url is not None and not isinstance(url, str):
                return f"{POLICY_REFUSAL_PREFIX}{_PART_NAMES[part_type]} part may not reference a URL or a file"
        elif part_type == "input_file":
            for name in _FILE_REFERENCE_FIELDS["input_file"]:
                if part.get(name) is not None:
                    return f"{POLICY_REFUSAL_PREFIX}an 'input_file' part may not reference a URL or a file"
    return None


def _policy_refusal_response(detail: str) -> Response:
    """403 in the OpenAI error envelope. The `message` is the whole refusal
    `detail`, so it still starts `POLICY_REFUSAL_PREFIX` and the executor's
    failure message (which reads the detail the proxy recorded) is unchanged."""
    payload = {"error": {"message": detail, "type": "invalid_request_error", "code": "forge_policy_refusal"}}
    return Response(status_code=403, content=json.dumps(payload), media_type="application/json")


def _auth_headers(real_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {real_key}"}


# ---------------------------------------------------------------------------
# Headers (ADR 0014 §Amendment 2026-10-17 point 3)
# ---------------------------------------------------------------------------

# Request headers forwarded upstream: exactly the ones the PINNED OpenAI Agents SDK
# (openai-agents 0.23.1 over openai 3.28.0) was measured to send on `POST /v1/responses`,
# 2026-10-19, from the pinned `forge-agent-sandbox-openai` image pointed at a recording
# server (two calls of a tool round trip). Left out of that set: `authorization` (the
# proxy's own is added), `host`, `content-length` and `connection` (transport), and
# `accept-encoding` (the proxy's own client negotiates the encodings it can decode).
#
# NEVER forwarded, because they are not on the list and the pinned SDK sends neither unless
# the process is configured with `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID`, which the runner is
# not: `OpenAI-Organization` and `OpenAI-Project`. Either one would bill a different
# organisation or project than the one the platform's key belongs to. They are dropped
# silently, not refused: a header name outside an allowlist needs no refusal, and a
# refusal would fail a build over a header the real runner does not send anyway.
OPENAI_INBOUND_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "user-agent",
        "x-stainless-arch",
        "x-stainless-async",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-read-timeout",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
    }
)

# Response headers returned to L3: what the pinned `openai` client READS (read from its
# source in the pinned image: `_base_client.py` reads `retry-after-ms`, `retry-after` and
# `x-should-retry` to decide a retry, `x-request-id` for an error's text; `content-type`
# picks the JSON or SSE parser). No rate-limit header is read, so none is returned: the
# `x-ratelimit-*` family describes the platform's account. Never returned:
# `openai-organization`, `openai-project`, `openai-processing-ms`, `set-cookie`, `cf-*`.
OPENAI_OUTBOUND_HEADERS = frozenset(
    {"content-type", "x-request-id", "retry-after", "retry-after-ms", "x-should-retry"}
)


def check_headers_policy(lines: list[tuple[str, str]]) -> str | None:
    """No header VALUE is refused for this dialect: its pinned SDK sends nothing whose value
    changes what the provider bills, and the headers that would (`OpenAI-Organization`,
    `OpenAI-Project`) are never forwarded at all."""
    return None


# ---------------------------------------------------------------------------
# Prices and metering
# ---------------------------------------------------------------------------

# List prices, USD per MILLION tokens, standard tier: (input, cached input, output).
# Source: https://developers.openai.com/api/docs/pricing, read 2026-10-11
# (ADR 0035 §Decision 4). A price change is a code change. Matching is on the
# exact model id, or on that id plus a dated-snapshot suffix (`-YYYY-MM-DD`, see
# `_priced_rates`) -- never any other prefix.
#
# THIS TABLE IS ALSO THE MODEL ALLOWLIST (ADR 0014 §Amendment 2026-10-17 point 2;
# ADR 0035 §Amendment 2026-10-17). A request naming a model with no row is refused at the
# proxy as a recorded policy refusal, and a response naming one is unmeterable (fail
# closed). There is no ceiling row: the old one ($1.75 / $14 per MTok) undercounted a
# model on the same endpoint that is priced near $15 / $120.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
}

# Processing-tier multipliers on the whole figure. The tier is the one the
# RESPONSE reports (what was served and billed), not the one requested.
_TIER_MULTIPLIERS = {"default": 1.0, "priority": 2.0, "fast": 2.0, "flex": 0.5}
# Any other tier -- including none reported -- is charged at the dearest
# multiplier, so an unfamiliar tier can only over-count.
_CEILING_TIER_MULTIPLIER = 2.0

# The API reports the model as a dated snapshot (`gpt-5-mini-2025-08-07`) though a
# request names the alias. A snapshot is the same model at the same price, so
# exactly that one suffix is resolved to its base id; nothing else is.
_SNAPSHOT_SUFFIX = re.compile(r"^(?P<base>.+)-\d{4}-\d{2}-\d{2}$")


def _priced_rates(model: object) -> tuple[float, float, float] | None:
    """The price row for `model`, or None when it has none: the exact id, else its base id
    when the only difference is a dated-snapshot suffix."""
    if not isinstance(model, str):
        return None
    rates = _PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        match = _SNAPSHOT_SUFFIX.match(model)
        if match is not None:
            rates = _PRICING_USD_PER_MTOK.get(match.group("base"))
    return rates


def is_priced_model(model: object) -> bool:
    """True when the `openai` dialect has a price row for `model`."""
    return _priced_rates(model) is not None


def _price_usage(model: str | None, tier: object, usage: dict) -> tuple[int, float]:
    """(tokens, cost_usd) for one Responses `usage` object (ADR 0035 §Decision 4).

    `input_tokens` includes the cached tokens, so uncached = input - cached.
    cost = uncached x input rate + cached x cached rate + output x output rate,
    times the tier multiplier. tokens = uncached + output: cached reads are
    priced into the cost but not counted, as in ADR 0014 §Amendment point 4.
    Raises `_UnmeterableUsage` if `input_tokens` / `output_tokens` are missing, any
    count is not a non-negative integer, or `cached_tokens` exceeds `input_tokens`."""
    input_tokens = _count(usage, "input_tokens", required=True)
    output_tokens = _count(usage, "output_tokens", required=True)
    details = usage.get("input_tokens_details")
    cached = _count(details, "cached_tokens") if isinstance(details, dict) else 0
    if cached > input_tokens:
        raise _UnmeterableUsage("usage field 'cached_tokens' exceeds 'input_tokens'")
    uncached = input_tokens - cached

    rates = _priced_rates(model)
    if rates is None:
        # No row, so no price (never the dearest row's). The one exception is a usage that
        # billed nothing, whose cost is 0 at any price.
        if input_tokens or output_tokens:
            shown = _shown(model, 64) if isinstance(model, str) else "(none)"
            raise _UnmeterableUsage(f"the response names model {shown}, which has no price row")
        return 0, 0.0
    in_rate, cached_rate, out_rate = rates
    multiplier = _TIER_MULTIPLIERS.get(tier, _CEILING_TIER_MULTIPLIER) if isinstance(tier, str) else _CEILING_TIER_MULTIPLIER
    cost = uncached * in_rate + cached * cached_rate + output_tokens * out_rate
    return uncached + output_tokens, multiplier * cost / 1_000_000


def _is_sse(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").lower().startswith("text/event-stream")


def _meter(response: httpx.Response) -> tuple[str | None, dict, int, float]:
    """Format is chosen by the response `content-type`: `text/event-stream` is
    parsed as SSE, anything else as JSON. A body that disagrees with its
    content-type fails the parse and is unmetered, not guessed at."""
    body = _response_object_from_sse(response.content) if _is_sse(response) else _response_object_from_json(response.content)
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise _UnmeterableUsage("response has no usage object")
    model = body.get("model")
    tokens, cost_usd = _price_usage(model, body.get("service_tier"), usage)
    return (model if isinstance(model, str) else None), usage, tokens, cost_usd


def _response_object_from_json(content: bytes) -> dict:
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        raise _UnmeterableUsage("response body could not be decoded") from None
    if not isinstance(data, dict):
        raise _UnmeterableUsage("response JSON is not an object")
    return data


# The events that end a stream and carry the finished response object -- usage,
# model and tier included -- under `response`.
_TERMINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")


def _terminal_response(events) -> dict | None:
    """The `response` object of the first terminal event that has one."""
    for event in events:
        if event.get("type") in _TERMINAL_EVENTS and isinstance(event.get("response"), dict):
            return event["response"]
    return None


def _response_object_from_sse(content: bytes) -> dict:
    """The terminal `response` of a buffered `text/event-stream` body. A stream that
    never reached a terminal event, or whose terminal event has no response object,
    is unmetered: the proxy has no figure it can vouch for. (There is no carve-out
    for an `error` event: nothing in what was measured says an OpenAI stream that
    errors generated, or billed, nothing.)"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _UnmeterableUsage("response body could not be decoded") from None
    found = _terminal_response(_sse_events(text))
    if found is None:
        raise _UnmeterableUsage("SSE stream has no terminal response event")
    return found


# ---------------------------------------------------------------------------
# Build trace capture
# ---------------------------------------------------------------------------


def _capture_trace(
    registry: TokenRegistry,
    token: str,
    response: httpx.Response,
    *,
    request_body: bytes,
    started_at: datetime | None,
    ended_at: datetime | None,
    metered: tuple[str | None, dict, int, float] | None,
) -> None:
    """Records one step for a forwarded 2xx Responses call, in the same step shape
    the anthropic dialect records, so the platform side reads either.

    Measured fields come from what metering read (None when it could not meter).
    Content is the response's output items plus the `function_call_output` items of
    the request's last turn -- the history the client re-sends on every call is
    never re-recorded. A parse failure of the content is not an error here: the
    step is recorded `unparsed` with its measured fields."""
    step: dict = {
        "ordinal": 0,  # assigned by the registry
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "tokens": None,
        "cost_usd": None,
        "stop_reason": None,
        "content": CONTENT_UNPARSED,
        "thinking": [],
        "blocks": [],
    }
    if metered is not None:
        model, usage, tokens, cost_usd = metered
        step["model"] = _bounded_field(model, token)
        step["tokens"] = tokens
        step["cost_usd"] = cost_usd
        step.update(_usage_fields(usage))
        try:
            body = _response_object_from_sse_or_json(response)
            items = body.get("output")
            if not isinstance(items, list):
                raise _UnparseableContent("response has no output list")
            blocks, thinking = _record_items(items, _last_turn_function_outputs(request_body), token)
        except Exception as exc:  # noqa: BLE001 - see the docstring: unparsed, not an error
            logger.warning("llm proxy: a call's content could not be parsed (%s)", type(exc).__name__)
        else:
            step["stop_reason"] = _bounded_field(_stop_reason(body), token)
            step["blocks"] = blocks
            step["thinking"] = thinking
            step["content"] = CONTENT_RECORDED
    registry._append_step(token, step)


def _response_object_from_sse_or_json(response: httpx.Response) -> dict:
    if _is_sse(response):
        events = list(_sse_events(response.content.decode("utf-8")))
        found = _terminal_response(events)
        if found is None:
            raise _UnparseableContent("SSE stream has no terminal response event")
        if not isinstance(found.get("output"), list):
            # A terminal event that does not carry the output items: rebuild them from
            # the per-item `response.output_item.done` events, in `output_index` order.
            done = [
                e for e in events if e.get("type") == "response.output_item.done" and isinstance(e.get("item"), dict)
            ]
            done.sort(key=lambda e: e["output_index"] if isinstance(e.get("output_index"), int) else 0)
            found = {**found, "output": [e["item"] for e in done]}
        return found
    data = json.loads(response.content)
    if not isinstance(data, dict):
        raise _UnparseableContent("response JSON is not an object")
    return data


def _stop_reason(body: dict) -> str | None:
    """`status`, or for an incomplete response the reason it gave."""
    status = body.get("status")
    details = body.get("incomplete_details")
    if status == "incomplete" and isinstance(details, dict) and isinstance(details.get("reason"), str):
        return f"incomplete:{details['reason']}"
    return status if isinstance(status, str) else None


def _usage_fields(usage: dict) -> dict:
    """The per-call token counts of a usage object `_price_usage` already read.
    The same reading rules, so the parts agree with the priced total.
    `input_tokens` is the UNCACHED part, `cache_read_tokens` the cached part and
    `cache_write_tokens` is the count OpenAI reports as written to its cache --
    recorded as reported and priced at the input rate with the rest of the input
    (ADR 0035 §Decision 4 names no separate write rate)."""
    details = usage.get("input_tokens_details")
    cached = _count(details, "cached_tokens") if isinstance(details, dict) else 0
    written = _count(details, "cache_write_tokens") if isinstance(details, dict) else 0
    return {
        "input_tokens": _count(usage, "input_tokens") - cached,
        "output_tokens": _count(usage, "output_tokens"),
        "cache_read_tokens": cached,
        "cache_write_tokens": written,
    }


def _last_turn_function_outputs(request_body: bytes) -> list[dict]:
    """The `function_call_output` items of the LAST turn of a Responses request's
    `input`: those after the last item the model itself produced (a
    `function_call`, a `reasoning` item or an assistant `message`). Everything
    earlier in `input` was recorded on an earlier call. A string `input` has none."""
    if not request_body:
        return []
    parsed = json.loads(request_body)
    items = parsed.get("input") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    outputs: list[dict] = []
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("function_call", "reasoning") or (
            (kind == "message" or kind is None) and item.get("role") == "assistant"
        ):
            break
        if kind == "function_call_output":
            outputs.append(item)
    outputs.reverse()
    return outputs


def _record_items(
    output_items: list, function_outputs: list[dict], token: str
) -> tuple[list[dict], list[dict]]:
    """(content blocks, reasoning markers). Reasoning text is never kept, nor its
    `encrypted_content`: only that it was there, and how long (ADR 0035 §Decision 3)."""
    blocks: list[dict] = []
    thinking: list[dict] = []
    for item in output_items:
        if not isinstance(item, dict):
            raise _UnparseableContent("an output item is not an object")
        kind = item.get("type")
        if kind == "message":
            blocks.append({"kind": "assistant_text", **_bounded_text(_message_text(item.get("content")), token)})
        elif kind == "function_call":
            arguments = item.get("arguments")
            rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, default=str)
            blocks.append(
                {
                    "kind": "tool_use",
                    "id": _bounded_field(item.get("call_id"), token),
                    "name": _bounded_field(item.get("name"), token),
                    **_bounded_text(rendered, token),
                }
            )
        elif kind == "reasoning":
            thinking.append({"chars": _reasoning_length(item)})
        else:
            blocks.append({"kind": "other", "type": _bounded_field(kind, token)})
    for item in function_outputs:
        blocks.append(
            {
                "kind": "tool_result",
                "tool_use_id": _bounded_field(item.get("call_id"), token),
                "is_error": None,  # the Responses API has no error flag on a function_call_output
                **_bounded_text(_output_text(item.get("output")), token),
            }
        )
    return blocks, thinking


def _message_text(content: object) -> str:
    """A `message` item's text: its `output_text` parts, and a `refusal` part's
    refusal text (the model's own words), joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
            parts.append(part["refusal"])
    return "".join(parts)


def _reasoning_length(item: dict) -> int:
    """How long a `reasoning` item was, never what it said: its summary's length
    when it has one, else the length of its opaque `encrypted_content` (the
    counterpart of an anthropic `redacted_thinking` block)."""
    summary = item.get("summary")
    if isinstance(summary, list):
        total = sum(len(p["text"]) for p in summary if isinstance(p, dict) and isinstance(p.get("text"), str))
        if total:
            return total
    encrypted = item.get("encrypted_content")
    return len(encrypted) if isinstance(encrypted, str) else 0


def _output_text(output: object) -> str:
    """A `function_call_output`'s `output` as plain text: a string as is; a list of
    parts with text parts joined and anything else (an image, a file) named by its type."""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for part in output:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, dict):
            parts.append(f"[{_shown(str(part.get('type')), 32)} block]")
    return "\n".join(parts)


OPENAI_DIALECT = Dialect(
    name="openai",
    key_env_var="FORGE_PROXY_OPENAI_API_KEY",
    default_upstream_base_url=_DEFAULT_UPSTREAM_BASE_URL,
    check_endpoint=check_endpoint_policy,
    check_request=check_request_policy,
    upstream_auth_headers=_auth_headers,
    refusal_response=_policy_refusal_response,
    meter=_meter,
    capture_trace=lambda *args, **kwargs: _capture_trace(*args, **kwargs),
    check_headers=check_headers_policy,
    inbound_headers=OPENAI_INBOUND_HEADERS,
    outbound_headers=OPENAI_OUTBOUND_HEADERS,
)
