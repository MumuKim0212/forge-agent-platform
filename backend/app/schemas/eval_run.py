"""the shapes of an eval run and its cases (admin-only endpoints).

Hand-built from named values, like `schemas/engine.py`: nothing here is an ORM dump. The
`check_outcomes` are the platform's own short outcome strings (never a response body), and a
case links to its build by id, which is where its trace lives.
"""


from pydantic import BaseModel

from app.schemas.types import UtcDateTime


class EvalCaseOut(BaseModel):
    id: str
    case_id: str
    position: int
    state: str  # PENDING | RUNNING | DONE
    result: str | None  # PASSED | FAILED | NOT_RUN_BUDGET, once DONE
    failure_reason: str | None
    failed_check_index: int | None
    check_outcomes: list[dict] | None
    project_id: str | None
    build_id: str | None
    # The build's policy-gate verdict, read from `policy_verdicts` at request time (an eval build is
    # gated but creates no approval request). None until the gate has run for the build.
    policy_risk_tier: str | None
    policy_flagged_checks: list[str]
    cost_usd: float | None
    cost_tokens: int | None
    duration_seconds: float | None
    repair_attempts: int | None  # NULL = not measured; 0 = the first attempt shipped or the loop was off
    started_at: UtcDateTime | None
    ended_at: UtcDateTime | None


class EvalRunOut(BaseModel):
    id: str
    status: str  # RUNNING | COMPLETED | STOPPED_BUDGET | FAILED
    engine: str
    model: str | None
    request_set_version: int
    prompt_fingerprint: str | None  # NULL for a run from before it was recorded
    repair_max_attempts: int | None  # NULL = not recorded (a run from before it was recorded), never 0
    budget_usd: float
    spend_usd: float
    started_by_id: str
    started_by_name: str | None
    started_at: UtcDateTime
    ended_at: UtcDateTime | None
    duration_seconds: float | None
    detail: str | None
    total: int
    passed: int
    failed: int
    not_run: int


class EvalRunDetailOut(EvalRunOut):
    cases: list[EvalCaseOut]


class EvalStatusOut(BaseModel):
    """`GET /evals/status`: exactly these fields, assigned by
    hand in the router. Availability is a boolean; no key, and nothing derived from one beyond
    that, is here."""

    budget_usd: float
    case_min_budget_usd: float
    executor_active: bool
    engine: str | None
    model: str | None
    engine_available: bool
    run_in_progress: bool
    request_set_version: int | None
    case_count: int | None
