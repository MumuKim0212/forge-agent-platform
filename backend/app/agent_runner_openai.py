"""In-sandbox agent runner for the `openai` engine (ADR 0035 §Decision 2).

The counterpart of `agent_runner.py`, staged into the workspace by
`services/agent_adapter.py` for a build that resolved to the `openai` engine
and run as the sandbox's command. Same contract: read `task_input.json`, drive
the engine against `/workspace`, write `.forge/result.json` in the existing
shape `{"ok", "summary", "usage": {"tokens", "cost_usd"}}`.

This is the *only* module that touches the OpenAI Agents SDK. It is standalone
by design, like the Claude runner: no imports from the rest of `app/` (pinned
by `tests/test_agent_runner_openai.py`), and no network beyond the LLM proxy
(ADR 0003 invariant 3): the SDK is pointed at the proxy with the per-build
proxy token, never the real key.

What is different from the Claude runner, and why (ADR 0035 §Context):

- **The tools are ours.** The Agents SDK sends only the function tools the
  caller registers and reads nothing from the workspace or the home directory,
  so the declared tool set (`task_input.json` `tools`, composed host-side in
  `services/agent_tools.py`) is implemented here as plain functions and
  registered only if declared. A declared name this runner does not implement
  fails closed, exactly like a missing or malformed list (ADR 0029 §1).
- **The file tools are confined to `/workspace`.** Every path is resolved with
  `realpath` and refused unless it stays inside the workspace's real path, so
  `..` traversal and a symlink out of the workspace are both refused. The
  shell runs inside the sandbox, which is what contains it.
- **Four request properties the proxy enforces are set here too**:
  `store=False`, `service_tier="default"` (the SDK has no field for it, so it
  goes through `extra_args`), an explicit model, and the turn limit. Because
  nothing is stored, the request also asks for `reasoning.encrypted_content`.
- **Tracing is off twice.** The SDK uploads traces to OpenAI by default. It is
  disabled in code and by `OPENAI_AGENTS_DISABLE_TRACING`; the proxy would
  refuse the upload anyway, so this is defence in depth.
- **The usage this writes is diagnostic only.** The proxy is the only cost
  source (ADR 0014 §Amendment 2026-10-01); `cost_usd` here is always 0.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys

TASK_INPUT_PATH = "/workspace/.forge/task_input.json"
RESULT_PATH = "/workspace/.forge/result.json"
WORKSPACE = "/workspace"

# Bounds on what one tool call may hand back to the model.
MAX_READ_CHARS = 100_000
MAX_LIST_ENTRIES = 500
MAX_GREP_MATCHES = 200
MAX_SHELL_OUTPUT_CHARS = 30_000
DEFAULT_SHELL_TIMEOUT_SECONDS = 120
MAX_SHELL_TIMEOUT_SECONDS = 600


class ToolSetError(Exception):
    """The declared tool set is absent, malformed, or names a tool this runner
    does not implement. Fatal, never defaulted (ADR 0029 §1)."""


class ToolRefused(Exception):
    """A tool call the runner refuses (path outside the workspace, no match,
    ...). The Agents SDK hands the message back to the model as the tool's
    result, so it names the problem and nothing else."""


# --------------------------------------------------------------------------
# The declared tool set
# --------------------------------------------------------------------------


def _require_tools(task_input: dict) -> list[str]:
    """The platform-declared tool set from the task input, verbatim. Fails
    closed on a missing, null, non-list, empty or non-string-entry value."""
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


def _require_model(task_input: dict) -> str:
    """The model to run, from the task input. Absent is a host bug: this engine
    has no default model to fall back on, and silently picking one would change
    the price of the build."""
    model = task_input.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ToolSetError("task_input.json has no 'model' -- refusing to pick a model of the runner's own")
    return model


# --------------------------------------------------------------------------
# Path confinement
# --------------------------------------------------------------------------


def _workspace_real() -> str:
    return os.path.realpath(WORKSPACE)


def _inside(real_root: str, real_path: str) -> bool:
    return real_path == real_root or real_path.startswith(real_root.rstrip(os.sep) + os.sep)


def _resolve(path: str) -> str:
    """`path` (absolute, or relative to the workspace) as a real path that is
    inside the workspace's real path, or `ToolRefused`. `realpath` follows every
    symlink on the way, so a link that points out of the workspace resolves to
    its target and is refused; `..` is collapsed before the check."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ToolRefused("path must be a non-empty string")
    root = _workspace_real()
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(candidate)
    if not _inside(root, real):
        raise ToolRefused(f"path {path!r} is outside the workspace")
    return real


def _display(real_path: str) -> str:
    return os.path.relpath(real_path, _workspace_real()).replace(os.sep, "/")


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated: {len(text) - limit} more characters]"


# --------------------------------------------------------------------------
# The tools (plain functions; `_build_tools` registers the declared ones)
# --------------------------------------------------------------------------


def read_file(path: str, offset: int = 0, limit: int = MAX_READ_CHARS) -> str:
    """Read a text file in the workspace. `offset` and `limit` are in characters."""
    real = _resolve(path)
    if not os.path.isfile(real):
        raise ToolRefused(f"{path!r} is not a file")
    with open(real, encoding="utf-8", errors="replace") as f:
        text = f.read()
    start = max(0, int(offset))
    cap = min(max(1, int(limit)), MAX_READ_CHARS)
    chunk = text[start : start + cap]
    if start + cap < len(text):
        chunk += f"\n...[truncated: {len(text) - start - cap} more characters]"
    return chunk


def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file in the workspace, creating parent directories."""
    real = _resolve(path)
    if os.path.isdir(real):
        raise ToolRefused(f"{path!r} is a directory")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} characters to {_display(real)}"


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace `old_string` with `new_string` in a workspace file. It must match
    exactly once unless `replace_all` is true."""
    real = _resolve(path)
    if not os.path.isfile(real):
        raise ToolRefused(f"{path!r} is not a file")
    if not old_string:
        raise ToolRefused("old_string must not be empty")
    with open(real, encoding="utf-8") as f:
        text = f.read()
    count = text.count(old_string)
    if count == 0:
        raise ToolRefused("old_string was not found")
    if count > 1 and not replace_all:
        raise ToolRefused(f"old_string matches {count} times; make it unique or set replace_all")
    with open(real, "w", encoding="utf-8") as f:
        f.write(text.replace(old_string, new_string))
    return f"replaced {count} occurrence(s) in {_display(real)}"


def _walk_files(base_real: str):
    """Real files under `base_real` whose own real path is inside the
    workspace. A symlink to a file or directory outside is skipped, never
    followed."""
    root = _workspace_real()
    for dirpath, dirnames, filenames in os.walk(base_real, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if _inside(root, os.path.realpath(os.path.join(dirpath, d))))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            real = os.path.realpath(full)
            if _inside(root, real) and os.path.isfile(real):
                yield full


def glob_files(pattern: str) -> str:
    """List workspace files whose path (relative to the workspace) matches a glob
    such as `**/*.py` or `src/*.ts`."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolRefused("pattern must be a non-empty string")
    root = _workspace_real()
    # `**/` matches zero or more directories; fnmatch's `*` already crosses `/`.
    regex = re.compile(fnmatch.translate(pattern.replace("**/", "*")))
    found = []
    for full in _walk_files(root):
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        # A pattern with no `/` (for example `*.py`) matches the file name anywhere.
        if regex.match(rel) or ("/" not in pattern and regex.match(os.path.basename(rel))):
            found.append(rel)
            if len(found) >= MAX_LIST_ENTRIES:
                break
    return "\n".join(found) if found else "(no matches)"


def grep_files(pattern: str, path: str = ".", glob: str | None = None) -> str:
    """Search workspace files for a regular expression. Returns `path:line:text`
    lines. `path` narrows the search to a file or directory; `glob` filters file
    names (for example `*.py`)."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolRefused(f"invalid regular expression: {exc}") from exc
    base = _resolve(path)
    files = [base] if os.path.isfile(base) else _walk_files(base)
    hits: list[str] = []
    for full in files:
        if glob and not fnmatch.fnmatch(os.path.basename(full), glob):
            continue
        try:
            with open(full, encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    if regex.search(line):
                        hits.append(f"{_display(os.path.realpath(full))}:{number}:{line.rstrip()[:300]}")
                        if len(hits) >= MAX_GREP_MATCHES:
                            return "\n".join(hits) + "\n...[truncated]"
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: not searchable text
    return "\n".join(hits) if hits else "(no matches)"


# The proxy URL and token are for the SDK client in this process, so they are not handed to
# the shell's environment. That keeps them from being inherited BY ACCIDENT (a command that
# dumps `env`, a child that logs its environment). It is NOT a boundary, and it does not
# keep the token from the code the agent runs: the shell is a child of this runner and runs
# as the SAME uid, so it can read `/proc/<runner pid>/environ` (and the runner's memory) and
# get the token and URL whenever it wants. (Measured 2026-10-19 in the pinned image as
# 65534 with every capability dropped and `no-new-privileges`, as the sandbox runs: a child
# whose environment lacks the key read it from `/proc/<parent pid>/environ`.)
#
# What actually bounds a build that holds its own proxy token is the proxy: the token is
# per build, dies with the build, is worth only what that build's budget allows, and every
# request made with it passes the proxy's policy (endpoint, request shape, model allowlist,
# headers) however it was made. The uid model is unchanged here.
_SHELL_ENV_DROPPED = ("OPENAI_API_KEY", "OPENAI_BASE_URL")


def run_shell(command: str, timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS) -> str:
    """Run a bash command in the workspace and return its exit code and output."""
    if not isinstance(command, str) or not command.strip():
        raise ToolRefused("command must be a non-empty string")
    timeout = min(max(1, int(timeout_seconds)), MAX_SHELL_TIMEOUT_SECONDS)
    env = {k: v for k, v in os.environ.items() if k not in _SHELL_ENV_DROPPED}
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=WORKSPACE,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolRefused(f"command timed out after {timeout} seconds") from None
    output = (proc.stdout or "") + (proc.stderr or "")
    return f"exit code {proc.returncode}\n{_bound(output, MAX_SHELL_OUTPUT_CHARS)}"


# The runner's implementations, by declared name. The names are the host's
# (`services/agent_tools.py` OPENAI_TOOLS); a declared name missing here is a
# `ToolSetError`, and an implemented name that is not declared is not registered.
IMPLEMENTATIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "glob": glob_files,
    "grep": grep_files,
    "shell": run_shell,
}


def _build_tools(declared: list[str], function_tool) -> list:
    unknown = [name for name in declared if name not in IMPLEMENTATIONS]
    if unknown:
        raise ToolSetError(
            f"task_input.json 'tools' names tools this runner does not implement: {', '.join(sorted(unknown))}"
        )
    return [function_tool(IMPLEMENTATIONS[name], name_override=name) for name in declared]


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


async def _run(
    intent_text: str,
    max_turns: int | None,
    system_prompt: str | None,
    tools: list[str],
    model: str,
) -> dict:
    # Tracing is off by environment first, before the SDK is even imported (it reads the
    # variable on first use), and in code just below.
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

    # Imported here so the file can be import-checked and unit-tested without
    # the SDK; only the pinned OpenAI sandbox image needs it.
    from agents import (
        Agent,
        ModelSettings,
        Runner,
        function_tool,
        set_default_openai_client,
        set_tracing_disabled,
    )
    from openai import AsyncOpenAI

    set_tracing_disabled(True)

    # The proxy URL (including `/v1`) and the per-build proxy token come from the
    # sandbox environment. Never the real key: the sandbox never holds it.
    client = AsyncOpenAI(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )
    set_default_openai_client(client, use_for_tracing=False)

    agent = Agent(
        name="forge-builder",
        instructions=system_prompt,
        model=model,
        model_settings=ModelSettings(
            store=False,
            # With `store=False` nothing is kept server-side, so the reasoning a turn produced can
            # only be carried to the next turn as its encrypted content. Asking for it is OpenAI's
            # documented stateless flow; without it a re-sent reasoning item is an id the API does
            # not hold. The proxy records it as presence and length only, never as text.
            response_include=["reasoning.encrypted_content"],
            # No ModelSettings field for it; the proxy refuses a request without it.
            extra_args={"service_tier": "default"},
        ),
        tools=_build_tools(tools, function_tool),
    )

    result = await Runner.run(agent, intent_text, max_turns=max_turns)

    usage = result.context_wrapper.usage
    tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "output_tokens", 0) or 0)
    final = result.final_output
    summary = (final if isinstance(final, str) else json.dumps(final, default=str)) if final is not None else ""
    return {
        "ok": True,
        "summary": summary.strip() or "agent produced no summary text",
        "usage": {"tokens": tokens, "cost_usd": 0.0},
    }


def main() -> int:
    try:
        with open(TASK_INPUT_PATH, encoding="utf-8") as f:
            task_input = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _write_result({"ok": False, "summary": f"failed to read task input: {exc}", "usage": {}})
        return 1

    system_prompt = task_input.get("system_prompt")
    if not system_prompt:
        print(
            "agent_runner_openai: task_input.json has no system_prompt -- running "
            "without one (a host bug, not an agent failure)",
            file=sys.stderr,
        )

    # ADR 0029 §1: fail closed before the engine is ever imported.
    try:
        tools = _require_tools(task_input)
        model = _require_model(task_input)
        unknown = [name for name in tools if name not in IMPLEMENTATIONS]
        if unknown:
            raise ToolSetError(
                f"task_input.json 'tools' names tools this runner does not implement: {', '.join(sorted(unknown))}"
            )
    except ToolSetError as exc:
        print(f"agent_runner_openai: {exc}", file=sys.stderr)
        _write_result({"ok": False, "summary": f"agent_runner_openai: {exc}", "usage": {}})
        return 1

    try:
        result = asyncio.run(
            _run(task_input["intent_text"], task_input.get("max_turns"), system_prompt, tools, model)
        )
    except Exception as exc:  # noqa: BLE001 - any engine failure must still produce a result file
        _write_result({"ok": False, "summary": f"agent engine error: {exc}", "usage": {}})
        return 1

    _write_result(result)
    return 0 if result["ok"] else 1


def _write_result(result: dict) -> None:
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    sys.exit(main())
