"""monthly cost rollups, two-layer limits, warn-only breach, and the
usage read API.

Standard in-memory SQLite fixtures, dev auth mode (`X-Forge-User-Id`) — no
Docker, no network, no LLM proxy. Every test that involves spend pins an
explicit month (`?month=`), so nothing here changes behaviour as the calendar
moves; the one test that exercises the *default* window asserts a property of
"now" instead of a literal date.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.user import User
from app.services import usage
from app.services.roles import Role
from app.services.status import BuildStatus, ProjectStatus

MONTH = "2026-07"
PREVIOUS_MONTH = "2026-06"


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Forge-User-Id": user_id}


@pytest.fixture()
def window() -> usage.MonthWindow:
    return usage.resolve_month_window(MONTH)


@pytest.fixture()
def requester(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.REQUESTER).first()


@pytest.fixture()
def approver(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.APPROVER).first()


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.ADMIN).first()


@pytest.fixture()
def project(db_session, requester, seeded_ids) -> Project:
    row = Project(
        name="Usage Test Project",
        owner_id=requester.id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _build(
    db_session,
    project: Project,
    *,
    cost_usd: float | None,
    ended_at: datetime | None,
    status: str = BuildStatus.SUCCEEDED,
    requester: User | None = None,
) -> Build:
    """One build's worth of spend. `requester=None` deliberately writes **no**
    `ChangeRequest`, which is how a build becomes unattributed."""
    build = Build(
        project_id=project.id, type="create", status=status, cost_usd=cost_usd, ended_at=ended_at
    )
    db_session.add(build)
    db_session.flush()
    if requester is not None:
        db_session.add(
            ChangeRequest(
                project_id=project.id,
                requester_id=requester.id,
                nl_request="test request",
                build_id=build.id,
            )
        )
        db_session.flush()
    return build


def _inside(window: usage.MonthWindow, days: int = 1) -> datetime:
    return window.starts_at + timedelta(days=days)


# ---------------------------------------------------------------------------
# The monthly window
# ---------------------------------------------------------------------------


def test_the_window_is_the_calendar_month_and_the_month_string_is_canonical():
    w = usage.resolve_month_window("2026-7")
    assert w.month == "2026-07"  # canonicalized, so a response can't be ambiguous
    assert (w.starts_at.year, w.starts_at.month, w.starts_at.day) == (2026, 7, 1)
    # `ends_at` is exclusive: the 1st of the next month, not the 31st.
    assert (w.ends_at.year, w.ends_at.month, w.ends_at.day) == (2026, 8, 1)


def test_december_rolls_the_year_over():
    w = usage.resolve_month_window("2026-12")
    assert (w.ends_at.year, w.ends_at.month) == (2027, 1)


@pytest.mark.parametrize("bad", ["2026", "26-07", "2026-13", "July 2026", "", "2026-07-15"])
def test_a_malformed_month_is_rejected_rather_than_guessed(bad):
    with pytest.raises(usage.InvalidMonthError):
        usage.resolve_month_window(bad)


def test_month_boundaries_are_evaluated_in_the_configured_timezone(monkeypatch):
    """Monthly window: "this month" must mean the same window for
    everyone, evaluated in ONE configured zone — so the boundary instants move
    with `PLATFORM_TIMEZONE`, and are not UTC midnight unless the zone is."""
    monkeypatch.setattr(settings, "platform_timezone", "Asia/Seoul")
    seoul = usage.resolve_month_window(MONTH)
    assert seoul.timezone == "Asia/Seoul"
    # Midnight on 2026-07-01 in +09:00 is 15:00 UTC on 2026-06-30.
    assert seoul.starts_at == datetime(2026, 6, 30, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert seoul.ends_at == datetime(2026, 7, 31, 15, 0, tzinfo=ZoneInfo("UTC"))


def test_a_build_on_the_boundary_lands_in_the_local_month_not_the_utc_one(
    db_session, project, monkeypatch
):
    """The subtle case the timezone setting exists for: 2026-07-01 02:00 in
    Seoul is still 2026-06-30 in UTC. With the platform in Seoul it must count
    against July, and against June if the platform is in UTC."""
    ended_at = datetime(2026, 6, 30, 17, 0, tzinfo=ZoneInfo("UTC"))  # 02:00 KST, Jul 1
    _build(db_session, project, cost_usd=7.0, ended_at=ended_at)

    monkeypatch.setattr(settings, "platform_timezone", "Asia/Seoul")
    assert usage.project_usage_for(
        db_session, usage.resolve_month_window(MONTH), project
    ).spend_usd == 7.0
    assert usage.project_usage_for(
        db_session, usage.resolve_month_window(PREVIOUS_MONTH), project
    ).spend_usd == 0.0

    monkeypatch.setattr(settings, "platform_timezone", "UTC")
    assert usage.project_usage_for(
        db_session, usage.resolve_month_window(MONTH), project
    ).spend_usd == 0.0
    assert usage.project_usage_for(
        db_session, usage.resolve_month_window(PREVIOUS_MONTH), project
    ).spend_usd == 7.0


def test_the_default_window_is_the_current_month_in_the_platform_timezone():
    w = usage.resolve_month_window()
    now_local = datetime.now(settings.platform_tzinfo)
    assert w.month == f"{now_local.year:04d}-{now_local.month:02d}"
    assert w.timezone == settings.platform_timezone
    assert w.starts_at <= now_local < w.ends_at


# ---------------------------------------------------------------------------
# Per-project rollup
# ---------------------------------------------------------------------------


def test_project_rollup_sums_cost_per_project_for_the_month(db_session, project, window, seeded_ids):
    other = Project(
        name="Other Project",
        owner_id=project.owner_id,
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
    )
    db_session.add(other)
    db_session.flush()

    _build(db_session, project, cost_usd=1.5, ended_at=_inside(window))
    _build(db_session, project, cost_usd=2.25, ended_at=_inside(window, days=9))
    _build(db_session, other, cost_usd=10.0, ended_at=_inside(window, days=3))

    by_id = {line.subject_id: line for line in usage.project_usage(db_session, window)}
    assert by_id[project.id].spend_usd == pytest.approx(3.75)
    assert by_id[project.id].build_count == 2
    assert by_id[other.id].spend_usd == pytest.approx(10.0)
    assert by_id[project.id].kind == usage.UsageSubject.PROJECT


def test_a_project_with_no_builds_this_month_is_still_a_line(db_session, project, window):
    """Otherwise "not in breach" and "not listed" are indistinguishable."""
    line = usage.project_usage_for(db_session, window, project)
    assert line.spend_usd == 0.0
    assert line.build_count == 0
    assert line.effective_limit_usd == settings.quota_monthly_usd_default
    assert line.in_breach is False


def test_failed_builds_count(db_session, project, window):
    """A build that burned tokens and then failed
    spent real money. Excluding failures would make the total wrong in the
    direction that hides problems."""
    _build(
        db_session, project, cost_usd=4.0, ended_at=_inside(window), status=BuildStatus.FAILED
    )
    line = usage.project_usage_for(db_session, window, project)
    assert line.spend_usd == pytest.approx(4.0)
    assert line.build_count == 1


def test_null_cost_is_summed_as_zero_but_reported_as_unmeasured(db_session, project, window):
    """`NULL` means *not
    measured* and is a different fact from a measured `0.0`. Both sum to the
    same number, so the distinction has to survive somewhere — it survives in
    `unmeasured_build_count`."""
    _build(db_session, project, cost_usd=None, ended_at=_inside(window))
    _build(db_session, project, cost_usd=0.0, ended_at=_inside(window, days=2))
    _build(db_session, project, cost_usd=3.0, ended_at=_inside(window, days=3))

    line = usage.project_usage_for(db_session, window, project)
    assert line.spend_usd == pytest.approx(3.0)
    assert line.build_count == 3
    # Exactly one of the two zero-contributing builds was never measured.
    assert line.unmeasured_build_count == 1


def test_only_null_costs_count_as_unmeasured(db_session, project, window):
    """The other half of the same rule, so a mutation that treats `0.0` as
    unmeasured (or vice versa) fails."""
    _build(db_session, project, cost_usd=0.0, ended_at=_inside(window))
    assert usage.project_usage_for(db_session, window, project).unmeasured_build_count == 0


def test_a_build_that_has_not_ended_belongs_to_no_month(db_session, project, window):
    """Cost counts against the month the build
    ENDED. A running/queued build has no such month, so it is in no window's
    total — and it is not lost either: it starts counting in whichever month it
    eventually ends in."""
    running = _build(db_session, project, cost_usd=99.0, ended_at=None, status=BuildStatus.RUNNING)

    assert usage.project_usage_for(db_session, window, project).spend_usd == 0.0
    assert usage.project_usage_for(
        db_session, usage.resolve_month_window(PREVIOUS_MONTH), project
    ).spend_usd == 0.0

    running.ended_at = _inside(window)
    db_session.flush()
    assert usage.project_usage_for(db_session, window, project).spend_usd == pytest.approx(99.0)


def test_historical_months_stay_queryable_and_a_rollover_deletes_nothing(
    db_session, project, window
):
    """A reset is a new window, not a deletion."""
    previous = usage.resolve_month_window(PREVIOUS_MONTH)
    _build(db_session, project, cost_usd=12.0, ended_at=_inside(previous, days=5))
    _build(db_session, project, cost_usd=2.0, ended_at=_inside(window))

    assert usage.project_usage_for(db_session, previous, project).spend_usd == pytest.approx(12.0)
    assert usage.project_usage_for(db_session, window, project).spend_usd == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Per-user rollup + unattributed spend
# ---------------------------------------------------------------------------


def test_user_rollup_attributes_spend_through_the_change_request(
    db_session, project, window, requester, approver
):
    _build(db_session, project, cost_usd=5.0, ended_at=_inside(window), requester=requester)
    _build(db_session, project, cost_usd=1.0, ended_at=_inside(window, days=2), requester=approver)

    assert usage.user_usage_for(db_session, window, requester).spend_usd == pytest.approx(5.0)
    assert usage.user_usage_for(db_session, window, approver).spend_usd == pytest.approx(1.0)


def test_unattributed_spend_is_its_own_line_and_is_never_folded_into_the_owner(
    db_session, project, window, requester
):
    """the watch-out this task exists to honour.

    Per-user attribution depends on `ChangeRequest.build_id`. A build with no
    change request must be reported as unattributed, NOT credited to the
    project owner: `services/approval.resolve_requester_for_build` falls back to
    the owner (so the self-approval ban never fails open), and reusing that
    fallback here would silently move someone else's spend onto the owner and
    make per-user totals stop reconciling against per-project ones."""
    _build(db_session, project, cost_usd=8.0, ended_at=_inside(window))  # no ChangeRequest
    assert project.owner_id == requester.id

    lines = usage.user_usage(db_session, window)
    unattributed = [line for line in lines if line.kind == usage.UsageSubject.UNATTRIBUTED]
    assert len(unattributed) == 1
    assert unattributed[0].spend_usd == pytest.approx(8.0)
    assert unattributed[0].subject_id is None
    # The owner is not credited with it, even though the ban would resolve to
    # them for this same build.
    assert usage.user_usage_for(db_session, window, requester).spend_usd == 0.0
    from app.services.approval import resolve_requester_for_build

    orphan = db_session.query(Build).filter(Build.cost_usd == 8.0).one()
    assert (
        resolve_requester_for_build(
            db_session, build_id=orphan.id, project_owner_id=project.owner_id
        )
        == requester.id
    )


def test_the_unattributed_line_is_present_even_when_it_is_zero(db_session, project, window, requester):
    """A line that appears only when nonzero cannot be used to reconcile."""
    _build(db_session, project, cost_usd=3.0, ended_at=_inside(window), requester=requester)
    kinds = [line.kind for line in usage.user_usage(db_session, window)]
    assert kinds.count(usage.UsageSubject.UNATTRIBUTED) == 1
    unattributed = next(
        line for line in usage.user_usage(db_session, window)
        if line.kind == usage.UsageSubject.UNATTRIBUTED
    )
    assert unattributed.spend_usd == 0.0
    # No subject, so no limit and no breach — the project rollup is where that
    # spend meets a limit.
    assert unattributed.effective_limit_usd is None
    assert unattributed.in_breach is False


def test_unmeasured_builds_are_reported_on_the_unattributed_line_too(db_session, project, window):
    _build(db_session, project, cost_usd=None, ended_at=_inside(window))
    unattributed = next(
        line for line in usage.user_usage(db_session, window)
        if line.kind == usage.UsageSubject.UNATTRIBUTED
    )
    assert unattributed.build_count == 1
    assert unattributed.unmeasured_build_count == 1


# ---------------------------------------------------------------------------
# Limits and breach
# ---------------------------------------------------------------------------


def test_a_null_override_resolves_to_the_default_and_is_not_unlimited(monkeypatch):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    limit, source = usage.effective_limit(None)
    assert (limit, source) == (100.0, usage.QuotaLimitSource.DEFAULT)
    # "Not unlimited" is the load-bearing half: spending past the default with
    # no override set is still a breach.
    assert usage.is_in_breach(100.01, limit) is True


def test_an_override_wins_over_the_default(monkeypatch):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    assert usage.effective_limit(250.0) == (250.0, usage.QuotaLimitSource.OVERRIDE)


def test_a_zero_override_means_any_spend_is_a_breach_not_unlimited():
    """`0.0` is a legal limit used to flag a subject. The
    failure mode this pins is falsy-check code (`if override:`) treating it as
    "no override" and quietly resolving to the 100 USD default."""
    limit, source = usage.effective_limit(0.0)
    assert (limit, source) == (0.0, usage.QuotaLimitSource.OVERRIDE)
    assert usage.is_in_breach(0.01, limit) is True
    # Strictly greater, so a subject that spent nothing is not in breach of a
    # zero limit either.
    assert usage.is_in_breach(0.0, limit) is False


def test_breach_is_a_read_time_comparison_that_follows_the_override(
    db_session, project, window, monkeypatch
):
    """Derived, not stored. Nothing about the
    build rows changes between these three reads — only the override does."""
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    _build(db_session, project, cost_usd=40.0, ended_at=_inside(window))

    assert usage.project_usage_for(db_session, window, project).in_breach is False

    project.monthly_quota_usd = 10.0
    db_session.flush()
    assert usage.project_usage_for(db_session, window, project).in_breach is True

    project.monthly_quota_usd = None
    db_session.flush()
    assert usage.project_usage_for(db_session, window, project).in_breach is False


def test_a_negative_override_is_rejected(db_session, project, admin):
    with pytest.raises(usage.QuotaValidationError):
        usage.set_monthly_quota(
            db_session,
            actor_id=admin.id,
            subject_kind=usage.UsageSubject.PROJECT,
            subject=project,
            new_quota=-1.0,
        )
    assert project.monthly_quota_usd is None


def test_setting_a_quota_audit_logs_both_old_and_new_values(db_session, project, admin):
    project.monthly_quota_usd = 50.0
    db_session.flush()

    old = usage.set_monthly_quota(
        db_session,
        actor_id=admin.id,
        subject_kind=usage.UsageSubject.PROJECT,
        subject=project,
        new_quota=75.0,
    )
    db_session.flush()

    assert old == 50.0
    entry = db_session.query(AuditLog).filter_by(action="project.quota_changed").one()
    assert entry.actor_id == admin.id
    assert entry.target == f"project:{project.id}"
    assert entry.metadata_json == {"old_monthly_quota_usd": 50.0, "new_monthly_quota_usd": 75.0}


# ---------------------------------------------------------------------------
# The platform summary
# ---------------------------------------------------------------------------


def test_platform_summary_totals_include_unattributed_spend(
    db_session, project, window, requester, monkeypatch
):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    _build(db_session, project, cost_usd=6.0, ended_at=_inside(window), requester=requester)
    _build(db_session, project, cost_usd=4.0, ended_at=_inside(window, days=2))  # unattributed
    _build(db_session, project, cost_usd=None, ended_at=_inside(window, days=3), requester=requester)

    summary = usage.platform_usage(db_session, window)
    assert summary.total_spend_usd == pytest.approx(10.0)
    assert summary.unattributed_spend_usd == pytest.approx(4.0)
    assert summary.build_count == 3
    assert summary.unmeasured_build_count == 1
    assert summary.default_limit_usd == 100.0
    assert summary.projects_in_breach == []

    project.monthly_quota_usd = 1.0
    db_session.flush()
    assert usage.platform_usage(db_session, window).projects_in_breach == [project.id]


# ---------------------------------------------------------------------------
# Read API permissions
# ---------------------------------------------------------------------------


def test_a_project_owner_reads_their_own_project_usage(client, db_session, project, window, requester):
    _build(db_session, project, cost_usd=2.5, ended_at=_inside(window))
    db_session.commit()

    resp = client.get(
        f"/api/usage/projects/{project.id}", params={"month": MONTH}, headers=_headers(requester.id)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"]["spend_usd"] == pytest.approx(2.5)
    assert body["subject"]["subject_id"] == project.id
    # spec rule 3: the response states the window it was computed for.
    assert body["window"]["month"] == MONTH
    assert body["window"]["timezone"] == settings.platform_timezone


def test_a_non_owner_without_admin_cannot_read_a_project_usage(
    client, db_session, project, approver, admin
):
    db_session.commit()
    assert (
        client.get(f"/api/usage/projects/{project.id}", headers=_headers(approver.id)).status_code
        == 403
    )
    # `admin` overrides ownership; `approver` does not.
    assert (
        client.get(f"/api/usage/projects/{project.id}", headers=_headers(admin.id)).status_code
        == 200
    )


def test_a_user_reads_their_own_total_but_not_another_users(
    client, db_session, window, project, requester, approver, admin
):
    _build(db_session, project, cost_usd=1.0, ended_at=_inside(window), requester=requester)
    db_session.commit()

    own = client.get(
        f"/api/usage/users/{requester.id}", params={"month": MONTH}, headers=_headers(requester.id)
    )
    assert own.status_code == 200
    assert own.json()["subject"]["spend_usd"] == pytest.approx(1.0)

    # The leak this endpoint has to refuse: reading someone else's figure by id.
    assert (
        client.get(f"/api/usage/users/{requester.id}", headers=_headers(approver.id)).status_code
        == 403
    )
    assert (
        client.get(f"/api/usage/users/{requester.id}", headers=_headers(admin.id)).status_code == 200
    )


def test_asking_about_an_unknown_user_never_reveals_whether_it_exists(client, db_session, approver, admin):
    db_session.commit()
    assert client.get("/api/usage/users/nope", headers=_headers(approver.id)).status_code == 403
    assert client.get("/api/usage/users/nope", headers=_headers(admin.id)).status_code == 404


@pytest.mark.parametrize(
    "path", ["/api/usage/projects", "/api/usage/users", "/api/usage/summary"]
)
def test_every_cross_subject_read_requires_admin(client, db_session, path, requester, approver, admin):
    """Reads across subjects require admin. These are the endpoints with no ownership
    filter at all, which is deliberate: an unfiltered default that answered a
    non-admin would expose every project and every user's spend."""
    db_session.commit()
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_headers(requester.id)).status_code == 403
    assert client.get(path, headers=_headers(approver.id)).status_code == 403
    assert client.get(path, headers=_headers(admin.id)).status_code == 200


def test_anonymous_reads_are_401(client, db_session, project):
    db_session.commit()
    assert client.get(f"/api/usage/projects/{project.id}").status_code == 401
    assert client.get("/api/usage/users/anyone").status_code == 401


def test_the_admin_project_list_carries_limits_and_breach_flags(
    client, db_session, project, window, admin, monkeypatch
):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    _build(db_session, project, cost_usd=120.0, ended_at=_inside(window))
    db_session.commit()

    body = client.get(
        "/api/usage/projects", params={"month": MONTH}, headers=_headers(admin.id)
    ).json()
    line = next(s for s in body["subjects"] if s["subject_id"] == project.id)
    assert line["spend_usd"] == pytest.approx(120.0)
    assert line["effective_limit_usd"] == 100.0
    assert line["limit_source"] == "default"
    assert line["in_breach"] is True


def test_the_admin_user_list_includes_the_unattributed_line(client, db_session, project, window, admin):
    _build(db_session, project, cost_usd=9.0, ended_at=_inside(window))
    db_session.commit()

    body = client.get(
        "/api/usage/users", params={"month": MONTH}, headers=_headers(admin.id)
    ).json()
    unattributed = [s for s in body["subjects"] if s["kind"] == "unattributed"]
    assert len(unattributed) == 1
    assert unattributed[0]["spend_usd"] == pytest.approx(9.0)


def test_a_malformed_month_query_is_400(client, db_session, admin):
    db_session.commit()
    resp = client.get("/api/usage/summary", params={"month": "last-july"}, headers=_headers(admin.id))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The admin override endpoints
# ---------------------------------------------------------------------------


def test_admin_sets_a_project_override_and_it_takes_effect_on_the_next_read(
    client, db_session, project, window, admin, monkeypatch
):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    _build(db_session, project, cost_usd=40.0, ended_at=_inside(window))
    db_session.commit()

    resp = client.patch(
        f"/api/usage/quotas/projects/{project.id}",
        json={"monthly_quota_usd": 10.0},
        headers=_headers(admin.id),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "subject_kind": "project",
        "subject_id": project.id,
        "monthly_quota_usd": 10.0,
        "effective_limit_usd": 10.0,
        "limit_source": "override",
    }

    # Effective immediately — no restart, nothing to invalidate.
    body = client.get(
        f"/api/usage/projects/{project.id}", params={"month": MONTH}, headers=_headers(admin.id)
    ).json()
    assert body["subject"]["in_breach"] is True

    entry = db_session.query(AuditLog).filter_by(action="project.quota_changed").one()
    assert entry.actor_id == admin.id
    assert entry.metadata_json == {"old_monthly_quota_usd": None, "new_monthly_quota_usd": 10.0}


def test_clearing_an_override_returns_the_subject_to_the_default(
    client, db_session, requester, admin, monkeypatch
):
    monkeypatch.setattr(settings, "quota_monthly_usd_default", 100.0)
    requester.monthly_quota_usd = 500.0
    db_session.commit()

    resp = client.patch(
        f"/api/usage/quotas/users/{requester.id}",
        json={"monthly_quota_usd": None},
        headers=_headers(admin.id),
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_quota_usd"] is None
    # Cleared means "use the default", not "unlimited".
    assert resp.json()["effective_limit_usd"] == 100.0
    assert resp.json()["limit_source"] == "default"

    entry = db_session.query(AuditLog).filter_by(action="user.quota_changed").one()
    assert entry.metadata_json == {"old_monthly_quota_usd": 500.0, "new_monthly_quota_usd": None}


def test_a_zero_override_is_accepted_through_the_api(client, db_session, project, admin):
    db_session.commit()
    resp = client.patch(
        f"/api/usage/quotas/projects/{project.id}",
        json={"monthly_quota_usd": 0.0},
        headers=_headers(admin.id),
    )
    assert resp.status_code == 200
    assert resp.json()["effective_limit_usd"] == 0.0
    assert resp.json()["limit_source"] == "override"


def test_a_negative_override_is_400_through_the_api(client, db_session, project, admin):
    db_session.commit()
    resp = client.patch(
        f"/api/usage/quotas/projects/{project.id}",
        json={"monthly_quota_usd": -5.0},
        headers=_headers(admin.id),
    )
    assert resp.status_code == 400
    db_session.refresh(project)
    assert project.monthly_quota_usd is None


@pytest.mark.parametrize("subject_kind", ["projects", "users"])
def test_only_admin_may_change_an_override(
    client, db_session, project, subject_kind, requester, approver, admin
):
    db_session.commit()
    subject_id = project.id if subject_kind == "projects" else requester.id
    path = f"/api/usage/quotas/{subject_kind}/{subject_id}"
    body = {"monthly_quota_usd": 1.0}

    assert client.patch(path, json=body).status_code == 401
    # Not even the project's own owner: a subject raising its own limit would
    # make the limit meaningless (only `admin` may set it).
    assert client.patch(path, json=body, headers=_headers(requester.id)).status_code == 403
    assert client.patch(path, json=body, headers=_headers(approver.id)).status_code == 403
    assert client.patch(path, json=body, headers=_headers(admin.id)).status_code == 200


@pytest.mark.parametrize("path", ["/api/usage/quotas/projects/nope", "/api/usage/quotas/users/nope"])
def test_an_unknown_override_subject_is_404(client, db_session, path, admin):
    db_session.commit()
    resp = client.patch(path, json={"monthly_quota_usd": 1.0}, headers=_headers(admin.id))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Warn only: nothing on the build path consults accumulated spend
# ---------------------------------------------------------------------------


PIPELINE_MODULES = (
    "routers/projects.py",
    "services/build_queue.py",
    "services/sandbox_build_executor.py",
    "services/deploy.py",
    "services/deploy_manager.py",
    "routers/deployments.py",
    "worker.py",
)


def test_no_build_pipeline_module_imports_the_usage_service():
    """Pinned mechanically rather than by review.

    Warn-only is a claim about what the build path does NOT do, and the only
    durable way to hold that is to assert the pipeline cannot reach the rollup
    at all: submitting a change request, queueing a build, executing it and
    deploying it never consult accumulated spend, so no rollup query can end up
    between a user and their work.

    `app/routers/usage.py` and `app/services/usage.py` are of course allowed to
    import it; nothing in this list may."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for relative in PIPELINE_MODULES:
        module_path = app_dir / relative
        assert module_path.exists(), f"{relative} moved — update this test's list"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "usage" in (node.module or ""):
                offenders.append(f"{relative}: from {node.module}")
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{relative}: import {alias.name}"
                    for alias in node.names
                    if "usage" in alias.name
                )
            # `from app.services import usage`
            if isinstance(node, ast.ImportFrom) and node.module == "app.services":
                offenders.extend(
                    f"{relative}: from app.services import {alias.name}"
                    for alias in node.names
                    if "usage" in alias.name  # `usage`, `usage_dashboard`
                )
    assert offenders == []


def test_a_subject_far_over_its_limit_can_still_submit_work(
    client, db_session, project, window, requester
):
    """The behavioural half of warn-only: a project 100x over its
    limit keeps working. If a future change ever puts a rollup on the build
    path, this is the test that fails."""
    project.monthly_quota_usd = 1.0
    project.confirmed_ref = "c0ffee0"  # a modify request needs one; this test is about quota
    _build(db_session, project, cost_usd=100.0, ended_at=_inside(window), requester=requester)
    db_session.commit()

    assert usage.project_usage_for(db_session, window, project).in_breach is True

    resp = client.post(
        f"/api/projects/{project.id}/change-requests",
        json={"nl_request": "add a CSV export button"},
        headers=_headers(requester.id),
    )
    assert resp.status_code == 201


def test_there_is_no_resource_usage_table(db_session):
    """rollups stay derived. `ResourceUsage` has been listed as
    deferred from the start and is deferred *by decision* now — a materialized
    rollup would be a second copy of `builds.cost_usd`, and second copies
    drift. Materializing it needs a new ADR with evidence, which would also
    change this test."""
    assert "resource_usage" not in Base.metadata.tables
    assert not any("usage" in name for name in Base.metadata.tables)
