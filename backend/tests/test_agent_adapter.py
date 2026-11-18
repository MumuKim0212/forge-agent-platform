"""Unit tests for the thin agent adapter against a fake
`SandboxRunner` — no Docker, no real key. Covers: staging the runner script
and task input into the workspace, building the sandbox command/env
correctly (proxy base URL + proxy token, never a real key), and mapping
sandbox/harvested-result outcomes to the adapter contract
({status, summary, usage}).
"""

from __future__ import annotations

import json
import os

import pytest

from app.services.agent_adapter import AgentAdapter, AgentAdapterInput
from app.services.sandbox_runner import SandboxRunResult
from app.services.status import BuildFailureReason


class FakeSandboxRunner:
    """Records the config it was called with and returns a canned result."""

    def __init__(self, result: SandboxRunResult):
        self.result = result
        self.last_config = None

    def run(self, config):
        self.last_config = config
        return self.result


def _adapter_input(tmp_path, **overrides) -> AgentAdapterInput:
    defaults = dict(
        intent_text="build a hello world service",
        workspace_dir=str(tmp_path),
        proxy_base_url="http://10.0.0.5:8801",
        proxy_token="proxy-token-abc",
        system_prompt="do not drop any tables",
        tools=("Read", "Bash"),
    )
    defaults.update(overrides)
    return AgentAdapterInput(**defaults)


def _write_harvested_result(tmp_path, result: dict) -> str:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(parents=True, exist_ok=True)
    (forge_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return str(tmp_path)


def test_stages_task_input_and_runner_script_into_workspace(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)

    adapter.run(_adapter_input(tmp_path, intent_text="do the thing"))

    task_input_path = tmp_path / ".forge" / "task_input.json"
    runner_script_path = tmp_path / ".forge" / "agent_runner.py"
    assert task_input_path.exists()
    assert runner_script_path.exists()
    assert json.loads(task_input_path.read_text(encoding="utf-8"))["intent_text"] == "do the thing"
    assert "claude_agent_sdk" in runner_script_path.read_text(encoding="utf-8")


def test_task_input_carries_exactly_the_contract_key_set(tmp_path):
    """the contract is a literal four-key dict today
    (intent_text, system_prompt, tools, max_turns); this test pins the key
    set explicitly so a future field lands deliberately, not silently."""
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)

    adapter.run(_adapter_input(tmp_path))

    task_input_path = tmp_path / ".forge" / "task_input.json"
    written = json.loads(task_input_path.read_text(encoding="utf-8"))
    assert set(written.keys()) == {"intent_text", "system_prompt", "tools", "max_turns"}


def test_declared_tools_are_staged_as_a_list_unchanged(tmp_path):
    """whatever the caller declared reaches task_input.json as a
    JSON list, in order -- the adapter neither adds to it nor drops from it."""
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)

    adapter.run(_adapter_input(tmp_path, tools=("Grep", "Read", "Bash")))

    written = json.loads((tmp_path / ".forge" / "task_input.json").read_text(encoding="utf-8"))
    assert written["tools"] == ["Grep", "Read", "Bash"]


def test_tools_is_a_required_input():
    """a caller that forgets the tool set fails on the host, at
    construction -- there is no default that could reach the runner."""
    with pytest.raises(TypeError):
        AgentAdapterInput(
            intent_text="x",
            workspace_dir="/tmp/x",
            proxy_base_url="http://p",
            proxy_token="t",
            system_prompt="p",
        )


def test_system_prompt_is_always_written_and_non_empty(tmp_path):
    """`agent_adapter.py` always writes `system_prompt`, and
    for a normal build it is non-empty -- an empty prompt would forward
    into `ClaudeAgentOptions` indistinguishably from "not composed"."""
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)

    adapter.run(_adapter_input(tmp_path, system_prompt="additive columns only, no drop"))

    task_input_path = tmp_path / ".forge" / "task_input.json"
    written = json.loads(task_input_path.read_text(encoding="utf-8"))
    assert written["system_prompt"] == "additive columns only, no drop"
    assert written["system_prompt"] != ""


def test_sandbox_command_and_env_never_carry_a_real_key(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)

    adapter.run(
        _adapter_input(tmp_path, proxy_base_url="http://10.0.0.5:8801", proxy_token="tok-xyz")
    )

    config = fake.last_config
    assert config.command == ["python", "/workspace/.forge/agent_runner.py"]
    assert config.env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:8801"
    assert config.env["ANTHROPIC_API_KEY"] == "tok-xyz"
    assert config.env["PYTHONDONTWRITEBYTECODE"] == "1"  # ADR 0032 §Amendment 2026-10-05
    assert "sk-ant" not in str(config.env)  # never a real-looking key


def test_network_is_forwarded_to_sandbox_config(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(tmp_path)))
    _write_harvested_result(tmp_path, {"ok": True, "summary": "done", "usage": {}})
    adapter = AgentAdapter(sandbox_runner=fake)
    sentinel_network = object()

    adapter.run(_adapter_input(tmp_path, network=sentinel_network))

    assert fake.last_config.network is sentinel_network


def test_sandbox_level_failure_maps_through_reason_unchanged(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=False, reason=BuildFailureReason.TIMEOUT, logs="timed out"))
    adapter = AgentAdapter(sandbox_runner=fake)

    result = adapter.run(_adapter_input(tmp_path))

    assert result.ok is False
    assert result.reason == BuildFailureReason.TIMEOUT
    assert result.logs == "timed out"


def test_missing_harvest_dir_is_harvest_error(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=None))
    adapter = AgentAdapter(sandbox_runner=fake)

    result = adapter.run(_adapter_input(tmp_path))

    assert result.ok is False
    assert result.reason == BuildFailureReason.HARVEST_ERROR


def test_missing_result_file_is_agent_error(tmp_path):
    empty_dir = tmp_path / "harvested-empty"
    empty_dir.mkdir()
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(empty_dir)))
    adapter = AgentAdapter(sandbox_runner=fake)

    result = adapter.run(_adapter_input(tmp_path))

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR


def test_result_file_reporting_failure_is_agent_error_with_summary(tmp_path):
    harvested = tmp_path / "harvested"
    harvested.mkdir()
    _write_harvested_result(harvested, {"ok": False, "summary": "agent crashed", "usage": {"tokens": 42}})
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(harvested)))
    adapter = AgentAdapter(sandbox_runner=fake)

    result = adapter.run(_adapter_input(tmp_path))

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.summary == "agent crashed"
    assert result.usage_tokens == 42


def test_successful_result_carries_summary_and_usage(tmp_path):
    harvested = tmp_path / "harvested"
    harvested.mkdir()
    _write_harvested_result(
        harvested, {"ok": True, "summary": "created the service", "usage": {"tokens": 1234, "cost_usd": 0.05}}
    )
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(harvested)))
    adapter = AgentAdapter(sandbox_runner=fake)

    result = adapter.run(_adapter_input(tmp_path))

    assert result.ok is True
    assert result.reason is None
    assert result.summary == "created the service"
    assert result.usage_tokens == 1234
    assert result.usage_cost_usd == 0.05


# --- ADR 0032 §Decision 5: the harvested directory is returned ------------


def test_a_successful_run_returns_the_harvested_directory_as_output_dir(tmp_path):
    harvested = tmp_path / "harvested"
    harvested.mkdir()
    (harvested / "agent-wrote-this.txt").write_text("bytes", encoding="utf-8")
    _write_harvested_result(harvested, {"ok": True, "summary": "done", "usage": {}})
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(harvested)))

    result = AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path))

    assert result.output_dir == str(harvested)
    # The input directory is not the output: the two are different paths.
    assert result.output_dir != str(tmp_path)
    # The adapter did not copy, move or delete anything in it.
    assert (harvested / "agent-wrote-this.txt").read_text(encoding="utf-8") == "bytes"


@pytest.mark.parametrize(
    "reason",
    [BuildFailureReason.TIMEOUT, BuildFailureReason.RESOURCE_LIMIT, BuildFailureReason.AGENT_ERROR],
)
def test_a_sandbox_failure_that_produced_a_harvest_still_returns_the_directory(tmp_path, reason):
    """So the caller can remove it: a failed build's harvest is as real a
    temp directory as a successful one's."""
    harvested = tmp_path / "harvested"
    harvested.mkdir()
    fake = FakeSandboxRunner(SandboxRunResult(ok=False, reason=reason, harvested_workspace_dir=str(harvested)))

    result = AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path))

    assert result.ok is False
    assert result.reason == reason
    assert result.output_dir == str(harvested)


def test_the_directory_is_returned_when_the_result_file_is_missing_or_reports_failure(tmp_path):
    no_result = tmp_path / "no-result"
    no_result.mkdir()
    reported_failure = tmp_path / "reported-failure"
    reported_failure.mkdir()
    _write_harvested_result(reported_failure, {"ok": False, "summary": "crashed", "usage": {}})

    for harvested in (no_result, reported_failure):
        fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=str(harvested)))
        result = AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path))
        assert result.ok is False
        assert result.output_dir == str(harvested)


def test_no_harvest_means_no_output_dir(tmp_path):
    fake = FakeSandboxRunner(SandboxRunResult(ok=False, reason=BuildFailureReason.HARVEST_ERROR))
    assert AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path)).output_dir is None

    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=None))
    assert AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path)).output_dir is None


# --- `.forge/result.json` is L3-authored, so the adapter never raises over its contents ---
#
# This replaces `test_a_result_file_the_adapter_cannot_interpret_does_not_leak_the_directory`,
# which pinned the OPPOSITE on purpose: a wrong-typed `usage.tokens` raised `ValueError` out of
# `run()`, after the adapter had removed the harvested directory. Propagating was the defect, not
# a contract (ADR 0014 §Amendment 2026-10-17, "the engine's own result file can never suppress
# the proxy's record"): the exception travelled out of the executor past the step that reads the
# proxy's usage, refusals and budget outcome, so a build whose agent had blown its budget and
# written a garbage file was recorded as an executor crash, with no cost and no budget reason.
# Now the file is read by type; whatever it holds the adapter RETURNS a result, carrying the
# harvested directory for its caller to remove (the contract for every other failure).

NO_RESULT = "agent runner produced no usable result"


def _harvest_with(tmp_path, raw: bytes | str | None) -> str:
    harvested = tmp_path / "harvested"
    (harvested / ".forge").mkdir(parents=True)
    if raw is not None:
        (harvested / ".forge" / "result.json").write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    return str(harvested)


def _run_on(tmp_path, harvested: str):
    fake = FakeSandboxRunner(SandboxRunResult(ok=True, harvested_workspace_dir=harvested))
    return AgentAdapter(sandbox_runner=fake).run(_adapter_input(tmp_path))


@pytest.mark.parametrize(
    "garbage",
    [
        "[]",
        '[{"ok": true}]',
        '"ok"',
        "42",
        "null",
        "true",
        "",
        "   ",
        "{not json",
        '{"ok": true, "summary":',
        "NaN",
        "[" * 200_000,  # nested far past the parser's depth
        '{"ok": true, "usage": {"tokens": 1' + "0" * 5000 + "}}",  # an integer past the int-digit limit
    ],
    ids=[
        "list", "list_of_dict", "string", "number", "null", "bool", "empty", "blank", "truncated_object",
        "cut_off", "nan", "nested_too_deep", "huge_integer",
    ],
)
def test_a_result_file_that_is_not_a_json_object_is_no_usable_result_and_never_raises(tmp_path, garbage):
    harvested = _harvest_with(tmp_path, garbage)

    result = _run_on(tmp_path, harvested)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.summary == NO_RESULT
    assert result.output_dir == harvested and os.path.isdir(harvested)  # the caller removes it
    assert (result.usage_tokens, result.usage_cost_usd) == (0, 0.0)


def test_a_result_file_that_is_not_utf8_is_no_usable_result(tmp_path):
    harvested = _harvest_with(tmp_path, b'{"ok": true, "summary": "' + bytes([0xFF, 0xFE]) + b'"}')

    result = _run_on(tmp_path, harvested)

    assert (result.ok, result.reason, result.summary) == (False, BuildFailureReason.AGENT_ERROR, NO_RESULT)


def test_a_result_file_over_the_size_limit_is_not_read(tmp_path):
    """The limit is 1 MiB: a real file is a short summary and two numbers. One byte over is
    refused, one byte under is read (so the limit is the boundary and not a guess)."""
    from app.services.agent_adapter import RESULT_MAX_BYTES

    assert RESULT_MAX_BYTES == 1024 * 1024
    head = b'{"ok": true, "summary": "'
    tail = b'", "usage": {}}'
    pad = lambda total: b"x" * (total - len(head) - len(tail))  # noqa: E731

    over = _run_on(tmp_path, _harvest_with(tmp_path, head + pad(RESULT_MAX_BYTES + 1) + tail))
    assert (over.ok, over.summary) == (False, NO_RESULT)

    (tmp_path / "harvested" / ".forge" / "result.json").write_bytes(head + pad(RESULT_MAX_BYTES) + tail)
    exact = _run_on(tmp_path, str(tmp_path / "harvested"))
    assert exact.ok is True and len(exact.summary) == RESULT_MAX_BYTES - len(head) - len(tail)


def test_a_result_json_that_is_a_directory_is_no_usable_result(tmp_path):
    harvested = _harvest_with(tmp_path, None)
    os.mkdir(os.path.join(harvested, ".forge", "result.json"))

    result = _run_on(tmp_path, harvested)

    assert (result.ok, result.summary) == (False, NO_RESULT)


def test_a_result_json_that_is_a_symlink_is_not_followed(tmp_path):
    harvested = _harvest_with(tmp_path, None)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"ok": True, "summary": "from a file the agent pointed at"}), encoding="utf-8")
    try:
        os.symlink(target, os.path.join(harvested, ".forge", "result.json"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")

    result = _run_on(tmp_path, harvested)

    assert (result.ok, result.summary) == (False, NO_RESULT)


@pytest.mark.parametrize(
    ("payload", "ok", "summary", "tokens", "cost"),
    [
        # wrong types in every field, one at a time, on a success the agent claims
        ({"ok": True, "summary": ["a"], "usage": {}}, True, None, 0, 0.0),
        ({"ok": True, "summary": 7, "usage": {}}, True, None, 0, 0.0),
        ({"ok": True, "summary": "x", "usage": "lots"}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": [1, 2]}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": None}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": "not-a-number"}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": "12"}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": 1.5}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": True}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": -3}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"tokens": [1]}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"cost_usd": "9.99"}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"cost_usd": True}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"cost_usd": -1.0}}, True, "x", 0, 0.0),
        ({"ok": True, "summary": "x", "usage": {"cost_usd": {"a": 1}}}, True, "x", 0, 0.0),
        # a number that is valid JSON to Python but not a usable cost
        ('{"ok": true, "summary": "x", "usage": {"cost_usd": NaN}}', True, "x", 0, 0.0),
        ('{"ok": true, "summary": "x", "usage": {"cost_usd": Infinity}}', True, "x", 0, 0.0),
        ('{"ok": true, "summary": "x", "usage": {"cost_usd": 1e999}}', True, "x", 0, 0.0),
        ('{"ok": true, "summary": "x", "usage": {"tokens": 1' + "0" * 400 + "}}", True, "x", 10**400, 0.0),
        # `ok` is read by type: only the JSON `true` is a success
        ({"ok": "false", "summary": "x", "usage": {}}, False, "x", 0, 0.0),
        ({"ok": 1, "summary": "x", "usage": {}}, False, "x", 0, 0.0),
        ({"ok": [True], "summary": "x", "usage": {}}, False, "x", 0, 0.0),
        ({"summary": "x", "usage": {}}, False, "x", 0, 0.0),
        # well-formed numbers survive, as the diagnostic they are
        ({"ok": True, "summary": "x", "usage": {"tokens": 7, "cost_usd": 0.25}}, True, "x", 7, 0.25),
        ({"ok": False, "summary": "crashed", "usage": {"tokens": 7, "cost_usd": 2}}, False, "crashed", 7, 2.0),
    ],
)
def test_a_result_file_with_wrong_types_is_read_by_type_never_raised_on(tmp_path, payload, ok, summary, tokens, cost):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    harvested = _harvest_with(tmp_path, raw)

    result = _run_on(tmp_path, harvested)

    assert result.ok is ok
    assert result.reason == (None if ok else BuildFailureReason.AGENT_ERROR)
    assert result.summary == summary
    assert (result.usage_tokens, result.usage_cost_usd) == (tokens, cost)
    assert result.output_dir == harvested and os.path.isdir(harvested)


def test_an_exception_while_interpreting_the_result_is_a_failed_result_not_a_raise(tmp_path, monkeypatch):
    """The boundary, separate from the parser: if `_interpret_harvested_result` raised for any
    reason (a bug, a case no test thought of), `run()` still returns, carrying the directory.
    The rule is that nothing about L3's file may propagate, so it is enforced where the call is
    made and not only in the code that was written to be safe."""
    harvested = _harvest_with(tmp_path, json.dumps({"ok": True, "summary": "x", "usage": {}}))

    def explode(self, *args, **kwargs):
        raise RuntimeError("a case nobody anticipated")

    monkeypatch.setattr(AgentAdapter, "_interpret_harvested_result", explode)

    result = _run_on(tmp_path, harvested)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR
    assert result.summary == NO_RESULT
    assert result.output_dir == harvested and os.path.isdir(harvested)
