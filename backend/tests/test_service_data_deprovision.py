"""Unit tests for the pure-Python guard/naming half of
`app/services/service_data_deprovision.py` -- no Docker,
no Postgres. The provisioning/drop tests that need a real engine live in
`test_service_data_deprovision_integration.py`, matching the
`preview_data.py`/`test_preview_data.py` split.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import service_data_deprovision
from app.services.service_data_deprovision import ServiceDataDeprovisionError
from tests.fake_admin_dsn import FAKE_ADMIN_DSN


def test_drop_refuses_a_non_service_identifier_before_touching_the_admin_dsn(monkeypatch):
    """(a2): the guard raises BEFORE `_admin_url()` is ever reached --
    proven by unsetting the admin DSN entirely, so a connection attempt
    would itself fail with a DIFFERENT message ("...is not configured...",
    from `service_data._admin_url`)."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    with pytest.raises(ServiceDataDeprovisionError, match="refusing to drop"):
        service_data_deprovision._drop_service_postgres("pv_some_preview")


def test_drop_refuses_a_non_service_identifier_even_with_a_dsn_configured(monkeypatch):
    """Same guard, with a (garbage, unreachable) admin DSN configured --
    proves the guard is unconditional, not merely a side effect of the
    unconfigured-DSN error happening to look similar."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", FAKE_ADMIN_DSN)
    with pytest.raises(ServiceDataDeprovisionError, match="refusing to drop"):
        service_data_deprovision._drop_service_postgres("pv_some_preview")


def test_drop_service_postgres_is_a_no_op_when_nothing_is_configured(monkeypatch):
    """A `svc_`-prefixed name passes the guard; with no admin DSN at all,
    nothing could ever have been provisioned for it either, so this is a
    silent no-op (a3's "never provisioned" promise, extended to "never
    could have")."""
    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    service_data_deprovision._drop_service_postgres("svc_some_project")  # must not raise


def test_drop_service_redis_is_a_no_op_with_no_docker_client():
    """(a3): a `None` docker client never raises -- mirrors
    `preview_data._drop_preview_redis`'s exact shape."""
    service_data_deprovision._drop_service_redis("some-project-id", docker_client=None)  # must not raise


def test_delete_service_backups_is_a_no_op_when_no_directory_exists(tmp_path, monkeypatch):
    """Idempotent (mirrors a3's shape for the backup half): a project that
    was never backed up (or whose backups were already deleted) returns
    `False` rather than raising."""
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path / "does-not-exist"))
    assert service_data_deprovision.delete_service_backups("some-project-id") is False


def test_delete_service_backups_removes_the_whole_directory(tmp_path, monkeypatch):
    """The directory (named after the `svc_`-prefixed database name, via
    `service_data_backup.project_backup_dir`) and every dump inside it are
    gone afterward -- not merely the newest-dump-preserving behaviour the
    scheduled retention sweep has."""
    from app.services.service_data import _project_identifier

    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path))
    project_id = "11111111-1111-1111-1111-111111111111"
    name = _project_identifier(project_id)
    project_dir = tmp_path / name
    project_dir.mkdir()
    (project_dir / "20260101T000000Z.sql").write_text("-- dump 1", encoding="utf-8")
    (project_dir / "20260102T000000Z.sql").write_text("-- dump 2 (newest)", encoding="utf-8")

    assert service_data_deprovision.delete_service_backups(project_id) is True
    assert not project_dir.exists()


def test_deprovision_service_data_defaults_to_keeping_backups(tmp_path, monkeypatch):
    """`delete_backups=False` (the default) never touches the filesystem --
    proven by planting a backup directory and confirming it survives a
    call with no admin DSN / no Docker client configured."""
    from app.services.service_data import _project_identifier

    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path))
    project_id = "22222222-2222-2222-2222-222222222222"
    name = _project_identifier(project_id)
    project_dir = tmp_path / name
    project_dir.mkdir()
    (project_dir / "20260101T000000Z.sql").write_text("-- dump", encoding="utf-8")

    result = service_data_deprovision.deprovision_service_data(project_id, docker_client=None)

    assert result == {"backups_deleted": False}
    assert project_dir.exists()


def test_deprovision_service_data_deletes_backups_only_when_asked(tmp_path, monkeypatch):
    from app.services.service_data import _project_identifier

    monkeypatch.setattr(settings, "service_postgres_admin_dsn", None)
    monkeypatch.setattr(settings, "service_data_backup_dir", str(tmp_path))
    project_id = "33333333-3333-3333-3333-333333333333"
    name = _project_identifier(project_id)
    project_dir = tmp_path / name
    project_dir.mkdir()
    (project_dir / "20260101T000000Z.sql").write_text("-- dump", encoding="utf-8")

    result = service_data_deprovision.deprovision_service_data(
        project_id, docker_client=None, delete_backups=True
    )

    assert result == {"backups_deleted": True}
    assert not project_dir.exists()
