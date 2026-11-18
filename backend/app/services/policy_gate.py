"""persistence + pipeline-hook side of the policy gate.
`policy_checks.py` holds
the pure check functions; this module is the impure shell that runs them
against a completed build's output (the executor's sanitised copy of the
agent's harvested workspace, ADR 0032), persists the verdict +
per-check results, and writes the audit-log entry — all in the same DB
transaction as the rest of that build's publish step.

Verdicts are immutable per build (spec: "No re-running checks on an
unchanged build") — `run_and_persist_policy_gate` is written to be called
at most once per build (the caller, `sandbox_build_executor.py`, only calls
it once, right after publish/preview); it does not itself defend against a
second call for the same build (that would need a DB uniqueness check this
task's scope didn't require enforcing beyond the `unique=True` column,
which will raise on an actual double-insert).

the gate as a whole fails closed.
`record_gate_error_verdict` is what the caller records when running or persisting the
gate (or routing its verdict) raised: a platform-authored verdict at the highest tier, so
the build is routed to a human and the reason is on the timeline, instead of a
`SUCCEEDED` build with no verdict and no request. The CALLER owns the savepoint around
both (`sandbox_build_executor._run_policy_gate`), which is what discards a half-written
verdict before this one is written.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.build import Build
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.services.audit import write_audit_log
from app.services.policy_checks import (
    PolicyCheckName,
    PolicyCheckStatus,
    PolicyRiskTier,
    PolicyVerdictResult,
    build_policy_verdict,
)

logger = logging.getLogger("app.policy_gate")


def run_and_persist_policy_gate(
    db: Session,
    build: Build,
    *,
    workspace_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    medium_tier_checks: list[str],
    high_tier_checks: list[str],
    network_available: bool = False,
) -> PolicyVerdict:
    """Runs the six checks, persists `PolicyVerdict` + `PolicyCheckResult`
    rows, and audit-logs the verdict. Does not commit — the caller commits
    as part of the build's own transaction (same pattern as
    `services/audit.write_audit_log`). Never raises for a check failure
    (checks fail closed internally); only a DB-level error from this
    function itself would propagate, same as any other DB write in the
    build pipeline."""
    result: PolicyVerdictResult = build_policy_verdict(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_skeleton_dir,
        protected_paths=protected_paths,
        medium_tier_checks=medium_tier_checks,
        high_tier_checks=high_tier_checks,
        network_available=network_available,
    )

    verdict = PolicyVerdict(build_id=build.id, risk_tier=result.risk_tier)
    db.add(verdict)
    db.flush()  # assign verdict.id for the check-result rows below

    for check in result.checks:
        db.add(
            PolicyCheckResult(
                verdict_id=verdict.id,
                check_name=check.check_name,
                result=check.result,
                summary=check.summary,
                findings=check.findings,
            )
        )

    summary_by_check = {c.check_name: c.result for c in result.checks}
    write_audit_log(
        db,
        actor_id=None,  # worker-driven, no human actor (same convention as build_queue.py)
        action="policy_gate.verdict",
        target=f"build:{build.id}",
        metadata={"risk_tier": result.risk_tier, "checks": summary_by_check},
    )

    logger.info(
        "policy gate: build %s verdict=%s checks=%s",
        build.id,
        result.risk_tier,
        summary_by_check,
    )

    return verdict


# The two places the caller can fail, as they are named in the stored text. A fixed
# vocabulary on purpose: the stored summary is platform-authored, and an exception's own
# text (paths, SQL, a value from the workspace) never goes into it.
GATE_STAGE_CHECKS = "running and recording the checks"
GATE_STAGE_ROUTING = "routing the verdict to approval"


def record_gate_error_verdict(db: Session, build: Build, *, stage: str, error: BaseException) -> PolicyVerdict:
    """the verdict a build gets when the gate itself failed -- `high` (the highest tier,
    so `approval.create_approval_request_for_verdict` makes it `PENDING` and a human decides),
    with ONE flagged row, `PolicyCheckName.GATE_ERROR`, whose summary says what happened.
    Audit-logged as `policy_gate.error`. Does not commit, like `run_and_persist_policy_gate`.

    Only the exception's CLASS is recorded (the summary, the finding and the audit row): the
    text of an exception raised inside the gate can carry a path, a SQL fragment or a value from
    the agent's workspace, and this row is shown to every reader of the timeline. The text goes
    to the worker log (the caller's `logger.exception`).

    It must run in a savepoint of its own, after the failed attempt's savepoint was rolled back:
    `PolicyVerdict.build_id` is unique, so a half-written verdict from that attempt would make
    this insert fail too."""
    error_class = type(error).__name__
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.HIGH)
    db.add(verdict)
    db.flush()  # assign verdict.id for the row below
    db.add(
        PolicyCheckResult(
            verdict_id=verdict.id,
            check_name=PolicyCheckName.GATE_ERROR,
            result=PolicyCheckStatus.FLAG,
            summary=(
                f"The policy gate failed ({error_class} while {stage}), so none of its checks "
                "could be relied on. This build is held at the highest risk tier for a human "
                "decision; the worker log has the detail."
            ),
            findings=[{"type": "gate_error", "stage": stage, "error_class": error_class}],
        )
    )
    write_audit_log(
        db,
        actor_id=None,  # worker-driven, no human actor (same convention as the verdict above)
        action="policy_gate.error",
        target=f"build:{build.id}",
        metadata={"risk_tier": PolicyRiskTier.HIGH, "stage": stage, "error_class": error_class},
    )
    logger.error("policy gate: build %s has a gate-error verdict (%s while %s)", build.id, error_class, stage)
    return verdict
