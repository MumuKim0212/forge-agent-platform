"""Thin agent adapter: the only module (together with
`app/agent_runner.py`, which runs inside the sandbox) that knows which
engine is in use.

Contract:
    input  {intent text, workspace, constraints/budgets}
    output {status, summary, usage, output_dir}

`output_dir` (ADR 0032 §Decision 5) is the harvested directory: what the agent
built, extracted from the sandbox by `SandboxRunner`. It is the build's output
and it is untrusted (authored in L3). The adapter returns it as is, whenever
the sandbox produced one -- including on failure, so the caller can clean it
up -- and never copies, edits or reads anything in it except
`.forge/result.json`. Sanitising it is the executor's job
(`app/services/harvest.py`); the input `workspace_dir` is staged input only.

`tools` is the second -- the platform-declared tool set,
composed in `app/services/agent_tools.py`, staged unchanged.

`system_prompt` is the first real instance of the "constraints"
half of that input -- composed by `app/services/agent_prompt.py`, never
here and never inline, and staged into `task_input.json` unchanged.

The engine (Claude Agent SDK, via `agent_runner.py`) runs inside the L3
sandbox via `SandboxRunner` (ADR 0014) — this module stages the task input
and the engine's runner script into the workspace, builds the `SandboxRunConfig`
(command + env pointing the engine's base-URL variable at the LLM proxy with a
per-build proxy token; ADR 0035: two engines, `anthropic` and `openai`), runs it, and interprets the harvested result file.
It has no security role: isolation is the sandbox's job, budgets
are the proxy's job. This module owns none of that — it only shapes input
and reads output.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
from dataclasses import dataclass

from app.services.sandbox_runner import SandboxRunConfig, SandboxRunner
from app.services.status import BuildFailureReason

logger = logging.getLogger("app.agent_adapter")

_APP_DIR = os.path.join(os.path.dirname(__file__), "..")

# `.forge/result.json` is written inside L3, so it is attacker-controlled input to the
# host. A real one is a short summary and two numbers; this bounds what the adapter will read
# of it (a result file the size of the disk cap must not be loaded into the worker's memory).
RESULT_MAX_BYTES = 1024 * 1024

_NO_USABLE_RESULT = "agent runner produced no usable result"


@dataclass(frozen=True)
class _EngineWiring:
    """How the sandbox reaches one engine (ADR 0035 §Decision 1: each engine has its own
    runner script and, here, its own environment variable names). The variables carry the
    proxy URL and the per-build proxy TOKEN, never the real key."""

    runner_script: str  # file name under app/, staged as .forge/<same name>
    base_url_env: str
    token_env: str
    base_url_suffix: str  # appended to the proxy's base URL (the OpenAI client wants `/v1`)


# The closed set (ADR 0035). An engine outside it raises in `run()` before anything is staged.
_ENGINE_WIRING: dict[str, _EngineWiring] = {
    "anthropic": _EngineWiring("agent_runner.py", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", ""),
    "openai": _EngineWiring("agent_runner_openai.py", "OPENAI_BASE_URL", "OPENAI_API_KEY", "/v1"),
}


@dataclass(frozen=True)
class AgentAdapterInput:
    """Input to one adapter run."""

    intent_text: str
    workspace_dir: str  # local dir staged into the sandbox, same as SandboxRunConfig.workspace_dir
    proxy_base_url: str
    proxy_token: str
    # The composed system prompt
    # app/services/agent_prompt.py). Required, not defaulted: every build
    # must carry it, unlike `token_budget` below, which is accepted but
    # never read anywhere -- do not follow that precedent for this field.
    system_prompt: str
    # The declared tool set (ADR 0029 §1,
    # app/services/agent_tools.py). Required, like `system_prompt`: a build
    # that forgot it must fail on the host, not run with the engine's
    # default tool set. Staged into task_input.json as `tools`.
    tools: tuple[str, ...]
    # ADR 0035: which engine's runner, environment and image this run uses, and the model
    # the runner is told to use (staged as `model` in task_input.json only when set -- the
    # Claude runner takes none). Decided by the worker at claim (`services/engines.py`),
    # never here.
    engine: str = "anthropic"
    model: str | None = None
    token_budget: int | None = None
    max_turns: int | None = None
    timeout_seconds: int | None = None
    image: str | None = None
    # the sandbox network shared with the build's LLM proxy
    # container (ADR 0014). When None, SandboxRunner creates (and
    # destroys) its own network as usual — used by tests that don't need
    # real proxy egress.
    network: object | None = None
    # the build this run belongs to. It goes onto the sandbox container (and onto a
    # network the runner creates itself) as `forge.build_id`, which is how the reconciler
    # decides they may not exist any more. None only for a run with no build (a test, the
    # disk-cap probe); the reconciler treats an object with no build as belonging to no
    # RUNNING build.
    build_id: str | None = None


@dataclass(frozen=True)
class AgentAdapterResult:
    """Output of one adapter run. `ok=False` always carries a `reason` from
    the failure taxonomy (BuildFailureReason)."""

    ok: bool
    reason: str | None = None
    summary: str | None = None
    usage_tokens: int = 0
    usage_cost_usd: float = 0.0
    logs: str = ""
    # ADR 0032: the harvested directory (the build's output, untrusted). Set
    # whenever the sandbox produced a harvest, ok or not; None when it did not.
    # The caller owns it from here and must remove it.
    output_dir: str | None = None
    # the sandbox's platform-authored sentence for a failure its reason code
    # under-describes (`SandboxRunResult.detail`: the disk or harvest limit that was
    # crossed). Only ever set from the sandbox runner's own templates; the executor
    # may put it in `Build.failure_detail`.
    detail: str | None = None


class AgentAdapter:
    """Runs the engine inside a sandbox for one build. A fresh instance (or
    a fresh call to `run()`) is expected per build."""

    def __init__(self, sandbox_runner: SandboxRunner | None = None) -> None:
        self._runner = sandbox_runner or SandboxRunner()

    def run(self, adapter_input: AgentAdapterInput) -> AgentAdapterResult:
        wiring = _ENGINE_WIRING.get(adapter_input.engine)
        if wiring is None:
            raise ValueError(f"no runner wiring for engine {str(adapter_input.engine)[:32]!r}")
        self._stage_runner_files(adapter_input, wiring)

        config = SandboxRunConfig(
            command=["python", f"/workspace/.forge/{wiring.runner_script}"],
            workspace_dir=adapter_input.workspace_dir,
            image=adapter_input.image,
            timeout_seconds=adapter_input.timeout_seconds,
            env={
                wiring.base_url_env: adapter_input.proxy_base_url + wiring.base_url_suffix,
                # The SDK sends this as its credential header; it is the proxy token, never the real key.
                wiring.token_env: adapter_input.proxy_token,
                # ADR 0032 §Amendment 2026-10-05: no bytecode is written in the sandbox
                # (and none is published: the sanitising copy drops it regardless).
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            network=adapter_input.network,
            build_id=adapter_input.build_id,
        )

        sandbox_result = self._runner.run(config)

        if not sandbox_result.ok:
            # Sandbox-level failure (timeout/resource/infra/harvest/agent
            # nonzero exit before writing a result file) — reason already
            # comes from the failure taxonomy via SandboxRunner.
            return AgentAdapterResult(
                ok=False,
                reason=sandbox_result.reason,
                logs=sandbox_result.logs,
                output_dir=sandbox_result.harvested_workspace_dir,
                detail=sandbox_result.detail,
            )

        # (ADR 0014 §Amendment 2026-10-17, "the engine's result file can never suppress the
        # proxy's record"). The result file is L3-authored and `_interpret_harvested_result` reads
        # it defensively, so it should never raise; this is the boundary that makes it unable to.
        # An exception here used to propagate out of `execute`, past the step that reads the
        # proxy's usage, refusals and budget outcome, so a build whose agent blew the budget and
        # wrote a garbage result file was recorded as an executor crash with no cost. Now it is an
        # ordinary failed result that CARRIES the harvested directory (the caller removes it).
        try:
            return self._interpret_harvested_result(
                sandbox_result.harvested_workspace_dir, sandbox_result.logs
            )
        except Exception:  # noqa: BLE001 - see the comment above: nothing about L3's file may propagate
            logger.exception("agent adapter: the harvested result file could not be interpreted")
            return AgentAdapterResult(
                ok=False,
                reason=BuildFailureReason.AGENT_ERROR,
                summary=_NO_USABLE_RESULT,
                logs=sandbox_result.logs,
                output_dir=sandbox_result.harvested_workspace_dir,
            )

    def _stage_runner_files(self, adapter_input: AgentAdapterInput, wiring: _EngineWiring) -> None:
        forge_dir = os.path.join(adapter_input.workspace_dir, ".forge")
        os.makedirs(forge_dir, exist_ok=True)

        task_input = {
            "intent_text": adapter_input.intent_text,
            "system_prompt": adapter_input.system_prompt,
            "tools": list(adapter_input.tools),
            "max_turns": adapter_input.max_turns,
        }
        if adapter_input.model is not None:
            task_input["model"] = adapter_input.model
        with open(os.path.join(forge_dir, "task_input.json"), "w", encoding="utf-8") as f:
            json.dump(task_input, f)

        with open(os.path.join(_APP_DIR, wiring.runner_script), encoding="utf-8") as src:
            runner_source = src.read()
        with open(os.path.join(forge_dir, wiring.runner_script), "w", encoding="utf-8") as dst:
            dst.write(runner_source)

    def _interpret_harvested_result(
        self, harvested_workspace_dir: str | None, logs: str
    ) -> AgentAdapterResult:
        if harvested_workspace_dir is None:
            return AgentAdapterResult(
                ok=False, reason=BuildFailureReason.HARVEST_ERROR, logs=logs
            )  # no harvest, so no output_dir

        result = _read_result_file(os.path.join(harvested_workspace_dir, ".forge", "result.json"))
        if result is None:
            return AgentAdapterResult(
                ok=False,
                reason=BuildFailureReason.AGENT_ERROR,
                summary=_NO_USABLE_RESULT,
                logs=logs,
                output_dir=harvested_workspace_dir,
            )

        # Every field is read by type, never by truthiness or by a conversion that can raise.
        # `ok` must BE `True` (a string "false" is truthy). The usage figures are diagnostic
        # only -- the proxy is the only source of the cost fields -- so a value that is not a
        # plain non-negative number is 0, not an error.
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        summary = result.get("summary")
        summary = summary if isinstance(summary, str) else None
        usage_tokens = _non_negative_int(usage.get("tokens"))
        usage_cost_usd = _non_negative_float(usage.get("cost_usd"))

        if result.get("ok") is not True:
            return AgentAdapterResult(
                ok=False,
                reason=BuildFailureReason.AGENT_ERROR,
                summary=summary,
                usage_tokens=usage_tokens,
                usage_cost_usd=usage_cost_usd,
                logs=logs,
                output_dir=harvested_workspace_dir,
            )

        return AgentAdapterResult(
            ok=True,
            summary=summary,
            usage_tokens=usage_tokens,
            usage_cost_usd=usage_cost_usd,
            logs=logs,
            output_dir=harvested_workspace_dir,
        )


def _read_result_file(path: str) -> dict | None:
    """The parsed `.forge/result.json`, or None when there is no usable one: missing, not a
    regular file (a link or a special file), larger than `RESULT_MAX_BYTES`, not UTF-8, not
    JSON, nested past the parser's depth, or JSON that is not an object (a list, a string, a
    number). Never raises: this is the one file L3 writes that the host parses."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        with open(path, "rb") as f:
            raw = f.read(RESULT_MAX_BYTES + 1)
        if len(raw) > RESULT_MAX_BYTES:
            return None
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None
    return parsed if isinstance(parsed, dict) else None


def _non_negative_int(value: object) -> int:
    """`value` if it is a plain non-negative int (a bool is not one), else 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _non_negative_float(value: object) -> float:
    """`value` as a float if it is a plain finite non-negative number (a bool is not one), else 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0
