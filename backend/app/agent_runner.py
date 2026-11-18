"""In-sandbox agent runner (ADR 0014).

Staged into the workspace by `services/agent_adapter.py` and executed as the
sandbox's command via `SandboxRunner`. Runs entirely inside L3: reads the
task input written alongside it, drives the Claude Agent SDK's `query()`
against the workspace, and writes a JSON result file the host-side adapter
reads back after harvest.

This is the *only* module that touches the Claude Agent SDK — the thin
adapter's engine-specific half. Swapping engines later means
replacing this script (and the pinned image that ships its runtime), not
touching `agent_adapter.py`'s contract, `sandbox_build_executor.py`, or the
worker/queue.

Standalone by design: no imports from the rest of `app/` (it runs inside
the sandbox container, not the platform process), and it must not assume
network access beyond the LLM proxy (ADR 0003 invariant 3) — the Claude
Agent SDK reaches the proxy via `ANTHROPIC_BASE_URL`, set in this process's
environment by the sandbox config, exactly like it would reach the real API.
"""

from __future__ import annotations

import asyncio
import json
import sys

TASK_INPUT_PATH = "/workspace/.forge/task_input.json"
RESULT_PATH = "/workspace/.forge/result.json"


class ToolSetError(Exception):
    """The declared tool set is absent or malformed. Fatal, never defaulted:
    a runner that fell back to the engine's default tool set would widen the
    boundary silently (ADR 0029 §1)."""


def _require_tools(task_input: dict) -> list[str]:
    """The platform-declared tool set from the task input, verbatim.

    Fails closed, unlike `system_prompt` (which warns and continues): a
    missing prompt degrades the output, a missing tool set widens the
    boundary. The two failures carry distinguishable messages so a host bug
    ("never staged") is not confused with a corrupt declaration."""
    if "tools" not in task_input or task_input["tools"] is None:
        raise ToolSetError("task_input.json has no 'tools' key -- refusing to run with the engine's default tool set")
    tools = task_input["tools"]
    if not isinstance(tools, list):
        raise ToolSetError(
            f"task_input.json 'tools' is malformed: expected a list of tool names, got {type(tools).__name__}"
        )
    if not tools:
        raise ToolSetError("task_input.json 'tools' is malformed: the list is empty")
    if not all(isinstance(t, str) and t.strip() for t in tools):
        raise ToolSetError("task_input.json 'tools' is malformed: every entry must be a non-empty string")
    return list(tools)


async def _run(
    intent_text: str, max_turns: int | None, system_prompt: str | None, tools: list[str]
) -> dict:
    # Imported here, not at module scope, so this file can be unit-tested
    # (import-checked) even in environments without the SDK installed —
    # only the pinned sandbox image needs it.
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

    options = ClaudeAgentOptions(
        cwd="/workspace",
        # A build has no human to answer a prompt. With `tools`, `setting_sources`
        # and the proxy's request rule in place this mode governs only whether
        # an *available* tool asks first, never which tools exist (ADR 0029 §5).
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        # ADR 0029 §1: the platform-declared tool set, forwarded verbatim, never
        # authored here. `tools` (SDK: "the base set of available built-in
        # tools") makes unlisted tools UNAVAILABLE. `allowed_tools` would only
        # auto-approve the listed ones and is deliberately not used.
        tools=tools,
        # ADR 0029 §3: load no engine configuration from the workspace (project
        # settings, hooks, .mcp.json, CLAUDE.md are untrusted input on a modify
        # build). `[]` is the SDK's "SDK isolation mode"; the default (None)
        # loads every source. No `mcp_servers` are declared, and
        # `strict_mcp_config` makes the engine ignore every other MCP source too.
        setting_sources=[],
        strict_mcp_config=True,
        # forwarded, never authored here
        # — composed host-side by app/services/agent_prompt.py
        # and carried in task_input.json. May be None; see main()'s
        # handling of an absent key below.
        system_prompt=system_prompt,
    )

    summary_parts: list[str] = []
    usage_tokens = 0
    usage_cost_usd = 0.0
    ok = True

    async for message in query(prompt=intent_text, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    summary_parts.append(block.text)
        elif isinstance(message, ResultMessage):
            ok = not message.is_error
            usage_cost_usd = message.total_cost_usd or 0.0
            usage = message.usage or {}
            usage_tokens = int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )

    return {
        "ok": ok,
        "summary": "\n".join(summary_parts).strip() or "agent produced no summary text",
        "usage": {"tokens": usage_tokens, "cost_usd": usage_cost_usd},
    }


def main() -> int:
    try:
        with open(TASK_INPUT_PATH, encoding="utf-8") as f:
            task_input = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _write_result({"ok": False, "summary": f"failed to read task input: {exc}", "usage": {}})
        return 1

    # A missing prompt is a host bug, not an agent
    # failure -- .get() with a safe (None) default, matching max_turns,
    # and never folded into AGENT_ERROR. But it must not run in silence:
    # say so on stderr so the gap is visible in the build logs.
    system_prompt = task_input.get("system_prompt")
    if not system_prompt:
        print(
            "agent_runner: task_input.json has no system_prompt -- running "
            "without one (a host bug, not an agent failure)",
            file=sys.stderr,
        )

    # ADR 0029 §1: fail closed before the engine is ever imported.
    try:
        tools = _require_tools(task_input)
    except ToolSetError as exc:
        print(f"agent_runner: {exc}", file=sys.stderr)
        _write_result({"ok": False, "summary": f"agent_runner: {exc}", "usage": {}})
        return 1

    try:
        result = asyncio.run(
            _run(task_input["intent_text"], task_input.get("max_turns"), system_prompt, tools)
        )
    except Exception as exc:  # noqa: BLE001 - any engine failure must still produce a result file
        _write_result({"ok": False, "summary": f"agent engine error: {exc}", "usage": {}})
        return 1

    _write_result(result)
    return 0 if result["ok"] else 1


def _write_result(result: dict) -> None:
    import os

    os.makedirs("/workspace/.forge", exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    sys.exit(main())
