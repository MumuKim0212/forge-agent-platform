"""the idle sweep's live-service exemption is a runtime fact read from the
persisted traffic-signal state, and the sweep holds the worker lease before it stops anything.

The sweep fixtures are shared with `tests/test_worker.py` (the tests that pin the
port-unset behaviour and must keep passing unchanged)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.worker as worker
from app.config import settings
from app.models.deployment import DeploymentStatus
from app.models.traffic_signal import TrafficSignalState
from app.services import project_lifecycle
from app.services.build_executor import PlaceholderBuildExecutor
from app.services.status import ProjectStatus
from app.services.worker_lease import LeaseLost
from app.worker import run_iteration, run_traffic_poll_if_due, sweep_idle_projects
from tests.test_deploy_service import FakeDeployManager
from tests.test_worker import (
    _FAR_PAST_EPOCH_NOW,
    _active_deployment,
    _idle_project,
    _pin_sweep,
    _sweep_audit_actions,
)

NOW = _FAR_PAST_EPOCH_NOW


def _signal_state(db, *, last_poll_age, observed_for):
    db.add(
        TrafficSignalState(
            id="platform",
            traefik_started_at=NOW - observed_for,
            last_poll_at=NOW - last_poll_age,
            observed_since=NOW - observed_for,
        )
    )
    db.commit()


def _live_idle_service(db, seeded_ids, name):
    stale = NOW - timedelta(days=200)
    project = _idle_project(db, seeded_ids, name=name, status=ProjectStatus.ACTIVE, last_active_at=stale)
    return project, _active_deployment(db, project)


def _pin_signal(monkeypatch, *, port=8082, traefik_enabled=True):
    _pin_sweep(monkeypatch, traefik_enabled=traefik_enabled)
    monkeypatch.setattr(settings, "traefik_metrics_port", port)


def test_trusted_signal_a_live_idle_service_is_flagged_stopped_and_archived(db_session, seeded_ids, monkeypatch):
    """Both conditions hold: the poll is 30 s old and the signal has watched 100 days (threshold 90)."""
    _pin_signal(monkeypatch)
    _signal_state(db_session, last_poll_age=timedelta(seconds=30), observed_for=timedelta(days=100))
    project, deployment = _live_idle_service(db_session, seeded_ids, "live-observed")
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=NOW, deploy_manager=manager)

    assert result == 1
    db_session.refresh(project)
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.STOPPED
    assert manager.removed == [deployment.id]
    assert project.status == ProjectStatus.ARCHIVED
    assert _sweep_audit_actions(db_session, project) == ["project.archived", "project.idle_flagged"]


def test_the_three_minute_freshness_boundary_is_inclusive(db_session, seeded_ids, monkeypatch):
    _pin_signal(monkeypatch)
    _signal_state(db_session, last_poll_age=timedelta(minutes=3), observed_for=timedelta(days=100))
    project, _ = _live_idle_service(db_session, seeded_ids, "live-boundary")

    candidates = project_lifecycle.idle_candidates(db_session, cutoff=NOW - timedelta(days=90), now=NOW)

    assert [p.id for p in candidates] == [project.id]


def test_a_stale_poll_exempts_a_live_idle_service(db_session, seeded_ids, monkeypatch):
    """The last successful poll is 4 minutes old (> 3), however long the signal has watched."""
    _pin_signal(monkeypatch)
    _signal_state(db_session, last_poll_age=timedelta(minutes=4), observed_for=timedelta(days=100))
    project, deployment = _live_idle_service(db_session, seeded_ids, "live-stale-signal")
    manager = FakeDeployManager()

    assert sweep_idle_projects(db_session, now=NOW, deploy_manager=manager) == 0

    db_session.refresh(project)
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert manager.removed == []
    assert project.status == ProjectStatus.ACTIVE
    assert _sweep_audit_actions(db_session, project) == []


def test_a_signal_that_is_too_young_exempts_a_live_idle_service(db_session, seeded_ids, monkeypatch):
    """Fresh poll, but the signal has watched 10 days of a 90-day window."""
    _pin_signal(monkeypatch)
    _signal_state(db_session, last_poll_age=timedelta(seconds=30), observed_for=timedelta(days=10))
    project, deployment = _live_idle_service(db_session, seeded_ids, "live-young-signal")
    manager = FakeDeployManager()

    assert sweep_idle_projects(db_session, now=NOW, deploy_manager=manager) == 0

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert manager.removed == []
    assert _sweep_audit_actions(db_session, project) == []


def test_port_unset_exempts_a_live_idle_service_whatever_state_exists(db_session, seeded_ids, monkeypatch):
    _pin_signal(monkeypatch, port=None)
    _signal_state(db_session, last_poll_age=timedelta(seconds=30), observed_for=timedelta(days=100))
    _, deployment = _live_idle_service(db_session, seeded_ids, "live-no-port")
    manager = FakeDeployManager()

    assert sweep_idle_projects(db_session, now=NOW, deploy_manager=manager) == 0

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert manager.removed == []


def test_a_configured_signal_that_never_polled_exempts_a_live_idle_service(db_session, seeded_ids, monkeypatch):
    _pin_signal(monkeypatch)  # port set, no TrafficSignalState row
    _, deployment = _live_idle_service(db_session, seeded_ids, "live-never-polled")

    assert sweep_idle_projects(db_session, now=NOW, deploy_manager=FakeDeployManager()) == 0

    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE


def test_traefik_off_the_port_and_a_stale_state_change_nothing(db_session, seeded_ids, monkeypatch):
    """Traefik off: the service proxy observes, so the signal state is not consulted at all."""
    _pin_signal(monkeypatch, traefik_enabled=False)
    _signal_state(db_session, last_poll_age=timedelta(days=5), observed_for=timedelta(days=1))
    project, _ = _live_idle_service(db_session, seeded_ids, "live-traefik-off")

    assert sweep_idle_projects(db_session, now=NOW, deploy_manager=FakeDeployManager()) == 1

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


def test_the_sweep_checks_the_lease_before_it_stops_anything(db_session, seeded_ids, monkeypatch):
    """a worker that has lost its lease stops nothing. The fake raises `LeaseLost`
    as `WorkerLease.require` does; the deployment is still ACTIVE and nothing was removed."""
    _pin_signal(monkeypatch)
    _signal_state(db_session, last_poll_age=timedelta(seconds=30), observed_for=timedelta(days=100))
    project, deployment = _live_idle_service(db_session, seeded_ids, "live-lease-lost")
    manager = FakeDeployManager()

    class _LostLease:
        def require(self):
            raise LeaseLost("taken over")

    with pytest.raises(LeaseLost):
        sweep_idle_projects(db_session, now=NOW, deploy_manager=manager, lease=_LostLease())

    db_session.refresh(deployment)
    db_session.refresh(project)
    assert manager.removed == []
    assert deployment.status == DeploymentStatus.ACTIVE
    assert project.status == ProjectStatus.ACTIVE
    assert _sweep_audit_actions(db_session, project) == []


def test_the_sweep_with_a_held_lease_still_runs(db_session, seeded_ids, monkeypatch):
    _pin_signal(monkeypatch, traefik_enabled=False)
    _live_idle_service(db_session, seeded_ids, "live-lease-held")
    calls = []

    class _HeldLease:
        def require(self):
            calls.append("require")

    result = sweep_idle_projects(db_session, now=NOW, deploy_manager=FakeDeployManager(), lease=_HeldLease())

    assert result == 1
    assert calls == ["require"]


def test_the_traffic_poll_is_due_every_sixty_seconds_under_the_lease_and_only_when_configured(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_metrics_port", 8082)
    order = []

    class _Poller:
        def poll(self, db, *, now=None):
            order.append("poll")
            return True

    class _Lease:
        def require(self):
            order.append("lease")

    state: dict = {}
    assert run_traffic_poll_if_due(db_session, _Poller(), state, now=NOW, lease=_Lease()) is True
    assert order == ["lease", "poll"]  # the lease first
    assert run_traffic_poll_if_due(db_session, _Poller(), state, now=NOW + timedelta(seconds=59), lease=_Lease()) is False
    assert run_traffic_poll_if_due(db_session, _Poller(), state, now=NOW + timedelta(seconds=60), lease=_Lease()) is True
    assert order == ["lease", "poll", "lease", "poll"]

    monkeypatch.setattr(settings, "traefik_metrics_port", None)
    assert run_traffic_poll_if_due(db_session, _Poller(), {}, now=NOW, lease=_Lease()) is False
    monkeypatch.setattr(settings, "traefik_metrics_port", 8082)
    monkeypatch.setattr(settings, "traefik_enabled", False)
    assert run_traffic_poll_if_due(db_session, _Poller(), {}, now=NOW, lease=_Lease()) is False
    assert len(order) == 4


def test_a_lost_lease_stops_the_poll_before_it_reads(db_session, monkeypatch):
    monkeypatch.setattr(settings, "traefik_enabled", True)
    monkeypatch.setattr(settings, "traefik_metrics_port", 8082)
    polled = []

    class _Poller:
        def poll(self, db, *, now=None):
            polled.append(1)
            return True

    class _LostLease:
        def require(self):
            raise LeaseLost("taken over")

    with pytest.raises(LeaseLost):
        run_traffic_poll_if_due(db_session, _Poller(), {}, now=NOW, lease=_LostLease())
    assert polled == []


def test_run_iteration_polls_before_the_idle_sweep(db_session, monkeypatch):
    order = []
    monkeypatch.setattr(worker, "run_traffic_poll_if_due", lambda *a, **k: order.append("poll"))
    monkeypatch.setattr(worker, "sweep_idle_projects", lambda *a, **k: order.append("sweep"))

    run_iteration(db_session, PlaceholderBuildExecutor())

    assert order == ["poll", "sweep"]


def test_run_iteration_survives_a_raising_poll(db_session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(worker, "run_traffic_poll_if_due", boom)
    swept = []
    monkeypatch.setattr(worker, "sweep_idle_projects", lambda *a, **k: swept.append(1))

    run_iteration(db_session, PlaceholderBuildExecutor())

    assert swept == [1]
