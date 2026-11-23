"""a preview that fails to start is recorded on the build as a platform-authored code.

No Docker. The construction sites of `PreviewStartError` are driven through a `PreviewManager` over a
fake client (each test makes exactly one site fail and asserts the code it carries, as a literal); the
executor, the API and `clear_preview_fields` are driven over the in-memory database. Nothing but the
code is stored or served: a distinctive string put in the exception text must appear nowhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from docker.errors import APIError, ImageNotFound

from app.config import settings
from app.models.policy_verdict import PolicyVerdict
from app.models.user import User
from app.services import build_queue, preview_data
from app.services import sandbox_build_executor as sbe
from app.services.preview_manager import (
    PreviewManager,
    PreviewSigningSecretMissingError,
    PreviewStartError,
)
from tests.test_measured_facts_api import _as, project_row  # noqa: F401 - a fixture, used by name
from tests.test_sandbox_build_executor_harvest import Harness
from tests.test_sandbox_build_executor_schema import CODE_ONLY, INPUT
from tests.test_smoke_checks_api import _build, _timeline_build

LEAK = "SECRET-DOCKER-TEXT-9f3"


class _Network:
    name = "forge-preview-net-b1-abcd1234"

    def connect(self, *_a, **_k):
        pass


def _manager(monkeypatch, *, network_error=None, run_error=None, data_services=False, get_error=None):
    def create(*_a, **_k):
        if network_error is not None:
            raise network_error
        return _Network()

    def run(**_k):
        if run_error is not None:
            raise run_error
        return SimpleNamespace(id="c1")

    def get(*_a, **_k):
        if get_error is not None:
            raise get_error
        return SimpleNamespace()

    client = SimpleNamespace(
        networks=SimpleNamespace(create=create),
        containers=SimpleNamespace(run=run, get=get),
    )
    manager = PreviewManager(client=client)
    monkeypatch.setattr(manager, "stop_preview_for_project", lambda *a, **k: 0)
    monkeypatch.setattr(manager, "stop_preview", lambda *a, **k: False)
    monkeypatch.setattr(manager, "_abort_start", lambda *a, **k: None)
    monkeypatch.setattr(
        type(settings), "service_postgres_configured", property(lambda self: data_services)
    )
    return manager


def _start(manager, *, image_ref="img:1"):
    return manager.start_preview(project_id="p1", build=SimpleNamespace(id="b1", image_ref=image_ref))


def _code_of(manager, **kw) -> str:
    with pytest.raises(PreviewStartError) as caught:
        _start(manager, **kw)
    return caught.value.code


def test_a_build_with_no_image_is_no_image(monkeypatch):
    assert _code_of(_manager(monkeypatch), image_ref=None) == "NO_IMAGE"


def test_a_network_that_cannot_be_created_is_network_failed(monkeypatch):
    assert _code_of(_manager(monkeypatch, network_error=APIError(LEAK))) == "NETWORK_FAILED"


def test_the_previews_own_image_missing_is_image_missing_not_a_container_start_failure(monkeypatch):
    assert _code_of(_manager(monkeypatch, run_error=ImageNotFound(LEAK))) == "IMAGE_MISSING"


def test_any_other_docker_refusal_to_start_the_container_is_container_start_failed(monkeypatch):
    assert _code_of(_manager(monkeypatch, run_error=APIError(LEAK))) == "CONTAINER_START_FAILED"


def _provision_raises(monkeypatch, cause):
    def provision(build_id, *, docker_client=None):
        raise preview_data.PreviewDataProvisionError("redis container failed") from cause

    monkeypatch.setattr(preview_data, "provision_preview_data_services", provision)


def test_a_data_service_image_missing_is_image_missing(monkeypatch):
    manager = _manager(monkeypatch, data_services=True)
    _provision_raises(monkeypatch, ImageNotFound(LEAK))
    assert _code_of(manager) == "IMAGE_MISSING"


def test_a_data_service_that_fails_for_another_reason_is_data_services_failed(monkeypatch):
    manager = _manager(monkeypatch, data_services=True)
    _provision_raises(monkeypatch, APIError(LEAK))
    assert _code_of(manager) == "DATA_SERVICES_FAILED"


def test_a_data_service_that_cannot_be_attached_is_data_services_failed(monkeypatch):
    manager = _manager(monkeypatch, data_services=True, get_error=APIError(LEAK))
    monkeypatch.setattr(preview_data, "provision_preview_data_services", lambda *a, **k: {})
    assert _code_of(manager) == "DATA_SERVICES_FAILED"


def test_the_text_stays_in_the_message_and_the_code_is_what_the_executor_gets(monkeypatch):
    manager = _manager(monkeypatch, run_error=APIError(LEAK))
    with pytest.raises(PreviewStartError) as caught:
        _start(manager)
    assert LEAK in str(caught.value)  # the log keeps it...
    assert caught.value.code == "CONTAINER_START_FAILED"  # ...the column gets this


def test_a_missing_signing_secret_escapes_the_manager_as_its_own_type(monkeypatch):
    manager = _manager(monkeypatch)
    monkeypatch.setattr(settings, "preview_signing_secret", None)
    with pytest.raises(PreviewSigningSecretMissingError):
        _start(manager)


# --- the executor: a convenience, never a gate ------------------------------------


def _executor_with_preview(monkeypatch, start_preview):
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)
    h.executor._preview_manager = SimpleNamespace(start_preview=start_preview)
    monkeypatch.setattr(
        h.executor, "_start_preview", sbe.SandboxBuildExecutor._start_preview.__get__(h.executor)
    )
    h.smoke_factory_calls = []
    h.executor._smoke_prober_factory = lambda build: h.smoke_factory_calls.append(build)
    return h


def _raising(exc):
    def start_preview(**_k):
        raise exc

    return start_preview


@pytest.mark.parametrize(
    ("raises", "code"),
    [
        (PreviewStartError(LEAK, "IMAGE_MISSING"), "IMAGE_MISSING"),
        (PreviewStartError(LEAK, "CONTAINER_START_FAILED"), "CONTAINER_START_FAILED"),
        (PreviewSigningSecretMissingError(LEAK), "SIGNING_SECRET_MISSING"),
        (RuntimeError(LEAK), "UNEXPECTED"),
    ],
    ids=["image-missing", "container-start", "signing-secret", "unexpected"],
)
def test_a_failed_preview_records_its_code_and_the_build_still_succeeds_and_is_gated(
    monkeypatch, make_build_with_request, db_session, raises, code
):
    h = _executor_with_preview(monkeypatch, _raising(raises))
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True and result.reason is None
    assert build.preview_start_failure == code
    assert build.preview_url is None and build.smoke_checks is None
    assert h.smoke_factory_calls == []  # "not measured": the check was not even attempted
    assert db_session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 1
    assert LEAK not in (result.agent_summary or "") and LEAK not in (result.summary or "")


def test_a_preview_that_starts_clears_an_earlier_failure(monkeypatch, make_build_with_request):
    issued = SimpleNamespace(
        url="/u", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc), token_hash="h"
    )
    h = _executor_with_preview(monkeypatch, lambda **k: issued)
    build = make_build_with_request()
    build.preview_start_failure = "IMAGE_MISSING"

    h.executor._start_preview(build)

    assert build.preview_start_failure is None and build.preview_url == "/u"


# --- the API and the "expired" distinction ----------------------------------------


def test_the_code_reaches_the_list_the_detail_and_the_timeline(
    client, db_session, project_row, seeded_ids  # noqa: F811
):
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)
    build.preview_start_failure = "IMAGE_MISSING"
    db_session.commit()
    uid = seeded_ids["user_id"]
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    listing = client.get("/api/approvals", headers=_as(approver.id))
    assert listing.status_code == 200
    (listed,) = [r for r in listing.json() if r["id"] == request.id]
    detail = client.get(f"/api/approvals/{request.id}", headers=_as(uid)).json()
    timeline = _timeline_build(client, project_row, build, uid)

    assert listed["preview_start_failure"] == "IMAGE_MISSING" and listed["preview_url"] is None
    assert detail["preview_start_failure"] == "IMAGE_MISSING"
    assert timeline["preview_start_failure"] == "IMAGE_MISSING"
    assert detail["smoke_checks"] is None  # the two facts are not conflated


def test_a_build_whose_preview_started_serves_null(client, db_session, project_row, seeded_ids):  # noqa: F811
    build, request = _build(db_session, project_row, seeded_ids, smoke=None)
    uid = seeded_ids["user_id"]

    assert client.get(f"/api/approvals/{request.id}", headers=_as(uid)).json()["preview_start_failure"] is None
    assert _timeline_build(client, project_row, build, uid)["preview_start_failure"] is None


def test_clear_preview_fields_does_not_erase_a_start_failure(db_session, project_row, seeded_ids):  # noqa: F811
    build, _ = _build(db_session, project_row, seeded_ids, smoke=None)
    build.preview_start_failure = "CONTAINER_START_FAILED"
    build.preview_url = "/u"
    db_session.commit()

    build_queue.clear_preview_fields(db_session, build.id)

    db_session.refresh(build)
    assert build.preview_url is None
    assert build.preview_start_failure == "CONTAINER_START_FAILED"
