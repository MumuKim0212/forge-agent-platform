from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.build import Build
from app.models.project import Project
from app.models.user import User
from app.schemas.build_trace import BuildTraceOut
from app.services.build_trace import load_trace_view
from app.services.roles import may_read_build_trace

router = APIRouter(prefix="/builds", tags=["builds"])


@router.get("/{build_id}/trace", response_model=BuildTraceOut)
def get_build_trace(
    build_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BuildTraceOut:
    """one build's stored trace, for the
    project's owner and for `approver` and above (`roles.may_read_build_trace`). The
    check runs BEFORE any trace content is loaded: only the build and its project's owner
    are read to decide it. A requester who does not own the project gets 403, and a request
    with no identity gets 401 from `get_current_user`.

    A build with no trace (it never reached the agent, or predates the trace) is a 404:
    nothing was stored and nothing is backfilled. The response labels what was measured
    and what is an agent claim, separately."""
    build = db.get(Build, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="Build not found")
    project = db.get(Project, build.project_id)
    if not may_read_build_trace(current_user, project.owner_id if project is not None else None):
        raise HTTPException(status_code=403, detail="Project owner or approver role required")
    view = load_trace_view(db, build_id)
    if view is None:
        raise HTTPException(status_code=404, detail="This build has no stored trace")
    return view
