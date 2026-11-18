"""Every way a forwarded call can end WITHOUT a measured
response leaves the token unmetered, for BOTH dialects.

One was already handled: a handler cancelled mid-call. Anything else that left `client.request(...)`
(a `RuntimeError` from the transport, an error raised while the response body was being read)
released the slot with the call's spend unrecorded and the token still usable, so the next budget
check under-counted by a call the provider may have billed. The boundary is now "anything but a
response": the families below are the ones that reach it, and the last block pins what is
deliberately NOT marked, so a reader of this file sees both sides of the rule.

No Docker, no network. The proxy is driven in process over a transport that fails on purpose; every
await is bounded, and nothing here waits on anything the test does not set itself.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import llm_proxy
from tests.test_llm_proxy_policy import KITS, WAIT, _build, _post, _proxy_client, _run

UNEXPECTED_REASON = "a forwarded call failed unexpectedly before its usage could be recorded"
HOSTILE = "library-message-that-must-not-reach-the-build-HOSTILE-7c1"


class _Transport(httpx.AsyncBaseTransport):
    """An upstream whose FIRST call is `first(request)` and whose later calls answer `reply`."""

    def __init__(self, first, reply: bytes):
        self._first, self._reply, self.calls = first, reply, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            return await self._first(request)
        return httpx.Response(200, content=self._reply, headers={"content-type": "application/json"})


class _ExplodingBody(httpx.AsyncByteStream):
    """A response body that starts and then fails: what an error raised while reading looks like
    (the status line and headers arrived, so the call WAS sent and accepted)."""

    def __init__(self, error: BaseException):
        self._error = error

    async def __aiter__(self):
        yield b'{"id": "msg_1", "mod'
        raise self._error


def _raising(error: BaseException):
    async def first(request):
        if isinstance(error, httpx.HTTPError):
            raise type(error)(str(error), request=request)
        raise error

    return first


def _failing_while_reading(error: BaseException):
    async def first(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_ExplodingBody(error))

    return first


def _two_calls(kit, first):
    """The first request meets `first`; then a second, well-formed one is sent. Returns
    `(first_response, second_response, upstream_calls, usage)`."""
    transport = _Transport(first, kit.reply(10, 5))

    async def scenario():
        app, registry, token = _build(kit, httpx.AsyncClient(transport=transport, base_url="http://upstream.test"))
        async with _proxy_client(app, raise_app_exceptions=False) as client:
            one = await asyncio.wait_for(_post(client, kit, token), WAIT)
            two = await asyncio.wait_for(_post(client, kit, token), WAIT)
        return one, two, registry.usage_for(token), registry._lookup(token)

    one, two, usage, entry = _run(scenario())
    assert entry.inflight == 0 and not entry.slot.locked()  # the slot is free on every path here
    return one, two, transport.calls, usage


# =============================================================================================
# Exceptions that reach the boundary leave the token unmetered
# =============================================================================================

_UNEXPECTED = [
    RuntimeError(HOSTILE),
    ValueError(HOSTILE),
    OSError(HOSTILE),
    KeyError(HOSTILE),
    type("ThirdPartyError", (Exception,), {})(HOSTILE),
]


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize("error", _UNEXPECTED, ids=lambda e: type(e).__name__)
def test_any_exception_raised_after_the_send_leaves_the_token_unmetered(kit, error):
    """An arbitrary exception raised by the transport while the call is out. Previously this
    released the slot, recorded nothing, and let the next request through (it was FORWARDED)."""
    one, two, calls, usage = _two_calls(kit, _raising(error))

    assert one.status_code == 500  # unchanged: the exception still propagates, the proxy is not hiding it
    assert usage.unmetered is True
    assert usage.unmetered_reason == UNEXPECTED_REASON
    assert HOSTILE not in usage.unmetered_reason  # a library's message never reaches a build's failure detail
    assert two.status_code == 403
    assert two.text == f"USAGE_UNMETERED: {UNEXPECTED_REASON}"
    assert calls == 1  # the second request was refused, never forwarded
    assert usage.tokens == 0 and usage.cost_usd == 0.0  # nothing was invented either


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize("error", [RuntimeError(HOSTILE), ValueError(HOSTILE)], ids=lambda e: type(e).__name__)
def test_an_exception_raised_while_the_response_is_being_read_leaves_the_token_unmetered(kit, error):
    """The other family: the response HEADERS arrived (the provider accepted the call and may bill
    it) and the body failed to read. Nothing was measured, so the token is unmetered."""
    one, two, calls, usage = _two_calls(kit, _failing_while_reading(error))

    assert one.status_code == 500
    assert usage.unmetered is True and usage.unmetered_reason == UNEXPECTED_REASON
    assert two.status_code == 403 and two.text.startswith("USAGE_UNMETERED: ")
    assert calls == 1


@pytest.mark.parametrize("kit", KITS)
def test_a_figure_that_was_read_but_could_not_be_recorded_leaves_the_token_unmetered(kit, monkeypatch):
    """The third family, AFTER the response: the usage was read and priced, and writing it onto
    the token raised. It used to propagate out of `_account_usage` (which promises never to raise)
    with nothing marked."""
    transport = _Transport(
        lambda request: _answer(kit.reply(10, 5)),  # the first call is a good 2xx
        kit.reply(10, 5),
    )

    def broken_record(self, token, *, tokens, cost_usd):
        raise RuntimeError(HOSTILE)

    monkeypatch.setattr(llm_proxy.TokenRegistry, "_record", broken_record)

    async def scenario():
        app, registry, token = _build(kit, httpx.AsyncClient(transport=transport, base_url="http://upstream.test"))
        async with _proxy_client(app, raise_app_exceptions=False) as client:
            one = await asyncio.wait_for(_post(client, kit, token), WAIT)
            two = await asyncio.wait_for(_post(client, kit, token), WAIT)
        return one, two, registry.usage_for(token)

    one, two, usage = _run(scenario())

    assert one.status_code == 200  # the response is still returned: the call was made and answered
    assert usage.unmetered is True
    assert usage.unmetered_reason == "unexpected error while reading usage"
    assert HOSTILE not in usage.unmetered_reason
    assert two.status_code == 403 and two.text.startswith("USAGE_UNMETERED: ")
    assert transport.calls == 1


async def _answer(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


# =============================================================================================
# What is deliberately NOT marked (the open PLAN decision, and the baseline)
# =============================================================================================

_TRANSPORT_ERRORS = [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("no answer in time"),
    httpx.ReadError("connection reset"),
    httpx.RemoteProtocolError("server disconnected"),
]


@pytest.mark.parametrize("kit", KITS)
@pytest.mark.parametrize("error", _TRANSPORT_ERRORS, ids=lambda e: type(e).__name__)
def test_an_httpx_transport_error_still_answers_502_and_does_not_mark_the_token(kit, error):
    """PINS TODAY'S BEHAVIOUR of the one family that was not changed, not a verdict on it. A
    transport error after the request left (a read timeout the provider may still bill) is the open
    PLAN decision ("a transport error after the request was sent is unrecorded spend"): marking
    every network blip would fail builds, and the cost of leaving it is a possible under-count.
    When that decision is made, this test is the one that changes."""
    one, two, calls, usage = _two_calls(kit, _raising(error))

    assert (one.status_code, one.text) == (502, "ENGINE_UNREACHABLE")
    assert usage.unmetered is False and usage.unmetered_reason is None
    assert two.status_code == 200  # the next request is forwarded as before
    assert calls == 2


@pytest.mark.parametrize("kit", KITS)
def test_a_clean_call_is_not_marked_by_the_boundary(kit):
    """Non-vacuity for the boundary: a normal response, metered, leaves the token usable."""
    one, two, calls, usage = _two_calls(kit, lambda request: _answer(kit.reply(10, 5)))

    assert one.status_code == 200 and two.status_code == 200
    assert usage.unmetered is False
    assert usage.tokens == 30 and calls == 2  # two calls of 15 tokens each
