"""Host-side tests for the in-sandbox OpenAI runner (ADR 0035 §Decision 2).

Like `test_agent_runner.py` for the Claude runner: the OpenAI Agents SDK is replaced by a
fake `agents` / `openai` pair injected into `sys.modules`, which RECORDS what the runner
passed it, so each option the proxy or the ADR depends on is asserted at the seam it crosses:
`store=False`, `service_tier="default"`, the turn limit, the model, tracing off twice, the
proxy URL and token (never anything else) on the client, and exactly the declared tools.

The tool functions are plain functions and are tested directly against a throwaway
workspace. Symlink escapes need `os.symlink`, which an unprivileged Windows host cannot do:
those tests skip there and run on Linux/CI, and `test_agent_runner_openai_container.py` runs
the same escapes inside a real container so they are exercised on this machine too.

Expected values are literals; `IMPLEMENTATIONS`' names are asserted against a literal list
because they are a contract with the host's declared set (`services/agent_tools.py`).
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

from app import agent_runner_openai as runner

_TOOLS = ["read_file", "shell"]


def _can_symlink(tmp_path) -> bool:
    try:
        os.symlink(str(tmp_path), str(tmp_path / "probe-link"))
    except (OSError, NotImplementedError):
        return False
    return True


# --------------------------------------------------------------------------
# A fake SDK that records what the runner hands it
# --------------------------------------------------------------------------


class FakeSdk:
    def __init__(self, *, final_output="all done", input_tokens=7, output_tokens=5, raises=None):
        self.final_output = final_output
        self.usage = types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        self.raises = raises
        self.agents: list[dict] = []
        self.model_settings: list[dict] = []
        self.clients: list[dict] = []
        self.default_clients: list[tuple] = []
        self.tracing_disabled_calls: list[bool] = []
        self.tracing_env_when_disabled: list[str | None] = []
        self.runs: list[dict] = []
        self.tools: list[tuple[str, object]] = []

    def install(self, monkeypatch) -> None:
        sdk = self

        class Agent:
            def __init__(self, **kwargs):
                sdk.agents.append(kwargs)

        class ModelSettings:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                sdk.model_settings.append(kwargs)

        class Runner:
            @staticmethod
            async def run(agent, input, **kwargs):  # noqa: A002 - mirrors the SDK's signature
                sdk.runs.append({"input": input, **kwargs})
                if sdk.raises:
                    raise sdk.raises
                return types.SimpleNamespace(
                    final_output=sdk.final_output,
                    context_wrapper=types.SimpleNamespace(usage=sdk.usage),
                )

        def function_tool(fn, **kwargs):
            tool = ("tool", kwargs.get("name_override"), fn)
            sdk.tools.append((kwargs.get("name_override"), fn))
            return tool

        def set_default_openai_client(client, use_for_tracing=True):
            sdk.default_clients.append((client, use_for_tracing))

        def set_tracing_disabled(flag):
            sdk.tracing_disabled_calls.append(flag)
            sdk.tracing_env_when_disabled.append(os.environ.get("OPENAI_AGENTS_DISABLE_TRACING"))

        class AsyncOpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                sdk.clients.append(kwargs)

        agents = types.ModuleType("agents")
        agents.Agent, agents.ModelSettings, agents.Runner = Agent, ModelSettings, Runner
        agents.function_tool = function_tool
        agents.set_default_openai_client = set_default_openai_client
        agents.set_tracing_disabled = set_tracing_disabled
        openai = types.ModuleType("openai")
        openai.AsyncOpenAI = AsyncOpenAI
        monkeypatch.setitem(sys.modules, "agents", agents)
        monkeypatch.setitem(sys.modules, "openai", openai)


@pytest.fixture()
def sdk(monkeypatch):
    fake = FakeSdk()
    fake.install(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://10.9.8.7:8801/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "forge-proxy-token-xyz")
    monkeypatch.delenv("OPENAI_AGENTS_DISABLE_TRACING", raising=False)
    return fake


def _run(sdk_, *, intent="do it", max_turns=None, system_prompt="the rules", tools=None, model="gpt-5-mini"):
    return asyncio.run(runner._run(intent, max_turns, system_prompt, list(tools or _TOOLS), model))


# --------------------------------------------------------------------------
# What reaches the SDK
# --------------------------------------------------------------------------


def test_the_agent_is_given_the_model_the_prompt_and_the_intent(sdk):
    _run(sdk, intent="build a todo app", system_prompt="forward-only rule text", model="gpt-5-nano")

    agent = sdk.agents[0]
    assert agent["model"] == "gpt-5-nano"
    assert agent["instructions"] == "forward-only rule text"
    assert sdk.runs[0]["input"] == "build a todo app"


def test_store_is_false_and_service_tier_is_default(sdk):
    """The proxy refuses a request without both. The SDK has no `service_tier` field,
    so it rides `extra_args`; `store` is a real field."""
    _run(sdk)

    assert sdk.model_settings[0]["store"] is False
    assert sdk.model_settings[0]["extra_args"] == {"service_tier": "default"}
    # Stateless: the reasoning can only travel to the next turn encrypted.
    assert sdk.model_settings[0]["response_include"] == ["reasoning.encrypted_content"]
    assert sdk.agents[0]["model_settings"].kwargs["store"] is False


def test_the_turn_limit_is_the_platforms_and_not_the_sdks_default_of_ten(sdk):
    _run(sdk, max_turns=37)

    assert sdk.runs[0]["max_turns"] == 37


def test_no_turn_limit_is_passed_as_none_not_dropped(sdk):
    """Omitting it would apply the SDK's default of 10; `None` is the platform's "no limit of
    its own" (the proxy's budget is the bound), as for the Claude runner."""
    _run(sdk, max_turns=None)

    assert "max_turns" in sdk.runs[0] and sdk.runs[0]["max_turns"] is None


def test_tracing_is_disabled_in_code_and_by_environment_before_the_first_run(sdk):
    _run(sdk)

    assert sdk.tracing_disabled_calls == [True]
    assert os.environ["OPENAI_AGENTS_DISABLE_TRACING"] == "1"
    assert sdk.tracing_env_when_disabled == ["1"]  # the variable was set when the SDK was told, i.e. before any run


def test_the_client_points_at_the_proxy_with_the_proxy_token_and_is_not_used_for_tracing(sdk):
    _run(sdk)

    assert sdk.clients == [{"base_url": "http://10.9.8.7:8801/v1", "api_key": "forge-proxy-token-xyz"}]
    assert len(sdk.default_clients) == 1
    client, use_for_tracing = sdk.default_clients[0]
    assert client.kwargs == sdk.clients[0]  # the one the SDK was told to use is the one built from the proxy env
    assert use_for_tracing is False  # the proxy token is never a tracing credential


def test_only_the_declared_tools_are_registered_each_under_its_declared_name(sdk):
    _run(sdk, tools=["read_file", "grep", "shell"])

    assert [name for name, _ in sdk.tools] == ["read_file", "grep", "shell"]
    assert len(sdk.agents[0]["tools"]) == 3  # nothing beyond the registered function tools reaches the agent
    assert all(t[0] == "tool" for t in sdk.agents[0]["tools"])


def test_an_implemented_tool_that_is_not_declared_is_not_registered(sdk):
    _run(sdk, tools=["read_file"])

    assert [name for name, _ in sdk.tools] == ["read_file"]


def test_the_declared_names_are_the_ones_the_host_declares():
    """The contract with `services/agent_tools.py` OPENAI_AGENT_TOOLS, as a literal."""
    assert sorted(runner.IMPLEMENTATIONS) == ["edit_file", "glob", "grep", "read_file", "shell", "write_file"]


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


def test_the_summary_is_the_final_output_and_usage_is_diagnostic_only(sdk):
    sdk.final_output = "  Built the app and ran its tests.  "
    sdk.usage = types.SimpleNamespace(input_tokens=1200, output_tokens=345)

    result = _run(sdk)

    assert result == {
        "ok": True,
        "summary": "Built the app and ran its tests.",
        "usage": {"tokens": 1545, "cost_usd": 0.0},  # cost is the proxy's alone (ADR 0014 amendment)
    }


def test_an_empty_final_output_gets_the_fixed_placeholder(sdk):
    sdk.final_output = None

    assert _run(sdk)["summary"] == "agent produced no summary text"


# --------------------------------------------------------------------------
# main(): fails closed before the engine is imported
# --------------------------------------------------------------------------


def _main_with(tmp_path, monkeypatch, task_input: dict):
    path = tmp_path / "task_input.json"
    path.write_text(json.dumps(task_input), encoding="utf-8")
    monkeypatch.setattr(runner, "TASK_INPUT_PATH", str(path))
    written: list = []
    monkeypatch.setattr(runner, "_write_result", written.append)
    return runner.main(), written


def test_main_runs_and_writes_the_result_in_the_existing_shape(tmp_path, monkeypatch, sdk):
    code, written = _main_with(
        tmp_path,
        monkeypatch,
        {"intent_text": "x", "system_prompt": "p", "tools": _TOOLS, "max_turns": 9, "model": "gpt-5-mini"},
    )

    assert code == 0
    assert written == [{"ok": True, "summary": "all done", "usage": {"tokens": 12, "cost_usd": 0.0}}]
    assert sdk.runs[0]["max_turns"] == 9 and sdk.agents[0]["model"] == "gpt-5-mini"


@pytest.mark.parametrize(
    ("tools", "fragment"),
    [
        ("MISSING", "has no 'tools' key"),
        (None, "has no 'tools' key"),
        ("read_file,shell", "got str"),
        ([], "the list is empty"),
        (["read_file", 7], "non-empty string"),
        (["read_file", ""], "non-empty string"),
        ({"read_file": True}, "got dict"),
        (["read_file", "web_search"], "does not implement: web_search"),
    ],
)
def test_main_fails_closed_on_a_missing_malformed_or_unimplemented_tool_set(
    tmp_path, monkeypatch, capsys, sdk, tools, fragment
):
    task_input = {"intent_text": "x", "system_prompt": "p", "model": "gpt-5-mini"}
    if tools != "MISSING":
        task_input["tools"] = tools

    code, written = _main_with(tmp_path, monkeypatch, task_input)

    assert code == 1
    assert sdk.agents == [] and sdk.runs == []  # the engine was never started, so never with a default tool set
    assert written[0]["ok"] is False
    assert fragment in capsys.readouterr().err


@pytest.mark.parametrize("model", [None, "", "  ", 5])
def test_main_refuses_to_run_without_a_model_rather_than_pick_one(tmp_path, monkeypatch, capsys, sdk, model):
    task_input = {"intent_text": "x", "system_prompt": "p", "tools": _TOOLS}
    if model is not None:
        task_input["model"] = model

    code, written = _main_with(tmp_path, monkeypatch, task_input)

    assert code == 1 and sdk.runs == []
    assert "no 'model'" in capsys.readouterr().err
    assert written[0]["ok"] is False


def test_an_engine_exception_still_writes_a_failed_result(tmp_path, monkeypatch, sdk):
    sdk.raises = RuntimeError("Max turns (3) exceeded")

    code, written = _main_with(
        tmp_path, monkeypatch, {"intent_text": "x", "system_prompt": "p", "tools": _TOOLS, "model": "m"}
    )

    assert code == 1
    assert written[0]["ok"] is False and "Max turns (3) exceeded" in written[0]["summary"]


def test_a_missing_system_prompt_warns_on_stderr_and_still_runs(tmp_path, monkeypatch, capsys, sdk):
    code, _ = _main_with(tmp_path, monkeypatch, {"intent_text": "x", "tools": _TOOLS, "model": "m"})

    assert code == 0
    assert "system_prompt" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The file tools are confined to the workspace
# --------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hello')\nTODO = 1\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    monkeypatch.setattr(runner, "WORKSPACE", str(root))
    return root


def test_read_write_and_edit_work_inside_the_workspace(ws):
    assert "hello" in runner.read_file("src/app.py")
    assert runner.write_file("new/dir/file.txt", "abc").startswith("wrote 3 characters")
    assert (ws / "new" / "dir" / "file.txt").read_text() == "abc"
    assert runner.read_file(str(ws / "new" / "dir" / "file.txt")) == "abc"  # an absolute path inside is fine
    assert runner.edit_file("src/app.py", "TODO = 1", "TODO = 2").startswith("replaced 1")
    assert "TODO = 2" in (ws / "src" / "app.py").read_text()


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "src/../../../etc/passwd"])
def test_a_traversal_path_is_refused_for_every_file_tool(ws, path):
    for call in (
        lambda: runner.read_file(path),
        lambda: runner.write_file(path, "x"),
        lambda: runner.edit_file(path, "a", "b"),
        lambda: runner.grep_files("SECRET", path),
    ):
        with pytest.raises(runner.ToolRefused, match="outside the workspace"):
            call()
    assert (ws.parent / "outside.txt").read_text() == "OUTSIDE-SECRET\n"


def test_an_absolute_path_outside_the_workspace_is_refused(ws):
    outside = str(ws.parent / "outside.txt")
    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.read_file(outside)
    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.write_file(outside, "overwritten")
    assert (ws.parent / "outside.txt").read_text() == "OUTSIDE-SECRET\n"


def test_a_sibling_directory_sharing_the_workspace_prefix_is_not_inside_it(ws, tmp_path):
    sibling = tmp_path / "workspace-evil"
    sibling.mkdir()
    (sibling / "f.txt").write_text("x", encoding="utf-8")

    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.read_file(str(sibling / "f.txt"))


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b"])
def test_an_empty_or_nul_path_is_refused(ws, bad):
    with pytest.raises(runner.ToolRefused):
        runner.read_file(bad)


def test_a_symlink_to_a_file_outside_the_workspace_is_refused_for_read_write_and_edit(ws, tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("os.symlink is not permitted on this host (the container test covers it)")
    os.symlink(str(tmp_path / "outside.txt"), str(ws / "link.txt"))

    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.read_file("link.txt")
    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.write_file("link.txt", "overwritten")
    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.edit_file("link.txt", "OUTSIDE", "CHANGED")
    assert (tmp_path / "outside.txt").read_text() == "OUTSIDE-SECRET\n"


def test_a_symlinked_directory_out_of_the_workspace_is_refused_and_not_listed_or_searched(ws, tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("os.symlink is not permitted on this host (the container test covers it)")
    os.symlink(str(tmp_path), str(ws / "escape"))

    with pytest.raises(runner.ToolRefused, match="outside the workspace"):
        runner.read_file("escape/outside.txt")
    assert "outside.txt" not in runner.glob_files("**/*")
    assert "OUTSIDE-SECRET" not in runner.grep_files("SECRET")


def test_a_symlink_that_stays_inside_the_workspace_is_allowed(ws, tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("os.symlink is not permitted on this host")
    os.symlink(str(ws / "src" / "app.py"), str(ws / "alias.py"))

    assert "hello" in runner.read_file("alias.py")


def test_read_bounds_what_it_returns_and_honours_offset_and_limit(ws):
    (ws / "big.txt").write_text("a" * 150_000, encoding="utf-8")

    whole = runner.read_file("big.txt")
    assert len(whole) < 150_000 and "truncated: 50000 more characters" in whole
    assert runner.read_file("src/app.py", offset=6, limit=7) == "'hello'" + "\n...[truncated: 11 more characters]"


def test_edit_requires_exactly_one_match_unless_replace_all(ws):
    (ws / "dup.txt").write_text("x x x", encoding="utf-8")

    with pytest.raises(runner.ToolRefused, match="matches 3 times"):
        runner.edit_file("dup.txt", "x", "y")
    with pytest.raises(runner.ToolRefused, match="not found"):
        runner.edit_file("dup.txt", "z", "y")
    assert runner.edit_file("dup.txt", "x", "y", replace_all=True).startswith("replaced 3")
    assert (ws / "dup.txt").read_text() == "y y y"


def test_glob_matches_by_path_and_by_bare_name(ws):
    (ws / "README.md").write_text("r", encoding="utf-8")

    assert runner.glob_files("**/*.py").splitlines() == ["src/app.py"]
    assert runner.glob_files("*.py").splitlines() == ["src/app.py"]  # no "/": matches the name anywhere
    assert runner.glob_files("*.md").splitlines() == ["README.md"]
    assert runner.glob_files("*.rs") == "(no matches)"


def test_grep_returns_path_line_and_text_and_filters_by_glob(ws):
    (ws / "notes.txt").write_text("TODO: later\n", encoding="utf-8")

    assert runner.grep_files("TODO").splitlines() == ["notes.txt:1:TODO: later", "src/app.py:2:TODO = 1"]
    assert runner.grep_files("TODO", glob="*.py") == "src/app.py:2:TODO = 1"
    assert runner.grep_files("TODO", path="src") == "src/app.py:2:TODO = 1"
    with pytest.raises(runner.ToolRefused, match="invalid regular expression"):
        runner.grep_files("(")


# --------------------------------------------------------------------------
# The shell
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="bash in the sandbox image; covered in the container test")
def test_shell_runs_in_the_workspace_without_the_proxy_url_or_token(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "forge-proxy-token-xyz")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://10.9.8.7:8801/v1")

    out = runner.run_shell("pwd; echo key=[$OPENAI_API_KEY] url=[$OPENAI_BASE_URL]; exit 3")

    assert out.startswith("exit code 3\n")
    assert f"{ws}" in out
    assert "key=[] url=[]" in out


def test_shell_refuses_an_empty_command(ws):
    with pytest.raises(runner.ToolRefused):
        runner.run_shell("  ")


# --------------------------------------------------------------------------
# Standalone: nothing from app/ (it runs inside the sandbox, not the platform process)
# --------------------------------------------------------------------------


def test_the_openai_runner_imports_nothing_from_app():
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [f"{node.lineno}: import {a.name}" for a in node.names if a.name == "app" or a.name.startswith("app.")]
        elif isinstance(node, ast.ImportFrom) and node.module and (node.module == "app" or node.module.startswith("app.")):
            offenders.append(f"{node.lineno}: from {node.module} import ...")

    assert offenders == []
