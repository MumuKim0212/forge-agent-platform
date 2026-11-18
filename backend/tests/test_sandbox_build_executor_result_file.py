"""ADR 0014 §Amendment 2026-10-17: the engine's own result file never suppresses the
proxy's record.

`.forge/result.json` is written inside L3. Before this task a wrong-typed field in it (a
string where `usage.tokens` is read, a JSON list where an object is) raised out of the adapter,
out of `execute`, and past the step that reads the proxy's usage, refusals and budget outcome:
the build was recorded as an executor crash (`INFRA_ERROR`), with no cost and no budget reason,
however far over budget the agent had gone. These tests drive `SandboxBuildExecutor.execute`
through the REAL `AgentAdapter` (over a fake sandbox runner that hands back a harvested directory
holding whatever result file the test chose) and a fake proxy whose usage is the figure the real
one would report, and assert what the build is recorded as.

No Docker. The proxy side (what it meters and refuses) is `test_llm_proxy*.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapter
from app.services.sandbox_runner import SandboxRunResult
from app.services.status import BuildFailureReason

TOKEN = "tok-SECRET-0123456789"
POLICY = "tool policy: request field 'mcp_servers' is not allowed"
UNMETERED_REASON = "the response names model 'x', which has no price row"

BUDGET_USAGE = {
    "tokens": 1500,
    "cost_usd": 0.0123,
    "budget_exceeded": True,
    "policy_refusals": [],
    "unmetered": False,
    "unmetered_reason": None,
}


def _usage(**over) -> dict:
    return {**{**BUDGET_USAGE, "budget_exceeded": False, "tokens": 0, "cost_usd": 0.0}, **over}


class _Network:
    def remove(self):
        self.removed = True


class _Proxy:
    """Records that it was asked for usage and that teardown ran."""

    instances: list = []

    def __init__(self, *a, **k):
        self.usage_asked = False
        self.revoked = False
        self.stopped = False
        _Proxy.instances.append(self)

    base_url_for_sandbox = "http://proxy"
    usage_payload: dict = {}

    def start(self, **k):
        pass

    def issue_token(self, *a, **k):
        return TOKEN

    def drain(self, token, *, timeout_seconds):
        return True  # nothing of the token is still running

    def usage(self, token):
        assert token == TOKEN
        self.usage_asked = True
        return dict(self.usage_payload)

    def trace(self, token):  # the trace is best-effort; an unreadable one changes nothing
        raise RuntimeError("no trace in this test")

    def revoke(self, *a):
        self.revoked = True

    def stop(self):
        self.stopped = True


class _FakeSandbox:
    """Hands back a harvested directory holding `result_file` (bytes), as the sandbox runner would."""

    def __init__(self, result_file: bytes | None):
        self.result_file = result_file
        self.harvested: str | None = None

    def run(self, config):
        self.harvested = tempfile.mkdtemp(prefix="forge-harvest-test-")
        os.makedirs(os.path.join(self.harvested, ".forge"))
        if self.result_file is not None:
            with open(os.path.join(self.harvested, ".forge", "result.json"), "wb") as f:
                f.write(self.result_file)
        return SandboxRunResult(ok=True, harvested_workspace_dir=self.harvested, logs="the agent's log")


def _executor(monkeypatch, *, usage: dict, adapter):
    reached_publish: list = []
    _Proxy.instances = []
    _Proxy.usage_payload = usage
    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=adapter)
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor,
        "_publish",
        lambda *a, **k: reached_publish.append(True) or sbe.ExecutionResult(ok=True, summary="published"),
    )
    return executor, reached_publish


def _real_adapter(result_file: bytes | None):
    sandbox = _FakeSandbox(result_file)
    return AgentAdapter(sandbox_runner=sandbox), sandbox


# What a hostile or broken engine can leave in `.forge/result.json`. Every one of the first four
# used to raise out of the adapter (`AttributeError` / `ValueError` / `TypeError`).
GARBAGE = [
    pytest.param(b"[]", id="json_list"),
    pytest.param(b'{"ok": true, "summary": "x", "usage": {"tokens": "not-a-number"}}', id="tokens_a_string"),
    pytest.param(b'{"ok": true, "summary": "x", "usage": "lots"}', id="usage_a_string"),
    pytest.param(b'{"ok": true, "summary": "x", "usage": {"cost_usd": "NaN"}}', id="cost_a_string"),
    pytest.param(b"not json at all", id="not_json"),
    pytest.param(b"", id="empty"),
    pytest.param(None, id="no_file"),
]


@pytest.mark.parametrize("garbage", GARBAGE)
def test_a_blown_budget_reads_budget_exceeded_even_when_result_json_is_garbage(
    monkeypatch, make_build_with_request, garbage
):
    adapter, sandbox = _real_adapter(garbage)
    executor, reached_publish = _executor(monkeypatch, usage=BUDGET_USAGE, adapter=adapter)
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert "1500 tokens" in result.summary and reached_publish == []
    # the proxy's figures are what the build records, whatever the file said
    assert (build.cost_tokens, build.cost_usd) == (1500, pytest.approx(0.0123))
    assert not os.path.exists(sandbox.harvested)  # the executor removed the harvested directory


@pytest.mark.parametrize("garbage", GARBAGE)
def test_a_policy_refusal_is_recorded_even_when_result_json_is_garbage(monkeypatch, make_build_with_request, garbage):
    adapter, _ = _real_adapter(garbage)
    executor, reached_publish = _executor(
        monkeypatch, usage=_usage(tokens=7, cost_usd=0.0001, policy_refusals=[POLICY]), adapter=adapter
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
    assert result.summary == "1 request was refused by tool policy: request field 'mcp_servers' is not allowed"
    assert (build.cost_tokens, build.cost_usd) == (7, pytest.approx(0.0001))
    assert reached_publish == []


@pytest.mark.parametrize("garbage", GARBAGE)
def test_an_unmetered_response_reads_budget_exceeded_even_when_result_json_is_garbage(
    monkeypatch, make_build_with_request, garbage
):
    adapter, _ = _real_adapter(garbage)
    executor, _ = _executor(
        monkeypatch, usage=_usage(unmetered=True, unmetered_reason=UNMETERED_REASON), adapter=adapter
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert UNMETERED_REASON in result.summary
    assert (build.cost_tokens, build.cost_usd) == (0, 0.0)  # measured zero, not NULL: the proxy answered


@pytest.mark.parametrize("garbage", GARBAGE)
def test_a_garbage_result_file_with_a_clean_proxy_is_an_agent_failure_that_still_records_cost(
    monkeypatch, make_build_with_request, garbage
):
    adapter, _ = _real_adapter(garbage)
    executor, reached_publish = _executor(monkeypatch, usage=_usage(tokens=42, cost_usd=0.002), adapter=adapter)
    build = make_build_with_request()

    result = executor.execute(build)

    # `{"ok": true, ...}` with wrong-typed usage is still a success the agent claimed, which the
    # executor then publishes; every other file is "no usable result".
    claimed_success = garbage is not None and b'"ok": true' in garbage
    if claimed_success:
        assert result.ok is True and reached_publish == [True]
    else:
        assert result.ok is False and result.reason == BuildFailureReason.AGENT_ERROR
        assert reached_publish == []
    assert (build.cost_tokens, build.cost_usd) == (42, pytest.approx(0.002))


# --- the adapter raising for a reason of its own ------------------------------------------------


class _RaisingAdapter:
    def __init__(self, error: Exception):
        self.error = error

    def run(self, adapter_input):
        raise self.error


def test_an_adapter_that_raises_still_has_its_proxy_record_read_and_a_blown_budget_wins(
    monkeypatch, make_build_with_request
):
    executor, reached_publish = _executor(
        monkeypatch, usage=BUDGET_USAGE, adapter=_RaisingAdapter(RuntimeError("SECRET-TEXT-of-the-exception"))
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.BUDGET_EXCEEDED
    assert (build.cost_tokens, build.cost_usd) == (1500, pytest.approx(0.0123))
    proxy = _Proxy.instances[0]
    assert proxy.usage_asked and proxy.revoked and proxy.stopped  # and the teardown ran
    assert reached_publish == []


def test_an_adapter_that_raises_with_a_clean_proxy_fails_infra_error_naming_only_the_class(
    monkeypatch, make_build_with_request
):
    executor, reached_publish = _executor(
        monkeypatch,
        usage=_usage(tokens=42, cost_usd=0.002),
        adapter=_RaisingAdapter(RuntimeError("SECRET-TEXT-of-the-exception")),
    )
    build = make_build_with_request()

    result = executor.execute(build)  # does not raise

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert "RuntimeError" in result.summary and "see the worker log" in result.summary
    assert "SECRET-TEXT" not in result.summary and TOKEN not in result.summary
    assert (build.cost_tokens, build.cost_usd) == (42, pytest.approx(0.002))
    assert _Proxy.instances[0].usage_asked and reached_publish == []


def test_an_adapter_that_raises_beside_a_refusal_reports_the_refusal(monkeypatch, make_build_with_request):
    executor, _ = _executor(
        monkeypatch,
        usage=_usage(policy_refusals=[POLICY]),
        adapter=_RaisingAdapter(ValueError("whatever")),
    )
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.reason == BuildFailureReason.AGENT_ERROR and "refused by tool policy" in result.summary


def test_an_adapter_that_raises_when_the_proxy_cannot_be_read_back_is_still_infra_error(
    monkeypatch, make_build_with_request
):
    """The proxy's own record failing is a different failure and keeps its own detail: the cost
    is NULL (not measured), as for any build whose usage cannot be read."""
    executor, _ = _executor(monkeypatch, usage={"nonsense": True}, adapter=_RaisingAdapter(RuntimeError("x")))
    build = make_build_with_request()

    result = executor.execute(build)

    assert result.reason == BuildFailureReason.INFRA_ERROR
    assert "could not be read back" in result.summary
    assert build.cost_tokens is None and build.cost_usd is None


def test_the_executor_leaves_no_harvested_directory_behind_on_these_paths(monkeypatch, make_build_with_request):
    adapter, sandbox = _real_adapter(json.dumps([1, 2, 3]).encode())
    executor, _ = _executor(monkeypatch, usage=BUDGET_USAGE, adapter=adapter)

    executor.execute(make_build_with_request())

    assert sandbox.harvested is not None and not os.path.exists(sandbox.harvested)
