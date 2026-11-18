"""`python -m app.teams create|list`. Every database is a migrated tmp_path SQLite file."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as app_db
import app.teams as teams
from app.models.audit import AuditLog
from app.models.user import Team
from app.schema_check import alembic_config, assert_schema_at_head
from app.seed import seed

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def migrated_prod_template(tmp_path_factory):
    """Migrated and seeded as prod seeds it: the template, no team."""
    db_file = tmp_path_factory.mktemp("teams-template") / "platform.db"
    url = f"sqlite:///{db_file.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as session:
        seed(session, is_prod=True)
    engine.dispose()
    return db_file


@pytest.fixture()
def platform_db(migrated_prod_template, tmp_path, monkeypatch):
    db_file = tmp_path / "platform.db"
    shutil.copy(migrated_prod_template, db_file)
    url = f"sqlite:///{db_file.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _teams(factory):
    with factory() as s:
        return [(t.id, t.name) for t in s.query(Team).order_by(Team.name)]


def test_create_inserts_one_team_and_prints_id_and_name(platform_db, capsys):
    assert teams.main(["create", "Platform"]) == 0
    rows = _teams(platform_db)
    assert [n for _, n in rows] == ["Platform"]
    assert capsys.readouterr().out.strip() == f"{rows[0][0]}\tPlatform"


def test_create_twice_prints_the_same_id_and_adds_no_row_or_audit(platform_db, capsys):
    assert teams.main(["create", "Platform"]) == 0
    first = capsys.readouterr().out
    assert teams.main(["create", "Platform"]) == 0
    assert capsys.readouterr().out == first
    assert len(_teams(platform_db)) == 1
    with platform_db() as s:
        assert s.query(AuditLog).filter_by(action="team.created").count() == 1


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 101])
def test_bad_names_are_refused_and_insert_nothing(platform_db, capsys, name):
    assert teams.main(["create", name]) == 1
    assert capsys.readouterr().err.startswith("teams: refused")
    assert _teams(platform_db) == []


def test_a_name_at_the_limit_is_accepted(platform_db):
    assert teams.main(["create", "x" * 100]) == 0
    assert len(_teams(platform_db)) == 1


def test_list_prints_every_team_and_succeeds_on_an_empty_table(platform_db, capsys):
    assert teams.main(["list"]) == 0
    assert capsys.readouterr().out == ""
    teams.main(["create", "Alpha"])
    teams.main(["create", "Beta"])
    capsys.readouterr()
    assert teams.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(line.split("\t")[1] for line in lines) == ["Alpha", "Beta"]
    assert {line.split("\t")[0] for line in lines} == {i for i, _ in _teams(platform_db)}


@pytest.mark.parametrize("argv", [["create", "T"], ["list"]])
def test_both_refuse_when_the_schema_is_not_at_head(tmp_path, monkeypatch, capsys, argv):
    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", sessionmaker(bind=engine))
    with pytest.raises(Exception) as expected:
        assert_schema_at_head(engine)
    assert teams.main(argv) == 2
    err = capsys.readouterr().err
    assert str(expected.value) in err
    assert "There is no override flag." in err
    engine.dispose()


def test_create_writes_an_operator_audit_entry_with_no_user_actor(platform_db):
    teams.main(["create", "Platform"])
    with platform_db() as s:
        team = s.query(Team).one()
        entry = s.query(AuditLog).filter_by(action="team.created").one()
        assert entry.actor_id is None
        assert entry.target == f"team:{team.id}"
        assert entry.metadata_json == {"actor": "operator command", "name": "Platform"}


def test_a_connection_error_prints_the_class_and_never_the_credential(monkeypatch, capsys):
    secret = "hunter2-SECRET"
    engine = create_engine(f"postgresql+psycopg://forge:{secret}@127.0.0.1:1/forge")
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", sessionmaker(bind=engine))
    assert teams.main(["list"]) == 2
    captured = capsys.readouterr()
    assert "database error (" in captured.err
    assert secret not in captured.err + captured.out
    engine.dispose()


def test_a_team_made_by_the_command_lets_a_user_create_a_project(client, db_session, seeded_ids):
    # The fixture DB is dev-seeded (its users reference the seeded team, so it cannot be
    # emptied); the project below uses a team that exists only because the command made it.
    headers = {"X-Forge-User-Id": seeded_ids["user_id"]}
    team, created = teams.create_team(db_session, "Platform")
    assert created and team.id != seeded_ids["team_id"]
    assert team.id in [t["id"] for t in client.get("/api/teams", headers=headers).json()]
    body = {
        "name": "p1",
        "template_id": seeded_ids["template_id"],
        "team_id": team.id,
        "nl_request": "make a tool",
    }
    resp = client.post("/api/projects", headers=headers, json=body)
    assert resp.status_code == 201, resp.text


def test_the_real_command_line_in_a_child_process(migrated_prod_template, tmp_path):
    db_file = tmp_path / "platform.db"
    shutil.copy(migrated_prod_template, db_file)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file.as_posix()}"}

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "app.teams", *args],
            cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=60,
        )

    made = run("create", "Platform")
    assert made.returncode == 0, made.stderr
    listed = run("list")
    assert listed.returncode == 0 and listed.stdout == made.stdout
