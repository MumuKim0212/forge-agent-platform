"""row builders shared by the approval tests that need real database sessions
(`test_approval_one_decision_per_actor.py` on SQLite, `test_approval_postgres.py` on Postgres)."""

from __future__ import annotations

import uuid

from app.models.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from app.models.build import Build
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.services.approval import decide


def roles(db) -> dict[str, str]:
    return {
        "owner": db.query(User).filter_by(role="requester").first().id,
        "approver": db.query(User).filter_by(role="approver").first().id,
        "admin": db.query(User).filter_by(role="admin").first().id,
    }


def new_request(db, *, required: int) -> str:
    """A PENDING request on a fresh project; returns its id. The project's owner is also the
    requester (the seeded `requester`), so `approver` and `admin` are the two who may decide."""
    owner = roles(db)["owner"]
    project = Project(
        name=f"p-{uuid.uuid4().hex[:8]}",
        owner_id=owner,
        team_id=db.query(Team).first().id,
        template_id=db.query(Template).first().id,
    )
    db.add(project)
    db.flush()
    build = Build(project_id=project.id, type="create", status="SUCCEEDED")
    db.add(build)
    db.flush()
    request = ApprovalRequest(
        build_id=build.id,
        project_id=project.id,
        requester_id=owner,
        risk_tier="high",
        status=ApprovalStatus.PENDING,
        required_approvals=required,
    )
    db.add(request)
    db.commit()
    return request.id


def newer_build(db, project_id: str) -> str:
    """The build whose arrival supersedes older PENDING requests (a real row: the superseded
    request's `superseded_by_build_id` is a foreign key)."""
    build = Build(project_id=project_id, type="modify", status="SUCCEEDED")
    db.add(build)
    db.commit()
    return build.id


def approve(db, request_id: str, actor_id: str, *, decision: str = "approve", comment: str | None = None):
    request = db.get(ApprovalRequest, request_id)
    owner = db.get(Project, request.project_id).owner_id
    return decide(db, request, actor_id=actor_id, project_owner_id=owner, decision=decision, comment=comment)


def decision_rows(db, request_id: str, decision: str | None = None) -> int:
    query = db.query(ApprovalDecision).filter_by(approval_request_id=request_id)
    if decision:
        query = query.filter_by(decision=decision)
    return query.count()
