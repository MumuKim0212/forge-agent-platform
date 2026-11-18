"""every timestamp the API serialises carries a UTC offset, though SQLite drops tzinfo on reload.

Endpoint-level: rows are committed and the session expired, so the values really come back naive."""

from __future__ import annotations

import importlib
import pkgutil
import typing
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

import app.schemas as schemas_pkg
from app.models.approval import ApprovalStatus
from app.models.build import Build
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Project
from app.schemas.build import BuildOut
from app.schemas.types import UtcDateTime
from app.services.approval import create_approval_request_for_verdict
from app.models.policy_verdict import PolicyVerdict
from app.services.policy_checks import PolicyRiskTier
from app.services.status import BuildStatus, ProjectStatus

OFFSET = ("Z", "+00:00")


def _all_schema_models() -> list[type[BaseModel]]:
    found: dict[int, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f"app.schemas.{info.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__.startswith("app.schemas."):
                found[id(obj)] = obj
    return list(found.values())


def _is_utc_marker(meta) -> bool:
    return getattr(meta, "func", None) is not None and meta.func.__name__ == "to_utc"


def _contains_plain_datetime(annotation, metadata=()) -> bool:
    """True when a `datetime` appears without the UtcDateTime validator on it. Pydantic hoists the
    top-level Annotated metadata into `field.metadata`; inside a union it stays on the member."""
    if typing.get_origin(annotation) is typing.Annotated:
        inner, *meta = typing.get_args(annotation)
        return _contains_plain_datetime(inner, tuple(meta))
    if annotation is datetime:
        return not any(_is_utc_marker(m) for m in metadata)
    return any(_contains_plain_datetime(arg) for arg in typing.get_args(annotation) if arg is not type(None))


def test_every_datetime_field_in_app_schemas_uses_the_utc_type():
    models = _all_schema_models()
    assert len(models) > 30  # premise: the walk really found the schema modules
    offenders = [
        f"{model.__module__}.{model.__name__}.{name}"
        for model in models
        for name, field in model.model_fields.items()
        if _contains_plain_datetime(field.annotation, tuple(field.metadata))
    ]
    assert offenders == []


def test_the_type_reads_naive_as_utc_and_converts_aware():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(UtcDateTime)
    kst = timezone(timedelta(hours=9))
    assert adapter.dump_python(datetime(2026, 1, 1, 12, 0), mode="json") == "2026-01-01T12:00:00Z"
    assert adapter.dump_python(datetime(2026, 1, 1, 21, 0, tzinfo=kst), mode="json") == "2026-01-01T12:00:00Z"


def _headers(user_id):
    return {"X-Forge-User-Id": user_id}


@pytest.fixture()
def world(db_session, seeded_ids):
    now = datetime.now(timezone.utc)
    project = Project(
        name="Offset Project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(
        project_id=project.id,
        type="create",
        diff_ref="forge-build-x#1",
        status=BuildStatus.SUCCEEDED,
        started_at=now,
        ended_at=now,
        heartbeat_at=now,
        preview_url="/p",
        preview_expires_at=now + timedelta(hours=1),
    )
    db_session.add(build)
    db_session.flush()
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id, requester_id=project.owner_id
    )
    deployment = Deployment(
        project_id=project.id,
        build_id=build.id,
        confirmed_by_id=project.owner_id,
        status=DeploymentStatus.ACTIVE,
        started_at=now,
        ended_at=now,
    )
    db_session.add(deployment)
    db_session.commit()
    db_session.expire_all()
    # Premise: SQLite really hands the values back naive.
    assert db_session.get(Build, build.id).preview_expires_at.tzinfo is None
    return {"project_id": project.id, "build_id": build.id, "approval_id": request.id, "user": seeded_ids["user_id"]}


def _assert_offsets(values: dict, keys):
    for key in keys:
        assert values[key].endswith(OFFSET), (key, values[key])


def test_project_list_and_detail(client, world):
    headers = _headers(world["user"])
    listed = [p for p in client.get("/api/projects", headers=headers).json() if p["id"] == world["project_id"]][0]
    _assert_offsets(listed, ["created_at", "last_active_at"])
    detail = client.get(f"/api/projects/{world['project_id']}", headers=headers).json()
    _assert_offsets(detail, ["created_at", "last_active_at"])


def test_build_out(db_session, world):
    build = db_session.get(Build, world["build_id"])
    assert build.started_at.tzinfo is None  # premise
    out = BuildOut.model_validate(build, from_attributes=True).model_dump(mode="json")
    _assert_offsets(out, ["started_at", "ended_at", "heartbeat_at", "preview_expires_at", "created_at"])


def test_approval(client, world):
    body = client.get(f"/api/approvals/{world['approval_id']}", headers=_headers(world["user"])).json()
    assert body["created_at"].endswith(OFFSET), body["created_at"]


def test_deployment(client, world):
    rows = client.get(f"/api/projects/{world['project_id']}/deployments", headers=_headers(world["user"])).json()
    assert rows
    _assert_offsets(rows[0], ["created_at", "started_at", "ended_at"])


def test_timeline_including_preview_expires_at(client, world):
    entries = client.get(f"/api/projects/{world['project_id']}/timeline", headers=_headers(world["user"])).json()
    assert entries
    for entry in entries:
        assert entry["ts"].endswith(OFFSET), entry
    build_entry = [e for e in entries if e["kind"] == "build"][0]
    assert build_entry["data"]["preview_expires_at"].endswith(OFFSET), build_entry["data"]["preview_expires_at"]
