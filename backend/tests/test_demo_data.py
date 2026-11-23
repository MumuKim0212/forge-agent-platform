"""The demo-data command itself.

Standard in-memory SQLite `db_session` fixture — no Docker, no network. What
pytest can settle that reading the module cannot: that the prod refusal
actually blocks (and writes nothing) for both the populate and the reset
paths, that `--reset`'s removal really is a traversal of the rows the demo
dataset owns rather than a table wipe, and that the populated rows really do
satisfy the original list plus the enrichment branches — proven by
walking the real `GET /projects/{id}/timeline` response for the timeline
claim, not by re-deriving the same query in the test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.demo_data import (
    DEMO_TEAM_NAME,
    DEMO_DISABLED_TEMPLATE_NAME,
    DEMO_USER_EMAILS,
    DemoDataResetBlockedError,
    ProdDemoDataRefusalError,
    populate_demo_data,
    remove_demo_data,
    reset_demo_data,
)
from app.models.approval import ApprovalRequest, ApprovalStatus
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment, DeploymentStatus
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.services.account_status import AccountStatus
from app.services.status import BuildStatus, ProjectStatus
from app.services.usage import resolve_month_window

import pytest


def test_refuses_on_prod_and_writes_nothing(db_session):
    with pytest.raises(ProdDemoDataRefusalError):
        populate_demo_data(db_session, is_prod=True)

    assert db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).first() is None
    assert db_session.query(Project).count() == 0


def test_populates_a_plausible_pipeline(db_session):
    summary = populate_demo_data(db_session, is_prod=False)

    assert summary.created is True
    assert summary.projects == 4
    assert summary.teams == 1
    assert summary.users == 6

    projects = db_session.query(Project).all()
    assert {p.name for p in projects} == {
        "사내 헬프데스크",
        "사내 설문조사 도구",
        "사내 회의실 예약 현황판",
        "사내 명함 신청 도구",
    }

    # (a4) a SUCCEEDED build with an APPROVED (human-decided, not just
    # auto-approved) approval request exists.
    approved_requests = (
        db_session.query(ApprovalRequest).filter_by(status=ApprovalStatus.APPROVED).all()
    )
    assert len(approved_requests) >= 1
    approved_build = db_session.get(Build, approved_requests[0].build_id)
    assert approved_build.status == BuildStatus.SUCCEEDED

    # (a4) several PolicyCheckResults, at least one flagged, spanning more
    # than one risk tier.
    verdicts = db_session.query(PolicyVerdict).all()
    assert len(verdicts) >= 3
    assert len({v.risk_tier for v in verdicts}) > 1
    flagged = db_session.query(PolicyCheckResult).filter_by(result="flag").all()
    assert len(flagged) >= 1

    # (a4) ApprovalRequests in PENDING and decided states, with
    # ApprovalDecision rows attached to at least one of them.
    statuses = {r.status for r in db_session.query(ApprovalRequest).all()}
    assert ApprovalStatus.PENDING in statuses
    assert statuses & {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.AUTO_APPROVED}
    decided_with_decisions = [
        r
        for r in db_session.query(ApprovalRequest).all()
        if r.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED) and len(r.decisions) > 0
    ]
    assert len(decided_with_decisions) >= 2  # the reject and the approve

    # (a4) Deployments covering ACTIVE, a superseded one, a failed one, and
    # a rollback of another.
    deployments = db_session.query(Deployment).all()
    deployment_statuses = {d.status for d in deployments}
    assert DeploymentStatus.ACTIVE in deployment_statuses
    assert DeploymentStatus.SUPERSEDED in deployment_statuses
    assert DeploymentStatus.FAILED in deployment_statuses
    rollbacks = [d for d in deployments if d.rollback_of is not None]
    assert len(rollbacks) >= 1
    # Exactly one ACTIVE per project -- the partial unique index would have
    # raised at insert time if this were violated, but assert the visible
    # property too.
    active_by_project: dict[str, int] = {}
    for d in deployments:
        if d.status == DeploymentStatus.ACTIVE:
            active_by_project[d.project_id] = active_by_project.get(d.project_id, 0) + 1
    assert all(count == 1 for count in active_by_project.values())


def test_project_statuses_are_not_all_the_default_submitted(db_session):
    """Leaving every demo project at the
    constructor default `SUBMITTED` made `StatusBadge` render in exactly one tone
    across all three portal screens no matter how rich the rest of the pipeline
    was. Each demo project's status must actually reflect its own pipeline
    position. (The pipeline now advances status itself; this file is seed
    data and hand-sets it, so the next test checks the hand-set values.)"""
    populate_demo_data(db_session, is_prod=False)

    projects = {p.name: p.status for p in db_session.query(Project).all()}
    assert len(set(projects.values())) > 1, "every demo project has the same status"

    helpdesk = db_session.query(Project).filter_by(name="사내 헬프데스크").first()
    assert helpdesk.status == ProjectStatus.ACTIVE  # it has an ACTIVE deployment (d4)

    survey = db_session.query(Project).filter_by(name="사내 설문조사 도구").first()
    assert survey.status == ProjectStatus.AWAITING_APPROVAL  # its one request (ar5) is still PENDING


def test_demo_project_statuses_match_the_stage_their_own_history_evidences(db_session):
    """`demo_data.py` sets `Project.status` by hand, so nothing but this test keeps it
    from drifting from the pipeline's own rule. The rule is the backfill migration's
    (`d91c3e7a5b28`): the latest stage a project's rows evidence, `SUBMITTED` when none. Its
    correctness is pinned on its own, against literal expectations, in
    `test_project_status_backfill.py`; here it is only the yardstick for the seed rows."""
    import importlib.util
    from pathlib import Path

    migration_file = next(
        (Path(__file__).resolve().parent.parent / "migrations" / "versions").glob("d91c3e7a5b28_*.py")
    )
    spec = importlib.util.spec_from_file_location("backfill_project_status", migration_file)
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)
    populate_demo_data(db_session, is_prod=False)

    connection = db_session.connection()
    checked = 0
    for project in db_session.query(Project).all():
        if project.status == ProjectStatus.ARCHIVED:
            continue  # archive is its own writer; the evidence says nothing about it
        derived = backfill._latest_evidenced_stage(connection, project.id) or ProjectStatus.SUBMITTED
        assert project.status == derived, project.name
        checked += 1
    assert checked == 3


def test_pending_approval_requests_build_has_a_preview_link(db_session):
    """Before this, 0 of the demo
    builds set `preview_url`, so the approval list's nested preview `<a>`
    (`ApprovalConsolePage.tsx`'s `ApprovalList`) never rendered on the real
    screen -- exactly the element `ListRow`'s `shouldActivateRow` target
    guard exists to protect, and untestable on a screen where it never
    appears. The one request the demo data leaves genuinely PENDING must
    carry a preview link, pointed at a `.invalid` host (RFC 2606 -- never
    resolves), consistent with T1: markup for a link that goes nowhere,
    never a real preview."""
    populate_demo_data(db_session, is_prod=False)

    pending = db_session.query(ApprovalRequest).filter_by(status=ApprovalStatus.PENDING).one()
    build = db_session.get(Build, pending.build_id)

    assert build.preview_url is not None
    assert ".invalid" in build.preview_url


def test_all_six_timeline_kinds_render_for_the_rich_project(db_session, client):
    populate_demo_data(db_session, is_prod=False)

    helpdesk = db_session.query(Project).filter_by(name="사내 헬프데스크").first()
    assert helpdesk is not None
    any_user = db_session.query(User).first()

    resp = client.get(
        f"/api/projects/{helpdesk.id}/timeline", headers={"X-Forge-User-Id": any_user.id}
    )
    assert resp.status_code == 200
    kinds = {entry["kind"] for entry in resp.json()}
    assert kinds == {
        "change_request",
        "build",
        "audit_log",
        "policy_verdict",
        "approval_request",
        "deployment",
    }


def test_idempotent_second_call_is_a_no_op(db_session):
    first = populate_demo_data(db_session, is_prod=False)
    assert first.created is True

    project_count = db_session.query(Project).count()
    build_count = db_session.query(Build).count()

    second = populate_demo_data(db_session, is_prod=False)
    assert second.created is False

    assert db_session.query(Project).count() == project_count
    assert db_session.query(Build).count() == build_count


# ---------------------------------------------------------------------------
# enrichment -- each exists because a specific panel branch
# could not otherwise render (see the task's `## Result` for the mapping).
# ---------------------------------------------------------------------------


def test_signup_queue_has_a_pending_and_a_rejected_user(db_session):
    """Every demo user before this task was ACTIVE (`_make_user`'s default,
    matching `User.status`'s own column default) -- so `SignupRequestsPanel`'s
    대기중 and 거부됨 tabs (`GET /admin/signup-requests?status=`) were both
    permanently empty."""
    populate_demo_data(db_session, is_prod=False)

    statuses = {u.status for u in db_session.query(User).filter(User.email.like("demo.%")).all()}
    assert AccountStatus.PENDING in statuses
    assert AccountStatus.REJECTED in statuses


def test_templates_panel_has_a_second_disabled_template(db_session):
    """`TemplatesPanel` had exactly one, always-enabled row before this --
    its empty-detail and disabled-row states were both unreachable."""
    populate_demo_data(db_session, is_prod=False)

    templates = db_session.query(Template).all()
    assert len(templates) >= 2
    disabled = [t for t in templates if not t.enabled]
    assert len(disabled) >= 1
    assert disabled[0].name == DEMO_DISABLED_TEMPLATE_NAME


def test_usage_panel_branches_are_all_reachable(db_session):
    """Before this task: no project ever carried a `monthly_quota_usd`
    override (so 개별 지정 and the zero-override note never rendered), no
    build ever had `cost_usd IS NULL` (so 미측정 never rendered), every
    build had a ChangeRequest (so the unattributed line was permanently
    $0.00), and nothing ever exceeded the $100 default (so a breach never
    rendered)."""
    populate_demo_data(db_session, is_prod=False)

    projects = {p.name: p for p in db_session.query(Project).all()}
    overrides = {p.monthly_quota_usd for p in projects.values() if p.monthly_quota_usd is not None}
    assert overrides, "no project carries a monthly_quota_usd override"
    assert 0.0 in overrides, "no project carries an override of exactly 0"

    survey = projects["사내 설문조사 도구"]
    survey_spend = sum(
        b.cost_usd or 0.0
        for b in db_session.query(Build).filter_by(project_id=survey.id).all()
    )
    assert survey_spend > 0.0
    assert survey.monthly_quota_usd == 0.0  # any positive spend is therefore a breach

    helpdesk = projects["사내 헬프데스크"]
    helpdesk_spend = sum(
        b.cost_usd or 0.0
        for b in db_session.query(Build).filter_by(project_id=helpdesk.id).all()
    )
    assert helpdesk.monthly_quota_usd is not None and helpdesk_spend > helpdesk.monthly_quota_usd

    unmeasured = db_session.query(Build).filter_by(cost_usd=None).all()
    assert len(unmeasured) >= 1

    builds_without_change_request = [
        b
        for b in db_session.query(Build).all()
        if db_session.query(ChangeRequest).filter_by(build_id=b.id).first() is None
    ]
    assert len(builds_without_change_request) >= 1
    assert any(b.cost_usd and b.cost_usd > 0 for b in builds_without_change_request)


def test_usage_month_window_has_two_this_month_and_one_last_month(db_session):
    """rollups key on
    `Build.ended_at` inside the calendar month window, and every build this
    module wrote before this task was backdated 2-20 days -- on an
    early-month day, most or all of them fall outside the default 이번 달
    view. Anchored to the run date (T2), not hardcoded, so this holds
    whatever day the suite happens to run on."""
    populate_demo_data(db_session, is_prod=False)

    this_month = resolve_month_window()
    last_month_probe = this_month.starts_at - timedelta(days=1)
    last_month = resolve_month_window(last_month_probe.strftime("%Y-%m"))

    def _count_in(window) -> int:
        return (
            db_session.query(Build)
            .filter(Build.ended_at.is_not(None))
            .filter(Build.ended_at >= window.starts_at)
            .filter(Build.ended_at < window.ends_at)
            .count()
        )

    assert _count_in(this_month) >= 2
    assert _count_in(last_month) >= 1


def test_deployment_health_reaches_more_than_two_states(db_session):
    """Of `deploymentHealth.ts`'s nine states, only SERVING (an ACTIVE
    deployment) and NEVER_DEPLOYED (no deployment row at all) were ever
    reachable before this task."""
    populate_demo_data(db_session, is_prod=False)

    deployments = db_session.query(Deployment).all()
    statuses = {d.status for d in deployments}
    # ACTIVE + NEVER_DEPLOYED (a project with zero rows) were already
    # reachable; STOPPED and FAILED-with-no-active-deployment are new.
    assert DeploymentStatus.STOPPED in statuses

    never_deployed_projects = [
        p
        for p in db_session.query(Project).all()
        if db_session.query(Deployment).filter_by(project_id=p.id).count() == 0
    ]
    assert len(never_deployed_projects) >= 1

    # A project whose only deployment is FAILED (active=null, latest=FAILED
    # -- frontend `deploymentHealthState`'s LAST_ATTEMPT_FAILED).
    failed_only_projects = []
    for p in db_session.query(Project).all():
        rows = db_session.query(Deployment).filter_by(project_id=p.id).all()
        if rows and all(d.status != DeploymentStatus.ACTIVE for d in rows) and any(
            d.status == DeploymentStatus.FAILED for d in rows
        ):
            failed_only_projects.append(p)
    assert len(failed_only_projects) >= 1


# ---------------------------------------------------------------------------
# the re-apply path.
# ---------------------------------------------------------------------------


def test_reset_refuses_on_prod_and_removes_nothing(db_session):
    """(a4): the prod refusal covers the re-apply path too, checked before
    anything is read or deleted."""
    populate_demo_data(db_session, is_prod=False)
    project_count_before = db_session.query(Project).count()

    with pytest.raises(ProdDemoDataRefusalError):
        remove_demo_data(db_session, is_prod=True)

    assert db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).first() is not None
    assert db_session.query(Project).count() == project_count_before

    with pytest.raises(ProdDemoDataRefusalError):
        reset_demo_data(db_session, is_prod=True)

    assert db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).first() is not None
    assert db_session.query(Project).count() == project_count_before


def test_reset_removes_the_demo_dataset_and_repopulates_it(db_session):
    """(a2): a re-apply path exists that removes the demo dataset and
    repopulates it, so a change to the dataset is observable without
    recreating the database by hand."""
    first = populate_demo_data(db_session, is_prod=False)
    assert first.created is True
    team_before = db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).one()
    original_project_ids = {
        p.id for p in db_session.query(Project).filter_by(team_id=team_before.id).all()
    }
    original_helpdesk_id = (
        db_session.query(Project).filter_by(name="사내 헬프데스크").first().id
    )

    removal, population = reset_demo_data(db_session, is_prod=False)

    assert removal.existed is True
    assert removal.projects == 4
    assert removal.users == 6
    assert removal.teams == 1
    assert population.created is True
    assert population.projects == 4

    # Only one demo team, and its rows are freshly created -- the old
    # helpdesk project's id is gone, replaced by a new row with the same
    # name.
    assert db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).count() == 1
    new_helpdesk = db_session.query(Project).filter_by(name="사내 헬프데스크").one()
    assert new_helpdesk.id != original_helpdesk_id
    assert original_project_ids.isdisjoint({p.id for p in db_session.query(Project).all()})


def test_reset_preserves_non_demo_project_user_and_team(db_session):
    """(a3): the removal is a traversal of rows the demo dataset owns, not
    a table wipe and not a truncate. `seed.py`'s template and its three
    users are not the only things that must survive -- proven here with a
    bespoke non-demo project/user/team, not by reusing the seeded ones."""
    template = db_session.query(Template).first()  # seed.py's shared template

    other_team = Team(name="다른 부서 팀")
    db_session.add(other_team)
    db_session.flush()
    other_user = User(team_id=other_team.id, name="김보존", email="survivor@example.com", role="requester")
    db_session.add(other_user)
    db_session.flush()
    other_project = Project(
        name="살아남아야 하는 프로젝트",
        owner_id=other_user.id,
        team_id=other_team.id,
        template_id=template.id,
    )
    db_session.add(other_project)
    db_session.commit()
    other_team_id, other_user_id, other_project_id = other_team.id, other_user.id, other_project.id

    populate_demo_data(db_session, is_prod=False)
    reset_demo_data(db_session, is_prod=False)

    surviving_team = db_session.get(Team, other_team_id)
    surviving_user = db_session.get(User, other_user_id)
    surviving_project = db_session.get(Project, other_project_id)

    assert surviving_team is not None and surviving_team.name == "다른 부서 팀"
    assert surviving_user is not None and surviving_user.email == "survivor@example.com"
    assert surviving_project is not None and surviving_project.name == "살아남아야 하는 프로젝트"
    assert surviving_project.owner_id == other_user_id
    assert surviving_project.team_id == other_team_id


# ---------------------------------------------------------------------------
# the reset deletes what the generator created, not whatever sits under its team.
# ---------------------------------------------------------------------------


def _real_person_with_a_project_under_the_demo_team(db_session):
    """Someone who is not a demo user, in the demo team, with a project there that has a build, a
    change request and audit rows of its own -- `create_project` accepts any `team_id`, so this
    is reachable. Returns (user, project, build, change_request, audit_entry)."""
    from app.services.audit import write_audit_log

    team = db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).one()
    template = db_session.query(Template).first()
    real = User(team_id=team.id, name="실사용자", email="real.person@example.com", role="requester")
    db_session.add(real)
    db_session.flush()
    project = Project(name="실제 프로젝트", owner_id=real.id, team_id=team.id, template_id=template.id)
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED)
    db_session.add(build)
    db_session.flush()
    change_request = ChangeRequest(
        project_id=project.id, requester_id=real.id, nl_request="실제 요청", build_id=build.id
    )
    db_session.add(change_request)
    entry = write_audit_log(
        db_session, actor_id=real.id, action="project.created", target=f"project:{project.id}", metadata={}
    )
    db_session.commit()
    return real, project, build, change_request, entry


def test_reset_keeps_a_real_users_project_that_sits_under_the_demo_team(db_session):
    """The demo team is the demo generator's, but a project is the generator's only if a demo user
    owns it. A real person's project under that team, with everything attached to it, survives
    `--reset` -- and so does the team row it references and the person's own row. The demo
    dataset is replaced around it."""
    populate_demo_data(db_session, is_prod=False)
    real, project, build, change_request, entry = _real_person_with_a_project_under_the_demo_team(db_session)
    real_id, project_id, build_id, change_request_id, entry_id = (
        real.id, project.id, build.id, change_request.id, entry.id,
    )
    team_id = db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).one().id
    old_demo_project_ids = {
        p.id for p in db_session.query(Project).filter(Project.id != project_id).all()
    }

    removal, population = reset_demo_data(db_session, is_prod=False)

    assert db_session.get(Project, project_id).name == "실제 프로젝트"
    assert db_session.get(Build, build_id) is not None
    assert db_session.get(ChangeRequest, change_request_id) is not None
    assert db_session.get(User, real_id).email == "real.person@example.com"
    assert db_session.get(AuditLog, entry_id) is not None
    # The team is still referenced, so it stays, and the new demo dataset reuses it.
    assert db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).one().id == team_id
    assert removal.teams == 0
    assert (removal.projects, removal.users) == (4, 6)  # only the demo users' projects, only the demo users
    assert population.created is True and population.projects == 4
    new_demo_projects = db_session.query(Project).filter(Project.id != project_id).all()
    assert len(new_demo_projects) == 4
    assert old_demo_project_ids.isdisjoint({p.id for p in new_demo_projects})
    assert {p.team_id for p in new_demo_projects} == {team_id}


def test_reset_keeps_the_audit_rows_a_real_person_wrote_and_removes_the_ones_the_dataset_wrote(db_session):
    """`audit_log` is append-only, so a reset removes a row only when EVERY identity in it
    belongs to the fabricated dataset: the actor is nobody (a worker-driven row) or a demo user,
    AND the target is a demo entity. A real person's act on a demo entity stays (its target then
    names a thing that no longer exists, which is only text); so does anything about a real
    entity. Counted both ways."""
    from app.services.audit import write_audit_log

    populate_demo_data(db_session, is_prod=False)
    real, real_project, _build, _cr, _entry = _real_person_with_a_project_under_the_demo_team(db_session)
    demo_admin = db_session.query(User).filter_by(email="demo.admin@forge.local").one()
    demo_project = db_session.query(Project).filter_by(name="사내 헬프데스크").one()
    demo_deployment = db_session.query(Deployment).filter_by(project_id=demo_project.id).first()

    real_on_demo = write_audit_log(
        db_session, actor_id=real.id, action="project.quota_changed",
        target=f"project:{demo_project.id}", metadata={"by": "a real admin"},
    )
    demo_on_demo = write_audit_log(
        db_session, actor_id=demo_admin.id, action="project.quota_changed",
        target=f"project:{demo_project.id}", metadata={},
    )
    nobody_on_demo = write_audit_log(
        db_session, actor_id=None, action="deployment.failed",
        target=f"deployment:{demo_deployment.id}", metadata={},
    )
    real_on_real = write_audit_log(
        db_session, actor_id=real.id, action="project.quota_changed",
        target=f"project:{real_project.id}", metadata={},
    )
    db_session.commit()
    kept_ids = {real_on_demo.id, real_on_real.id}
    gone_ids = {demo_on_demo.id, nobody_on_demo.id}
    generator_rows = db_session.query(AuditLog).filter(AuditLog.target.like("approval_request:%")).count()
    assert generator_rows > 0  # the generator's own rows exist before the reset

    removal, population = reset_demo_data(db_session, is_prod=False)

    surviving = {row.id for row in db_session.query(AuditLog)}
    assert kept_ids <= surviving
    assert gone_ids.isdisjoint(surviving)
    assert removal.audit_log_entries_kept == 1  # the real person's act on the demo project
    # The old generator rows are gone and the new population wrote its own.
    assert removal.audit_log_entries >= generator_rows
    assert population.audit_log_entries > 0


def test_reset_refuses_and_changes_nothing_when_a_surviving_row_still_needs_a_demo_user(db_session):
    """Someone used a demo identity on a real project, so a row that must stay (a change request
    on the real project) names a demo user. Deleting that user would either fail on the foreign
    key halfway or rewrite the row; the reset refuses, says what blocks it, and leaves the whole
    dataset exactly as it was."""
    populate_demo_data(db_session, is_prod=False)
    _real, project, _build, _cr, _entry = _real_person_with_a_project_under_the_demo_team(db_session)
    demo_owner = db_session.query(User).filter_by(email="demo.owner1@forge.local").one()
    db_session.add(
        ChangeRequest(project_id=project.id, requester_id=demo_owner.id, nl_request="데모 계정으로 보낸 요청")
    )
    db_session.commit()
    tables = (Team, User, Project, Build, ChangeRequest, ApprovalRequest, Deployment, AuditLog)
    before = {model.__name__: db_session.query(model).count() for model in tables}

    with pytest.raises(DemoDataResetBlockedError, match="demo user"):
        reset_demo_data(db_session, is_prod=False)

    after = {model.__name__: db_session.query(model).count() for model in tables}
    assert after == before
    assert db_session.query(User).filter_by(email="demo.owner1@forge.local").count() == 1


def _demo_identity_acts_outside_the_dataset(db_session, *, action: str, target: str) -> str:
    """A demo identity's audit row about something the dataset does not own, with a payload value
    the message must not carry. Returns the payload value."""
    payload = "PAYLOAD-VALUE-MUST-NOT-LEAK"
    demo_admin = db_session.query(User).filter_by(email="demo.owner1@forge.local").one()
    db_session.add(AuditLog(actor_id=demo_admin.id, action=action, target=target, metadata_json={"detail": payload}))
    db_session.commit()
    return payload


def test_a_blocked_reset_names_the_engine_change_that_blocks_it(db_session):
    """`platform.engine_changed` is written by whoever switched the engine -- here a demo identity --
    with target `platform:agent_engine`, which is not one of the dataset's targets, so the row
    survives the delete and the user delete meets it. The error names the action and the target (not
    the payload), and nothing was deleted."""
    populate_demo_data(db_session, is_prod=False)
    payload = _demo_identity_acts_outside_the_dataset(
        db_session, action="platform.engine_changed", target="platform:agent_engine"
    )
    tables = (Team, User, Project, Build, ChangeRequest, ApprovalRequest, Deployment, AuditLog)
    before = {model.__name__: db_session.query(model).count() for model in tables}

    with pytest.raises(DemoDataResetBlockedError) as blocked:
        reset_demo_data(db_session, is_prod=False)

    message = str(blocked.value)
    assert "platform.engine_changed" in message
    assert "platform:agent_engine" in message
    assert payload not in message
    after = {model.__name__: db_session.query(model).count() for model in tables}
    assert after == before


def test_a_blocked_reset_names_the_eval_run_that_blocks_it(db_session):
    populate_demo_data(db_session, is_prod=False)
    payload = _demo_identity_acts_outside_the_dataset(
        db_session, action="eval.run_started", target="eval_run:run-id-1234"
    )
    before = db_session.query(AuditLog).count()

    with pytest.raises(DemoDataResetBlockedError) as blocked:
        remove_demo_data(db_session, is_prod=False)

    message = str(blocked.value)
    assert "eval.run_started" in message
    assert "eval_run:run-id-1234" in message
    assert payload not in message
    assert db_session.query(AuditLog).count() == before
    assert db_session.query(User).filter_by(email="demo.owner1@forge.local").count() == 1


def test_a_blocked_reset_with_no_surviving_audit_row_still_refuses_without_naming_one(db_session):
    """The sentence is added only when there is something to name; the existing blocker (a real
    row naming a demo user) keeps its message."""
    populate_demo_data(db_session, is_prod=False)
    _real, project, _build, _cr, _entry = _real_person_with_a_project_under_the_demo_team(db_session)
    demo_owner = db_session.query(User).filter_by(email="demo.owner1@forge.local").one()
    db_session.add(ChangeRequest(project_id=project.id, requester_id=demo_owner.id, nl_request="데모 계정으로 보낸 요청"))
    db_session.commit()

    with pytest.raises(DemoDataResetBlockedError) as blocked:
        reset_demo_data(db_session, is_prod=False)

    assert "Audit rows written by a demo identity" not in str(blocked.value)


def test_the_demo_users_are_the_ones_the_generator_creates(db_session):
    """The reset finds the dataset by these addresses (a real person is never matched by team or
    by guess), so the list is pinned against what `populate_demo_data` actually creates."""
    assert set(DEMO_USER_EMAILS) == {
        "demo.owner1@forge.local",
        "demo.owner2@forge.local",
        "demo.approver@forge.local",
        "demo.admin@forge.local",
        "demo.pending@forge.local",
        "demo.rejected@forge.local",
    }
    populate_demo_data(db_session, is_prod=False)
    team = db_session.query(Team).filter_by(name=DEMO_TEAM_NAME).one()

    assert {u.email for u in db_session.query(User).filter_by(team_id=team.id)} == set(DEMO_USER_EMAILS)


def test_remove_demo_data_is_a_no_op_when_nothing_exists(db_session):
    removal = remove_demo_data(db_session, is_prod=False)
    assert removal.existed is False


# ---------------------------------------------------------------------------
# failed builds, so the timeline's failure lines can be reviewed.
# ---------------------------------------------------------------------------


def test_demo_data_has_failed_builds_with_a_code_and_a_detail(db_session):
    populate_demo_data(db_session, is_prod=False)

    failed = db_session.query(Build).filter_by(status=BuildStatus.FAILED).all()

    assert len(failed) >= 2
    assert {b.failure_reason for b in failed} >= {"AGENT_ERROR", "TIMEOUT"}
    for build in failed:
        assert build.failure_detail and build.failure_detail.strip()
        assert len(build.failure_detail) <= 1000
        assert build.diff_ref is None and build.image_ref is None  # a failure produced no PR or image
    # Only FAILED builds carry either field.
    others = db_session.query(Build).filter(Build.status != BuildStatus.FAILED).all()
    assert all(b.failure_reason is None and b.failure_detail is None for b in others)


def test_the_failed_builds_reach_the_timeline_with_their_detail(db_session, client):
    populate_demo_data(db_session, is_prod=False)
    project = db_session.query(Project).filter_by(name="사내 명함 신청 도구").one()
    any_user = db_session.query(User).first()

    resp = client.get(
        f"/api/projects/{project.id}/timeline", headers={"X-Forge-User-Id": any_user.id}
    )

    assert resp.status_code == 200
    builds = [e["data"] for e in resp.json() if e["kind"] == "build"]
    failed: dict[str, list[str]] = {}
    for b in builds:
        if b["status"] == "FAILED":
            failed.setdefault(b["failure_reason"], []).append(b["failure_detail"])
    # A second AGENT_ERROR build exists (the schema refusal), so a reason maps to a list.
    assert any("refused by tool policy" in d for d in failed["AGENT_ERROR"])
    assert any("maximum build duration" in d for d in failed["TIMEOUT"])
    succeeded = [b for b in builds if b["status"] == "SUCCEEDED"]
    assert succeeded and all(b["failure_detail"] is None for b in succeeded)


def test_reset_recreates_the_failed_builds(db_session):
    populate_demo_data(db_session, is_prod=False)
    before = db_session.query(Build).filter_by(status=BuildStatus.FAILED).count()
    assert before >= 2

    reset_demo_data(db_session, is_prod=False)

    assert db_session.query(Build).filter_by(status=BuildStatus.FAILED).count() == before


# ---------------------------------------------------------------------------
# the measured facts the console and timeline render.
# ---------------------------------------------------------------------------


def test_demo_data_measures_every_branch_the_screens_render(db_session):
    populate_demo_data(db_session, is_prod=False)
    builds = db_session.query(Build).all()
    measured = [b for b in builds if b.changed_files is not None]

    # Not measured (NULL) exists too: a build that never reached harvest shows nothing.
    assert any(b.changed_files is None for b in builds)
    assert measured
    for b in measured:
        files = b.changed_files
        assert sorted(files) == ["entries", "totals", "truncated"]
        assert len(files["entries"]) <= 500
        assert [e["path"] for e in files["entries"]] == sorted(e["path"] for e in files["entries"])
        assert b.protected_path_drift is not None
    # A list longer than the approval detail shows (50), and one truncated in storage.
    assert any(len(b.changed_files["entries"]) > 50 for b in measured)
    assert any(b.changed_files["truncated"] is True for b in measured)
    truncated = next(b for b in measured if b.changed_files["truncated"])
    assert sum(truncated.changed_files["totals"].values()) > len(truncated.changed_files["entries"])
    # Drift present, and absent-but-measured.
    assert any(b.protected_path_drift for b in measured)
    assert any(b.protected_path_drift == [] for b in measured)
    # Every change kind appears somewhere.
    kinds = {e["change"] for b in measured for e in b.changed_files["entries"]}
    assert kinds == {"added", "modified", "deleted"}


def test_demo_data_has_a_schema_refused_modify_build_with_its_changed_files(db_session):
    populate_demo_data(db_session, is_prod=False)

    refused = [
        b
        for b in db_session.query(Build).filter_by(status=BuildStatus.FAILED, type="modify").all()
        if b.failure_detail and "database schema" in b.failure_detail
    ]

    assert len(refused) == 1
    (build,) = refused
    assert any(e["path"].startswith("app/migrations/") for e in build.changed_files["entries"])
    # The detail is the platform's sentence: it names no file the agent changed.
    assert not any(e["path"] in build.failure_detail for e in build.changed_files["entries"])


def test_demo_data_has_findings_with_a_long_path_and_a_redacted_match(db_session):
    populate_demo_data(db_session, is_prod=False)

    findings = [
        f
        for c in db_session.query(PolicyCheckResult).filter_by(result="flag").all()
        for f in (c.findings or [])
    ]

    assert any(len(f.get("path", "")) > 100 for f in findings)
    assert any("REDACTED" in f.get("match", "") for f in findings)


def test_the_pending_request_carries_the_hostile_path_and_the_drift_through_the_api(db_session, client):
    populate_demo_data(db_session, is_prod=False)
    pending = db_session.query(ApprovalRequest).filter_by(status=ApprovalStatus.PENDING).all()
    any_user = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the detail is an approver's here

    bodies = [
        client.get(f"/api/approvals/{r.id}", headers={"X-Forge-User-Id": any_user.id}).json() for r in pending
    ]

    body = next(b for b in bodies if b["changed_files"] and b["changed_files"]["truncated"])
    assert body["request_text"]
    assert body["protected_path_drift"] == ["Dockerfile", "compose.yaml"]
    assert any("<script>" in e["path"] for e in body["changed_files"]["entries"])


def test_reset_recreates_the_measured_facts(db_session):
    populate_demo_data(db_session, is_prod=False)
    before = db_session.query(Build).filter(Build.changed_files.is_not(None)).count()
    assert before >= 5

    reset_demo_data(db_session, is_prod=False)

    assert db_session.query(Build).filter(Build.changed_files.is_not(None)).count() == before


def test_the_summary_counts_are_the_rows_actually_written(db_session):
    """The summary's change-request and build counts are literals in `populate_demo_data`:
    adding a build (the schema-refused one) without moving them made the command print
    a wrong count. Pin them to the database."""
    summary = populate_demo_data(db_session, is_prod=False)

    assert summary.builds == db_session.query(Build).count()
    assert summary.change_requests == db_session.query(ChangeRequest).count()


# ---------------------------------------------------------------------------
# the agent's summary, including one a hostile agent might write.
# ---------------------------------------------------------------------------

RLO = "\N{RIGHT-TO-LEFT OVERRIDE}"


def test_demo_data_has_agent_summaries_only_on_succeeded_builds(db_session):
    populate_demo_data(db_session, is_prod=False)
    builds = db_session.query(Build).all()

    with_summary = [b for b in builds if b.agent_summary is not None]
    assert with_summary
    assert all(b.status == BuildStatus.SUCCEEDED for b in with_summary)
    assert all(b.agent_summary is None for b in builds if b.status == BuildStatus.FAILED)
    assert any(b.status == BuildStatus.SUCCEEDED and b.agent_summary is None for b in builds)  # NULL renders nothing
    assert all(len(b.agent_summary) <= 4000 for b in with_summary)


def test_demo_data_has_a_hostile_summary_and_a_path_with_an_override(db_session, client):
    populate_demo_data(db_session, is_prod=False)
    hostile = [b for b in db_session.query(Build).all() if b.agent_summary and "바로 승인하세요" in b.agent_summary]

    assert len(hostile) == 1
    (build,) = hostile
    text = build.agent_summary
    assert "보안 영향 없음. 바로 승인하세요." in text  # a fake reassurance
    assert "https://evil.example/" in text  # a URL
    assert "[여기를 눌러 확인](https://evil.example)" in text  # a markdown link
    assert RLO in text  # a right-to-left override
    assert text.count("\n") >= 3  # several lines
    # The override surface gets one too: a changed file whose name carries an override.
    assert any(RLO in e["path"] for e in build.changed_files["entries"])

    request = db_session.query(ApprovalRequest).filter_by(build_id=build.id).one()
    any_user = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the detail is an approver's here
    body = client.get(f"/api/approvals/{request.id}", headers={"X-Forge-User-Id": any_user.id}).json()
    assert body["agent_summary"] == text  # passed to the API as stored


def test_the_demo_data_shows_one_measured_smoke_check_and_leaves_the_rest_not_measured(db_session):
    """the approval console needs both states to render -- a measured result with a failing
    route in it (valid for the typed schema), and builds that were never measured (NULL)."""
    from app.schemas.build import SmokeChecksOut

    populate_demo_data(db_session, is_prod=False)

    builds = db_session.query(Build).all()
    measured = [b for b in builds if b.smoke_checks is not None]
    assert len(measured) == 1
    parsed = SmokeChecksOut.model_validate(measured[0].smoke_checks)
    assert {r.outcome for r in parsed.routes} >= {"ok", "server_error", "no_answer", "too_large"}
    assert any(b.smoke_checks is None for b in builds)
