"""ADR 0014 §Amendment 2026-10-17 and ADR 0035 §Amend: the LLM proxy's
policy, for BOTH dialects.

1. One upstream call in flight per token, so the budget check sees the previous call's cost.
2. A model with no price row is refused (never priced at a ceiling); a dated snapshot of a priced
   model is not.
3. Header allowlists in both directions, built from what the pinned SDKs were measured to send.
4. A URL image nested inside another block's content is refused like a top-level one.

No Docker, no network. The proxy is driven in process over a stub upstream that RECORDS what it
receives, so "refused" can mean "never forwarded" and "serialised" can mean "the upstream saw one
call, not six".

Every concurrency test is BOUNDED when the thing it tests is removed:
the number of requests is a small constant, every wait has a timeout, and what the test parks on
is an `asyncio.Event` it sets itself in a `finally`. A proxy with no slot makes these tests fail by
counting; it cannot make them loop or hang.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.services import llm_proxy
from app.services.llm_proxy import ANTHROPIC_DIALECT, TokenRegistry, check_headers_policy, check_request_policy, create_app
from app.services.llm_proxy_openai import OPENAI_DIALECT
from app.services.llm_proxy_openai import check_request_policy as openai_check_request_policy
from tests.openai_proxy_support import usage_obj

REAL_KEY = "sk-REAL-KEY-never-leaves-the-proxy"
ADMIN_SECRET = "test-admin-secret"
PREFIX = "tool policy: "

BIG = 10**9
WAIT = 5.0  # no await in this file waits longer than this


# --- the two dialects, behind one shape --------------------------------------------------------


@dataclass(frozen=True)
class Kit:
    """What a test needs to talk to one dialect."""

    name: str
    dialect: object
    path: str
    model: str  # a model with a price row
    snapshot: str  # a dated snapshot of a priced model

    def body(self, **over) -> dict:
        if self.name == "anthropic":
            base = {"model": self.model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        else:
            base = {
                "model": self.model,
                "input": [{"role": "user", "content": "hi"}],
                "store": False,
                "service_tier": "default",
            }
        base.update(over)
        return {k: v for k, v in base.items() if v is not _DROP}

    def auth(self, token: str) -> dict:
        return {"x-api-key": token} if self.name == "anthropic" else {"authorization": f"Bearer {token}"}

    def reply(self, tokens_in: int, tokens_out: int, *, model: str | None = None) -> bytes:
        """A 2xx JSON body whose usage is exactly `tokens_in + tokens_out` counted tokens."""
        model = model or self.model
        if self.name == "anthropic":
            return json.dumps(
                {"id": "msg_1", "model": model, "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}
            ).encode()
        return json.dumps(
            {"id": "resp_1", "object": "response", "model": model, "status": "completed", "output": [],
             "service_tier": "default", "usage": usage_obj(tokens_in, tokens_out)}
        ).encode()

    def real_key_header(self) -> tuple[str, str]:
        return ("x-api-key", REAL_KEY) if self.name == "anthropic" else ("authorization", f"Bearer {REAL_KEY}")


_DROP = object()

ANTHROPIC = Kit("anthropic", ANTHROPIC_DIALECT, "/v1/messages", "claude-opus-4-8", "claude-opus-5-5-20260301")
OPENAI = Kit("openai", OPENAI_DIALECT, "/v1/responses", "gpt-5-mini", "gpt-5-mini-2025-08-07")
KITS = [pytest.param(ANTHROPIC, id="anthropic"), pytest.param(OPENAI, id="openai")]


@dataclass
class Stub:
    """A stub upstream. It records every call (headers, body) and answers each with `reply`.

    `park` makes the FIRST `park` calls wait for `release` (an event the test sets); `delay` makes
    every call take that long. `extra_headers` are added to every response, which is how a test
    gives the upstream headers L3 must never see."""

    reply: bytes
    delay: float = 0.0
    park: int = 0
    status: int = 200
    extra_headers: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    started: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def anything(path: str, request: Request) -> Response:
            body = await request.body()
            self.calls.append((dict(request.headers), body))
            self.started += 1
            if len(self.calls) <= self.park:
                await self.release.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            return Response(
                content=self.reply,
                status_code=self.status,
                media_type="application/json",
                headers=self.extra_headers,
            )

        return app


def _build(kit: Kit, http_client: httpx.AsyncClient, *, token_budget=BIG, cost_budget_usd=float(BIG)):
    registry = TokenRegistry()
    app = create_app(
        registry, real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET, http_client=http_client, dialect=kit.dialect
    )
    token = registry.issue("build-1", token_budget=token_budget, cost_budget_usd=cost_budget_usd)
    return app, registry, token


def _stub_client(stub: Stub) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=stub.app()), base_url="http://upstream.test")


def _proxy_client(app, *, raise_app_exceptions=True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://proxy.test",
    )


async def _post(client: httpx.AsyncClient, kit: Kit, token: str, **over):
    return await client.post(kit.path, headers=kit.auth(token), json=kit.body(**over))


async def _until(predicate, what: str) -> None:
    """Waits (bounded) for `predicate()`; fails the test, naming `what`, if it never holds."""
    deadline = asyncio.get_running_loop().time() + WAIT
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {what}")
        await asyncio.sleep(0.005)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


# =============================================================================================
# 1. One upstream call in flight per token
# =============================================================================================


@pytest.mark.parametrize("kit", KITS)
def test_concurrent_requests_against_a_budget_one_call_exhausts_forward_exactly_one(kit):
    """N requests arrive together; the budget (1000 tokens) is spent by ONE call (1500). Exactly
    one is forwarded, the rest are refused BUDGET_EXCEEDED, and what was recorded is the budget
    plus one call at most. Without the slot every request passes the budget check against the
    same zero and all N are forwarded."""
    stub = Stub(kit.reply(1000, 500), delay=0.05)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub), token_budget=1000)
        async with _proxy_client(app) as client:
            responses = await asyncio.gather(*[_post(client, kit, token) for _ in range(6)])
        return responses, registry.usage_for(token)

    responses, usage = _run(scenario())

    assert len(stub.calls) == 1
    assert sorted(r.status_code for r in responses) == [200, 403, 403, 403, 403, 403]
    assert {r.text for r in responses if r.status_code == 403} == {"BUDGET_EXCEEDED"}
    assert usage.tokens == 1500 and usage.tokens <= 1000 + 1500  # the budget plus one call
    assert usage.budget_exceeded is True


@pytest.mark.parametrize("kit", KITS)
def test_a_cost_budget_is_held_under_concurrency_too(kit):
    """The same, for the other half of the budget: the cost. gpt-5-mini / opus-4-8 at 1,000,000
    input tokens cost 0.25 / 5.00 USD; the cap is below either."""
    stub = Stub(kit.reply(1_000_000, 0), delay=0.05)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub), cost_budget_usd=0.2)
        async with _proxy_client(app) as client:
            responses = await asyncio.gather(*[_post(client, kit, token) for _ in range(5)])
        return responses, registry.usage_for(token)

    responses, usage = _run(scenario())

    assert len(stub.calls) == 1
    assert sorted(r.status_code for r in responses) == [200, 403, 403, 403, 403]
    assert usage.budget_exceeded is True


@pytest.mark.parametrize("kit", KITS)
def test_calls_within_the_budget_are_serialised_not_refused(kit):
    """The slot is a queue, not a refusal: with a large budget all N requests succeed, and the
    upstream never sees two at once."""
    in_flight = {"now": 0, "max": 0}
    stub = Stub(kit.reply(10, 5), delay=0.03)
    original_app = stub.app()

    async def tracked(scope, receive, send):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        try:
            await original_app(scope, receive, send)
        finally:
            in_flight["now"] -= 1

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=tracked), base_url="http://upstream.test")

    async def scenario():
        app, registry, token = _build(kit, http)
        async with _proxy_client(app) as client:
            responses = await asyncio.gather(*[_post(client, kit, token) for _ in range(5)])
        return responses, registry.usage_for(token)

    responses, usage = _run(scenario())

    assert [r.status_code for r in responses] == [200] * 5
    assert in_flight["max"] == 1
    assert usage.tokens == 5 * 15


@pytest.mark.parametrize("kit", KITS)
def test_a_waiting_request_blocks_no_other_token(kit):
    """Token A's call is parked at the upstream. Token B, issued by the same registry, completes
    while A is still parked: the slot belongs to the token, and waiting on it holds nothing else."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token_a = _build(kit, _stub_client(stub))
        token_b = registry.issue("build-2", token_budget=BIG, cost_budget_usd=float(BIG))
        async with _proxy_client(app) as client:
            parked = asyncio.create_task(_post(client, kit, token_a))
            waiting = None
            try:
                await _until(lambda: stub.started == 1, "token A's call to reach the upstream")
                waiting = asyncio.create_task(_post(client, kit, token_a))  # A's second call queues behind it
                b_response = await asyncio.wait_for(_post(client, kit, token_b), WAIT)
                a_done_while_b_ran = parked.done()
            finally:
                stub.release.set()
            a_response = await asyncio.wait_for(parked, WAIT)
            await asyncio.wait_for(waiting, WAIT)
        return b_response, a_response, a_done_while_b_ran

    b_response, a_response, a_done_while_b_ran = _run(scenario())

    assert b_response.status_code == 200 and a_response.status_code == 200
    assert a_done_while_b_ran is False  # B really did finish while A's call was still in flight


@pytest.mark.parametrize("kit", KITS)
def test_a_request_that_waited_for_a_token_revoked_meanwhile_is_not_forwarded(kit):
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            first = asyncio.create_task(_post(client, kit, token))
            second = None
            try:
                await _until(lambda: stub.started == 1, "the first call to reach the upstream")
                second = asyncio.create_task(_post(client, kit, token))
                await _until(lambda: registry._lookup(token).inflight == 2, "the second request to be in flight")
                registry.revoke(token)  # the build ended while the second request waited
            finally:
                stub.release.set()
            await asyncio.wait_for(first, WAIT)
            return await asyncio.wait_for(second, WAIT)

    second = _run(scenario())

    assert second.status_code == 401
    assert len(stub.calls) == 1  # only the call that was already in flight


# --- the slot is released on every path ---------------------------------------------------------


class _FailsOnce(httpx.AsyncBaseTransport):
    """An upstream transport whose first call fails with `error` and whose later calls answer."""

    def __init__(self, error: Exception, reply: bytes):
        self._error, self._reply, self.calls = error, reply, 0

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls == 1:
            if isinstance(self._error, httpx.HTTPError):
                raise type(self._error)(str(self._error), request=request)
            raise self._error
        return httpx.Response(200, content=self._reply, headers={"content-type": "application/json"})


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize(
    ("error", "first_status"),
    [
        (httpx.ConnectError("refused"), 502),  # the upstream was unreachable
    ],
    ids=["upstream_unreachable"],
)
def test_the_slot_is_released_when_the_upstream_call_fails(kit, error, first_status):
    transport = _FailsOnce(error, kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, httpx.AsyncClient(transport=transport, base_url="http://upstream.test"))
        async with _proxy_client(app, raise_app_exceptions=False) as client:
            first = await asyncio.wait_for(_post(client, kit, token), WAIT)
            second = await asyncio.wait_for(_post(client, kit, token), WAIT)  # would hang on a leaked slot
        return first, second, registry._lookup(token)

    first, second, entry = _run(scenario())

    assert first.status_code == first_status
    assert second.status_code == 200
    assert entry.inflight == 0 and not entry.slot.locked()


@pytest.mark.parametrize("kit", KITS)
def test_the_slot_is_released_when_the_upstream_call_raises_something_unexpected(kit):
    """The second half of what this test used to pin has changed. The slot is still released
    (the follow-up is ANSWERED, not stuck behind a leaked slot), but the follow-up is no longer
    forwarded: the call that raised may have been sent and billed, so the token is unmetered
    (`tests/test_llm_proxy_unmetered_paths.py` pins that rule itself)."""
    transport = _FailsOnce(RuntimeError("something the proxy did not expect"), kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, httpx.AsyncClient(transport=transport, base_url="http://upstream.test"))
        async with _proxy_client(app, raise_app_exceptions=False) as client:
            first = await asyncio.wait_for(_post(client, kit, token), WAIT)
            second = await asyncio.wait_for(_post(client, kit, token), WAIT)  # would hang on a leaked slot
        return first, second, registry._lookup(token)

    first, second, entry = _run(scenario())

    assert first.status_code == 500
    assert second.status_code == 403 and second.text.startswith("USAGE_UNMETERED: ")
    assert transport.calls == 1  # the follow-up was refused, not forwarded
    assert entry.inflight == 0 and not entry.slot.locked()


@pytest.mark.parametrize("kit", KITS)
def test_the_slot_is_released_when_the_client_goes_away_mid_call(kit):
    """The first request is cancelled while its upstream call is in flight (what a client that
    disconnects mid-response looks like to the handler). The next request is not stuck behind it:
    it is ANSWERED, within the bound, and the slot and the in-flight count are back to free.

    The answer changed as well: it was a forwarded 200 (the cancelled call's spend was
    simply never recorded, so the budget check under-counted); it is now the unmetered refusal,
    because the cancelled call may have been billed and nothing can say for how much
    (`tests/test_llm_proxy_drain.py` pins the fail-closed rule itself)."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            doomed = asyncio.create_task(_post(client, kit, token))
            try:
                await _until(lambda: stub.started == 1, "the doomed call to reach the upstream")
                doomed.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await doomed
                follow_up = await asyncio.wait_for(_post(client, kit, token), WAIT)
            finally:
                stub.release.set()
        return follow_up, registry._lookup(token)

    follow_up, entry = _run(scenario())

    assert follow_up.status_code == 403
    assert follow_up.text.startswith("USAGE_UNMETERED: ")
    assert len(stub.calls) == 1  # the follow-up was refused, not forwarded
    assert entry.inflight == 0 and not entry.slot.locked()


@pytest.mark.parametrize("kit", KITS)
def test_the_slot_is_released_after_every_kind_of_refusal(kit):
    """A budget refusal, an unmetered refusal and a policy refusal each leave the token usable
    by the next request (here: the next refusal, which would hang on a leaked slot)."""
    stub = Stub(kit.reply(1000, 500))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub), token_budget=1000)
        async with _proxy_client(app) as client:
            first = await asyncio.wait_for(_post(client, kit, token), WAIT)  # spends the budget
            budget = [await asyncio.wait_for(_post(client, kit, token), WAIT) for _ in range(3)]
            policy = await asyncio.wait_for(_post(client, kit, token, model="not-a-priced-model"), WAIT)
            registry._mark_unmetered(token, "forced for the test")
            unmetered = await asyncio.wait_for(_post(client, kit, token), WAIT)
        return first, budget, policy, unmetered, registry._lookup(token)

    first, budget, policy, unmetered, entry = _run(scenario())

    assert first.status_code == 200
    assert [r.text for r in budget] == ["BUDGET_EXCEEDED"] * 3
    assert policy.status_code == 403 and PREFIX in policy.text
    assert unmetered.status_code == 403
    assert unmetered.text == "USAGE_UNMETERED: forced for the test"
    assert entry.inflight == 0 and not entry.slot.locked()
    assert len(stub.calls) == 1


# --- the queue the slot makes is bounded ----------------------------------------------------------


@pytest.mark.parametrize("kit", KITS)
def test_requests_past_the_per_token_cap_are_refused_429_before_their_body_is_read(kit, monkeypatch):
    """With the cap at 2 and the first call parked: of five requests, two are alive in the proxy
    and three are refused 429 straight away. (Without the cap all five queue on the slot.) The
    refusal is not a policy refusal: nothing is recorded against the build."""
    monkeypatch.setattr(llm_proxy, "MAX_CONCURRENT_REQUESTS_PER_TOKEN", 2)
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            first = asyncio.create_task(_post(client, kit, token))
            tasks = [first]
            try:
                await _until(lambda: stub.started == 1, "the first call to reach the upstream")
                tasks.append(asyncio.create_task(_post(client, kit, token)))
                await _until(lambda: registry._lookup(token).inflight == 2, "two requests in flight")
                refused = [asyncio.create_task(_post(client, kit, token)) for _ in range(3)]
                tasks.extend(refused)
                done, pending = await asyncio.wait(refused, timeout=WAIT)
                refused_now = len(done)
            finally:
                stub.release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 30)
        return results, refused_now, registry.usage_for(token), registry._lookup(token)

    results, refused_now, usage, entry = _run(scenario())

    assert refused_now == 3
    assert sorted(r.status_code for r in results) == [200, 200, 429, 429, 429]
    assert all(r.text.startswith("TOO_MANY_CONCURRENT_REQUESTS") for r in results if r.status_code == 429)
    assert len(stub.calls) == 2
    assert usage.policy_refusals == []
    assert entry.inflight == 0


def test_the_per_token_cap_is_pinned():
    """A literal: the engines never come near it (two at most), and a larger number is a bigger
    worst-case of buffered request bodies (cap x 16 MiB) inside a 512 MiB container."""
    assert llm_proxy.MAX_CONCURRENT_REQUESTS_PER_TOKEN == 8


# =============================================================================================
# 2. A model with no price row is refused
# =============================================================================================

_UNPRICED = [
    ("claude-invented-9", "model 'claude-invented-9' has no price row, so it is not allowed"),
    ("gpt-9", "model 'gpt-9' has no price row, so it is not allowed"),
    ("claude-opus-5-5-x", "model 'claude-opus-5-5-x' has no price row, so it is not allowed"),  # no prefix match
    ("gpt-5-mini-high", "model 'gpt-5-mini-high' has no price row, so it is not allowed"),
    ("GPT-5", "model 'GPT-5' has no price row, so it is not allowed"),  # case matters
    ("", "model '' has no price row, so it is not allowed"),
    ("gpt-5-pro", "model 'gpt-5-pro' has no price row, so it is not allowed"),  # the case the ceiling undercounted
    (_DROP, "the request names no model, and a request with no model cannot be priced"),
    (None, "the request names no model, and a request with no model cannot be priced"),
    (7, "the request's 'model' is not a string (int)"),
    (["gpt-5"], "the request's 'model' is not a string (list)"),
]
_UNPRICED_IDS = [
    "invented", "gpt-9", "prefix_lookalike", "suffix_lookalike", "case", "empty", "pro_model",
    "absent", "null", "int", "list",
]


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize(("model", "detail"), _UNPRICED, ids=_UNPRICED_IDS)
def test_a_request_naming_a_model_with_no_price_row_is_refused_recorded_and_never_forwarded(kit, model, detail):
    """Neither dialect's table holds `model` (a model priced for the OTHER dialect is unpriced
    here too, which is the point of one table per dialect)."""
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            response = await _post(client, kit, token, model=model)
        return response, registry.usage_for(token)

    response, usage = _run(scenario())

    expected = PREFIX + detail
    assert response.status_code == 403
    assert expected in response.text  # in the dialect's own error envelope
    assert stub.calls == []  # never forwarded
    assert usage.policy_refusals == [expected]  # recorded, so the build fails and says why
    assert (usage.tokens, usage.cost_usd, usage.unmetered) == (0, 0.0, False)  # and spends nothing


@pytest.mark.parametrize("kit", KITS)
def test_a_priced_model_and_a_dated_snapshot_of_one_are_forwarded(kit):
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            return [await _post(client, kit, token, model=m) for m in (kit.model, kit.snapshot)]

    responses = _run(scenario())

    assert [r.status_code for r in responses] == [200, 200]
    assert [json.loads(b)["model"] for _, b in stub.calls] == [kit.model, kit.snapshot]


def test_the_unpriced_model_rule_is_the_last_rule_so_other_refusals_keep_their_own_detail():
    """A body refused for its tools names the tools, not the (also unpriced) model."""
    body = json.dumps({"model": "claude-invented-9", "tools": [{"type": "web_search_20250305"}]}).encode()

    assert check_request_policy(body) == PREFIX + "tool type 'web_search_20250305' is not allowed"


def test_an_empty_body_is_still_left_to_the_upstream_to_reject():
    """Unchanged: a POST that carries nothing cannot be priced or spent, and the anthropic rule
    has always let it through (the openai dialect refuses it for a different reason)."""
    assert check_request_policy(b"") is None
    assert openai_check_request_policy(b"") == PREFIX + "request body is empty"


# =============================================================================================
# 3. Header allowlists
# =============================================================================================

# The headers the pinned SDKs were MEASURED to send (2026-10-19, scratch capture against the pinned
# images, no network, no key). Literals on purpose: the proxy's own sets are on the other side.
CLAUDE_CLI_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br, zstd",
    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,effort-2025-11-24,"
    "structured-outputs-2025-12-15",
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.285 (external, sdk-py, agent-sdk/0.2.162)",
    "x-app": "cli",
    "x-claude-code-session-id": "883dd95d-ffa8-4005-87d7-1e4f81a695cf",
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Linux",
    "x-stainless-package-version": "0.127.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v26.3.0",
    "x-stainless-timeout": "600",
}
OPENAI_SDK_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate",
    "content-type": "application/json",
    "user-agent": "Agents/Python 0.23.1",
    "x-stainless-arch": "x64",
    "x-stainless-async": "async:asyncio",
    "x-stainless-lang": "python",
    "x-stainless-os": "Linux",
    "x-stainless-package-version": "3.28.0",
    "x-stainless-read-timeout": "600",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "CPython",
    "x-stainless-runtime-version": "3.11.16",
}
# Sent by the pinned SDK but deliberately NOT forwarded (the transport's, and the credential).
NOT_FORWARDED_OF_THE_MEASURED = ("accept-encoding",)


def _sdk_headers(kit: Kit) -> dict:
    return dict(CLAUDE_CLI_HEADERS if kit.name == "anthropic" else OPENAI_SDK_HEADERS)


def _one_call(kit: Kit, headers: dict | list, *, stub: Stub | None = None, **over):
    """One request carrying `headers` (and the client's own credential); (response, stub, usage)."""
    stub = stub or Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        sent = [*(headers.items() if isinstance(headers, dict) else headers), *kit.auth(token).items()]
        async with _proxy_client(app) as client:
            response = await client.post(kit.path, headers=sent, json=kit.body(**over))
        return response, registry.usage_for(token), token

    response, usage, token = _run(scenario())
    return response, stub, usage, token


@pytest.mark.parametrize("kit", KITS)
def test_what_the_pinned_sdk_sends_is_forwarded_except_the_transport_headers(kit):
    sent = _sdk_headers(kit)

    response, stub, _, token = _one_call(kit, sent)

    assert response.status_code == 200
    (received, _), = stub.calls
    for name, value in sent.items():
        if name in NOT_FORWARDED_OF_THE_MEASURED:
            continue
        assert received.get(name) == value, name  # forwarded, value untouched
    # the credential header is the real key, whatever the client put there
    key_name, key_value = kit.real_key_header()
    assert received[key_name] == key_value
    assert token not in json.dumps(received)


@pytest.mark.parametrize("kit", KITS)
def test_a_header_off_the_allowlist_is_not_forwarded(kit):
    """OpenAI-Organization / OpenAI-Project would bill another organisation or project than the
    platform's key belongs to; the others are headers the pinned SDKs never send. Dropped, not
    refused: the request still succeeds, minus the header."""
    hostile = {
        "openai-organization": "org-someone-else",
        "openai-project": "proj_someone_else",
        "openai-beta": "assistants=v2",
        "anthropic-organization-id": "org-someone-else",
        "x-forge-evil": "1",
        "cookie": "a=b",
        "x-forwarded-for": "10.0.0.9",
        "idempotency-key": "k",
    }

    response, stub, usage, _ = _one_call(kit, {**_sdk_headers(kit), **hostile})

    assert response.status_code == 200
    (received, _), = stub.calls
    assert not (set(hostile) & set(received))
    assert usage.policy_refusals == []  # a name outside an allowlist is not a boundary event


@pytest.mark.parametrize("kit", KITS)
def test_the_clients_own_credential_never_reaches_the_upstream_and_cannot_replace_the_real_key(kit):
    """The client's token goes in its dialect's credential header; for the anthropic dialect it
    also sends a bearer header of its own (the proxy reads `x-api-key` first, so that one is
    only noise it must not relay). Neither reaches the upstream, and the other vendor's
    credential header is never made up."""
    extra = {"authorization": "Bearer client-bearer"} if kit is ANTHROPIC else {}
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        headers = {**extra, **kit.auth(token)}
        async with _proxy_client(app) as client:
            return await client.post(kit.path, headers=headers, json=kit.body())

    _run(scenario())

    (received, _), = stub.calls
    key_name, key_value = kit.real_key_header()
    assert received[key_name] == key_value
    other = "authorization" if key_name == "x-api-key" else "x-api-key"
    assert other not in received  # the other style's header is not forwarded at all
    assert "client-" not in json.dumps(received)


@pytest.mark.parametrize("kit", KITS)
def test_a_repeated_allowed_header_forwards_its_first_line_only(kit):
    response, stub, _, _ = _one_call(kit, [("user-agent", "first"), ("user-agent", "second")])

    assert response.status_code == 200
    (received, _), = stub.calls
    assert received["user-agent"] == "first"


# --- anthropic-beta ----------------------------------------------------------------------------


def test_every_beta_value_the_pinned_cli_sent_is_allowed():
    measured = CLAUDE_CLI_HEADERS["anthropic-beta"].split(",")
    assert len(measured) == 10

    assert check_headers_policy([("anthropic-beta", CLAUDE_CLI_HEADERS["anthropic-beta"])]) is None
    for beta in measured:
        assert check_headers_policy([("anthropic-beta", beta)]) is None


def test_the_allowed_beta_set_is_exactly_the_measured_one():
    """A literal copy of the measured values, so adding to the production set is a red test that
    says why: each addition needs its pricing read first (see the comment on the constant)."""
    assert llm_proxy.ANTHROPIC_ALLOWED_BETAS == frozenset(CLAUDE_CLI_HEADERS["anthropic-beta"].split(","))


@pytest.mark.parametrize(
    "lines",
    [
        [("anthropic-beta", "context-1m-2025-08-07")],  # the long-context beta: a premium rate the table does not model
        [("anthropic-beta", "claude-code-20250219,context-1m-2025-08-07")],  # one bad value among good ones
        [("anthropic-beta", "claude-code-20250219"), ("anthropic-beta", "context-1m-2025-08-07")],  # on a second line
        [("Anthropic-Beta", "context-1m-2025-08-07")],  # any case of the name
        [("anthropic-beta", "claude-code-20250219 , context-1m-2025-08-07 ")],  # stray spaces
        [("anthropic-beta", "CLAUDE-CODE-20250219")],  # values are exact
    ],
    ids=["long_context", "among_good", "second_line", "name_case", "spaces", "value_case"],
)
def test_an_unmeasured_beta_value_is_refused(lines):
    detail = check_headers_policy(lines)

    assert detail is not None and detail.startswith(PREFIX + "anthropic-beta value '")
    assert detail.endswith("' is not allowed")


def test_a_beta_header_with_only_blank_entries_is_not_a_value():
    assert check_headers_policy([("anthropic-beta", " , ,")]) is None
    assert check_headers_policy([("x-other", "context-1m-2025-08-07")]) is None  # only that header is read


def test_a_request_with_an_unmeasured_beta_is_refused_recorded_and_never_forwarded():
    response, stub, usage, _ = _one_call(
        ANTHROPIC, {**CLAUDE_CLI_HEADERS, "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07"}
    )

    expected = PREFIX + "anthropic-beta value 'context-1m-2025-08-07' is not allowed"
    assert response.status_code == 403
    assert response.json()["detail"] == expected
    assert stub.calls == []
    assert usage.policy_refusals == [expected]
    assert (usage.tokens, usage.cost_usd) == (0, 0.0)


def test_a_beta_value_on_a_second_header_line_is_refused_not_dropped():
    response, stub, usage, _ = _one_call(
        ANTHROPIC,
        [("anthropic-beta", "claude-code-20250219"), ("anthropic-beta", "context-1m-2025-08-07"), ("anthropic-version", "2023-06-01")],
    )

    assert response.status_code == 403
    assert stub.calls == []
    assert usage.policy_refusals == [PREFIX + "anthropic-beta value 'context-1m-2025-08-07' is not allowed"]


def test_the_openai_dialect_has_no_beta_rule_and_never_forwards_one():
    """`openai-beta` is not a header the pinned SDK sends, so it is off the list: dropped."""
    response, stub, _, _ = _one_call(OPENAI, {**OPENAI_SDK_HEADERS, "openai-beta": "assistants=v2"})

    assert response.status_code == 200
    assert "openai-beta" not in stub.calls[0][0]


# --- outbound ------------------------------------------------------------------------------------

_UPSTREAM_ONLY = {
    "anthropic-organization-id": "org-the-platform",
    "openai-organization": "org-the-platform",
    "openai-project": "proj_the_platform",
    "openai-processing-ms": "123",
    "set-cookie": "__cf_bm=abc; HttpOnly",
    "cf-ray": "abc-FRA",
    "x-ratelimit-remaining-tokens": "999",
    "anthropic-ratelimit-requests-remaining": "49",
    "server": "cloudflare",
}
_SDK_NEEDS = {
    "request-id": "req_1",
    "x-request-id": "req_2",
    "retry-after": "3",
    "retry-after-ms": "3000",
    "x-should-retry": "true",
}


@pytest.mark.parametrize("kit", KITS)
def test_only_what_the_sdk_reads_goes_back_to_the_sandbox(kit):
    stub = Stub(kit.reply(10, 5), extra_headers={**_UPSTREAM_ONLY, **_SDK_NEEDS})

    response, stub, _, _ = _one_call(kit, _sdk_headers(kit), stub=stub)

    assert response.status_code == 200
    assert not ({k.lower() for k in _UPSTREAM_ONLY} & {k.lower() for k in response.headers})
    # content-type always; and, of the SDK-read headers, the ones the pinned SDK of THIS dialect reads
    assert response.headers["content-type"].startswith("application/json")
    reads = ("request-id", "retry-after", "retry-after-ms", "x-should-retry") if kit is ANTHROPIC else (
        "x-request-id", "retry-after", "retry-after-ms", "x-should-retry")
    for name in reads:
        assert response.headers[name] == _SDK_NEEDS[name]
    other = "x-request-id" if kit is ANTHROPIC else "request-id"
    assert other not in response.headers  # the OTHER vendor's request id is not this SDK's


@pytest.mark.parametrize("kit", KITS)
def test_an_upstream_error_keeps_its_retry_headers_and_loses_the_account_ones(kit):
    """The SDK decides a retry from `retry-after` / `x-should-retry` on a 429, so those survive."""
    stub = Stub(b'{"error": {"message": "slow down"}}', status=429, extra_headers={**_UPSTREAM_ONLY, **_SDK_NEEDS})

    response, stub, usage, _ = _one_call(kit, _sdk_headers(kit), stub=stub)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3" and response.headers["x-should-retry"] == "true"
    assert "openai-organization" not in response.headers and "set-cookie" not in response.headers
    assert (usage.tokens, usage.unmetered) == (0, False)  # an HTTP error is not metered, as before


# =============================================================================================
# 4. Nested content sources
# =============================================================================================

URL_IMAGE = {"type": "image", "source": {"type": "url", "url": "https://attacker.example/x.png?leak=secret"}}
B64_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}


def _doc_with(*inner) -> dict:
    return {"type": "document", "source": {"type": "content", "content": list(inner)}}


def _messages(*blocks, role="user") -> dict:
    return {"model": "claude-opus-4-8", "max_tokens": 1, "messages": [{"role": role, "content": list(blocks)}]}


def _tool_result(*inner) -> dict:
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": list(inner)}


SOURCE_REFUSAL = PREFIX + "content source type 'url' is not allowed"


@pytest.mark.parametrize(
    ("name", "blocks"),
    [
        ("top_level", [URL_IMAGE]),
        ("in_a_tool_result", [_tool_result(URL_IMAGE)]),
        ("in_a_documents_content_source", [_doc_with(URL_IMAGE)]),
        ("document_in_a_tool_result", [_tool_result(_doc_with(URL_IMAGE))]),
        ("document_in_a_document", [_doc_with(_doc_with(URL_IMAGE))]),
        ("after_a_clean_block", [B64_IMAGE, _doc_with({"type": "text", "text": "x"}, URL_IMAGE)]),
        (
            "in_the_content_of_some_other_block",
            [{"type": "text", "text": "x", "content": [URL_IMAGE]}],  # any list-valued `content` is walked
        ),
        (
            "six_levels_deep",
            [_tool_result(_doc_with(_tool_result(_doc_with(_tool_result(URL_IMAGE)))))],
        ),
    ],
)
def test_a_url_image_is_refused_at_any_depth_of_content(name, blocks):
    assert check_request_policy(json.dumps(_messages(*blocks)).encode()) == SOURCE_REFUSAL


@pytest.mark.parametrize(
    "bad_source",
    [{"type": "file", "file_id": "file_1"}, {"type": "url", "url": "https://x"}, {"type": 5}, {}],
    ids=["file", "url", "non_string_type", "no_type"],
)
def test_every_non_inline_source_nested_in_a_document_is_refused(bad_source):
    body = _messages(_doc_with({"type": "image", "source": bad_source}))

    detail = check_request_policy(json.dumps(body).encode())

    assert detail is not None and detail.startswith(PREFIX + "content source type ")


def test_the_nested_walk_is_not_vacuous_clean_nesting_passes():
    clean = _messages(
        _tool_result(_doc_with({"type": "text", "text": "x"}, B64_IMAGE)),
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "hello"}},
        {"type": "document", "source": {"type": "content", "content": "a plain string"}},  # string content: no blocks
        {"type": "tool_use", "id": "t", "name": "Read", "input": {"content": [URL_IMAGE]}},  # agent data, never read
    )

    assert check_request_policy(json.dumps(clean).encode()) is None


def test_a_deeply_nested_body_is_still_walked_to_the_bottom():
    """300 levels of tool_result: the URL image at the bottom is found. (The walk is a worklist,
    not recursion, so depth costs time and never stack; `json.loads` has a depth limit of its
    own, which the existing 'not valid JSON' refusal covers.)"""
    block: dict = URL_IMAGE
    for _ in range(300):
        block = _tool_result(block)

    assert check_request_policy(json.dumps(_messages(block)).encode()) == SOURCE_REFUSAL


def test_a_nested_url_image_through_the_proxy_is_refused_recorded_and_not_forwarded():
    stub = Stub(ANTHROPIC.reply(10, 5))

    async def scenario():
        app, registry, token = _build(ANTHROPIC, _stub_client(stub))
        async with _proxy_client(app) as client:
            response = await client.post(
                ANTHROPIC.path, headers=ANTHROPIC.auth(token), json=_messages(_tool_result(_doc_with(URL_IMAGE)))
            )
        return response, registry.usage_for(token)

    response, usage = _run(scenario())

    assert response.status_code == 403
    assert response.json()["detail"] == SOURCE_REFUSAL
    assert stub.calls == []
    assert usage.policy_refusals == [SOURCE_REFUSAL]


# --- the openai dialect's input items ------------------------------------------------------------

OPENAI_URL_REFUSAL = PREFIX + "an 'input_image' part may not reference a URL or a file"


def _input_body(*items) -> bytes:
    return json.dumps(
        {"model": "gpt-5-mini", "store": False, "service_tier": "default", "input": list(items)}
    ).encode()


@pytest.mark.parametrize(
    ("name", "items", "expected"),
    [
        (
            "message_content",
            [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://x/y.png"}]}],
            OPENAI_URL_REFUSAL,
        ),
        (
            "function_call_output_list",
            [{"type": "function_call_output", "call_id": "c", "output": [{"type": "input_image", "image_url": "https://x"}]}],
            OPENAI_URL_REFUSAL,
        ),
        (
            "function_call_output_list_file_id",
            [{"type": "function_call_output", "call_id": "c", "output": [{"type": "input_image", "file_id": "file_1"}]}],
            OPENAI_URL_REFUSAL,
        ),
        (
            # The gap: a `computer_call_output`'s `output` is an OBJECT, not a list. Now a
            # `computer_call_output` ITEM is refused by its type before the walk (see the allowlist tests
            # below), so the walk's object branch is exercised through an item type that is allowed.
            "an_object_valued_output",
            [{"type": "function_call_output", "call_id": "c", "output": {"type": "computer_screenshot", "image_url": "https://x"}}],
            PREFIX + "a 'computer_screenshot' part may not reference a URL or a file",
        ),
        (
            "an_object_valued_output_file_id",
            [{"type": "function_call_output", "call_id": "c", "output": {"type": "computer_screenshot", "file_id": "file_1"}}],
            PREFIX + "a 'computer_screenshot' part may not reference a URL or a file",
        ),
        (
            "an_image_inside_a_part_that_carries_parts",  # not a shape today; the walk follows it anyway
            [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x", "content": [{"type": "input_image", "image_url": "https://x"}]}]}],
            OPENAI_URL_REFUSAL,
        ),
        (
            "input_file_url",
            [{"type": "message", "role": "user", "content": [{"type": "input_file", "file_url": "https://x"}]}],
            PREFIX + "an 'input_file' part may not reference a URL or a file",
        ),
    ],
)
def test_openai_input_items_are_walked_to_every_part(name, items, expected):
    assert openai_check_request_policy(_input_body(*items)) == expected


def test_openai_inline_data_urls_and_plain_text_still_pass():
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
        {"type": "function_call_output", "call_id": "c", "output": "plain text"},
        {"type": "function_call_output", "call_id": "c2", "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,AAAA"}},
        {"type": "function_call", "call_id": "c", "name": "n", "arguments": json.dumps({"output": [{"type": "input_image", "image_url": "https://x"}]})},
    ]

    assert openai_check_request_policy(_input_body(*items)) is None


# --- the input ITEM allowlist -------------------------------------
#
# `input` is an open set of item types and one of them, `item_reference`, makes the provider read a
# stored item by id: server-side state, the class ADR 0029 / 0035 already refuse as
# `previous_response_id` and `conversation`. The rule is an allowlist of the types the pinned runner
# is MEASURED to send, not a denylist of the one name.

# What the pinned runner replays (openai-agents 0.23.1 / openai 3.28.0), as recorded off the wire by
# `tests/test_openai_sdk_wire_integration.py` over a session whose model answered with a reasoning
# item, some text and two parallel function calls: the task text as a role message with NO `type`,
# then `reasoning`, `message`, `function_call` and `function_call_output`. Literals, copied from the
# measurement and not built from the proxy's constants.
MEASURED_RUNNER_ITEMS = [
    {"content": "Create a.txt.", "role": "user"},
    {"id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking"}], "type": "reasoning", "encrypted_content": "ENC1"},
    {
        "id": "msg_1",
        "content": [{"type": "output_text", "text": "I will write the file.", "annotations": []}],
        "role": "assistant",
        "status": "completed",
        "type": "message",
    },
    {"arguments": '{"path": "a.txt", "content": "x"}', "call_id": "call_1", "name": "write_file", "type": "function_call", "id": "fc_1", "status": "completed"},
    {"call_id": "call_1", "output": "ok", "type": "function_call_output"},
]


def test_every_input_item_the_runner_is_measured_to_replay_passes_whole_and_one_by_one():
    assert openai_check_request_policy(_input_body(*MEASURED_RUNNER_ITEMS)) is None
    for item in MEASURED_RUNNER_ITEMS:
        assert openai_check_request_policy(_input_body(item)) is None, item


def test_the_input_item_allowlist_is_pinned_literally():
    """Widening it is a deliberate edit with a measurement behind it."""
    from app.services import llm_proxy_openai

    assert llm_proxy_openai._ALLOWED_INPUT_ITEM_TYPES == frozenset(
        {"message", "reasoning", "function_call", "function_call_output"}
    )
    assert llm_proxy_openai._ROLES == frozenset({"user", "assistant", "system", "developer"})


ITEM_REFERENCE = {"type": "item_reference", "id": "msg_68a1b2c3d4e5f6"}
ITEM_REFERENCE_REFUSAL = PREFIX + "input item type 'item_reference' is not allowed"


@pytest.mark.parametrize("position", ["only", "first", "middle", "last"])
def test_an_item_reference_is_refused_wherever_it_sits_in_the_input(position):
    clean = MEASURED_RUNNER_ITEMS
    items = {
        "only": [ITEM_REFERENCE],
        "first": [ITEM_REFERENCE, *clean],
        "middle": [*clean[:2], ITEM_REFERENCE, *clean[2:]],
        "last": [*clean, ITEM_REFERENCE],
    }[position]

    assert openai_check_request_policy(_input_body(*items)) == ITEM_REFERENCE_REFUSAL


@pytest.mark.parametrize(
    "item_type",
    [
        "computer_call_output",
        "computer_call",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",
        "local_shell_call",
        "local_shell_call_output",
        "shell_call",
        "apply_patch_call",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
        "tool_search_call",
        "compaction",
        "an_item_type_shipped_next_year",
        "",  # empty
        "Message",  # not the value, whatever the case
        "message ",  # nor with a trailing blank
        "function_call_output\n",
    ],
    ids=repr,
)
def test_an_item_type_off_the_allowlist_is_refused_and_named(item_type):
    detail = openai_check_request_policy(_input_body({"type": item_type, "id": "x"}))

    assert detail == PREFIX + f"input item type {item_type!r} is not allowed"


@pytest.mark.parametrize(
    ("item_type", "shown"), [(None, "NoneType"), (7, "int"), (["message"], "list"), ({"a": 1}, "dict"), (True, "bool")], ids=repr
)
def test_an_item_whose_type_is_not_a_string_is_refused(item_type, shown):
    detail = openai_check_request_policy(_input_body({"type": item_type, "role": "user", "content": "x"}))

    assert detail == PREFIX + f"input item type {shown!r} is not allowed"


def test_a_long_item_type_is_cut_in_the_detail_not_echoed_whole():
    detail = openai_check_request_policy(_input_body({"type": "x" * 5000}))

    assert detail == PREFIX + f"input item type {'x' * 64!r} is not allowed"


@pytest.mark.parametrize("role", ["user", "assistant", "system", "developer"])
def test_a_role_message_with_no_type_is_the_one_typeless_item_admitted(role):
    assert openai_check_request_policy(_input_body({"role": role, "content": "x"})) is None
    assert openai_check_request_policy(_input_body({"role": role, "content": [{"type": "input_text", "text": "x"}]})) is None


@pytest.mark.parametrize(
    "item",
    [
        {"id": "msg_68a1b2c3d4e5f6"},  # what a reference looks like with its type left out
        {"content": "x"},
        {},
        {"role": None, "content": "x"},
        {"role": 7, "content": "x"},
        {"role": ["user"], "content": "x"},
        {"role": "tool", "content": "x"},
        {"role": "User", "content": "x"},
        {"role": "", "content": "x"},
    ],
    ids=repr,
)
def test_an_item_with_no_type_and_no_role_message_shape_is_refused(item):
    detail = openai_check_request_policy(_input_body(item))

    assert detail == PREFIX + "an 'input' item has no type and is not a role message"


@pytest.mark.parametrize("entry", ["plain text", None, 7, ["message"], True], ids=repr)
def test_an_input_entry_that_is_not_an_object_is_refused(entry):
    assert openai_check_request_policy(_input_body(entry)) == PREFIX + "an 'input' entry is not an object"


def test_a_string_input_is_the_task_text_and_is_not_an_item_list():
    body = json.dumps({"model": "gpt-5-mini", "store": False, "service_tier": "default", "input": "hello"}).encode()

    assert openai_check_request_policy(body) is None


def test_the_item_rule_runs_before_the_part_walk_and_before_the_model_rule():
    """Each refusal names its own condition, in this order: an item type, then a part, then the model."""
    url_image_in_a_reference_body = json.dumps(
        {
            "model": "not-a-priced-model",
            "store": False,
            "service_tier": "default",
            "input": [ITEM_REFERENCE, {"role": "user", "content": [{"type": "input_image", "image_url": "https://x"}]}],
        }
    ).encode()
    url_image_only = _input_body({"role": "user", "content": [{"type": "input_image", "image_url": "https://x"}]})

    assert openai_check_request_policy(url_image_in_a_reference_body) == ITEM_REFERENCE_REFUSAL
    assert openai_check_request_policy(url_image_only) == OPENAI_URL_REFUSAL


def test_an_item_reference_through_the_proxy_is_refused_recorded_and_never_forwarded():
    stub = Stub(OPENAI.reply(10, 5))

    async def scenario():
        app, registry, token = _build(OPENAI, _stub_client(stub))
        async with _proxy_client(app) as client:
            refused = await client.post(
                OPENAI.path,
                headers=OPENAI.auth(token),
                json=OPENAI.body(input=[{"role": "user", "content": "hi"}, ITEM_REFERENCE]),
            )
            clean = await client.post(OPENAI.path, headers=OPENAI.auth(token), json=OPENAI.body())
        return refused, clean, registry.usage_for(token)

    refused, clean, usage = _run(scenario())

    assert refused.status_code == 403
    assert refused.json()["error"]["message"] == ITEM_REFERENCE_REFUSAL
    assert refused.json()["error"]["code"] == "forge_policy_refusal"
    assert usage.policy_refusals == [ITEM_REFERENCE_REFUSAL]  # recorded: the build will fail closed
    assert usage.tokens == 15 and clean.status_code == 200  # the clean request after it still goes
    assert len(stub.calls) == 1  # only the clean one reached the upstream


# =============================================================================================
# The sync seam: the per-dialect rules also hold through the plain TestClient (the shape the
# older suites use), so a change to how requests are read cannot hide behind the async driver.
# =============================================================================================


@pytest.mark.parametrize("kit", KITS)
def test_a_refusal_through_the_sync_client_is_recorded_on_the_usage_endpoint(kit):
    stub = Stub(kit.reply(10, 5))
    registry = TokenRegistry()
    app = create_app(
        registry,
        real_api_key=REAL_KEY,
        admin_secret=ADMIN_SECRET,
        http_client=_stub_client(stub),
        dialect=kit.dialect,
    )
    client = TestClient(app)
    token = client.post(
        "/_forge_admin/tokens",
        json={"build_id": "b", "token_budget": BIG, "cost_budget_usd": 10.0},
        headers={"X-Forge-Admin-Secret": ADMIN_SECRET},
    ).json()["token"]

    resp = client.post(kit.path, headers=kit.auth(token), json=kit.body(model="not-priced"))
    usage = client.get(
        f"/_forge_admin/tokens/{token}/usage", headers={"X-Forge-Admin-Secret": ADMIN_SECRET}
    ).json()

    assert resp.status_code == 403
    assert usage["policy_refusals"] == [PREFIX + "model 'not-priced' has no price row, so it is not allowed"]
    assert stub.calls == []
