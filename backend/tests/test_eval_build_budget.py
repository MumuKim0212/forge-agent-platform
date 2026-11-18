"""the per-build cost budget is a per-build value.

`SandboxBuildExecutor` issues the proxy token with `Build.cost_budget_usd` when the build carries
one (an eval case's build: the smaller of the normal ceiling and what is left of the run budget),
and with `BUILD_COST_BUDGET_USD` when it does not (every normal build, unchanged). The token
BUDGET is the figure the proxy enforces before each call (ADR 0014), so this is where the run
budget becomes enforcement rather than bookkeeping.

Unit seam, no Docker: a fake `ProxyContainer` that records what `issue_token` was given, the same
shape as `test_sandbox_build_executor_usage.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output


def _executor(monkeypatch, *, usage):
    issued: list[dict] = []

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            pass

        def issue_token(self, build_id, **kwargs):
            issued.append(dict(build_id=build_id, **kwargs))
            return "tok-0123456789"

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
            return with_output(AgentAdapterResult(ok=True, summary="done"))

    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: SimpleNamespace(remove=lambda: None)))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor, "_publish", lambda *a, **k: sbe.ExecutionResult(ok=True, summary="published")
    )
    return executor, issued


USAGE = {
    "tokens": 10,
    "cost_usd": 0.001,
    "budget_exceeded": False,
    "policy_refusals": [],
    "unmetered": False,
    "unmetered_reason": None,
}


@pytest.fixture(autouse=True)
def ceilings(monkeypatch):
    monkeypatch.setattr(settings, "build_token_budget", 200_000)
    monkeypatch.setattr(settings, "build_cost_budget_usd", 5.0)


def test_a_normal_build_is_issued_a_token_with_the_setting(monkeypatch, make_build_with_request):
    executor, issued = _executor(monkeypatch, usage=USAGE)
    build = make_build_with_request()
    assert build.cost_budget_usd is None

    executor.execute(build)

    assert [(i["token_budget"], i["cost_budget_usd"]) for i in issued] == [(200_000, 5.0)]


def test_a_build_that_carries_a_budget_is_issued_a_token_with_that_budget_not_the_setting(
    monkeypatch, make_build_with_request
):
    executor, issued = _executor(monkeypatch, usage=USAGE)
    build = make_build_with_request()
    build.cost_budget_usd = 0.70

    executor.execute(build)

    assert [(i["token_budget"], i["cost_budget_usd"]) for i in issued] == [(200_000, 0.70)]


def test_a_carried_budget_of_zero_is_a_budget_not_a_missing_one(monkeypatch, make_build_with_request):
    """`0.0` is falsy: `or settings.build_cost_budget_usd` would turn a spent run into a 5-dollar build."""
    executor, issued = _executor(monkeypatch, usage=USAGE)
    build = make_build_with_request()
    build.cost_budget_usd = 0.0

    executor.execute(build)

    assert issued[0]["cost_budget_usd"] == 0.0


def test_the_budget_exceeded_message_names_the_budget_the_build_actually_had(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch, usage={**USAGE, "tokens": 900, "cost_usd": 0.7123, "budget_exceeded": True}
    )
    build = make_build_with_request()
    build.cost_budget_usd = 0.70

    result = executor.execute(build)

    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert result.summary == (
        "the per-build LLM budget was spent: 900 tokens and $0.7123 used against limits of "
        "200000 tokens and $0.70"
    )
