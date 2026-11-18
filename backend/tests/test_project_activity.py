"""`Project.last_active_at` as a
real activity signal.

Unit tests for `services/project_activity.py` itself (the coarsening
logic and the "never raises" guarantee — task (b): "a write that fails
must not break the thing it is observing"), plus the one boundary this
task deliberately did NOT wire up: viewing a build's *preview* does not
count as the deployed service being in use.

Every *counted* lifecycle event (build claimed/succeeded/failed, change
request submitted, deploy confirmed, rollback requested, stop, and
proxied traffic to a deployed service) is tested at its own real call
site in the module that owns it (test_worker.py, test_change_requests.py,
test_deployments_api.py) — "a test per counted event...
driving the real code path, not calling a helper directly."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.build import Build
from app.models.project import Project
from app.services import project_activity
from app.services.preview_manager import hash_token, issue_preview_token
from app.services.project_activity import (
    TRAFFIC_TOUCH_COARSEN_INTERVAL,
    _as_aware_utc,
    touch_last_active,
    touch_last_active_for_traffic,
)
from app.services.status import BuildStatus, ProjectStatus


def _make_project(db, seeded_ids, name="activity-project"):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# -- touch_last_active ------------------------------------------------------


def test_touch_last_active_sets_current_time(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids)
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()

    touch_last_active(project)

    assert project.last_active_at > stale


def test_touch_last_active_uses_the_supplied_now(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids)
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)

    touch_last_active(project, now=fixed)

    assert project.last_active_at == fixed


def test_touch_last_active_swallows_and_logs_when_the_write_raises(
    db_session, seeded_ids, monkeypatch, caplog
):
    """(b2): forces the underlying now-computation to raise and proves the
    call is a silent no-op — the value is left exactly as it was, and
    nothing propagates to the caller."""
    project = _make_project(db_session, seeded_ids)
    unchanged = project.last_active_at

    def _raise():
        raise RuntimeError("clock is broken")

    monkeypatch.setattr(project_activity, "_utcnow", _raise)

    with caplog.at_level("ERROR", logger="app.project_activity"):
        touch_last_active(project)  # must not raise

    assert project.last_active_at == unchanged
    assert "failed to record activity" in caplog.text


# -- touch_last_active_for_traffic (coarsening, task (a)/(b1)) --------------
#
# `_as_aware_utc` (imported above) wraps every assertion against a value
# that has just round-tripped through a `db_session.commit()`: SQLite
# (the dev/test DB) does not preserve `DateTime(timezone=True)` across a
# reload, so the ORM hands back a tzinfo-naive datetime that is still UTC
# by construction — the exact quirk `touch_last_active_for_traffic` itself
# has to tolerate, since a project loaded fresh in a real request
# (`db.get(Project, ...)`) is reloaded the same way.


def test_traffic_touch_writes_when_stale(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids)
    stale = datetime.now(timezone.utc) - TRAFFIC_TOUCH_COARSEN_INTERVAL - timedelta(minutes=1)
    project.last_active_at = stale
    db_session.commit()

    wrote = touch_last_active_for_traffic(project)

    assert wrote is True
    assert _as_aware_utc(project.last_active_at) > stale


def test_traffic_touch_skips_within_the_interval(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids)
    recent = datetime.now(timezone.utc) - timedelta(minutes=1)
    project.last_active_at = recent
    db_session.commit()

    wrote = touch_last_active_for_traffic(project)

    assert wrote is False
    assert _as_aware_utc(project.last_active_at) == recent


def test_traffic_touch_writes_again_once_the_interval_elapses(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    project.last_active_at = t0
    db_session.commit()

    just_inside = t0 + TRAFFIC_TOUCH_COARSEN_INTERVAL - timedelta(seconds=1)
    assert touch_last_active_for_traffic(project, now=just_inside) is False
    assert _as_aware_utc(project.last_active_at) == t0

    just_outside = t0 + TRAFFIC_TOUCH_COARSEN_INTERVAL + timedelta(seconds=1)
    assert touch_last_active_for_traffic(project, now=just_outside) is True
    assert _as_aware_utc(project.last_active_at) == just_outside


def test_traffic_touch_swallows_and_logs_when_the_write_raises(
    db_session, seeded_ids, monkeypatch, caplog
):
    project = _make_project(db_session, seeded_ids)
    unchanged = project.last_active_at

    def _raise():
        raise RuntimeError("clock is broken")

    monkeypatch.setattr(project_activity, "_utcnow", _raise)

    with caplog.at_level("ERROR", logger="app.project_activity"):
        wrote = touch_last_active_for_traffic(project)  # must not raise

    assert wrote is False
    assert project.last_active_at == unchanged
    assert "failed to record traffic activity" in caplog.text


# -- excluded boundary (a3): a preview view is not "the service in use" -----


class _FakePreviewManager:
    """Stands in for `PreviewManager` so this test needs no Docker — the
    boundary being pinned is "does routers/preview_proxy.py's preview route
    call the activity writer at all", not preview-container mechanics
    (already covered, with real Docker, by
    test_preview_manager_integration.py)."""

    def proxy(self, build_id, *, method="GET", path="/", **_forwarded):
        return 200, b"preview says hi", None


def test_viewing_a_build_preview_does_not_move_last_active_at(
    client, db_session, seeded_ids, monkeypatch
):
    """previews are pre-approval, TTL-bound L3 artifacts (ADR
    0016) — reviewing one is part of the build/approval workflow already
    reflected by the build lifecycle events, not evidence that the
    *deployed* service (the population being counted) is
    in use. Counting it would also blur the never-deployed vs.
    deployed-then-idle distinction, since a preview exists for builds that
    may never be deployed at all."""
    import app.routers.preview_proxy as preview_proxy_router

    monkeypatch.setattr(preview_proxy_router, "_preview_manager", _FakePreviewManager)

    project = _make_project(db_session, seeded_ids)
    build = Build(
        id="pv-activity-1",
        project_id=project.id,
        type="create",
        status=BuildStatus.SUCCEEDED,
    )
    db_session.add(build)
    db_session.commit()

    token, _expires_at = issue_preview_token(build.id)
    build.preview_token_hash = hash_token(token)
    db_session.commit()
    unchanged = project.last_active_at

    for _ in range(3):
        resp = client.get(f"/api/projects/{project.id}/preview/{build.id}/page?token={token}")
        assert resp.status_code == 200
        assert resp.content == b"preview says hi"

    db_session.refresh(project)
    assert project.last_active_at == unchanged
