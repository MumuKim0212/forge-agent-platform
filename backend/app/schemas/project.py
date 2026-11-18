
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.change_request import NL_REQUEST_MAX_CHARS
from app.schemas.types import UtcDateTime

# see `NL_REQUEST_MAX_CHARS`. The name is shown in lists and audit rows, so it
# gets a bound of its own; rejected, never truncated. Mirrored by the frontend.
PROJECT_NAME_MAX_CHARS = 100


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=PROJECT_NAME_MAX_CHARS)
    template_id: str
    team_id: str
    nl_request: str = Field(min_length=1, max_length=NL_REQUEST_MAX_CHARS)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    owner_id: str
    team_id: str
    template_id: str
    intent_spec: dict | None
    status: str
    repo_url: str | None
    container_id: str | None
    # the last merged (confirmed)
    # ref. `service_env_json` is deliberately NOT exposed here — it carries
    # the project's injected secrets, which are never
    # returned by any API.
    confirmed_ref: str | None
    # `normal` or `eval` (the throwaway project an eval case builds in).
    kind: str
    created_at: UtcDateTime
    last_active_at: UtcDateTime


class DeployControlsOut(BaseModel):
    """What the project's deploy
    controls may offer right now, computed by the same functions the confirm and rollback
    actions use (`services/deploy.deploy_controls`). The state half only: who may act is the
    routes' own 403. The `*_reason` values are `services/deploy.DeployRefusalCode`, the same
    codes the refused request's `detail` starts with."""

    # The one build a confirm would accept; None with `confirm_blocked_reason` otherwise.
    confirmable_build_id: str | None
    confirm_blocked_reason: str | None
    # The SUPERSEDED deployment a rollback would redeploy; None with `rollback_blocked_reason`.
    rollback_target_id: str | None
    rollback_blocked_reason: str | None
    # What a stop would stop.
    active_deployment_id: str | None
    # A build is QUEUED/RUNNING or a deployment is DEPLOYING: the state will change on its own.
    in_flight: bool


class ProjectDetailOut(ProjectOut):
    owner_name: str
    team_name: str
    template_name: str
    # the URL a person opens for the running service (`services/deploy.service_url`):
    # an absolute Traefik URL, or the relative dev route `/api/projects/{id}/service/`. None
    # unless the project has an ACTIVE deployment. Not a secret (the service route carries no
    # identity); computed from settings and the id, never from `service_env_json`.
    service_url: str | None = None
    # filled by `GET /projects/{id}` only (the read the detail page uses); None on the
    # create and un-archive responses, which do not ask Docker anything.
    deploy_controls: DeployControlsOut | None = None


class ProjectDeprovisionRequest(BaseModel):
    """the confirmation-echo body for `POST /projects/{id}/
    deprovision`. `project_id` must equal the path's own project id
    (`_confirms_target_project` in `routers/projects.py`) -- there is no
    typed-confirmation precedent elsewhere in this codebase, and this is
    the first destructive action that needs one."""

    project_id: str = Field(min_length=1)
    delete_backups: bool = False


class TimelineEntry(BaseModel):
    kind: str  # "change_request" | "build" | "audit_log" | "policy_verdict" | "approval_request" | "deployment"
    ts: UtcDateTime
    data: dict
