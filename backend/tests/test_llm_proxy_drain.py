"""What the proxy does when a call outlives its caller, for BOTH
dialects.

1. **A cancelled forwarded call leaves the token unmetered.** When a handler is cancelled after its
   call went upstream, the provider may have billed it and nothing here can learn the cost; the
   next budget check would under-count. The token is marked unmetered (the fail-closed state an
   unmeterable response leaves), so every later request is refused and the build ends
   BUDGET_EXCEEDED. A cancel BEFORE the forward began (a request still waiting for the slot) forwards
   nothing, so it marks nothing.
2. **A token can be drained.** `POST /_forge_admin/tokens/{token}/drain` closes the token (no new
   request admitted, a queued one refused at the slot) without forgetting it, then waits, bounded,
   for the requests already alive to finish and record, so the usage read after it is final.

No Docker, no network: the proxy is driven in process over a stub upstream that parks calls on an
event the test sets. Every wait here is BOUNDED when the thing under test is removed (PLAN durable
rule): the requests are a small constant, each await is wrapped in a timeout, the parked calls are
released in a `finally`, and a missing drain or a missing mark fails by an assertion, never by
waiting.
"""

from __future__ import annotations

import asyncio
import time

import pytest

# The helpers (a stub upstream that parks calls, the two dialects behind one shape) are the ones
# `test_llm_proxy_policy.py` built; the concurrency scenarios here are the same scenarios.
from tests.test_llm_proxy_policy import (  # noqa: F401 - re-used helpers
    ADMIN_SECRET,
    ANTHROPIC,
    KITS,
    WAIT,
    Stub,
    _build,
    _post,
    _proxy_client,
    _run,
    _stub_client,
    _until,
)
from app.services.llm_proxy import TokenRegistry, create_app

CANCELLED_REASON = "a forwarded call was cancelled before its usage could be recorded"
ADMIN = {"X-Forge-Admin-Secret": ADMIN_SECRET}


def _drain(client, token: str, timeout_seconds: float, *, headers=ADMIN):
    return client.post(f"/_forge_admin/tokens/{token}/drain?timeout_seconds={timeout_seconds}", headers=headers)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT)


# =============================================================================================
# 1. A cancelled forwarded call
# =============================================================================================


@pytest.mark.parametrize("kit", KITS)
def test_a_call_cancelled_after_the_forward_began_leaves_the_token_unmetered(kit):
    """The handler is cancelled while its upstream call is out. Nothing was recorded (that is the
    problem), so the token is unmetered with the platform's reason; the usage endpoint says so,
    and the next request is refused 403 USAGE_UNMETERED and never forwarded."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            doomed = asyncio.create_task(_post(client, kit, token))
            try:
                await _until(lambda: stub.started == 1, "the doomed call to reach the upstream")
                await _cancel(doomed)
                follow_up = await asyncio.wait_for(_post(client, kit, token), WAIT)
                usage_over_http = (await client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN)).json()
            finally:
                stub.release.set()
        return follow_up, usage_over_http, registry.usage_for(token), registry._lookup(token)

    follow_up, usage_over_http, usage, entry = _run(scenario())

    assert follow_up.status_code == 403
    assert follow_up.text == "USAGE_UNMETERED: " + CANCELLED_REASON
    assert len(stub.calls) == 1  # the follow-up never reached the upstream
    assert usage.tokens == 0 and usage.cost_usd == 0.0  # the cancelled call recorded nothing
    assert usage.unmetered is True and usage.unmetered_reason == CANCELLED_REASON
    assert usage_over_http["unmetered"] is True and usage_over_http["unmetered_reason"] == CANCELLED_REASON
    assert entry.inflight == 0 and not entry.slot.locked()  # and the slot is still released


@pytest.mark.parametrize("kit", KITS)
def test_the_mark_is_made_before_the_slot_is_released_so_a_queued_request_sees_it(kit):
    """A request waiting behind the doomed one takes the slot the moment it is released. It must
    find the token already unmetered: refused, not forwarded and billed with no record of it."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            doomed = asyncio.create_task(_post(client, kit, token))
            queued = None
            try:
                await _until(lambda: stub.started == 1, "the doomed call to reach the upstream")
                queued = asyncio.create_task(_post(client, kit, token))
                await _until(lambda: registry._lookup(token).inflight == 2, "the second request to be queued")
                await _cancel(doomed)
                queued_response = await asyncio.wait_for(queued, WAIT)
            finally:
                stub.release.set()
                if queued is not None and not queued.done():
                    queued.cancel()
        return queued_response, registry.usage_for(token)

    queued_response, usage = _run(scenario())

    assert queued_response.status_code == 403
    assert queued_response.text == "USAGE_UNMETERED: " + CANCELLED_REASON
    assert len(stub.calls) == 1
    assert usage.unmetered is True


@pytest.mark.parametrize("kit", KITS)
def test_a_request_cancelled_while_it_waits_for_the_slot_forwarded_nothing_and_marks_nothing(kit):
    """The other side of the rule: a cancel BEFORE the forward began (the request is still queued)
    sent nothing upstream, so there is no unknown spend. The token stays metered; the call that
    was out completes and is recorded; the next request goes through."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            first = asyncio.create_task(_post(client, kit, token))
            second = None
            try:
                await _until(lambda: stub.started == 1, "the first call to reach the upstream")
                second = asyncio.create_task(_post(client, kit, token))
                await _until(lambda: registry._lookup(token).inflight == 2, "the second request to be queued")
                await _cancel(second)
                stub.release.set()
                first_response = await asyncio.wait_for(first, WAIT)
                third_response = await asyncio.wait_for(_post(client, kit, token), WAIT)
            finally:
                stub.release.set()
                for task in (first, second):
                    if task is not None and not task.done():
                        task.cancel()
        return first_response, third_response, registry.usage_for(token), registry._lookup(token)

    first_response, third_response, usage, entry = _run(scenario())

    assert first_response.status_code == 200 and third_response.status_code == 200
    assert len(stub.calls) == 2  # the first and the third; the cancelled second never went out
    assert usage.unmetered is False and usage.unmetered_reason is None
    assert usage.tokens == 30  # two calls of 15, both recorded
    assert entry.inflight == 0 and not entry.slot.locked()


# =============================================================================================
# 2. Draining a token
# =============================================================================================


@pytest.mark.parametrize("kit", KITS)
def test_drain_waits_for_a_call_in_flight_and_the_usage_read_after_it_includes_the_spend(kit):
    """A call is out when the drain starts (the sandbox died with it in flight). The drain does
    not return while it is out; once it answers and records, the drain returns `drained: true` and
    the usage then holds the call's tokens. A read made when the drain STARTED would have held 0."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            call = asyncio.create_task(_post(client, kit, token))
            drain = None
            try:
                await _until(lambda: stub.started == 1, "the call to reach the upstream")
                tokens_when_the_drain_started = registry.usage_for(token).tokens
                drain = asyncio.create_task(_drain(client, token, 5))
                await asyncio.sleep(0.3)
                waiting = not drain.done()
                stub.release.set()
                drain_response = await asyncio.wait_for(drain, WAIT)
                call_response = await asyncio.wait_for(call, WAIT)
            finally:
                stub.release.set()
                for task in (call, drain):
                    if task is not None and not task.done():
                        task.cancel()
        return waiting, tokens_when_the_drain_started, drain_response, call_response, registry.usage_for(token)

    waiting, tokens_at_start, drain_response, call_response, usage = _run(scenario())

    assert tokens_at_start == 0
    assert waiting is True  # the drain was still waiting while the call was parked
    assert drain_response.status_code == 200
    assert drain_response.json() == {"drained": True, "inflight": 0}
    assert call_response.status_code == 200  # the call was NOT cut short; it finished and recorded
    assert usage.tokens == 15


@pytest.mark.parametrize("kit", KITS)
def test_a_drained_token_admits_nothing_and_a_queued_request_is_refused_not_forwarded(kit):
    """After the drain starts: a new request is the 401 a revoked token gets, and a request that
    was already queued behind the call in flight is refused when it reaches the slot -- so only the
    ONE call that was out is waited for, never the queue behind it."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            call = asyncio.create_task(_post(client, kit, token))
            queued = drain = None
            try:
                await _until(lambda: stub.started == 1, "the call to reach the upstream")
                queued = asyncio.create_task(_post(client, kit, token))
                await _until(lambda: registry._lookup(token).inflight == 2, "the second request to be queued")
                drain = asyncio.create_task(_drain(client, token, 5))
                await _until(lambda: registry._lookup(token) is None, "the token to be closed")
                late = await asyncio.wait_for(_post(client, kit, token), WAIT)
                stub.release.set()
                results = await asyncio.wait_for(asyncio.gather(call, queued, drain), WAIT)
            finally:
                stub.release.set()
                for task in (call, queued, drain):
                    if task is not None and not task.done():
                        task.cancel()
        return late, results, registry.usage_for(token)

    late, (call_response, queued_response, drain_response), usage = _run(scenario())

    assert late.status_code == 401 and late.text == "unknown or revoked proxy token"
    assert call_response.status_code == 200
    assert queued_response.status_code == 401 and queued_response.text == "unknown or revoked proxy token"
    assert drain_response.json() == {"drained": True, "inflight": 0}
    assert len(stub.calls) == 1  # neither the late nor the queued request was forwarded
    assert usage.tokens == 15


@pytest.mark.parametrize("kit", KITS)
def test_a_drain_that_runs_out_of_time_says_so_and_does_not_hang(kit):
    """A call that does not finish in the wait: the drain answers `drained: false` with the count of
    requests still alive, after about the wait it was given (not before, not much after), and
    asking again once the call has finished says `drained: true`."""
    stub = Stub(kit.reply(10, 5), park=1)

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            call = asyncio.create_task(_post(client, kit, token))
            try:
                await _until(lambda: stub.started == 1, "the call to reach the upstream")
                started = time.monotonic()
                short = await asyncio.wait_for(_drain(client, token, 0.4), WAIT)
                waited = time.monotonic() - started
                stub.release.set()
                await asyncio.wait_for(call, WAIT)
                again = await asyncio.wait_for(_drain(client, token, 0.4), WAIT)
            finally:
                stub.release.set()
                if not call.done():
                    call.cancel()
        return short, waited, again

    short, waited, again = _run(scenario())

    assert short.status_code == 200 and short.json() == {"drained": False, "inflight": 1}
    assert 0.35 <= waited < 3.0  # it waited for what it was given, and returned
    assert again.json() == {"drained": True, "inflight": 0}


@pytest.mark.parametrize("kit", KITS)
def test_draining_an_idle_token_is_immediate_idempotent_and_leaves_the_record_readable(kit):
    """The ordinary build: nothing is alive. The drain is one round trip, can be repeated, and
    `usage` and `trace` still answer afterwards (a revoke would have deleted both); the real revoke
    after it removes the token."""
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            assert (await _post(client, kit, token)).status_code == 200
            started = time.monotonic()
            first = await asyncio.wait_for(_drain(client, token, 5), WAIT)
            second = await asyncio.wait_for(_drain(client, token, 5), WAIT)
            elapsed = time.monotonic() - started
            usage = await client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN)
            trace = await client.get(f"/_forge_admin/tokens/{token}/trace", headers=ADMIN)
            revoked = await client.delete(f"/_forge_admin/tokens/{token}", headers=ADMIN)
            usage_after_revoke = await client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN)
        return first, second, elapsed, usage, trace, revoked, usage_after_revoke

    first, second, elapsed, usage, trace, revoked, usage_after_revoke = _run(scenario())

    assert first.json() == second.json() == {"drained": True, "inflight": 0}
    assert elapsed < 2.0
    assert usage.status_code == 200 and usage.json()["tokens"] == 15
    assert trace.status_code == 200 and trace.json()["calls_total"] == 1
    assert revoked.status_code == 200
    assert usage_after_revoke.status_code == 404


def test_the_drain_route_needs_the_admin_secret_and_an_unauthorised_call_closes_nothing():
    kit = ANTHROPIC
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, _stub_client(stub))
        async with _proxy_client(app) as client:
            no_secret = await _drain(client, token, 1, headers={})
            wrong_secret = await _drain(client, token, 1, headers={"X-Forge-Admin-Secret": "not-the-secret"})
            still_serves = await _post(client, kit, token)
        return no_secret, wrong_secret, still_serves

    no_secret, wrong_secret, still_serves = _run(scenario())

    assert no_secret.status_code == 403 and wrong_secret.status_code == 403
    assert still_serves.status_code == 200  # the token was not closed by either


def test_the_drain_route_answers_404_for_an_unknown_token_and_422_for_a_wait_out_of_bounds():
    kit = ANTHROPIC

    async def scenario():
        app, registry, token = _build(kit, _stub_client(Stub(kit.reply(10, 5))))
        async with _proxy_client(app) as client:
            unknown = await _drain(client, "no-such-token", 1)
            bad = {
                value: await _drain(client, token, value) for value in ("-1", "121", "1e9", "nan", "inf", "abc")
            }
            at_the_bound = await _drain(client, token, 120)
        return unknown, bad, at_the_bound

    unknown, bad, at_the_bound = _run(scenario())

    assert unknown.status_code == 404
    assert {value: response.status_code for value, response in bad.items()} == dict.fromkeys(bad, 422)
    assert at_the_bound.status_code == 200  # 120 s is the endpoint's bound, and it is allowed (idle: immediate)


def test_the_sandbox_facing_app_does_not_serve_the_drain_route_at_all():
    kit = ANTHROPIC
    registry = TokenRegistry()
    app = create_app(
        registry,
        real_api_key="k",
        admin_secret=ADMIN_SECRET,
        http_client=_stub_client(Stub(kit.reply(10, 5))),
        dialect=kit.dialect,
        serve="proxy",
    )
    token = registry.issue("b", token_budget=10**9, cost_budget_usd=10.0)

    async def scenario():
        async with _proxy_client(app) as client:
            response = await _drain(client, token, 1)
        return response

    response = _run(scenario())

    assert response.status_code == 404
    assert registry._lookup(token) is not None  # and the token was not closed by trying


# =============================================================================================
# 3. The registry's side of it
# =============================================================================================


def test_admit_counts_refuses_at_the_cap_and_closes_atomically():
    registry = TokenRegistry()
    token = registry.issue("b", token_budget=10, cost_budget_usd=1.0)

    entry, admitted = registry._admit(token, 2)
    assert admitted is True and entry.inflight == 1
    same, admitted = registry._admit(token, 2)
    assert admitted is True and same is entry and entry.inflight == 2
    same, admitted = registry._admit(token, 2)
    assert admitted is False and same is entry and entry.inflight == 2  # at the cap: counted nothing

    registry._release(entry)
    assert entry.inflight == 1

    assert registry.close(token) is True
    assert registry._admit(token, 2) == (None, False)  # closed: not admitted, and not "at the cap"
    assert registry._lookup(token) is None
    assert registry.usage_for(token) is not None  # but still readable
    assert registry.close(token) is True  # idempotent
    assert registry.close("no-such-token") is False
    assert registry._admit("no-such-token", 2) == (None, False)


@pytest.mark.parametrize("kit", KITS)
def test_the_in_flight_count_is_only_ever_changed_with_the_registry_lock_held(kit, monkeypatch):
    """The drain reads `inflight` from another thread and decides from it, so the count and the
    `closed` check have to be one step: every change to `inflight` -- the handler's admission and its
    release, through the real app -- happens with the registry's lock held. (A race between a
    request's look-up and its increment cannot be provoked on demand; this pins the structure that
    rules it out.)"""
    from app.services import llm_proxy

    registry = TokenRegistry()
    held_when_changed: list[bool] = []

    class Watched(llm_proxy._TokenEntry):
        @property
        def inflight(self) -> int:
            return self.__dict__.get("_inflight", 0)

        @inflight.setter
        def inflight(self, value: int) -> None:
            held_when_changed.append(registry._lock.locked())
            self.__dict__["_inflight"] = value

    monkeypatch.setattr(llm_proxy, "_TokenEntry", Watched)
    stub = Stub(kit.reply(10, 5))

    async def scenario():
        app = create_app(
            registry, real_api_key="k", admin_secret=ADMIN_SECRET, http_client=_stub_client(stub), dialect=kit.dialect
        )
        token = registry.issue("b", token_budget=10**9, cost_budget_usd=10.0)
        async with _proxy_client(app) as client:
            assert (await _post(client, kit, token)).status_code == 200
            assert (await _post(client, kit, token, model="not-a-priced-model")).status_code == 403
        return token

    token = _run(scenario())

    # the entry's own construction (0), then two requests x (admit, release)
    assert len(held_when_changed) == 5, held_when_changed
    assert all(held_when_changed), held_when_changed
    assert registry._lookup(token).inflight == 0


def test_is_idle_is_false_while_a_request_is_alive_or_the_slot_is_held():
    registry = TokenRegistry()
    token = registry.issue("b", token_budget=10, cost_budget_usd=1.0)
    assert registry.is_idle("no-such-token") is None
    assert registry.is_idle(token) == (True, 0)

    entry, _ = registry._admit(token, 8)
    assert registry.is_idle(token) == (False, 1)
    registry._release(entry)
    assert registry.is_idle(token) == (True, 0)

    async def with_the_slot_held():
        await entry.slot.acquire()
        try:
            return registry.is_idle(token)
        finally:
            entry.slot.release()

    assert asyncio.run(asyncio.wait_for(with_the_slot_held(), WAIT)) == (False, 0)
    assert registry.is_idle(token) == (True, 0)

    registry.revoke(token)
    assert registry.is_idle(token) is None
