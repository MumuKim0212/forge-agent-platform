"""Host-side tests for the in-sandbox runner.

`fake_claude_agent_sdk.query()` (docker/agent-sandbox-stub) never inspects
`options`, so the existing sandbox-stub integration test
(test_sandbox_build_executor_integration.py) can only prove a build still
runs -- it cannot prove a prompt reached the SDK. These tests import
`app.agent_runner` directly, inject a fake `claude_agent_sdk` module into
`sys.modules`, and drive `_run`/`main` themselves. Importing the runner
from a test is fine even though `agent_runner.py` imports nothing from
`app/`: the standalone rule (see its module docstring) constrains what the
runner imports, not what imports it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from app import agent_runner


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class _FakeResultMessage:
    def __init__(self, is_error=False, total_cost_usd=0.0, usage=None):
        self.is_error = is_error
        self.total_cost_usd = total_cost_usd
        self.usage = usage or {}


class _FakeOptions:
    """Mirrors the real `ClaudeAgentOptions`/the stub's `**kwargs`-swallowing
    shape closely enough to record exactly what `_run` passed in."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


# A literal, not imported from app: the runner must forward whatever the host
# declared, so the test's value is deliberately not the production constant.
_TOOLS = ["Read", "Bash"]


def _install_fake_sdk(monkeypatch, captured_options: list) -> None:
    async def fake_query(*, prompt, options=None):
        captured_options.append(options)
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text=f"echo: {prompt}")])
        yield _FakeResultMessage(
            is_error=False, total_cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 2}
        )

    fake_module = types.ModuleType("claude_agent_sdk")
    fake_module.AssistantMessage = _FakeAssistantMessage
    fake_module.ClaudeAgentOptions = _FakeOptions
    fake_module.ResultMessage = _FakeResultMessage
    fake_module.TextBlock = _FakeTextBlock
    fake_module.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)


# ---------------------------------------------------------------------------
# (c1)/(c3): the prompt reaches ClaudeAgentOptions
# ---------------------------------------------------------------------------


def test_run_forwards_the_system_prompt_into_claude_agent_options(monkeypatch):
    captured: list = []
    _install_fake_sdk(monkeypatch, captured)

    asyncio.run(agent_runner._run("do the thing", None, "the forward-only rule text", _TOOLS))

    assert len(captured) == 1
    assert captured[0].kwargs["system_prompt"] == "the forward-only rule text"


def test_run_forwards_none_when_prompt_is_absent(monkeypatch):
    """(c4)'s companion at the `_run` level: an absent prompt still reaches
    `ClaudeAgentOptions`, explicitly as `None`, not silently dropped."""
    captured: list = []
    _install_fake_sdk(monkeypatch, captured)

    asyncio.run(agent_runner._run("do the thing", None, None, _TOOLS))

    assert len(captured) == 1
    assert captured[0].kwargs["system_prompt"] is None


# ---------------------------------------------------------------------------
# (c4): main() still runs when the key is absent, and warns on stderr
# ---------------------------------------------------------------------------


def test_main_runs_and_warns_on_stderr_when_prompt_is_absent(tmp_path, monkeypatch, capsys):
    task_input_path = tmp_path / "task_input.json"
    task_input_path.write_text(
        json.dumps({"intent_text": "do it", "max_turns": None, "tools": _TOOLS}), encoding="utf-8"
    )
    monkeypatch.setattr(agent_runner, "TASK_INPUT_PATH", str(task_input_path))

    written_results: list = []
    monkeypatch.setattr(agent_runner, "_write_result", written_results.append)

    captured_options: list = []
    _install_fake_sdk(monkeypatch, captured_options)

    exit_code = agent_runner.main()

    assert exit_code == 0
    assert written_results[0]["ok"] is True
    assert captured_options[0].kwargs["system_prompt"] is None
    err = capsys.readouterr().err
    assert "system_prompt" in err


def test_main_forwards_a_present_prompt_without_warning(tmp_path, monkeypatch, capsys):
    """Non-vacuity for the test above: a present prompt is forwarded
    verbatim and produces no stderr warning at all."""
    task_input_path = tmp_path / "task_input.json"
    task_input_path.write_text(
        json.dumps(
            {"intent_text": "do it", "max_turns": 3, "system_prompt": "the rule text", "tools": _TOOLS}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_runner, "TASK_INPUT_PATH", str(task_input_path))
    monkeypatch.setattr(agent_runner, "_write_result", lambda result: None)

    captured_options: list = []
    _install_fake_sdk(monkeypatch, captured_options)

    exit_code = agent_runner.main()

    assert exit_code == 0
    assert captured_options[0].kwargs["system_prompt"] == "the rule text"
    assert captured_options[0].kwargs["max_turns"] == 3
    err = capsys.readouterr().err
    assert "system_prompt" not in err


# ---------------------------------------------------------------------------
# ADR 0029: the tool set, config sources, MCP and permission
# mode reach the engine exactly as declared -- and a bad declaration fails closed
# ---------------------------------------------------------------------------


def _options_from_run(monkeypatch, tools) -> dict:
    captured: list = []
    _install_fake_sdk(monkeypatch, captured)
    asyncio.run(agent_runner._run("do it", None, "p", tools))
    assert len(captured) == 1
    return captured[0].kwargs


def test_run_forwards_the_declared_tool_set_verbatim_through_tools(monkeypatch):
    """(c1) The option that makes unlisted tools UNAVAILABLE is `tools`. The
    auto-approve option `allowed_tools` must not stand in for it: it restricts
    nothing (SDK types.py, `allowed_tools` docstring)."""
    declared = ["Read", "Edit", "Bash", "SomethingElse"]
    kwargs = _options_from_run(monkeypatch, declared)

    assert kwargs["tools"] == ["Read", "Edit", "Bash", "SomethingElse"]
    assert "allowed_tools" not in kwargs


def test_run_suppresses_every_configuration_source(monkeypatch):
    """(c2) `[]` is the SDK's isolation mode. Absent/None loads user, project
    and local settings -- and the engine-up capture proved a workspace
    `.mcp.json` then adds a tool the `tools` option does not remove."""
    kwargs = _options_from_run(monkeypatch, _TOOLS)

    assert kwargs["setting_sources"] == []
    assert kwargs["setting_sources"] is not None


def test_run_declares_no_mcp_servers_and_ignores_every_other_source(monkeypatch):
    """(c2) No `mcp_servers` (or an empty one), and `strict_mcp_config` so the
    engine ignores every MCP source the runner did not declare."""
    kwargs = _options_from_run(monkeypatch, _TOOLS)

    assert not kwargs.get("mcp_servers")
    assert kwargs["strict_mcp_config"] is True


def test_run_keeps_bypass_permissions(monkeypatch):
    """(c3) ADR 0029 §5: the mode stays; the tool set is what bounds the agent."""
    kwargs = _options_from_run(monkeypatch, _TOOLS)

    assert kwargs["permission_mode"] == "bypassPermissions"


def test_runner_source_cites_adr_0029_at_the_permission_mode():
    """(c3) The comment now says why the mode is safe, and cites 0029, not 0004."""
    lines = Path(agent_runner.__file__).read_text(encoding="utf-8").splitlines()
    idx = next(i for i, line in enumerate(lines) if 'permission_mode="bypassPermissions"' in line)
    assert "ADR 0029" in " ".join(lines[max(0, idx - 4) : idx + 1])


def _run_main_with_task_input(tmp_path, monkeypatch, task_input: dict):
    path = tmp_path / "task_input.json"
    path.write_text(json.dumps(task_input), encoding="utf-8")
    monkeypatch.setattr(agent_runner, "TASK_INPUT_PATH", str(path))
    written: list = []
    monkeypatch.setattr(agent_runner, "_write_result", written.append)
    captured: list = []
    _install_fake_sdk(monkeypatch, captured)
    return agent_runner.main(), written, captured


def test_main_fails_closed_when_the_tool_set_is_missing(tmp_path, monkeypatch, capsys):
    exit_code, written, captured = _run_main_with_task_input(
        tmp_path, monkeypatch, {"intent_text": "x", "system_prompt": "p", "max_turns": None}
    )

    assert exit_code == 1
    assert captured == []  # the engine was never started, so never with its default tool set
    assert written[0]["ok"] is False
    err = capsys.readouterr().err
    assert "has no 'tools' key" in err
    assert "malformed" not in err


def test_main_fails_closed_when_the_tool_set_is_null(tmp_path, monkeypatch, capsys):
    exit_code, written, captured = _run_main_with_task_input(
        tmp_path, monkeypatch, {"intent_text": "x", "system_prompt": "p", "tools": None}
    )

    assert exit_code == 1 and captured == []
    assert "has no 'tools' key" in capsys.readouterr().err


def test_main_fails_closed_when_the_tool_set_is_not_a_list(tmp_path, monkeypatch, capsys):
    exit_code, written, captured = _run_main_with_task_input(
        tmp_path, monkeypatch, {"intent_text": "x", "system_prompt": "p", "tools": "Read,Bash"}
    )

    assert exit_code == 1
    assert captured == []
    assert written[0]["ok"] is False
    err = capsys.readouterr().err
    assert "malformed" in err and "got str" in err
    assert "has no 'tools' key" not in err  # distinguishable from the missing case


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ([], "the list is empty"),
        (["Read", 7], "non-empty string"),
        (["Read", ""], "non-empty string"),
        ({"Read": True}, "got dict"),
    ],
)
def test_main_fails_closed_on_other_malformed_tool_sets(tmp_path, monkeypatch, capsys, bad, fragment):
    exit_code, _written, captured = _run_main_with_task_input(
        tmp_path, monkeypatch, {"intent_text": "x", "system_prompt": "p", "tools": bad}
    )

    assert exit_code == 1 and captured == []
    err = capsys.readouterr().err
    assert "malformed" in err and fragment in err


# ---------------------------------------------------------------------------
# (c2): agent_runner.py still imports nothing from app/ -- pinned mechanically
# ---------------------------------------------------------------------------


def test_agent_runner_imports_nothing_from_app():
    """Structural rule pinned the same way test_rbac.py pins its own
    invariants: walk the AST rather than trust the module docstring, so a
    future edit that adds `from app... import ...` fails a test instead of
    only being caught by review."""
    source = Path(agent_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    offenders.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "app" or node.module.startswith("app.")):
                offenders.append(f"{node.lineno}: from {node.module} import ...")

    assert offenders == []
