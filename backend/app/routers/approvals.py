from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.user import User
from app.schemas.build import ChangedFilesOut, SmokeChecksOut, served_repair_routes
from app.schemas.approval import ApprovalDecisionInput, ApprovalRequestOut, ApprovalRequestSummaryOut
from app.services.approval import InvalidDecisionError, SelfApprovalError, decide
from app.services.roles import Role, holds_at_least

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _require_approver(current_user: User) -> None:
    """"Approve/reject requires the
    approver (or admin) role".

    expressed as one rung on the ladder instead of the old
    two-element membership tuple — `admin` clears the `approver` bar by
    ranking above it (the ladder is total), so the "(or admin)"
    half needs no second comparison. Note this is the
    *role* gate only; the self-approval ban is applied separately in
    `services/approval.decide` and has no admin exception."""
    if not holds_at_least(current_user.role, Role.APPROVER):
        raise HTTPException(status_code=403, detail="Approver or admin role required")


def _flagged_checks(request: ApprovalRequest, db: Session) -> list[str]:
    from app.models.policy_verdict import PolicyVerdict

    if request.policy_verdict_id is None:
        return []
    verdict = db.get(PolicyVerdict, request.policy_verdict_id)
    if verdict is None:
        return []
    return [c.check_name for c in verdict.checks if c.result == "flag"]


def _to_summary(request: ApprovalRequest, db: Session) -> ApprovalRequestSummaryOut:
    project = db.get(Project, request.project_id)
    requester = db.get(User, request.requester_id)
    build = db.get(Build, request.build_id)
    approvals_so_far = sum(1 for d in request.decisions if d.decision == "approve")
    return ApprovalRequestSummaryOut(
        id=request.id,
        build_id=request.build_id,
        project_id=request.project_id,
        project_name=project.name if project else "",
        requester_id=request.requester_id,
        requester_name=requester.name if requester else "",
        risk_tier=request.risk_tier,
        status=request.status,
        required_approvals=request.required_approvals,
        approvals_so_far=approvals_so_far,
        flagged_checks=_flagged_checks(request, db),
        diff_ref=build.diff_ref if build else None,
        preview_url=build.preview_url if build else None,
        preview_start_failure=build.preview_start_failure if build else None,
        changed_counts=(
            {"totals": build.changed_files["totals"], "truncated": build.changed_files["truncated"]}
            if build and build.changed_files
            else None
        ),
        created_at=request.created_at,
    )


def _to_detail(request: ApprovalRequest, db: Session) -> ApprovalRequestOut:
    """the request plus what the approver
    needs to judge it -- the text that was asked for (the one change request of this
    build, `uq_change_requests_build_id`) and what the platform measured about the build."""
    out = ApprovalRequestOut.model_validate(request)
    build = db.get(Build, request.build_id)
    project = db.get(Project, request.project_id)
    change_request = db.scalars(
        select(ChangeRequest).where(ChangeRequest.build_id == request.build_id)
    ).first()
    return out.model_copy(
        update={
            # so the console can tell, from the payload, whether the viewer is barred
            # by the self-approval rule (the requester OR the project's owner).
            "project_owner_id": project.owner_id if project else None,
            "request_text": change_request.nl_request if change_request else None,
            "changed_files": (
                ChangedFilesOut.model_validate(build.changed_files) if build and build.changed_files else None
            ),
            "protected_path_drift": build.protected_path_drift if build else None,
            # informational, never an input to the decision.
            "smoke_checks": (
                SmokeChecksOut.model_validate(build.smoke_checks)
                if build and build.smoke_checks is not None
                else None
            ),
            "preview_start_failure": build.preview_start_failure if build else None,
            # As stored, 0 included (never coerced).
            "repair_attempts": build.repair_attempts if build else None,
            # As stored when it is the shape the executor writes, otherwise None.
            "repair_routes": served_repair_routes(build.repair_routes) if build else None,
            # Display only -- the agent's own text, never an input to a decision.
            "agent_summary": build.agent_summary if build else None,
        }
    )


@router.get("", response_model=list[ApprovalRequestSummaryOut])
def list_approvals(
    status: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[ApprovalRequestSummaryOut]:
    """"Lists pending requests
    visible to approvers ... project, requester, tier, flagged findings
    summary, links to PR and preview." Defaults to pending + recently
    decided (no filter); `status` narrows to one status explicitly.

    Authenticated, not `approver`-gated: the permission
    matrix puts approval *reads* in the "authenticated" row (requesters see
    their own requests' status), while only the decisions require the role."""
    stmt = select(ApprovalRequest)
    if status:
        stmt = stmt.where(ApprovalRequest.status == status)
    stmt = stmt.order_by(ApprovalRequest.created_at.desc())
    requests = list(db.scalars(stmt))
    return [_to_summary(r, db) for r in requests]


@router.get("/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ApprovalRequestOut:
    approval = db.get(ApprovalRequest, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return _to_detail(approval, db)


@router.post("/{approval_id}/approve", response_model=ApprovalRequestOut)
def approve_approval(
    approval_id: str,
    body: ApprovalDecisionInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApprovalRequestOut:
    return _to_detail(
        _decide(approval_id, decision="approve", comment=body.comment, db=db, current_user=current_user), db
    )


@router.post("/{approval_id}/reject", response_model=ApprovalRequestOut)
def reject_approval(
    approval_id: str,
    body: ApprovalDecisionInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApprovalRequestOut:
    return _to_detail(
        _decide(approval_id, decision="reject", comment=body.comment, db=db, current_user=current_user), db
    )


def _decide(
    approval_id: str,
    *,
    decision: str,
    comment: str | None,
    db: Session,
    current_user: User,
) -> ApprovalRequest:
    _require_approver(current_user)

    request = db.get(ApprovalRequest, approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")

    project = db.get(Project, request.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        decide(
            db,
            request,
            actor_id=current_user.id,
            project_owner_id=project.owner_id,
            decision=decision,
            comment=comment,
        )
    except SelfApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except InvalidDecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(request)
    return request
