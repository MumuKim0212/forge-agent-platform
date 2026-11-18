"""LLM egress proxy (L2), ADR 0014.

Sits between an L3 sandbox running the agent and the real model API.
Holds the real API key in its own process environment only — the sandbox
never sees it. The sandbox gets a base URL pointed at this proxy and a
per-build ephemeral proxy token in place of the real key.

Engine-neutral core plus one dialect per engine (ADR 0035 §Decision 3). THIS
module is the core -- the token registry, the budget, the unmetered
fail-closed rule, the admin endpoints including `/trace`, the trace bounds, and
the request pipeline in `create_app` -- and it also holds the `anthropic`
dialect (the Messages API: endpoint allowlist, request-shape policy, `x-api-key`
injection, usage parsing, price table, trace parser) in its own section at the
end. The `openai` dialect lives in `llm_proxy_openai.py`. A `Dialect` is the
engine-specific half; `create_app` takes one (default `anthropic`), and a proxy
container runs exactly one dialect with that dialect's one key
(`proxy_dialects.py` names them).

Responsibilities (ADR 0014):
- Issue a random per-build proxy token when a build's sandbox starts;
  revoke it at build end. A revoked or unknown token gets 401/403.
- Swap a valid proxy token for the real Anthropic API key on every
  forwarded request; the real key is never logged or echoed back.
- Enforce a per-build token/cost budget; once spent, reject further LLM
  calls (the executor maps this to `BUDGET_EXCEEDED`).
- Record usage (tokens, cost) per build so the executor can write it onto
  `Build.cost_tokens` / `Build.cost_usd` at build end, including on
  failure. The proxy is the ONLY source of those figures (ADR 0014 §Amendment
  2026-10-01): it meters JSON and SSE (`text/event-stream`) Messages
  responses, prices cache reads/writes, and fails closed -- marks the token
  unmetered and refuses every later request -- on a 2xx response it cannot
  read usage from.
- Refuse any request that asks the provider to act outside the sandbox
  (ADR 0029 §2): this is the one L3 -> internet path that default-deny
  egress does not cover, because the provider makes the outbound call. The
  rule (`check_request_policy`) is an allowlist of request shapes, applies to
  every body the proxy forwards, never forwards a refused request, spends no
  budget on it, and records it against the token so the executor can fail
  the build closed with a message that names the condition.

- ADR 0014 §Amendment 2026-10-17: ONE upstream call is in flight per token (the
  request takes the token's slot, THEN checks the budget, forwards, records, and releases it,
  on every path), so the next request's budget check sees the previous call's cost; the
  price table is also the MODEL ALLOWLIST (a request naming a model with no row is refused,
  a response naming one is unmeterable, and there is no "ceiling" price); request and
  response HEADERS pass by per-dialect allowlist built from what the pinned SDKs are measured
  to send and read, and an unmeasured `anthropic-beta` value is refused; and the content walk
  follows every nested `content` list.

- Record a bounded per-call build trace beside the usage figures: measured fields per forwarded Messages
  call, plus the content NEW in that call. The trace is observability: parsing
  it never shares a failure with metering (`_account_usage` meters first, and
  captures the trace behind its own boundary), and its bounds are enforced here,
  in memory, as it records.

This module intentionally has no FastAPI dependency on the platform DB —
`TokenRegistry` is a small in-process store owned by whichever process runs
the proxy (the worker, in this slice). It is not itself a `BuildExecutor`;
`sandbox_build_executor.py` starts one proxy instance per build and reads
its accounted usage after the sandbox run finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

logger = logging.getLogger("app.llm_proxy")

# No dependency on app.config here deliberately: this module runs inside
# the standalone proxy container (docker/llm-proxy/Dockerfile), which ships
# only this file's own imports, not the full platform app/config.py (which
# would pull in pydantic-settings, SQLAlchemy, etc. for no reason). The
# proxy process gets its configuration from plain env vars via
# app/proxy_server.py instead.
_DEFAULT_UPSTREAM_BASE_URL = "https://api.anthropic.com"

# List prices, USD per MILLION tokens: (input, output, cache read). Source: the
# `claude-api` skill's model table, cached 2026-09-25 (ADR 0014 §Amendment
# 2026-10-01 point 5). A price change is a code change. Matching is on the exact
# model id, or on that id plus a dated-snapshot suffix (`-YYYYMMDD`, see
# `_priced_rates`) -- never any other prefix.
#
# THIS TABLE IS ALSO THE MODEL ALLOWLIST (ADR 0014 §Amendment 2026-10-17
# point 2). A request naming a model with no row is refused at the proxy as a
# recorded policy refusal, and a response naming one is unmeterable (fail closed).
# There is no ceiling row: providers sell models priced above any row we hold, so a
# "most expensive row" is not a ceiling, only a guess that undercounts.
#
# Cache writes are derived, not tabled: 1.25x the input rate for the 5-minute
# TTL, 2x for the 1-hour TTL (`_CACHE_WRITE_5M_MULT` / `_CACHE_WRITE_1H_MULT`).
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-mythos-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0
_FAST_SPEED_MULT = 2.0


class _UnmeterableUsage(ValueError):
    """A usage object that cannot be read; the message is the reason recorded
    against the token."""


# The API reports an alias as the dated snapshot it resolved to (`claude-haiku-4-5` ->
# `claude-haiku-4-5-20251001`). A snapshot is the same model at the same price, so exactly
# that one suffix (eight digits) is resolved to its base id; nothing else is.
_SNAPSHOT_SUFFIX = re.compile(r"^(?P<base>.+)-\d{8}$")


def _priced_rates(model: object) -> tuple[float, float, float] | None:
    """The price row for `model`, or None when it has none: the exact id, else its base
    id when the only difference is a dated-snapshot suffix."""
    if not isinstance(model, str):
        return None
    rates = _PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        match = _SNAPSHOT_SUFFIX.match(model)
        if match is not None:
            rates = _PRICING_USD_PER_MTOK.get(match.group("base"))
    return rates


def is_priced_model(model: object) -> bool:
    """True when the `anthropic` dialect has a price row for `model`."""
    return _priced_rates(model) is not None


def model_refusal(model: object, is_priced: Callable[[object], bool]) -> str | None:
    """The refusal `detail` for a request whose `model` has no price row, or None. Shared by
    both dialects (each passes its own `is_priced`): the price table is the model
    allowlist, so a model with no row is not forwarded, spends nothing, and is recorded."""
    if model is None:
        return f"{POLICY_REFUSAL_PREFIX}the request names no model, and a request with no model cannot be priced"
    if not isinstance(model, str):
        return f"{POLICY_REFUSAL_PREFIX}the request's 'model' is not a string ({type(model).__name__})"
    if not is_priced(model):
        return f"{POLICY_REFUSAL_PREFIX}model '{_shown(model, 64)}' has no price row, so it is not allowed"
    return None


def _count(usage: dict, key: str, *, required: bool = False) -> int:
    """A non-negative integer token count from `usage[key]`. Absent/null is 0
    unless `required`. Anything else (a string, a float, a bool, a negative) is
    unreadable -- a number the proxy cannot vouch for is not silently zeroed."""
    value = usage.get(key)
    if value is None:
        if required:
            raise _UnmeterableUsage(f"usage field {key!r} is missing")
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _UnmeterableUsage(f"usage field {key!r} is not a non-negative integer")
    return value


def _price_usage(model: str | None, usage: dict) -> tuple[int, float]:
    """(tokens, cost_usd) for one Messages `usage` object (ADR 0014 §Amendment
    points 3-4).

    tokens = input + cache_creation + output. Cache READS are priced into the
    cost but not counted as tokens: they re-read tokens already counted.
    Raises `_UnmeterableUsage` if `input_tokens` / `output_tokens` are missing
    or any count is not a non-negative integer."""
    input_tokens = _count(usage, "input_tokens", required=True)
    output_tokens = _count(usage, "output_tokens", required=True)
    cache_read = _count(usage, "cache_read_input_tokens")

    # Cache writes. With the TTL breakdown, 5m at 1.25x and 1h at 2x. Anything
    # the breakdown does not account for (no breakdown at all, or a total larger
    # than its parts) is charged at 2x, the dearer TTL.
    cache_total = _count(usage, "cache_creation_input_tokens")
    breakdown = usage.get("cache_creation")
    write_5m = write_1h = 0
    if isinstance(breakdown, dict):
        write_5m = _count(breakdown, "ephemeral_5m_input_tokens")
        write_1h = _count(breakdown, "ephemeral_1h_input_tokens")
    cache_total = max(cache_total, write_5m + write_1h)
    write_unattributed = cache_total - write_5m - write_1h

    rates = _priced_rates(model)
    if rates is None:
        # No row, so no price. Not the dearest row's price: a model nobody priced fails closed.
        # The one exception is a usage that billed nothing, whose cost is 0 at any price (the
        # provider's own `error` event, which carries no model).
        if input_tokens or output_tokens or cache_read or cache_total:
            shown = _shown(model, 64) if isinstance(model, str) else "(none)"
            raise _UnmeterableUsage(f"the response names model {shown}, which has no price row")
        return 0, 0.0
    in_rate, out_rate, read_rate = rates
    scale = _FAST_SPEED_MULT if usage.get("speed") == "fast" else 1.0
    cost = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read * read_rate
        + write_5m * in_rate * _CACHE_WRITE_5M_MULT
        + (write_1h + write_unattributed) * in_rate * _CACHE_WRITE_1H_MULT
    )
    return input_tokens + cache_total + output_tokens, scale * cost / 1_000_000


@dataclass
class BuildUsage:
    """Accumulated usage for one build's proxy token."""

    tokens: int = 0
    cost_usd: float = 0.0
    budget_exceeded: bool = False
    # ADR 0029 §2: the `detail` of each request refused by the tool
    # policy, in order, capped at `_MAX_RECORDED_REFUSALS`. Refusals spend no
    # tokens and no cost; this list is how the executor learns of them.
    policy_refusals: list[str] = field(default_factory=list)
    # ADR 0014 §Amendment 2026-10-01 point 6: a 2xx Messages response from which
    # no usage could be read. The budget can no longer be enforced, so every
    # later request is refused and the executor fails the build closed. The
    # first reason is kept.
    unmetered: bool = False
    unmetered_reason: str | None = None


# Backend constants, not
# settings. The platform side re-applies the same caps when it stores the record.
TRACE_BLOCK_MAX_CHARS = 8_000  # one content field
TRACE_MAX_CALLS = 400  # calls that keep content; later calls keep measured fields only
TRACE_MAX_BYTES = 2 * 1024 * 1024  # serialised content of all recorded steps
# Measured-only steps are tiny but unbounded in number for a build that loops on
# small calls, so the step LIST has its own ceiling; past it a call is folded into
# the `omitted_*` totals (so the per-call sums still reconcile with the usage).
TRACE_MAX_STEP_ENTRIES = 4_000
TRACE_FIELD_MAX_CHARS = 128  # a tool name, a tool-use id, a stop reason, a model id
# What the per-build proxy token is replaced by wherever it appears in content.
TOKEN_REDACTION_MARKER = "[proxy-token-redacted]"

# `content` of a step: its blocks were recorded / could not be parsed / were
# dropped by a bound (then the step keeps its measured fields only).
CONTENT_RECORDED = "recorded"
CONTENT_UNPARSED = "unparsed"
CONTENT_DROPPED = "dropped"


@dataclass
class _TraceRecord:
    """The build's step record, bounded as it grows."""

    steps: list[dict] = field(default_factory=list)
    calls_total: int = 0
    content_bytes: int = 0
    content_truncated: bool = False
    omitted_calls: int = 0
    omitted_tokens: int = 0
    omitted_cost_usd: float = 0.0


@dataclass
class _TokenEntry:
    build_id: str
    token_budget: int
    cost_budget_usd: float
    usage: BuildUsage = field(default_factory=BuildUsage)
    # Deliberately NOT part of `usage`: the trace and the metering are separate
    # records with separate failures.
    trace: _TraceRecord = field(default_factory=_TraceRecord)
    # ADR 0014 §Amendment 2026-10-17 point 1: the per-token slot. ONE upstream call
    # is in flight per token: a request takes it, THEN checks the budget, forwards, records,
    # and releases it (`async with`, so every path releases it). It belongs to this token
    # alone, so a request waiting on it holds nothing any other token needs. An asyncio lock
    # is used only on the proxy listener's event loop; it is never taken on the admin one.
    slot: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    # How many requests of this token are alive in the proxy right now (reading the body,
    # waiting on the slot, or forwarding). Changed only under the registry's lock
    # (`TokenRegistry._admit` / `_release`), because the drain on the admin listener's thread
    # reads it beside the requests' own thread; the check-and-increment is one step with the
    # `closed` check, so a token that has been closed admits nothing further.
    inflight: int = 0
    # ADR 0014 §Amendment 2026-10-17 follow-up: set by
    # `TokenRegistry.close`. A closed token is still in the registry -- its usage, refusals
    # and trace stay readable, which a revoke would delete -- but `_lookup` no longer returns
    # it, so no new request is admitted and a request that waited on the slot is refused
    # (the re-check inside the slot) instead of being forwarded.
    closed: bool = False


class TokenRegistry:
    """Issues/revokes per-build proxy tokens and tracks usage against a
    per-build budget. Thread-safe: the proxy serves requests from uvicorn's
    worker thread(s) while the executor issues/revokes/reads from the
    worker's main thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _TokenEntry] = {}

    def issue(self, build_id: str, *, token_budget: int, cost_budget_usd: float) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = _TokenEntry(
                build_id=build_id,
                token_budget=token_budget,
                cost_budget_usd=cost_budget_usd,
            )
        return token

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def usage_for(self, token: str) -> BuildUsage | None:
        with self._lock:
            entry = self._tokens.get(token)
            return entry.usage if entry else None

    def _lookup(self, token: str) -> _TokenEntry | None:
        """The live entry for `token`: None for an unknown, revoked or CLOSED token."""
        with self._lock:
            entry = self._tokens.get(token)
            return None if entry is None or entry.closed else entry

    def _admit(self, token: str, limit: int) -> tuple[_TokenEntry | None, bool]:
        """the look-up, the queue cap and the in-flight count as ONE step under the
        registry's lock, so `close` cannot slip between the look-up and the increment.
        `(None, False)`: unknown, revoked or closed (401). `(entry, False)`: `limit` requests are
        already alive (429), nothing counted. `(entry, True)`: admitted and counted; the caller
        gives it back with `_release` on every way out."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None or entry.closed:
                return None, False
            if entry.inflight >= limit:
                return entry, False
            entry.inflight += 1
            return entry, True

    def _release(self, entry: _TokenEntry) -> None:
        with self._lock:
            entry.inflight -= 1

    def close(self, token: str) -> bool:
        """stop admitting requests for `token` WITHOUT forgetting it. False for an
        unknown token. Idempotent. What `revoke` is for the traffic (new requests get 401), and
        none of what it is for the record: `usage_for` and `trace_for` keep answering, so the
        executor can read the figures once nothing is left that could change them (`is_idle`)."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return False
            entry.closed = True
            return True

    def is_idle(self, token: str) -> tuple[bool, int] | None:
        """`(idle, inflight)` for `token`, or None when it is unknown. Idle = no request of the
        token alive and its slot free. Read from the admin listener's thread; the slot's own
        `locked()` is a plain flag read, and no loop-bound state is touched."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            return entry.inflight == 0 and not entry.slot.locked(), entry.inflight

    def _record_refusal(self, token: str, detail: str) -> None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return
            if len(entry.usage.policy_refusals) < _MAX_RECORDED_REFUSALS:
                entry.usage.policy_refusals.append(detail)

    def _mark_unmetered(self, token: str, reason: str) -> None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None or entry.usage.unmetered:
                return
            entry.usage.unmetered = True
            entry.usage.unmetered_reason = reason

    def _unmetered_reason(self, entry: _TokenEntry) -> str | None:
        with self._lock:
            return entry.usage.unmetered_reason if entry.usage.unmetered else None

    def _over_budget(self, entry: _TokenEntry) -> bool:
        with self._lock:
            return (
                entry.usage.tokens >= entry.token_budget
                or entry.usage.cost_usd >= entry.cost_budget_usd
            )

    def trace_for(self, token: str) -> dict | None:
        """A copy of the build's trace record, or None for an unknown token."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            rec = entry.trace
            return {
                "steps": [dict(step) for step in rec.steps],
                "calls_total": rec.calls_total,
                "content_truncated": rec.content_truncated,
                "omitted_calls": rec.omitted_calls,
                "omitted_tokens": rec.omitted_tokens,
                "omitted_cost_usd": rec.omitted_cost_usd,
            }

    def _append_step(self, token: str, step: dict) -> None:
        """Adds one step, enforcing every bound HERE, in the proxy's memory
. Assigns the ordinal. A step that would pass the
        call or byte limit keeps its measured fields and loses its content."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return
            rec = entry.trace
            rec.calls_total += 1
            step["ordinal"] = rec.calls_total
            if step["content"] == CONTENT_RECORDED:
                over_calls = rec.calls_total > TRACE_MAX_CALLS
                size = len(json.dumps(step, ensure_ascii=False))
                if over_calls or rec.content_bytes + size > TRACE_MAX_BYTES:
                    step["blocks"] = []
                    step["thinking"] = []
                    step["content"] = CONTENT_DROPPED
                    rec.content_truncated = True
                else:
                    rec.content_bytes += size
            if len(rec.steps) < TRACE_MAX_STEP_ENTRIES:
                rec.steps.append(step)
            else:
                rec.omitted_calls += 1
                rec.omitted_tokens += step.get("tokens") or 0
                rec.omitted_cost_usd += step.get("cost_usd") or 0.0

    def _record(self, token: str, *, tokens: int, cost_usd: float) -> None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return
            entry.usage.tokens += tokens
            entry.usage.cost_usd += cost_usd
            if (
                entry.usage.tokens >= entry.token_budget
                or entry.usage.cost_usd >= entry.cost_budget_usd
            ):
                entry.usage.budget_exceeded = True


@dataclass(frozen=True)
class Dialect:
    """The engine-specific half of the proxy (ADR 0035 §Decision 3). The core
    calls these; it never looks inside a body itself.

    - `check_endpoint(method, path, query)` / `check_request(body)`: the refusal
      `detail` (starting `POLICY_REFUSAL_PREFIX`) or None. The endpoint rule runs
      first and the body rule only if it passed.
    - `upstream_auth_headers(real_key)`: the header(s) that carry the real key.
      The core has already stripped every inbound credential header.
    - `refusal_response(detail)`: the 403 in the engine's own error envelope.
    - `meter(response)`: `(model, usage, tokens, cost_usd)` for one 2xx response, or
      raises `_UnmeterableUsage` (the reason is recorded against the token).
    - `capture_trace(registry, token, response, *, request_body, started_at,
      ended_at, metered)`: records one build-trace step. Called only behind the
      core's own boundary, after metering.
    - `check_headers(pairs)`: the refusal `detail` for an inbound request header whose VALUE
      the dialect will not forward (every `(name, value)` line, duplicates included), or None.
      A header NAME outside `inbound_headers` is not refused, it is simply not forwarded.
    - `inbound_headers`: the lower-case names of the request headers forwarded upstream, and
      nothing else is (an allowlist built from what the pinned SDK is measured to
      send). The credential, `host` and `content-length` are never on it; the real key is
      added by `upstream_auth_headers`.
    - `outbound_headers`: the lower-case names of the upstream response headers returned to
      L3, and nothing else is (what the pinned SDK is measured to read).
    """

    name: str
    key_env_var: str
    default_upstream_base_url: str
    check_endpoint: Callable[[str, str, str], str | None]
    check_request: Callable[[bytes], str | None]
    upstream_auth_headers: Callable[[str], dict[str, str]]
    refusal_response: Callable[[str], Response]
    meter: Callable[[httpx.Response], tuple[str | None, dict, int, float]]
    capture_trace: Callable[..., None]
    check_headers: Callable[[list[tuple[str, str]]], str | None]
    inbound_headers: frozenset[str]
    outbound_headers: frozenset[str]


class TraceResponse(BaseModel):
    steps: list[dict]
    calls_total: int
    content_truncated: bool
    omitted_calls: int
    omitted_tokens: int
    omitted_cost_usd: float


class IssueTokenRequest(BaseModel):
    build_id: str
    token_budget: int
    cost_budget_usd: float


class IssueTokenResponse(BaseModel):
    token: str


class DrainResponse(BaseModel):
    """`drained` is True when the token has no request alive and its slot is free, so its
    usage, refusals and trace can no longer change; `inflight` is how many were still alive when
    the wait ended (0 when drained)."""

    drained: bool
    inflight: int


class UsageResponse(BaseModel):
    tokens: int
    cost_usd: float
    budget_exceeded: bool
    policy_refusals: list[str] = []
    unmetered: bool = False
    unmetered_reason: str | None = None


# the largest request body the
# proxy reads. It used to `await request.body()` with no bound, so one authenticated
# request from the sandbox could make the proxy (which holds the real key, in a container
# of its own) buffer whatever it was sent. An over-limit body is refused with 413 BEFORE
# it is read in full: by its declared Content-Length when there is one, and by a running
# count while streaming when there is not (chunked), stopping at the limit.
#
# Why 4 MiB (the earlier 16 MiB, which the next paragraph shows was not bounded by what
# the proxy holds in memory). Two things set it.
#
# What a request needs. A Messages request carries the whole conversation so far, so it is the
# largest request of a build that matters; the per-build token budget (`build_token_budget`,
# 200,000 tokens by default) caps what a conversation can hold at roughly 0.8 MB of text (about 4
# bytes a token), about 1.2 MB once JSON-escaped; the agent's own tools return text, not images.
# The pinned CLI's real requests were measured at 4 KB, 12.6 KB and 13 KB for a short session
# (its system prompt and six tool definitions are most of that). 4 MiB is more than three times the
# budget's text bound and leaves room for a couple of base64 images in a tool result.
#
# What the proxy can hold. `json.loads` of a body is not the body's size: the parse allocates one
# Python object per JSON value, so a body of nothing but small containers is expanded 25 to 33
# times (MEASURED on the proxy image's own Python 3.14.8, `ru_maxrss` of the real policy parse,
# one shape per process: `[{},{},...]` x25.0, `[[],[],...]` x25.0, `[{"":0},...]` x29, and
# `[{"":{}},...]` x33.2, the worst; a list of strings or floats x11, of small ints x5). At 16 MiB
# the worst shape peaked at 612 MiB with nothing else in the process, over the 512 MiB container cap
# (`proxy_container.PROXY_MEMORY_LIMIT`): one authenticated request killed the proxy, with the
# registry in it, so every cost the build had recorded was lost. Reproduced against the real
# container before the change: `OOMKilled=True`, exit 137. The parse is synchronous (no `await`
# between the body and the verdict), so two parses never overlap; the other memory is the bodies
# that wait on the slot, at most `MAX_CONCURRENT_REQUESTS_PER_TOKEN` of them. Worst case:
#   process (~50 MiB) + (queue 8 + parse 33x + the decoded text 1x + the read buffer 1x) x cap
#   = 50 + 43 x 4 = ~222 MiB at 4 MiB, under half of the cap; the same sum at 16 MiB is ~740 MiB.
# `tests/test_llm_proxy_request_surface.py` pins the arithmetic with those measured factors as
# literals, so raising this constant, the queue or the expansion without raising the container's
# cap is a red test. A constant, not a setting: nothing here is worth an operator's tuning. (The
# value is read at call time, so a test sets it small.)
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024

# the longest wait one `POST /_forge_admin/tokens/{token}/drain` accepts, and how often it
# looks. The caller (`ProxyContainer.drain`) chooses the wait; this is only the endpoint's own bound,
# so a request to the control plane can never park a thread for longer than this.
MAX_DRAIN_SECONDS = 120.0
_DRAIN_POLL_SECONDS = 0.05

# the proxy's own bound on a call to the provider (httpx's, per phase: connect, write, read
# and pool each get this long). It is also how long a call whose caller is gone can stay in flight,
# so the executor's drain derives its total wait from THIS constant
# (`sandbox_build_executor.PROXY_DRAIN_TOTAL_SECONDS`) and never restates the number.
UPSTREAM_TIMEOUT_SECONDS = 120.0

# serialising one token's upstream calls makes a QUEUE (the requests waiting on the
# slot), and a request that waits has already read its body, up to the limit above. So the
# queue is bounded, and from the moment the token is looked up, not from the moment the slot
# is: a request past this many alive at once is refused 429 before its body is read. The
# pinned engines never get near it (the Claude CLI sends its title call beside the first
# turn, so two; the OpenAI Agents SDK sends one at a time); the bound is for a build's own
# code opening connections, whose memory would otherwise be requests x 16 MiB in a 512 MiB
# container. Read at call time, so a test sets it small.
MAX_CONCURRENT_REQUESTS_PER_TOKEN = 8


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is longer than `limit` bytes. Never reads
    more than `limit` plus one chunk: a declared `Content-Length` over the limit is
    refused without reading a byte, and otherwise the body is counted as it arrives
    (a chunked body declares nothing, and a declared length is the client's claim)
    and the read stops the moment the count passes the limit."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            pass  # not a number: the running count below is what decides
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            return None
    return bytes(received)


def create_app(
    registry: TokenRegistry,
    *,
    real_api_key: str,
    admin_secret: str,
    upstream_base_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    dialect: Dialect | None = None,
    serve: str = "all",
) -> FastAPI:
    """Builds the proxy's FastAPI app for one `dialect` (default `anthropic`).

    **`serve` says which routes this app carries:** `"proxy"` the catch-all
    that forwards a sandbox's requests, `"control"` the `/_forge_admin/*` control plane,
    `"all"` both (the default, and what every in-process caller and test uses). The
    proxy container runs two apps over ONE registry on two listeners
    (`proxy_server.main`): the sandbox-facing one serves `"proxy"` only, so the
    sandbox cannot reach an admin route at all, and the admin one is bound to the
    container's loopback. On a `"proxy"` app `/_forge_admin/*` answers 404 before
    anything else is looked at.

    `real_api_key` is captured in this
    closure only — never put on `app.state` in a form that would be logged,
    never included in any response body. A pre-built `http_client` can be
    injected for tests (stub upstream).

    `admin_secret` gates the `/_forge_admin/*` control-plane endpoints
    (issue/revoke/usage) — these are called by the worker process (L2),
    never reachable meaningfully from inside the sandbox since the sandbox
    only ever holds a proxy token, not the admin secret. When the proxy
    itself runs as a Docker container, these
    endpoints are how the worker controls a registry that now lives in a
    different process.
    """

    if serve not in ("all", "control", "proxy"):
        raise ValueError(f"serve must be 'all', 'control' or 'proxy', not {serve!r}")
    dialect = dialect or ANTHROPIC_DIALECT
    base_url = upstream_base_url or dialect.default_upstream_base_url
    client = http_client or httpx.AsyncClient(base_url=base_url, timeout=UPSTREAM_TIMEOUT_SECONDS)

    app = FastAPI(title="forge-llm-proxy")
    # The two halves are routers so `serve` can leave one out; the admin one is
    # included first, so in an "all" app its routes still win over the catch-all.
    admin = APIRouter()
    public = APIRouter()
    admin_secret_bytes = admin_secret.encode()

    def _require_admin(x_forge_admin_secret: str | None = Header(default=None)) -> None:
        # a constant-time comparison, on bytes (`compare_digest` on `str`
        # refuses a non-ASCII one with a TypeError, which would be a 500 instead of
        # a 403 for a header an attacker controls).
        if not secrets.compare_digest((x_forge_admin_secret or "").encode(), admin_secret_bytes):
            raise HTTPException(status_code=403, detail="invalid admin secret")

    @admin.get("/_forge_admin/health")
    def health() -> dict:
        return {"ok": True}

    @admin.post("/_forge_admin/tokens", response_model=IssueTokenResponse)
    def issue_token(
        body: IssueTokenRequest, x_forge_admin_secret: str | None = Header(default=None)
    ) -> IssueTokenResponse:
        _require_admin(x_forge_admin_secret)
        token = registry.issue(
            body.build_id,
            token_budget=body.token_budget,
            cost_budget_usd=body.cost_budget_usd,
        )
        return IssueTokenResponse(token=token)

    @admin.delete("/_forge_admin/tokens/{token}")
    def revoke_token(token: str, x_forge_admin_secret: str | None = Header(default=None)) -> dict:
        _require_admin(x_forge_admin_secret)
        registry.revoke(token)
        return {"ok": True}

    @admin.get("/_forge_admin/tokens/{token}/usage", response_model=UsageResponse)
    def get_usage(
        token: str, x_forge_admin_secret: str | None = Header(default=None)
    ) -> UsageResponse:
        _require_admin(x_forge_admin_secret)
        usage = registry.usage_for(token)
        if usage is None:
            raise HTTPException(status_code=404, detail="unknown token")
        return UsageResponse(
            tokens=usage.tokens,
            cost_usd=usage.cost_usd,
            budget_exceeded=usage.budget_exceeded,
            policy_refusals=list(usage.policy_refusals),
            unmetered=usage.unmetered,
            unmetered_reason=usage.unmetered_reason,
        )

    # A sibling of the usage read, not a field on it. The usage payload
    # is the fail-closed contract the executor validates field by field, and its
    # staleness check (`_AMENDMENT_USAGE_FIELDS`) must stay meaningful; the trace
    # is large, best-effort and read on its own, so a trace that cannot be read
    # can never make usage unreadable, nor the reverse. Same admin auth.
    @admin.get("/_forge_admin/tokens/{token}/trace", response_model=TraceResponse)
    def get_trace(
        token: str, x_forge_admin_secret: str | None = Header(default=None)
    ) -> TraceResponse:
        _require_admin(x_forge_admin_secret)
        record = registry.trace_for(token)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown token")
        return TraceResponse(**record)

    # "Revoke, then wait until nothing of this token is left".
    # Closes the token (no new request is admitted, a queued one is refused when it reaches the
    # slot) WITHOUT forgetting it, then waits up to `timeout_seconds` for the requests already
    # alive -- at most the one holding the slot, plus queued ones that are refused at once -- to
    # finish and record. Answers 200 either way: `drained: false` is "the wait ran out", for the
    # caller to treat as an incomplete read. The wait polls plain state (the registry's lock and
    # the slot's `locked()` flag); it never awaits the slot, which belongs to the proxy
    # listener's event loop and may not be touched from this one.
    @admin.post("/_forge_admin/tokens/{token}/drain", response_model=DrainResponse)
    async def drain_token(
        token: str,
        timeout_seconds: float = Query(default=30.0, ge=0.0, le=MAX_DRAIN_SECONDS),
        x_forge_admin_secret: str | None = Header(default=None),
    ) -> DrainResponse:
        _require_admin(x_forge_admin_secret)
        if not registry.close(token):
            raise HTTPException(status_code=404, detail="unknown token")
        deadline = time.monotonic() + timeout_seconds
        while True:
            state = registry.is_idle(token)
            if state is None:  # revoked while waiting
                raise HTTPException(status_code=404, detail="unknown token")
            idle, inflight = state
            if idle:
                return DrainResponse(drained=True, inflight=0)
            if time.monotonic() >= deadline:
                return DrainResponse(drained=False, inflight=inflight)
            await asyncio.sleep(_DRAIN_POLL_SECONDS)

    @public.api_route("/{path:path}", methods=["POST", "GET"])
    async def proxy(path: str, request: Request) -> Response:
        # the sandbox-facing app has no admin routes. Not "forbidden" and not "401
        # for a missing token": there is nothing here to authenticate against, so the
        # answer is the same as for any path that does not exist, before the token (or
        # anything else the caller sent) is looked at.
        if serve == "proxy" and (path == "_forge_admin" or path.startswith("_forge_admin/")):
            return Response(status_code=404, content="not found")

        proxy_token = request.headers.get("x-api-key") or _bearer_token(request)
        if not proxy_token:
            return Response(status_code=401, content="missing proxy token")

        # the requests of one token that are alive at once are bounded (see
        # `MAX_CONCURRENT_REQUESTS_PER_TOKEN`), before the body is read. The count is
        # taken and given back around everything below, so no path leaks it. The
        # look-up, the cap and the count are one step (`_admit`), and a CLOSED token (the
        # executor is draining it before it reads the usage) is the same 401 as a revoked one.
        entry, admitted = registry._admit(proxy_token, MAX_CONCURRENT_REQUESTS_PER_TOKEN)
        if entry is None:
            return Response(status_code=401, content="unknown or revoked proxy token")
        if not admitted:
            logger.warning("llm proxy: a token already has %d requests in flight; refused 429", entry.inflight)
            return Response(
                status_code=429,
                content=f"TOO_MANY_CONCURRENT_REQUESTS: at most {MAX_CONCURRENT_REQUESTS_PER_TOKEN} at once",
            )
        try:
            return await _serve(path, request, proxy_token, entry)
        finally:
            registry._release(entry)

    async def _serve(path: str, request: Request, proxy_token: str, entry: _TokenEntry) -> Response:
        # bounded BEFORE it is read in full, and only for a caller that holds a
        # live token (the refusals before this never touch the body).
        body = await _read_bounded_body(request, MAX_REQUEST_BODY_BYTES)
        if body is None:
            logger.warning(
                "llm proxy: a request body over the %d-byte limit was refused", MAX_REQUEST_BODY_BYTES
            )
            return Response(
                status_code=413,
                content=f"REQUEST_TOO_LARGE: the request body is over the {MAX_REQUEST_BODY_BYTES}-byte limit",
            )

        # ADR 0029 §Amendment 2026-09-30: which endpoint, before what is in the
        # body. A dialect's body rule only reads the shape of its one model call;
        # other endpoints carry shapes it was never written to read. So only the
        # dialect's measured (method, path) pairs are forwarded at all.
        refusal = dialect.check_endpoint(request.method, path, request.url.query)

        # an inbound header VALUE the dialect will not forward (an unmeasured
        # `anthropic-beta`) is refused like any other boundary event rather than silently
        # stripped, which would send the provider a different request than the SDK built.
        header_lines = list(request.headers.items())
        if refusal is None:
            refusal = dialect.check_headers(header_lines)

        # ADR 0029 §2: before budget and before forwarding. A refused request
        # is never sent upstream, spends nothing, and is recorded so the
        # build fails closed and says why. Placed ahead of the budget check so
        # a boundary event is never masked by an exhausted budget.
        if refusal is None:
            refusal = dialect.check_request(body)
        if refusal is not None:
            registry._record_refusal(proxy_token, refusal)
            logger.warning("llm proxy: request refused by tool policy: %s", refusal)
            return dialect.refusal_response(refusal)

        # ADR 0014 §Amendment 2026-10-17 point 1: ONE upstream call in flight per
        # token. The slot is taken BEFORE the budget is read and given back only after the
        # call's usage is recorded, so the next request's budget check sees this call's
        # cost. `async with` releases it on every way out: a refusal, an upstream error, a
        # response that cannot be metered, an exception, or this task being cancelled (a
        # client that disconnects). The slot is this token's own: waiting on it blocks
        # nobody else's requests.
        async with entry.slot:
            # The token may have been revoked (the build ended) while this request waited.
            if registry._lookup(proxy_token) is not entry:
                return Response(status_code=401, content="unknown or revoked proxy token")

            # ADR 0014 §Amendment 2026-10-01 point 6: an earlier success could not
            # be metered, so the budget can no longer be enforced -- refuse, with
            # its own detail (distinct from BUDGET_EXCEEDED), never forwarding.
            unmetered_reason = registry._unmetered_reason(entry)
            if unmetered_reason is not None:
                return Response(status_code=403, content=f"{UNMETERED_REFUSAL_PREFIX}{unmetered_reason}")

            if registry._over_budget(entry):
                return Response(status_code=403, content="BUDGET_EXCEEDED")

            # an allowlist of header names, built from what the pinned SDK is
            # measured to send (the dialect's `inbound_headers`). The first line of a
            # repeated name wins. The real key is added after, so nothing from L3 can
            # replace it.
            forward_headers: dict[str, str] = {}
            for name, value in header_lines:
                lowered = name.lower()
                if lowered in dialect.inbound_headers and lowered not in forward_headers:
                    forward_headers[lowered] = value
            forward_headers.update(dialect.upstream_auth_headers(real_api_key))

            started_at = datetime.now(timezone.utc)
            try:
                upstream_response = await client.request(
                    request.method,
                    f"/{path}",
                    params=request.query_params,
                    headers=forward_headers,
                    content=body,
                )
            except httpx.HTTPError as exc:
                logger.warning("llm proxy: upstream unreachable: %s", exc)
                return Response(status_code=502, content="ENGINE_UNREACHABLE")
            except BaseException as exc:
                # ANYTHING other than a
                # response, or the `httpx.HTTPError` family above, that leaves the forward.
                # The request may have been sent, so the provider may bill it, and nothing here
                # can learn what it cost: `_account_usage` below never runs. The next budget
                # check would under-count by that call, and would again for every repeat. So the
                # token is marked unmetered, the same fail-closed state an unmeterable response
                # leaves: every later request is refused, and the build ends BUDGET_EXCEEDED
                # ("could not meter"). The mark is made BEFORE the slot is released (this is
                # inside `async with entry.slot`), so a request queued behind this one sees it.
                # This handles the cancel (a handler's task cancelled mid-call; nothing L3
                # does cancels one today, a client disconnect leaves it running);
                # and every other exception too: earlier, a `RuntimeError` from the
                # transport while the body was being read released the slot with the spend
                # unrecorded and the token still usable. Enumerating exception types is the
                # failure this replaces, so this is the boundary: nothing leaves the forward
                # except a measured response, an `httpx.HTTPError` (the open PLAN decision), or
                # a token that is unmetered. What it cannot tell apart is a failure BEFORE the
                # first byte left (building the request) from one after; both mark, which is the
                # fail-closed side. Always re-raised, as before.
                reason = (
                    UNMETERED_CANCELLED_REASON
                    if isinstance(exc, asyncio.CancelledError)
                    else UNMETERED_UNEXPECTED_REASON
                )
                registry._mark_unmetered(proxy_token, reason)
                logger.warning(
                    "llm proxy: a forwarded call ended with %s; its spend is unknown, the token is unmetered",
                    type(exc).__name__,
                )
                raise

            # Only a 2xx success is metered (a 3xx is not a model result, and an
            # HTTP error is not accounted -- ADR 0014 §Amendment "Watch out").
            if 200 <= upstream_response.status_code < 300:
                _account_usage(
                    dialect,
                    registry,
                    proxy_token,
                    upstream_response,
                    request_body=body,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc),
                )

        # only what the pinned SDK is measured to read goes back to L3. The upstream
        # also sends account-identifying and quota headers (an organisation id, rate-limit
        # budgets, cookies) that no SDK needs and L3 has no business seeing.
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers={
                k: v for k, v in upstream_response.headers.items() if k.lower() in dialect.outbound_headers
            },
        )

    if serve in ("all", "control"):
        app.include_router(admin)
    if serve in ("all", "proxy"):
        app.include_router(public)
    return app


_MAX_RECORDED_REFUSALS = 20

# Prefix of every refusal `detail`, so the executor's build-failure message and
# the tests can tell a policy refusal from any other 403.
POLICY_REFUSAL_PREFIX = "tool policy: "

# Prefix of the body of the 403 sent for a request on a token whose earlier
# response could not be metered; the reason follows it.
UNMETERED_REFUSAL_PREFIX = "USAGE_UNMETERED: "

# why a token is unmetered after a forwarded call was cancelled. Platform text; it reaches
# `Build.failure_detail` through the executor's "could not meter a response" message.
UNMETERED_CANCELLED_REASON = "a forwarded call was cancelled before its usage could be recorded"

# the same for every other exception that leaves the forward (neither a response nor an
# `httpx.HTTPError`). A constant, so no library's message reaches the build's failure detail.
UNMETERED_UNEXPECTED_REASON = "a forwarded call failed unexpectedly before its usage could be recorded"

# Tool `type` values the proxy forwards. A tool definition the client executes
# has no `type` or `type: "custom"`; every other value is an Anthropic-defined
# tool, and the ones that matter are executed by the *provider* (web search, web
# fetch, code execution, the MCP connector's toolset) -- a channel that leaves
# the sandbox over the one path default-deny egress does not cover. An allowlist,
# not a denylist: the next server tool the provider ships is refused by default.
_ALLOWED_TOOL_TYPES = frozenset({"custom"})

# Content-block `source.type` values the proxy forwards. `url` makes the
# provider fetch an address the agent chose (an exfiltration channel through
# the query string, and an injection channel through the response); `file`
# references provider-side storage. Neither is something a build needs.
_ALLOWED_SOURCE_TYPES = frozenset({"base64", "text", "content"})

# The top-level request fields the `anthropic` dialect forwards: an ALLOWLIST, and exactly the
# fields the pinned Claude Code CLI (2.1.285, agent-sdk 0.2.162) was MEASURED to send (
# 2026-10-28: the real runner in the pinned `forge-agent-sandbox` image, pointed at a recording
# server; the production path in a clean and a hostile workspace, 3 requests each (the title call and
# two tool-result turns), whose union is exactly these ten. A default-tool-set run that calls
# WebSearch sends one more request kind, the nested provider-side search, and that one alone carries
# an eleventh field, `tool_choice: {"type": "auto"}`; it is refused (for the field, and for its
# server tool), and the build never makes it). Any other top-level field is refused, whatever it
# is, however it is spelled, and whether or not anybody has heard of it. The denylist this replaces (`mcp_servers`, `container`)
# named two of the provider's request fields; the Messages API has others that make the provider
# call out or change the price (`inference_geo`, `service_tier`, `speed`, `safeguards`, `thread`,
# a top-level `cache_control`, ...), and the next one it ships would have been forwarded. NOT on
# the list, so refused: `mcp_servers` (the MCP connector: the provider connects to a remote server
# the request names), `container` (the provider-side code-execution container),
# `inference_geo` (the CLI's own cost function prices `us` at 1.1x), `service_tier` (priority
# capacity), `speed` (`fast` is billed at 2x; `_price_usage` prices a RESPONSE that reports it, but
# the request that asks for it is not measured), `tool_choice`, `temperature`, `stop_sequences`,
# `safeguards`, `thread`, `diagnostics`. Adding a field is a deliberate edit here, after the capture
# test (`test_every_top_level_field_the_cli_sends_is_one_the_proxy_allows`) is re-run and the
# field's effect on the price is read from the provider's own documentation.
ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS = frozenset(
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


# ADR 0029 §Amendment 2026-09-30: the ONLY (method, path) pairs forwarded, each
# with the raw query strings allowed on it. Derived from a recording of the real
# pinned CLI (2.1.285) by tests/test_agent_tool_capture_integration.py, clean and
# hostile workspace and a default-tool-set run that calls WebSearch: the model
# traffic is `POST /v1/messages?beta=true` and nothing else the proxy forwards.
# (The CLI also sends `HEAD /api/hello`, a reachability probe; the proxy has
# never routed HEAD, answers 405, and the CLI proceeds -- so it is not listed.)
#
# Query rule: the raw query string must be one of the listed values, compared
# exactly -- `beta=true` as measured, or empty. Any other parameter is refused
# rather than passed to a provider endpoint the capture never saw it used on.
# `path` is the routed path without its leading slash; matching is exact, so
# `v1/messages/`, `v1/messages/batches`, `v1/messages/../x` and `/v1/messages`
# (a doubled slash) are all different paths and all refused.
#
# Widening this is an explicit change, re-measured against the capture, like
# widening the tool-type set.
ALLOWED_ENDPOINTS: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "v1/messages"): frozenset({"", "beta=true"}),
}


def check_endpoint_policy(method: str, path: str, query: str) -> str | None:
    """The `anthropic` dialect's endpoint rule: `endpoint_refusal` over
    `ALLOWED_ENDPOINTS`, read at call time."""
    return endpoint_refusal(ALLOWED_ENDPOINTS, method, path, query)


def endpoint_refusal(
    allowed: dict[tuple[str, str], frozenset[str]], method: str, path: str, query: str
) -> str | None:
    """Returns the refusal `detail` for a (method, path, query) a dialect's
    `allowed` table does not list, or `None`. Two distinct conditions: the pair
    is not allowlisted at all, or the pair is but the query string is not."""
    allowed_queries = allowed.get((method, path))
    if allowed_queries is None:
        return f"{POLICY_REFUSAL_PREFIX}endpoint {_shown(method, 16)} /{_shown(path, 96)} is not allowed"
    if query not in allowed_queries:
        return f"{POLICY_REFUSAL_PREFIX}query string '{_shown(query, 64)}' is not allowed on {method} /{path}"
    return None


def unknown_field_refusal(parsed: dict, allowed: frozenset[str]) -> str | None:
    """the refusal `detail` for the first top-level field of `parsed` that is not in
    `allowed`, or None. Shared by both dialects, each with its own measured set. The name is
    caller-controlled, so it is bounded and escaped; a name on the list never reaches here."""
    for name in parsed:
        if name not in allowed:
            return f"{POLICY_REFUSAL_PREFIX}request field '{_shown(name, 64)}' is not allowed"
    return None


def _shown(value: str, limit: int) -> str:
    """A caller-controlled string, bounded and escaped, for a refusal `detail`
    (which is logged, returned to the agent and stored)."""
    text = value if len(value) <= limit else value[:limit] + "..."
    return repr(text)[1:-1]


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """`json.loads` keeps the LAST of a repeated key; a different parser
    upstream may keep the FIRST. Accepting a body the two would read
    differently lets a repeated `tools` key pass this check and mean something
    else to the provider. Refuse it instead."""
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError(key)
        seen[key] = value
    return seen


def check_request_policy(body: bytes) -> str | None:
    """ADR 0029 §2. Returns the refusal `detail` for a request body that asks
    the provider to act outside the sandbox, or `None` if it may be forwarded.

    An allowlist of shapes applied to every body the proxy forwards, not only
    `/v1/messages`:
    - no body: allowed (a GET, or a POST that carries nothing to inspect);
    - a non-empty body must be one JSON object with no repeated key (a body
      the proxy cannot read is a body it cannot vouch for -- including a
      compressed one);
    - every top-level field must be on `ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS` (what the pinned
      CLI was measured to send; `mcp_servers`, `container` and every other field are refused);
    - `tools`, if present, must be a list of objects, each with no `type` or
      `type == "custom"`;
    - no `image`/`document` content block may name a `url` or `file` source, at any
      depth: inside a `tool_result`'s content, and inside a `document` whose source is
      `type: "content"`;
    - `model` must be a model with a price row (checked last, so a body refused for
      another reason keeps naming that reason).

    Every condition has its own `detail`, each starting `POLICY_REFUSAL_PREFIX`.
    """
    if not body:
        return None

    try:
        parsed = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        return f"{POLICY_REFUSAL_PREFIX}request body repeats the key {str(exc)[:64]!r}"
    except (ValueError, RecursionError):
        return f"{POLICY_REFUSAL_PREFIX}request body is not valid JSON"

    if not isinstance(parsed, dict):
        return f"{POLICY_REFUSAL_PREFIX}request body is not a JSON object"

    unknown = unknown_field_refusal(parsed, ANTHROPIC_ALLOWED_TOP_LEVEL_FIELDS)
    if unknown is not None:
        return unknown

    if "tools" in parsed:
        tools = parsed["tools"]
        if not isinstance(tools, list):
            return f"{POLICY_REFUSAL_PREFIX}'tools' is not a list"
        for tool in tools:
            if not isinstance(tool, dict):
                return f"{POLICY_REFUSAL_PREFIX}a 'tools' entry is not an object"
            if "type" in tool:
                tool_type = tool["type"]
                if not isinstance(tool_type, str) or tool_type not in _ALLOWED_TOOL_TYPES:
                    shown = tool_type[:64] if isinstance(tool_type, str) else type(tool_type).__name__
                    return f"{POLICY_REFUSAL_PREFIX}tool type {shown!r} is not allowed"

    return _check_content_sources(parsed.get("messages")) or model_refusal(parsed.get("model"), is_priced_model)


def _check_content_sources(messages: object) -> str | None:
    """`image`/`document` blocks anywhere in `messages` may carry only an inline source.

    the walk follows EVERY list-valued `content` of a block (a `tool_result`'s, and
    any other block's) and the list inside a `source` of `type: "content"` (a `document`
    carries its own blocks there), not only the two shapes it was first written for. A URL
    image inside a document's content is the same outbound channel as a top-level one, and
    the first version of this walk went one level too shallow. The walk is a worklist, not
    recursion: a body of any depth costs time linear in its size (the proxy bounds the body)
    and cannot overflow the stack.

    Does not descend into `tool_use.input`, which is arbitrary agent-authored data and not a
    block the provider interprets (a `tool_use` block has no list-valued `content`, and
    its `input` is never read)."""
    if not isinstance(messages, list):
        return None
    pending: list[object] = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            pending.extend(message["content"])
    while pending:
        block = pending.pop()
        if not isinstance(block, dict):
            continue
        source = block.get("source")
        if block.get("type") in ("image", "document") and isinstance(source, dict):
            source_type = source.get("type")
            if not isinstance(source_type, str) or source_type not in _ALLOWED_SOURCE_TYPES:
                shown = source_type[:64] if isinstance(source_type, str) else type(source_type).__name__
                return f"{POLICY_REFUSAL_PREFIX}content source type {shown!r} is not allowed"
            if source_type == "content" and isinstance(source.get("content"), list):
                pending.extend(source["content"])
        if isinstance(block.get("content"), list):
            pending.extend(block["content"])
    return None


def _policy_refusal_response(detail: str) -> Response:
    """403 in the Messages API's own error envelope, plus a top-level `detail`
    so callers and tests can read the condition without unwrapping it."""
    payload = {
        "type": "error",
        "error": {"type": "permission_error", "message": detail},
        "detail": detail,
    }
    return Response(status_code=403, content=json.dumps(payload), media_type="application/json")


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("Bearer ") :]
    return None


def _account_usage(
    dialect: Dialect,
    registry: TokenRegistry,
    token: str,
    response: httpx.Response,
    *,
    request_body: bytes = b"",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> None:
    """Meters one 2xx model response against the token (ADR 0014 §Amendment
    2026-10-01 points 2-4, 6). Never raises -- the proxied response is returned
    regardless -- but a response it cannot read usage from marks the token
    UNMETERED (fail closed) rather than being recorded as zero.

    Reading and pricing the response is the dialect's `meter`; what is done with
    the outcome is the same for every dialect, and is this function's job.

    Metering comes first and stands alone. The build trace is captured
    afterwards, behind its own boundary: nothing it does can change what was
    metered, and an unmeterable response is still unmetered however the trace
    fares."""
    metered: tuple[str | None, dict, int, float] | None = None
    reason = ""
    try:
        model, usage, tokens, cost_usd = dialect.meter(response)
        # the record is inside the boundary, not in an `else`: a figure that was read but
        # could not be written down is as unmeasured as one that could not be read.
        registry._record(token, tokens=tokens, cost_usd=cost_usd)
    except _UnmeterableUsage as exc:
        reason = str(exc)
    except Exception:  # noqa: BLE001 - nothing may escape the response path
        logger.exception("llm proxy: unexpected error reading usage")
        reason = "unexpected error while reading usage"
    else:
        metered = (model, usage, tokens, cost_usd)

    if metered is None:
        logger.warning("llm proxy: response could not be metered: %s", reason)
        registry._mark_unmetered(token, reason)

    try:
        dialect.capture_trace(
            registry,
            token,
            response,
            request_body=request_body,
            started_at=started_at,
            ended_at=ended_at,
            metered=metered,
        )
    except Exception as exc:  # noqa: BLE001 - the trace is observability and never fails a request
        logger.warning("llm proxy: build trace could not be recorded (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# The `anthropic` dialect (Messages API) -- everything below until the dialect
# object at the end of the module. Unchanged by ADR 0035: the existing suite
# passes as it was.
# ---------------------------------------------------------------------------


def _anthropic_meter(response: httpx.Response) -> tuple[str | None, dict, int, float]:
    """Format is chosen by the response `content-type`: `text/event-stream` is
    parsed as SSE, anything else as JSON. A body that disagrees with its
    content-type fails the parse and is unmetered, not guessed at."""
    if response.headers.get("content-type", "").lower().startswith("text/event-stream"):
        model, usage = _usage_from_sse(response.content)
    else:
        model, usage = _usage_from_json(response.content)
    tokens, cost_usd = _price_usage(model, usage)
    return model, usage, tokens, cost_usd


def _usage_from_json(content: bytes) -> tuple[str | None, dict]:
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        raise _UnmeterableUsage("response body could not be decoded") from None
    if not isinstance(data, dict):
        raise _UnmeterableUsage("response JSON is not an object")
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise _UnmeterableUsage("response JSON has no usage object")
    model = data.get("model")
    return (model if isinstance(model, str) else None), usage


# `message_delta.usage` fields that supersede the running usage. The API reports
# CUMULATIVE counts, so a later value replaces an earlier one -- it is never
# added (verified against the official Python SDK's stream accumulator,
# anthropic 1.11.0 lib/streaming/_messages.py:520-530: "Usage counts on a
# message_delta are cumulative totals, so they overwrite rather than add").
# A field a delta omits (or sends null) keeps its message_start value.
_SUPERSEDING_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation",
    "speed",
)


def _usage_from_sse(content: bytes) -> tuple[str | None, dict]:
    """(model, usage) from a buffered `text/event-stream` Messages body: usage
    is `message_start.message.usage` superseded field by field by each later
    `message_delta.usage`; the model is `message_start.message.model`. An
    `error` event after a `message_start` changes nothing -- what the stream
    carried up to then is what was metered."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _UnmeterableUsage("response body could not be decoded") from None

    model: str | None = None
    usage: dict | None = None
    first_kind: str | None = None  # the first event that is not a keep-alive `ping`
    for event in _sse_events(text):
        kind = event.get("type")
        if first_kind is None and kind != "ping":
            first_kind = kind
        if kind == "message_start" and usage is None:
            message = event.get("message")
            start_usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(start_usage, dict):
                usage = dict(start_usage)
                candidate = message.get("model")
                model = candidate if isinstance(candidate, str) else None
        elif kind == "message_delta" and usage is not None:
            delta_usage = event.get("usage")
            if isinstance(delta_usage, dict):
                for name in _SUPERSEDING_USAGE_FIELDS:
                    if delta_usage.get(name) is not None:
                        usage[name] = delta_usage[name]
    if usage is None:
        # A 2xx stream whose first event is the provider's own `error` (for
        # example `overloaded_error`) is its explicit report that nothing was
        # generated: zero usage, measured. It is not a body the proxy cannot
        # read, and calling it unmetered would turn a transient upstream hiccup
        # into a dead build. An `error` that follows other events with no
        # `message_start` is not that shape and stays unmetered.
        if first_kind == "error":
            return None, {"input_tokens": 0, "output_tokens": 0}
        raise _UnmeterableUsage("SSE stream has no message_start usage")
    return model, usage


_SSE_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _sse_events(text: str):
    """Yields the JSON object carried by each SSE event's `data:` field(s).
    Events are blank-line separated; a multi-line `data` is joined with newlines
    (the SSE rule); comments and non-JSON or non-object data are skipped."""
    data_lines: list[str] = []

    def flush():
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # SSE line endings are CRLF, LF or CR only -- not `str.splitlines()`, which
    # would also split on U+2028 etc. that a JSON string may carry unescaped.
    for line in _SSE_LINE_BREAK.split(text):
        if line == "":
            parsed = flush()
            if parsed is not None:
                yield parsed
        elif line.startswith("data:"):
            value = line[len("data:") :]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    parsed = flush()
    if parsed is not None:
        yield parsed


# ---------------------------------------------------------------------------
# Build trace capture
# ---------------------------------------------------------------------------


class _UnparseableContent(ValueError):
    """The response's content blocks cannot be read. The step is then recorded
    as `unparsed`, with its measured fields."""


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


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
    """Records one step for a forwarded 2xx Messages call.

    Measured fields come from what metering read (None when it could not meter:
    the figures are then absent, never guessed). Content is the response's
    assistant blocks plus the `tool_result` blocks of the request's LAST user turn
    -- the history the client re-sends on every call is never re-recorded. A parse
    failure of the content is not an error here: the step is recorded `unparsed`
    with its measured fields."""
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
            is_sse = response.headers.get("content-type", "").lower().startswith("text/event-stream")
            raw_blocks, stop_reason = (
                _blocks_from_sse(response.content) if is_sse else _blocks_from_json(response.content)
            )
            blocks, thinking = _record_blocks(raw_blocks, _last_user_tool_results(request_body), token)
        except Exception as exc:  # noqa: BLE001 - see the docstring: unparsed, not an error
            logger.warning("llm proxy: a call's content could not be parsed (%s)", type(exc).__name__)
        else:
            step["stop_reason"] = _bounded_field(stop_reason, token)
            step["blocks"] = blocks
            step["thinking"] = thinking
            step["content"] = CONTENT_RECORDED
    registry._append_step(token, step)


def _usage_fields(usage: dict) -> dict:
    """The per-call token counts of a usage object `_price_usage` already read.
    The same reading rules, so the parts agree with the priced total."""
    breakdown = usage.get("cache_creation")
    write_5m = write_1h = 0
    if isinstance(breakdown, dict):
        write_5m = _count(breakdown, "ephemeral_5m_input_tokens")
        write_1h = _count(breakdown, "ephemeral_1h_input_tokens")
    cache_write = max(_count(usage, "cache_creation_input_tokens"), write_5m + write_1h)
    return {
        "input_tokens": _count(usage, "input_tokens"),
        "output_tokens": _count(usage, "output_tokens"),
        "cache_read_tokens": _count(usage, "cache_read_input_tokens"),
        "cache_write_tokens": cache_write,
    }


def _redact(text: str, token: str) -> str:
    return text.replace(token, TOKEN_REDACTION_MARKER) if token else text


def _bounded_field(value: object, token: str) -> str | None:
    """A short provider/agent-chosen string (model id, stop reason, tool name)."""
    if not isinstance(value, str):
        return None
    text = _redact(value, token)
    return text if len(text) <= TRACE_FIELD_MAX_CHARS else text[:TRACE_FIELD_MAX_CHARS]


def _bounded_text(value: str, token: str) -> dict:
    """`{text, chars, truncated}` for one content field. The token is replaced
    BEFORE the cut, so a cut can never leave a recognisable prefix of it."""
    text = _redact(value, token)
    truncated = len(text) > TRACE_BLOCK_MAX_CHARS
    return {
        "text": text[:TRACE_BLOCK_MAX_CHARS] if truncated else text,
        "chars": len(text),
        "truncated": truncated,
    }


def _last_user_tool_results(request_body: bytes) -> list[dict]:
    """The `tool_result` blocks of the LAST user message of a Messages request.
    Everything earlier in `messages` was recorded on an earlier call."""
    if not request_body:
        return []
    parsed = json.loads(request_body)
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, list):
                return []
            return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    return []


def _blocks_from_json(content: bytes) -> tuple[list, str | None]:
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise _UnparseableContent("response JSON has no content list")
    stop_reason = data.get("stop_reason")
    return data["content"], stop_reason if isinstance(stop_reason, str) else None


def _blocks_from_sse(content: bytes) -> tuple[list, str | None]:
    """The assistant blocks of a buffered `text/event-stream` body, rebuilt from
    `content_block_start` / `_delta` events in index order."""
    text = content.decode("utf-8")
    started: dict[int, dict] = {}
    pieces: dict[int, list[str]] = {}
    stop_reason: str | None = None
    for event in _sse_events(text):
        kind = event.get("type")
        if kind == "content_block_start":
            index, block = event.get("index"), event.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                raise _UnparseableContent("malformed content_block_start")
            started[index] = dict(block)
            pieces[index] = []
        elif kind == "content_block_delta":
            index, delta = event.get("index"), event.get("delta")
            if index not in started or not isinstance(delta, dict):
                raise _UnparseableContent("a delta for a block that never started")
            for name in ("text", "partial_json", "thinking"):
                part = delta.get(name)
                if isinstance(part, str):
                    pieces[index].append(part)
        elif kind == "message_delta":
            delta = event.get("delta")
            candidate = delta.get("stop_reason") if isinstance(delta, dict) else None
            if isinstance(candidate, str):
                stop_reason = candidate
    blocks: list[dict] = []
    for index in sorted(started):
        block, joined = started[index], "".join(pieces[index])
        kind = block.get("type")
        if kind == "text":
            block["text"] = (block.get("text") or "") + joined
        elif kind == "thinking":
            block["thinking"] = (block.get("thinking") or "") + joined
        elif kind == "tool_use" and joined:
            try:
                block["input"] = json.loads(joined)
            except (ValueError, RecursionError):
                block["input"] = joined  # kept as the model sent it
        blocks.append(block)
    return blocks, stop_reason


def _record_blocks(
    response_blocks: list, tool_results: list[dict], token: str
) -> tuple[list[dict], list[dict]]:
    """(content blocks, thinking markers). Thinking text is never kept: only that
    it was there, and how long it was."""
    blocks: list[dict] = []
    thinking: list[dict] = []
    for block in response_blocks:
        if not isinstance(block, dict):
            raise _UnparseableContent("a content block is not an object")
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            blocks.append({"kind": "assistant_text", **_bounded_text(text if isinstance(text, str) else "", token)})
        elif kind == "tool_use":
            tool_input = block.get("input")
            if isinstance(tool_input, str):
                rendered = tool_input
            else:
                rendered = json.dumps(tool_input, ensure_ascii=False, default=str)
            blocks.append(
                {
                    "kind": "tool_use",
                    "id": _bounded_field(block.get("id"), token),
                    "name": _bounded_field(block.get("name"), token),
                    **_bounded_text(rendered, token),
                }
            )
        elif kind in ("thinking", "redacted_thinking"):
            body = block.get("thinking") if kind == "thinking" else block.get("data")
            thinking.append({"chars": len(body) if isinstance(body, str) else 0})
        else:
            blocks.append({"kind": "other", "type": _bounded_field(kind, token)})
    for block in tool_results:
        is_error = block.get("is_error")
        blocks.append(
            {
                "kind": "tool_result",
                "tool_use_id": _bounded_field(block.get("tool_use_id"), token),
                "is_error": is_error if isinstance(is_error, bool) else None,
                **_bounded_text(_tool_result_text(block.get("content")), token),
            }
        )
    return blocks, thinking


def _tool_result_text(content: object) -> str:
    """A `tool_result`'s content as plain text: a string as is; a list of blocks
    with text blocks joined and anything else (an image) named by its type."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, dict):
            parts.append(f"[{_shown(str(item.get('type')), 32)} block]")
    return "\n".join(parts)


def _anthropic_auth_headers(real_key: str) -> dict[str, str]:
    return {"x-api-key": real_key}


# ---------------------------------------------------------------------------
# Headers (ADR 0014 §Amendment 2026-10-17 point 3)
# ---------------------------------------------------------------------------

# Request headers forwarded upstream: exactly the ones the PINNED Claude Code CLI (2.1.285,
# agent-sdk 0.2.162) was measured to send on `POST /v1/messages?beta=true`, 2026-10-19,
# from the pinned `forge-agent-sandbox` image pointed at a recording server (every call of
# a six-tool session, the CLI's own title call included: 7 requests, 10 distinct
# `anthropic-beta` values). Left out of that set: `x-api-key` (the proxy's own is added),
# `host`, `content-length` and `connection` (transport), and `accept-encoding` (the proxy's
# own client negotiates the encodings it can decode). Nothing the CLI did not send is on
# the list, so a header an L3 process invents is dropped rather than relayed beside the
# real key. An SDK bump that adds a header is caught by the engine-up capture test, which
# asserts every header on the wire is on this list or is one of the transport ones.
ANTHROPIC_INBOUND_HEADERS = frozenset(
    {
        "accept",
        "anthropic-beta",
        "anthropic-dangerous-direct-browser-access",
        "anthropic-version",
        "content-type",
        "user-agent",
        "x-app",
        "x-claude-code-session-id",
        "x-stainless-arch",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
        "x-stainless-timeout",
    }
)

# Response headers returned to L3: what the pinned SDK READS. The CLI's embedded Stainless
# client reads `retry-after`, `retry-after-ms` and `x-should-retry` to decide a retry and
# `request-id` for an error's text; `content-type` picks the JSON or SSE parser. The CLI
# also reads the `anthropic-ratelimit-unified-*` family, but only for subscription (OAuth)
# traffic and ignores a response without `anthropic-ratelimit-unified-status`; the
# platform's key is an API key, so none of them is sent back (the plain
# `anthropic-ratelimit-requests-*` / `-tokens-*` budgets are not read by the CLI at all, and
# they describe the platform's account). Never returned: `anthropic-organization-id`,
# `set-cookie`, `cf-*`, `server`, the rate-limit budgets.
ANTHROPIC_OUTBOUND_HEADERS = frozenset(
    {"content-type", "request-id", "retry-after", "retry-after-ms", "x-should-retry"}
)

# The `anthropic-beta` values the pinned CLI was measured to send (same capture as above).
# A request carrying any other value is REFUSED (a recorded policy refusal), not stripped:
# a beta can change what the provider bills in a way the price table does not model (the
# long-context beta `context-1m-2025-08-07` bills the whole request at a premium rate, and
# is the case this guards), and stripping it would send a different request than the SDK
# built and fail in a way nobody could read. None of the values below is a price
# multiplier, to the extent that can be known without the provider's own documentation
# (reasoned, not measured: the names describe request features, not rate tiers). Adding a
# value is a deliberate edit here after reading what the beta does to the price.
ANTHROPIC_ALLOWED_BETAS = frozenset(
    {
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "thinking-token-count-2026-05-13",
        "context-management-2025-06-27",
        "prompt-caching-scope-2026-01-05",
        "mid-conversation-system-2026-04-07",
        "per-turn-control-2026-07-01",
        "mid-conversation-tool-changes-2026-07-01",
        "effort-2025-11-24",
        "structured-outputs-2025-12-15",
    }
)


def check_headers_policy(lines: list[tuple[str, str]]) -> str | None:
    """The `anthropic` dialect's header-value rule: every value of every `anthropic-beta`
    line must be on `ANTHROPIC_ALLOWED_BETAS`. All lines are read, not only the first, so a
    value hidden on a second line is refused rather than quietly dropped."""
    for name, value in lines:
        if name.lower() != "anthropic-beta":
            continue
        for item in value.split(","):
            beta = item.strip()
            if beta and beta not in ANTHROPIC_ALLOWED_BETAS:
                return f"{POLICY_REFUSAL_PREFIX}anthropic-beta value '{_shown(beta, 64)}' is not allowed"
    return None


def _anthropic_capture(*args, **kwargs) -> None:
    # Looked up by name at call time, so the seam stays patchable.
    _capture_trace(*args, **kwargs)


ANTHROPIC_DIALECT = Dialect(
    name="anthropic",
    key_env_var="FORGE_PROXY_ANTHROPIC_API_KEY",
    default_upstream_base_url=_DEFAULT_UPSTREAM_BASE_URL,
    check_endpoint=check_endpoint_policy,
    check_request=check_request_policy,
    upstream_auth_headers=_anthropic_auth_headers,
    refusal_response=_policy_refusal_response,
    meter=_anthropic_meter,
    capture_trace=_anthropic_capture,
    check_headers=check_headers_policy,
    inbound_headers=ANTHROPIC_INBOUND_HEADERS,
    outbound_headers=ANTHROPIC_OUTBOUND_HEADERS,
)
