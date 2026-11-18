"""The executor drains the proxy's token
before it reads the usage, for as long as the proxy's own upstream timeout, and a drain that cannot
finish does not overrule the reason the build already had.

A call can outlive its sandbox: the sandbox died (a timeout, a crash, a harvest limit) with a request
out, uvicorn does not cancel a handler when its client goes, and the call records its spend when the
provider answers, AFTER a read made at once. That spend would be missing from `builds.cost_usd`.
So `_read_proxy_usage` first asks the proxy to drain the token (close it, wait, bounded, for the
requests alive to record) and reads the usage only after.

the proxy waits up to `llm_proxy.UPSTREAM_TIMEOUT_SECONDS` for a provider call, and one exec
cannot wait that long (docker-py's read timeout is 60 s), so the executor asks in STEPS until the
proxy says idle or a total derived from that constant is spent. A drain that runs out of time or
cannot be asked leaves the cost NULL and never stores a short figure; the build keeps the reason it
already had (a TIMEOUT stays a TIMEOUT, whose cost could not be read back), and is INFRA_ERROR only
when nothing else failed first.

Two seams, no Docker:

- **A fake `ProxyContainer`** whose `drain()` returns, or raises, what the real one does: the
  decisions the executor makes on each answer.
- **The REAL proxy apps** (`llm_proxy.create_app`) over one registry on TWO event loops in two
  threads, the shape of the proxy container (a sandbox-facing listener and a loopback admin
  listener), over a stub upstream that holds a call. The fake adapter starts a request on the proxy
  loop and returns at once, which is exactly "the sandbox is gone and its call is still out". So the
  executor really waits, the drain really crosses threads, and the figure it stores is the real
  registry's.

Every wait is BOUNDED when the thing under test is removed: the stub holds a
call for a fixed number of seconds at most whether or not the test releases it, every cross-thread
wait has a timeout, the fake proxy raises a `BaseException` (which no `except Exception` can turn
into a clean exit) once the executor has asked for more drain steps than any bound allows, and a
missing drain fails by the cost figure, not by waiting.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from app.services import llm_proxy
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.llm_proxy import TokenRegistry, create_app
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output
from tests.test_llm_proxy_policy import ADMIN_SECRET, ANTHROPIC, REAL_KEY

TOKEN = "tok-SECRET-0123456789"
ADMIN = {"X-Forge-Admin-Secret": ADMIN_SECRET}
CANCELLED_REASON = "a forwarded call was cancelled before its usage could be recorded"
MCP = "tool policy: request field 'mcp_servers' is not allowed"
COST_NOTE = "the LLM proxy's usage could not be read back at build end, so this build's cost is unknown: "
RAN_OUT = "a call was still in flight on the proxy after waiting 150 s, so its spend could not be included"


class _Network:
    def remove(self):
        pass


def _usage(**over):
    base = {
        "tokens": 7,
        "cost_usd": 0.0001,
        "budget_exceeded": False,
        "policy_refusals": [],
        "unmetered": False,
        "unmetered_reason": None,
    }
    base.update(over)
    return base


def _executor_with_proxy(monkeypatch, proxy, adapter):
    """An executor whose `ProxyContainer` is `proxy` and whose adapter is `adapter`; `_publish` is a
    tripwire so a build that should have failed cannot quietly reach it."""
    reached_publish: list = []
    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
    monkeypatch.setattr(sbe, "ProxyContainer", lambda *a, **k: proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=adapter)
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor,
        "_publish",
        lambda *a, **k: reached_publish.append(True) or sbe.ExecutionResult(ok=True, summary="published"),
    )
    return executor, reached_publish


# =============================================================================================
# 1. What the executor decides on each answer from the drain (a fake ProxyContainer)
# =============================================================================================

MAX_FAKE_DRAINS = 40


class _Runaway(BaseException):
    """The fake proxy's guard. The executor asked for far more drain steps than the constants allow
    (at most five at the defaults), so its loop has lost its exit. A `BaseException` on purpose: the
    executor's `except Exception` around a drain would turn an ordinary error into a 'failed drain'
    that ENDS the loop and lets a broken exit pass; this one ends the test, red, in milliseconds."""


class _FakeProxy:
    base_url_for_sandbox = "http://proxy"

    def __init__(self, *, drain, usage=None):
        """`drain`: one answer for every step, or a list (one per step, the last one repeated). An
        answer that is an exception is raised. `usage`: a dict, or an exception to raise."""
        self._drain = drain
        self._usage = usage if usage is not None else _usage()
        self.calls: list[tuple] = []
        self.start_kwargs: dict | None = None

    @property
    def drain_waits(self) -> list[float]:
        return [call[2] for call in self.calls if call[0] == "drain"]

    def start(self, **kwargs):
        self.start_kwargs = kwargs

    def issue_token(self, *args, **kwargs):
        return TOKEN

    def drain(self, token, *, timeout_seconds):
        self.calls.append(("drain", token, timeout_seconds))
        if len(self.drain_waits) > MAX_FAKE_DRAINS:
            raise _Runaway(f"the executor asked for more than {MAX_FAKE_DRAINS} drain steps")
        if isinstance(self._drain, list):
            answer = self._drain[min(len(self.drain_waits), len(self._drain)) - 1]
        else:
            answer = self._drain
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def usage(self, token):
        self.calls.append(("usage", token))
        if isinstance(self._usage, BaseException):
            raise self._usage
        return dict(self._usage) if isinstance(self._usage, dict) else self._usage

    def trace(self, token):
        return {"steps": [], "calls_total": 0, "content_truncated": False}

    def revoke(self, token):
        self.calls.append(("revoke", token))

    def stop(self):
        pass


class _OkAdapter:
    def __init__(self, result=None, *, raises: Exception | None = None):
        self._result = result
        self._raises = raises

    def run(self, adapter_input):
        if self._raises is not None:
            raise self._raises
        return with_output(self._result or AgentAdapterResult(ok=True, summary="done"))


def _run(monkeypatch, make_build_with_request, proxy, adapter=None):
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter or _OkAdapter())
    build = make_build_with_request()
    build.cost_tokens, build.cost_usd = 41, 4.1  # a NULL must OVERWRITE a stale figure, not be left alone
    return executor.execute(build), build, reached_publish


def test_the_proxy_is_drained_before_its_usage_is_read_and_for_the_modules_wait(monkeypatch, make_build_with_request):
    proxy = _FakeProxy(drain=True)

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy)

    assert result.ok is True and reached_publish == [True]
    assert proxy.calls == [("drain", TOKEN, 30.0), ("usage", TOKEN), ("revoke", TOKEN)]
    assert build.cost_tokens == 7  # the figure read AFTER the drain is the one stored


def test_the_drain_step_and_margin_are_pinned_and_a_step_sits_under_the_docker_clients_read_timeout():
    """Literals. A step is one `exec`, so it must be shorter than docker-py's own 60 s read timeout
    (the exec adds its own 5 s margin on top: `ProxyContainer.drain`). The proxy's upstream timeout
    is pinned here once, so the tests below may read the production constant on the expected side."""
    assert llm_proxy.UPSTREAM_TIMEOUT_SECONDS == 120.0
    assert sbe.PROXY_DRAIN_STEP_SECONDS == 30.0
    assert sbe.PROXY_DRAIN_MARGIN_SECONDS == 30.0
    assert sbe.PROXY_DRAIN_STEP_SECONDS + 5.0 < 60.0


def test_the_total_wait_covers_the_proxys_upstream_timeout_with_a_margin():
    """a call whose caller died can stay out for as long as the proxy waits on the provider."""
    assert sbe.PROXY_DRAIN_TOTAL_SECONDS == llm_proxy.UPSTREAM_TIMEOUT_SECONDS + sbe.PROXY_DRAIN_MARGIN_SECONDS
    assert sbe.PROXY_DRAIN_TOTAL_SECONDS > llm_proxy.UPSTREAM_TIMEOUT_SECONDS
    assert sbe.PROXY_DRAIN_TOTAL_SECONDS >= 125.0  # the literal 120 plus at least a few seconds
    assert sbe.PROXY_DRAIN_TOTAL_SECONDS == 150.0


def test_the_total_is_written_in_terms_of_the_proxys_constant_and_not_as_a_number():
    """The number 150 above passes for a restated `150.0` too, and then the two drift apart the day
    the proxy's timeout changes. So the assignment itself must name the proxy's constant and carry no
    number of its own."""
    tree = ast.parse(Path(sbe.__file__).read_text(encoding="utf-8"))
    (assign,) = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["PROXY_DRAIN_TOTAL_SECONDS"]
    ]
    names = {n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)}
    numbers = [n.value for n in ast.walk(assign.value) if isinstance(n, ast.Constant)]

    assert "UPSTREAM_TIMEOUT_SECONDS" in names
    assert numbers == []


def test_the_proxys_own_upstream_client_is_built_with_that_constant(monkeypatch):
    """The other half of 'they cannot drift': the number the executor derives from is the one the
    proxy's client really uses (not a second `120.0` written at the client)."""
    built: list[dict] = []

    def recording_client(**kwargs):
        built.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(httpx, "AsyncClient", recording_client)

    create_app(TokenRegistry(), real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET)

    (kwargs,) = built
    assert kwargs["timeout"] == 120.0
    assert kwargs["timeout"] == llm_proxy.UPSTREAM_TIMEOUT_SECONDS


def test_a_drain_that_is_not_idle_at_first_is_asked_again_until_it_is(monkeypatch, make_build_with_request):
    """a call held longer than one step is waited for by asking again; the usage is read after
    the step that said idle, and not before."""
    proxy = _FakeProxy(drain=[False, False, True])

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy)

    assert result.ok is True and reached_publish == [True]
    assert [call[0] for call in proxy.calls] == ["drain", "drain", "drain", "usage", "revoke"]
    assert proxy.drain_waits == [30.0, 30.0, 30.0]
    assert build.cost_tokens == 7


def test_the_steps_never_exceed_the_step_add_up_to_the_total_and_then_stop(monkeypatch, make_build_with_request):
    """A proxy that never goes idle: five steps of 30 s at the defaults (the five add up to the 150 s
    total), and no sixth. The fake raises past 40, so a loop that lost its exit ends the test red."""
    proxy = _FakeProxy(drain=False)

    result, _, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert proxy.drain_waits == [30.0] * 5
    assert sum(proxy.drain_waits) == sbe.PROXY_DRAIN_TOTAL_SECONDS
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert result.summary == COST_NOTE + RAN_OUT


def test_a_total_that_is_not_a_multiple_of_the_step_ends_with_a_shorter_last_step(monkeypatch, make_build_with_request):
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 0.4)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 1.0)
    proxy = _FakeProxy(drain=False)

    result, _, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert len(proxy.drain_waits) == 3
    assert all(0 < wait <= 0.4 for wait in proxy.drain_waits)
    assert sum(proxy.drain_waits) == pytest.approx(1.0)
    assert "after waiting 1 s" in result.summary


def test_a_step_that_raises_after_an_unfinished_one_ends_the_wait_and_fails_closed(monkeypatch, make_build_with_request):
    proxy = _FakeProxy(drain=[False, RuntimeError("the exec failed " + TOKEN)])

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy)

    assert len(proxy.drain_waits) == 2  # asked again after the False, and not a third time after the raise
    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert build.cost_tokens is None and build.cost_usd is None
    assert reached_publish == []
    assert TOKEN not in result.summary


@pytest.mark.parametrize("answer", [None, "true", 1, 0, "yes", [], {}], ids=repr)
def test_an_answer_that_is_neither_true_nor_false_ends_the_wait_at_once(monkeypatch, make_build_with_request, answer):
    """Not 'still alive', so not a reason to wait on: one step, then the failure. (Only the boolean
    `True` counts as drained.)"""
    proxy = _FakeProxy(drain=answer)

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy)

    assert len(proxy.drain_waits) == 1
    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert build.cost_tokens is None and build.cost_usd is None
    assert reached_publish == []


# --- what a drain that could not finish does to the build ---------------------------------------


def test_a_drain_that_ran_out_with_no_earlier_failure_is_infra_error_with_cost_unknown_and_no_figure_stored(
    monkeypatch, make_build_with_request
):
    """`drained: false` for the whole total means a call is still out, so a read now would be short.
    The build had not failed, so it is INFRA_ERROR with both cost fields NULL ('not measured'). The
    usage IS read, but only for the proxy's refusals and flags: its figures (7 tokens) are never
    stored."""
    proxy = _FakeProxy(drain=False, usage=_usage(tokens=7, cost_usd=0.0001))

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy)

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert result.summary == COST_NOTE + RAN_OUT
    assert build.cost_tokens is None and build.cost_usd is None
    assert reached_publish == []


@pytest.mark.parametrize(
    "reason,detail",
    [
        (BuildFailureReason.TIMEOUT, None),
        (BuildFailureReason.RESOURCE_LIMIT, "the sandbox's disk use passed the 2048 MiB limit"),
        (BuildFailureReason.AGENT_ERROR, None),
    ],
    ids=["timeout", "resource_limit", "agent_error"],
)
def test_a_drain_that_ran_out_keeps_the_reason_the_build_already_had_and_says_its_cost_is_unknown(
    monkeypatch, make_build_with_request, reason, detail
):
    """A build killed at its wall-clock limit with a call out is a
    TIMEOUT whose cost could not be read back, not an infrastructure fault; the same for a disk or
    harvest limit and for an agent error. The cost stays NULL, and the detail says so."""
    adapter = _OkAdapter(AgentAdapterResult(ok=False, reason=reason, detail=detail))
    proxy = _FakeProxy(drain=False, usage=_usage(tokens=7, cost_usd=0.0001))

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy, adapter)

    limit_note = f": {detail}" if detail else ""
    assert result.ok is False and result.reason == reason
    assert result.summary == (
        f"the agent run in the sandbox ended unsuccessfully ({reason}){limit_note}; "
        f"its output is in the worker log; {COST_NOTE}{RAN_OUT}"
    )
    assert build.cost_tokens is None and build.cost_usd is None  # the 7 tokens read for the flags are not stored
    assert reached_publish == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("drain failed (148): b'{\"detail\":\"Not Found\"}' " + TOKEN),
        TimeoutError("read timed out"),
        KeyError("drained"),
    ],
    ids=["a_proxy_image_without_the_route", "timeout", "key_error"],
)
@pytest.mark.parametrize("adapter_reason", [None, BuildFailureReason.TIMEOUT], ids=["adapter_ok", "timeout"])
def test_a_drain_that_cannot_be_asked_fails_closed_naming_only_the_exception_class(
    monkeypatch, make_build_with_request, error, adapter_reason
):
    """Whatever the reason the build kept (none: INFRA_ERROR, or TIMEOUT), the exec's raw text stays
    in the worker log and the detail names the class."""
    adapter = _OkAdapter(AgentAdapterResult(ok=False, reason=adapter_reason)) if adapter_reason else _OkAdapter()
    proxy = _FakeProxy(drain=error)

    result, build, reached_publish = _run(monkeypatch, make_build_with_request, proxy, adapter)

    expected_reason = adapter_reason or BuildFailureReason.INFRA_ERROR
    assert result.ok is False and result.reason == expected_reason
    assert result.summary.endswith(
        COST_NOTE + f"the proxy could not be drained before the usage read ({type(error).__name__}); see the worker log"
    )
    assert TOKEN not in result.summary and "Not Found" not in result.summary  # the raw text stays in the log
    assert build.cost_tokens is None and build.cost_usd is None
    assert len(proxy.drain_waits) == 1  # a failed exec is not retried
    assert reached_publish == []


def test_a_recorded_refusal_is_kept_as_the_reason_when_the_drain_ran_out(monkeypatch, make_build_with_request):
    """The refusals only grow, so one the proxy has recorded is real even with a call still alive. The
    build is AGENT_ERROR with the refusal's sentence, and its cost is unknown."""
    proxy = _FakeProxy(drain=False, usage=_usage(tokens=7, policy_refusals=[MCP]))

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.summary == f"1 request was refused by tool policy: {MCP.removeprefix('tool policy: ')}; {COST_NOTE}{RAN_OUT}"
    assert build.cost_tokens is None and build.cost_usd is None


def test_an_unmetered_response_is_kept_as_the_reason_when_the_drain_ran_out(monkeypatch, make_build_with_request):
    proxy = _FakeProxy(drain=False, usage=_usage(unmetered=True, unmetered_reason="SSE stream has no message_start usage"))

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert result.summary == (
        "the LLM proxy could not meter a response, so the build budget could not be enforced: "
        f"SSE stream has no message_start usage; {COST_NOTE}{RAN_OUT}"
    )
    assert build.cost_tokens is None and build.cost_usd is None


def test_a_spent_budget_is_kept_as_the_reason_without_quoting_a_figure_that_may_be_short(
    monkeypatch, make_build_with_request
):
    proxy = _FakeProxy(drain=False, usage=_usage(tokens=123456, cost_usd=98.7654, budget_exceeded=True))

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert result.summary.startswith("the per-build LLM budget was spent (limits of ")
    assert "123456" not in result.summary and "98.7654" not in result.summary  # the figures are not trusted
    assert result.summary.endswith(COST_NOTE + RAN_OUT)
    assert build.cost_tokens is None and build.cost_usd is None


def test_a_refusal_outranks_the_adapters_failure_after_a_failed_drain_as_it_does_after_a_good_one(
    monkeypatch, make_build_with_request
):
    adapter = _OkAdapter(AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT))
    proxy = _FakeProxy(drain=False, usage=_usage(policy_refusals=[MCP]))

    result, _, _ = _run(monkeypatch, make_build_with_request, proxy, adapter)

    assert result.reason == BuildFailureReason.AGENT_ERROR


@pytest.mark.parametrize(
    "usage",
    [
        RuntimeError("usage lookup failed " + TOKEN),
        ["not", "an", "object"],
        {"tokens": 0, "cost_usd": 0.0, "budget_exceeded": True, "policy_refusals": [MCP]},  # an older shape
        _usage(unmetered="no"),
    ],
    ids=["usage_unreadable", "not_an_object", "stale_shape", "unmetered_not_a_boolean"],
)
@pytest.mark.parametrize("adapter_failed", [True, False], ids=["adapter_timeout", "adapter_ok"])
def test_a_record_that_cannot_be_trusted_after_a_failed_drain_is_ignored_and_the_adapters_reason_stands(
    monkeypatch, make_build_with_request, usage, adapter_failed
):
    """The salvage read is held to the same shape check as the normal one: a payload the executor does
    not trust contributes no refusal and no budget state. With the adapter failed the reason is the
    adapter's; with nothing else failed it is INFRA_ERROR."""
    adapter = _OkAdapter(AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT)) if adapter_failed else None
    proxy = _FakeProxy(drain=False, usage=usage)

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy, adapter)

    if adapter_failed:
        assert result.reason == BuildFailureReason.TIMEOUT
        assert result.summary.endswith(COST_NOTE + RAN_OUT)
    else:
        assert result.reason == BuildFailureReason.INFRA_ERROR
        assert result.summary == COST_NOTE + RAN_OUT
    assert TOKEN not in result.summary
    assert build.cost_tokens is None and build.cost_usd is None


def test_an_adapter_exception_stays_the_reason_after_a_failed_drain_and_does_not_claim_the_usage_was_recorded(
    monkeypatch, make_build_with_request
):
    proxy = _FakeProxy(drain=False)
    adapter = _OkAdapter(raises=RuntimeError("sandbox runner blew up"))

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy, adapter)

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert result.summary == (
        f"the agent adapter raised RuntimeError after the sandbox run; see the worker log; {COST_NOTE}{RAN_OUT}"
    )
    assert "was recorded" not in result.summary  # after a good drain it says so; here it was not read
    assert "blew up" not in result.summary
    assert build.cost_tokens is None and build.cost_usd is None


def test_the_cost_note_survives_the_detail_bound_when_the_failure_text_is_long(monkeypatch, make_build_with_request):
    """The detail is cut at 1000 characters, from the end. Twenty distinct refusals make a long first
    part; it gives way to the note, never the other way round."""
    refusals = [f"tool policy: request field 'field_number_{index:02d}_{'x' * 60}' is not allowed" for index in range(20)]
    proxy = _FakeProxy(drain=False, usage=_usage(policy_refusals=refusals))

    result, _, _ = _run(monkeypatch, make_build_with_request, proxy)

    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert len(result.summary) <= 1000
    assert result.summary.endswith(COST_NOTE + RAN_OUT)
    assert "…[truncated]; " in result.summary  # the cut is visible


def test_a_usage_read_that_fails_after_a_good_drain_still_outranks_the_adapters_failure(
    monkeypatch, make_build_with_request
):
    """Only a failed DRAIN keeps the earlier reason. The proxy that could not be read at all after it
    drained is an infrastructure fault, unchanged (`test_sandbox_build_executor_usage.py`)."""
    adapter = _OkAdapter(AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT))
    proxy = _FakeProxy(drain=True, usage=RuntimeError("usage lookup failed"))

    result, build, _ = _run(monkeypatch, make_build_with_request, proxy, adapter)

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert build.cost_tokens is None and build.cost_usd is None


def test_the_log_names_the_failure_and_never_the_token(monkeypatch, make_build_with_request, caplog):
    proxy = _FakeProxy(drain=RuntimeError("drain failed: " + TOKEN))
    executor, _ = _executor_with_proxy(monkeypatch, proxy, _OkAdapter())
    build = make_build_with_request()

    with caplog.at_level("WARNING", logger="app.sandbox_build_executor"):
        executor.execute(build)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "could not be drained" in text
    assert TOKEN not in text


# =============================================================================================
# 2. The real proxy apps, two event loops, a call still out when the sandbox is gone
# =============================================================================================


class _Loop:
    """An event loop on a thread of its own. The proxy container runs two uvicorn listeners, so the
    sandbox-facing app and the admin app are on different loops over one registry; the drain must
    work across that, touching no loop-bound state."""

    def __init__(self, name: str):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro, timeout: float):
        return self.submit(coro).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.loop.close()


class _HoldingUpstream:
    """A stub provider that answers every call after holding it until `release` is set or `max_hold`
    seconds have passed, whichever is first: the hold is bounded whether or not the test releases it."""

    def __init__(self, reply: bytes, *, max_hold: float):
        self.reply = reply
        self.max_hold = max_hold
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def anything(path: str, request: Request) -> Response:
            await request.body()
            self.calls += 1
            self.started.set()
            clock = asyncio.get_running_loop().time
            deadline = clock() + self.max_hold
            while not self.release.is_set() and clock() < deadline:
                await asyncio.sleep(0.02)
            return Response(content=self.reply, media_type="application/json")

        return app


class _RealProxy:
    """What `SandboxBuildExecutor` needs from a `ProxyContainer`, served by the real proxy code: the
    sandbox-facing app on one loop thread, the control app on another, one registry, one upstream."""

    base_url_for_sandbox = "http://proxy"

    def __init__(self, upstream: _HoldingUpstream):
        self.registry = TokenRegistry()
        self.proxy_loop = _Loop("caseaj-proxy")
        self.control_loop = _Loop("caseaj-control")
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream.app()), base_url="http://upstream.test"
        )

        def app(serve: str) -> FastAPI:
            return create_app(
                self.registry,
                real_api_key=REAL_KEY,
                admin_secret=ADMIN_SECRET,
                http_client=client,
                dialect=ANTHROPIC.dialect,
                serve=serve,
            )

        self.sandbox_side = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app("proxy")), base_url="http://proxy.test"
        )
        self.control_side = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app("control")), base_url="http://control.test"
        )
        self.drains: list[float] = []

    def close(self):
        self.proxy_loop.stop()
        self.control_loop.stop()

    # --- what the adapter's sandbox does ---
    def send_call(self, token: str):
        """A request from the sandbox, on the proxy loop; returns its future."""
        return self.proxy_loop.submit(
            self.sandbox_side.post(ANTHROPIC.path, headers=ANTHROPIC.auth(token), json=ANTHROPIC.body())
        )

    # --- the ProxyContainer surface the executor uses ---
    def start(self, **kwargs):
        pass

    def issue_token(self, build_id, *, token_budget, cost_budget_usd):
        return self.registry.issue(build_id, token_budget=token_budget, cost_budget_usd=cost_budget_usd)

    def _control(self, method: str, path: str, timeout: float):
        response = self.control_loop.call(
            self.control_side.request(method, path, headers=ADMIN), timeout=timeout
        )
        response.raise_for_status()
        return response.json()

    def drain(self, token, *, timeout_seconds):
        self.drains.append(timeout_seconds)
        if len(self.drains) > MAX_FAKE_DRAINS:
            raise _Runaway(f"the executor asked for more than {MAX_FAKE_DRAINS} drain steps")
        answer = self._control(
            "POST", f"/_forge_admin/tokens/{token}/drain?timeout_seconds={timeout_seconds}", timeout_seconds + 10
        )
        return answer["drained"]

    def usage(self, token):
        return self._control("GET", f"/_forge_admin/tokens/{token}/usage", 10)

    def trace(self, token):
        return self._control("GET", f"/_forge_admin/tokens/{token}/trace", 10)

    def revoke(self, token):
        self.registry.revoke(token)

    def stop(self):
        pass


class _AdapterThatLeavesACallOut:
    """The sandbox 'dies' with a request in flight: it sends one on the proxy loop, waits until the
    upstream has it, and RETURNS without waiting for the answer. `then` runs next (optional).
    `result` is what it returns (a success by default; a TIMEOUT or RESOURCE_LIMIT for a build whose
    sandbox was killed)."""

    def __init__(self, proxy: _RealProxy, upstream: _HoldingUpstream, *, then=None, result=None):
        self.proxy = proxy
        self.upstream = upstream
        self.then = then
        self.result = result
        self.call = None
        self.token: str | None = None

    def run(self, adapter_input):
        self.token = adapter_input.proxy_token
        self.call = self.proxy.send_call(adapter_input.proxy_token)
        assert self.upstream.started.wait(10), "the call never reached the upstream"
        if self.then is not None:
            self.then(self)
        return with_output(self.result or AgentAdapterResult(ok=True, summary="done"))


@pytest.fixture()
def real_proxy():
    made: list[_RealProxy] = []

    def make(upstream: _HoldingUpstream) -> _RealProxy:
        proxy = _RealProxy(upstream)
        made.append(proxy)
        return proxy

    yield make
    for proxy in made:
        proxy.close()


def test_a_call_still_out_when_the_sandbox_is_gone_is_waited_for_and_its_spend_is_stored(
    monkeypatch, make_build_with_request, real_proxy
):
    """The adapter returns with a call in flight (the upstream holds it for 1.2 s). The executor's
    usage read waits for it: the stored figure is that call's 15 tokens, not the 0 an immediate
    read sees, and the build is not failed for it."""
    upstream = _HoldingUpstream(ANTHROPIC.reply(10, 5), max_hold=1.2)
    proxy = real_proxy(upstream)
    adapter = _AdapterThatLeavesACallOut(proxy, upstream)
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter)
    build = make_build_with_request()

    try:
        result = executor.execute(build)
    finally:
        upstream.release.set()
    call_response = adapter.call.result(10)

    assert result.ok is True and reached_publish == [True]
    assert call_response.status_code == 200  # the call was not cut short
    assert build.cost_tokens == 15  # the spend a read at the adapter's return would have missed
    assert build.cost_usd is not None and build.cost_usd > 0
    assert proxy.drains == [30.0]
    assert upstream.calls == 1


def test_a_call_held_longer_than_one_step_but_inside_the_total_is_drained_in_steps_and_its_spend_stored(
    monkeypatch, make_build_with_request, real_proxy
):
    """Against the real proxy apps on two loops. Steps of 0.3 s (total 6 s) against a call the
    upstream holds for 1.2 s: the first steps answer `drained: false` after their wait, a later one
    answers true, and the usage then holds the call's 15 tokens. Each wait asked of the proxy is at
    most one step."""
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 0.3)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 6.0)
    upstream = _HoldingUpstream(ANTHROPIC.reply(10, 5), max_hold=1.2)
    proxy = real_proxy(upstream)
    adapter = _AdapterThatLeavesACallOut(proxy, upstream)
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter)
    build = make_build_with_request()

    try:
        result = executor.execute(build)
    finally:
        upstream.release.set()
    call_response = adapter.call.result(10)

    assert result.ok is True and reached_publish == [True]
    assert call_response.status_code == 200
    assert build.cost_tokens == 15
    assert len(proxy.drains) >= 3  # more than one step was needed
    assert all(wait <= 0.3 for wait in proxy.drains)
    assert len(proxy.drains) < 20  # and it stopped asking once the proxy said idle


def test_a_call_that_outlasts_the_wait_fails_the_build_closed_and_stores_no_short_figure(
    monkeypatch, make_build_with_request, real_proxy
):
    """The same, with the whole wait cut to 0.4 s against a call held for 4 s: the proxy answers
    `drained: false`, and the build is INFRA_ERROR with the cost unknown (NULL), not a stored 0."""
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 0.4)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 0.4)
    upstream = _HoldingUpstream(ANTHROPIC.reply(10, 5), max_hold=4.0)
    proxy = real_proxy(upstream)
    adapter = _AdapterThatLeavesACallOut(proxy, upstream)
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter)
    build = make_build_with_request()

    try:
        result = executor.execute(build)
    finally:
        upstream.release.set()  # the held call finishes now; it is bounded by `max_hold` regardless
    adapter.call.result(10)

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert result.summary == COST_NOTE + (
        "a call was still in flight on the proxy after waiting 0.4 s, so its spend could not be included"
    )
    assert build.cost_tokens is None and build.cost_usd is None
    assert reached_publish == []


@pytest.mark.parametrize(
    "reason,detail",
    [
        (BuildFailureReason.TIMEOUT, None),
        (BuildFailureReason.RESOURCE_LIMIT, "the sandbox's disk use passed the 2048 MiB limit"),
    ],
    ids=["timeout", "resource_limit"],
)
def test_a_killed_build_whose_call_outlives_the_total_keeps_its_reason_with_the_cost_unknown(
    monkeypatch, make_build_with_request, real_proxy, reason, detail
):
    """End to end on the real proxy apps: the sandbox was killed (a wall-clock TIMEOUT, or a disk
    RESOURCE_LIMIT) with a call out, and the call outlives three 0.3 s steps (it is held for 4 s). The
    build is the TIMEOUT / RESOURCE_LIMIT it was, not an INFRA_ERROR, its cost is NULL, and the detail
    says so. The call finishes afterwards and its 15 tokens sit in the proxy, unread, which is the
    point: nothing short was stored."""
    monkeypatch.setattr(sbe, "PROXY_DRAIN_STEP_SECONDS", 0.3)
    monkeypatch.setattr(sbe, "PROXY_DRAIN_TOTAL_SECONDS", 0.9)
    upstream = _HoldingUpstream(ANTHROPIC.reply(10, 5), max_hold=4.0)
    proxy = real_proxy(upstream)
    adapter = _AdapterThatLeavesACallOut(
        proxy, upstream, result=AgentAdapterResult(ok=False, reason=reason, detail=detail)
    )
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter)
    build = make_build_with_request()
    build.cost_tokens, build.cost_usd = 41, 4.1

    try:
        result = executor.execute(build)
    finally:
        upstream.release.set()
    adapter.call.result(10)

    limit_note = f": {detail}" if detail else ""
    assert result.ok is False and result.reason == reason
    assert result.summary == (
        f"the agent run in the sandbox ended unsuccessfully ({reason}){limit_note}; "
        f"its output is in the worker log; {COST_NOTE}"
        "a call was still in flight on the proxy after waiting 0.9 s, so its spend could not be included"
    )
    assert build.cost_tokens is None and build.cost_usd is None
    assert proxy.drains == [pytest.approx(0.3)] * 3
    assert reached_publish == []


def test_a_cancelled_forwarded_call_makes_the_build_budget_exceeded(
    monkeypatch, make_build_with_request, real_proxy
):
    """End to end: the handler is cancelled after the forward began (the future of the
    sandbox's request is cancelled while the upstream holds the call). Nothing was recorded, so the
    token is unmetered and the build ends BUDGET_EXCEEDED ("could not meter"), the same as an
    unmeterable response. It does not read as a build that spent nothing."""
    upstream = _HoldingUpstream(ANTHROPIC.reply(10, 5), max_hold=5.0)
    proxy = real_proxy(upstream)

    def cancel_the_call(adapter):
        adapter.call.cancel()
        for _ in range(250):  # at most 5 s
            usage = proxy.registry.usage_for(adapter.token)
            if usage is not None and usage.unmetered:
                return
            time.sleep(0.02)
        raise AssertionError("the cancelled call never left the token unmetered")

    adapter = _AdapterThatLeavesACallOut(proxy, upstream, then=cancel_the_call)
    executor, reached_publish = _executor_with_proxy(monkeypatch, proxy, adapter)
    build = make_build_with_request()

    try:
        result = executor.execute(build)
    finally:
        upstream.release.set()

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert result.summary == (
        "the LLM proxy could not meter a response, so the build budget could not be enforced: " + CANCELLED_REASON
    )
    assert build.cost_tokens == 0 and build.cost_usd == 0.0  # what was recorded: nothing, and it is flagged
    assert reached_publish == []
