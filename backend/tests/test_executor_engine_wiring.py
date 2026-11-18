"""the executor runs the build on the engine the worker recorded at claim.

Unit seam, no Docker: `SandboxBuildExecutor.execute` over a fake proxy and a fake adapter that
record what they were handed. The agent run reports failure, so nothing is published; what is
asserted is the WIRING -- which dialect and key the proxy was started with, which image, tool
set, engine and model the adapter received, and that the real key goes to the proxy and
nowhere else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.status import BuildFailureReason

ANTHROPIC_KEY = "sk-ant-REAL-anthropic-key-0001"
OPENAI_KEY = "sk-proj-REAL-openai-key-0002-abcdefghijklmnop"
ISSUED_TOKEN = "tok-issued-for-this-build"


class _Network:
    def remove(self):
        pass


class Wiring:
    def __init__(self, monkeypatch):
        self.proxy_starts: list[dict] = []
        self.adapter_inputs: list = []
        wiring = self

        class _Proxy:
            base_url_for_sandbox = "http://proxy.internal:8801"

            def __init__(self, *a, **k):
                pass

            def start(self, **kwargs):
                wiring.proxy_starts.append(kwargs)

            def issue_token(self, *a, **k):
                return ISSUED_TOKEN

            def drain(self, token, *, timeout_seconds):
                return True  # nothing of the token is still running

            def usage(self, token):
                return {
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "budget_exceeded": False,
                    "policy_refusals": [],
                    "unmetered": False,
                    "unmetered_reason": None,
                }

            def revoke(self, *a):
                pass

            def stop(self):
                pass

        class _Adapter:
            def run(self, adapter_input):
                wiring.adapter_inputs.append(adapter_input)
                return AgentAdapterResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary="stop here")

        docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
        monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
        self.executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter(), git_provider=object())
        monkeypatch.setattr(self.executor, "_stage_workspace", lambda build, workspace_dir: None)

    def run(self, build):
        result = self.executor.execute(build)
        assert result.reason == BuildFailureReason.AGENT_ERROR  # it got as far as the agent run
        return self.proxy_starts[-1], self.adapter_inputs[-1]


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", ANTHROPIC_KEY)
    monkeypatch.setattr(settings, "openai_api_key", OPENAI_KEY)
    monkeypatch.setattr(settings, "anthropic_upstream_base_url", "http://anthropic-stub.invalid")
    monkeypatch.setattr(settings, "openai_upstream_base_url", "http://openai-stub.invalid")
    monkeypatch.setattr(settings, "agent_sandbox_image", "claude-image:test")
    monkeypatch.setattr(settings, "openai_agent_sandbox_image", "openai-image:test")


def test_the_executor_declares_that_it_runs_an_engine():
    """How the worker knows to assign one at claim."""
    assert sbe.SandboxBuildExecutor.uses_engine is True


def test_an_openai_build_starts_the_openai_dialect_proxy_with_the_openai_key_and_upstream(
    monkeypatch, configured, make_build_with_request
):
    build = make_build_with_request()
    build.engine, build.model = "openai", "gpt-5-nano"
    h = Wiring(monkeypatch)

    start, _ = h.run(build)

    assert start["dialect"] == "openai"
    assert start["real_api_key"] == OPENAI_KEY
    assert start["upstream_base_url"] == "http://openai-stub.invalid"


def test_an_openai_build_hands_the_adapter_its_engine_image_tools_and_the_recorded_model(
    monkeypatch, configured, make_build_with_request
):
    build = make_build_with_request()
    build.engine, build.model = "openai", "gpt-5-nano"
    h = Wiring(monkeypatch)

    _, given = h.run(build)

    assert given.engine == "openai"
    assert given.model == "gpt-5-nano"  # the one recorded at claim is the one sent
    assert given.image == "openai-image:test"
    assert list(given.tools) == ["read_file", "write_file", "edit_file", "glob", "grep", "shell"]
    assert given.proxy_base_url == "http://proxy.internal:8801"


def test_an_anthropic_build_is_wired_exactly_as_before(monkeypatch, configured, make_build_with_request):
    build = make_build_with_request()
    build.engine = "anthropic"
    h = Wiring(monkeypatch)

    start, given = h.run(build)

    assert start["dialect"] == "anthropic" and start["real_api_key"] == ANTHROPIC_KEY
    assert start["upstream_base_url"] == "http://anthropic-stub.invalid"
    assert (given.engine, given.model, given.image) == ("anthropic", None, "claude-image:test")
    assert list(given.tools) == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


def test_a_build_with_no_recorded_engine_fails_closed_before_anything_is_provisioned(
    monkeypatch, configured, make_build_with_request
):
    """The engine is decided by the worker at claim. A build that
    reaches `execute` with none recorded is not run on a default engine; it fails with a
    platform-authored detail, and no proxy, network or token is started."""
    build = make_build_with_request(engine=None)  # it never went through a claim
    h = Wiring(monkeypatch)

    result = h.executor.execute(build)

    assert result.ok is False
    assert result.reason == "INFRA_ERROR"
    assert "no recorded agent engine" in result.summary
    assert h.proxy_starts == []


@pytest.mark.parametrize("engine", ["openai", "anthropic"])
def test_the_real_key_reaches_only_the_proxy_never_the_adapter_input(monkeypatch, configured, make_build_with_request, engine):
    """The sandbox env is built from the adapter input, so this is the boundary: the input carries
    the per-build proxy TOKEN, and neither real key appears anywhere in it."""
    build = make_build_with_request()
    build.engine = engine
    build.model = "gpt-5-mini" if engine == "openai" else None
    h = Wiring(monkeypatch)

    start, given = h.run(build)

    assert given.proxy_token == ISSUED_TOKEN
    assert start["real_api_key"] != given.proxy_token
    for key in (ANTHROPIC_KEY, OPENAI_KEY):
        assert key not in repr(given)


def test_an_engine_outside_the_closed_set_does_not_run_at_all(monkeypatch, configured, make_build_with_request):
    build = make_build_with_request()
    build.engine = "gemini"
    h = Wiring(monkeypatch)

    with pytest.raises(ValueError, match="unknown engine"):
        h.executor.execute(build)

    assert h.proxy_starts == [] and h.adapter_inputs == []
