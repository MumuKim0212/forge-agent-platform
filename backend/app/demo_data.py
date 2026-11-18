"""(a1-a8): a
dev-only, explicitly-invoked command that populates the platform DB with a
plausible pipeline history, so the portal and admin-console screens have
something to render.

**Why this exists.** Nothing in this repository can create a real
`PolicyVerdict`, `ApprovalRequest`, or `Deployment` row outside of pytest:
`routers/projects.py::_initial_build_status` only queues a build when
`use_sandbox_executor` AND a real `anthropic_api_key` are both set (neither
is true in `backend/.env`); the only code path that ever creates a
`PolicyVerdict`/`ApprovalRequest` is `sandbox_build_executor.py`, which only
the worker runs; and `dev.ps1` never starts the worker. So `/approvals`,
`ProjectDetailPage`'s deploy controls, and four of `Timeline`'s six entry
kinds have never rendered for anyone. This command exists to make them
render, on purpose, for review — not to simulate a real build pipeline.

**What this command is not.** It never starts a container, never builds an
image, never talks to Docker, git, or the shared service Postgres. Every
row it writes describes a pipeline that did not run: `image_ref` is a
plausible-looking string nothing will ever pull, `Deployment.status` values
are written directly in their terminal state (never `DEPLOYING` — a
`DEPLOYING` row with no worker running would just sit there, but writing
one anyway would be needless risk for zero benefit), and no `service_env_json`
is ever set (so a live worker, if one were ever started against this DB,
would still refuse promotion via `PROVISION_UNCONFIGURED` before touching
anything). Pressing 중지 against a row this command marks `ACTIVE` calls
`DeployManager.remove_service`, which is best-effort/idempotent against a
container that was never created — harmless, and now written down rather
than merely assumed.

**Idempotency (a1/a3), unchanged by this task.** Coarse, not per-row: a
single marker (`Team.name == DEMO_TEAM_NAME`) gates the whole operation. If
that team already exists, a plain `python -m app.demo_data` is a no-op — it
never re-creates or duplicates rows. This is simpler and safer than
tracking per-entity idempotency across a dozen interrelated tables, and it
matches the one property (a1) actually asks for: safe and predictable to
run more than once with no flag.

**The re-apply path (a2-a4).** The coarse marker above means a change
to this module's own scenario is invisible until the dev DB is dropped and
recreated by hand.
`--reset`
removes the existing demo dataset and repopulates it fresh:

    python -m app.demo_data --reset

`remove_demo_data` is a **traversal of the rows the demo dataset owns**,
never a table-level wipe and never a `TRUNCATE`.

**What the dataset owns.** The demo users, found by the fixed
addresses in `DEMO_USER_EMAILS` -- never by team: `create_project` takes any
`team_id`, so "under the demo team" says nothing about who made a row. A
project is the dataset's only if a demo user owns it; the traversal then walks
to every row those projects/builds/approval-requests own, deleting children
before parents so no foreign key is ever left dangling. A real person's
project under the demo team, and that person, survive; the team row stays too
while anything still references it, and the next population reuses it.
`seed.py`'s shared template (`GENERIC_WEBTOOL_TEMPLATE_NAME`) and its three
seeded users live under a different team (`seed.SEED_TEAM_NAME`) and are never
named by any of these queries.

**Audit rows.** `audit_log` is append-only, so the reset deletes a row only if
EVERY identity in it is the dataset's: the actor is nobody (a worker-driven
row) or a demo user, and the target is a demo entity. A real person's act on a
demo entity is kept (its target then names a thing that no longer exists,
which is only text; the CLI reports how many) and so is every row about a real
entity. If a surviving row still references a demo user (a demo identity was
used on a real project), the reset refuses and changes nothing
(`DemoDataResetBlockedError`) rather than rewrite that row. The prod refusal (a4) is checked in
`remove_demo_data` itself, before it reads or deletes a single row, so
`--reset` against a prod DB refuses exactly as loudly as a plain invocation
does — it never silently degrades to "populate only".

Invocation (a1, a8 — not wired into `dev.ps1` or the app lifespan, both
deliberately): from `backend/`, with the venv active,
`python -m app.demo_data` (or `--reset`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment, DeploymentFailureReason, DeploymentStatus
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.seed import GENERIC_WEBTOOL_TEMPLATE_NAME
from app.services.account_status import AccountStatus
from app.services.approval import create_approval_request_for_verdict, decide
from app.services.audit import write_audit_log
from app.services.policy_checks import CheckResult, PolicyCheckName, PolicyCheckStatus, compute_risk_tier
from app.services.roles import Role
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus

DEMO_TEAM_NAME = "포지 데모팀"

# the demo users, spelled once. `populate_demo_data` creates exactly these and
# `remove_demo_data` finds the dataset by them (never by team): the dataset is whatever these
# six own. `tests/test_demo_data.py` pins the list against what populate creates.
_OWNER1_EMAIL = "demo.owner1@forge.local"
_OWNER2_EMAIL = "demo.owner2@forge.local"
_APPROVER_EMAIL = "demo.approver@forge.local"
_ADMIN_EMAIL = "demo.admin@forge.local"
_PENDING_EMAIL = "demo.pending@forge.local"
_REJECTED_EMAIL = "demo.rejected@forge.local"
DEMO_USER_EMAILS = (
    _OWNER1_EMAIL,
    _OWNER2_EMAIL,
    _APPROVER_EMAIL,
    _ADMIN_EMAIL,
    _PENDING_EMAIL,
    _REJECTED_EMAIL,
)

# agent-chosen strings the demo data uses to exercise the literal, bounded rendering
# of paths: one that is very long, one that looks like markup and a link (it is only text).
_LONG_PATH = (
    "app/" + "/".join(f"generated_module_{i}" for i in range(14)) + "/credentials_loader_with_a_very_long_name.py"
)
_HOSTILE_PATH = "app/<script>alert('x')</script>/<a href='http://evil.example/'>report</a>.html"


# a second template, disabled — distinct from the shared
# `GENERIC_WEBTOOL_TEMPLATE_NAME` (`seed.py` and any real project may
# reference that one; this name exists only for this module and is safe to
# delete on `--reset`).
DEMO_DISABLED_TEMPLATE_NAME = "포지 데모 — 레거시 Flask 템플릿 (사용 중지)"


class ProdDemoDataRefusalError(RuntimeError):
    """Raised when `populate_demo_data` or `remove_demo_data` is invoked
    with `FORGE_ENV=prod` (a1, a4). Deliberately a hard refusal, not
    `seed.py`'s silent skip-the-dev-half-in-prod behaviour: this command's
    whole output is fabricated pipeline history, and a prod DB carrying
    fabricated approvals/deployments — or having real rows swept up by a
    demo-data removal — is a very different mistake than a prod DB merely
    missing optional dev fixtures."""


class DemoDataResetBlockedError(RuntimeError):
    """`--reset` found a row it must keep that still references a demo user (a demo identity
    was used on a real project: a change request, a decision, an audit row about a real entity).
    Deleting the user would fail on the foreign key or rewrite that row, so nothing was deleted;
    the message names what the database refused."""


@dataclass(frozen=True)
class DemoDataSummary:
    """What `populate_demo_data` did, for both the CLI's printed summary and
    the pytest assertions in (a7). Counts are of rows THIS CALL created —
    not a table-wide `COUNT(*)`, which would also include whatever else
    already lives in the DB (the real dev DB already has an unrelated
    project from earlier manual testing)."""

    created: bool
    teams: int = 0
    users: int = 0
    projects: int = 0
    templates: int = 0
    change_requests: int = 0
    builds: int = 0
    policy_verdicts: int = 0
    policy_check_results: int = 0
    approval_requests: int = 0
    approval_decisions: int = 0
    deployments: int = 0
    audit_log_entries: int = 0


@dataclass(frozen=True)
class DemoDataRemovalSummary:
    """What `remove_demo_data` removed (a2), mirroring
    `DemoDataSummary`'s shape so the CLI can print both with the same kind
    of table. `existed=False` means there was no demo dataset to remove —
    itself a no-op, not an error."""

    existed: bool
    teams: int = 0
    users: int = 0
    projects: int = 0
    templates: int = 0
    change_requests: int = 0
    builds: int = 0
    policy_verdicts: int = 0
    policy_check_results: int = 0
    approval_requests: int = 0
    approval_decisions: int = 0
    deployments: int = 0
    audit_log_entries: int = 0
    # audit rows that name a demo entity but were written by a real person (or are about
    # a real entity's actor), which the reset therefore kept.
    audit_log_entries_kept: int = 0


def _all_pass_checks() -> list[CheckResult]:
    return [
        CheckResult(
            check_name=PolicyCheckName.SECRETS_PII,
            result=PolicyCheckStatus.PASS,
            summary="No hardcoded secrets or PII-handling patterns found.",
            findings=[],
        ),
        CheckResult(
            check_name=PolicyCheckName.DEPENDENCY_VULNERABILITIES,
            result=PolicyCheckStatus.PASS,
            summary="No known-vulnerable dependency versions found.",
            findings=[],
        ),
        CheckResult(
            check_name=PolicyCheckName.EGRESS_DETECTION,
            result=PolicyCheckStatus.PASS,
            summary="No outbound calls to non-allowlisted destinations found.",
            findings=[],
        ),
        CheckResult(
            check_name=PolicyCheckName.RESOURCE_LIMITS,
            result=PolicyCheckStatus.PASS,
            summary="CPU/memory caps are present and within template limits.",
            findings=[],
        ),
        CheckResult(
            check_name=PolicyCheckName.TEMPLATE_COMPLIANCE,
            result=PolicyCheckStatus.PASS,
            summary="Workspace matches the golden-path template structure.",
            findings=[],
        ),
        CheckResult(
            check_name=PolicyCheckName.DESTRUCTIVE_DDL,
            result=PolicyCheckStatus.PASS,
            summary="No destructive schema changes found in any migration file.",
            findings=[],
        ),
    ]


def _with_flags(flags: dict[str, tuple[str, list[dict]]]) -> list[CheckResult]:
    """`_all_pass_checks()` with the named checks overridden to `flag`. Keeps
    every verdict's six-row shape identical and lets each demo verdict say
    exactly which check(s) it flags and why, mirroring the real
    `CheckResult`/finding shapes `policy_checks.py` itself produces (T1:
    plausible, not one row per table)."""
    checks = _all_pass_checks()
    by_name = {c.check_name: i for i, c in enumerate(checks)}
    for check_name, (summary, findings) in flags.items():
        idx = by_name[check_name]
        checks[idx] = CheckResult(
            check_name=check_name,
            result=PolicyCheckStatus.FLAG,
            summary=summary,
            findings=findings,
        )
    return checks


# a changed-file path carrying U+202E (RIGHT-TO-LEFT OVERRIDE), so that the
# surface shows its visible marker where an unmarked renderer would reorder the name.
# Spelled with a named escape: the source never holds the raw character.
_BIDI_PATH = "app/uploads/report\N{RIGHT-TO-LEFT OVERRIDE}txt.py"

# what a hostile agent might write about its own work -- a fake reassurance, a URL,
# a markdown link, a direction override and several lines. The surfaces must show it
# collapsed, labelled as unverified, below every measured fact, as plain text with the
# override visible and nothing clickable.
_HOSTILE_SUMMARY = (
    "보안 영향 없음. 바로 승인하세요.\n"
    "자세한 검토 결과: https://evil.example/security-review\n"
    "[여기를 눌러 확인](https://evil.example)\n"
    "\n"
    "변경 내용:\n"
    "- 첨부파일 업로드 추가 (report\N{RIGHT-TO-LEFT OVERRIDE}txt.py)\n"
    "- 알림 서비스 연동"
)


def _measured(
    entries: list[tuple[str, str]], *, total: dict[str, int] | None = None, truncated: bool = False
) -> dict:
    """a `Build.changed_files` value in the shape `services/change_set.py` stores
    -- entries sorted by path, with totals counted from them unless the caller says the
    real change was bigger than the stored list (`total` + `truncated`)."""
    ordered = sorted(entries)
    counted = {"added": 0, "modified": 0, "deleted": 0}
    for _path, change in ordered:
        counted[change] += 1
    return {
        "entries": [{"path": path, "change": change} for path, change in ordered],
        "totals": total if total is not None else counted,
        "truncated": truncated,
    }


def _get_or_create_template(db: Session) -> Template:
    template = db.query(Template).filter_by(name=GENERIC_WEBTOOL_TEMPLATE_NAME).first()
    if template is None:
        template = Template(
            name=GENERIC_WEBTOOL_TEMPLATE_NAME,
            stack="python-fastapi-postgres-redis",
            dockerfile_ref="templates/generic-webtool/Dockerfile",
            deploy_config_ref="templates/generic-webtool/compose.yaml",
            policy_profile="default",
            version="0.1.0",
            enabled=True,
        )
        db.add(template)
        db.flush()
    return template


def _create_disabled_template(db: Session) -> Template:
    """"Templates: a second template, disabled" — `TemplatesPanel`
    has never had a second row, so the empty-vs-populated detail pane and the
    "사용 가능: 아니오" row are both unreachable today. Owned by this module
    (unlike the shared webtool template above): safe for `--reset` to delete
    by name."""
    template = Template(
        name=DEMO_DISABLED_TEMPLATE_NAME,
        stack="python-flask-sqlite",
        dockerfile_ref="templates/legacy-flask/Dockerfile",
        deploy_config_ref="templates/legacy-flask/compose.yaml",
        policy_profile="default",
        version="0.0.1",
        enabled=False,
    )
    db.add(template)
    db.flush()
    return template


def _make_user(
    db: Session,
    *,
    team_id: str,
    name: str,
    email: str,
    role: str,
    status: str = AccountStatus.ACTIVE,
    department: str | None = None,
    created_at: datetime | None = None,
) -> User:
    kwargs: dict = {
        "team_id": team_id,
        "name": name,
        "email": email,
        "role": role,
        "status": status,
        "department": department,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    user = User(**kwargs)
    db.add(user)
    db.flush()
    return user


def _make_build(
    db: Session,
    *,
    project: Project,
    build_type: str,
    image_ref: str | None,
    created_at: datetime,
    cost_tokens: int | None,
    cost_usd: float | None,
    duration_minutes: int | None = None,
    ended_at: datetime | None = None,
    pr_number: int | None = None,
    preview_url: str | None = None,
    preview_expires_at: datetime | None = None,
    failure_reason: str | None = None,
    failure_detail: str | None = None,
    changed_files: dict | None = None,
    protected_path_drift: list[str] | None = None,
    smoke_checks: dict | None = None,
    agent_summary: str | None = None,
) -> Build:
    """The `Build` row alone, with **no** `ChangeRequest` — factored out of
    `_make_change_request_and_build` below so a build can exist without one
    (a6: "at least one build with no ChangeRequest, so the
    unattributed line is non-zero" — every build this module wrote used to
    have a `ChangeRequest`, which is exactly why that line was
    always exactly $0.00)."""
    if ended_at is None:
        if duration_minutes is None:
            raise ValueError("_make_build needs either ended_at or duration_minutes")
        ended_at = created_at + timedelta(minutes=duration_minutes)
    diff_ref = (
        f"https://git.internal.forge.local/demo/{project.id[:8]}/pull/{pr_number}#{pr_number}"
        if pr_number is not None
        else None
    )
    build = Build(
        project_id=project.id,
        type=build_type,
        # a build with a failure reason is FAILED and carries the
        # reason + detail a real failure would; everything else is SUCCEEDED
        # exactly as before.
        status=BuildStatus.FAILED if failure_reason is not None else BuildStatus.SUCCEEDED,
        failure_reason=failure_reason,
        failure_detail=failure_detail,
        # NULL (the default) is "not measured", which a build that never reached
        # harvest, or one from before the measurement existed, shows as nothing.
        changed_files=changed_files,
        protected_path_drift=protected_path_drift,
        # NULL (the default) is "not measured" -- most demo builds have no preview.
        smoke_checks=smoke_checks,
        # the agent's own text; only a SUCCEEDED build has one.
        agent_summary=None if failure_reason is not None else agent_summary,
        diff_ref=diff_ref,
        image_ref=image_ref,
        started_at=created_at,
        ended_at=ended_at,
        cost_tokens=cost_tokens,
        cost_usd=cost_usd,
        # r2: most demo builds carry no preview link at all, matching
        # (a7)'s "no preview exists anywhere in this data". The one build
        # that sets it (project 2's still-pending one, below) points at a
        # `.invalid` host (RFC 2606: guaranteed never to resolve).
        preview_url=preview_url,
        preview_expires_at=preview_expires_at,
        created_at=created_at,
    )
    db.add(build)
    db.flush()
    return build


def _make_change_request_and_build(
    db: Session,
    *,
    project: Project,
    requester: User,
    nl_request: str,
    build_type: str,
    pr_number: int | None,
    image_ref: str | None,
    created_at: datetime,
    duration_minutes: int,
    cost_tokens: int | None,
    cost_usd: float | None,
    preview_url: str | None = None,
    preview_expires_at: datetime | None = None,
    failure_reason: str | None = None,
    failure_detail: str | None = None,
    changed_files: dict | None = None,
    protected_path_drift: list[str] | None = None,
    smoke_checks: dict | None = None,
    agent_summary: str | None = None,
) -> tuple[ChangeRequest, Build]:
    build = _make_build(
        db,
        project=project,
        build_type=build_type,
        image_ref=image_ref,
        created_at=created_at,
        cost_tokens=cost_tokens,
        cost_usd=cost_usd,
        duration_minutes=duration_minutes,
        pr_number=pr_number,
        preview_url=preview_url,
        preview_expires_at=preview_expires_at,
        failure_reason=failure_reason,
        failure_detail=failure_detail,
        changed_files=changed_files,
        protected_path_drift=protected_path_drift,
        smoke_checks=smoke_checks,
        agent_summary=agent_summary,
    )

    change_request = ChangeRequest(
        project_id=project.id,
        requester_id=requester.id,
        nl_request=nl_request,
        build_id=build.id,
        created_at=created_at,
    )
    db.add(change_request)
    db.flush()
    return change_request, build


def _make_verdict(
    db: Session, *, build: Build, checks: list[CheckResult], created_at: datetime
) -> PolicyVerdict:
    tier = compute_risk_tier(
        checks,
        medium_tier_checks=settings.policy_medium_tier_checks_list,
        high_tier_checks=settings.policy_high_tier_checks_list,
    )
    verdict = PolicyVerdict(build_id=build.id, risk_tier=tier, created_at=created_at)
    db.add(verdict)
    db.flush()
    for check in checks:
        db.add(
            PolicyCheckResult(
                verdict_id=verdict.id,
                check_name=check.check_name,
                result=check.result,
                summary=check.summary,
                findings=check.findings,
            )
        )
    db.flush()
    return verdict


def _make_approval_request(
    db: Session,
    *,
    build: Build,
    verdict: PolicyVerdict,
    project: Project,
    requester: User,
    created_at: datetime,
):
    """Wraps the real `services.approval.create_approval_request_for_verdict`
    (T1: correct status-on-tier, correct `required_approvals` snapshot,
    correct auto-supersede-older-PENDING behaviour, and a real audit log —
    reusing it rather than re-deriving those rules by hand) and then
    backdates only the row's own `created_at`. The function's internal
    audit-log writes are left at the real wall-clock time deliberately: they
    target `approval_request:<id>`, which `GET /projects/{id}/timeline`
    never surfaces (only `project:<id>` and `deployment:<id>` targets are),
    so their timestamp is never user-visible."""
    request = create_approval_request_for_verdict(
        db, build=build, verdict=verdict, project_id=project.id, requester_id=requester.id
    )
    request.created_at = created_at
    return request


def _decide(db: Session, *, request, actor: User, project: Project, decision: str, comment: str | None, ts: datetime):
    """Wraps the real `services.approval.decide` the same way — correct
    self-approval ban, correct terminal-status transition, correct
    required-approvals gate — then backdates the new `ApprovalDecision` row.
    See `_make_approval_request`'s docstring for why the function's own
    audit-log write is left at wall-clock time."""
    request = decide(
        db,
        request,
        actor_id=actor.id,
        project_owner_id=project.owner_id,
        decision=decision,
        comment=comment,
    )
    request.decisions[-1].created_at = ts
    return request


def _audit(db: Session, *, actor_id: str | None, action: str, target: str, metadata: dict, ts: datetime) -> None:
    entry = write_audit_log(db, actor_id=actor_id, action=action, target=target, metadata=metadata)
    entry.ts = ts


def _make_deployment(
    db: Session,
    *,
    project: Project,
    build: Build,
    confirmed_by: User,
    status: str,
    ts: datetime,
    rollback_of: Deployment | None = None,
    supersedes: Deployment | None = None,
    failure_reason: str | None = None,
) -> Deployment:
    """Creates one `Deployment` row **directly in its final, terminal
    status** — never `DEPLOYING` (a2: a `DEPLOYING` row is this table's
    worker-claim queue, per `services/deploy.claim_next_deployment`; nothing
    in this command starts a worker, but writing one anyway would be
    needless risk for a purely cosmetic gain). Writes the same
    `deployment.*` audit actions/targets `services/deploy.py` itself would,
    at plausible backdated timestamps, so the audit_log-kind timeline entry
    (a5) reads like a real history rather than a single flat write."""
    activated_at = ts + timedelta(seconds=45)
    ended_at = ts + timedelta(minutes=2) if status != DeploymentStatus.ACTIVE else None

    # Mirrors `services/deploy.py::_activate`: the old ACTIVE row must leave
    # ACTIVE (flushed) BEFORE the new row can become it — the partial
    # unique index (`uq_deployments_one_active_per_project`) is enforced
    # per statement, so inserting the new ACTIVE row first, even inside the
    # same still-uncommitted transaction, would have both rows ACTIVE at
    # once at flush time and raise an IntegrityError.
    if status == DeploymentStatus.ACTIVE and supersedes is not None:
        supersedes.status = DeploymentStatus.SUPERSEDED
        supersedes.ended_at = activated_at
        db.flush()

    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=confirmed_by.id,
        rollback_of=rollback_of.id if rollback_of else None,
        status=status,
        failure_reason=failure_reason,
        started_at=ts,
        ended_at=ended_at,
        created_at=ts,
    )
    db.add(deployment)
    db.flush()

    if rollback_of is not None:
        _audit(
            db,
            actor_id=confirmed_by.id,
            action="deployment.rollback_requested",
            target=f"deployment:{deployment.id}",
            metadata={"project_id": project.id, "build_id": build.id, "rollback_of": rollback_of.id},
            ts=ts,
        )
    elif status == DeploymentStatus.STOPPED:
        # a stop never goes through the confirm/merge pair below
        # (`services/deploy.py::stop_service` flips an already-ACTIVE row's
        # status in place and writes exactly one `deployment.stopped` entry
        # — matched here rather than reusing the create/merge shape, which
        # would misdescribe a stop as a fresh confirm).
        _audit(
            db,
            actor_id=confirmed_by.id,
            action="deployment.stopped",
            target=f"deployment:{deployment.id}",
            metadata={"project_id": project.id},
            ts=ts,
        )
    else:
        _audit(
            db,
            actor_id=confirmed_by.id,
            action="deployment.confirmed",
            target=f"deployment:{deployment.id}",
            metadata={"project_id": project.id, "build_id": build.id},
            ts=ts,
        )
        _audit(
            db,
            actor_id=None,
            action="deployment.merged",
            target=f"project:{project.id}",
            metadata={"build_id": build.id, "confirmed_ref": f"demo-merge-{build.id[:8]}"},
            ts=ts + timedelta(seconds=5),
        )

    if status == DeploymentStatus.FAILED:
        _audit(
            db,
            actor_id=None,
            action="deployment.failed",
            target=f"deployment:{deployment.id}",
            metadata={"project_id": project.id, "build_id": build.id, "reason": failure_reason},
            ts=activated_at,
        )
    elif status == DeploymentStatus.ACTIVE:
        if supersedes is not None:
            # Status/ended_at were already flipped and flushed above, before
            # the new row was inserted -- only the audit entry is written
            # here.
            _audit(
                db,
                actor_id=None,
                action="deployment.superseded",
                target=f"deployment:{supersedes.id}",
                metadata={"project_id": project.id, "superseded_by": deployment.id},
                ts=activated_at,
            )
        _audit(
            db,
            actor_id=None,
            action="deployment.activated",
            target=f"deployment:{deployment.id}",
            metadata={"project_id": project.id, "build_id": build.id},
            ts=activated_at,
        )

    db.flush()
    return deployment


def _current_month_floor(now: datetime) -> datetime:
    """The first instant of `now`'s calendar month, in UTC. Always `<= now`
    (a timestamp cannot precede the 1st of its own month), so anchoring an
    enrichment build's `ended_at` here guarantees it falls inside the
    current month's usage window regardless of which day of the month this
    command happens to run on (T2: "a dataset that renders well on the 25th
    and renders empty on the 2nd is a dataset that will waste a review
    pass"). `services/usage.py` keys the month window on `Build.ended_at`,
    never on when the command was run, so this is the one timestamp that
    actually matters for that rule."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def populate_demo_data(db: Session, *, is_prod: bool | None = None) -> DemoDataSummary:
    """(a1) Refuses outright on a prod DB rather than skipping silently.
    (a3) Idempotent: a no-op if a demo user already exists (this was the demo team,
    but `--reset` may leave that team in place for a real person's project to keep referencing,
    so the team can exist without a dataset; the demo users are the marker now, and an existing
    team is reused).
    (a6): see the module docstring and the task's `## Result` for the exact
    scenario this builds, and which branch each enrichment exists for."""
    if is_prod is None:
        is_prod = settings.is_prod
    if is_prod:
        raise ProdDemoDataRefusalError(
            "populate_demo_data refuses to run with FORGE_ENV=prod: every row "
            "it writes is fabricated pipeline history (fake approvals, fake "
            "deployments), which must never exist in a real environment. "
            "This command is dev-only by design."
        )

    if db.query(User.id).filter(User.email.in_(DEMO_USER_EMAILS)).first() is not None:
        return DemoDataSummary(created=False)

    # Counted by an actual before/after delta, not hand-tallied: the reused
    # `create_approval_request_for_verdict`/`decide` calls below write their
    # own audit rows too (`approval_request.created`/`superseded`/`approved`/
    # `rejected`, targeting `approval_request:<id>`) on top of the
    # `deployment.*`/`project:<id>` ones this module writes directly, and a
    # hand-tally that only counted the latter undercounted the real total by
    # exactly that many rows on first measurement against the live dev DB —
    # caught by comparing this function's own printed summary against a
    # direct `SELECT COUNT(*)` before trusting either number.
    audit_log_count_before = db.query(AuditLog).count()

    now = datetime.now(timezone.utc)
    month_start = _current_month_floor(now)

    template = _get_or_create_template(db)
    _create_disabled_template(db)

    team = db.query(Team).filter_by(name=DEMO_TEAM_NAME).first()
    team_created = team is None
    if team is None:
        team = Team(name=DEMO_TEAM_NAME)
        db.add(team)
        db.flush()

    owner1 = _make_user(db, team_id=team.id, name="김도윤", email=_OWNER1_EMAIL, role=Role.REQUESTER)
    owner2 = _make_user(db, team_id=team.id, name="최수아", email=_OWNER2_EMAIL, role=Role.REQUESTER)
    approver = _make_user(db, team_id=team.id, name="이서준", email=_APPROVER_EMAIL, role=Role.APPROVER)
    admin = _make_user(db, team_id=team.id, name="박지민", email=_ADMIN_EMAIL, role=Role.ADMIN)

    # "users in PENDING and in a rejected/deactivated state, so
    # 대기중 and 거부됨 are non-empty" — every demo user before this task was
    # `ACTIVE` (`_make_user`'s default, matching `User.status`'s own column
    # default), so both of `SignupRequestsPanel`'s non-active tabs were
    # permanently empty. `AccountStatus.REJECTED` covers both a rejected
    # signup and a deactivated account (`services/signup.py`'s transition
    # table collapses them to the same value); this row stands in for
    # either reading, since the panel cannot distinguish them from the
    # status alone either.
    pending_user = _make_user(
        db,
        team_id=team.id,
        name="이신입",
        email=_PENDING_EMAIL,
        role=Role.REQUESTER,
        status=AccountStatus.PENDING,
        department="백엔드개발2팀",
        created_at=now - timedelta(days=1, hours=3),
    )
    rejected_user = _make_user(
        db,
        team_id=team.id,
        name="정탈퇴",
        email=_REJECTED_EMAIL,
        role=Role.REQUESTER,
        status=AccountStatus.REJECTED,
        department="재무회계팀",
        created_at=now - timedelta(days=10),
    )

    # ------------------------------------------------------------------
    # Project 1: "사내 헬프데스크" -- the rich scenario. Four builds across
    # three risk tiers, four approval-request statuses (AUTO_APPROVED,
    # SUPERSEDED, REJECTED, APPROVED), and a deployment history covering
    # ACTIVE / a superseded row / a failed row / a rollback -- all in one
    # project, so (a4)'s full list and (a5)'s "all six kinds, one project"
    # are both satisfied here.
    # ------------------------------------------------------------------
    project1 = Project(
        name="사내 헬프데스크",
        owner_id=owner1.id,
        team_id=team.id,
        template_id=template.id,
        status=ProjectStatus.ACTIVE,
        last_active_at=now - timedelta(days=1),
        # an override (not zero) — this project's own spend
        # (~$4.70 across its four builds, below) exceeds it, so a breach
        # actually renders.
        monthly_quota_usd=2.0,
    )
    db.add(project1)
    db.flush()

    # B1 (create): all checks pass -> LOW -> AUTO_APPROVED.
    t_b1 = now - timedelta(days=20)
    _cr1, b1 = _make_change_request_and_build(
        db,
        project=project1,
        requester=owner1,
        nl_request="사내 문의를 접수하고 상태를 추적할 수 있는 헬프데스크 도구를 만들어줘",
        build_type="create",
        pr_number=1,
        image_ref="forge-build-helpdesk-1",
        created_at=t_b1,
        duration_minutes=6,
        cost_tokens=48213,
        cost_usd=1.42,
        # a create build -- everything is new; the agent left the manifests alone.
        changed_files=_measured(
            [
                (f"app/{name}", "added")
                for name in ("app.py", "models.py", "routes.py", "templates/index.html", "static/app.css", "tests/test_app.py")
            ]
        ),
        protected_path_drift=[],
        agent_summary=(
            "헬프데스크 도구를 만들었습니다.\n"
            "- 문의 접수 폼과 상태 추적 목록\n"
            "- 담당자 지정과 상태 변경 이력"
        ),
    )
    v1 = _make_verdict(db, build=b1, checks=_all_pass_checks(), created_at=b1.ended_at)
    ar1 = _make_approval_request(
        db, build=b1, verdict=v1, project=project1, requester=owner1, created_at=v1.created_at
    )

    # D1: first confirm of B1 fails health check. D2: retry succeeds (ACTIVE
    # for now -- superseded later when B4 deploys).
    t_d1 = t_b1 + timedelta(hours=2)
    d1 = _make_deployment(
        db,
        project=project1,
        build=b1,
        confirmed_by=owner1,
        status=DeploymentStatus.FAILED,
        ts=t_d1,
        failure_reason=DeploymentFailureReason.HEALTH_TIMEOUT,
    )
    t_d2 = t_d1 + timedelta(minutes=15)
    d2 = _make_deployment(
        db, project=project1, build=b1, confirmed_by=owner1, status=DeploymentStatus.ACTIVE, ts=t_d2
    )

    # B2 (modify): dependency_vulnerabilities flagged -> MEDIUM -> PENDING.
    # Never decided -- superseded when B3's request is created below.
    t_b2 = now - timedelta(days=14)
    _cr2, b2 = _make_change_request_and_build(
        db,
        project=project1,
        requester=owner1,
        nl_request="문의 목록에 검색 기능을 추가해줘",
        build_type="modify",
        pr_number=2,
        image_ref="forge-build-helpdesk-2",
        created_at=t_b2,
        duration_minutes=5,
        cost_tokens=31870,
        cost_usd=0.91,
        changed_files=_measured(
            [("app/routes.py", "modified"), ("app/templates/index.html", "modified"), ("app/search.py", "added")]
        ),
        protected_path_drift=[],
    )
    v2 = _make_verdict(
        db,
        build=b2,
        checks=_with_flags(
            {
                PolicyCheckName.DEPENDENCY_VULNERABILITIES: (
                    "Found 1 known-vulnerable dependency pin.",
                    [
                        {
                            "type": "vulnerable_dependency",
                            "package": "requests",
                            "pinned_version": "2.25.0",
                            "advisory": "CVE-2023-32681",
                        }
                    ],
                )
            }
        ),
        created_at=b2.ended_at,
    )
    ar2 = _make_approval_request(
        db, build=b2, verdict=v2, project=project1, requester=owner1, created_at=v2.created_at
    )

    # B3 (modify): secrets_pii + resource_limits flagged -> HIGH -> PENDING,
    # then REJECTED. Creating this request auto-supersedes AR2 (still
    # PENDING) -- the real `create_approval_request_for_verdict` behaviour.
    t_b3 = now - timedelta(days=9)
    _cr3, b3 = _make_change_request_and_build(
        db,
        project=project1,
        requester=owner1,
        nl_request="첨부파일 업로드 기능을 추가해줘",
        build_type="modify",
        pr_number=3,
        image_ref="forge-build-helpdesk-3",
        created_at=t_b3,
        duration_minutes=7,
        cost_tokens=52310,
        cost_usd=1.58,
        # the agent tried to edit the Dockerfile (restored, reported as drift) and
        # added a file with a very long path, to see the bounded, literal rendering.
        changed_files=_measured(
            [
                ("Dockerfile", "modified"),
                ("app/services/notify.py", "added"),
                ("app/uploads.py", "added"),
                ("app/routes.py", "modified"),
                (_LONG_PATH, "added"),
                # a right-to-left override in a file name -- shown with its visible
                # marker, not as the different name it would otherwise read as.
                (_BIDI_PATH, "added"),
            ]
        ),
        protected_path_drift=["Dockerfile"],
        agent_summary=_HOSTILE_SUMMARY,
    )
    v3 = _make_verdict(
        db,
        build=b3,
        checks=_with_flags(
            {
                PolicyCheckName.SECRETS_PII: (
                    "Secrets/PII scan found 2 findings in the workspace.",
                    [
                        {
                            "type": "hardcoded_secret",
                            "pattern": "generic_api_key_assignment",
                            "path": "app/services/notify.py",
                            "match": "sk-t***REDACTED***",
                        },
                        {
                            "type": "hardcoded_secret",
                            "pattern": "aws_access_key_id",
                            "path": _LONG_PATH,
                            "match": "AKIA***REDACTED***",
                        },
                    ],
                ),
                PolicyCheckName.RESOURCE_LIMITS: (
                    "compose.yaml is missing a memory cap for the web service.",
                    [{"type": "missing_resource_limit", "service": "web", "field": "mem_limit"}],
                ),
            }
        ),
        created_at=b3.ended_at,
    )
    ar3 = _make_approval_request(
        db, build=b3, verdict=v3, project=project1, requester=owner1, created_at=v3.created_at
    )
    ar2.superseded_at = v3.created_at  # cosmetic backdate only; not rendered anywhere (see docstring above)
    t_decide3 = t_b3 + timedelta(days=1)
    ar3 = _decide(
        db,
        request=ar3,
        actor=approver,
        project=project1,
        decision="reject",
        comment="보안 점검에서 지적된 시크릿 하드코딩을 먼저 해결해 주세요. 재작업 후 다시 제출 부탁드립니다.",
        ts=t_decide3,
    )

    # B4 (modify): template_compliance flagged -> MEDIUM -> PENDING, then
    # APPROVED. This is (a4)'s "SUCCEEDED build + APPROVED approval request"
    # -- and, being the latest build in this project, it leaves the 배포
    # 확정 button reachable even after D4 (below) rolls the active
    # deployment back to B1's image.
    t_b4 = now - timedelta(days=5)
    _cr4, b4 = _make_change_request_and_build(
        db,
        project=project1,
        requester=owner1,
        nl_request="첨부파일 업로드 기능을 다시 검토해서 추가하고, 목록에 담당자 필드도 넣어줘",
        build_type="modify",
        pr_number=4,
        image_ref="forge-build-helpdesk-4",
        created_at=t_b4,
        duration_minutes=4,
        cost_tokens=27650,
        cost_usd=0.79,
        # more files than the approval detail shows (50), so its "외 N개" line renders.
        changed_files=_measured(
            [(f"app/attachments/part_{i:02d}.py", "added") for i in range(55)]
            + [("app/models.py", "modified"), ("app/routes.py", "modified"), ("app/templates/index.html", "modified")]
            + [("app/scratch.py", "added"), ("app/old_helper.py", "deleted"), ("app/static/legacy.css", "deleted")]
            + [("docs/NOTES.md", "added"), ("app/forms.py", "added")]
        ),
        protected_path_drift=[],
    )
    v4 = _make_verdict(
        db,
        build=b4,
        checks=_with_flags(
            {
                PolicyCheckName.TEMPLATE_COMPLIANCE: (
                    "Workspace has 1 file outside the golden-path template structure.",
                    [{"type": "unexpected_file", "path": "app/scratch.py"}],
                )
            }
        ),
        created_at=b4.ended_at,
    )
    ar4 = _make_approval_request(
        db, build=b4, verdict=v4, project=project1, requester=owner1, created_at=v4.created_at
    )
    t_decide4 = t_b4 + timedelta(hours=3)
    ar4 = _decide(
        db, request=ar4, actor=approver, project=project1, decision="approve", comment=None, ts=t_decide4
    )

    # D3: B4 goes live, superseding D2. D4: an admin rolls back to the
    # previous good deployment (D2's build, B1) after a problem is found --
    # the rollback becomes the final ACTIVE row, superseding D3.
    t_d3 = t_decide4 + timedelta(hours=1)
    d3 = _make_deployment(
        db, project=project1, build=b4, confirmed_by=owner1, status=DeploymentStatus.ACTIVE, ts=t_d3, supersedes=d2
    )
    t_d4 = t_d3 + timedelta(days=2)
    d4 = _make_deployment(
        db,
        project=project1,
        build=b1,
        confirmed_by=admin,
        status=DeploymentStatus.ACTIVE,
        ts=t_d4,
        rollback_of=d2,
        supersedes=d3,
    )

    project1.last_active_at = t_d4

    # ------------------------------------------------------------------
    # Project 2: "사내 설문조사 도구" -- the complementary, simpler scenario:
    # one SUCCEEDED build, a HIGH-tier verdict, and an approval request
    # left genuinely PENDING (nobody decides it) -- the live "대기 중" item
    # in /approvals, and a project with no deployment history at all (the
    # empty 배포 이력 state Project 1 never shows).
    # ------------------------------------------------------------------
    project2 = Project(
        name="사내 설문조사 도구",
        owner_id=owner2.id,
        team_id=team.id,
        template_id=template.id,
        status=ProjectStatus.AWAITING_APPROVAL,
        last_active_at=now - timedelta(days=2),
        # an override of exactly 0 — "any spend is a breach"
        #, and this project has real
        # spend (below), so the breach renders, not just the zero label.
        monthly_quota_usd=0.0,
    )
    db.add(project2)
    db.flush()

    t_b5 = now - timedelta(days=2)
    _cr5, b5 = _make_change_request_and_build(
        db,
        project=project2,
        requester=owner2,
        nl_request="설문을 만들고 응답을 모을 수 있는 도구를 만들어줘",
        build_type="create",
        pr_number=1,
        image_ref="forge-build-survey-1",
        created_at=t_b5,
        duration_minutes=5,
        cost_tokens=39820,
        cost_usd=1.10,
        # r2: this is the request the demo data leaves genuinely
        # PENDING (ar5, below) -- the one row `ApprovalConsolePage`'s list
        # can actually put a preview link on, which is exactly the
        # nested-interactive-content case `ListRow`'s `shouldActivateRow`
        # target guard exists to protect.
        preview_url="https://forge-preview-survey-1.invalid/",
        preview_expires_at=t_b5 + timedelta(minutes=30),
        # the pending request an approver opens. More changed than the stored list
        # holds (500 of 640), a path that looks like markup and a URL (shown literally), and
        # drift on both protected manifests.
        changed_files=_measured(
            [(f"app/generated/form_{i:03d}.py", "added") for i in range(498)]
            + [(_HOSTILE_PATH, "added"), ("app/services/responses.py", "modified")],
            total={"added": 600, "modified": 38, "deleted": 2},
            truncated=True,
        ),
        protected_path_drift=["Dockerfile", "compose.yaml"],
        # the platform called this preview's own GET routes: two answered, one failed
        # on the server, one did not answer, one body was over the read limit, and one route
        # was left out (it needs a path parameter). The other demo builds carry NULL.
        smoke_checks={
            "routes": [
                {"path": "/", "status": 200, "outcome": "ok"},
                {"path": "/healthz", "status": 200, "outcome": "ok"},
                {"path": "/responses", "status": 500, "outcome": "server_error"},
                {"path": "/slow", "status": None, "outcome": "no_answer"},
                {"path": "/export", "status": 200, "outcome": "too_large"},
            ],
            "skipped": 1,
            "truncated": False,
        },
    )
    v5 = _make_verdict(
        db,
        build=b5,
        checks=_with_flags(
            {
                PolicyCheckName.SECRETS_PII: (
                    "Secrets/PII scan found 1 finding in the workspace.",
                    [
                        {
                            "type": "pii_pattern",
                            "pattern": "email_collection_hint",
                            "path": "app/services/responses.py",
                            "match": "coll***REDACTED***",
                        }
                    ],
                ),
                PolicyCheckName.EGRESS_DETECTION: (
                    "Found 1 outbound call to a non-allowlisted destination.",
                    [
                        {
                            "type": "unallowlisted_egress",
                            "path": "app/services/notify.py",
                            "destination": "https://webhook.example-external.com/notify",
                        }
                    ],
                ),
            }
        ),
        created_at=b5.ended_at,
    )
    ar5 = _make_approval_request(
        db, build=b5, verdict=v5, project=project2, requester=owner2, created_at=v5.created_at
    )

    # usage/month-window enrichment, all three attached to
    # project2 so project1's already-asserted six-timeline-kinds scenario
    # is untouched.
    #
    # b_unattributed carries **no ChangeRequest** -- every build until this
    # task had one, which is exactly why the unattributed line
    # (`services/usage.py`'s `subject_id is None` row) was always exactly
    # $0.00. Anchored to the current month (see `_current_month_floor`).
    b_unattributed = _make_build(
        db,
        project=project2,
        build_type="modify",
        image_ref="forge-build-survey-2",
        created_at=min(month_start + timedelta(hours=3), now),
        duration_minutes=3,
        cost_tokens=18420,
        cost_usd=2.35,
    )

    # b_unmeasured has `cost_usd IS NULL` -- "unmeasured", never a measured
    # $0.00. Attributed (has
    # a ChangeRequest) so the per-user rollup shows the same distinction,
    # not only the platform/project one. Also anchored to the current
    # month -- with b_unattributed above, that is the two builds (a6) asks
    # for "inside the current month on any day it is run".
    _cr_unmeasured, b_unmeasured = _make_change_request_and_build(
        db,
        project=project2,
        requester=owner2,
        nl_request="응답 결과를 CSV로 내보낼 수 있게 해줘",
        build_type="modify",
        pr_number=2,
        image_ref="forge-build-survey-3",
        created_at=min(month_start + timedelta(hours=9), now),
        duration_minutes=4,
        cost_tokens=None,
        cost_usd=None,  # never reached the LLM proxy's usage accounting
    )

    # b_prev falls in the **previous** calendar month, always -- three days
    # before this month started is never anything but last month, whatever
    # month this command happens to run in. Gives the month selector a
    # second populated option (a6).
    _cr_prev, b_prev = _make_change_request_and_build(
        db,
        project=project2,
        requester=owner2,
        nl_request="설문 응답에 별점 문항을 추가해줘",
        build_type="modify",
        pr_number=3,
        image_ref="forge-build-survey-4",
        created_at=month_start - timedelta(days=3),
        duration_minutes=5,
        cost_tokens=22110,
        cost_usd=1.75,
    )

    # ------------------------------------------------------------------
    # Projects 3-4: deployment-health spread only (a6). Of
    # `deploymentHealth.ts`'s nine states, projects 1-2 above only ever
    # reach SERVING (project1, via D4) and NEVER_DEPLOYED (project2, no
    # deployment) -- two states. Two more small, standalone projects push
    # that to four without contorting either existing scenario, and
    # without ever writing DEPLOYING (a2/T2's rule, unchanged).
    # ------------------------------------------------------------------
    project3 = Project(
        name="사내 회의실 예약 현황판",
        owner_id=owner1.id,
        team_id=team.id,
        template_id=template.id,
        status=ProjectStatus.ARCHIVED,
        last_active_at=now - timedelta(days=30),
    )
    db.add(project3)
    db.flush()
    t_b6 = now - timedelta(days=45)
    b6 = _make_build(
        db,
        project=project3,
        build_type="create",
        image_ref="forge-build-roombook-1",
        created_at=t_b6,
        duration_minutes=5,
        cost_tokens=21040,
        cost_usd=0.61,
    )
    t_d5 = t_b6 + timedelta(hours=1)
    # Written directly in STOPPED (never DEPLOYING, per a2's rule; never
    # ACTIVE-then-flipped either, since `_make_deployment` has a dedicated
    # STOPPED audit branch -- see its own docstring) -- a service an
    # operator has since stopped, distinct from one that never deployed.
    d5 = _make_deployment(
        db, project=project3, build=b6, confirmed_by=owner1, status=DeploymentStatus.STOPPED, ts=t_d5
    )

    project4 = Project(
        name="사내 명함 신청 도구",
        owner_id=owner2.id,
        team_id=team.id,
        template_id=template.id,
        # seed data sets `status` by hand (the one file allowed to; every
        # other writer is pinned by tests/test_project_status.py), so each value
        # is what the pipeline WOULD have left behind -- here builds were queued
        # and ran but none reached a preview, so BUILD_PENDING.
        # tests/test_demo_data.py checks all four against the backfill's rule.
        status=ProjectStatus.BUILD_PENDING,
        last_active_at=now - timedelta(days=6),
    )
    db.add(project4)
    db.flush()
    # two FAILED builds on this project, so the timeline's failed-build
    # lines can be reviewed on the real screen. The details are literal copies
    # of the platform's own templates (sandbox_build_executor.py,
    # build_queue.sweep_timed_out_builds) -- never agent text -- and neither
    # build produced a PR or an image. The policy refusal reached the proxy's
    # usage accounting (small cost); the timeout never did (unmeasured).
    _make_change_request_and_build(
        db,
        project=project4,
        requester=owner2,
        nl_request="명함 신청서에 첨부파일 업로드 기능을 넣어줘",
        build_type="create",
        pr_number=None,
        image_ref=None,
        created_at=now - timedelta(days=8),
        duration_minutes=3,
        cost_tokens=4210,
        cost_usd=0.12,
        failure_reason=BuildFailureReason.AGENT_ERROR,
        failure_detail=(
            "1 request was refused by tool policy: "
            "request field 'mcp_servers' is not allowed"
        ),
    )
    _make_change_request_and_build(
        db,
        project=project4,
        requester=owner2,
        nl_request="신청 내역을 팀별로 집계해서 보여줘",
        build_type="modify",
        pr_number=None,
        image_ref=None,
        created_at=now - timedelta(days=7),
        duration_minutes=settings.build_max_duration_minutes + 1,
        cost_tokens=None,
        cost_usd=None,
        failure_reason=BuildFailureReason.TIMEOUT,
        failure_detail=(
            "the build did not finish within the maximum build duration of "
            f"{settings.build_max_duration_minutes} minutes"
        ),
    )
    # a modify build the platform REFUSED for changing the database schema: its
    # failure detail is the platform's sentence (a count and the declared paths, no file
    # name), while the changed-file list it recorded shows what the agent actually tried.
    _make_change_request_and_build(
        db,
        project=project4,
        requester=owner2,
        nl_request="신청 내역에 부서 컬럼을 추가해줘",
        build_type="modify",
        pr_number=None,
        image_ref=None,
        created_at=now - timedelta(days=6, hours=6),
        duration_minutes=4,
        cost_tokens=19320,
        cost_usd=0.51,
        failure_reason=BuildFailureReason.AGENT_ERROR,
        failure_detail=(
            "the agent changed the database schema on a self-modify build "
            "(1 file(s) under app/migrations/, app/alembic.ini); a schema change is not "
            "allowed on a self-modify build and must take the developer path"
        ),
        changed_files=_measured(
            [
                ("app/migrations/versions/20261001_add_department.py", "added"),
                ("app/models.py", "modified"),
                ("app/routes.py", "modified"),
            ]
        ),
        protected_path_drift=[],
    )
    t_b7 = now - timedelta(days=6)
    b7 = _make_build(
        db,
        project=project4,
        build_type="create",
        image_ref="forge-build-cardreq-1",
        created_at=t_b7,
        duration_minutes=4,
        cost_tokens=15870,
        cost_usd=0.44,
    )
    t_d6 = t_b7 + timedelta(hours=2)
    d6 = _make_deployment(
        db,
        project=project4,
        build=b7,
        confirmed_by=owner2,
        status=DeploymentStatus.FAILED,
        ts=t_d6,
        failure_reason=DeploymentFailureReason.START_FAILED,
    )

    db.commit()

    return DemoDataSummary(
        created=True,
        teams=1 if team_created else 0,
        users=6,
        projects=4,
        templates=1,
        change_requests=10,
        builds=13,
        policy_verdicts=5,
        policy_check_results=5 * 6,
        approval_requests=5,
        approval_decisions=2,  # ar3 (reject) + ar4 (approve); ar1 auto-approved, ar2 superseded, ar5 still pending
        deployments=6,
        # Actual before/after delta (see the comment above `audit_log_count_
        # before`) — covers both this module's own `deployment.*`/
        # `project:<id>` writes AND the reused approval-service functions'
        # `approval_request.*` writes, which a hand-tally missed on first
        # measurement against the live dev DB.
        audit_log_entries=db.query(AuditLog).count() - audit_log_count_before,
    )


_BLOCKER_LIST_LIMIT = 10


def _surviving_audit_blockers(db: Session, user_ids: list[str], demo_targets: list[str]) -> str:
    """One sentence naming the audit rows that most often block a reset, or "" when there are none.

    A demo identity that acted on something the dataset does not own -- `platform.engine_changed`
    (target `platform:agent_engine`), `eval.run_started` / `eval.run_ended` (target `eval_run:<id>`)
    -- leaves audit rows that `remove_demo_data` does not delete: it deletes only rows whose target is
    one of `demo_targets`. The user delete then meets `audit_logs.actor_id`. Said plainly, so the
    operator knows which rows to look at instead of reading a bare foreign-key error.

    Action and target only, each with a row count: both are identifiers (`kind:id`), never the row's
    metadata, so no payload value can reach the message. Read after the rollback, on the restored
    state. Nothing is deleted here: the fail-safe is that the reset refuses."""
    survivors = (
        db.query(AuditLog.action, AuditLog.target, func.count(AuditLog.id))
        .filter(
            AuditLog.actor_id.in_(user_ids),
            (AuditLog.target.is_(None)) | (AuditLog.target.not_in(demo_targets)),
        )
        .group_by(AuditLog.action, AuditLog.target)
        .order_by(AuditLog.action, AuditLog.target)
        .all()
    )
    if not survivors:
        return ""
    named = "; ".join(
        f"{action} on {target if target is not None else '(no target)'} x{count}"
        for action, target, count in survivors[:_BLOCKER_LIST_LIMIT]
    )
    more = len(survivors) - _BLOCKER_LIST_LIMIT
    if more > 0:
        named += f"; and {more} more action/target pairs"
    return (
        "Audit rows written by a demo identity, about something the dataset does not own, are "
        f"kept by the reset and block the user delete: {named}. "
    )


def remove_demo_data(db: Session, *, is_prod: bool | None = None) -> DemoDataRemovalSummary:
    """the re-apply path's destructive half.

    (a4) The prod refusal is checked here, first, before a single row is
    read or deleted — identical guard to `populate_demo_data`'s, so
    `--reset` against a prod DB refuses exactly as loudly.

    (a3) A **traversal of the rows the demo dataset owns**, not a table
    wipe and not a `TRUNCATE`: everything is scoped by the demo users
    (`DEMO_USER_EMAILS`) -- NOT by team (see the module docstring) --
    and then by the ids those rows themselves own (their builds, verdicts,
    approval requests, deployments, and the audit-log rows that name any of
    the above AND were written by nobody or by a demo user). `seed.py`'s
    template and its three users live under a different team
    (`seed.SEED_TEAM_NAME`) and are never named by any of these queries —
    proven by a test that plants a non-demo project/user/team and checks
    they survive, not merely assumed from the team-scoping.

    Deletion order is children-before-parents throughout (no cascade is
    configured on any of these foreign keys, to match
    existing structure — nothing else in this codebase relies on
    `ondelete=CASCADE` either), so no delete here can ever hit a foreign
    key still pointing at a row that no longer exists."""
    if is_prod is None:
        is_prod = settings.is_prod
    if is_prod:
        raise ProdDemoDataRefusalError(
            "remove_demo_data refuses to run with FORGE_ENV=prod: this is the "
            "same fabricated-data guard as populate_demo_data, "
            "checked before any row is read, let alone deleted."
        )

    user_ids = [row[0] for row in db.query(User.id).filter(User.email.in_(DEMO_USER_EMAILS)).all()]
    if not user_ids:
        return DemoDataRemovalSummary(existed=False)
    team = db.query(Team).filter_by(name=DEMO_TEAM_NAME).first()

    # The dataset's projects are the ones a demo user owns -- not the ones under the demo team.
    project_ids = [row[0] for row in db.query(Project.id).filter(Project.owner_id.in_(user_ids)).all()]
    build_ids = [row[0] for row in db.query(Build.id).filter(Build.project_id.in_(project_ids)).all()]
    verdict_ids = [
        row[0] for row in db.query(PolicyVerdict.id).filter(PolicyVerdict.build_id.in_(build_ids)).all()
    ]
    approval_request_ids = [
        row[0]
        for row in db.query(ApprovalRequest.id).filter(ApprovalRequest.project_id.in_(project_ids)).all()
    ]
    deployment_ids = [
        row[0] for row in db.query(Deployment.id).filter(Deployment.project_id.in_(project_ids)).all()
    ]

    # The audit rows that name one of the dataset's entities. Whether each is the dataset's to
    # delete is decided by its ACTOR (module docstring): nobody or a demo user -> deleted; a real
    # person -> kept, and counted.
    demo_targets = (
        [f"project:{pid}" for pid in project_ids]
        + [f"user:{uid}" for uid in user_ids]
        + [f"deployment:{did}" for did in deployment_ids]
        + [f"approval_request:{aid}" for aid in approval_request_ids]
        + [f"build:{bid}" for bid in build_ids]
    )
    audit_about_the_dataset = db.query(AuditLog).filter(AuditLog.target.in_(demo_targets))
    fabricated_audit = audit_about_the_dataset.filter(
        (AuditLog.actor_id.is_(None)) | (AuditLog.actor_id.in_(user_ids))
    )
    audit_log_entries_kept = (
        audit_about_the_dataset.filter(AuditLog.actor_id.is_not(None), AuditLog.actor_id.not_in(user_ids)).count()
    )

    try:
        approval_decisions = (
            db.query(ApprovalDecision)
            .filter(ApprovalDecision.approval_request_id.in_(approval_request_ids))
            .delete(synchronize_session=False)
        )
        approval_requests = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.id.in_(approval_request_ids))
            .delete(synchronize_session=False)
        )
        policy_check_results = (
            db.query(PolicyCheckResult)
            .filter(PolicyCheckResult.verdict_id.in_(verdict_ids))
            .delete(synchronize_session=False)
        )
        policy_verdicts = (
            db.query(PolicyVerdict).filter(PolicyVerdict.id.in_(verdict_ids)).delete(synchronize_session=False)
        )
        # `Deployment.rollback_of` is self-referential (a rollback row points at
        # the row it rolled back to, e.g. D4 -> D2 above) -- with FK enforcement
        # on (`conftest.py`'s `PRAGMA foreign_keys=ON`), a single bulk DELETE
        # covering both a row and whatever still points at it via that column
        # is not guaranteed to process the referencing row first. Breaking the
        # self-reference in one UPDATE before the DELETE removes that ordering
        # dependency entirely, rather than trusting SQLite's row-processing
        # order to happen to be safe.
        db.query(Deployment).filter(Deployment.id.in_(deployment_ids)).update(
            {Deployment.rollback_of: None}, synchronize_session=False
        )
        deployments = (
            db.query(Deployment).filter(Deployment.id.in_(deployment_ids)).delete(synchronize_session=False)
        )
        change_requests = (
            db.query(ChangeRequest)
            .filter(ChangeRequest.project_id.in_(project_ids))
            .delete(synchronize_session=False)
        )
        builds = db.query(Build).filter(Build.id.in_(build_ids)).delete(synchronize_session=False)
        audit_log_entries = fabricated_audit.delete(synchronize_session=False)
        projects = db.query(Project).filter(Project.id.in_(project_ids)).delete(synchronize_session=False)
        users = db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        templates = (
            db.query(Template).filter_by(name=DEMO_DISABLED_TEMPLATE_NAME).delete(synchronize_session=False)
        )
        # The team goes only when nothing references it any more: a real person's project (or
        # the person) can still sit under it, and that must not be what blocks the reset.
        team_removed = 0
        if team is not None:
            still_used = (
                db.query(Project.id).filter_by(team_id=team.id).first() is not None
                or db.query(User.id).filter_by(team_id=team.id).first() is not None
            )
            if not still_used:
                db.delete(team)
                team_removed = 1
        db.flush()
    except IntegrityError as exc:
        # Everything above is one uncommitted transaction: undone as a whole.
        db.rollback()
        raise DemoDataResetBlockedError(
            "demo_data --reset did not delete anything: a row outside the demo dataset still "
            "references a demo user or a demo row (a demo identity was used on a real project, or "
            "a real project uses the demo template), and removing it would rewrite that row. "
            f"{_surviving_audit_blockers(db, user_ids, demo_targets)}"
            f"The database said: {exc.orig}"
        ) from exc

    db.commit()

    return DemoDataRemovalSummary(
        existed=True,
        teams=team_removed,
        users=users,
        projects=projects,
        templates=templates,
        change_requests=change_requests,
        builds=builds,
        policy_verdicts=policy_verdicts,
        policy_check_results=policy_check_results,
        approval_requests=approval_requests,
        approval_decisions=approval_decisions,
        deployments=deployments,
        audit_log_entries=audit_log_entries,
        audit_log_entries_kept=audit_log_entries_kept,
    )


def reset_demo_data(
    db: Session, *, is_prod: bool | None = None
) -> tuple[DemoDataRemovalSummary, DemoDataSummary]:
    """remove whatever demo dataset currently exists, then
    repopulate it fresh, so a change to this module's own scenario is
    observable without recreating the database by hand. Both steps share
    one prod refusal, and it is `remove_demo_data`'s own check that fires
    first — `populate_demo_data` is never even reached against a prod DB
    here (a4)."""
    if is_prod is None:
        is_prod = settings.is_prod
    removal = remove_demo_data(db, is_prod=is_prod)
    population = populate_demo_data(db, is_prod=is_prod)
    return removal, population


def run(*, reset: bool = False):
    db = SessionLocal()
    try:
        if reset:
            return reset_demo_data(db)
        return populate_demo_data(db)
    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Populate the platform DB's dev-only demo dataset. Default: "
            "populate once, no-op thereafter (a1, unchanged). --reset: "
            "remove the existing demo dataset (a traversal of the rows it "
            "owns, never a table wipe) and repopulate it fresh (a2)."
        )
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove the existing demo dataset and repopulate it fresh.",
    )
    args = parser.parse_args()

    try:
        result = run(reset=args.reset)
    except (ProdDemoDataRefusalError, DemoDataResetBlockedError) as exc:
        print(f"demo_data: refused -- {exc}", file=sys.stderr)
        sys.exit(1)

    if args.reset:
        removal, summary = result
        if not removal.existed:
            print("demo_data: --reset found no existing demo dataset -- nothing to remove.")
        else:
            print("demo_data: removed the existing demo dataset:")
            print(f"  teams={removal.teams} users={removal.users} projects={removal.projects} templates={removal.templates}")
            print(f"  change_requests={removal.change_requests} builds={removal.builds}")
            print(
                f"  policy_verdicts={removal.policy_verdicts} "
                f"policy_check_results={removal.policy_check_results}"
            )
            print(
                f"  approval_requests={removal.approval_requests} "
                f"approval_decisions={removal.approval_decisions}"
            )
            print(f"  deployments={removal.deployments} audit_log_entries={removal.audit_log_entries}")
            if removal.audit_log_entries_kept:
                print(
                    f"  kept {removal.audit_log_entries_kept} audit_log entries a real user wrote about "
                    "demo entities (the audit log is append-only; their targets no longer exist)."
                )
    else:
        summary = result

    if not summary.created:
        print("demo_data: already populated (the demo users exist) -- nothing to do.")
    else:
        print("demo_data: populated.")
        print(f"  teams={summary.teams} users={summary.users} projects={summary.projects} templates={summary.templates}")
        print(f"  change_requests={summary.change_requests} builds={summary.builds}")
        print(
            f"  policy_verdicts={summary.policy_verdicts} "
            f"policy_check_results={summary.policy_check_results}"
        )
        print(
            f"  approval_requests={summary.approval_requests} "
            f"approval_decisions={summary.approval_decisions}"
        )
        print(f"  deployments={summary.deployments} audit_log_entries={summary.audit_log_entries}")
