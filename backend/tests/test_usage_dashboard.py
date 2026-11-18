"""the cost dashboard read.

In-memory SQLite, dev auth mode (`X-Forge-User-Id`), no Docker. Every test pins
`platform_timezone` explicitly where the zone matters (never
depend on ambient `.env`) and every test that needs "today" passes it in, or uses
a month that is wholly in the past, so nothing here moves with the real date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings, settings
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.user import User
from app.services import usage, usage_dashboard
from app.services.roles import Role
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus

MONTH = "2026-07"


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Forge-User-Id": user_id}


@pytest.fixture(autouse=True)
def _pin_zone(monkeypatch):
    """UTC unless a test says otherwise, whatever `.env` holds."""
    monkeypatch.setattr(settings, "platform_timezone", "UTC")


@pytest.fixture()
def requester(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.REQUESTER).first()


@pytest.fixture()
def admin(db_session) -> User:
    return db_session.query(User).filter_by(role=Role.ADMIN).first()


@pytest.fixture()
def project(db_session, requester, seeded_ids) -> Project:
    row = Project(
        name="Dashboard Test Project",
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
    failure_reason: str | None = None,
    requester: User | None = None,
    cost_tokens: int | None = None,
) -> Build:
    """`requester=None` writes no `ChangeRequest`: the build is unattributed."""
    build = Build(
        project_id=project.id,
        type="create",
        status=status,
        failure_reason=failure_reason,
        cost_usd=cost_usd,
        cost_tokens=cost_tokens,
        ended_at=ended_at,
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


def _at(day: int, hour: int = 12, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=UTC)


def _service(db_session, month: str = MONTH, **kwargs) -> usage_dashboard.CostDashboard:
    # `today` far after the month unless a test says otherwise: a full month.
    kwargs.setdefault("today", date(2027, 1, 1))
    return usage_dashboard.cost_dashboard(db_session, usage.resolve_month_window(month), **kwargs)


# ---------------------------------------------------------------------------
# Day bucketing: platform timezone, every day present, no future days
# ---------------------------------------------------------------------------


def test_days_are_bucketed_in_the_platform_timezone_not_utc(db_session, project, monkeypatch):
    monkeypatch.setattr(settings, "platform_timezone", "Asia/Seoul")
    # 16:00 UTC on the 10th is 01:00 on the 11th in Seoul (+09:00): two different
    # calendar days, depending on the zone.
    _build(db_session, project, cost_usd=2.0, ended_at=_at(10, 16))
    # 14:59 UTC on the 10th is still 23:59 on the 10th in Seoul.
    _build(db_session, project, cost_usd=1.0, ended_at=_at(10, 14) + timedelta(minutes=59))
    # 16:00 UTC on 30 June is 01:00 on 1 July in Seoul: in the July window, and on
    # July 1st -- UTC would say June 30th, a different month altogether.
    _build(db_session, project, cost_usd=4.0, ended_at=datetime(2026, 6, 30, 16, 0, tzinfo=UTC))
    db_session.commit()

    by_day = {b.day: b for b in _service(db_session).days}

    assert by_day[date(2026, 7, 11)].spend_usd == pytest.approx(2.0)
    assert by_day[date(2026, 7, 10)].spend_usd == pytest.approx(1.0)
    assert by_day[date(2026, 7, 1)].spend_usd == pytest.approx(4.0)
    assert sum(b.build_count for b in by_day.values()) == 3


def test_every_day_of_a_past_month_is_present_with_zeros(db_session, project):
    _build(db_session, project, cost_usd=3.0, ended_at=_at(5))
    db_session.commit()

    days = _service(db_session).days

    assert [b.day for b in days] == [date(2026, 7, n) for n in range(1, 32)]
    empty = next(b for b in days if b.day == date(2026, 7, 6))
    assert (empty.spend_usd, empty.build_count, empty.unmeasured_build_count) == (0.0, 0, 0)
    assert empty.failed_spend_usd == 0.0
    assert next(b for b in days if b.day == date(2026, 7, 5)).spend_usd == pytest.approx(3.0)


def test_in_the_current_month_days_after_today_are_omitted(db_session, project):
    _build(db_session, project, cost_usd=3.0, ended_at=_at(5))
    db_session.commit()

    days = _service(db_session, today=date(2026, 7, 10)).days

    assert [b.day for b in days] == [date(2026, 7, n) for n in range(1, 11)]


def test_a_future_day_that_holds_builds_is_kept(db_session, project):
    """A day after `today` that holds builds (clock skew) is kept: dropping it
    would make the buckets disagree with the summary."""
    _build(db_session, project, cost_usd=3.0, ended_at=_at(20))
    db_session.commit()

    days = _service(db_session, today=date(2026, 7, 10)).days

    assert days[-1].day == date(2026, 7, 20)
    assert date(2026, 7, 15) not in [b.day for b in days]
    assert sum(b.spend_usd for b in days) == pytest.approx(3.0)


def test_omission_applies_only_when_today_is_in_the_windows_month(db_session):
    # `today` in a later month: July is a past month, so it is complete.
    assert len(_service(db_session, today=date(2026, 8, 2)).days) == 31


def test_failed_spend_is_a_subset_of_the_days_spend(db_session, project):
    _build(db_session, project, cost_usd=3.0, ended_at=_at(5))
    _build(
        db_session,
        project,
        cost_usd=5.0,
        ended_at=_at(5, 20),
        status=BuildStatus.FAILED,
        failure_reason=BuildFailureReason.BUDGET_EXCEEDED,
    )
    db_session.commit()

    day = next(b for b in _service(db_session).days if b.day == date(2026, 7, 5))

    assert day.spend_usd == pytest.approx(8.0)
    assert day.failed_spend_usd == pytest.approx(5.0)
    assert day.build_count == 2


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_daily_outcome_and_summary_totals_reconcile(client, db_session, project, requester, admin):
    # Succeeded, attributed.
    _build(db_session, project, cost_usd=1.5, ended_at=_at(2), requester=requester)
    _build(db_session, project, cost_usd=2.25, ended_at=_at(9), requester=requester)
    # Failed, three different reasons (one of them unmeasured).
    _build(
        db_session,
        project,
        cost_usd=4.0,
        ended_at=_at(9, 20),
        status=BuildStatus.FAILED,
        failure_reason=BuildFailureReason.BUDGET_EXCEEDED,
        requester=requester,
    )
    _build(
        db_session,
        project,
        cost_usd=0.5,
        ended_at=_at(15),
        status=BuildStatus.FAILED,
        failure_reason=BuildFailureReason.TIMEOUT,
    )
    _build(
        db_session,
        project,
        cost_usd=None,
        ended_at=_at(16),
        status=BuildStatus.FAILED,
        failure_reason=BuildFailureReason.INFRA_ERROR,
    )
    # Unmeasured and succeeded.
    _build(db_session, project, cost_usd=None, ended_at=_at(17), requester=requester)
    # Unattributed (no change request), measured.
    _build(db_session, project, cost_usd=0.75, ended_at=_at(18))
    # Outside the window: the last minute of June, the first instant of August, and
    # a build that has not ended.
    _build(db_session, project, cost_usd=100.0, ended_at=datetime(2026, 6, 30, 23, 59, tzinfo=UTC))
    _build(db_session, project, cost_usd=200.0, ended_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
    _build(db_session, project, cost_usd=300.0, ended_at=None, status=BuildStatus.RUNNING)
    db_session.commit()

    dash = client.get(f"/api/usage/dashboard?month={MONTH}", headers=_headers(admin.id))
    summary = client.get(f"/api/usage/summary?month={MONTH}", headers=_headers(admin.id))
    assert dash.status_code == 200 and summary.status_code == 200
    dash, summary = dash.json(), summary.json()

    daily_spend = sum(d["spend_usd"] for d in dash["days"])
    outcome_spend = sum(o["spend_usd"] for o in dash["outcomes"])
    assert daily_spend == pytest.approx(9.0)
    assert outcome_spend == pytest.approx(daily_spend)
    assert daily_spend == pytest.approx(summary["total_spend_usd"])

    daily_builds = sum(d["build_count"] for d in dash["days"])
    outcome_builds = sum(o["build_count"] for o in dash["outcomes"])
    assert daily_builds == outcome_builds == summary["build_count"] == 7

    daily_unmeasured = sum(d["unmeasured_build_count"] for d in dash["days"])
    outcome_unmeasured = sum(o["unmeasured_build_count"] for o in dash["outcomes"])
    assert daily_unmeasured == outcome_unmeasured == summary["unmeasured_build_count"] == 2

    # The FAILED outcome's reasons partition the FAILED outcome.
    failed = next(o for o in dash["outcomes"] if o["status"] == BuildStatus.FAILED)
    assert {r["failure_reason"] for r in failed["failure_reasons"]} == {
        BuildFailureReason.BUDGET_EXCEEDED,
        BuildFailureReason.TIMEOUT,
        BuildFailureReason.INFRA_ERROR,
    }
    assert sum(r["spend_usd"] for r in failed["failure_reasons"]) == pytest.approx(
        failed["spend_usd"]
    )
    assert sum(r["build_count"] for r in failed["failure_reasons"]) == failed["build_count"]
    assert sum(d["failed_spend_usd"] for d in dash["days"]) == pytest.approx(failed["spend_usd"])
    # Builds outside the window appear nowhere (their costs are 100/200/300).
    assert all(b["cost_usd"] < 100.0 for b in dash["expensive_builds"])
    assert dash["window"]["month"] == MONTH
    assert dash["window"]["timezone"] == "UTC"


def test_non_failed_statuses_have_no_failure_reasons_and_unknown_reason_is_null(
    db_session, project
):
    _build(db_session, project, cost_usd=1.0, ended_at=_at(2))
    _build(db_session, project, cost_usd=2.0, ended_at=_at(3), status=BuildStatus.FAILED)
    db_session.commit()

    outcomes = {o.status: o for o in _service(db_session).outcomes}

    assert outcomes[BuildStatus.SUCCEEDED].failure_reasons == []
    assert [r.failure_reason for r in outcomes[BuildStatus.FAILED].failure_reasons] == [None]


def test_an_empty_month_is_all_zero_and_does_not_fail(db_session):
    dash = _service(db_session)

    assert dash.outcomes == []
    assert dash.expensive_builds == []
    assert len(dash.days) == 31 and sum(b.spend_usd for b in dash.days) == 0.0


# ---------------------------------------------------------------------------
# Most expensive builds
# ---------------------------------------------------------------------------


def test_most_expensive_builds_are_measured_only_highest_first(db_session, project, requester):
    cheap = _build(db_session, project, cost_usd=1.0, ended_at=_at(2), requester=requester)
    top = _build(
        db_session,
        project,
        cost_usd=9.0,
        ended_at=_at(3),
        status=BuildStatus.FAILED,
        failure_reason=BuildFailureReason.BUDGET_EXCEEDED,
        requester=requester,
        cost_tokens=1234,
    )
    zero = _build(db_session, project, cost_usd=0.0, ended_at=_at(4), requester=requester)
    _build(db_session, project, cost_usd=None, ended_at=_at(5), requester=requester)
    db_session.commit()

    listed = _service(db_session).expensive_builds

    # The unmeasured build is absent; a measured 0.0 is present (it is a fact).
    assert [b.build_id for b in listed] == [top.id, cheap.id, zero.id]
    assert listed[0].status == BuildStatus.FAILED
    assert listed[0].failure_reason == BuildFailureReason.BUDGET_EXCEEDED
    assert listed[0].cost_tokens == 1234
    assert listed[0].project_id == project.id
    assert listed[0].requester_id == requester.id


def test_limit_is_respected(db_session, project):
    for n in range(1, 8):
        _build(db_session, project, cost_usd=float(n), ended_at=_at(n))
    db_session.commit()

    listed = _service(db_session, limit=3).expensive_builds

    assert [b.cost_usd for b in listed] == [7.0, 6.0, 5.0]


def test_ties_are_broken_by_build_id_ascending(db_session, project):
    ids = [_build(db_session, project, cost_usd=5.0, ended_at=_at(n)).id for n in (2, 3, 4, 5)]
    db_session.commit()

    assert [b.build_id for b in _service(db_session).expensive_builds] == sorted(ids)
    # And the cut falls at the same place: the lowest ids survive a small limit.
    assert [b.build_id for b in _service(db_session, limit=2).expensive_builds] == sorted(ids)[:2]


def test_an_unattributed_build_has_a_null_requester(db_session, project):
    _build(db_session, project, cost_usd=5.0, ended_at=_at(2))
    db_session.commit()

    assert _service(db_session).expensive_builds[0].requester_id is None


def test_builds_outside_the_window_are_not_listed(db_session, project):
    _build(db_session, project, cost_usd=99.0, ended_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
    _build(db_session, project, cost_usd=1.0, ended_at=_at(2))
    db_session.commit()

    assert [b.cost_usd for b in _service(db_session).expensive_builds] == [1.0]


# ---------------------------------------------------------------------------
# The metering caveat
# ---------------------------------------------------------------------------


def test_the_caveat_date_is_pinned_and_is_not_a_setting():
    assert usage_dashboard.METERING_CAVEAT_DATE == date(2026, 10, 1)
    assert not any("caveat" in name for name in Settings.model_fields)


@pytest.mark.parametrize(
    ("month", "before"),
    [("2026-07", True), ("2026-09", True), ("2026-10", False), ("2026-11", False), ("2027-01", False)],
)
def test_the_response_says_whether_the_window_starts_before_the_caveat(db_session, month, before):
    caveat = _service(db_session, month=month).metering_caveat

    assert caveat.date == date(2026, 10, 1)
    assert caveat.window_starts_before is before


def test_the_caveat_boundary_is_judged_in_the_platform_timezone(db_session, monkeypatch):
    """October's window starts at 2026-09-30T15:00Z in Seoul: a UTC-date
    comparison would call it 'before 2026-10-01'; the local date is the 1st."""
    monkeypatch.setattr(settings, "platform_timezone", "Asia/Seoul")

    assert _service(db_session, month="2026-10").metering_caveat.window_starts_before is False
    assert _service(db_session, month="2026-09").metering_caveat.window_starts_before is True


# ---------------------------------------------------------------------------
# The route: authorization, validation, shape
# ---------------------------------------------------------------------------


def test_unauthenticated_is_401(client):
    assert client.get("/api/usage/dashboard").status_code == 401


@pytest.mark.parametrize("role", [Role.REQUESTER, Role.APPROVER])
def test_non_admin_is_403(client, db_session, role):
    user = db_session.query(User).filter_by(role=role).first()

    resp = client.get(f"/api/usage/dashboard?month={MONTH}", headers=_headers(user.id))

    assert resp.status_code == 403


def test_the_role_check_runs_before_any_dashboard_work(client, requester, monkeypatch):
    """A non-admin must never cause the dashboard query to run, and gets 403 even
    when the request is also invalid (bad limit, bad month)."""

    def _boom(*args, **kwargs):
        raise AssertionError("the dashboard was computed for a non-admin")

    monkeypatch.setattr(usage_dashboard, "cost_dashboard", _boom)

    for query in (f"month={MONTH}", "limit=0", "month=garbage"):
        resp = client.get(f"/api/usage/dashboard?{query}", headers=_headers(requester.id))
        assert resp.status_code == 403, query


def test_the_admin_gets_the_documented_shape(client, db_session, project, admin, requester):
    _build(db_session, project, cost_usd=2.0, ended_at=_at(2), requester=requester)
    db_session.commit()

    resp = client.get(f"/api/usage/dashboard?month={MONTH}", headers=_headers(admin.id))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"window", "limit", "days", "outcomes", "expensive_builds", "metering_caveat"}
    assert body["limit"] == 10
    assert body["days"][0] == {
        "day": "2026-07-01",
        "spend_usd": 0.0,
        "build_count": 0,
        "unmeasured_build_count": 0,
        "failed_spend_usd": 0.0,
    }
    assert body["metering_caveat"] == {"date": "2026-10-01", "window_starts_before": True}
    assert set(body["expensive_builds"][0]) == {
        "build_id",
        "project_id",
        "type",
        "status",
        "failure_reason",
        "cost_usd",
        "cost_tokens",
        "ended_at",
        "requester_id",
    }


def test_a_malformed_month_is_400(client, admin):
    resp = client.get("/api/usage/dashboard?month=2026-13", headers=_headers(admin.id))

    assert resp.status_code == 400


@pytest.mark.parametrize("limit", [0, -1, 51, 1000])
def test_a_limit_outside_1_to_50_is_422(client, admin, limit):
    resp = client.get(
        f"/api/usage/dashboard?month={MONTH}&limit={limit}", headers=_headers(admin.id)
    )

    assert resp.status_code == 422


def test_the_default_limit_is_ten_and_the_maximum_is_fifty(client, db_session, project, admin):
    for n in range(1, 29):
        for k in range(2):
            _build(db_session, project, cost_usd=float(n * 10 + k), ended_at=_at(n))
    db_session.commit()  # 56 measured builds

    def read(query: str) -> dict:
        resp = client.get(f"/api/usage/dashboard?month={MONTH}{query}", headers=_headers(admin.id))
        assert resp.status_code == 200
        return resp.json()

    default, maximum, one = read(""), read("&limit=50"), read("&limit=1")

    assert len(default["expensive_builds"]) == 10 and default["limit"] == 10
    assert len(maximum["expensive_builds"]) == 50 and maximum["limit"] == 50
    assert len(one["expensive_builds"]) == 1


def test_the_default_window_is_the_current_month_in_the_platform_timezone(client, admin):
    body = client.get("/api/usage/dashboard", headers=_headers(admin.id)).json()

    assert body["window"]["month"] == usage.resolve_month_window().month


def test_the_dashboard_writes_nothing(client, db_session, project, admin):
    _build(db_session, project, cost_usd=2.0, ended_at=_at(2))
    db_session.commit()
    before = db_session.query(Build).count()

    client.get(f"/api/usage/dashboard?month={MONTH}", headers=_headers(admin.id))

    assert db_session.query(Build).count() == before
