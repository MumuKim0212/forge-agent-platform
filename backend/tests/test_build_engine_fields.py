"""ADR 0035 §Decision 5: `builds.engine` / `builds.model` are measured facts, shown
in `BuildOut` and on the timeline's build entry, and NULL (not an invented default) on a
build that predates them.
"""

from __future__ import annotations

from app.models.build import Build
from app.models.project import Project
from app.schemas.build import BuildOut
from app.services.status import BuildStatus, ProjectStatus


def _project(db_session, seeded_ids) -> Project:
    project = Project(
        name="engine-fields",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def test_build_out_carries_engine_and_model(db_session, seeded_ids):
    project = _project(db_session, seeded_ids)
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED, engine="openai", model="gpt-5-mini")
    db_session.add(build)
    db_session.commit()

    out = BuildOut.model_validate(build)

    assert (out.engine, out.model) == ("openai", "gpt-5-mini")


def test_the_timeline_build_entry_carries_engine_and_model_and_null_for_an_older_build(client, db_session, seeded_ids):
    project = _project(db_session, seeded_ids)
    new = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED, engine="openai", model="gpt-5-mini")
    claude = Build(project_id=project.id, type="modify", status=BuildStatus.SUCCEEDED, engine="anthropic")
    old = Build(project_id=project.id, type="modify", status=BuildStatus.SUCCEEDED)
    db_session.add_all([new, claude, old])
    db_session.commit()

    resp = client.get(
        f"/api/projects/{project.id}/timeline", headers={"X-Forge-User-Id": seeded_ids["user_id"]}
    )

    assert resp.status_code == 200
    by_id = {e["data"]["id"]: e["data"] for e in resp.json() if e["kind"] == "build"}
    assert (by_id[new.id]["engine"], by_id[new.id]["model"]) == ("openai", "gpt-5-mini")
    assert (by_id[claude.id]["engine"], by_id[claude.id]["model"]) == ("anthropic", None)
    assert "engine" in by_id[old.id] and by_id[old.id]["engine"] is None and by_id[old.id]["model"] is None
