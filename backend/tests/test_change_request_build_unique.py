"""at most one change request per
build, enforced by the database (`uq_change_requests_build_id`) and by a
migration that refuses -- never resolves -- data that already violates it."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.schema_check import alembic_config
from app.seed import seed

BEFORE_THE_CONSTRAINT = "a50f1d3c7e92"
# The revision under test. Upgrades here stop AT it, not at head: the ORM `Build` below is the
# current model, so its later columns are added by hand (see `_seed_file_db_at`), and a later
# migration that adds them again would collide.
THE_CONSTRAINT = "b53e9d2a4c18"


def _cr(project_id, requester_id, build_id, text="req") -> ChangeRequest:
    return ChangeRequest(
        project_id=project_id, requester_id=requester_id, nl_request=text, build_id=build_id
    )


# --- (a2) the constraint itself ------------------------------------------------


def test_a_second_change_request_for_the_same_build_is_refused(db_session, make_build_with_request):
    build = make_build_with_request()
    requester = db_session.query(ChangeRequest).first().requester_id
    db_session.add(_cr(build.project_id, requester, build.id, "second"))

    with pytest.raises(IntegrityError) as raised:
        db_session.commit()

    # The named constraint, not some other integrity failure (a FK, a NOT NULL).
    assert "change_requests.build_id" in str(raised.value) or "uq_change_requests_build_id" in str(
        raised.value
    )
    db_session.rollback()
    assert db_session.query(ChangeRequest).filter_by(build_id=build.id).count() == 1


def test_change_requests_with_no_build_coexist(db_session, seeded_ids):
    for text in ("first", "second", "third"):
        db_session.add(_cr(None, seeded_ids["user_id"], None, text))

    db_session.commit()

    assert db_session.query(ChangeRequest).filter(ChangeRequest.build_id.is_(None)).count() == 3


def test_distinct_builds_each_keep_their_own_change_request(db_session, make_build_with_request):
    a = make_build_with_request(name="proj-a")
    b = make_build_with_request(name="proj-b")

    assert a.id != b.id
    assert db_session.query(ChangeRequest).filter(ChangeRequest.build_id.in_([a.id, b.id])).count() == 2


# --- (a3) the migration refuses duplicate data ---------------------------------


def _seed_file_db_at(db_file: Path, revision: str):
    """A file DB migrated to `revision`, with the seeded users/template, and a
    session on it (models at head are structurally identical to `revision`'s
    schema apart from the constraint under test)."""
    url = f"sqlite:///{db_file.as_posix()}"
    command.upgrade(alembic_config(url), revision)
    engine = create_engine(url)
    with engine.begin() as conn:  # columns the current ORM `Build` selects, added after `revision`
        for column in ("changed_files", "protected_path_drift"):
            conn.exec_driver_sql(f"ALTER TABLE builds ADD COLUMN {column} JSON")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN agent_summary TEXT")
        for column in ("engine", "model"):
            conn.exec_driver_sql(f"ALTER TABLE builds ADD COLUMN {column} VARCHAR")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN cost_budget_usd FLOAT")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN smoke_checks JSON")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN preview_start_failure VARCHAR")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN repair_routes JSON")
        # `projects.kind` is NOT NULL, so a column added after the fact needs a default.
        conn.exec_driver_sql("ALTER TABLE projects ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'normal'")
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    seed(session)
    return url, engine, session


def _build_with_two_requests(session) -> tuple[str, str, str]:
    owner = session.scalars(select(User).where(User.role == "requester")).first()
    project = Project(
        name="dup",
        owner_id=owner.id,
        team_id=session.scalars(select(Team)).first().id,
        template_id=session.scalars(select(Template)).first().id,
    )
    session.add(project)
    session.flush()
    build = Build(project_id=project.id, type="create")
    session.add(build)
    session.flush()
    first = _cr(project.id, owner.id, build.id, "first")
    second = _cr(project.id, owner.id, build.id, "second")
    session.add_all([first, second])
    session.commit()
    return build.id, first.id, second.id


def _constraint_names(engine) -> set[str]:
    from sqlalchemy import inspect

    return {u["name"] for u in inspect(engine).get_unique_constraints("change_requests")}


def test_upgrade_refuses_duplicates_names_the_build_and_leaves_the_rows_alone(tmp_path):
    db_file = tmp_path / "dup.db"
    url, engine, session = _seed_file_db_at(db_file, BEFORE_THE_CONSTRAINT)
    try:
        build_id, first_id, second_id = _build_with_two_requests(session)

        with pytest.raises(Exception) as raised:
            command.upgrade(alembic_config(url), THE_CONSTRAINT)

        message = str(raised.value)
        assert build_id in message
        assert "1 build id" in message
        assert "operator" in message
        # Nothing was deleted, merged or chosen, and the constraint was not added.
        session.expire_all()
        ids = {row.id for row in session.scalars(select(ChangeRequest).where(ChangeRequest.build_id == build_id))}
        assert ids == {first_id, second_id}
        assert "uq_change_requests_build_id" not in _constraint_names(engine)

        # After the operator removes one by hand, the same upgrade goes through.
        session.delete(session.get(ChangeRequest, second_id))
        session.commit()
        command.upgrade(alembic_config(url), THE_CONSTRAINT)

        assert "uq_change_requests_build_id" in _constraint_names(engine)
        session.expire_all()
        remaining = list(session.scalars(select(ChangeRequest.id).where(ChangeRequest.build_id == build_id)))
        assert remaining == [first_id]
    finally:
        session.close()
        engine.dispose()


def test_upgrade_with_no_duplicates_and_null_build_ids_succeeds(tmp_path):
    db_file = tmp_path / "clean.db"
    url, engine, session = _seed_file_db_at(db_file, BEFORE_THE_CONSTRAINT)
    try:
        owner = session.scalars(select(User)).first()
        session.add_all([_cr(None, owner.id, None, "a"), _cr(None, owner.id, None, "b")])
        session.commit()

        command.upgrade(alembic_config(url), THE_CONSTRAINT)

        assert "uq_change_requests_build_id" in _constraint_names(engine)
        assert session.query(ChangeRequest).count() == 2
    finally:
        session.close()
        engine.dispose()


def test_the_refusal_names_a_bounded_number_of_builds_and_counts_the_rest(tmp_path):
    db_file = tmp_path / "many.db"
    url, engine, session = _seed_file_db_at(db_file, BEFORE_THE_CONSTRAINT)
    try:
        ids = [_build_with_two_requests_in_new_project(session, i) for i in range(12)]

        with pytest.raises(Exception) as raised:
            command.upgrade(alembic_config(url), THE_CONSTRAINT)

        message = str(raised.value)
        assert "12 build id(s)" in message
        assert "and 2 more" in message
        named = sorted(ids)[:10]
        assert all(build_id in message for build_id in named)
        assert not any(build_id in message for build_id in sorted(ids)[10:])
    finally:
        session.close()
        engine.dispose()


def _build_with_two_requests_in_new_project(session, n: int) -> str:
    owner = session.scalars(select(User).where(User.role == "requester")).first()
    project = Project(
        name=f"dup-{n}",
        owner_id=owner.id,
        team_id=session.scalars(select(Team)).first().id,
        template_id=session.scalars(select(Template)).first().id,
    )
    session.add(project)
    session.flush()
    build = Build(project_id=project.id, type="create")
    session.add(build)
    session.flush()
    session.add_all([_cr(project.id, owner.id, build.id, "x"), _cr(project.id, owner.id, build.id, "y")])
    session.commit()
    return build.id
