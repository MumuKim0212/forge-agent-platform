"""the executor's resource teardown, and what a failed proxy start leaves behind.

Unit seam, no Docker (the same one `test_sandbox_build_executor_usage.py` uses): `execute` is driven
with a fake `ProxyContainer` and a fake sandbox network that record every call, in order, in ONE log,
so a test can say what was torn down and in which sequence. The real engine -- a real proxy container
that exits right after it starts, real networks -- is in `test_sandbox_build_executor_integration.py`.

Two guarantees, each named for the way it used to fail:

1. **Any exception out of the proxy's start ends with the network destroyed**, and surfaces as
   ENGINE_UNREACHABLE. The start was caught for `ProxyStartError` alone, so a container that started
   and died (`exec_run` on it is a Docker 409; a missing network entry is a `KeyError`) escaped
   `execute` with the sandbox network still there.
2. **Each teardown step is guarded on its own.** The steps ran in a row, so a `revoke()` that raised
   something that is not a Docker error (a docker-py `ReadTimeout` is a `requests` error) skipped
   `proxy.stop()` -- the container holding the real key stayed up -- and the network and temp trees.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest
from docker.errors import APIError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.proxy_container import ProxyStartError
from app.services.status import BuildFailureReason
from tests.harvest_support import with_output

TOKEN = "tok-SECRET-0123456789"
START_FAILED = "the LLM proxy container failed to start; see the worker log"


class _Network:
    name = "forge-sandbox-net-test"

    def __init__(self, log, raises=None):
        self._log, self._raises = log, raises

    def remove(self):
        self._log.append("network.remove")
        if self._raises is not None:
            raise self._raises


def _executor(
    monkeypatch,
    *,
    start_raises=None,
    revoke_raises=None,
    stop_raises=None,
    network_raises=None,
    adapter_result=None,
):
    """(executor, log, temp_dirs). `log` is the ordered record of every proxy / network /
    adapter call; `temp_dirs` every directory `tempfile.mkdtemp` made while `execute` ran."""
    log: list[str] = []
    temp_dirs: list[str] = []

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            log.append("proxy.start")
            if start_raises is not None:
                raise start_raises

        def issue_token(self, *a, **k):
            log.append("proxy.issue_token")
            return TOKEN

        def drain(self, token, *, timeout_seconds):
            log.append("proxy.drain")
            return True

        def usage(self, token):
            log.append("proxy.usage")
            return {
                "tokens": 0,
                "cost_usd": 0.0,
                "budget_exceeded": False,
                "policy_refusals": [],
                "unmetered": False,
                "unmetered_reason": None,
            }

        def trace(self, token):
            log.append("proxy.trace")
            return {"steps": [], "calls_total": 0, "content_truncated": False}

        def revoke(self, token):
            log.append("proxy.revoke")
            if revoke_raises is not None:
                raise revoke_raises

        def stop(self):
            log.append("proxy.stop")
            if stop_raises is not None:
                raise stop_raises

    class _Adapter:
        def run(self, adapter_input):
            log.append("adapter.run")
            return with_output(adapter_result or AgentAdapterResult(ok=True, summary="done"))

    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        temp_dirs.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    docker = SimpleNamespace(
        networks=SimpleNamespace(create=lambda *a, **k: _Network(log, network_raises)),
    )
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        executor,
        "_publish",
        lambda *a, **k: log.append("publish") or sbe.ExecutionResult(ok=True, summary="published"),
    )
    return executor, log, temp_dirs


# --- 1. a proxy that fails to start, however it fails --------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        ProxyStartError("proxy container did not become healthy: b''"),
        APIError("409 Conflict: container is not running"),  # exec_run on a container that already exited
        KeyError("forge-sandbox-net-test"),  # the container never joined the sandbox network
        RequestsConnectionError("pipe closed"),
        ReadTimeout("the daemon did not answer"),
        RuntimeError("something unforeseen"),
    ],
    ids=lambda failure: type(failure).__name__,
)
def test_any_exception_out_of_the_proxy_start_destroys_the_network_and_is_engine_unreachable(
    monkeypatch, make_build_with_request, failure
):
    executor, log, _ = _executor(monkeypatch, start_raises=failure)

    result = executor.execute(make_build_with_request())

    assert result.ok is False
    assert result.reason == BuildFailureReason.ENGINE_UNREACHABLE  # not INFRA_ERROR, not an escaped exception
    assert result.summary == START_FAILED
    assert log == ["proxy.start", "proxy.stop", "network.remove"]  # no token, no agent, and the network is gone


def test_a_proxy_start_failure_never_reaches_the_agent_or_issues_a_token(monkeypatch, make_build_with_request):
    executor, log, _ = _executor(monkeypatch, start_raises=APIError("409"))

    executor.execute(make_build_with_request())

    assert "adapter.run" not in log and "proxy.issue_token" not in log


# --- 2. each teardown step on its own ---------------------------------------------------------------

_FULL_TEARDOWN = ["proxy.trace", "proxy.revoke", "proxy.stop", "network.remove"]


def test_a_complete_run_tears_down_in_order_trace_token_proxy_network(monkeypatch, make_build_with_request):
    executor, log, _ = _executor(monkeypatch)

    result = executor.execute(make_build_with_request())

    assert result.ok is True
    assert log[log.index("proxy.trace") :] == _FULL_TEARDOWN


def test_a_revoke_that_raises_a_non_docker_error_still_stops_the_proxy_and_removes_the_network_and_temp_trees(
    monkeypatch, make_build_with_request
):
    """The finding, exactly: `revoke()` raising a `requests` ReadTimeout used to end the teardown
    right there."""
    executor, log, temp_dirs = _executor(monkeypatch, revoke_raises=ReadTimeout("the daemon did not answer"))

    result = executor.execute(make_build_with_request())

    assert result.ok is True  # a teardown failure never turns a finished build into an exception
    assert log[log.index("proxy.revoke") :] == ["proxy.revoke", "proxy.stop", "network.remove"]
    assert temp_dirs, "the run made temp directories (staged workspace, harvested output, sanitised copy)"
    assert [d for d in temp_dirs if os.path.exists(d)] == []


@pytest.mark.parametrize(
    "broken_step",
    ["storing the build trace", "revoking the proxy token", "stopping the proxy container", "removing the sandbox network"],
)
def test_no_teardown_step_can_stop_the_ones_after_it(monkeypatch, make_build_with_request, caplog, broken_step):
    boom = RequestsConnectionError("pipe closed")
    kwargs = {
        "revoking the proxy token": {"revoke_raises": boom},
        "stopping the proxy container": {"stop_raises": boom},
        "removing the sandbox network": {"network_raises": boom},
    }.get(broken_step, {})
    executor, log, temp_dirs = _executor(monkeypatch, **kwargs)
    if broken_step == "storing the build trace":

        def exploding_store(*a, **k):
            raise boom

        monkeypatch.setattr(executor, "_store_trace", exploding_store)
        expected_after = ["proxy.revoke", "proxy.stop", "network.remove"]
    else:
        expected_after = _FULL_TEARDOWN[1:]

    result = executor.execute(make_build_with_request())

    assert result.ok is True
    assert log[log.index("proxy.revoke") :][: len(expected_after)] == expected_after
    assert [d for d in temp_dirs if os.path.exists(d)] == []
    # Said, so an operator can see what was left: the step's name where the step is wrapped here,
    # the network's own message where `_destroy_network` already catches and names it.
    said = "could not be removed" if broken_step == "removing the sandbox network" else broken_step
    assert any(said in record.getMessage() for record in caplog.records)


def test_a_teardown_failure_does_not_replace_the_builds_own_failure(monkeypatch, make_build_with_request):
    executor, log, _ = _executor(
        monkeypatch,
        revoke_raises=ReadTimeout("slow"),
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary="L3 text"),
    )

    result = executor.execute(make_build_with_request())

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert log[-2:] == ["proxy.stop", "network.remove"]


def test_the_teardown_log_line_never_carries_the_proxy_token(monkeypatch, make_build_with_request, caplog):
    """A failed `revoke` is logged by step and exception, and the
    text a `requests` error carries names an endpoint, not the token the call was about."""
    executor, _, _ = _executor(monkeypatch, revoke_raises=ReadTimeout("read timed out"))

    executor.execute(make_build_with_request())

    assert TOKEN not in caplog.text


# --- 3. a limit the sandbox runner enforced reaches the build's failure detail ---------------------------


def test_the_runners_limit_sentence_is_part_of_the_failure_detail(monkeypatch, make_build_with_request):
    executor, _, _ = _executor(
        monkeypatch,
        adapter_result=AgentAdapterResult(
            ok=False,
            reason=BuildFailureReason.RESOURCE_LIMIT,
            summary="L3-authored words that must not appear",
            logs="more L3 text",
            detail="the build's output is larger than the 2048 MiB limit",
        ),
    )

    result = executor.execute(make_build_with_request())

    assert result.reason == BuildFailureReason.RESOURCE_LIMIT
    assert result.summary == (
        "the agent run in the sandbox ended unsuccessfully (RESOURCE_LIMIT): "
        "the build's output is larger than the 2048 MiB limit; its output is in the worker log"
    )
    assert "L3-authored" not in result.summary and "more L3 text" not in result.summary


def test_a_failure_with_no_runner_sentence_keeps_its_existing_detail(monkeypatch, make_build_with_request):
    executor, _, _ = _executor(
        monkeypatch,
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary="L3 text"),
    )

    result = executor.execute(make_build_with_request())

    assert result.summary == (
        "the agent run in the sandbox ended unsuccessfully (AGENT_ERROR); its output is in the worker log"
    )


# --- 3. a build that runs a repair attempt tears down once, and leaks nothing of either ----------
#
# The loop makes more of everything per build (a workspace, a harvest, a sanitised copy, an image
# context per attempt) and still has ONE proxy, one token and one network. The harness is the repair
# tests' (`test_sandbox_build_executor_repair.py`); its event log also records the proxy's teardown.


def test_a_repaired_build_tears_the_proxy_and_the_network_down_once_in_the_usual_order(monkeypatch, make_build_with_request):
    from tests.test_sandbox_build_executor_repair import A0, A1, BROKEN, FIXED, RepairHarness, Run

    build = make_build_with_request()
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED])

    result = h.run(build)

    assert result.ok is True and h.adapter_runs() == 2
    assert h.events[h.events.index("trace") :] == ["trace", "revoke", "stop", "network.remove"]
    assert h.events.count("revoke") == h.events.count("stop") == h.events.count("network.remove") == 1
    assert h.networks_created == 1  # the second attempt did not make a second sandbox network


def test_a_revoke_that_raises_after_a_repaired_build_still_stops_the_proxy_removes_the_network_and_every_tree(
    monkeypatch, make_build_with_request
):
    from tests.test_sandbox_build_executor_repair import A0, A1, BROKEN, FIXED, RepairHarness, Run

    build = make_build_with_request()
    h = RepairHarness(
        monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], revoke_raises=ReadTimeout("the daemon did not answer")
    )

    result = h.run(build)

    assert result.ok is True  # a teardown failure never turns a finished build into an exception
    assert h.events[h.events.index("revoke") :] == ["revoke", "stop", "network.remove"]
    assert h.leaked_trees(build) == []
    assert [i["workspace_dir"] for i in h.inputs if os.path.exists(i["workspace_dir"])] == []
    assert [d for d in h.output_dirs if os.path.exists(d)] == []


def test_a_build_that_fails_after_its_repair_attempt_removes_the_network_and_leaves_no_preview_or_image(
    monkeypatch, make_build_with_request
):
    from tests.test_sandbox_build_executor_repair import A0, A1, BROKEN, FIXED, RepairHarness, Run, usage

    build = make_build_with_request()
    refused = usage(20, 0.02, policy_refusals=["tool policy: tool type 'web_search_20250305' is not allowed"])
    h = RepairHarness(monkeypatch, [Run(A0), Run(A1)], smoke=[BROKEN, FIXED], proxy_usage=[usage(), refused])

    result = h.run(build)

    assert result.ok is False
    assert h.events[-4:] == ["trace", "revoke", "stop", "network.remove"]
    assert h.live_preview is None and h.tag(build) in h.removed_images
    assert h.leaked_trees(build) == []
