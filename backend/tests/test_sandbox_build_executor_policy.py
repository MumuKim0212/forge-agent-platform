"""ADR 0029 §2: a tool-policy refusal recorded by the LLM proxy
fails the build closed, `AGENT_ERROR`, with a message naming the condition.

Unit seam, no Docker: `SandboxBuildExecutor.execute` is driven with a fake
`ProxyContainer` (whose `usage()` returns what the real proxy's usage endpoint
returns, including `policy_refusals`) and a fake adapter. That is the seam
where the executor reads the refusal back; the proxy side of the same contract
is proven against the real container in `test_proxy_container_integration.py`
and by the engine-up capture. Neither path is faked end to end here: `_publish`
is replaced by a tripwire so a build that should have failed cannot quietly
reach publish.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output

WEB_SEARCH = "tool policy: tool type 'web_search_20250305' is not allowed"
MCP = "tool policy: request field 'mcp_servers' is not allowed"


class _Network:
    def remove(self):
        pass


def _executor(monkeypatch, *, usage: dict, adapter_result: AgentAdapterResult):
    reached_publish: list = []

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            pass

        def issue_token(self, *a, **k):
            return "tok"

        def drain(self, token, *, timeout_seconds):
            return True  # nothing of the token is still running

        def usage(self, token):
            return dict(usage)

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Adapter:
        def run(self, adapter_input):
            # a success carries the harvested directory the executor publishes from.
            return with_output(adapter_result)

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


def test_a_refused_request_fails_the_build_as_agent_error_and_names_the_condition(monkeypatch, make_build_with_request):
    executor, reached_publish = _executor(
        monkeypatch,
        usage=_usage(tokens=120, cost_usd=0.01, policy_refusals=[WEB_SEARCH]),
        adapter_result=AgentAdapterResult(ok=True, summary="the agent thinks it worked"),
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR == "AGENT_ERROR"
    assert "refused by tool policy" in result.summary
    assert "tool type 'web_search_20250305' is not allowed" in result.summary
    assert reached_publish == []  # the agent's own "ok" did not carry it to publish
    assert build.cost_tokens == 120  # spend is still recorded on a failed build


def test_the_message_names_each_distinct_condition_once_and_counts_requests(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(policy_refusals=[WEB_SEARCH, MCP, WEB_SEARCH]),
        adapter_result=AgentAdapterResult(ok=True, summary="s"),
    )

    result = executor.execute(make_build_with_request())

    assert result.summary == (
        "3 requests were refused by tool policy: "
        "tool type 'web_search_20250305' is not allowed; "
        "request field 'mcp_servers' is not allowed"
    )


def test_a_single_refusal_message_is_singular(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(policy_refusals=[MCP]),
        adapter_result=AgentAdapterResult(ok=True, summary="s"),
    )

    result = executor.execute(make_build_with_request())

    assert result.summary == "1 request was refused by tool policy: request field 'mcp_servers' is not allowed"


def test_a_refusal_outranks_an_exhausted_budget(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(tokens=999_999, budget_exceeded=True, policy_refusals=[MCP]),
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR),
    )

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert "refused by tool policy" in result.summary


def test_a_build_whose_only_traffic_was_refused_still_fails_with_zero_proxy_spend(
    monkeypatch, make_build_with_request
):
    """A build whose only traffic was refused has 0 proxy tokens -- the trigger
    of the engine-usage fallback that was deleted. The refusal still fails it, and
    the cost fields are the proxy's measured 0, not the engine's self-report."""
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(tokens=0, policy_refusals=[WEB_SEARCH]),
        adapter_result=AgentAdapterResult(ok=True, summary="s", usage_tokens=50, usage_cost_usd=0.01),
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False
    assert "refused by tool policy" in result.summary
    assert (build.cost_tokens, build.cost_usd) == (0, 0.0)


def test_no_refusal_leaves_the_build_on_its_normal_path(monkeypatch, make_build_with_request):
    """Non-vacuity: the tripwire is reachable, so its silence above means something."""
    executor, reached_publish = _executor(
        monkeypatch,
        usage=_usage(tokens=10),
        adapter_result=AgentAdapterResult(ok=True, summary="s"),
    )

    result = executor.execute(make_build_with_request())

    assert result.ok is True
    assert reached_publish == [True]


def test_an_older_proxy_without_the_field_is_not_a_refusal(monkeypatch, make_build_with_request):
    """A usage payload with no `policy_refusals` key (an image built before the
    key existed) reads as none recorded -- the test suite's other stubs rely on it."""
    executor, reached_publish = _executor(
        monkeypatch,
        usage={"tokens": 10, "cost_usd": 0.0, "budget_exceeded": False, "unmetered": False, "unmetered_reason": None},
        adapter_result=AgentAdapterResult(ok=True, summary="s"),
    )

    result = executor.execute(make_build_with_request())

    assert result.ok is True and reached_publish == [True]


@pytest.mark.parametrize("bad", [None, []])
def test_empty_or_null_refusals_are_not_a_refusal(monkeypatch, make_build_with_request, bad):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(tokens=10, policy_refusals=bad),
        adapter_result=AgentAdapterResult(ok=True, summary="s"),
    )

    assert executor.execute(make_build_with_request()).ok is True
