"""Eval runs: starting one, and the worker's step that drives it.

Two halves with one rule between them: **the API never executes a build**. `start_run`
(called by `routers/evals.py`) only records a run and its cases. Everything that costs money --
queueing a case's build -- happens in `advance_runs`, which the worker calls on every loop tick,
beside the build queue.

The run budget bounds spend the way the proxy bounds a build (ADR 0014):

- **remaining** = the run's budget minus the METERED `cost_usd` of its finished cases. The figure
  is copied from `Build.cost_usd`, which `SandboxBuildExecutor` writes only from the LLM proxy's
  accounting; nothing the engine reports about itself reaches this arithmetic. A finished case
  whose cost is NULL (unmeasured) leaves nothing remaining.
- the next case's build carries a per-build cost budget of `min(BUILD_COST_BUDGET_USD, remaining)`
  (`Build.cost_budget_usd`, passed to the proxy token), so the proxy enforces it before each call;
- below `EVAL_CASE_MIN_BUDGET_USD` the case and every later one are `NOT_RUN_BUDGET` and the run
  ends `STOPPED_BUDGET`.

A run is driven from its rows alone, one case at a time, so a worker restart resumes it, and a
case that is `DONE` is never touched again (no case is judged twice).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.eval_run import (
    EvalCase,
    EvalCaseResult,
    EvalCaseState,
    EvalFailureReason,
    EvalRun,
    EvalRunStatus,
)
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.services import agent_prompt
from app.services.audit import write_audit_log
from app.services.build_queue import clear_preview_fields
from app.services.engines import ENGINES, Unavailable, engine_config, resolve_engine, sandbox_executor_active
from app.services.eval_judge import Probe, PreviewRouteProbe, Verdict, judge_case
from app.services.eval_request_set import EvalCaseSpec, RequestSet, RequestSetError, load_request_set
from app.services.project_status import advance
from app.services.status import BuildStatus, ProjectKind, ProjectStatus

logger = logging.getLogger("app.eval_runner")

# The stuck-run margin: a run is FAILED when nothing has progressed for the maximum build
# duration plus this much. Named so a test pins the arithmetic.
STUCK_MARGIN_MINUTES = 10

# The template an eval project is created on, by the seed's own name for it; the first enabled
# template is the fallback on a platform whose seed was never run (prod seeds the template only).
_GENERIC_WEBTOOL_TEMPLATE_NAME = "범용 웹툴"
_FALLBACK_TEAM_NAME = "Platform evals"

CONFLICT = "conflict"
UNAVAILABLE = "unavailable"


class EvalStartRefused(Exception):
    """A run cannot be started. `kind` is `CONFLICT` (a run is in progress, 409) or `UNAVAILABLE`
    (the executor is inactive, the engine is unavailable or the request set is unusable, 422).
    The message is platform-authored and safe to show."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes for `DateTime(timezone=True)`; every writer is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The budget. Pure functions, so a test states the figures literally.
# ---------------------------------------------------------------------------


def remaining_budget(budget_usd: float, finished_costs: Iterable[float | None]) -> float:
    """The run budget minus the metered cost of the finished cases, never below zero. A NULL
    cost (the proxy could not say what that case spent) leaves ZERO: an unknown spend is counted
    as the whole budget rather than as nothing (ADR 0014 §Amendment point 6)."""
    costs = list(finished_costs)
    if any(cost is None for cost in costs):
        return 0.0
    return max(0.0, budget_usd - sum(costs))  # type: ignore[arg-type]


def case_cost_budget(remaining: float, build_cost_budget_usd: float) -> float:
    """The per-build cost budget of the next case's build: never more than the setting a normal
    build gets, never more than what is left of the run."""
    return min(build_cost_budget_usd, remaining)


def has_room_for_a_case(remaining: float, min_budget_usd: float) -> bool:
    """False when what is left is below the floor: the case is not started."""
    return remaining >= min_budget_usd


def ran_on_the_runs_engine(run: EvalRun, build: Build) -> bool:
    """Whether the case's build ran on the engine and model the run recorded. The engine is resolved
    when the worker CLAIMS a build (ADR 0035 §5), so an admin who switches engines between a case
    being queued and claimed makes the build run on the new selection while the run still states
    the old one. Such a case must not be reported under the run's engine.

    `build.engine` NULL means no engine was recorded (never the case for a claimed eval build) and is
    not a difference. `build.model` NULL beside an equal engine is not a difference either: that is
    how a build that failed at claim (engine unavailable) is recorded, and the engine did not change."""
    if build.engine is None:
        return True
    if build.engine != run.engine:
        return False
    return build.model is None or build.model == run.model


def _finished_costs(cases: Iterable[EvalCase]) -> list[float | None]:
    """The `cost_usd` of every case that actually ran a build and has been judged."""
    return [
        c.cost_usd
        for c in cases
        if c.state == EvalCaseState.DONE and c.result != EvalCaseResult.NOT_RUN_BUDGET
    ]


# ---------------------------------------------------------------------------
# Starting a run (the API half).
# ---------------------------------------------------------------------------


def run_in_progress(db: Session) -> EvalRun | None:
    return db.scalars(select(EvalRun).where(EvalRun.status == EvalRunStatus.RUNNING)).first()


@dataclass(frozen=True)
class EvalStatus:
    """What the console needs to know before any run exists.
    Every field is a named, platform-computed value; none is a key, and availability is a boolean."""

    budget_usd: float
    case_min_budget_usd: float
    executor_active: bool
    engine: str | None
    model: str | None
    engine_available: bool
    run_in_progress: bool
    request_set_version: int | None  # None when the set cannot be loaded
    case_count: int | None


def status_snapshot(db: Session, cfg: Settings = settings) -> EvalStatus:
    """The effective eval settings and whether a run could start now, from the same functions
    `start_run` refuses on (`sandbox_executor_active`, `resolve_engine`, `run_in_progress`), so the
    console's reading cannot drift from the backend's answer."""
    choice = resolve_engine(db, cfg)
    engine = choice.engine
    available = not isinstance(choice, Unavailable)
    model = choice.model if available else (engine_config(engine, cfg).model if engine in ENGINES else None)
    try:
        request_set: RequestSet | None = load_request_set()
    except RequestSetError:
        request_set = None
    return EvalStatus(
        budget_usd=cfg.eval_run_budget_usd,
        case_min_budget_usd=cfg.eval_case_min_budget_usd,
        executor_active=sandbox_executor_active(cfg),
        engine=engine,
        model=model,
        engine_available=available,
        run_in_progress=run_in_progress(db) is not None,
        request_set_version=request_set.version if request_set is not None else None,
        case_count=len(request_set.cases) if request_set is not None else None,
    )


def start_run(db: Session, *, admin: User, cfg: Settings = settings) -> EvalRun:
    """Record a run and one pending case per request-set case. Refuses (`EvalStartRefused`) when
    a run is in progress, the sandbox executor is inactive, the selected engine is unavailable,
    or the request set cannot be loaded. Audits `eval.run_started`. Does not commit: the router
    owns the transaction. Starts nothing: the worker queues the first case."""
    if run_in_progress(db) is not None:
        raise EvalStartRefused(CONFLICT, "an eval run is already in progress; wait for it to finish")
    if not sandbox_executor_active(cfg):
        raise EvalStartRefused(
            UNAVAILABLE,
            "the sandbox executor is not active, so no build can run: it needs USE_SANDBOX_EXECUTOR "
            "and at least one engine's API key",
        )
    choice = resolve_engine(db, cfg)
    if isinstance(choice, Unavailable):
        raise EvalStartRefused(UNAVAILABLE, choice.detail)
    try:
        request_set = load_request_set()
    except RequestSetError as exc:
        raise EvalStartRefused(UNAVAILABLE, f"the eval request set is unusable: {exc}") from exc

    run = EvalRun(
        status=EvalRunStatus.RUNNING,
        engine=choice.engine,
        model=choice.model,
        request_set_version=request_set.version,
        # The prompt a create build receives, from the composer the executor calls.
        prompt_fingerprint=agent_prompt.prompt_fingerprint(agent_prompt.compose_system_prompt("create")),
        # The loop's setting at start; the worker compares it before each case.
        repair_max_attempts=cfg.build_repair_max_attempts,
        budget_usd=cfg.eval_run_budget_usd,
        spend_usd=0.0,
        started_by_id=admin.id,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError as exc:  # the partial unique index: two starts raced, one lost
        db.rollback()
        raise EvalStartRefused(CONFLICT, "an eval run is already in progress; wait for it to finish") from exc
    for position, spec in enumerate(request_set.cases):
        db.add(EvalCase(run_id=run.id, case_id=spec.id, position=position))
    write_audit_log(
        db,
        actor_id=admin.id,
        action="eval.run_started",
        target=f"eval_run:{run.id}",
        metadata={
            "engine": run.engine,
            "model": run.model,
            "request_set_version": run.request_set_version,
            "repair_max_attempts": run.repair_max_attempts,
            "budget_usd": run.budget_usd,
        },
    )
    db.flush()
    return run


# ---------------------------------------------------------------------------
# Driving a run (the worker half).
# ---------------------------------------------------------------------------

ProbeFactory = Callable[[Build], Probe]


def advance_runs(
    db: Session,
    *,
    now: datetime | None = None,
    preview_manager=None,
    probe_factory: ProbeFactory | None = None,
    request_set: RequestSet | None = None,
    cfg: Settings = settings,
) -> int:
    """One worker tick's work on eval runs: advance the `RUNNING` run, if any. Returns how many
    runs it looked at (0 or 1). Never raises: a failure in one run is logged, rolled back and
    retried next tick; the stuck-run check is what ends a run that never recovers."""
    advanced = 0
    for run in list(db.scalars(select(EvalRun).where(EvalRun.status == EvalRunStatus.RUNNING))):
        advanced += 1
        try:
            _advance(
                db,
                run,
                now=now or _utcnow(),
                preview_manager=preview_manager,
                probe_factory=probe_factory,
                request_set=request_set,
                cfg=cfg,
            )
        except Exception:  # noqa: BLE001 - a raising run must never kill the worker loop
            logger.exception("eval run %s: advancing it raised; it is retried next tick", run.id)
            db.rollback()
    return advanced


def _cases(db: Session, run: EvalRun) -> list[EvalCase]:
    return list(db.scalars(select(EvalCase).where(EvalCase.run_id == run.id).order_by(EvalCase.position)))


def _end_run(db: Session, run: EvalRun, *, detail: str | None, now: datetime) -> None:
    """Close a run whose `status` the caller has just set to one of the terminal constants
    (written at the call site so `tests/test_project_status.py`'s scan can see it is an eval
    run's status and not a project's)."""
    cases = _cases(db, run)
    run.ended_at = now
    run.detail = detail
    run.spend_usd = _spend(cases)
    write_audit_log(
        db,
        actor_id=None,
        action="eval.run_ended",
        target=f"eval_run:{run.id}",
        metadata={
            "status": run.status,
            "passed": sum(1 for c in cases if c.result == EvalCaseResult.PASSED),
            "total": len(cases),
            "spend_usd": run.spend_usd,
        },
    )
    db.commit()


def _spend(cases: list[EvalCase]) -> float:
    return sum(cost for cost in _finished_costs(cases) if cost is not None)


def _stuck_since(db: Session, run: EvalRun, cases: list[EvalCase]) -> datetime:
    """When this run last made progress: its own marker, or -- for a case whose build has been
    running -- the build's start, whichever is later, so time spent queued behind other builds
    does not count against a build that has only just been claimed."""
    since = _aware(run.last_progress_at)
    for case in cases:
        if case.state == EvalCaseState.RUNNING and case.build_id:
            build = db.get(Build, case.build_id)
            if build is not None and build.started_at is not None:
                since = max(since, _aware(build.started_at))
    return since


def _advance(
    db: Session,
    run: EvalRun,
    *,
    now: datetime,
    preview_manager,
    probe_factory: ProbeFactory | None,
    request_set: RequestSet | None,
    cfg: Settings,
) -> None:
    cases = _cases(db, run)

    limit = timedelta(minutes=cfg.build_max_duration_minutes + STUCK_MARGIN_MINUTES)
    if now - _stuck_since(db, run, cases) > limit:
        for case in cases:
            if case.state == EvalCaseState.RUNNING:
                case.state = EvalCaseState.DONE
                case.result = EvalCaseResult.FAILED
                case.failure_reason = EvalFailureReason.RUN_STUCK
                case.ended_at = now
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail=(
                "no case progressed within the maximum build duration "
                f"({cfg.build_max_duration_minutes} minutes) plus a {STUCK_MARGIN_MINUTES}-minute margin"
            ),
            now=now,
        )
        return

    # The set is the repository file, read each tick (a restarted worker needs nothing in memory).
    # A run is judged against the version it started on only: a changed file ends the run, since
    # its results would no longer be comparable.
    current = request_set if request_set is not None else _load_or_none()
    if current is None or current.version != run.request_set_version:
        found = "could not be loaded" if current is None else f"is now version {current.version}"
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail=f"the run started on request set version {run.request_set_version}, which {found}",
            now=now,
        )
        return

    running = next((c for c in cases if c.state == EvalCaseState.RUNNING), None)
    if running is not None:
        build = db.get(Build, running.build_id) if running.build_id else None
        if build is None:
            _finish_case_without_build(db, run, running, now=now)
        elif build.status in (BuildStatus.SUCCEEDED, BuildStatus.FAILED):
            spec = _spec_for(running, current)
            if spec is None:
                run.status = EvalRunStatus.FAILED
                _end_run(
                    db,
                    run,
                    detail=f"case {running.case_id!r} is not in the current request set, so it cannot be judged",
                    now=now,
                )
                return
            _judge_and_finish(
                db,
                run,
                running,
                build,
                spec,
                now=now,
                preview_manager=preview_manager,
                probe_factory=probe_factory,
            )
        else:
            return  # its build is queued or running: wait, one case at a time
        cases = _cases(db, run)

    pending = [c for c in cases if c.state == EvalCaseState.PENDING]
    if not pending:
        run.status = EvalRunStatus.COMPLETED
        _end_run(db, run, detail=None, now=now)
        return

    # The run never switches engine or model: the selection it started on is the
    # one the next claim will use, or the run stops rather than spend on a different one.
    choice = resolve_engine(db, cfg)
    if isinstance(choice, Unavailable) or (choice.engine, choice.model) != (run.engine, run.model):
        shown = choice.engine if isinstance(choice, Unavailable) else f"{choice.engine}/{choice.model}"
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail=(
                f"the run started on {run.engine}/{run.model} but the selection is now {shown}; "
                "a run never switches engine or model, so it was stopped before spending more"
            ),
            now=now,
        )
        return

    # The repair setting is recorded by the API process and executed by the worker's process:
    # a restart of only one would make the record lie, so the run stops rather than spend
    # on a configuration it did not record. A run with no record (NULL) is not guarded.
    if run.repair_max_attempts is not None and run.repair_max_attempts != cfg.build_repair_max_attempts:
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail=(
                f"the run recorded BUILD_REPAIR_MAX_ATTEMPTS={run.repair_max_attempts} but the worker "
                f"is running with {cfg.build_repair_max_attempts}; it was stopped before spending more. "
                "Restart both the API and the worker processes so they agree, then start a new run"
            ),
            now=now,
        )
        return

    remaining = remaining_budget(run.budget_usd, _finished_costs(cases))
    nxt = pending[0]
    if not has_room_for_a_case(remaining, cfg.eval_case_min_budget_usd):
        for case in pending:
            case.state = EvalCaseState.DONE
            case.result = EvalCaseResult.NOT_RUN_BUDGET
            case.failure_reason = EvalFailureReason.NOT_RUN_BUDGET
            case.ended_at = now
        run.status = EvalRunStatus.STOPPED_BUDGET
        _end_run(
            db,
            run,
            detail=(
                f"${remaining:.4f} of the ${run.budget_usd:.2f} run budget is left, below the "
                f"${cfg.eval_case_min_budget_usd:.2f} floor for starting another case"
            ),
            now=now,
        )
        return

    spec = _spec_for(nxt, current)
    if spec is None:
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail=f"case {nxt.case_id!r} is not in the current request set, so it cannot be run",
            now=now,
        )
        return
    _queue_case(
        db,
        run,
        nxt,
        spec,
        cost_budget_usd=case_cost_budget(remaining, cfg.build_cost_budget_usd),
        now=now,
    )


def _spec_for(case: EvalCase, current: RequestSet) -> EvalCaseSpec | None:
    """The request-set entry for a stored case, from the set whose version the run recorded."""
    return next((s for s in current.cases if s.id == case.case_id), None)


def _load_or_none() -> RequestSet | None:
    try:
        return load_request_set()
    except RequestSetError:
        logger.exception("eval runner: the request set could not be loaded")
        return None


def _team_for(db: Session, admin: User) -> str:
    if admin.team_id:
        return admin.team_id
    team = db.scalars(select(Team).order_by(Team.created_at, Team.name)).first()
    if team is None:
        team = Team(name=_FALLBACK_TEAM_NAME)
        db.add(team)
        db.flush()
    return team.id


def _template_for(db: Session) -> Template | None:
    template = db.scalars(select(Template).where(Template.name == _GENERIC_WEBTOOL_TEMPLATE_NAME)).first()
    if template is None:
        template = db.scalars(select(Template).where(Template.enabled.is_(True)).order_by(Template.created_at)).first()
    return template


def eval_project_name(run: EvalRun, case_id: str) -> str:
    """`eval-<run short id>-<case id>`: recognisable in the registry, in the audit log and, with
    `GIT_PROVIDER=github`, as the repository name in the organisation."""
    return f"eval-{run.id[:8]}-{case_id}"


def _queue_case(
    db: Session, run: EvalRun, case: EvalCase, spec: EvalCaseSpec, *, cost_budget_usd: float, now: datetime
) -> None:
    """The eval project, the change request that carries the case's request, and the create
    build, QUEUED like any other -- in ONE transaction with the case's own state change, so a
    crash leaves either nothing or a case that points at its build."""
    admin = db.get(User, run.started_by_id)
    template = _template_for(db)
    if admin is None or template is None:
        run.status = EvalRunStatus.FAILED
        _end_run(
            db,
            run,
            detail="the run cannot create an eval project: the triggering admin or the template is missing",
            now=now,
        )
        return

    project = Project(
        name=eval_project_name(run, spec.id),
        owner_id=admin.id,
        team_id=_team_for(db, admin),
        template_id=template.id,
        kind=ProjectKind.EVAL,  # `status` is left to the model's default (SUBMITTED), like any new project
    )
    db.add(project)
    db.flush()

    change_request = ChangeRequest(project_id=project.id, requester_id=admin.id, nl_request=spec.request)
    db.add(change_request)
    db.flush()
    build = Build(
        project_id=project.id,
        type="create",
        status=BuildStatus.QUEUED,
        cost_budget_usd=cost_budget_usd,
    )
    db.add(build)
    db.flush()
    change_request.build_id = build.id
    advance(db, project, ProjectStatus.BUILD_PENDING, cause=f"build:{build.id}", actor_id=admin.id)

    case.state = EvalCaseState.RUNNING
    case.project_id = project.id
    case.build_id = build.id
    case.started_at = now
    run.last_progress_at = now
    db.commit()


def _finish_case_without_build(db: Session, run: EvalRun, case: EvalCase, *, now: datetime) -> None:
    case.state = EvalCaseState.DONE
    case.result = EvalCaseResult.FAILED
    case.failure_reason = "INFRA_ERROR"
    case.ended_at = now
    run.last_progress_at = now
    db.commit()


def _default_probe(build: Build, preview_manager) -> Probe:
    manager = preview_manager
    if manager is None:
        from app.services.preview_manager import PreviewManager

        manager = PreviewManager()
    return PreviewRouteProbe(
        build_id=build.id,
        preview_url=build.preview_url,
        token_hash=build.preview_token_hash,
        preview_manager=manager,
    )


def _judge_and_finish(
    db: Session,
    run: EvalRun,
    case: EvalCase,
    build: Build,
    spec: EvalCaseSpec,
    *,
    now: datetime,
    preview_manager,
    probe_factory: ProbeFactory | None,
) -> None:
    """Judge the case, record the result and the build's metered figures in ONE commit (so a
    crash before it leaves the case `RUNNING` and it is judged on resume, and a crash after it
    leaves it `DONE` and it is never judged again), then tear the preview down."""
    if not ran_on_the_runs_engine(run, build):
        # Not judged: nothing is requested of its preview. Its metered cost is still copied below.
        verdict = Verdict(
            result=EvalCaseResult.FAILED,
            failure_reason=EvalFailureReason.ENGINE_CHANGED,
            failed_check_index=None,
            outcomes=[],
        )
    else:
        if build.status == BuildStatus.SUCCEEDED:
            probe = probe_factory(build) if probe_factory is not None else _default_probe(build, preview_manager)
        else:
            probe = _never_called  # a failed build is judged on its status alone; nothing is requested
        verdict = judge_case(
            build_status=build.status,
            build_failure_reason=build.failure_reason,
            checks=spec.checks,
            probe=probe,
        )

    case.state = EvalCaseState.DONE
    case.result = verdict.result
    case.failure_reason = verdict.failure_reason
    case.failed_check_index = verdict.failed_check_index
    case.check_outcomes = verdict.outcomes
    # The proxy's figures, copied from the build; the engine's own report is not a source.
    case.cost_usd = build.cost_usd
    case.cost_tokens = build.cost_tokens
    # The build's own count of repair attempts (0 is a measurement, not "absent").
    case.repair_attempts = build.repair_attempts
    if build.started_at is not None and build.ended_at is not None:
        case.duration_seconds = (_aware(build.ended_at) - _aware(build.started_at)).total_seconds()
    case.ended_at = now
    run.last_progress_at = now
    run.spend_usd = _spend(_cases(db, run))
    db.commit()

    _tear_down_preview(db, build, preview_manager)


def _never_called(method: str, path: str):  # pragma: no cover - guards the failed-build branch
    raise AssertionError("a failed build is judged without making a request")


def _tear_down_preview(db: Session, build: Build, preview_manager) -> None:
    """The case's preview is no longer needed once it is judged.
    Best-effort: the TTL sweep reaps anything this misses."""
    if not build.preview_url:
        return
    try:
        manager = preview_manager
        if manager is None:
            from app.services.preview_manager import PreviewManager

            manager = PreviewManager()
        manager.stop_preview(build.id)
    except Exception:  # noqa: BLE001 - teardown must never fail the tick; the TTL sweep is the backstop
        logger.exception("eval run: failed to stop the preview of build %s", build.id)
    clear_preview_fields(db, build.id)
