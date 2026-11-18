
from pydantic import BaseModel, ConfigDict

from app.schemas.build import ChangedCountsOut, ChangedFilesOut, RepairAttemptOut, SmokeChecksOut
from app.schemas.policy import PolicyVerdictOut
from app.schemas.types import UtcDateTime


class ApprovalDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str
    decision: str
    comment: str | None
    created_at: UtcDateTime


class ApprovalRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    build_id: str
    project_id: str
    requester_id: str
    # the project's owner, who is barred from deciding the request exactly like the
    # requester (`services/approval._is_self_approval`). Filled by the router; None if the
    # project row is gone (such a request cannot be decided: the route answers 404).
    project_owner_id: str | None = None
    policy_verdict_id: str | None
    risk_tier: str
    status: str
    required_approvals: int
    superseded_by_build_id: str | None
    superseded_at: UtcDateTime | None
    created_at: UtcDateTime
    decisions: list[ApprovalDecisionOut] = []
    # Full per-check results for the console's detail view
    # (the detail view shows per-check
    # results with findings).
    policy_verdict: PolicyVerdictOut | None = None
    # what was asked for and what the
    # platform measured about the build. `request_text` is user-authored; the changed
    # files and drift come from `Build` (None = not measured). Filled by the router.
    request_text: str | None = None
    changed_files: ChangedFilesOut | None = None
    protected_path_drift: list[str] | None = None
    # what the platform saw when it called the preview's own GET routes. None = not
    # measured. Informational; the paths passed a character filter before they were stored.
    smoke_checks: SmokeChecksOut | None = None
    # shown beside the smoke check; a different fact (the preview never ran).
    preview_start_failure: str | None = None
    # Repair attempts the build started, as stored (0 = none ran); a platform-
    # measured count. Detail view only (the list is unchanged). None only if the build is gone.
    repair_attempts: int | None = None
    # The failing routes each repair attempt was started for, as stored; None = no repair was
    # started (or a build from before the column). Detail view only, like the count.
    repair_routes: list[RepairAttemptOut] | None = None
    # The agent's own text (an unverified claim), detail view only -- not in the
    # pending list. None on a build that did not succeed.
    agent_summary: str | None = None


class ApprovalRequestSummaryOut(BaseModel):
    """the list view — project,
    requester, tier, flagged-findings summary, PR/preview links — without
    the full per-check detail (`ApprovalRequestOut`/`GET /approvals/{id}`
    carries that)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    build_id: str
    project_id: str
    project_name: str
    requester_id: str
    requester_name: str
    risk_tier: str
    status: str
    required_approvals: int
    approvals_so_far: int
    flagged_checks: list[str]
    diff_ref: str | None
    preview_url: str | None
    # why the preview did not start (platform-authored code); None = it did not fail.
    preview_start_failure: str | None = None
    # counts only in the list; the full list is the detail's.
    changed_counts: ChangedCountsOut | None = None
    created_at: UtcDateTime


class ApprovalDecisionInput(BaseModel):
    comment: str | None = None
