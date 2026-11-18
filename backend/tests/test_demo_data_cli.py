"""`python -m app.demo_data --reset` as the operator runs it -- a real child process,
against a throwaway SQLite database built by `alembic upgrade head` (the real schema, with the
foreign keys the app enforces), never the dev DB.

`test_demo_data.py` proves the scoping rules on the in-memory fixture; this proves they survive
the command line: the exit codes, what is printed, and that a real person's project under the
demo team is still there afterwards.

The child's working directory is an empty temp directory, so no `.env` is found (the suite never
reads the developer's), `DATABASE_URL` names the throwaway file, and `PYTHONPATH` carries `backend/`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from tests import migration_parity_support as parity

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEMO_TEAM = "포지 데모팀"


def _env(url: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"AUTH_MODE", "CORS_ORIGINS", "DATABASE_URL"}}
    env.update({"FORGE_ENV": "dev", "DATABASE_URL": url, "PYTHONPATH": str(BACKEND_DIR), "PYTHONIOENCODING": "utf-8"})
    return env


def _cli(cwd: Path, url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.demo_data", *args],
        cwd=cwd, env=_env(url), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def _migrated_database(tmp_path: Path) -> str:
    url = parity.sqlite_url(tmp_path / "demo.db")
    command.upgrade(parity.config(url), "head")
    return url


def _real_person_under_the_demo_team(db: Session) -> tuple[str, str]:
    team = db.query(Team).filter_by(name=DEMO_TEAM).one()
    real = User(team_id=team.id, name="실사용자", email="real.person@example.com", role="requester")
    db.add(real)
    db.flush()
    project = Project(
        name="실제 프로젝트", owner_id=real.id, team_id=team.id, template_id=db.query(Template).first().id
    )
    db.add(project)
    db.flush()
    db.add(Build(project_id=project.id, type="create", status="SUCCEEDED"))
    db.commit()
    return real.id, project.id


def test_reset_from_the_command_line_keeps_a_real_persons_project_under_the_demo_team(tmp_path):
    url = _migrated_database(tmp_path)
    first = _cli(tmp_path, url)
    assert first.returncode == 0, first.stderr
    assert "demo_data: populated." in first.stdout

    engine = create_engine(url)
    try:
        with Session(engine) as db:
            _real_id, project_id = _real_person_under_the_demo_team(db)
            demo_project = db.query(Project).filter_by(name="사내 헬프데스크").one()
            old_demo_ids = {p.id for p in db.query(Project).filter(Project.id != project_id)}
            # A real admin's act on a demo project: the audit log keeps it.
            real_entry = AuditLog(
                actor_id=_real_id, action="project.quota_changed", target=f"project:{demo_project.id}", metadata_json={}
            )
            db.add(real_entry)
            db.commit()
            real_entry_id = real_entry.id

        reset = _cli(tmp_path, url, "--reset")

        assert reset.returncode == 0, reset.stderr
        assert "removed the existing demo dataset" in reset.stdout
        assert "teams=0 users=6 projects=4" in reset.stdout  # the team stays: a real project references it
        assert "kept 1 audit_log entries" in reset.stdout
        assert "demo_data: populated." in reset.stdout
        with Session(engine) as db:
            assert db.get(Project, project_id).name == "실제 프로젝트"
            assert db.query(Build).filter_by(project_id=project_id).count() == 1
            assert db.get(AuditLog, real_entry_id) is not None
            new_demo_ids = {p.id for p in db.query(Project).filter(Project.id != project_id)}
            assert len(new_demo_ids) == 4 and old_demo_ids.isdisjoint(new_demo_ids)
            assert db.query(Team).filter_by(name=DEMO_TEAM).count() == 1
    finally:
        engine.dispose()


def test_reset_from_the_command_line_refuses_with_exit_1_and_deletes_nothing_when_blocked(tmp_path):
    url = _migrated_database(tmp_path)
    assert _cli(tmp_path, url).returncode == 0

    engine = create_engine(url)
    try:
        with Session(engine) as db:
            _real_id, project_id = _real_person_under_the_demo_team(db)
            demo_owner = db.query(User).filter_by(email="demo.owner1@forge.local").one()
            db.add(ChangeRequest(project_id=project_id, requester_id=demo_owner.id, nl_request="데모 계정으로 보낸 요청"))
            db.commit()
            before = (db.query(Project).count(), db.query(User).count(), db.query(AuditLog).count())

        reset = _cli(tmp_path, url, "--reset")

        assert reset.returncode == 1
        assert "demo_data: refused" in reset.stderr and "demo user" in reset.stderr
        assert "removed the existing demo dataset" not in reset.stdout
        with Session(engine) as db:
            assert (db.query(Project).count(), db.query(User).count(), db.query(AuditLog).count()) == before
    finally:
        engine.dispose()


def test_reset_from_the_command_line_names_the_audit_row_that_blocks_it(tmp_path):
    url = _migrated_database(tmp_path)
    assert _cli(tmp_path, url).returncode == 0

    engine = create_engine(url)
    try:
        with Session(engine) as db:
            demo_owner = db.query(User).filter_by(email="demo.owner1@forge.local").one()
            db.add(
                AuditLog(
                    actor_id=demo_owner.id,
                    action="platform.engine_changed",
                    target="platform:agent_engine",
                    metadata_json={"detail": "PAYLOAD-VALUE-MUST-NOT-LEAK"},
                )
            )
            db.commit()
            before = (db.query(Project).count(), db.query(User).count(), db.query(AuditLog).count())

        reset = _cli(tmp_path, url, "--reset")

        assert reset.returncode == 1
        assert "demo_data: refused" in reset.stderr
        assert "platform.engine_changed" in reset.stderr and "platform:agent_engine" in reset.stderr
        assert "PAYLOAD-VALUE-MUST-NOT-LEAK" not in reset.stderr + reset.stdout
        assert "removed the existing demo dataset" not in reset.stdout
        with Session(engine) as db:
            assert (db.query(Project).count(), db.query(User).count(), db.query(AuditLog).count()) == before
    finally:
        engine.dispose()
