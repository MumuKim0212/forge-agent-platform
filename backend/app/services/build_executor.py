"""The pluggable executor interface the worker calls for each RUNNING build
(run flow, steps 2-4: provision/run agent/harvest).

This slice only wires the worker <-> executor boundary and ships a
placeholder implementation with NO sandbox, agent, or git behavior — those
land separately (sandbox, agent adapter, git/image/preview).
Swapping in the real executor later means implementing `BuildExecutor` and
pointing the worker at it; no worker/queue code changes.
"""

from dataclasses import dataclass
from typing import Protocol

from app.models.build import Build
from app.services.status import BuildFailureReason


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of running one build. `ok=False` always carries a `reason`
    from the failure taxonomy (BuildFailureReason)."""

    ok: bool
    reason: str | None = None
    # On `ok=False`: the platform-authored failure detail. Never agent text.
    summary: str | None = None
    # on `ok=True`, the AGENT's own text exactly as the adapter returned it, with
    # no platform sentence appended. The worker hands it to `mark_succeeded`; nothing
    # else reads it. Always None on a failure -- the failure path carries no agent text.
    agent_summary: str | None = None


class BuildExecutor(Protocol):
    """Adapter contract (design level): the worker only knows this
    interface, never the engine/sandbox behind it."""

    def execute(self, build: Build) -> ExecutionResult:
        ...


class PlaceholderBuildExecutor:
    """PLACEHOLDER EXECUTOR — no sandbox, no agent, no git.

    Deterministically succeeds for every build. This exists only so the
    worker's claim -> execute -> transition loop is exercisable end-to-end
    before the real sandbox/agent/git pipeline lands.
    Do not add real behavior here; implement a new BuildExecutor instead.
    """

    def execute(self, build: Build) -> ExecutionResult:  # noqa: ARG002
        return ExecutionResult(ok=True, summary="placeholder executor: no-op success")


class AlwaysFailBuildExecutor:
    """Test-only placeholder executor that always fails with AGENT_ERROR."""

    def execute(self, build: Build) -> ExecutionResult:  # noqa: ARG002
        return ExecutionResult(ok=False, reason=BuildFailureReason.AGENT_ERROR)
