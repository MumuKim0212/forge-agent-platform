"""ADR 0014 §Amendment 2026-10-01: the LLM proxy is the ONLY source
of `builds.cost_tokens` / `cost_usd`. The engine's own `result.json` usage is
written inside L3 and is never recorded.

Unit seam, no Docker: `SandboxBuildExecutor.execute` is driven with a fake
`ProxyContainer` (whose `usage()` returns, or raises, what the real proxy's
admin endpoint does) and a fake adapter that self-reports usage. `_publish` is
replaced by a tripwire so a build that should have failed cannot quietly reach
publish. The proxy side of the contract (what it meters, when it marks a token
unmetered) is `test_llm_proxy.py`; the real container is
`test_proxy_container_integration.py` and the engine-up capture.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output

TOKEN = "tok-SECRET-0123456789"
MCP = "tool policy: request field 'mcp_servers' is not allowed"
UNMETERED_REASON = "SSE stream has no message_start usage"

# What a hostile or buggy engine could write into result.json.
ENGINE_TOKENS = 999
ENGINE_COST = 9.99


class _Network:
    def remove(self):
        pass


def _executor(monkeypatch, *, usage, adapter_result=None):
    """`usage` is the dict the fake proxy returns, or an Exception to raise."""
    reached_publish: list = []

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            pass

        def issue_token(self, *a, **k):
            return TOKEN

        def drain(self, token, *, timeout_seconds):
            return True  # nothing of the token is still running

        def usage(self, token):
            assert token == TOKEN
            if isinstance(usage, BaseException):
                raise usage
            return usage if not isinstance(usage, dict) else dict(usage)

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Adapter:
        def run(self, adapter_input):
            # a success carries the harvested directory the executor publishes from.
            return with_output(
                adapter_result
                or AgentAdapterResult(
                    ok=True,
                    summary="the agent thinks it worked",
                    usage_tokens=ENGINE_TOKENS,
                    usage_cost_usd=ENGINE_COST,
                )
            )

    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor,
        "_publish",
        lambda *a, **k: reached_publish.append(True) or sbe.ExecutionResult(ok=True, summary="published"),
    )
    return executor, reached_publish


def _usage(**over):
    base = {
        "tokens": 0,
        "cost_usd": 0.0,
        "budget_exceeded": False,
        "policy_refusals": [],
        "unmetered": False,
        "unmetered_reason": None,
    }
    base.update(over)
    return base


# --- (c1, c2) the proxy's figures are what is written -----------------------------


def test_the_proxy_s_figures_are_written_not_the_engine_s(monkeypatch, make_build_with_request):
    executor, reached_publish = _executor(monkeypatch, usage=_usage(tokens=7, cost_usd=0.0001))
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is True and reached_publish == [True]
    assert build.cost_tokens == 7
    assert build.cost_usd == pytest.approx(0.0001)
    assert build.cost_tokens != ENGINE_TOKENS and build.cost_usd != ENGINE_COST


def test_a_proxy_that_metered_nothing_writes_a_measured_zero_not_the_engine_s_figures(
    monkeypatch, make_build_with_request
):
    """The deleted fallback's exact trigger: proxy tokens == 0 while the engine
    reported more than 0. Zero is a MEASUREMENT here (the proxy was asked and
    answered), distinct from the NULL of 'not measured'."""
    executor, _ = _executor(monkeypatch, usage=_usage(tokens=0, cost_usd=0.0))
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is True
    assert build.cost_tokens == 0 and build.cost_tokens is not None
    assert build.cost_usd == 0.0 and build.cost_usd is not None


def test_the_engine_s_figures_are_logged_beside_the_proxy_s_on_one_labelled_line(
    monkeypatch, make_build_with_request, caplog
):
    executor, _ = _executor(monkeypatch, usage=_usage(tokens=7, cost_usd=0.0001))
    build = make_build_with_request()

    with caplog.at_level(logging.INFO, logger="app.sandbox_build_executor"):
        executor.execute(build)

    lines = [r.getMessage() for r in caplog.records if "engine-reported" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "proxy tokens=7 cost_usd=0.0001" in line
    assert f"engine-reported (diagnostic only, never recorded) tokens={ENGINE_TOKENS} cost_usd={ENGINE_COST}" in line
    assert TOKEN not in line


@pytest.mark.parametrize("failing_adapter", [False, True])
def test_a_failed_build_records_the_proxy_s_figures_too(monkeypatch, make_build_with_request, failing_adapter):
    adapter_result = (
        AgentAdapterResult(
            ok=False,
            reason=BuildFailureReason.AGENT_ERROR,
            usage_tokens=ENGINE_TOKENS,
            usage_cost_usd=ENGINE_COST,
        )
        if failing_adapter
        else None
    )
    executor, _ = _executor(monkeypatch, usage=_usage(tokens=7, cost_usd=0.0001), adapter_result=adapter_result)
    build = make_build_with_request()

    executor.execute(build)

    assert (build.cost_tokens, build.cost_usd) == (7, pytest.approx(0.0001))


# --- (c3) an unreadable proxy: NULL, and fail closed ------------------------------


def _assert_read_back_failure(result, build, reached_publish):
    assert result.ok is False
    assert result.reason == BuildFailureReason.INFRA_ERROR == "INFRA_ERROR"
    assert "could not be read back" in result.summary
    assert "cost is unknown" in result.summary
    assert build.cost_tokens is None  # not measured -- never 0
    assert build.cost_usd is None
    assert reached_publish == []
    assert TOKEN not in result.summary


def test_a_proxy_whose_usage_cannot_be_read_back_writes_null_and_fails_infra_error(
    monkeypatch, make_build_with_request
):
    executor, reached_publish = _executor(
        monkeypatch, usage=RuntimeError(f"usage lookup failed (1): token {TOKEN} timed out")
    )
    build = make_build_with_request()
    build.cost_tokens, build.cost_usd = 41, 4.1  # NULL must overwrite, not be left alone

    result = executor.execute(build)

    _assert_read_back_failure(result, build, reached_publish)
    # the exception's own text (here a docker-exec-shaped message) is
    # free-form infrastructure output -- it stays in the log; the failure
    # summary names the class only. (This used to assert the text, scrubbed.)
    assert "RuntimeError" in result.summary
    assert "timed out" not in result.summary and "usage lookup failed" not in result.summary


@pytest.mark.parametrize(
    "bad",
    [
        {"cost_usd": 0.0},  # no tokens
        {"tokens": 7},  # no cost
        {"tokens": None, "cost_usd": 0.0},
        {"tokens": "seven", "cost_usd": 0.0},
        {"tokens": 7, "cost_usd": "free"},
        ["tokens", 7],  # not an object
    ],
    ids=["no_tokens", "no_cost", "null_tokens", "text_tokens", "text_cost", "not_an_object"],
)
def test_a_usage_answer_without_readable_figures_is_a_read_back_failure_not_zero(
    monkeypatch, make_build_with_request, bad
):
    executor, reached_publish = _executor(monkeypatch, usage=bad)
    build = make_build_with_request()

    result = executor.execute(build)

    _assert_read_back_failure(result, build, reached_publish)


def test_the_read_back_failure_does_not_borrow_the_engine_s_figures(monkeypatch, make_build_with_request):
    """The old behaviour on an exception was 'treat as zeros, then fall back to
    the engine's numbers'."""
    executor, _ = _executor(monkeypatch, usage=ConnectionError("proxy gone"))
    build = make_build_with_request()

    executor.execute(build)

    assert build.cost_tokens is None and build.cost_usd is None


def test_a_read_back_failure_outranks_an_adapter_failure(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=ConnectionError("proxy gone"),
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT),
    )

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.INFRA_ERROR


# --- (c4) an unmetered success: BUDGET_EXCEEDED -----------------------------------


def test_an_unmetered_usage_fails_the_build_budget_exceeded_naming_the_reason(
    monkeypatch, make_build_with_request
):
    executor, reached_publish = _executor(
        monkeypatch,
        usage=_usage(tokens=7, cost_usd=0.0001, unmetered=True, unmetered_reason=UNMETERED_REASON),
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED == "BUDGET_EXCEEDED"
    assert "could not meter a response" in result.summary
    assert "budget could not be enforced" in result.summary
    assert UNMETERED_REASON in result.summary
    assert reached_publish == []
    assert (build.cost_tokens, build.cost_usd) == (7, pytest.approx(0.0001))  # what WAS metered is kept


def test_an_unmetered_usage_without_a_recorded_reason_still_fails_closed(monkeypatch, make_build_with_request):
    executor, _ = _executor(monkeypatch, usage=_usage(unmetered=True, unmetered_reason=None))

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert "reason not recorded" in result.summary


def test_a_policy_refusal_still_outranks_an_unmetered_usage(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(policy_refusals=[MCP], unmetered=True, unmetered_reason=UNMETERED_REASON),
    )

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert "refused by tool policy" in result.summary
    assert UNMETERED_REASON not in result.summary


def test_an_unmetered_usage_outranks_an_adapter_failure(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(unmetered=True, unmetered_reason=UNMETERED_REASON),
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR),
    )

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED


def test_a_plain_budget_miss_keeps_its_bare_budget_exceeded(monkeypatch, make_build_with_request):
    """Non-vacuity for the two above: `budget_exceeded` alone keeps its code, and
    its failure carries no unmetered message. It used to carry no summary
    at all; it now states the budget (a platform template: counts and limits)."""
    executor, _ = _executor(monkeypatch, usage=_usage(tokens=500, budget_exceeded=True))

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert "budget was spent" in result.summary and "500 tokens" in result.summary
    assert "meter" not in result.summary and "unmetered" not in result.summary


# --- (f1) a stale proxy image fails closed ----------------------------------------

# What a `forge-llm-proxy` built before ADR 0014 §Amendment 2026-10-01 returns:
# plausible figures (it records streamed replies as 0) and none of the new keys.
_STALE_PROXY_PAYLOAD = {"tokens": 0, "cost_usd": 0.0, "budget_exceeded": False, "policy_refusals": []}


def _assert_stale_proxy_failure(result, build, reached_publish):
    _assert_read_back_failure(result, build, reached_publish)
    assert "stale" in result.summary
    assert "predates ADR 0014 §Amendment 2026-10-01" in result.summary


def test_a_usage_payload_from_a_stale_proxy_without_the_unmetered_fields_fails_closed(
    monkeypatch, make_build_with_request
):
    """The old behaviour read a missing key as 'metered', so a hand-built stale
    image silently undid the whole amendment: 0 tokens, a budget that never trips,
    a published build."""
    executor, reached_publish = _executor(monkeypatch, usage=dict(_STALE_PROXY_PAYLOAD))
    build = make_build_with_request()
    build.cost_tokens, build.cost_usd = 41, 4.1

    result = executor.execute(build)

    _assert_stale_proxy_failure(result, build, reached_publish)
    assert "'unmetered'" in result.summary


@pytest.mark.parametrize("missing", ["unmetered", "unmetered_reason"])
def test_each_field_the_amendment_added_is_required_on_its_own(monkeypatch, make_build_with_request, missing):
    payload = _usage(tokens=7, cost_usd=0.0001)
    del payload[missing]
    executor, reached_publish = _executor(monkeypatch, usage=payload)
    build = make_build_with_request()

    result = executor.execute(build)

    _assert_stale_proxy_failure(result, build, reached_publish)
    assert f"'{missing}'" in result.summary


@pytest.mark.parametrize("bad", [None, "false", 0, 1, []], ids=["null", "text", "zero", "one", "list"])
def test_an_unmetered_flag_that_is_not_a_boolean_is_an_unrecognised_shape(monkeypatch, make_build_with_request, bad):
    executor, reached_publish = _executor(monkeypatch, usage=_usage(tokens=7, unmetered=bad))
    build = make_build_with_request()

    result = executor.execute(build)

    _assert_read_back_failure(result, build, reached_publish)
    assert "not a boolean" in result.summary


def test_a_complete_payload_still_passes_so_the_stale_check_is_not_vacuous(monkeypatch, make_build_with_request):
    executor, reached_publish = _executor(monkeypatch, usage=_usage(tokens=10, cost_usd=0.5))
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is True and reached_publish == [True]
    assert (build.cost_tokens, build.cost_usd) == (10, 0.5)
