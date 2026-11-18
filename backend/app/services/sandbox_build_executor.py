"""Sandbox-backed `BuildExecutor`: the real agent build path.

Implements the `BuildExecutor` protocol (`build_executor.py`) around
the `SandboxRunner` and the agent adapter (`agent_adapter.py`), wired to
a per-build LLM egress proxy (`proxy_container.py`, ADR 0014). `SandboxRunner`
 is the standalone runner; this module is the wiring around it.

Run flow per build (steps 2-5):
    1. Start a per-build LLM proxy container, multi-homed onto a fresh
       per-run sandbox network (ADR 0014; verified in
       test_proxy_container_integration.py that this reaches the sandbox
       while keeping external egress for the real Anthropic API).
    2. Issue a proxy token scoped to the build's token/cost budget.
    3. Stage the workspace -- the INPUT directory: the template skeleton for a
       create build, the confirmed ref for a modify build -- and run the agent
       adapter, which drives the Claude Agent SDK inside the sandbox via
       SandboxRunner, with `ANTHROPIC_BASE_URL` pointed at the proxy. The
       staged directory is staging only: the adapter writes `.forge/` into it,
       and nothing is published from it (ADR 0032). What the agent built comes
       back as the adapter's `output_dir`, a separate harvested directory.
    4. Harvest usage from the proxy and write it onto `Build.cost_tokens` /
       `Build.cost_usd` — always, even on failure — revoke the token, and
       tear down the proxy container. The proxy is the ONLY source of those
       fields (ADR 0014 §Amendment 2026-10-01): the engine's own
       `result.json` usage is written inside L3 and is only ever logged. If
       the proxy's usage cannot be read back, both fields are written NULL
       (not measured) and the build fails `INFRA_ERROR`.
    5. ADR 0032: on agent success, sanitise the harvested directory
       into a fresh `forge-publish-*` directory (`app/services/harvest.py`:
       regular files and directories only; `.git` at any depth and the root
       `.forge` are not copied; a link or special file fails the build), then
       publish THAT copy (restore protected paths, commit, push a per-build
       branch, open a PR via the configured `GitProvider` --
       `app/services/publish.py`), assemble an OCI image from it
       (`app/services/image_assembly.py`) and run the policy gate over it.
       Writes `Build.diff_ref` / `Build.image_ref`. Host tooling (git, docker
       build, the policy checks) never runs in the harvested tree itself: its
       bytes were authored in L3. Publish/image-assembly failure fails the
       build with `HARVEST_ERROR` -- never silently.
    6. Every temporary tree -- staged, harvested, sanitised -- is removed when
       `execute` returns, on every path, best-effort and logged if it fails.
    7. With the repair loop (`BUILD_REPAIR_MAX_ATTEMPTS`, default 1, 0 = the flow above exactly): when
       the preview's smoke check measured a failing route (`preview_smoke.has_failing_route`), the
       build runs a repair attempt inside this same `execute`, on the same proxy, token and budget:
       a new single-use sandbox run over the previous attempt's sanitised output, told only the
       measured failing routes (`agent_prompt.compose_repair_intent_text`). The proxy's usage is
       drained and read ONCE, after the last attempt (a running read that does not close the token
       serves the limits); publish (one branch, one PR) and the policy gate run ONCE, for the last
       attempt that completed (`_execute_with_repair`, the comment above it says why publish is
       last). An attempt that does not complete is discarded and never fails a build that had
       passed; a recorded refusal, an unmetered response or a failed drain still does.

Failure detail (failure taxonomy): every
`ok=False` result carries a `summary` that the worker persists as the build's
`failure_detail`, which every authenticated reader of the timeline sees. So the
summary is platform-authored only -- a platform template filled with ids, codes,
counts, limits, the exception CLASS name, or the LLM proxy's own bounded refusal
and unmetered details. It is never the agent's summary, the runner's or
adapter's output (L3-authored), nor a library exception's free-form text; those
go to the log. The success summary is untouched (it is the PR body).

Failure mapping:
    - A modify-type build whose output adds, edits or deletes a file under a
      schema path (`Settings.protected_schema_paths`) -> AGENT_ERROR, before
      anything is published (ADR 0009 §Amendment 2026-10-05).
    - A build whose sanitised output lacks the template's service entrypoint ->
      MISSING_ENTRYPOINT, before the git provider is constructed (nothing pushed, assembled or
      previewed); a template whose Dockerfile yields no entrypoint -> INFRA_ERROR
      (`service_entrypoint.py`).
    - Proxy fails to start / becomes unreachable -> ENGINE_UNREACHABLE. ANY exception
      out of the start (not only `ProxyStartError` -- a container that exits
      right after it starts surfaces as a Docker 409 or a `KeyError`) ends with the
      sandbox network destroyed and this reason.
    - Proxy reports the token over budget, or reports a success it could
      not meter (the budget can no longer be enforced) -> BUDGET_EXCEEDED.
    - Proxy usage unreadable at build end -> INFRA_ERROR, cost NULL. When the cause is the
      drain (it failed or ran out with a call still alive), the build keeps the reason it already
      had (TIMEOUT, RESOURCE_LIMIT, a recorded refusal, a spent budget) and the detail adds that its
      cost could not be read back; INFRA_ERROR is only for a build with nothing else failed first.
    - A proxy image that does not advertise `drain` refuses the build before the agent runs
      (INFRA_ERROR naming the rebuild command): its cost could never be recorded.
    - A build whose requester is ambiguous at gate time (several change
      requests) -> INFRA_ERROR, no approval request created.
    - Publish (git push/PR) or image assembly failure, a harvested tree that
      holds a link or special file (ADR 0032), or a success with no harvested
      directory to publish -> HARVEST_ERROR (the failure taxonomy's own
      vocabulary for "diff extraction or image assembly failed" already
      covers the publish step's failure modes).
    - Everything else maps through unchanged from SandboxRunner / the
      adapter (TIMEOUT, RESOURCE_LIMIT, AGENT_ERROR, HARVEST_ERROR,
      INFRA_ERROR).
"""

from __future__ import annotations

import copy
import logging
import math
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import docker
from docker.errors import DockerException
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.config import settings
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.services.agent_adapter import AgentAdapter, AgentAdapterInput, AgentAdapterResult
from app.services.agent_prompt import compose_intent_text, compose_repair_intent_text, compose_system_prompt
from app.services.agent_tools import declared_agent_tools
from app.services.engines import engine_config
from app.services.build_trace import (
    DIFF_COMPUTED,
    DIFF_NOT_REACHED,
    DIFF_UNAVAILABLE,
    compute_diff,
    normalise_calls,
    store_trace,
)
from app.services.build_queue import FAILURE_DETAIL_MAX_CHARS, FAILURE_DETAIL_TRUNCATION_MARKER
from app.services.llm_proxy import POLICY_REFUSAL_PREFIX, UPSTREAM_TIMEOUT_SECONDS
from app.services.build_executor import ExecutionResult
from app.services.git_provider import GitProvider, GitProviderError, RepoRef, create_provider
from app.services.harvest import (
    UnsafeHarvestEntry,
    describe_counts,
    is_bytecode_name,
    remove_tree,
    sanitise_harvest,
)
from app.services.audit import write_audit_log
from app.services.image_assembly import ImageAssemblyError, assemble_image, build_tag_for
from app.services.approval import (
    AmbiguousRequesterError,
    create_approval_request_for_verdict,
    resolve_requester_for_build,
)
from app.services.policy_gate import (
    GATE_STAGE_CHECKS,
    GATE_STAGE_ROUTING,
    record_gate_error_verdict,
    run_and_persist_policy_gate,
)
from app.services.preview_manager import (
    PreviewManager,
    PreviewSigningSecretMissingError,
    PreviewStartError,
)
from app.services.preview_smoke import (
    PreviewSmokeProber,
    Prober,
    failing_routes,
    has_failing_route,
    run_smoke_check,
)
from app.services.platform_labels import Role, platform_labels
from app.services.proxy_container import (
    PROXY_IMAGE,
    REQUIRED_PROXY_CAPABILITIES,
    ProxyContainer,
    ProxyImageStaleError,
    ProxyStartError,
    proxy_image_gap,
)
from app.services.change_set import measure_changes
from app.services.protected_paths import (
    SchemaChangeRefused,
    protected_drift,
    refuse_schema_changes,
    restore_protected_paths,
)
from app.services.publish import PublishError, git_env, publish_build, run_git_process
from app.services.sandbox_runner import SandboxRunner
from app.services.service_entrypoint import EntrypointUndeterminable, missing_entrypoint
from app.services.status import BuildFailureReason, PreviewStartFailure, ProjectKind

logger = logging.getLogger("app.sandbox_build_executor")


class BuildRequestError(Exception):
    """The build has no single, usable request to run. Raised before
    any resource exists; the message is the build's failure summary."""


class NoConfirmedRefError(Exception):
    """A modify-type build has no confirmed ref to start from.
    The submit endpoint refuses such a request, so this
    is the defence in depth for a build queued before that refusal existed, or whose project's ref
    was cleared since. `execute` turns it into a failed result; the message is platform text and
    is the build's failure summary."""


NO_CONFIRMED_REF_BUILD_DETAIL = (
    "this modify build was not run: its project has no confirmed ref, so there is no confirmed "
    "code to modify. Confirm a first deployment, or submit a new create request"
)

# The usage-payload keys the amendment added (llm_proxy.UsageResponse). Literal,
# not imported from the proxy module: the point is to notice an image that lacks them.
_AMENDMENT_USAGE_FIELDS = ("unmetered", "unmetered_reason")

# Before it reads the proxy's usage, the executor drains the build's
# token: the proxy stops admitting requests and waits for the call that is still alive to finish and
# record. A call can outlive its sandbox: the sandbox died (timeout, crash, a harvest limit) with a
# request out, uvicorn does not cancel a handler when its client goes, and the call records its spend
# when the provider answers, after a read made too early. Normally nothing is alive and the wait is one
# round trip. Only ONE call can still be running when the wait starts (the proxy closes the token
# first, so a queued request is refused at the slot, not forwarded).
#
# That call can stay out for as long as the proxy's own upstream
# timeout (`llm_proxy.UPSTREAM_TIMEOUT_SECONDS`, 120 s), so the TOTAL wait is derived from that
# constant plus a margin and never restates the number. One exec cannot carry it: docker-py's read
# timeout is 60 s, so the drain is asked in STEPS of at most `PROXY_DRAIN_STEP_SECONDS`. The proxy's
# drain is idempotent (closing a closed token is a no-op), so the next step just waits on. Constants,
# not settings; read at call time so a test shortens them.
#
# Reasoned, not measured: httpx's timeout is per phase, so a response that keeps streaming chunks can
# outlast it; what the total covers is a call stuck on the provider. If it runs out, the usage read
# would be short, so the cost fields stay NULL and the figure is never stored (`_DrainError`); the
# build keeps the reason it already had (a TIMEOUT stays a TIMEOUT).
PROXY_DRAIN_STEP_SECONDS = 30.0
PROXY_DRAIN_MARGIN_SECONDS = 30.0
PROXY_DRAIN_TOTAL_SECONDS = UPSTREAM_TIMEOUT_SECONDS + PROXY_DRAIN_MARGIN_SECONDS


class _UsageReadError(Exception):
    """The proxy's usage for a build could not be read back. The message
    names why (it is for the log); it never contains the proxy token.

    `detail` is the part that may reach `Build.failure_detail`. It
    defaults to the message, which is right for every variant the platform
    authored in full; the variant built from an exception's own text passes a
    class-name-only `detail` instead, because that text can be free-form
    infrastructure output."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = message if detail is None else detail


class _DrainError(_UsageReadError):
    """the token could not be drained (the proxy could not be asked, answered something
    unreadable, or still had a call alive when the total wait ran out), so the usage read would be
    short. The cost stays NULL like any read-back failure, but unlike one it does not overrule the
    reason the build already had: a build that timed out is still a TIMEOUT whose cost is unknown
    (`SandboxBuildExecutor.execute`)."""


@dataclass
class _TraceCapture:
    """What `execute` collects for the build's trace on the way through:
    whether the agent was reached, its log, and the diff `_publish` computes. The proxy's
    call record is read at the end, in `_store_trace`. Pure bookkeeping -- nothing in
    `execute` reads any of it to decide anything."""

    agent_ran: bool = False
    logs: str | None = None
    diff_state: str = DIFF_NOT_REACHED
    diff: dict | None = None


def _utcnow() -> datetime:
    """The clock the repair loop's limits read: a build's remaining wall-clock and how long
    an attempt took. One function so a test can move it."""
    return datetime.now(timezone.utc)


@dataclass
class _Attempt:
    """One attempt of a build with the repair loop on: the tree it produced and
    the measured facts about it, kept so that a later attempt can replace it, be discarded in its
    favour, or fall back to it. Nothing here is read to make a decision except the smoke fact (the
    trigger) and `publish_dir`/`summary` (what ships)."""

    index: int  # 0 = the first attempt, k = the k-th repair attempt
    summary: str | None  # the agent's own text: it becomes the PR body of the attempt that ships
    publish_dir: str  # the sanitised copy; the only tree publish and the gate see
    tag: str | None = None  # the image assembled from it (None until it exists, and once removed)
    changed_files: dict | None = None
    protected_path_drift: list | None = None
    diff_state: str = DIFF_NOT_REACHED
    diff: dict | None = None
    preview_url: str | None = None
    preview_expires_at: datetime | None = None
    preview_token_hash: str | None = None
    preview_start_failure: str | None = None
    smoke_checks: dict | None = None
    touched_preview: bool = False  # `start_preview` was called for it, which replaces any earlier one


@dataclass
class _RepairRun:
    """What `_execute_with_repair` needs to clean up: every attempt that got as far as an image, and
    the one that shipped (None until publish succeeded)."""

    attempts: list[_Attempt] = field(default_factory=list)
    shipped: _Attempt | None = None
    touched_preview: bool = False


def _stage_failure(stage: str, exc: BaseException) -> str:
    """a harvest-stage failure's detail -- the stage and the exception
    CLASS only. The exception's text can carry git/Docker output and stays in the
    log."""
    return (
        f"harvesting the build's output failed at {stage} ({type(exc).__name__}); "
        "see the worker log"
    )


def _missing_entrypoint_detail(required: list[str]) -> str:
    """the failure detail of a build whose output lacks the template's entrypoint.
    Platform text: the paths come from the template's own Dockerfile (`service_entrypoint`),
    never from the agent's output."""
    wanted = " or ".join(required)
    return (
        f"the build's output has no app entrypoint: the template's service starts from {wanted}, "
        "and the output holds none of them. The build was stopped before an image was assembled "
        "or a preview started. Re-submit the request; on a modify build, do not delete or rename "
        "the app's entry file"
    )


class SandboxBuildExecutor:
    """Real `BuildExecutor` implementation: sandbox + LLM proxy + agent
    adapter. Selected by the worker only when `Settings.use_sandbox_executor`
    is set and at least one engine's key is configured (ADR 0035) — otherwise
    `PlaceholderBuildExecutor` stays the fallback.

    It does not decide the engine. The worker resolves it when it claims the
    build (`services/engines.resolve_engine`, `worker.assign_engine`) and writes
    it to `Build.engine`; `uses_engine` is how the worker knows to do that. A build
    with no engine recorded is one from before engines were (or a test that never
    went through a claim): `execute` fails it, it never defaults to an engine."""

    uses_engine = True
    # this executor starts previews (`_start_preview`), so the worker runs the preview
    # disk watcher beside it; the placeholder executor has no such attribute and starts none.
    starts_previews = True

    def __init__(
        self,
        *,
        docker_client: docker.DockerClient | None = None,
        adapter: AgentAdapter | None = None,
        git_provider: GitProvider | None = None,
        preview_manager: PreviewManager | None = None,
        proxy_image: str = PROXY_IMAGE,
        smoke_prober_factory: Callable[[Build], Prober] | None = None,
    ) -> None:
        self._docker = docker_client or docker.from_env()
        # which proxy image this executor starts a build's proxy from. A test seam (a stale
        # image, to see a build refused); the platform always runs the default.
        self._proxy_image = proxy_image
        self._adapter = adapter or AgentAdapter(SandboxRunner(client=self._docker))
        # injectable for tests (stub provider / stub HTTP upstream);
        # otherwise built from Settings on first use (lazy — constructing a
        # SandboxBuildExecutor must not require git provider config when
        # publish is never reached, e.g. in the existing tests).
        self._git_provider = git_provider
        # injectable for tests (fake Docker client double); otherwise
        # a real PreviewManager against the same Docker client.
        self._preview_manager = preview_manager or PreviewManager(client=self._docker)
        # how the smoke check reaches the preview. A test seam (a recording or failing
        # prober); the platform always uses the token-checked route through the preview manager.
        self._smoke_prober_factory = smoke_prober_factory or self._default_smoke_prober

    @property
    def proxy_image(self) -> str:
        """the image every build's LLM proxy container is started from."""
        return self._proxy_image

    @property
    def proxy_required_capabilities(self) -> tuple[str, ...]:
        """what the platform needs the proxy image to advertise (for the startup banner)."""
        return REQUIRED_PROXY_CAPABILITIES

    def proxy_image_gap(self) -> str | None:
        """None when the proxy image advertises everything the platform
        needs of it (`REQUIRED_PROXY_CAPABILITIES`), else one sentence naming the gap and the rebuild
        command. The worker logs it at startup (`worker._log_proxy_image_startup_banner`), the way
        `disk_cap_gap` is logged; a build against an image with a gap is refused in `execute`."""
        return proxy_image_gap(self._docker, self._proxy_image)

    def disk_cap_gap(self) -> str | None:
        """None when this host's container engine lets the platform enforce the
        build sandbox's disk cap, else one sentence naming the gap. The worker logs it at
        startup (`worker._log_disk_cap_startup_banner`); it is a real probe of the engine
        (`SandboxRunner.probe_disk_cap`), not a statement about the platform."""
        return SandboxRunner(client=self._docker).probe_disk_cap()

    def preview_disk_cap_gap(self) -> str | None:
        """the same question for the preview containers this executor starts. A preview
        never runs on `build_container_runtime`, so it is its own probe (a throwaway container on
        the default runtime), not the build's answer re-read; None when the engine reports a
        preview's writable layer, else one sentence naming the gap, worded for previews."""
        return SandboxRunner(client=self._docker).probe_disk_cap(preview=True)

    def execute(self, build: Build) -> ExecutionResult:
        # ADR 0035 §Decision 5, fail closed: the engine is decided by the worker at
        # claim and recorded on the build. A build that arrives with none was never through
        # a claim (or predates the column); running it on a default engine would pick a
        # vendor and a cost path nobody chose, so it fails before any resource exists.
        if build.engine is None:
            logger.error("sandbox executor: build %s reached execute with no recorded engine", build.id)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary=(
                    "the build has no recorded agent engine, so it was not run: the engine is "
                    "chosen when the worker claims a build, and this build never went through that"
                ),
            )

        # resolve what the
        # requester asked for BEFORE any resource exists. A build with no
        # request looks exactly like a successful one, so it never runs; and
        # failing here means no network, proxy container or token to clean up.
        try:
            intent_text = self._intent_text_for(build)
        except BuildRequestError as exc:
            logger.warning("sandbox executor: build %s has no runnable request: %s", build.id, exc)
            return ExecutionResult(
                ok=False, reason=BuildFailureReason.INFRA_ERROR, summary=str(exc)
            )

        # ADR 0035: the engine the worker recorded at claim, as this run's configuration.
        # Reading it is not deciding it; an engine outside the closed set raises (a crashed
        # executor is failed by the worker, never silently run on the default).
        engine = engine_config(build.engine, settings)
        model = build.model

        # the per-build cost ceiling is a per-build value. A normal build
        # carries none and takes the setting; an eval case's build carries the smaller of that
        # setting and what is left of its run's budget. The executor only reads it -- it is
        # written when the build is created, never by the engine (ADR 0014: the proxy enforces).
        cost_budget_usd = (
            build.cost_budget_usd if build.cost_budget_usd is not None else settings.build_cost_budget_usd
        )

        proxy = ProxyContainer(client=self._docker)
        network = None
        workspace_dir: str | None = None  # staged INPUT (ADR 0032): never published
        output_dir: str | None = None  # the adapter's harvested output: untrusted, never published as is
        publish_dir: str | None = None  # the sanitised copy of it: the only tree publish, image and gate see
        token: str | None = None
        trace = _TraceCapture()  # what the build's trace is assembled from
        temp_dirs: list[str] = []  # every tree a repair attempt makes; removed in `finally`

        try:
            network = self._create_build_network(build)
        except DockerException as exc:
            logger.warning("sandbox executor: network creation failed for build %s: %s", build.id, exc)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary=(
                    f"the sandbox network could not be created ({type(exc).__name__}); "
                    "see the worker log"
                ),
            )

        # the proxy's start is inside the SAME try/finally that tears the network
        # down. It used to be caught on its own for `ProxyStartError` alone, so a proxy
        # that started and then died (`exec_run` on an exited container is a Docker 409;
        # a missing network entry is a `KeyError`) propagated out of `execute` with the
        # network still there. The `finally` below is safe before anything started:
        # `proxy.stop()` is a no-op with no container, and `token` is still None.
        try:
            try:
                proxy.start(
                    real_api_key=engine.api_key,
                    sandbox_network=network,
                    upstream_base_url=engine.upstream_base_url,
                    dialect=engine.dialect,
                    build_id=build.id,
                    image=self._proxy_image,
                )
            except ProxyImageStaleError as exc:
                # the image cannot drain a build's token, so a build's cost could never be
                # recorded: refused BEFORE the agent runs (no money spent on a run that cannot be
                # metered). Not ENGINE_UNREACHABLE: the proxy is not down, the platform's own image
                # is out of date. The text is platform-authored (image name, capability, rebuild
                # command), so it is the detail.
                logger.error("sandbox executor: build %s refused: %s", build.id, exc)
                return ExecutionResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    summary=f"no agent ran: {exc}",
                )
            except Exception as exc:  # noqa: BLE001 - any failure to start means the proxy never became healthy
                logger.warning(
                    "sandbox executor: proxy failed to start for build %s (%s): %s",
                    build.id,
                    type(exc).__name__,
                    exc,
                )
                return ExecutionResult(
                    ok=False,
                    reason=BuildFailureReason.ENGINE_UNREACHABLE,
                    summary="the LLM proxy container failed to start; see the worker log",
                )

            token = proxy.issue_token(
                build.id,
                token_budget=settings.build_token_budget,
                cost_budget_usd=cost_budget_usd,
            )

            workspace_dir = tempfile.mkdtemp(prefix=f"forge-build-{build.id[:8]}-")
            try:
                self._stage_workspace(build, workspace_dir)
            except NoConfirmedRefError as exc:
                logger.warning("sandbox executor: build %s refused at staging: %s", build.id, exc)
                return ExecutionResult(
                    ok=False, reason=BuildFailureReason.INFRA_ERROR, summary=str(exc)
                )

            # With the repair loop on, the run is a sequence of attempts that settles
            # the proxy's usage ONCE, after the last one, and publishes once, for the one that ships.
            # With it off (`BUILD_REPAIR_MAX_ATTEMPTS=0`) the flow below is exactly what it was before.
            if settings.build_repair_max_attempts > 0:
                return self._execute_with_repair(
                    build,
                    proxy=proxy,
                    token=token,
                    network=network,
                    engine=engine,
                    model=model,
                    workspace_dir=workspace_dir,
                    intent_text=intent_text,
                    cost_budget_usd=cost_budget_usd,
                    trace=trace,
                    temp_dirs=temp_dirs,
                )

            result, adapter_error = self._run_agent(
                build,
                proxy=proxy,
                token=token,
                network=network,
                engine=engine,
                model=model,
                workspace_dir=workspace_dir,
                intent_text=intent_text,
                trace=trace,
            )
            # From here the harvested directory is this method's to remove,
            # whichever way it returns (including every early failure below).
            output_dir = result.output_dir

            failure = self._settle_usage(
                build,
                proxy,
                token,
                result=result,
                adapter_error=adapter_error,
                cost_budget_usd=cost_budget_usd,
            )
            if failure is not None:
                return failure

            # Only here -- after every failure above has had its say -- does the
            # harvested tree cross into L2, and only through the sanitising copy
            # (ADR 0032 §Decision 2).
            if output_dir is None:
                logger.warning(
                    "sandbox executor: build %s agent run reported success but produced no harvested directory",
                    build.id,
                )
                return ExecutionResult(
                    ok=False,
                    reason=BuildFailureReason.HARVEST_ERROR,
                    summary="the sandbox run produced no harvested output to publish",
                )
            publish_dir = tempfile.mkdtemp(prefix=f"forge-publish-{build.id[:8]}-")
            refused = self._sanitise_output(build, output_dir, publish_dir)
            if refused is not None:
                return refused

            return self._publish(build, publish_dir, result.summary, workspace_dir, trace=trace)
        finally:
            # every teardown step is its own guarded call. They used to run in a
            # row, so a `revoke()` that raised something other than a Docker error (a
            # docker-py `ReadTimeout`/`ConnectionError` is neither) skipped `proxy.stop()`
            # -- the container holding the real key stayed up -- and the network's and
            # the temp trees' removal with it. The order is unchanged (trace, token, proxy
            # container, network, temp trees); what is new is that no step can end the
            # sequence, and a step that fails is logged with its name and the exception.
            #
            # the trace on EVERY path that reached the agent --
            # success or failure -- and before the token is revoked. It cannot raise, and it
            # touches neither the build's status, reason, detail nor cost.
            if token is not None and trace.agent_ran:
                self._teardown_step(build, "storing the build trace", self._store_trace, build, proxy, token, trace)
            if token is not None:
                self._teardown_step(build, "revoking the proxy token", proxy.revoke, token)
            self._teardown_step(build, "stopping the proxy container", proxy.stop)
            self._teardown_step(build, "removing the sandbox network", self._destroy_network, network)
            # Each is best-effort and logged on failure: a leaked directory must
            # not turn a finished build into an exception, nor hide the others.
            remove_tree(workspace_dir, what="staged workspace")
            remove_tree(output_dir, what="harvested output")
            remove_tree(publish_dir, what="sanitised output")
            # Every tree a repair attempt made (its workspace, harvest, sanitised copy and
            # image context), whichever way the run ended. Empty when the loop is off.
            for path in temp_dirs:
                remove_tree(path, what="repair attempt tree")

    def _run_agent(
        self,
        build: Build,
        *,
        proxy: ProxyContainer,
        token: str,
        network,
        engine,
        model: str | None,
        workspace_dir: str,
        intent_text: str,
        trace: _TraceCapture,
    ) -> tuple[AgentAdapterResult, Exception | None]:
        """One sandbox run of the agent over `workspace_dir` (factored out of `execute`
        unchanged, so a first attempt and a repair attempt run the same way). Returns the adapter's
        result (a failed one standing in for an exception) and the exception it raised, if any: the
        exception is held, not raised, see the comment at the call below."""
        adapter_input = AgentAdapterInput(
            intent_text=intent_text,
            workspace_dir=workspace_dir,
            proxy_base_url=proxy.base_url_for_sandbox,
            proxy_token=token,
            # The composed agent-facing rule set, keyed off
            # this build's type (see app/services/agent_prompt.py). The same for a repair
            # attempt, so the prompt fingerprint is what it was.
            system_prompt=compose_system_prompt(build.type),
            # ADR 0029 §1: the platform-declared tool set, this engine's.
            tools=tuple(declared_agent_tools(engine.engine)),
            engine=engine.engine,
            model=model,
            image=engine.image,
            network=network,
            build_id=build.id,
        )

        trace.agent_ran = True
        # (ADR 0014 §Amendment 2026-10-17, "the engine's result file can never suppress
        # the proxy's record"). Whatever the adapter does, including raising, the step below
        # that reads the proxy's usage, refusals and budget outcome RUNS. The adapter no
        # longer raises over a malformed `result.json` (it reads the file defensively), but it
        # can still raise for reasons of its own (the sandbox runner, staging), and an
        # exception that skipped the proxy read recorded a build that had blown its budget as
        # an executor crash with no cost on it. So an adapter exception is held, not raised:
        # the proxy's record is read and written first, and a refusal, an unmetered response
        # or a spent budget outranks it as the build's reason, as they do any adapter failure.
        adapter_error: Exception | None = None
        try:
            result = self._adapter.run(adapter_input)
        except Exception as exc:  # noqa: BLE001 - held until the proxy's record has been read
            adapter_error = exc
            logger.exception("sandbox executor: the agent adapter raised for build %s", build.id)
            result = AgentAdapterResult(ok=False, reason=BuildFailureReason.INFRA_ERROR)
        else:
            trace.logs = result.logs  # the log's only destination is the trace; never a failure detail
        return result, adapter_error

    def _settle_usage(
        self,
        build: Build,
        proxy: ProxyContainer,
        token: str,
        *,
        result: AgentAdapterResult,
        adapter_error: Exception | None,
        cost_budget_usd: float,
        agent_outcome_counts: bool = True,
        ignore_budget_exceeded: bool = False,
    ) -> ExecutionResult | None:
        """Drains the build's token, reads the proxy's usage and writes it onto the build, then says
        whether the build fails before anything is published: the failed result, or None (the
        defaults are the single-attempt behaviour).

        `agent_outcome_counts=False` and `ignore_budget_exceeded=True` are for ONE case:
        the last repair attempt was discarded and an earlier completed attempt stands. Then that
        attempt's own failure, and a budget it ran out, do not fail the build; a recorded refusal,
        an unmetered response and a drain that failed still do."""
        # ADR 0014 §Amendment 2026-10-01 point 1: the proxy's accounting is
        # the only source of the cost fields. If it cannot be read back the
        # build's spend is unknown -- NULL, not 0 -- and it fails closed.
        judged_result = result if agent_outcome_counts else AgentAdapterResult(ok=True)
        judged_error = adapter_error if agent_outcome_counts else None
        try:
            usage = self._read_proxy_usage(proxy, token)
        except _UsageReadError as exc:
            build.cost_tokens = None
            build.cost_usd = None
            message = (
                "the LLM proxy's usage could not be read back at build end, "
                f"so this build's cost is unknown: {exc}"
            )
            logger.warning(
                "sandbox executor: build %s failed: %s (engine-reported, never recorded: "
                "tokens=%s cost_usd=%s)",
                build.id,
                message,
                result.usage_tokens,
                result.usage_cost_usd,
            )
            # The log keeps `exc`'s full text; the failure summary
            # takes only its `detail` (class name only for an exception that
            # carried infrastructure output).
            cost_note = (
                "the LLM proxy's usage could not be read back at build end, "
                f"so this build's cost is unknown: {exc.detail}"
            )
            # A drain that failed or ran out leaves the cost
            # unknown, but it does not overrule a reason the build already had. A build that hit
            # its wall-clock limit with a call out is a TIMEOUT whose cost could not be read
            # back, not an infrastructure fault; the same for a disk or harvest limit, a recorded
            # refusal or a spent budget. `INFRA_ERROR` is what a build gets when nothing else
            # failed first. (A usage read that failed AFTER a good drain keeps its own
            # precedence: the proxy itself could not be read, which is the infrastructure fault.)
            held = None
            if isinstance(exc, _DrainError):
                record = self._proxy_record_after_failed_drain(proxy, token)
                if record is not None and ignore_budget_exceeded:
                    record = {**record, "budget_exceeded": False}
                held = self._failure_before_publish(
                    build,
                    usage=record,
                    figures_known=False,
                    result=judged_result,
                    adapter_error=judged_error,
                    cost_budget_usd=cost_budget_usd,
                )
            if held is None:
                return ExecutionResult(ok=False, reason=BuildFailureReason.INFRA_ERROR, summary=cost_note)
            return ExecutionResult(
                ok=False, reason=held.reason, summary=self._with_cost_note(held.summary, cost_note)
            )
        self._record_usage(build, usage)
        logger.info(
            "sandbox executor: build %s usage: proxy tokens=%s cost_usd=%s | "
            "engine-reported (diagnostic only, never recorded) tokens=%s cost_usd=%s",
            build.id,
            usage["tokens"],
            usage["cost_usd"],
            result.usage_tokens,
            result.usage_cost_usd,
        )

        return self._failure_before_publish(
            build,
            usage={**usage, "budget_exceeded": False} if ignore_budget_exceeded else usage,
            figures_known=True,
            result=judged_result,
            adapter_error=judged_error,
            cost_budget_usd=cost_budget_usd,
        )

    # --- The in-build repair loop -------------------------------------------------
    #
    # What the order is, and why. With the loop on, an attempt is: agent run, harvest, sanitising
    # copy, the pre-publish checks, an image from a COPY of the tree, the preview, the smoke check.
    # Nothing is pushed until the loop has ended and the proxy's usage has been drained and read ONCE;
    # then the attempt that ships is published (branch, PR) and gated. Publishing is last because the
    # git providers cannot be re-run for one build: `open_pr` is not idempotent (GitHub answers 422 for
    # a second PR from the same branch, the local provider records a second one) and the interface
    # has no way to update or close one (it is kept that thin), so "one branch and PR from the
    # shipped attempt" means one publish. The image is built from a copy because `assemble_image`
    # rewrites its directory (the pinned Dockerfile, emptied ignore files) and the commit must hold
    # the bytes the agent wrote (ADR 0032 §Amendment 2026-10-28); the protected paths are restored in
    # that copy first, as publish restores them, so the image is built from what will be committed.

    def _execute_with_repair(
        self,
        build: Build,
        *,
        proxy: ProxyContainer,
        token: str,
        network,
        engine,
        model: str | None,
        workspace_dir: str,
        intent_text: str,
        cost_budget_usd: float,
        trace: _TraceCapture,
        temp_dirs: list[str],
    ) -> ExecutionResult:
        """The run with the repair loop on. A build that does not ship (any failed result, or an
        exception) leaves no image and no preview of any attempt behind; one that ships keeps the
        shipped attempt's and loses every other attempt's image."""
        run = _RepairRun()
        try:
            return self._run_repair_attempts(
                build,
                run,
                proxy=proxy,
                token=token,
                network=network,
                engine=engine,
                model=model,
                workspace_dir=workspace_dir,
                intent_text=intent_text,
                cost_budget_usd=cost_budget_usd,
                trace=trace,
                temp_dirs=temp_dirs,
            )
        finally:
            if run.shipped is None:
                self._discard_all_attempts(build, run)
            else:
                for attempt in run.attempts:
                    if attempt is not run.shipped:
                        self._remove_attempt_image(build, attempt)

    def _run_repair_attempts(
        self,
        build: Build,
        run: _RepairRun,
        *,
        proxy: ProxyContainer,
        token: str,
        network,
        engine,
        model: str | None,
        workspace_dir: str,
        intent_text: str,
        cost_budget_usd: float,
        trace: _TraceCapture,
        temp_dirs: list[str],
    ) -> ExecutionResult:
        max_repairs = settings.build_repair_max_attempts
        completed: list[_Attempt] = []  # in order; the last is the one the build's fields describe
        pending_failure: ExecutionResult | None = None  # attempt 0 ran but its output cannot ship
        last_result: AgentAdapterResult | None = None
        last_error: Exception | None = None
        last_discarded = False  # the last attempt that ran was a repair attempt that was discarded
        started = 0
        repair_entries: list[dict] = []  # `Build.repair_routes`: one entry per repair attempt started
        index = 0
        seen_tokens, seen_cost = 0, 0.0  # the proxy's cumulative figures at the last read
        token_deltas: list[int] = []
        cost_deltas: list[float] = []
        durations: list[timedelta] = []
        agent_dir = workspace_dir
        prompt = intent_text

        while True:
            began = _utcnow()
            result, adapter_error = self._run_agent(
                build,
                proxy=proxy,
                token=token,
                network=network,
                engine=engine,
                model=model,
                workspace_dir=agent_dir,
                intent_text=prompt,
                trace=trace,
            )
            last_result, last_error = result, adapter_error
            last_discarded = False
            if result.output_dir is not None:
                temp_dirs.append(result.output_dir)

            # A read of the proxy's own record that does NOT close the token (the drain does, and the
            # next attempt needs it open): what a repair decision may rest on, never the agent's
            # self-report (ADR 0014). It can be short only if a call is still alive after the
            # sandbox went; the final drain below settles the authoritative figure, and the proxy's
            # own budget check bounds any overshoot to one call.
            peek = self._peek_usage(proxy, token)
            agent_ok = adapter_error is None and result.ok
            hard = peek is not None and (bool(peek.get("policy_refusals")) or bool(peek.get("unmetered")))
            exceeded = peek is not None and bool(peek.get("budget_exceeded"))

            if hard:
                break  # a recorded refusal or an unmetered response fails the build: settled below
            if index == 0:
                if not agent_ok or exceeded:
                    break  # fails the build below, with the reason it always had
                attempt, failure = self._build_attempt(
                    build, run, index=0, result=result, base_input_dir=workspace_dir, trace=trace, temp_dirs=temp_dirs
                )
                if failure is not None:
                    pending_failure = failure
                    break
                completed.append(attempt)
            else:
                attempt, why = None, None
                if peek is None:
                    why = "the proxy's usage could not be read"
                elif not agent_ok:
                    why = "the agent run did not succeed"
                elif exceeded:
                    why = "the per-build budget was spent"
                else:
                    attempt, failure = self._build_attempt(
                        build,
                        run,
                        index=index,
                        result=result,
                        base_input_dir=workspace_dir,
                        trace=trace,
                        temp_dirs=temp_dirs,
                    )
                    if failure is not None:
                        why = "its output could not be made ready"
                    elif not build.preview_url:
                        why = "its preview could not start"
                if why is not None:
                    logger.warning(
                        "sandbox executor: build %s repair attempt %d discarded (%s); attempt %d stands",
                        build.id,
                        index,
                        why,
                        completed[-1].index,
                    )
                    last_discarded = True
                    self._discard_attempt(build, trace, attempt, completed[-1])
                else:
                    completed.append(attempt)

            if peek is not None:
                token_deltas.append(max(int(peek["tokens"]) - seen_tokens, 0))
                cost_deltas.append(max(float(peek["cost_usd"]) - seen_cost, 0.0))
                seen_tokens, seen_cost = int(peek["tokens"]), float(peek["cost_usd"])
            durations.append(_utcnow() - began)

            # The trigger and the limits.
            current = completed[-1]
            if started >= max_repairs or not has_failing_route(current.smoke_checks):
                break
            if peek is None or exceeded:
                break
            if not self._repair_fits(
                build,
                tokens_used=seen_tokens,
                cost_used=seen_cost,
                token_deltas=token_deltas,
                cost_deltas=cost_deltas,
                durations=durations,
                cost_budget_usd=cost_budget_usd,
            ):
                logger.info("sandbox executor: build %s: no repair attempt, the earlier ones do not fit what is left", build.id)
                break
            routes = failing_routes(current.smoke_checks)
            if not routes:
                break
            try:
                next_dir = tempfile.mkdtemp(prefix=f"forge-repair-{build.id[:8]}-")
                temp_dirs.append(next_dir)
                shutil.copytree(current.publish_dir, next_dir, dirs_exist_ok=True)
                restore_protected_paths(
                    workspace_dir=next_dir,
                    input_dir=workspace_dir,
                    template_skeleton_dir=settings.template_skeleton_dir,
                    protected_paths=settings.protected_paths_list,
                )
            except OSError as exc:
                logger.warning(
                    "sandbox executor: build %s: the repair workspace could not be prepared (%s)",
                    build.id,
                    type(exc).__name__,
                )
                break
            prompt = compose_repair_intent_text(
                intent_text=intent_text,
                failing_routes=[(route["path"], route["status"]) for route in routes],
            )
            agent_dir = next_dir
            started += 1
            index += 1
            # The reason this attempt started, from the SAME `routes` list the prompt was just made
            # from: the count and the list are written together and never disagree, and a
            # discarded or later-failed attempt keeps its entry exactly as the count keeps it. A new
            # list each time: a JSON column does not see an in-place append.
            repair_entries.append({"attempt": started, "routes": copy.deepcopy(routes)})
            build.repair_attempts = started
            build.repair_routes = copy.deepcopy(repair_entries)

        # Once, after the last attempt: the drain closes the token, the usage read is final, and
        # every fail-closed condition is judged on the proxy's whole record.
        failure = self._settle_usage(
            build,
            proxy,
            token,
            result=last_result,
            adapter_error=last_error,
            cost_budget_usd=cost_budget_usd,
            agent_outcome_counts=not last_discarded,
            ignore_budget_exceeded=last_discarded,
        )
        if failure is not None:
            return failure
        if not completed:
            return pending_failure or ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary="no attempt of the build produced output that could be published; see the worker log",
            )
        return self._ship(build, run, completed, base_input_dir=workspace_dir, trace=trace)

    @staticmethod
    def _peek_usage(proxy: ProxyContainer, token: str) -> dict | None:
        """The proxy's validated usage payload read WITHOUT draining, or None when it cannot be read
        (a repair decision is then never made; the final settle reads it again and decides)."""
        try:
            return SandboxBuildExecutor._read_usage_payload(proxy, token)
        except _UsageReadError as exc:
            logger.warning("sandbox executor: the proxy's running usage could not be read: %s", exc)
            return None

    @staticmethod
    def _repair_fits(
        build: Build,
        *,
        tokens_used: int,
        cost_used: float,
        token_deltas: list[int],
        cost_deltas: list[float],
        durations: list[timedelta],
        cost_budget_usd: float,
    ) -> bool:
        """Another attempt may start only if the largest cost (tokens and USD, the two
        limits the proxy enforces) and the longest duration of any earlier attempt still fit in what
        is left of the build's budget and wall-clock. No fixed fractions. A deadline that cannot be
        computed (no `started_at`) is not "fits"."""
        if not token_deltas or not cost_deltas or not durations:
            return False
        if cost_budget_usd - cost_used < max(cost_deltas):
            return False
        if settings.build_token_budget - tokens_used < max(token_deltas):
            return False
        started_at = build.started_at
        if started_at is None:
            return False
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        deadline = started_at + timedelta(minutes=settings.build_max_duration_minutes)
        return deadline - _utcnow() >= max(durations)

    def _build_attempt(
        self,
        build: Build,
        run: _RepairRun,
        *,
        index: int,
        result: AgentAdapterResult,
        base_input_dir: str,
        trace: _TraceCapture,
        temp_dirs: list[str],
    ) -> tuple[_Attempt | None, ExecutionResult | None]:
        """Everything an attempt does short of publishing: sanitise, check, assemble the image,
        start the preview, run the smoke check. Returns the attempt (None if it failed before it had a
        tree) and the failed result, or None when it is ready to ship. The build's measured fields and
        the trace's diff are written as `_publish` writes them; the caller undoes that for a discarded
        repair attempt (`_discard_attempt`)."""
        if result.output_dir is None:
            logger.warning(
                "sandbox executor: build %s agent run reported success but produced no harvested directory",
                build.id,
            )
            return None, ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary="the sandbox run produced no harvested output to publish",
            )
        publish_dir = tempfile.mkdtemp(prefix=f"forge-publish-{build.id[:8]}-")
        temp_dirs.append(publish_dir)
        refused = self._sanitise_output(build, result.output_dir, publish_dir)
        if refused is not None:
            return None, refused
        attempt = _Attempt(index=index, summary=result.summary, publish_dir=publish_dir)

        failure = self._measure_and_check(build, publish_dir, base_input_dir, result.summary, trace)
        if failure is not None:
            return attempt, failure
        attempt.changed_files = copy.deepcopy(build.changed_files)
        attempt.protected_path_drift = copy.deepcopy(build.protected_path_drift)
        attempt.diff_state, attempt.diff = trace.diff_state, copy.deepcopy(trace.diff)

        base_tag = build_tag_for(build.project_id, build.id)
        tag = base_tag if index == 0 else f"{base_tag}-r{index}"
        image_dir = tempfile.mkdtemp(prefix=f"forge-image-{build.id[:8]}-")
        temp_dirs.append(image_dir)
        stage = "image assembly"
        try:
            shutil.copytree(publish_dir, image_dir, dirs_exist_ok=True)
            stage = "restoring the protected paths for the image"
            restore_protected_paths(
                workspace_dir=image_dir,
                input_dir=base_input_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                protected_paths=settings.protected_paths_list,
            )
            stage = "image assembly"
            assemble_image(
                workspace_dir=image_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                tag=tag,
                labels=platform_labels(Role.BUILD_IMAGE, build_id=build.id, project_id=build.project_id),
                docker_client=self._docker,
            )
        except (ImageAssemblyError, OSError) as exc:
            logger.warning("sandbox executor: %s failed for build %s: %s", stage, build.id, exc)
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, result.summary)
            return attempt, ExecutionResult(
                ok=False, reason=BuildFailureReason.HARVEST_ERROR, summary=_stage_failure(stage, exc)
            )
        finally:
            remove_tree(image_dir, what="image context copy")
        attempt.tag = tag
        run.attempts.append(attempt)

        # The preview replaces any earlier attempt's in place (`PreviewManager.start_preview`), so
        # the build's preview fields are cleared first: a failed start must not leave the previous
        # attempt's URL on the row.
        build.image_ref = tag
        build.preview_url = None
        build.preview_expires_at = None
        build.preview_token_hash = None
        build.preview_start_failure = None
        build.smoke_checks = None
        attempt.touched_preview = True
        run.touched_preview = True
        self._start_preview(build)
        self._run_smoke_check(build)
        self._capture_preview(build, attempt)
        return attempt, None

    @staticmethod
    def _capture_preview(build: Build, attempt: _Attempt) -> None:
        attempt.preview_url = build.preview_url
        attempt.preview_expires_at = build.preview_expires_at
        attempt.preview_token_hash = build.preview_token_hash
        attempt.preview_start_failure = build.preview_start_failure
        attempt.smoke_checks = copy.deepcopy(build.smoke_checks)

    @staticmethod
    def _apply_facts(build: Build, trace: _TraceCapture, attempt: _Attempt) -> None:
        """Puts an attempt's measured change facts back on the build and the trace."""
        build.changed_files = copy.deepcopy(attempt.changed_files)
        build.protected_path_drift = copy.deepcopy(attempt.protected_path_drift)
        trace.diff_state, trace.diff = attempt.diff_state, copy.deepcopy(attempt.diff)

    def _reinstate(self, build: Build, trace: _TraceCapture, attempt: _Attempt) -> None:
        """Makes `attempt` the one the build describes and shows again: its facts, its image, and a
        fresh preview started from its image (the one a later attempt started replaced it in place).
        Its smoke fact was measured about this very image, so it is put back as it was. A preview that
        cannot be restarted leaves the build with no preview link and the failure code recorded, as
        any build whose preview did not start."""
        self._apply_facts(build, trace, attempt)
        build.image_ref = attempt.tag
        build.preview_url = None
        build.preview_expires_at = None
        build.preview_token_hash = None
        build.preview_start_failure = None
        self._start_preview(build)
        build.smoke_checks = copy.deepcopy(attempt.smoke_checks)
        self._capture_preview(build, attempt)

    def _discard_attempt(
        self, build: Build, trace: _TraceCapture, attempt: _Attempt | None, previous: _Attempt
    ) -> None:
        """A repair attempt that did not complete: its image goes, and the build goes back to
        describing the previous completed attempt."""
        touched_preview = attempt is not None and attempt.touched_preview
        if touched_preview:
            # Restarting the previous attempt's preview replaces this one's in place, so the image
            # is free to go afterwards (Docker refuses to remove an image a container still uses).
            self._reinstate(build, trace, previous)
        else:
            self._apply_facts(build, trace, previous)
        if attempt is not None:
            self._remove_attempt_image(build, attempt)

    def _remove_attempt_image(self, build: Build, attempt: _Attempt) -> None:
        """Best-effort removal of one attempt's image tag. The retention sweep owns images by their
        platform label; this only keeps a build from holding images nothing will use."""
        tag, attempt.tag = attempt.tag, None  # asked once: a second call has nothing to remove
        if not tag:
            return
        try:
            self._docker.images.remove(tag, force=False)
        except Exception as exc:  # noqa: BLE001 - cleanup never changes a build's outcome
            logger.warning(
                "sandbox executor: build %s attempt %d image %s could not be removed (%s)",
                build.id,
                attempt.index,
                tag,
                type(exc).__name__,
            )

    def _discard_all_attempts(self, build: Build, run: _RepairRun) -> None:
        """The build ends without shipping: no attempt's preview or image stays, and the row carries
        no preview or image reference to something that is gone."""
        if run.touched_preview:
            self._teardown_step(build, "stopping the preview of an attempt", self._preview_manager.stop_preview, build.id)
        for attempt in run.attempts:
            self._remove_attempt_image(build, attempt)
        if run.attempts:
            build.image_ref = None
            build.preview_url = None
            build.preview_expires_at = None
            build.preview_token_hash = None
            build.smoke_checks = None

    def _ship(
        self, build: Build, run: _RepairRun, completed: list[_Attempt], *, base_input_dir: str, trace: _TraceCapture
    ) -> ExecutionResult:
        """Publishes the last completed attempt, then runs the policy gate on it. An attempt whose
        publish fails is discarded and the one before it is tried (a failure to publish
        discards an attempt); the build fails only when none can be published."""
        live = completed[-1]  # the build's fields and the live preview are its
        first_failure: ExecutionResult | None = None
        for candidate in reversed(completed):
            if candidate is not live:
                self._reinstate(build, trace, candidate)
                live = candidate
            failure = self._publish_attempt(build, candidate, base_input_dir)
            if failure is None:
                run.shipped = candidate
                break
            first_failure = first_failure or failure
        else:
            return first_failure  # type: ignore[return-value] - `completed` is not empty
        gate_failure = self._run_policy_gate(build, run.shipped.publish_dir)
        if gate_failure is not None:
            return gate_failure
        return ExecutionResult(ok=True, agent_summary=run.shipped.summary)

    def _publish_attempt(self, build: Build, attempt: _Attempt, base_input_dir: str) -> ExecutionResult | None:
        """The publish step proper (branch and PR) for the attempt that ships: what `_publish` does
        between the provider and the diff reference, with the image already built. None on success."""
        try:
            provider = self._git_provider or create_provider(settings)
        except Exception as exc:  # noqa: BLE001 - missing/invalid git provider config
            logger.warning("sandbox executor: git provider unavailable for build %s: %s", build.id, exc)
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, attempt.summary)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=_stage_failure("git provider setup", exc),
            )
        try:
            project = self._project_for(build)
            repo = self._ensure_repo(provider, project)
            branch = f"forge-build-{build.id}"
            base_branch = self._base_branch_for(build, project, repo)
            publish_result = publish_build(
                provider=provider,
                repo=repo,
                workspace_dir=attempt.publish_dir,
                input_dir=base_input_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                protected_paths=settings.protected_paths_list,
                branch=branch,
                base_branch=base_branch,
                pr_title=f"Forge build {build.id}",
                pr_body=attempt.summary or "(no agent summary)",
            )
        except (GitProviderError, PublishError) as exc:
            logger.warning("sandbox executor: publish failed for build %s attempt %d: %s", build.id, attempt.index, exc)
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, attempt.summary)
            return ExecutionResult(
                ok=False, reason=BuildFailureReason.HARVEST_ERROR, summary=_stage_failure("publish", exc)
            )
        build.diff_ref = publish_result.diff_ref
        # Stored as restore reported it (the value measured before is the same judgement).
        build.protected_path_drift = list(publish_result.drift)
        return None

    def _create_build_network(self, build: Build):
        """The build's own `internal=True` network (ADR 0003 invariant 3), labelled
        with the instance, the `build-network` role and the build and project ids, so the
        reconciler can remove it when its build is no longer RUNNING."""
        return self._docker.networks.create(
            f"{settings.sandbox_network_name}-{build.id}-{uuid.uuid4().hex[:8]}",
            driver="bridge",
            internal=True,
            labels=platform_labels(
                Role.BUILD_NETWORK, build_id=build.id, project_id=build.project_id
            ),
        )

    @staticmethod
    def _teardown_step(build: Build, step: str, fn, *args) -> None:
        """runs one teardown step so that it cannot stop the next. Any
        `Exception` is logged (the step, the exception class and its text -- Docker
        and `requests` errors name an endpoint, never the proxy token or secret) and
        swallowed; `KeyboardInterrupt`/`SystemExit` still propagate."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning(
                "sandbox executor: build %s teardown step %r failed (%s): %s; continuing with the rest",
                build.id,
                step,
                type(exc).__name__,
                exc,
            )

    def _sanitise_output(self, build: Build, output_dir: str, publish_dir: str) -> ExecutionResult | None:
        """ADR 0032 §Decision 2: copy the harvested tree into `publish_dir`
        (created by the caller) by allowlist. Returns None on success, or the
        failed result. The detail is platform text: kinds and counts for a
        refused tree, stage + exception class for anything else -- never
        an entry's name or an exception's text."""
        try:
            sanitise_harvest(output_dir, publish_dir)
        except UnsafeHarvestEntry as exc:
            logger.warning("sandbox executor: build %s output refused: %s", build.id, exc)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=(
                    "the agent's output contains entries that cannot be published "
                    f"({describe_counts(exc.counts)}); links and special files are not allowed"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - any copy failure is a harvest failure, never silent
            logger.warning("sandbox executor: build %s output could not be sanitised: %s", build.id, exc)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=_stage_failure("sanitising the output", exc),
            )
        return None

    def _stage_workspace(self, build: Build, workspace_dir: str) -> None:
        """Run flow step 2 and modify-type rules: seed
        the pre-agent workspace from the base ref. Create-type: the
        template skeleton's app/ shape, copied in here
        (excluding the protected manifests themselves, which `publish.py`'s
        restore step re-pins from the same source of truth regardless).
        Modify-type: the project's current **confirmed ref** when
        one exists — cloned from the project repo at that exact ref (the base is
        the current confirmed ref, never an
        unmerged previous build branch). A project that has never been
        confirmed has nothing to modify: `NoConfirmedRefError`, never an empty workspace.
        """
        if build.type != "create":
            self._stage_modify_workspace(build, workspace_dir)
            return
        skeleton = settings.template_skeleton_dir
        app_src = os.path.join(skeleton, "app")
        if os.path.isdir(app_src):
            # ADR 0032 §Amendment 2026-10-05: no compiled bytecode from the host's copy of
            # the template (the suite imports it) -- it would be in every first commit.
            shutil.copytree(
                app_src,
                os.path.join(workspace_dir, "app"),
                dirs_exist_ok=True,
                ignore=lambda _dir, names: [n for n in names if is_bytecode_name(n)],
            )

    def _stage_modify_workspace(self, build: Build, workspace_dir: str) -> None:
        """base a modify-type build on `Project.confirmed_ref` (set
        by the deploy slice's merge step). Failures here propagate — a
        modify build that *should* base on confirmed code but can't must
        fail (INFRA_ERROR), never silently run against an empty workspace.

        with no confirmed ref this used to return, leaving the workspace
        empty; the agent then rewrote the app from nothing and the build still
        ended SUCCEEDED. It raises `NoConfirmedRefError` now (`execute` fails
        the build with `NO_CONFIRMED_REF_BUILD_DETAIL`)."""
        session = object_session(build)
        project = session.get(Project, build.project_id) if session is not None else None
        if project is None:
            raise RuntimeError(f"modify build {build.id} has no project to base its workspace on")
        if not project.confirmed_ref:
            raise NoConfirmedRefError(NO_CONFIRMED_REF_BUILD_DETAIL)

        provider = self._git_provider or create_provider(settings)
        repo = provider.create_repo(name=project.id)  # create-or-return lookup
        self._clone_at_ref(repo.clone_url, project.confirmed_ref, workspace_dir)

    @staticmethod
    def _clone_at_ref(clone_url: str, ref: str, workspace_dir: str) -> None:
        """`git clone` + detached checkout at the confirmed ref, then drop
        `.git` (publish re-inits a fresh repo from the harvested result).
        The clone URL may embed the provider token — any git error output is
        scrubbed of it before being raised.

        **bounded.** This used to be a plain `subprocess.run`, with no
        timeout and with git free to ask a terminal for a password: a git host that
        accepts the connection and then says nothing held the worker -- one build at
        a time -- forever. It now goes through `publish.run_git_process` (the
        bounded runner: output to temp files, git killed and reaped at
        `publish.GIT_TIMEOUT_SECONDS`, which a plain `subprocess.run(timeout=)` does
        not manage on Windows against a hung `git-remote-https`) with
        `GIT_TERMINAL_PROMPT=0` (`publish.git_env`). An expiry raises the same
        `RuntimeError` a failed clone does, so it reaches the worker as the same
        reason (`INFRA_ERROR`, "the build executor raised RuntimeError"), and it
        names the step and the limit, never the URL."""
        import subprocess

        def _run(args: list[str], cwd: str | None) -> None:
            try:
                run_git_process(args, cwd=cwd, env=git_env(), check=True)
            except subprocess.CalledProcessError as exc:
                output = (exc.stderr or exc.stdout or "").replace(clone_url, "<redacted-remote>")
                raise RuntimeError(
                    f"staging workspace at confirmed ref {ref} failed (git {args[1]}): {output}"
                ) from None  # never chain: the original exception carries the token-bearing argv
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"staging workspace at confirmed ref {ref} failed (git {args[1]}): "
                    f"timed out after {exc.timeout:g}s"
                ) from None  # never chain: the argv carries the (token-bearing) clone URL

        _run(["git", "clone", "--no-checkout", clone_url, workspace_dir], cwd=None)
        _run(["git", "checkout", "--detach", ref], cwd=workspace_dir)

        def _clear_readonly(func, path, _exc_info):
            # git marks object files read-only (notably on Windows), which
            # makes a plain rmtree skip them; clear the bit and retry.
            os.chmod(path, 0o700)
            func(path)

        shutil.rmtree(os.path.join(workspace_dir, ".git"), onerror=_clear_readonly)

    def _intent_text_for(self, build: Build) -> str:
        """The engine's user prompt (adapter
        contract), composed from the change request that created this build.

        The link is `ChangeRequest.build_id` -- the reverse of `Build`, which
        has no pointer back. That column carries a unique constraint
        (`uq_change_requests_build_id`), so "several linked requests" can
        no longer be stored; the `len(linked) > 1` branch stays as defence in
        depth rather than picking a row if the constraint is ever dropped.
        Zero or several linked requests, or a blank one, raises
        `BuildRequestError` (three distinguishable messages).

        A modify-type build additionally gets the project's originating
        request -- its earliest change request, `created_at` then `id` -- and
        nothing between the two (the confirmed ref the build starts from
        already embodies whatever was applied)."""
        session = object_session(build)
        if session is None:
            raise BuildRequestError(
                f"build {build.id} is not attached to a DB session, so its request cannot be read"
            )

        linked = list(
            session.execute(
                select(ChangeRequest).where(ChangeRequest.build_id == build.id)
            ).scalars()
        )
        if not linked:
            raise BuildRequestError(
                f"build {build.id} has no linked change request, so there is no request to build"
            )
        if len(linked) > 1:
            raise BuildRequestError(
                f"build {build.id} has {len(linked)} linked change requests (expected exactly one), "
                "so it is ambiguous which request to build"
            )
        current = linked[0]
        if not (current.nl_request or "").strip():
            raise BuildRequestError(
                f"build {build.id}'s change request {current.id} has a blank request text"
            )

        project = session.get(Project, build.project_id)
        if project is None:
            raise BuildRequestError(f"build {build.id}'s project {build.project_id} was not found")

        originating_text: str | None = None
        if build.type == "modify":
            originating = session.execute(
                select(ChangeRequest)
                .where(ChangeRequest.project_id == build.project_id)
                .order_by(ChangeRequest.created_at, ChangeRequest.id)
                .limit(1)
            ).scalar_one_or_none()
            # The originating request being the current one -> it appears once.
            if (
                originating is not None
                and originating.id != current.id
                and (originating.nl_request or "").strip()
            ):
                originating_text = originating.nl_request

        return compose_intent_text(
            build_type=build.type,
            project_name=project.name,
            request_text=current.nl_request,
            originating_request_text=originating_text,
        )

    def _store_trace(self, build: Build, proxy: ProxyContainer, token: str, trace: _TraceCapture) -> None:
        """reads the proxy's call record and stores the build's trace.
        Observability, not a guardrail: ANY failure -- the read, the parse, the insert -- is
        logged and swallowed, and the build carries no trace (or one that says its call
        record is unavailable). The two halves fail independently of each other, and of the
        usage read, which has already happened by the time this runs (or has failed on its
        own terms)."""
        try:
            try:
                calls = normalise_calls(proxy.trace(token))
            except Exception as exc:  # noqa: BLE001 - the call record is best-effort
                logger.warning(
                    "sandbox executor: build %s's call record could not be read back (%s)",
                    build.id,
                    type(exc).__name__,
                )
                calls = None
            session = object_session(build)
            if session is None:
                logger.warning("sandbox executor: build %s has no DB session, so no trace is stored", build.id)
                return
            store_trace(
                session,
                build,
                calls=calls,
                diff_state=trace.diff_state,
                diff=trace.diff,
                logs=trace.logs,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001 - a trace failure never changes the build's outcome
            logger.warning("sandbox executor: build %s's trace could not be stored (%s)", build.id, type(exc).__name__)

    def _measure_and_check(
        self,
        build: Build,
        workspace_dir: str,
        input_dir: str,
        summary: str | None,
        trace: _TraceCapture | None,
    ) -> ExecutionResult | None:
        """Everything `_publish` decides about a sanitised tree BEFORE any publish side effect:
        measure the agent's change set and diff (recorded on the build and the trace whichever way
        this returns), refuse a schema change on a modify build, and check the template's entrypoint.
        None when the tree may be published, else the failed result. It touches no git provider, no
        Docker object and no preview (factored out of `_publish` unchanged so the repair
        path can run the same checks on every attempt)."""
        # Measure the
        # agent's change set NOW -- the input tree against the sanitised tree, BEFORE
        # protected-path restore (inside `publish_build`) drops the agent's edits to a
        # protected file, so an attempted change to one is part of it -- and BEFORE the
        # schema refusal, so a refused modify build carries what it tried to change. Both
        # are recorded on the build whichever way this method then returns. The paths are
        # agent-chosen: they are stored as data and never interpolated into any
        # platform-authored text (failure detail, logs, the commit message).
        try:
            build.changed_files = measure_changes(input_dir=input_dir, output_dir=workspace_dir).to_json()
            build.protected_path_drift = protected_drift(
                workspace_dir=workspace_dir,
                input_dir=input_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                protected_paths=settings.protected_paths_list,
            )
        except OSError as exc:
            build.changed_files = None
            build.protected_path_drift = None
            logger.warning("sandbox executor: measuring the changes failed for build %s: %s", build.id, exc)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=_stage_failure("measuring the agent's changes", exc),
            )

        # the unified diff, from the SAME two trees, at the same
        # point -- before the protected-path restore and before the schema refusal below, so a
        # refused build shows what it tried to change. Display-only: a failure here is logged and
        # recorded as "unavailable", never a failed build.
        if trace is not None:
            try:
                trace.diff = compute_diff(input_dir=input_dir, output_dir=workspace_dir)
                trace.diff_state = DIFF_COMPUTED
            except Exception as exc:  # noqa: BLE001 - the diff is observability
                logger.warning(
                    "sandbox executor: the diff could not be computed for build %s (%s)",
                    build.id,
                    type(exc).__name__,
                )
                trace.diff, trace.diff_state = None, DIFF_UNAVAILABLE

        # ADR 0009 §Amendment 2026-10-05: on a modify-type build a change to a
        # schema path refuses the build, BEFORE any publish side effect -- the git
        # provider is not even constructed, so no repository is created and nothing is
        # pushed, assembled or gated. Create-type builds skip the check (they may add
        # forward-only migrations, ADR 0023). Any non-create build is treated as modify,
        # as staging does.
        if build.type != "create":
            try:
                refuse_schema_changes(
                    input_dir=input_dir,
                    output_dir=workspace_dir,
                    entries=settings.protected_schema_paths_list,
                )
            except SchemaChangeRefused as exc:
                # Platform text only: a count and the declared prefixes. The
                # file names are L3-authored and are not logged either.
                logger.warning("sandbox executor: build %s refused: %s", build.id, exc)
                logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, summary)
                return ExecutionResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary=str(exc))
            except OSError as exc:
                logger.warning("sandbox executor: schema check failed for build %s: %s", build.id, exc)
                return ExecutionResult(
                    ok=False,
                    reason=BuildFailureReason.HARVEST_ERROR,
                    summary=_stage_failure("checking the schema paths", exc),
                )

        # a build whose output has no
        # entrypoint for the template's service fails here, BEFORE the git provider is even
        # constructed -- nothing is pushed, no PR opened, no image assembled, no preview
        # started, so nothing is left for the TTL sweep to find. It is a build-outcome check,
        # not a policy check. The required paths come from the template's own pinned Dockerfile.
        try:
            absent = missing_entrypoint(workspace_dir, settings.template_skeleton_dir)
        except EntrypointUndeterminable as exc:
            # The platform's own template does not say what its entrypoint is: not the
            # requester's build at fault, and not a reason to let every build through unchecked.
            logger.warning("sandbox executor: build %s: %s", build.id, exc)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary=f"the build was not checked for an app entrypoint: {exc}; see the worker log",
            )
        if absent is not None:
            logger.warning("sandbox executor: build %s output has no app entrypoint (wanted one of %s)", build.id, absent)
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, summary)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.MISSING_ENTRYPOINT,
                summary=_missing_entrypoint_detail(absent),
            )
        return None

    def _publish(
        self,
        build: Build,
        workspace_dir: str,
        summary: str | None,
        input_dir: str,
        trace: _TraceCapture | None = None,
    ) -> ExecutionResult:
        """Publish the build's
        output (branch + PR via the git provider), assemble the OCI image and
        run the policy gate. Any failure here fails the build with
        HARVEST_ERROR ("diff extraction or image
        assembly failed") -- never silently.

        `workspace_dir` is the SANITISED copy of the agent's harvested output
        (ADR 0032), the only tree host git, `docker build` and the policy
        checks are allowed to run in. It is not the staged input directory
        (the template or confirmed ref the agent started from) and not the
        harvested directory itself, whose bytes are untrusted. `execute`
        removes it afterwards.

        `input_dir` IS the staged input directory: the baseline that
        protected-path drift is measured against. It outlives this call --
        `execute`'s `finally` is what removes it."""
        failure = self._measure_and_check(build, workspace_dir, input_dir, summary, trace)
        if failure is not None:
            return failure

        try:
            provider = self._git_provider or create_provider(settings)
        except Exception as exc:  # noqa: BLE001 - missing/invalid git provider config
            logger.warning("sandbox executor: git provider unavailable for build %s: %s", build.id, exc)
            # the agent's `summary` is L3-authored and stays out of a
            # failure (it is still logged); the detail is a stage + class only.
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, summary)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=_stage_failure("git provider setup", exc),
            )

        stage = "publish"
        try:
            project = self._project_for(build)
            repo = self._ensure_repo(provider, project)

            branch = f"forge-build-{build.id}"
            base_branch = self._base_branch_for(build, project, repo)

            publish_result = publish_build(
                provider=provider,
                repo=repo,
                workspace_dir=workspace_dir,
                input_dir=input_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                protected_paths=settings.protected_paths_list,
                branch=branch,
                base_branch=base_branch,
                pr_title=f"Forge build {build.id}",
                pr_body=summary or "(no agent summary)",
            )

            stage = "image assembly"
            image_tag = build_tag_for(build.project_id, build.id)
            assemble_image(
                workspace_dir=workspace_dir,
                template_skeleton_dir=settings.template_skeleton_dir,
                tag=image_tag,
                labels=platform_labels(
                    Role.BUILD_IMAGE, build_id=build.id, project_id=build.project_id
                ),
                docker_client=self._docker,
            )
        except (GitProviderError, PublishError, ImageAssemblyError) as exc:
            logger.warning("sandbox executor: %s failed for build %s: %s", stage, build.id, exc)
            logger.info("sandbox executor: build %s agent summary (not persisted): %s", build.id, summary)
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.HARVEST_ERROR,
                summary=_stage_failure(stage, exc),
            )

        build.diff_ref = publish_result.diff_ref
        build.image_ref = image_tag
        # Stored as restore reported it (the value measured above is the same judgement,
        # taken before anything was restored).
        build.protected_path_drift = list(publish_result.drift)

        # the drift is a measured fact on the build now and a failed
        # preview start shows as no preview link, so neither is appended to the agent's
        # text any more -- it is handed on exactly as the adapter returned it.
        self._start_preview(build)
        self._run_smoke_check(build)
        gate_failure = self._run_policy_gate(build, workspace_dir)
        if gate_failure is not None:
            return gate_failure

        return ExecutionResult(ok=True, agent_summary=summary)

    def _run_policy_gate(self, build: Build, workspace_dir: str) -> ExecutionResult | None:
        """runs right after publish/preview so a
        flagged verdict never blocks or delays preview issuance and never
        changes the build's own status (a flagged verdict is not a failed
        build — the build already returned ok=True by the time this runs).

        **the gate fails closed as a
        whole.** The checks, the persistence of their verdict and the approval routing
        run in ONE savepoint (`session.begin_nested()`, as `store_trace` does). If anything
        in it raises -- a check's plumbing, a flush the database refuses, the routing --
        the savepoint is rolled back, so no half-written verdict or request is committed
        with the build and the session is usable again (a failed flush leaves the
        transaction unusable until something rolls back, and the commit that follows used
        to raise out of the worker loop). A platform-authored GATE-ERROR verdict at the
        highest tier is then recorded and routed to human approval through the same
        `create_approval_request_for_verdict` path, in a savepoint of its own: the build is
        undeployable until a person has looked at it, and the timeline says why. It used
        to be swallowed, which left a `SUCCEEDED` build with a live preview, no verdict and
        no approval request, and no stated reason.

        Only if that recording fails too (the database itself is failing) is the build
        failed (`INFRA_ERROR`, platform-authored detail): a build with no verdict, no
        request and no reason is the state this exists to prevent, and the same database
        failure would fail the commit that follows anyway.

        Returns `None` normally. What fails the build here is that last resort and an
        ambiguous requester (see `_route_to_approval`), returned as an `ExecutionResult` for
        `_publish` to hand back."""
        session = object_session(build)
        if session is None:
            logger.warning("sandbox executor: policy gate skipped for build %s (no DB session)", build.id)
            return None
        # What failed, as `record_gate_error_verdict` words it. Set before each step so the
        # `except` below knows which one it is looking at.
        stage = GATE_STAGE_CHECKS
        ambiguous: AmbiguousRequesterError | None
        try:
            with session.begin_nested():
                verdict = run_and_persist_policy_gate(
                    session,
                    build,
                    workspace_dir=workspace_dir,
                    template_skeleton_dir=settings.template_skeleton_dir,
                    protected_paths=settings.protected_paths_list,
                    medium_tier_checks=settings.policy_medium_tier_checks_list,
                    high_tier_checks=settings.policy_high_tier_checks_list,
                    # No guaranteed network access from the platform process in
                    # this environment/spec's dependency-vulnerability check;
                    # wiring a real live-feed lookup is future work — see
                    # policy_checks.py's module docstring. Fixed False for now,
                    # not read from Settings, since there is no live-feed client
                    # implemented yet to make this True mean anything.
                    network_available=False,
                )
                stage = GATE_STAGE_ROUTING
                ambiguous = self._route_to_approval(session, build, verdict)
        except Exception as exc:  # noqa: BLE001 - the gate fails closed as a whole
            logger.exception(
                "sandbox executor: policy gate failed for build %s (%s); recording a gate-error verdict",
                build.id,
                stage,
            )
            try:
                with session.begin_nested():
                    verdict = record_gate_error_verdict(session, build, stage=stage, error=exc)
                    ambiguous = self._route_to_approval(session, build, verdict)
            except Exception as recording_exc:  # noqa: BLE001 - the last resort, see the docstring
                logger.exception(
                    "sandbox executor: the gate-error verdict for build %s could not be recorded", build.id
                )
                return ExecutionResult(
                    ok=False,
                    reason=BuildFailureReason.INFRA_ERROR,
                    summary=(
                        f"the policy gate failed ({type(exc).__name__}) and its failure could not be "
                        f"recorded ({type(recording_exc).__name__}), so the build has no policy verdict; "
                        "see the worker log"
                    ),
                )

        if ambiguous is not None:
            logger.error(
                "sandbox executor: build %s has an ambiguous requester, no approval request created: %s",
                build.id,
                ambiguous,
            )
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary=(
                    f"no approval request could be created: build {ambiguous.build_id} has "
                    f"{ambiguous.count} change requests (expected at most one), so its requester "
                    "is ambiguous and the self-approval ban cannot be applied; "
                    "an operator must resolve the duplicate request"
                ),
            )
        return None

    def _route_to_approval(
        self, session, build: Build, verdict: PolicyVerdict
    ) -> AmbiguousRequesterError | None:
        """every policy verdict creates exactly
        one ApprovalRequest. Runs inside `_run_policy_gate`'s savepoint, so anything it raises
        is the gate failing and is answered there, not swallowed here.

        an eval build is gated (the
        verdict is recorded and shown with its case) but never enters human approval -- no
        ApprovalRequest, so it never reaches an approver's queue and its project is never moved
        to AWAITING_APPROVAL. Deploying it is refused anyway.

        The one thing that is RETURNED, not raised, is an ambiguous requester: that is
        not the gate failing, it is the self-approval ban having no single identity to forbid.
        No ApprovalRequest is created -- a request snapshotting a guessed requester would let
        the others approve their own build -- and the build fails closed. It is returned so the
        savepoint still commits the verdict (the gate did run), and so the caller's generic
        handler cannot absorb it."""
        if self._skips_human_approval(session, build):
            logger.info("sandbox executor: build %s is an eval build; no approval request created", build.id)
            return None
        project = self._project_for(build)
        try:
            requester_id = self._requester_for(build, project)
        except AmbiguousRequesterError as exc:
            return exc
        create_approval_request_for_verdict(
            session,
            build=build,
            verdict=verdict,
            project_id=project.id,
            requester_id=requester_id,
        )
        return None

    @staticmethod
    def _skips_human_approval(session, build: Build) -> bool:
        """True for a build of an eval project: the one named predicate for the rule."""
        project = session.get(Project, build.project_id)
        return project is not None and project.kind == ProjectKind.EVAL

    def _requester_for(self, build: Build, project: Project) -> str:
        """The ApprovalRequest's `requester_id` — self-approval is forbidden
        for this identity.

        this used to return `project.owner_id` outright, because
        `Build` carries no pointer to who submitted it. It does not need one
        — `ChangeRequest.build_id` is the reverse link — and the stand-in was
        a real defect once requester and owner diverge, so the resolution
        rule now lives in `services/approval.resolve_requester_for_build`
, which falls back to the owner
        only when no `ChangeRequest` references the build."""
        session = object_session(build)
        if session is None:
            return project.owner_id
        return resolve_requester_for_build(
            session, build_id=build.id, project_owner_id=project.owner_id
        )

    def _start_preview(self, build: Build) -> None:
        """start the TTL
        preview container from the just-assembled image and persist the
        signed preview URL/expiry/token-hash onto the build. A preview
        start failure does not fail the overall build — the code change,
        PR, and image are already published successfully at this point
        (the preview container is a post-success
        convenience, not a gate); it shows as a build with no preview link
        (no longer a sentence appended to the agent's summary) and is
        logged (never silent about it)."""
        try:
            issued = self._preview_manager.start_preview(project_id=build.project_id, build=build)
        except PreviewStartError as exc:
            logger.warning("sandbox executor: preview start failed for build %s: %s", build.id, exc)
            build.preview_start_failure = exc.code
            return
        except PreviewSigningSecretMissingError as exc:
            # A configuration fault, not an access decision; it used to escape and fail the
            # build over a convenience. Recorded by code; the text stays in the log.
            logger.warning("sandbox executor: preview start failed for build %s: %s", build.id, exc)
            build.preview_start_failure = PreviewStartFailure.SIGNING_SECRET_MISSING
            return
        except Exception as exc:  # noqa: BLE001 - a preview is never a gate
            # Only the type is logged here: the text of an unexpected error is not ours to vouch for.
            logger.warning(
                "sandbox executor: preview start failed unexpectedly for build %s (%s)",
                build.id,
                type(exc).__name__,
            )
            build.preview_start_failure = PreviewStartFailure.UNEXPECTED
            return

        build.preview_start_failure = None
        build.preview_url = issued.url
        build.preview_expires_at = issued.expires_at
        build.preview_token_hash = issued.token_hash

        # "issuance is
        # audit-logged" — the token itself is never included (never log
        # secrets/tokens in plaintext).
        session = object_session(build)
        if session is not None:
            write_audit_log(
                session,
                actor_id=None,
                action="preview.issued",
                target=f"build:{build.id}",
                metadata={"project_id": build.project_id, "expires_at": issued.expires_at.isoformat()},
            )

    def _default_smoke_prober(self, build: Build) -> Prober:
        return PreviewSmokeProber(
            build_id=build.id,
            preview_url=build.preview_url,
            token_hash=build.preview_token_hash,
            preview_manager=self._preview_manager,
        )

    def _run_smoke_check(self, build: Build) -> None:
        """after the preview is live, call its own
        parameterless GET routes and store what answered (`services/preview_smoke.py`).

        **Informational: nothing here can fail the build or change what the gate does.** The whole
        body, including building the prober and writing the fact, is inside one `except Exception`
        that leaves `Build.smoke_checks` NULL ("not measured"); it returns nothing, so no caller can
        branch on it. A build whose preview did not start (no URL) is not probed at all. The write is
        flushed in a savepoint of its own so a database refusal is rolled back here rather than
        surfacing at the policy gate's flush or the worker's commit. Only the exception's type is
        logged: its text may carry what a generated service printed."""
        if not build.preview_url:
            return
        try:
            fact = run_smoke_check(self._smoke_prober_factory(build))
            if fact is None:
                return
            session = object_session(build)
            if session is None:
                build.smoke_checks = fact
                return
            with session.begin_nested():
                build.smoke_checks = fact
                session.flush()
        except Exception as exc:  # noqa: BLE001 - informational: any failure means "not measured"
            logger.warning(
                "sandbox executor: smoke check of build %s not measured (%s)", build.id, type(exc).__name__
            )
            try:
                build.smoke_checks = None
            except Exception:  # noqa: BLE001 - nothing further may escape either
                pass

    def _project_for(self, build: Build) -> Project:
        session = object_session(build)
        if session is None:
            raise PublishError(f"build {build.id} is not attached to a DB session")
        project = session.get(Project, build.project_id)
        if project is None:
            raise PublishError(f"project {build.project_id} not found for build {build.id}")
        return project

    def _ensure_repo(self, provider: GitProvider, project: Project) -> RepoRef:
        """Repo per project: create it (idempotently — the
        provider's create_repo is create-or-return) on first use and record
        `Project.repo_url`, so a later build for the same project reuses
        the same repo rather than re-triggering creation each time."""
        repo = provider.create_repo(name=project.id)
        if project.repo_url != repo.full_name:
            project.repo_url = repo.full_name
        return repo

    def _base_branch_for(self, build: Build, project: Project, repo: RepoRef) -> str:
        # `Project.confirmed_ref` now advances on confirm (the deploy
        # slice's merge step merges every confirmed PR into the repo's
        # default branch), so the default branch head IS the confirmed ref
        # on the happy path — PRs keep targeting it. The *workspace* base
        # for modify-type builds is the confirmed ref itself
        # (`_stage_modify_workspace`), never an unmerged build branch
        #.
        return repo.default_branch

    @staticmethod
    def _read_proxy_usage(proxy: ProxyContainer, token: str) -> dict:
        """The proxy's accounting, and nothing else, is the build's usage (ADR
        0014 §Amendment 2026-10-01 point 1). The engine's self-reported usage
        is deliberately not a parameter: it is written inside L3 and is
        attacker-controllable in both directions.

        Raises `_UsageReadError` when the proxy cannot be asked, or answers
        without a readable `tokens` / `cost_usd` -- never a zero stand-in, which
        would be indistinguishable from a build that measurably spent nothing.
        The returned dict has an int `tokens` and a float `cost_usd`.

        FIRST the proxy is told to drain the token (it stops
        admitting requests and waits for the ones alive to record), because a call can still be
        running on the proxy after the sandbox is gone and would add its spend AFTER a read made
        now. A drain that cannot be asked, or that ran out of time with a call still alive, is a
        read-back failure (`_DrainError`): the figure would be short, so it is not stored."""
        SandboxBuildExecutor._drain_proxy(proxy, token)
        return SandboxBuildExecutor._read_usage_payload(proxy, token)

    @staticmethod
    def _drain_proxy(proxy: ProxyContainer, token: str) -> None:
        """drains `token` in steps of at most `PROXY_DRAIN_STEP_SECONDS` until the proxy says
        nothing of it is left, or `PROXY_DRAIN_TOTAL_SECONDS` of waiting has been asked for. Raises
        `_DrainError` when the proxy cannot be asked, answers something other than a boolean, or still
        has a call alive when the total runs out; the usage is never read in those cases.

        A `for` over a computed number of steps, not a loop on the clock: the number of drain calls is
        bounded by the two constants whatever the proxy answers, and each step's wait is what the
        proxy itself spends (`drained: false` is returned only after that wait)."""
        step = PROXY_DRAIN_STEP_SECONDS
        total = PROXY_DRAIN_TOTAL_SECONDS
        steps = max(1, math.ceil(total / step - 1e-9))
        for index in range(steps):
            wait = min(step, total - index * step)
            try:
                drained = proxy.drain(token, timeout_seconds=wait)
            except Exception as exc:  # noqa: BLE001 - any failure to drain means the read cannot be trusted
                raise _DrainError(
                    f"the proxy could not be drained before the usage read: "
                    f"{type(exc).__name__}: {str(exc).replace(token, '<token>')[:200]}",
                    # the text above can be raw docker-exec output; it stays in the log.
                    detail=(
                        f"the proxy could not be drained before the usage read ({type(exc).__name__}); "
                        "see the worker log"
                    ),
                ) from None
            if drained is True:
                return
            if drained is not False:
                # Neither answer: not "still alive", so not a reason to wait on. The real
                # `ProxyContainer.drain` never returns this (it raises); this is the second layer.
                raise _DrainError(
                    f"the proxy's drain answered {type(drained).__name__}, not a boolean",
                    detail="the proxy's answer to the drain was not readable; see the worker log",
                )
        raise _DrainError(
            f"a call was still in flight on the proxy after waiting {total:g} s, "
            "so the usage read would not include it",
            detail=(
                f"a call was still in flight on the proxy after waiting {total:g} s, "
                "so its spend could not be included"
            ),
        )

    @staticmethod
    def _read_usage_payload(proxy: ProxyContainer, token: str) -> dict:
        """The usage read proper (after the drain): the validated payload, or `_UsageReadError`."""
        try:
            usage = proxy.usage(token)
        except Exception as exc:  # noqa: BLE001 - any failure to ask is a read-back failure
            raise _UsageReadError(
                f"{type(exc).__name__}: {str(exc).replace(token, '<token>')[:200]}",
                # the text above can be raw docker-exec output (a
                # `ProxyStartError`); it stays in the log. The detail is the class.
                detail=f"the usage request to the proxy failed ({type(exc).__name__}); see the worker log",
            ) from None
        if not isinstance(usage, dict):
            raise _UsageReadError("the usage endpoint did not return an object")
        try:
            tokens = int(usage["tokens"])
            cost_usd = float(usage["cost_usd"])
        except (KeyError, TypeError, ValueError):
            raise _UsageReadError("the usage endpoint's tokens / cost_usd are missing or unreadable") from None
        # Fields ADR 0014 §Amendment 2026-10-01 added to the usage payload. A
        # proxy image built before it (the image is built by hand, and a stale
        # one has happened) records streamed replies as 0 and never trips the
        # budget; reading its payload as "metered" would silently undo the whole
        # amendment. Absent or wrongly typed, the payload is not trusted.
        for field_name in _AMENDMENT_USAGE_FIELDS:
            if field_name not in usage:
                raise _UsageReadError(
                    f"the usage payload has no {field_name!r} field: the proxy image is stale "
                    "(it predates ADR 0014 §Amendment 2026-10-01) or returned an unrecognised shape; "
                    "rebuild forge-llm-proxy:latest"
                )
        if not isinstance(usage["unmetered"], bool):
            raise _UsageReadError(
                "the usage payload's 'unmetered' is not a boolean: the proxy returned an "
                "unrecognised shape (or predates ADR 0014 §Amendment 2026-10-01)"
            )
        return {**usage, "tokens": tokens, "cost_usd": cost_usd}

    @staticmethod
    def _proxy_record_after_failed_drain(proxy: ProxyContainer, token: str) -> dict | None:
        """after a drain that failed or ran out, what of the proxy's record may still be used:
        the refusals, the unmetered flag and its reason, and whether the budget was spent. These only
        ever GROW (a refusal once recorded stays, a token once unmetered stays, spend only rises), so a
        read made with a call still alive can miss one but can never invent one. The FIGURES are
        deliberately left out of what is returned: with a call alive they would be short, and a short
        figure is the thing the drain exists to prevent. None when the proxy cannot be read at all
        (or its payload is not the shape the executor trusts): then nothing of its record is known."""
        try:
            usage = SandboxBuildExecutor._read_usage_payload(proxy, token)
        except _UsageReadError as exc:
            logger.warning("sandbox executor: the proxy's record could not be read after a failed drain: %s", exc)
            return None
        return {
            "policy_refusals": usage.get("policy_refusals") or [],
            "unmetered": usage["unmetered"],
            "unmetered_reason": usage.get("unmetered_reason"),
            "budget_exceeded": bool(usage.get("budget_exceeded")),
        }

    @staticmethod
    def _with_cost_note(summary: str | None, cost_note: str) -> str:
        """the build's own failure text, then the sentence saying its cost is unknown. The
        failure detail is bounded (`build_queue.FAILURE_DETAIL_MAX_CHARS`) and cut at the end, so the
        first part gives way, never the note: that the cost could not be read back is what the reader
        most needs to see beside a reason that does not say it."""
        head = (summary or "").strip()
        room = FAILURE_DETAIL_MAX_CHARS - len(cost_note) - len("; ")
        if len(head) > room:
            head = head[: max(room - len(FAILURE_DETAIL_TRUNCATION_MARKER), 0)] + FAILURE_DETAIL_TRUNCATION_MARKER
        return f"{head}; {cost_note}" if head else cost_note

    def _failure_before_publish(
        self,
        build: Build,
        *,
        usage: dict | None,
        figures_known: bool,
        result: AgentAdapterResult,
        adapter_error: Exception | None,
        cost_budget_usd: float,
    ) -> ExecutionResult | None:
        """Every reason a build fails between the agent run and the publish, in the order they outrank
        one another: a recorded refusal, an unmetered response, a spent budget, an adapter exception,
        the adapter's own failure. None when none applies (the harvested tree may be published).

        `usage` is the proxy's record, or None when none could be read (then only the adapter's own
        failure can apply). `figures_known` says whether it carries the build's real `tokens` /
        `cost_usd`: False after a failed drain, when the messages must not quote a figure that
        may be short, nor say the usage was recorded."""
        # ADR 0029 §2: a request the proxy refused by tool policy
        # fails the build closed, even if the agent then reported success,
        # and says which condition. Ahead of the budget check on purpose --
        # a boundary event must not read as an ordinary budget failure.
        refusals = (usage or {}).get("policy_refusals") or []
        if refusals:
            message = self._policy_refusal_message(refusals)
            logger.warning("sandbox executor: build %s failed: %s", build.id, message)
            return ExecutionResult(ok=False, reason=BuildFailureReason.AGENT_ERROR, summary=message)

        # ADR 0014 §Amendment 2026-10-01 point 6: a success the proxy could
        # not meter means the budget could not be enforced -- counted as
        # exhausted, with a message that says so (not a plain budget miss).
        if usage is not None and usage.get("unmetered"):
            message = (
                "the LLM proxy could not meter a response, so the build budget "
                f"could not be enforced: {usage.get('unmetered_reason') or 'reason not recorded'}"
            )
            logger.warning("sandbox executor: build %s failed: %s", build.id, message)
            return ExecutionResult(ok=False, reason=BuildFailureReason.BUDGET_EXCEEDED, summary=message)

        if usage is not None and usage.get("budget_exceeded"):
            if figures_known:
                summary = (
                    "the per-build LLM budget was spent: "
                    f"{usage['tokens']} tokens and ${usage['cost_usd']:.4f} used against limits of "
                    f"{settings.build_token_budget} tokens and ${cost_budget_usd:.2f}"
                )
            else:
                summary = (
                    "the per-build LLM budget was spent (limits of "
                    f"{settings.build_token_budget} tokens and ${cost_budget_usd:.2f})"
                )
            return ExecutionResult(ok=False, reason=BuildFailureReason.BUDGET_EXCEEDED, summary=summary)

        if adapter_error is not None:
            # nothing about the proxy's record failed the build, so the adapter's
            # exception is the reason. Its text stays in the log (above); the detail names
            # its class, like every other exception-derived detail.
            recorded = " (the LLM proxy's usage was recorded)" if figures_known else ""
            return ExecutionResult(
                ok=False,
                reason=BuildFailureReason.INFRA_ERROR,
                summary=(
                    f"the agent adapter raised {type(adapter_error).__name__} after the sandbox run"
                    f"{recorded}; see the worker log"
                ),
            )

        if not result.ok:
            # the adapter's own summary is L3 text or an exception
            # string -- logged here, never persisted. The detail names the
            # adapter's reason code and nothing else.
            adapter_reason = result.reason or BuildFailureReason.AGENT_ERROR
            logger.warning(
                "sandbox executor: build %s agent run failed (%s): %s",
                build.id,
                adapter_reason,
                result.summary,
            )
            # `result.detail` is the sandbox runner's own sentence for a limit
            # it enforced (the disk cap, the harvest bound) -- platform text built from
            # a template and the configured limit, so unlike the adapter's summary it
            # may be shown.
            limit_note = f": {result.detail}" if result.detail else ""
            return ExecutionResult(
                ok=False,
                reason=adapter_reason,
                summary=(
                    f"the agent run in the sandbox ended unsuccessfully ({adapter_reason}){limit_note}; "
                    "its output is in the worker log"
                ),
            )
        return None

    @staticmethod
    def _policy_refusal_message(refusals: list[str]) -> str:
        """The build's failure message for a tool-policy refusal: says
        it was a tool-policy refusal, names each distinct condition once, and
        counts the requests. `refusals` are the proxy's `detail` strings, each
        already prefixed with `llm_proxy.POLICY_REFUSAL_PREFIX`."""
        distinct = list(dict.fromkeys(r.removeprefix(POLICY_REFUSAL_PREFIX) for r in refusals))
        count = len(refusals)
        noun = "request was" if count == 1 else "requests were"
        return f"{count} {noun} refused by tool policy: " + "; ".join(distinct)

    def _record_usage(self, build: Build, usage: dict) -> None:
        """Writes the proxy's figures (already validated by `_read_proxy_usage`).
        A proxy that metered nothing is a measured 0 / 0.0; an unreadable proxy
        never reaches here (the caller writes NULL)."""
        build.cost_tokens = usage["tokens"]
        build.cost_usd = usage["cost_usd"]

    def _destroy_network(self, network) -> None:
        if network is None:
            return
        try:
            network.remove()
        except Exception as exc:  # noqa: BLE001 - teardown never raises, and not only Docker's own errors
            logger.warning(
                "sandbox executor: network %s could not be removed (%s)",
                getattr(network, "name", "?"),
                type(exc).__name__,
            )
