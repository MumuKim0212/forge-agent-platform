"""ADR 0035 §Decision 1: the adapter wires the sandbox to the build's engine.

Against a fake `SandboxRunner`, no Docker. For `openai`: its own runner script is staged and
run, the sandbox gets `OPENAI_BASE_URL` (the proxy URL plus `/v1`, which is what the OpenAI
client expects) and `OPENAI_API_KEY` (the proxy TOKEN), the model rides `task_input.json`, and
no Anthropic variable appears. The `anthropic` path is pinned unchanged by `test_agent_adapter.py`.
"""

from __future__ import annotations

import json

import pytest

from app.services.agent_adapter import AgentAdapter, AgentAdapterInput
from app.services.agent_tools import declared_agent_tools
from app.services.sandbox_runner import SandboxRunResult
from tests.test_agent_adapter import FakeSandboxRunner, _write_harvested_result

REAL_KEY = "sk-proj-THE-REAL-KEY-the-sandbox-must-never-see-0123456789"


def _input(tmp_path, **overrides) -> AgentAdapterInput:
    defaults = dict(
        intent_text="build it",
        workspace_dir=str(tmp_path),
        proxy_base_url="http://10.0.0.5:8801",
        proxy_token="proxy-token-abc",
        system_prompt="the rules",
        tools=tuple(declared_agent_tools("openai")),
        engine="openai",
        model="gpt-5-mini",
    )
    defaults.update(overrides)
    return AgentAdapterInput(**defaults)


def _run(tmp_path, **overrides):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    AgentAdapter(sandbox_runner=fake).run(_input(tmp_path, **overrides))
    return fake.last_config


def test_the_openai_sandbox_runs_the_openai_runner_with_the_proxy_url_and_token(tmp_path):
    config = _run(tmp_path)

    assert config.command == ["python", "/workspace/.forge/agent_runner_openai.py"]
    assert config.env["OPENAI_BASE_URL"] == "http://10.0.0.5:8801/v1"
    assert config.env["OPENAI_API_KEY"] == "proxy-token-abc"
    assert config.env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_the_openai_sandbox_env_has_no_anthropic_variable_and_no_real_key(tmp_path):
    config = _run(tmp_path)

    assert not any("ANTHROPIC" in name for name in config.env)
    assert REAL_KEY not in str(config.env) and "sk-proj" not in str(config.env)
    assert sorted(config.env) == ["OPENAI_API_KEY", "OPENAI_BASE_URL", "PYTHONDONTWRITEBYTECODE"]


def test_the_anthropic_sandbox_env_has_no_openai_variable(tmp_path):
    config = _run(tmp_path, engine="anthropic", model=None, tools=tuple(declared_agent_tools("anthropic")))

    assert config.command == ["python", "/workspace/.forge/agent_runner.py"]
    assert config.env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:8801"  # no `/v1`: the Claude CLI appends its own path
    assert not any("OPENAI" in name for name in config.env)


def test_only_the_selected_engines_runner_is_staged(tmp_path):
    _run(tmp_path)

    forge = tmp_path / ".forge"
    assert (forge / "agent_runner_openai.py").exists()
    assert not (forge / "agent_runner.py").exists()
    staged = (forge / "agent_runner_openai.py").read_text(encoding="utf-8")
    assert "from agents import" in staged and "claude_agent_sdk" not in staged


def test_the_model_and_the_openai_tool_set_are_staged_in_the_task_input(tmp_path):
    _run(tmp_path, model="gpt-5-nano")

    written = json.loads((tmp_path / ".forge" / "task_input.json").read_text(encoding="utf-8"))
    assert set(written) == {"intent_text", "system_prompt", "tools", "max_turns", "model"}
    assert written["model"] == "gpt-5-nano"
    assert written["tools"] == ["read_file", "write_file", "edit_file", "glob", "grep", "shell"]


def test_a_build_with_no_configured_model_stages_no_model_key(tmp_path):
    """The Claude runner takes none; the key set stays the four it has always been."""
    _run(tmp_path, engine="anthropic", model=None, tools=("Read", "Bash"))

    written = json.loads((tmp_path / ".forge" / "task_input.json").read_text(encoding="utf-8"))
    assert set(written) == {"intent_text", "system_prompt", "tools", "max_turns"}


def test_an_engine_outside_the_closed_set_raises_before_anything_is_staged(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="no runner wiring"):
        AgentAdapter(sandbox_runner=fake).run(_input(tmp_path, engine="gemini"))

    assert fake.last_config is None
    assert not (tmp_path / ".forge").exists()


def test_the_engine_defaults_to_anthropic_so_existing_callers_are_unchanged(tmp_path):
    inp = AgentAdapterInput(
        intent_text="x",
        workspace_dir=str(tmp_path),
        proxy_base_url="http://p",
        proxy_token="t",
        system_prompt="p",
        tools=("Read",),
    )

    assert (inp.engine, inp.model) == ("anthropic", None)
