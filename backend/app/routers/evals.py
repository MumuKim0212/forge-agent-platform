"""The eval pipeline's admin endpoints.

**Not under `/api/console`**: the console router stays read-only by construction
(`tests/test_console.py` pins its paths and methods), and the one write the console makes here --
starting a run -- lives, like the engine switch, on its own admin-only audited endpoint. All four
routes take `require_role(Role.ADMIN)` as a dependency, so the role check runs before anything else.

`POST` only RECORDS a run; it never executes a build. The worker queues each case's
build, one at a time, within the run's budget (`services/eval_runner.advance_runs`).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_role
from app.models.eval_run import EvalCase, EvalCaseResult, EvalRun
from app.models.policy_verdict import PolicyVerdict
from app.models.user import User
from app.schemas.eval_run import EvalCaseOut, EvalRunDetailOut, EvalRunOut, EvalStatusOut
from app.services import eval_runner
from app.services.roles import Role

router = APIRouter(prefix="/evals", tags=["evals"])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _case_out(db: Session, case: EvalCase) -> EvalCaseOut:
    verdict = (
        db.scalars(select(PolicyVerdict).where(PolicyVerdict.build_id == case.build_id)).first()
        if case.build_id
        else None
    )
    return EvalCaseOut(
        id=case.id,
        case_id=case.case_id,
        position=case.position,
        state=case.state,
        result=case.result,
        failure_reason=case.failure_reason,
        failed_check_index=case.failed_check_index,
        check_outcomes=case.check_outcomes,
        project_id=case.project_id,
        build_id=case.build_id,
        policy_risk_tier=verdict.risk_tier if verdict is not None else None,
        policy_flagged_checks=[c.check_name for c in verdict.checks if c.result == "flag"] if verdict else [],
        cost_usd=case.cost_usd,
        cost_tokens=case.cost_tokens,
        duration_seconds=case.duration_seconds,
        repair_attempts=case.repair_attempts,
        started_at=case.started_at,
        ended_at=case.ended_at,
    )


def _run_fields(db: Session, run: EvalRun, cases: list[EvalCase]) -> dict:
    starter = db.get(User, run.started_by_id)
    duration = (
        (_aware(run.ended_at) - _aware(run.started_at)).total_seconds() if run.ended_at is not None else None
    )
    return dict(
        id=run.id,
        status=run.status,
        engine=run.engine,
        model=run.model,
        request_set_version=run.request_set_version,
        prompt_fingerprint=run.prompt_fingerprint,
        repair_max_attempts=run.repair_max_attempts,
        budget_usd=run.budget_usd,
        spend_usd=run.spend_usd,
        started_by_id=run.started_by_id,
        started_by_name=starter.name if starter is not None else None,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_seconds=duration,
        detail=run.detail,
        total=len(cases),
        passed=sum(1 for c in cases if c.result == EvalCaseResult.PASSED),
        failed=sum(1 for c in cases if c.result == EvalCaseResult.FAILED),
        not_run=sum(1 for c in cases if c.result == EvalCaseResult.NOT_RUN_BUDGET),
    )


def _cases_of(db: Session, run: EvalRun) -> list[EvalCase]:
    return list(db.scalars(select(EvalCase).where(EvalCase.run_id == run.id).order_by(EvalCase.position)))


@router.get("/status", response_model=EvalStatusOut)
def get_eval_status(
    db: Session = Depends(get_db), _admin: User = Depends(require_role(Role.ADMIN))
) -> EvalStatusOut:
    """What the start control needs before any run exists. An allowlist assigned by hand, like the
    console's configuration view: nothing is serialised from `Settings`."""
    snapshot = eval_runner.status_snapshot(db, settings)
    return EvalStatusOut(
        budget_usd=snapshot.budget_usd,
        case_min_budget_usd=snapshot.case_min_budget_usd,
        executor_active=snapshot.executor_active,
        engine=snapshot.engine,
        model=snapshot.model,
        engine_available=snapshot.engine_available,
        run_in_progress=snapshot.run_in_progress,
        request_set_version=snapshot.request_set_version,
        case_count=snapshot.case_count,
    )


@router.post("/runs", response_model=EvalRunDetailOut, status_code=201)
def start_eval_run(
    db: Session = Depends(get_db), admin: User = Depends(require_role(Role.ADMIN))
) -> EvalRunDetailOut:
    try:
        run = eval_runner.start_run(db, admin=admin, cfg=settings)
    except eval_runner.EvalStartRefused as exc:
        status = 409 if exc.kind == eval_runner.CONFLICT else 422
        raise HTTPException(status_code=status, detail=exc.message) from exc
    db.commit()
    db.refresh(run)
    cases = _cases_of(db, run)
    return EvalRunDetailOut(**_run_fields(db, run, cases), cases=[_case_out(db, c) for c in cases])


@router.get("/runs", response_model=list[EvalRunOut])
def list_eval_runs(
    db: Session = Depends(get_db), _admin: User = Depends(require_role(Role.ADMIN))
) -> list[EvalRunOut]:
    runs = list(db.scalars(select(EvalRun).order_by(EvalRun.started_at.desc())))
    return [EvalRunOut(**_run_fields(db, run, _cases_of(db, run))) for run in runs]


@router.get("/runs/{run_id}", response_model=EvalRunDetailOut)
def get_eval_run(
    run_id: str, db: Session = Depends(get_db), _admin: User = Depends(require_role(Role.ADMIN))
) -> EvalRunDetailOut:
    run = db.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Eval run not found")
    cases = _cases_of(db, run)
    return EvalRunDetailOut(**_run_fields(db, run, cases), cases=[_case_out(db, c) for c in cases])
