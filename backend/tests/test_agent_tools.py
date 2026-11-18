"""The declared agent tool set (ADR 0029 §1). No Docker.

The forbidden names below are the ones the pinned Claude Code CLI 2.1.285
actually uses: `test_agent_tool_capture_integration.py` records the CLI's own
default tool set against a fake upstream and asserts `WebSearch` and
`WebFetch` are in it, so these literals are not guesses and this test is not
vacuous. Literals here, never the production constant on the expected side.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.agent_tools import AGENT_TOOLS, declared_agent_tools

WEB_TOOLS = {"WebSearch", "WebFetch"}
MCP_PREFIX = "mcp__"


def test_declared_set_is_exactly_the_file_tools_plus_a_shell() -> None:
    assert sorted(AGENT_TOOLS) == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]


def test_declared_set_contains_no_web_tool() -> None:
    assert WEB_TOOLS.isdisjoint(AGENT_TOOLS)


def test_declared_set_contains_no_mcp_tool() -> None:
    assert [t for t in AGENT_TOOLS if t.startswith(MCP_PREFIX)] == []


def test_declared_set_keeps_the_shell_and_file_tools() -> None:
    """ADR 0029 §1: the shell stays -- executing code is what L3 is for."""
    assert {"Bash", "Read", "Write", "Edit"} <= set(AGENT_TOOLS)


def test_declared_set_has_no_duplicates_or_blanks() -> None:
    assert len(set(AGENT_TOOLS)) == len(AGENT_TOOLS)
    assert all(isinstance(t, str) and t.strip() for t in AGENT_TOOLS)


def test_declared_agent_tools_returns_a_fresh_json_list() -> None:
    first = declared_agent_tools()
    first.append("WebSearch")
    assert "WebSearch" not in declared_agent_tools()
    assert json.loads(json.dumps(declared_agent_tools())) == declared_agent_tools()


def test_the_executor_hands_the_declared_set_to_the_adapter(monkeypatch, make_build_with_request) -> None:
    """The set is wired from the one declaration, not restated at the call site.
    Driven through the executor's adapter seam (no Docker): the executor is
    stopped at the adapter, which records the input it was given."""
    from app.services import sandbox_build_executor as sbe

    seen: list = []

    class _StopHere(Exception):
        pass

    class _Adapter:
        def run(self, adapter_input):
            seen.append(adapter_input)
            raise _StopHere

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            pass

        def issue_token(self, *a, **k):
            return "tok"

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    class _Network:
        def remove(self):
            pass

    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter())
    monkeypatch.setattr(executor, "_stage_workspace", lambda *a, **k: None)
    build = make_build_with_request()

    try:
        executor.execute(build)
    except _StopHere:
        pass

    assert len(seen) == 1
    assert list(seen[0].tools) == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
