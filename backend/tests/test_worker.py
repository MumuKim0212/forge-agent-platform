"""worker + DB-backed build queue.

Covers: atomic claim ordering, the global concurrency cap, per-project
serialization, the timeout sweep (including "after a worker crash/restart"
— simulated by a stale RUNNING row with no further worker action), and the
placeholder executor's success/failure transitions via app.worker.
"""

import contextlib
import os
from datetime import datetime, timedelta, timezone

import pytest

import app.worker as worker
from app.config import settings
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.services import preview_data, project_lifecycle, service_data_backup
from app.services.build_executor import AlwaysFailBuildExecutor, PlaceholderBuildExecutor
from app.services.build_queue import claim_next_build, sweep_timed_out_builds
from app.services.project_activity import _as_aware_utc
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import run_iteration, run_one_build, sweep_idle_projects, sweep_orphaned_preview_stores
from tests.fake_admin_dsn import FAKE_ADMIN_DSN
from tests.test_deploy_service import FakeDeployManager


def _make_build(db, project_id, *, status=BuildStatus.QUEUED, created_at=None, started_at=None):
    build = Build(project_id=project_id, type="create", status=status)
    db.add(build)
    db.flush()
    if created_at is not None:
        build.created_at = created_at
    if started_at is not None:
        build.started_at = started_at
    db.commit()
    db.refresh(build)
    return build


def _make_project(db, seeded_ids, name):
    from app.models.project import Project
    from app.services.status import ProjectStatus

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


def test_claim_next_build_picks_oldest_queued(db_session, seeded_ids):
    p1 = _make_project(db_session, seeded_ids, "p1")
    p2 = _make_project(db_session, seeded_ids, "p2")
    now = datetime.now(timezone.utc)
    older = _make_build(db_session, p1.id, created_at=now - timedelta(minutes=5))
    newer = _make_build(db_session, p2.id, created_at=now)

    claimed = claim_next_build(db_session, concurrency_cap=5)

    assert claimed is not None
    assert claimed.id == older.id
    assert claimed.status == BuildStatus.RUNNING
    assert claimed.started_at is not None
    assert claimed.heartbeat_at is not None

    # the newer build is untouched
    db_session.refresh(newer)
    assert newer.status == BuildStatus.QUEUED


def test_claim_next_build_respects_global_concurrency_cap(db_session, seeded_ids):
    p1 = _make_project(db_session, seeded_ids, "p1")
    p2 = _make_project(db_session, seeded_ids, "p2")
    _make_build(db_session, p1.id, status=BuildStatus.RUNNING)
    queued = _make_build(db_session, p2.id)

    claimed = claim_next_build(db_session, concurrency_cap=1)

    assert claimed is None
    db_session.refresh(queued)
    assert queued.status == BuildStatus.QUEUED


def test_claim_next_build_serializes_per_project(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    _make_build(db_session, project.id, status=BuildStatus.RUNNING)
    second_queued = _make_build(db_session, project.id, status=BuildStatus.QUEUED)

    # Global cap is generous; only per-project serialization should block this.
    claimed = claim_next_build(db_session, concurrency_cap=10)

    assert claimed is None
    db_session.refresh(second_queued)
    assert second_queued.status == BuildStatus.QUEUED


def test_claim_next_build_allows_other_projects_when_one_is_busy(db_session, seeded_ids):
    busy_project = _make_project(db_session, seeded_ids, "busy")
    free_project = _make_project(db_session, seeded_ids, "free")
    _make_build(db_session, busy_project.id, status=BuildStatus.RUNNING)
    free_build = _make_build(db_session, free_project.id, status=BuildStatus.QUEUED)

    claimed = claim_next_build(db_session, concurrency_cap=10)

    assert claimed is not None
    assert claimed.id == free_build.id


def test_sweep_fails_stale_running_build_with_timeout(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    stale_started = datetime.now(timezone.utc) - timedelta(minutes=999)
    stale = _make_build(
        db_session, project.id, status=BuildStatus.RUNNING, started_at=stale_started
    )

    timed_out = sweep_timed_out_builds(db_session, max_duration_minutes=20)

    assert [b.id for b in timed_out] == [stale.id]
    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED
    assert stale.failure_reason == BuildFailureReason.TIMEOUT
    assert stale.ended_at is not None


def test_sweep_survives_worker_crash_simulation(db_session, seeded_ids):
    """A build left RUNNING (as if the worker that claimed it crashed) with
    no heartbeat/worker activity since must still be timed out by a later
    sweep — nothing about the sweep depends on the same worker instance
    that claimed it."""
    project = _make_project(db_session, seeded_ids, "p1")
    stale_started = datetime.now(timezone.utc) - timedelta(minutes=999)
    stale = _make_build(
        db_session, project.id, status=BuildStatus.RUNNING, started_at=stale_started
    )
    stale.heartbeat_at = None  # crashed worker never got to heartbeat
    db_session.commit()

    # Fresh call, simulating a new worker process's first sweep.
    timed_out = sweep_timed_out_builds(db_session, max_duration_minutes=20)

    assert [b.id for b in timed_out] == [stale.id]


def test_sweep_leaves_fresh_running_build_alone(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    fresh = _make_build(
        db_session,
        project.id,
        status=BuildStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )

    timed_out = sweep_timed_out_builds(db_session, max_duration_minutes=20)

    assert timed_out == []
    db_session.refresh(fresh)
    assert fresh.status == BuildStatus.RUNNING


def test_run_one_build_success_transitions_to_succeeded(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, PlaceholderBuildExecutor())

    assert result.status == BuildStatus.SUCCEEDED
    assert result.ended_at is not None
    assert result.failure_reason is None


def test_run_one_build_failure_transitions_to_failed_with_reason(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, AlwaysFailBuildExecutor())

    assert result.status == BuildStatus.FAILED
    assert result.failure_reason == BuildFailureReason.AGENT_ERROR
    assert result.ended_at is not None


def test_run_one_build_executor_exception_is_infra_error(db_session, seeded_ids):
    class RaisingExecutor:
        def execute(self, build):
            raise RuntimeError("boom")

    project = _make_project(db_session, seeded_ids, "p1")
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, RaisingExecutor())

    assert result.status == BuildStatus.FAILED
    assert result.failure_reason == BuildFailureReason.INFRA_ERROR


def test_run_iteration_claims_and_runs_build_end_to_end(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    queued = _make_build(db_session, project.id, status=BuildStatus.QUEUED)

    ran = run_iteration(db_session, PlaceholderBuildExecutor())

    assert ran is not None
    assert ran.id == queued.id
    db_session.refresh(queued)
    assert queued.status == BuildStatus.SUCCEEDED


def test_run_iteration_sweeps_timeouts_before_claiming(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    stale_started = datetime.now(timezone.utc) - timedelta(minutes=999)
    stale = _make_build(
        db_session, project.id, status=BuildStatus.RUNNING, started_at=stale_started
    )
    # Another project's build is queued and should now be claimable once the
    # stale build's project frees up isn't even required here, but we assert
    # the sweep ran as part of the same iteration.
    other_project = _make_project(db_session, seeded_ids, "p2")
    queued = _make_build(db_session, other_project.id, status=BuildStatus.QUEUED)

    run_iteration(db_session, PlaceholderBuildExecutor())

    db_session.refresh(stale)
    assert stale.status == BuildStatus.FAILED
    assert stale.failure_reason == BuildFailureReason.TIMEOUT
    db_session.refresh(queued)
    assert queued.status == BuildStatus.SUCCEEDED


# --- last_active_at as a real
# activity signal. "a build starting or finishing" (task (a)) drives each
# through its real production entry point (claim_next_build/run_one_build),
# not by calling services/project_activity.touch_last_active directly.


def test_claim_next_build_updates_project_last_active_at(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    _make_build(db_session, project.id, status=BuildStatus.QUEUED)

    claimed = claim_next_build(db_session, concurrency_cap=5)

    assert claimed is not None
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_run_one_build_success_updates_project_last_active_at(db_session, seeded_ids):
    """Closes the run-flow claim: "project
    last_active_at updated" on a SUCCEEDED finish."""
    project = _make_project(db_session, seeded_ids, "p1")
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, PlaceholderBuildExecutor())

    assert result.status == BuildStatus.SUCCEEDED
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_run_one_build_failure_updates_project_last_active_at(db_session, seeded_ids):
    project = _make_project(db_session, seeded_ids, "p1")
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, AlwaysFailBuildExecutor())

    assert result.status == BuildStatus.FAILED
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) > stale


def test_run_one_build_success_still_succeeds_when_activity_tracking_raises(
    db_session, seeded_ids, monkeypatch
):
    """(b2): the build/queue transition must not fail just because the
    activity bookkeeping did. Forces the shared writer's own now-lookup to
    raise (the same seam test_project_activity.py exercises directly) and
    drives it through the real worker entry point."""
    import app.services.project_activity as project_activity

    def _raise():
        raise RuntimeError("clock is broken")

    monkeypatch.setattr(project_activity, "_utcnow", _raise)

    project = _make_project(db_session, seeded_ids, "p1")
    stale = datetime.now(timezone.utc) - timedelta(days=90)
    project.last_active_at = stale
    db_session.commit()
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)

    result = run_one_build(db_session, build, PlaceholderBuildExecutor())

    assert result.status == BuildStatus.SUCCEEDED  # the build outcome is unaffected
    db_session.refresh(project)
    assert _as_aware_utc(project.last_active_at) == stale  # the activity write itself was a no-op


# --- ADR 0023 §Backup: the worker-level wiring --------------------


class _ExplodingDockerClient:
    """Any attribute access raises -- proves Docker was never touched.

    `touched` is a real instance attribute, set only from inside
    `__getattr__` right before it raises -- so a caller whose surrounding
    code happens to swallow that exception (e.g. an orchestration
    function's own broad `except Exception:`) can still assert `not
    client.touched` directly, rather than relying on the exception itself
    surviving to the test. `__init__` seeds it so reading `.touched` never
    itself falls through to `__getattr__` and raises."""

    def __init__(self):
        self.touched = False

    def __getattr__(self, name):
        self.touched = True
        raise AssertionError(f"Docker client attribute {name!r} was accessed unexpectedly")


def test_sweep_service_data_backups_noop_when_disabled(db_session, monkeypatch, tmp_path):
    """(a3): enabled=False disables everything -- no directory, no dump,
    no sweep."""
    backup_dir = str(tmp_path / "backups")
    monkeypatch.setattr(settings, "service_data_backup_enabled", False)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(settings, "service_data_backup_dir", backup_dir)

    result = worker.sweep_service_data_backups(db_session, docker_client=_ExplodingDockerClient())

    assert result == 0
    assert not os.path.exists(backup_dir)


def test_sweep_service_data_backups_noop_when_postgres_unconfigured(db_session, monkeypatch, tmp_path):
    """(d3): nothing runs when service_postgres_configured is false -- a
    platform with no admin DSN must not log an error every poll."""
    backup_dir = str(tmp_path / "backups")
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    monkeypatch.setattr(settings, "service_data_backup_dir", backup_dir)

    result = worker.sweep_service_data_backups(db_session, docker_client=_ExplodingDockerClient())

    assert result == 0
    assert not os.path.exists(backup_dir)


def test_run_iteration_sweeps_service_data_backups(db_session, monkeypatch, tmp_path):
    """(d1): a sweep function beside sweep_expired_previews, called from
    run_iteration."""
    backup_dir = str(tmp_path / "backups")
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(settings, "service_data_backup_dir", backup_dir)

    calls = []
    monkeypatch.setattr(service_data_backup, "list_service_databases", lambda: ["svc_fake"])
    monkeypatch.setattr(
        service_data_backup, "ensure_backup_dir", lambda d: calls.append(("ensure_dir", d))
    )
    monkeypatch.setattr(service_data_backup, "is_due", lambda *a, **k: True)

    def _fake_dump(database_name, *, docker_client, dest_path):
        calls.append(("dump", database_name, dest_path))

    monkeypatch.setattr(service_data_backup, "dump_database", _fake_dump)
    monkeypatch.setattr(
        service_data_backup, "sweep_backup_retention", lambda *a, **k: calls.append(("retention",))
    )

    run_iteration(db_session, PlaceholderBuildExecutor(), backup_docker_client=object())

    dump_calls = [c for c in calls if c[0] == "dump"]
    assert len(dump_calls) == 1
    assert dump_calls[0][1] == "svc_fake"
    assert any(c[0] == "retention" for c in calls)

    entry = (
        db_session.query(AuditLog)
        .filter_by(action="service_data_backup.created")
        .order_by(AuditLog.ts.desc())
        .first()
    )
    assert entry is not None
    assert entry.target == "database:svc_fake"


def test_run_iteration_does_not_crash_when_backup_dump_raises(db_session, monkeypatch, tmp_path):
    """(d2): sweep_timed_out_builds/sweep_stale_deployments are unwrapped
    in run_iteration, so a raising sweep would kill the worker process --
    the backup sweep must never be allowed to."""
    backup_dir = str(tmp_path / "backups")
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(settings, "service_data_backup_dir", backup_dir)

    monkeypatch.setattr(service_data_backup, "list_service_databases", lambda: ["svc_fake"])
    monkeypatch.setattr(service_data_backup, "ensure_backup_dir", lambda d: None)
    monkeypatch.setattr(service_data_backup, "is_due", lambda *a, **k: True)

    def _boom(*a, **k):
        raise RuntimeError("simulated pg_dump exec failure")

    monkeypatch.setattr(service_data_backup, "dump_database", _boom)
    monkeypatch.setattr(service_data_backup, "sweep_backup_retention", lambda *a, **k: 0)

    # Must not raise.
    run_iteration(db_session, PlaceholderBuildExecutor(), backup_docker_client=object())

    entry = (
        db_session.query(AuditLog)
        .filter_by(action="service_data_backup.failed")
        .order_by(AuditLog.ts.desc())
        .first()
    )
    assert entry is not None
    assert entry.target == "database:svc_fake"


def test_worker_startup_banner_reports_backup_configuration(monkeypatch, caplog):
    """(d4): the startup banner reports whether backup is on, its
    interval, and its directory."""
    monkeypatch.setattr(settings, "service_data_backup_enabled", True)
    monkeypatch.setattr(settings, "service_data_backup_interval_hours", 24)
    monkeypatch.setattr(settings, "service_data_backup_dir", "backups/service-data")

    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_backup_startup_banner()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "enabled=True" in messages
    assert "interval_hours=24" in messages
    assert "dir=backups/service-data" in messages


# -- the startup banner says whether the sandbox disk cap holds on this host ---------


def test_the_disk_cap_banner_says_enforced_when_the_probe_finds_no_gap(caplog, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", 2048)
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 1.0)

    class Executor:
        def disk_cap_gap(self):
            return None

    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_disk_cap_startup_banner(Executor())

    (record,) = caplog.records
    assert record.levelname == "INFO"
    assert "sandbox disk cap: enforced" in record.getMessage()
    assert "limit 2048 MiB" in record.getMessage()


def test_the_disk_cap_banner_warns_with_the_gap_when_the_cap_cannot_be_enforced(caplog):
    class Executor:
        def disk_cap_gap(self):
            return "the container engine does not report a container's writable-layer size"

    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_disk_cap_startup_banner(Executor())

    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "does not report a container's writable-layer size" in record.getMessage()


def test_a_probe_that_raises_is_a_warning_not_a_failed_startup(caplog):
    class Executor:
        def disk_cap_gap(self):
            raise RuntimeError("engine blew up")

    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_disk_cap_startup_banner(Executor())  # must not raise

    assert any(r.levelname == "WARNING" and "could not be verified" in r.getMessage() for r in caplog.records)


def test_an_executor_with_no_sandbox_has_no_disk_cap_to_report(caplog):
    from app.services.build_executor import PlaceholderBuildExecutor

    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_disk_cap_startup_banner(PlaceholderBuildExecutor())

    assert caplog.records == []


def test_the_sandbox_executor_probes_the_real_runner(monkeypatch):
    """`disk_cap_gap` is the runner's probe, not a statement of its own: whatever the runner says,
    the executor hands on."""
    from types import SimpleNamespace

    from app.services import sandbox_build_executor as sbe

    monkeypatch.setattr(sbe.SandboxRunner, "probe_disk_cap", lambda self: "a gap")
    executor = sbe.SandboxBuildExecutor(docker_client=SimpleNamespace(), adapter=SimpleNamespace())

    assert executor.disk_cap_gap() == "a gap"


# -- the same statement names previews, from a probe of their own ----------------------


class _Probed:
    """An executor that has both probes, answering what it is told."""

    def __init__(self, build_gap, preview_gap):
        self._build_gap = build_gap
        self._preview_gap = preview_gap

    def disk_cap_gap(self):
        return self._build_gap

    def preview_disk_cap_gap(self):
        return self._preview_gap


def _banner(executor, caplog):
    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_disk_cap_startup_banner(executor)
    return [(r.levelname, r.getMessage()) for r in caplog.records]


def test_the_startup_statement_says_previews_are_enforced_and_how_when_both_probes_find_no_gap(caplog, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_disk_limit_mb", 2048)
    monkeypatch.setattr(settings, "sandbox_disk_poll_interval_seconds", 1.0)

    (build_line, preview_line) = _banner(_Probed(None, None), caplog)

    assert build_line[0] == "INFO" and build_line[1].startswith("sandbox disk cap: enforced")
    level, message = preview_line
    assert level == "INFO"
    assert message.startswith("preview disk cap: enforced")
    assert "every live preview" in message and "keeps watching while a build runs" in message
    assert "2048 MiB" in message and "1.0s" in message
    # honest about what it is NOT, and about the (measured) host dependence
    assert "no harvest bound stands behind it" in message
    assert "overlay2 counts a sparse file at its apparent size" in message and "Docker Desktop" in message


def test_the_startup_statement_warns_for_previews_alone_when_only_the_preview_probe_finds_a_gap(caplog):
    gap = "the container engine reported 0 bytes of writable-layer use after a 8 MiB write, so a preview's disk cap is NOT reliably enforced"

    (build_line, preview_line) = _banner(_Probed(None, gap), caplog)

    assert build_line[0] == "INFO" and "sandbox disk cap: enforced" in build_line[1]
    assert preview_line == ("WARNING", f"preview disk cap: {gap}")


def test_the_startup_statement_warns_for_the_build_sandbox_alone_when_only_its_probe_finds_a_gap(caplog):
    (build_line, preview_line) = _banner(_Probed("a build gap", None), caplog)

    assert build_line == ("WARNING", "sandbox disk cap: a build gap")
    assert preview_line[0] == "INFO" and preview_line[1].startswith("preview disk cap: enforced")


def test_the_startup_statement_warns_twice_when_both_probes_find_a_gap(caplog):
    lines = _banner(_Probed("a build gap", "a preview gap"), caplog)

    assert lines == [
        ("WARNING", "sandbox disk cap: a build gap"),
        ("WARNING", "preview disk cap: a preview gap"),
    ]


def test_a_preview_probe_that_raises_is_a_warning_and_does_not_hide_the_build_statement(caplog):
    class Executor(_Probed):
        def preview_disk_cap_gap(self):
            raise RuntimeError("engine blew up")

    lines = _banner(Executor(None, None), caplog)

    assert lines[0][0] == "INFO" and "sandbox disk cap: enforced" in lines[0][1]
    assert ("WARNING", "preview disk cap: the disk cap could not be verified (the check raised)") in lines


def test_the_sandbox_executor_probes_previews_on_their_own_and_starts_previews(monkeypatch):
    from types import SimpleNamespace

    from app.services import sandbox_build_executor as sbe
    from app.services.build_executor import PlaceholderBuildExecutor

    def probe(self, **kwargs):
        return "a preview gap" if kwargs.get("preview") else "a build gap"

    monkeypatch.setattr(sbe.SandboxRunner, "probe_disk_cap", probe)
    executor = sbe.SandboxBuildExecutor(docker_client=SimpleNamespace(), adapter=SimpleNamespace())

    assert executor.preview_disk_cap_gap() == "a preview gap"
    assert executor.disk_cap_gap() == "a build gap"  # the build's own probe is not the preview's
    assert executor.starts_previews is True  # how the worker knows to run the watcher
    assert getattr(PlaceholderBuildExecutor(), "starts_previews", False) is False


# -- the startup banner says whether the proxy image can drain a build's token ------------


class _ProxyImageExecutor:
    """An executor that has the proxy-image probe, answering what it is told."""

    proxy_image = "forge-llm-proxy:latest"
    proxy_required_capabilities = ("drain",)

    def __init__(self, gap=None, *, raises: Exception | None = None):
        self._gap = gap
        self._raises = raises

    def proxy_image_gap(self):
        if self._raises is not None:
            raise self._raises
        return self._gap


def _proxy_banner(executor, caplog):
    with caplog.at_level("INFO", logger="app.worker"):
        worker._log_proxy_image_startup_banner(executor)
    return [(r.levelname, r.getMessage()) for r in caplog.records]


def test_the_proxy_image_banner_says_the_image_can_drain_when_the_probe_finds_no_gap(caplog):
    (line,) = _proxy_banner(_ProxyImageExecutor(None), caplog)

    assert line[0] == "INFO"
    assert line[1].startswith("LLM proxy image: forge-llm-proxy:latest advertises drain")


def test_the_proxy_image_banner_warns_with_the_gap_and_the_rebuild_command(caplog):
    gap = (
        "the LLM proxy image 'forge-llm-proxy:latest' does not advertise drain; rebuild it with: "
        "docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile . (run from backend/)"
    )

    assert _proxy_banner(_ProxyImageExecutor(gap), caplog) == [("WARNING", f"LLM proxy image: {gap}")]


def test_a_proxy_image_probe_that_raises_is_a_warning_not_a_failed_startup(caplog):
    lines = _proxy_banner(_ProxyImageExecutor(raises=RuntimeError("engine blew up")), caplog)  # must not raise

    assert ("WARNING", "LLM proxy image: the LLM proxy image's capabilities could not be verified (the check raised)") in lines


def test_an_executor_with_no_proxy_has_no_image_to_report(caplog):
    from app.services.build_executor import PlaceholderBuildExecutor

    assert _proxy_banner(PlaceholderBuildExecutor(), caplog) == []


def test_the_sandbox_executors_banner_reports_a_stale_image_from_its_own_docker_client(caplog):
    """End to end through the real executor, with a fake engine whose image has no capability label (an
    image built before the capability label): the banner names the image, the missing capability and the rebuild command."""
    from types import SimpleNamespace

    from app.services import sandbox_build_executor as sbe

    docker = SimpleNamespace(images=SimpleNamespace(get=lambda name: SimpleNamespace(labels={})))
    executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=SimpleNamespace())

    ((level, message),) = _proxy_banner(executor, caplog)

    assert level == "WARNING"
    assert message.startswith("LLM proxy image: the LLM proxy image 'forge-llm-proxy:latest' does not advertise drain")
    assert "docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile ." in message


def test_main_reports_the_proxy_image_before_its_first_iteration_and_a_gap_does_not_stop_it(monkeypatch, caplog):
    """The real `main()`: the banner runs at start, ahead of the loop, and a gap is a warning, not a
    refusal to start (the builds are what are refused)."""
    seen: list[str] = []

    def iteration(db, executor, **kwargs):
        seen.append("iteration")
        raise _StopLoop

    _drive_main(monkeypatch, iteration)
    monkeypatch.setattr(worker, "_select_executor", lambda: _ProxyImageExecutor("a gap in the proxy image"))

    with caplog.at_level("INFO", logger="app.worker"):
        with pytest.raises(_StopLoop):
            worker.main()

    messages = [r.getMessage() for r in caplog.records]
    assert "LLM proxy image: a gap in the proxy image" in messages
    assert seen == ["iteration"]  # it went on to run


# -- sweep_idle_projects -----


def _idle_project(db, seeded_ids, *, name, status=ProjectStatus.SUBMITTED, last_active_at):
    project = Project(
        name=name,
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=status,
    )
    db.add(project)
    db.flush()
    project.last_active_at = last_active_at
    db.commit()
    db.refresh(project)
    return project


def _active_deployment(db, project, *, build_id="bld-idle-1"):
    build = _make_build(db, project.id)
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.ACTIVE,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


_FAR_PAST_EPOCH_NOW = project_lifecycle.ACTIVITY_SIGNAL_EPOCH + timedelta(days=365)


def test_sweep_idle_projects_noop_when_disabled(db_session, seeded_ids, monkeypatch):
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", False)
    project = _idle_project(
        db_session,
        seeded_ids,
        name="disabled-sweep",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW)

    assert result == 0
    db_session.refresh(project)
    assert project.status == ProjectStatus.SUBMITTED


def test_sweep_idle_projects_archives_a_never_deployed_candidate_without_docker(
    db_session, seeded_ids, monkeypatch
):
    """(a3/a4): no `Deployment` row at all -- archived without error, and
    without ever constructing a real `DeployManager` (a4's "without a
    Docker call", pinned mechanically: the constructor is made to explode
    if it is ever reached)."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)

    def _explode(*a, **k):
        raise AssertionError("DeployManager must never be constructed for a never-deployed project")

    monkeypatch.setattr("app.services.deploy_manager.DeployManager.__init__", _explode)

    project = _idle_project(
        db_session,
        seeded_ids,
        name="never-deployed-idle",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW)

    assert result == 1
    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED

    flag = db_session.query(AuditLog).filter_by(action="project.idle_flagged").one()
    assert flag.metadata_json["population"] == project_lifecycle.POPULATION_NEVER_DEPLOYED

    archived = db_session.query(AuditLog).filter_by(action="project.archived").one()
    assert archived.metadata_json["previous_status"] == ProjectStatus.SUBMITTED
    assert archived.metadata_json["population"] == project_lifecycle.POPULATION_NEVER_DEPLOYED


def test_sweep_idle_projects_stops_and_archives_a_deployed_candidate(
    db_session, seeded_ids, monkeypatch
):
    """(a3): the deployed-then-idle population -- stage 2 stops the ACTIVE
    deployment before stage 3 archives, and (T1) the stop must not move
    `last_active_at` back to "now". Traefik is pinned OFF: the service
    proxy records traffic in that posture, so an ACTIVE deployment is judged
    like any other candidate."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)
    monkeypatch.setattr(settings, "traefik_enabled", False)

    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)
    project = _idle_project(
        db_session, seeded_ids, name="deployed-idle", status=ProjectStatus.ACTIVE, last_active_at=stale
    )
    deployment = _active_deployment(db_session, project)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 1
    db_session.refresh(project)
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.STOPPED
    assert deployment.id in manager.removed
    assert project.status == ProjectStatus.ARCHIVED
    assert _as_aware_utc(project.last_active_at) == stale  # T1: unmoved by the sweep's own stop

    archived = db_session.query(AuditLog).filter_by(action="project.archived").one()
    assert archived.metadata_json["previous_status"] == ProjectStatus.ACTIVE
    assert archived.metadata_json["population"] == project_lifecycle.POPULATION_DEPLOYED_THEN_IDLE


def test_sweep_idle_projects_leaves_a_failed_stop_unarchived_and_continues(
    db_session, seeded_ids, monkeypatch
):
    """(a4): a project whose stop raises stays unarchived (its own flag
    entry survives, since it was committed before the failing stop), and
    the sweep still archives the OTHER candidate in the same pass."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)
    monkeypatch.setattr(settings, "traefik_enabled", False)  # both candidates must be reachable

    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)

    class _ExplodingManager:
        def remove_service(self, deployment_id):
            raise RuntimeError("simulated Docker failure")

    failing_project = _idle_project(
        db_session, seeded_ids, name="failing-stop", status=ProjectStatus.ACTIVE, last_active_at=stale
    )
    _active_deployment(db_session, failing_project)

    healthy_project = _idle_project(
        db_session, seeded_ids, name="healthy-stop", status=ProjectStatus.ACTIVE, last_active_at=stale
    )
    healthy_deployment = _active_deployment(db_session, healthy_project)

    class _RoutingManager:
        """Explodes only for the failing project's deployment; a real
        `FakeDeployManager` otherwise, so the good candidate's own stop
        still goes through normally in the SAME sweep pass."""

        def __init__(self):
            self._exploding = _ExplodingManager()
            self._fake = FakeDeployManager()

        def remove_service(self, deployment_id):
            if deployment_id == healthy_deployment.id:
                return self._fake.remove_service(deployment_id)
            return self._exploding.remove_service(deployment_id)

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=_RoutingManager())

    assert result == 1  # only the healthy one counted as archived

    db_session.refresh(failing_project)
    assert failing_project.status == ProjectStatus.ACTIVE  # left unarchived (a4)
    failing_flag = (
        db_session.query(AuditLog)
        .filter_by(action="project.idle_flagged", target=f"project:{failing_project.id}")
        .one_or_none()
    )
    assert failing_flag is not None  # detection record survives the later failure

    db_session.refresh(healthy_project)
    assert healthy_project.status == ProjectStatus.ARCHIVED


# -- no traffic signal under Traefik, no idle verdict for a live service ------
#. Every test
# below pins `traefik_enabled` itself: the ambient value follows whatever FORGE_ENV the
# process was started with, which must never decide what these assert.


def _deployment_with_status(db, project, status):
    build = _make_build(db, project.id)
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=status,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def _sweep_audit_actions(db, project):
    """The sweep's own audit entries for `project`, sorted (the two stages can share a timestamp)."""
    return sorted(
        row.action
        for row in db.query(AuditLog).filter(
            AuditLog.target == f"project:{project.id}",
            AuditLog.action.in_(["project.idle_flagged", "project.archived"]),
        )
    )


def _pin_sweep(monkeypatch, *, traefik_enabled):
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)
    monkeypatch.setattr(settings, "traefik_enabled", traefik_enabled)


def test_traefik_on_a_live_service_idle_past_the_threshold_is_not_flagged_stopped_or_archived(
    db_session, seeded_ids, monkeypatch
):
    """200 days with no recorded activity, Traefik routing (so nothing here
    could have seen a request), an ACTIVE deployment: no flag, no stop, no archive."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)
    project = _idle_project(
        db_session, seeded_ids, name="live-under-traefik", status=ProjectStatus.ACTIVE, last_active_at=stale
    )
    deployment = _active_deployment(db_session, project)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 0
    db_session.refresh(project)
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.ACTIVE
    assert manager.removed == []
    assert project.status == ProjectStatus.ACTIVE
    assert _as_aware_utc(project.last_active_at) == stale
    assert _sweep_audit_actions(db_session, project) == []


def test_traefik_on_the_exemption_is_per_project_not_a_switch_for_the_whole_sweep(
    db_session, seeded_ids, monkeypatch
):
    """One pass, three equally idle projects. Only the live one is left alone."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)
    live = _idle_project(db_session, seeded_ids, name="live", status=ProjectStatus.ACTIVE, last_active_at=stale)
    live_deployment = _active_deployment(db_session, live)
    stopped = _idle_project(db_session, seeded_ids, name="stopped", status=ProjectStatus.ACTIVE, last_active_at=stale)
    _deployment_with_status(db_session, stopped, DeploymentStatus.STOPPED)
    never = _idle_project(db_session, seeded_ids, name="never", last_active_at=stale)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 2
    for project in (live, stopped, never):
        db_session.refresh(project)
    db_session.refresh(live_deployment)
    assert live.status == ProjectStatus.ACTIVE
    assert live_deployment.status == DeploymentStatus.ACTIVE
    assert stopped.status == ProjectStatus.ARCHIVED
    assert never.status == ProjectStatus.ARCHIVED
    assert manager.removed == []  # nothing was stopped: the live one is exempt, the others had nothing running


def test_traefik_on_a_never_deployed_project_keeps_the_full_lifecycle(db_session, seeded_ids, monkeypatch):
    _pin_sweep(monkeypatch, traefik_enabled=True)
    project = _idle_project(
        db_session, seeded_ids, name="never-deployed-traefik", last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200)
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW)

    assert result == 1
    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED
    assert _sweep_audit_actions(db_session, project) == ["project.archived", "project.idle_flagged"]


@pytest.mark.parametrize(
    "terminal_status",
    [DeploymentStatus.STOPPED, DeploymentStatus.SUPERSEDED, DeploymentStatus.FAILED],
)
def test_traefik_on_a_project_with_only_finished_deployments_keeps_the_stop_and_archive_path(
    db_session, seeded_ids, monkeypatch, terminal_status
):
    """Deployed once, nothing serving now: the population the amendment leaves alone."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    project = _idle_project(
        db_session,
        seeded_ids,
        name=f"only-{terminal_status.lower()}",
        status=ProjectStatus.ACTIVE,
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )
    _deployment_with_status(db_session, project, terminal_status)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 1
    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED
    assert manager.removed == []  # nothing ACTIVE to stop
    assert _sweep_audit_actions(db_session, project) == ["project.archived", "project.idle_flagged"]
    flag = db_session.query(AuditLog).filter_by(action="project.idle_flagged").one()
    assert flag.metadata_json["population"] == project_lifecycle.POPULATION_DEPLOYED_THEN_IDLE


def test_traefik_on_a_live_service_with_finished_deployments_behind_it_is_still_exempt(
    db_session, seeded_ids, monkeypatch
):
    """A rolled-back or re-deployed service carries older SUPERSEDED/STOPPED rows beside its ACTIVE
    one; the ACTIVE row is what decides."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    project = _idle_project(
        db_session,
        seeded_ids,
        name="history-and-live",
        status=ProjectStatus.ACTIVE,
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )
    _deployment_with_status(db_session, project, DeploymentStatus.SUPERSEDED)
    _deployment_with_status(db_session, project, DeploymentStatus.STOPPED)
    live = _deployment_with_status(db_session, project, DeploymentStatus.ACTIVE)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 0
    db_session.refresh(project)
    db_session.refresh(live)
    assert project.status == ProjectStatus.ACTIVE
    assert live.status == DeploymentStatus.ACTIVE
    assert manager.removed == []


def test_traefik_on_the_deployment_row_decides_not_the_project_status(db_session, seeded_ids, monkeypatch):
    """A legacy row whose status never moved past SUBMITTED but
    that has an ACTIVE deployment is exempt; one that CLAIMS to be ACTIVE with no deployment at
    all is not."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)
    legacy_live = _idle_project(
        db_session, seeded_ids, name="legacy-live", status=ProjectStatus.SUBMITTED, last_active_at=stale
    )
    _active_deployment(db_session, legacy_live)
    claims_active = _idle_project(
        db_session, seeded_ids, name="claims-active", status=ProjectStatus.ACTIVE, last_active_at=stale
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=FakeDeployManager())

    assert result == 1
    db_session.refresh(legacy_live)
    db_session.refresh(claims_active)
    assert legacy_live.status == ProjectStatus.SUBMITTED
    assert claims_active.status == ProjectStatus.ARCHIVED


def test_traefik_on_a_live_service_is_never_a_candidate_however_often_the_sweep_runs(
    db_session, seeded_ids, monkeypatch
):
    """A flag nobody will act on is noise: across repeated passes (the sweep runs hourly) the
    exempt project still collects no `project.idle_flagged` entry."""
    _pin_sweep(monkeypatch, traefik_enabled=True)
    project = _idle_project(
        db_session,
        seeded_ids,
        name="never-flagged",
        status=ProjectStatus.ACTIVE,
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )
    _active_deployment(db_session, project)

    for hours in (0, 2, 4):
        assert sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW + timedelta(hours=hours)) == 0

    assert db_session.query(AuditLog).filter_by(action="project.idle_flagged").count() == 0
    assert project_lifecycle.idle_candidates(db_session, cutoff=_FAR_PAST_EPOCH_NOW) == []


def test_traefik_off_a_live_service_idle_past_the_threshold_is_stopped_and_archived(
    db_session, seeded_ids, monkeypatch
):
    """The control: the service proxy records traffic here, so the signal is real and the project is
    judged like any other. Same data as the Traefik-on test above, one setting different."""
    _pin_sweep(monkeypatch, traefik_enabled=False)
    stale = _FAR_PAST_EPOCH_NOW - timedelta(days=200)
    project = _idle_project(
        db_session, seeded_ids, name="live-without-traefik", status=ProjectStatus.ACTIVE, last_active_at=stale
    )
    deployment = _active_deployment(db_session, project)
    manager = FakeDeployManager()

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, deploy_manager=manager)

    assert result == 1
    db_session.refresh(project)
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.STOPPED
    assert manager.removed == [deployment.id]
    assert project.status == ProjectStatus.ARCHIVED
    assert _sweep_audit_actions(db_session, project) == ["project.archived", "project.idle_flagged"]


def test_sweep_idle_projects_ignores_projects_active_within_the_threshold(
    db_session, seeded_ids, monkeypatch
):
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)

    project = _idle_project(
        db_session,
        seeded_ids,
        name="recently-active",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=10),
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW)

    assert result == 0
    db_session.refresh(project)
    assert project.status == ProjectStatus.SUBMITTED
    assert db_session.query(AuditLog).filter_by(action="project.idle_flagged").count() == 0


def test_sweep_idle_projects_ignores_already_archived_projects(db_session, seeded_ids, monkeypatch):
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)

    _idle_project(
        db_session,
        seeded_ids,
        name="already-archived",
        status=ProjectStatus.ARCHIVED,
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=400),
    )

    result = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW)

    assert result == 0
    assert db_session.query(AuditLog).filter_by(action="project.idle_flagged").count() == 0


def test_sweep_idle_projects_respects_the_activity_signal_epoch(db_session, seeded_ids, monkeypatch):
    """(c2): the single test that distinguishes "the feature works" from
    "the feature empties the platform on the day it is enabled" — same
    data, two simulated "todays". Shortly after the epoch, the sweep must
    take no action even though the project is stale by threshold; once
    "today" is past epoch + threshold, the identical data is acted on."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)

    epoch = project_lifecycle.ACTIVITY_SIGNAL_EPOCH
    # Recorded well BEFORE the epoch -- the fabricated pre-epoch shape
    # trap T2 describes. This is deliberate: `last_active_at` this stale
    # already satisfies the plain SQL candidate query (`idle_candidates`)
    # at BOTH "todays" below, so if the "too early" case comes back 0
    # candidates, that can only be the epoch guard refusing to act --
    # not the ordinary threshold check happening not to have tripped yet
    # (the bug this test's first draft actually had: pinning
    # `last_active_at = epoch` made the early case fail closed for the
    # wrong reason, which a planted-mutation check caught).
    project = _idle_project(
        db_session, seeded_ids, name="epoch-edge", last_active_at=epoch - timedelta(days=400)
    )

    just_after_epoch = epoch + timedelta(days=5)
    result_too_early = sweep_idle_projects(db_session, now=just_after_epoch)
    assert result_too_early == 0
    db_session.refresh(project)
    assert project.status == ProjectStatus.SUBMITTED
    assert db_session.query(AuditLog).filter_by(action="project.idle_flagged").count() == 0

    past_epoch_plus_threshold = epoch + timedelta(days=91)
    result_after = sweep_idle_projects(db_session, now=past_epoch_plus_threshold)
    assert result_after == 1
    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


def test_sweep_idle_projects_scan_state_gates_repeated_runs(db_session, seeded_ids, monkeypatch):
    """(design decision 3): an explicit `scan_state` dict makes a second
    call within the configured interval a no-op, even with a real
    candidate sitting there -- and due again once the interval elapses."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_interval_hours", 1)

    project = _idle_project(
        db_session,
        seeded_ids,
        name="interval-gated",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )

    scan_state: dict = {}
    first = sweep_idle_projects(db_session, now=_FAR_PAST_EPOCH_NOW, scan_state=scan_state)
    assert first == 1
    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED

    # A second, still-idle candidate arriving before the interval elapses
    # must not be scanned for yet -- the state dict says it isn't due.
    other = _idle_project(
        db_session,
        seeded_ids,
        name="interval-gated-2",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )
    soon_after = _FAR_PAST_EPOCH_NOW + timedelta(minutes=30)
    second = sweep_idle_projects(db_session, now=soon_after, scan_state=scan_state)
    assert second == 0
    db_session.refresh(other)
    assert other.status == ProjectStatus.SUBMITTED

    past_interval = _FAR_PAST_EPOCH_NOW + timedelta(hours=2)
    third = sweep_idle_projects(db_session, now=past_interval, scan_state=scan_state)
    assert third == 1
    db_session.refresh(other)
    assert other.status == ProjectStatus.ARCHIVED


@contextlib.contextmanager
def _frozen_utcnow(moment):
    """`sweep_idle_projects` defaults `now` to `datetime.now(timezone.
    utc)` when called with no override (exactly how `run_iteration` calls
    it) -- freezing `worker.datetime` for the duration of one call is
    simpler than threading a `now=` parameter through `run_iteration`
    itself, which no other sweep needs either."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is not None else moment.replace(tzinfo=None)

    original = worker.datetime
    worker.datetime = _FrozenDatetime
    try:
        yield
    finally:
        worker.datetime = original


def test_run_iteration_sweeps_idle_projects(db_session, seeded_ids, monkeypatch):
    """The wiring into `run_iteration` itself, mirroring
    `test_run_iteration_sweeps_service_data_backups`'s own shape."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)
    monkeypatch.setattr(settings, "project_lifecycle_idle_threshold_days", 90)

    project = _idle_project(
        db_session,
        seeded_ids,
        name="via-run-iteration",
        last_active_at=_FAR_PAST_EPOCH_NOW - timedelta(days=200),
    )

    with _frozen_utcnow(_FAR_PAST_EPOCH_NOW):
        run_iteration(db_session, PlaceholderBuildExecutor())

    db_session.refresh(project)
    assert project.status == ProjectStatus.ARCHIVED


def test_run_iteration_does_not_crash_when_idle_sweep_raises(db_session, monkeypatch):
    """(a1): `run_iteration`'s own try/except is a second layer, never the
    guarantee -- but it must still hold if `sweep_idle_projects` itself
    breaks before ever reaching its own per-candidate try/except (e.g. the
    candidate query itself raising)."""
    monkeypatch.setattr(settings, "project_lifecycle_zombie_scan_enabled", True)

    def _boom(*a, **k):
        raise RuntimeError("simulated candidate-query failure")

    monkeypatch.setattr(project_lifecycle, "idle_candidates", _boom)

    # Must not raise.
    run_iteration(db_session, PlaceholderBuildExecutor())


def test_run_iteration_wraps_expired_preview_sweep_in_its_own_try_except(db_session, monkeypatch):
    """(e1): `sweep_expired_previews` was the last unwrapped sweep in
    `run_iteration` -- a DB-level failure here must not kill the worker
    loop, the same guarantee every other sweep already has."""

    def _boom(db):
        raise RuntimeError("simulated preview-expiry query failure")

    monkeypatch.setattr(worker, "find_expired_preview_build_ids", _boom)

    # Must not raise.
    run_iteration(db_session, PlaceholderBuildExecutor())


# -- worker.sweep_orphaned_preview_stores --------------------------


def test_sweep_orphaned_preview_stores_noop_when_postgres_unconfigured(db_session, monkeypatch):
    """(a2): with no admin DSN configured, neither Postgres nor Docker is
    ever touched -- pinned with `_ExplodingDockerClient`, checked via its
    `.touched` flag directly (not merely via a swallowed exception, which
    an orchestration-level `except Exception:` could mask)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    docker_client = _ExplodingDockerClient()

    result = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

    assert result == 0
    assert docker_client.touched is False
    assert db_session.query(AuditLog).filter_by(action="preview_orphan.reclaimed").count() == 0


def test_sweep_orphaned_preview_stores_never_touches_docker_with_no_candidates(
    db_session, monkeypatch
):
    """(a2): Postgres IS configured, but there is nothing a Postgres-side
    orphan or a candidate build could point at -- Docker must still never
    be constructed."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    docker_client = _ExplodingDockerClient()

    result = sweep_orphaned_preview_stores(db_session, docker_client=docker_client)

    assert result == 0
    assert docker_client.touched is False


def test_sweep_orphaned_preview_stores_leaves_a_non_terminal_build_alone(
    db_session, seeded_ids, monkeypatch
):
    """(b1): a RUNNING build's `pv_` name is never reclaimed, whatever
    Postgres enumeration returns for it."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project = _make_project(db_session, seeded_ids, "orphan-unit-running")
    build = _make_build(db_session, project.id, status=BuildStatus.RUNNING)
    name = preview_data._preview_identifier(build.id)

    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [name])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [name])

    # NOT an "explode if called" double: the sweep's OWN per-orphan
    # try/except (a4) would silently swallow that and report result == 0
    # regardless of whether the drop was actually attempted -- a test
    # named for a risk must be able to reach it. Recording
    # the call instead is what a planted mutation (dropping the `and` to
    # an `or` in `_preview_store_is_orphaned`) actually catches.
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))

    result = sweep_orphaned_preview_stores(db_session, docker_client=_ExplodingDockerClient())

    assert result == 0
    assert dropped == []


def test_sweep_orphaned_preview_stores_leaves_a_live_preview_alone(db_session, seeded_ids, monkeypatch):
    """decision 1: TERMINAL alone is not enough -- a terminal build whose
    preview is still live (`preview_expires_at` set) is not an orphan
    either. Distinct from the non-terminal case above."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project = _make_project(db_session, seeded_ids, "orphan-unit-live-preview")
    build = _make_build(db_session, project.id, status=BuildStatus.SUCCEEDED)
    build.preview_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    db_session.commit()
    name = preview_data._preview_identifier(build.id)

    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [name])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])

    # See the non-terminal test above for why this records calls instead
    # of raising -- an "explode if called" double is silently swallowed by
    # the sweep's own per-orphan try/except and would never fail red.
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))

    result = sweep_orphaned_preview_stores(db_session, docker_client=_ExplodingDockerClient())

    assert result == 0
    assert dropped == []


def test_sweep_orphaned_preview_stores_reclaims_a_terminal_null_build(db_session, seeded_ids, monkeypatch):
    """decision 1, clause 1: TERMINAL + `preview_expires_at IS NULL`."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project = _make_project(db_session, seeded_ids, "orphan-unit-terminal-null")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)
    name = preview_data._preview_identifier(build.id)

    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [name])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [name])
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))
    # This build's own id IS an orphan candidate too (decision 1 applies
    # uniformly), so the Docker gate legitimately opens here regardless of
    # the Postgres-side result -- an empty redis list keeps this test
    # about the Postgres half only, without relying on an exploding
    # client's exception being silently swallowed by the sweep's own
    # enumeration-failure handling.
    monkeypatch.setattr(preview_data, "list_preview_redis_containers", lambda **k: [])

    result = sweep_orphaned_preview_stores(db_session, docker_client=object())

    assert result == 1
    assert dropped == [name]  # one union name, not once per set it appeared in
    entry = db_session.query(AuditLog).filter_by(action="preview_orphan.reclaimed").one()
    assert entry.target == f"preview_postgres:{name}"


def test_sweep_orphaned_preview_stores_reclaims_a_name_with_no_build_row(db_session, monkeypatch):
    """decision 1, clause 2: no build row at all maps to this `pv_`
    identifier -- the builds table can be entirely empty."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: ["pv_untracked_name"])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))
    # The Postgres orphan found above legitimately opens the Docker gate
    # (it might have a redis pair) -- see the terminal-null test above.
    monkeypatch.setattr(preview_data, "list_preview_redis_containers", lambda **k: [])

    result = sweep_orphaned_preview_stores(db_session, docker_client=object())

    assert result == 1
    assert dropped == ["pv_untracked_name"]


def test_sweep_orphaned_preview_stores_continues_after_one_drop_failure(db_session, monkeypatch):
    """(a4): one orphan's drop failure is logged and the sweep continues
    to the next one."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(
        preview_data, "list_preview_databases", lambda: ["pv_failing_one", "pv_healthy_one"]
    )
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_redis_containers", lambda **k: [])

    dropped = []

    def _drop(name):
        if name == "pv_failing_one":
            raise RuntimeError("simulated drop failure")
        dropped.append(name)

    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", _drop)

    result = sweep_orphaned_preview_stores(db_session, docker_client=object())

    assert result == 1
    assert dropped == ["pv_healthy_one"]


def test_sweep_orphaned_preview_stores_survives_a_raising_enumeration(db_session, monkeypatch):
    """Enumeration itself failing (an unreachable admin DSN, mid-sweep) is
    caught and logged -- never raised out of the sweep."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)

    def _boom():
        raise RuntimeError("simulated admin connection failure")

    monkeypatch.setattr(preview_data, "list_preview_databases", _boom)
    monkeypatch.setattr(preview_data, "list_preview_roles", _boom)

    # Must not raise.
    result = sweep_orphaned_preview_stores(db_session, docker_client=_ExplodingDockerClient())

    assert result == 0


def test_sweep_orphaned_preview_stores_reclaims_an_orphaned_redis_container(
    db_session, seeded_ids, monkeypatch
):
    """The Docker half, reconciled by `forge.build_id` (decision 4) -- a
    terminal+null build with no Postgres name enumerated at all still
    opens the Docker gate and gets its redis container reclaimed."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project = _make_project(db_session, seeded_ids, "orphan-unit-redis-only")
    build = _make_build(db_session, project.id, status=BuildStatus.FAILED)

    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    monkeypatch.setattr(
        preview_data,
        "list_preview_redis_containers",
        lambda *, docker_client: [{"name": "forge-preview-redis-" + build.id, "build_id": build.id}],
    )
    dropped = []
    monkeypatch.setattr(
        preview_data,
        "drop_preview_redis_by_build_id",
        lambda bid, *, docker_client: dropped.append(bid),
    )

    result = sweep_orphaned_preview_stores(db_session, docker_client=object())

    assert result == 1
    assert dropped == [build.id]
    entry = db_session.query(AuditLog).filter_by(action="preview_orphan.reclaimed").one()
    assert entry.target == f"preview_redis:forge-preview-redis-{build.id}"


def test_sweep_orphaned_preview_stores_ignores_a_redis_container_with_no_label(
    db_session, seeded_ids, monkeypatch
):
    """decision 4: never guess ownership from a container's name -- a
    missing `forge.build_id` label makes an otherwise-candidate-opened
    Docker pass skip that entry entirely rather than reverse its name."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    project = _make_project(db_session, seeded_ids, "orphan-unit-redis-no-label")
    _make_build(db_session, project.id, status=BuildStatus.FAILED)

    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    monkeypatch.setattr(
        preview_data,
        "list_preview_redis_containers",
        lambda *, docker_client: [{"name": "some-mystery-container", "build_id": None}],
    )

    # See the non-terminal-build test above for why this records calls
    # instead of raising -- an "explode if called" double is silently
    # swallowed by the sweep's own per-orphan try/except.
    dropped = []
    monkeypatch.setattr(
        preview_data, "drop_preview_redis_by_build_id", lambda bid, **k: dropped.append(bid)
    )

    result = sweep_orphaned_preview_stores(db_session, docker_client=object())

    assert result == 0
    assert dropped == []


def test_sweep_orphaned_preview_stores_scan_state_gates_repeated_runs(db_session, monkeypatch):
    """(decision 6): same in-process due-ness shape as `sweep_idle_
    projects`'s own -- a second call within the configured interval is a
    no-op even with a real orphan sitting there, and due again once the
    interval elapses."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(settings, "preview_orphan_sweep_interval_hours", 1)
    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: ["pv_scan_state_gated"])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))
    # The Postgres orphan found on every call that actually runs (first,
    # third) legitimately opens the Docker gate -- see the terminal-null
    # test above for why an empty redis list, not an exploding client, is
    # the honest way to say "not what this test is about".
    monkeypatch.setattr(preview_data, "list_preview_redis_containers", lambda **k: [])

    now = datetime.now(timezone.utc)
    scan_state: dict = {}

    first = sweep_orphaned_preview_stores(
        db_session, now=now, docker_client=object(), scan_state=scan_state
    )
    assert first == 1
    assert dropped == ["pv_scan_state_gated"]

    # Gated entirely before ANY Postgres/Docker call -- the exploding
    # client here really does prove nothing is touched.
    soon_after = now + timedelta(minutes=30)
    second = sweep_orphaned_preview_stores(
        db_session, now=soon_after, docker_client=_ExplodingDockerClient(), scan_state=scan_state
    )
    assert second == 0
    assert dropped == ["pv_scan_state_gated"]  # unchanged -- not re-examined yet

    past_interval = now + timedelta(hours=2)
    third = sweep_orphaned_preview_stores(
        db_session, now=past_interval, docker_client=object(), scan_state=scan_state
    )
    assert third == 1
    assert dropped == ["pv_scan_state_gated", "pv_scan_state_gated"]


def test_run_iteration_sweeps_orphaned_preview_stores(db_session, monkeypatch):
    """The wiring into `run_iteration` itself, mirroring
    `test_run_iteration_sweeps_service_data_backups`'s own shape.
    `service_postgres_admin_dsn` is shared process-wide settings, so it
    also makes `run_iteration`'s OWN backup sweep think Postgres is
    configured -- `service_data_backup.list_service_databases` is
    monkeypatched away for the same reason `test_run_iteration_sweeps_
    service_data_backups` monkeypatches that whole module: a real (and
    here, doomed-to-fail) connection attempt is not what this test is
    about."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    monkeypatch.setattr(service_data_backup, "list_service_databases", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_databases", lambda: ["pv_via_run_iteration"])
    monkeypatch.setattr(preview_data, "list_preview_roles", lambda: [])
    monkeypatch.setattr(preview_data, "list_preview_redis_containers", lambda **k: [])
    dropped = []
    monkeypatch.setattr(preview_data, "drop_preview_postgres_by_identifier", lambda n: dropped.append(n))

    run_iteration(db_session, PlaceholderBuildExecutor(), preview_orphan_docker_client=object())

    assert dropped == ["pv_via_run_iteration"]


def test_run_iteration_does_not_crash_when_orphan_sweep_raises(db_session, monkeypatch):
    """(a3): `run_iteration`'s own try/except is a second layer, never the
    guarantee -- but it must still hold if the sweep raises before its own
    per-orphan try/except (e.g. the build-row query itself failing)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    # Same shared-settings reason as test_run_iteration_sweeps_orphaned_
    # preview_stores above -- keep the backup sweep from attempting a real
    # (doomed) connection to the fake DSN this test doesn't care about.
    monkeypatch.setattr(service_data_backup, "list_service_databases", lambda: [])

    def _boom(*a, **k):
        raise RuntimeError("simulated build-row query failure")

    monkeypatch.setattr(worker, "select", _boom)

    # Must not raise.
    run_iteration(db_session, PlaceholderBuildExecutor())


# -- one iteration's exception does not end the worker -------------------


class _StopLoop(BaseException):
    """Raised by a test double to leave `main()`'s endless loop. A
    `BaseException` on purpose: the loop catches `Exception`, so this one gets
    out -- exactly as a `KeyboardInterrupt` would."""


def _drive_main(monkeypatch, iteration):
    """Runs the real `worker.main()` with its collaborators replaced: no schema
    check, no real session, `run_iteration` = `iteration`, `time.sleep`
    recorded. Returns (sessions closed, sleeps)."""
    closed: list[int] = []
    sleeps: list[float] = []

    class _Session:
        def close(self):
            closed.append(1)

    monkeypatch.setattr(worker, "assert_schema_at_head", lambda engine: None)
    monkeypatch.setattr(worker, "_select_executor", lambda: PlaceholderBuildExecutor())
    monkeypatch.setattr(worker, "SessionLocal", _Session)
    # main() also binds the instance id, builds a reconciler and runs the start-up
    # recovery before its loop; none of that is what these loop tests are about, and none of it
    # may touch the fake session (or Docker).
    monkeypatch.setattr(worker, "_bind_instance_id", lambda: "forge-test-drive-main")
    monkeypatch.setattr(worker, "_new_reconciler", lambda: None)
    monkeypatch.setattr(worker, "_recover_at_start", lambda reconciler, state, lease=None: None)
    monkeypatch.setattr(worker, "run_iteration", iteration)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))
    return closed, sleeps


def test_the_worker_loop_survives_an_iteration_that_raises(monkeypatch, caplog):
    """The real `main()`: the first iteration raises (a database error out of a
    sweep, say), and the loop must log it, wait one poll interval, and run the
    next iteration -- the exception used to end the process."""
    calls: list[int] = []

    def iteration(db, executor, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated database error")
        raise _StopLoop

    closed, sleeps = _drive_main(monkeypatch, iteration)

    with caplog.at_level("ERROR", logger="app.worker"):
        with pytest.raises(_StopLoop):
            worker.main()

    assert len(calls) == 2  # the loop went round again after the failure
    assert sleeps == [settings.worker_poll_interval_seconds]  # and waited, rather than spinning
    assert len(closed) == 2  # each iteration's session was closed, the failed one included
    failures = [r for r in caplog.records if "iteration failed" in r.getMessage()]
    assert failures and failures[0].exc_info is not None  # logged with the traceback


def test_the_worker_loop_does_not_catch_an_interrupt(monkeypatch):
    """Only `Exception` is survived: an interrupt still stops the worker."""
    def iteration(db, executor, **kwargs):
        raise KeyboardInterrupt

    _drive_main(monkeypatch, iteration)

    with pytest.raises(KeyboardInterrupt):
        worker.main()


def test_the_worker_loop_does_not_sleep_after_an_iteration_that_ran_something(monkeypatch):
    calls: list[int] = []

    def iteration(db, executor, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return object()  # something was run: no wait before the next one
        raise _StopLoop

    _closed, sleeps = _drive_main(monkeypatch, iteration)

    with pytest.raises(_StopLoop):
        worker.main()

    assert len(calls) == 2
    assert sleeps == []
