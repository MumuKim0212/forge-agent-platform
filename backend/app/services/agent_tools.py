"""The build agent's tool set (ADR 0029 §1).

Declared once, here, on the host, and carried into the sandbox as the
`tools` key of `task_input.json` (`agent_adapter.py._stage_runner_files`).
`agent_runner.py` forwards it verbatim to the engine's option that makes
unlisted tools **unavailable** and authors none of it -- the same rule as
`agent_prompt.py` for policy text.

It is policy, not operator tuning, so it is a constant and not a `Settings`
field.

The names are the ones the pinned Claude Code CLI (2.1.285, see
`docker/agent-sandbox/Dockerfile`) reports in its init message and sends in
its request, measured by the engine-up capture test -- not recalled from
documentation. A CLI upgrade re-runs that capture.

What is in: the file tools and a shell. Executing code is what L3 is for
(ADR 0003), and anything these can do inside the sandbox generated code can
already do at build or preview time.

What is out: every tool whose effect leaves the sandbox -- web search, web
fetch, any MCP tool (ADR 0029 §1). Also out, deliberately, and not because
they leave the sandbox: sub-agent spawning, scheduling/cron, worktree, skill
and notification tools. They add no capability a build needs and each is one
more surface that loads configuration of its own. The pinned CLI has no
purely-local todo tool in its default set, so the bookkeeping question ADR
0029 left open does not arise for this version.

The LLM proxy (`llm_proxy.py`) is the enforced half of this boundary; this
declaration is defence-in-depth behind it.
"""

from __future__ import annotations

AGENT_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
)


# ADR 0035 §Decision 2: the `openai` engine's declared set, the same capability
# (file tools and a shell, nothing that leaves the sandbox) under the names the
# OpenAI runner (`agent_runner_openai.py`) implements itself as function tools.
# Unlike the Claude set these names are not measured from a CLI: the runner owns
# them, registers exactly the declared ones, and fails closed on a name it does
# not implement.
OPENAI_AGENT_TOOLS: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "shell",
)

_TOOLS_BY_ENGINE: dict[str, tuple[str, ...]] = {
    "anthropic": AGENT_TOOLS,
    "openai": OPENAI_AGENT_TOOLS,
}


def declared_agent_tools(engine: str = "anthropic") -> list[str]:
    """The tool set for one build on `engine`, as a fresh list (JSON-serialisable,
    and not a shared mutable object a caller could widen). An unknown engine
    raises: there is no default set to fall back on (ADR 0029 §1)."""
    try:
        return list(_TOOLS_BY_ENGINE[engine])
    except KeyError:
        raise ValueError(f"no declared tool set for engine {engine!r}") from None
