"""the re-encrypt command.

`python -m app.reencrypt_service_env` re-encrypts every stored service env under
the current key, so an operator can follow the key change (set
`_OLD` -> set the new key -> re-encrypt -> drop `_OLD`) without finding out at the
next deploy that a row still needed the key they dropped.

Every database is a migrated `tmp_path` SQLite file (never the dev DB). Ciphertexts
are built here with `cryptography.Fernet` directly and read back the same way, so
the checks do not run through the production encrypt/decrypt they are checking.
The last test drives the real command line in a child process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker

import app.db as app_db
import app.reencrypt_service_env as reenc
from app.config import settings
from app.models.audit import AuditLog
from app.models.project import Project
from app.models.template import Template
from app.models.user import Team, User
from app.schema_check import alembic_config
from app.seed import seed
from app.services.service_env_crypto import decrypt_service_env

BACKEND_DIR = Path(__file__).resolve().parent.parent

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()
THIRD_KEY = Fernet.generate_key().decode()  # a key the platform was never told about

ENV_A = {"FORGE_SERVICE_ID": "a", "FORGE_APP_SECRET_KEY": "secret-a", "DATABASE_URL": "postgresql://a:pw-a@db/a"}
ENV_B = {"FORGE_SERVICE_ID": "b", "FORGE_APP_SECRET_KEY": "secret-b", "REDIS_URL": "redis://:pw-b@r:6379/0"}
ENV_C = {"FORGE_SERVICE_ID": "c", "FORGE_APP_SECRET_KEY": "secret-c"}


def _encrypt_under(key: str, env: dict) -> str:
    return Fernet(key.encode()).encrypt(json.dumps(env).encode()).decode()


def _decrypt_under(key: str, ciphertext: str) -> dict:
    return json.loads(Fernet(key.encode()).decrypt(ciphertext.encode()))


@pytest.fixture()
def keys(monkeypatch):
    """The platform's keys as an operator mid-rotation has them: a new current key and
    the previous one in `_OLD`, both configured (not generated for this process)."""
    monkeypatch.setattr(settings, "service_env_encryption_key", NEW_KEY)
    monkeypatch.setattr(settings, "service_env_encryption_key_old", OLD_KEY)
    monkeypatch.setattr(settings, "_service_env_encryption_key_configured", True)


@pytest.fixture(scope="module")
def migrated_seeded_template(tmp_path_factory):
    """Migrating takes most of a second; do it once per module and copy the file per test."""
    db_file = tmp_path_factory.mktemp("reencrypt-template") / "platform.db"
    url = f"sqlite:///{db_file.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as session:
        seed(session)
    engine.dispose()
    return db_file


@pytest.fixture()
def platform_db(migrated_seeded_template, tmp_path, monkeypatch):
    """A migrated, seeded file DB wired in as `app.db`'s engine/session factory (which
    the command reads at call time), plus a factory for projects."""
    db_file = tmp_path / "platform.db"
    shutil.copy(migrated_seeded_template, db_file)
    url = f"sqlite:///{db_file.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    session = factory()
    owner_id = session.query(User).filter_by(role="requester").first().id
    team_id = session.query(Team).first().id
    template_id = session.query(Template).first().id

    def add_project(name: str, ciphertext: str | None) -> str:
        project = Project(name=name, owner_id=owner_id, team_id=team_id, template_id=template_id)
        project.service_env_json = ciphertext
        session.add(project)
        session.commit()
        return project.id

    def stored(project_id: str) -> str | None:
        session.expire_all()
        return session.get(Project, project_id).service_env_json

    try:
        yield SimpleNamespace(
            session=session, factory=factory, engine=engine, url=url, db_file=db_file,
            add_project=add_project, stored=stored,
        )
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def mixed_projects(platform_db):
    """One project in each state the command has to tell apart."""
    return SimpleNamespace(
        old=platform_db.add_project("under-old", _encrypt_under(OLD_KEY, ENV_A)),
        current=platform_db.add_project("under-new", _encrypt_under(NEW_KEY, ENV_B)),
        empty=platform_db.add_project("never-deployed", None),
        lost=platform_db.add_project("under-a-third-key", _encrypt_under(THIRD_KEY, ENV_C)),
    )


# --- the three outcomes, and the property that makes the procedure safe -------------


def test_old_key_rows_are_rewritten_and_then_decrypt_under_the_new_key_alone(
    keys, platform_db, mixed_projects, monkeypatch
):
    before = platform_db.stored(mixed_projects.old)

    report = reenc.reencrypt_service_envs(platform_db.session)

    assert report.rewritten == 1
    assert report.already_current == 1
    assert report.no_env == 1
    assert report.unreadable == [mixed_projects.lost]
    assert report.changed_while_running == []
    assert report.still_under_old_key == 0

    after = platform_db.stored(mixed_projects.old)
    assert after != before  # really rewritten
    # Under the NEW key and nothing else -- read with Fernet directly, no `_OLD` in sight.
    assert _decrypt_under(NEW_KEY, after) == ENV_A
    with pytest.raises(Exception):
        _decrypt_under(OLD_KEY, after)
    # ...and `_OLD` can now be dropped without losing the row, by the platform's own reader too.
    monkeypatch.setattr(settings, "service_env_encryption_key_old", None)
    assert decrypt_service_env(after) == ENV_A


def test_a_second_run_rewrites_nothing(keys, platform_db, mixed_projects):
    reenc.reencrypt_service_envs(platform_db.session)
    snapshot = {name: platform_db.stored(pid) for name, pid in vars(mixed_projects).items()}

    second = reenc.reencrypt_service_envs(platform_db.session)

    assert second.rewritten == 0
    assert second.already_current == 2  # the one it rewrote last time, and the one that always was
    assert second.unreadable == [mixed_projects.lost]
    assert second.still_under_old_key == 0
    assert {name: platform_db.stored(pid) for name, pid in vars(mixed_projects).items()} == snapshot


def test_rows_already_current_are_not_touched(keys, platform_db, mixed_projects):
    before = platform_db.stored(mixed_projects.current)
    reenc.reencrypt_service_envs(platform_db.session)
    assert platform_db.stored(mixed_projects.current) == before  # byte for byte, not re-encrypted


def test_an_undecryptable_row_is_reported_by_id_and_left_exactly_as_it_was(keys, platform_db, mixed_projects):
    before = platform_db.stored(mixed_projects.lost)

    report = reenc.reencrypt_service_envs(platform_db.session)

    assert report.unreadable == [mixed_projects.lost]
    assert report.needs_attention is True
    assert platform_db.stored(mixed_projects.lost) == before
    assert _decrypt_under(THIRD_KEY, before) == ENV_C  # still readable by whoever holds that key


@pytest.mark.parametrize(
    "stored_value",
    ["not-fernet-at-all", "éé not ascii"],
    ids=["garbage", "non-ascii"],
)
def test_a_value_that_is_not_ciphertext_is_unreadable_not_a_crash(keys, platform_db, stored_value):
    pid = platform_db.add_project("corrupt", stored_value)
    report = reenc.reencrypt_service_envs(platform_db.session)
    assert report.unreadable == [pid]
    assert platform_db.stored(pid) == stored_value


def test_valid_ciphertext_of_something_that_is_not_json_is_unreadable(keys, platform_db):
    not_json = Fernet(NEW_KEY.encode()).encrypt(b"this is not json").decode()
    pid = platform_db.add_project("not-json", not_json)
    assert reenc.reencrypt_service_envs(platform_db.session).unreadable == [pid]


def test_with_no_old_key_a_row_under_it_is_unreadable_and_nothing_is_guessed(keys, platform_db, mixed_projects, monkeypatch):
    monkeypatch.setattr(settings, "service_env_encryption_key_old", None)
    before = platform_db.stored(mixed_projects.old)

    report = reenc.reencrypt_service_envs(platform_db.session)

    assert sorted(report.unreadable) == sorted([mixed_projects.old, mixed_projects.lost])
    assert report.rewritten == 0
    assert platform_db.stored(mixed_projects.old) == before


def test_re_encryption_changes_no_plaintext_and_is_not_project_activity(keys, platform_db, mixed_projects):
    """nothing a service sees changes, so nothing redeploys; and the zombie
    clock (`last_active_at`) is not moved by an operator command."""
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    platform_db.session.execute(
        update(Project).where(Project.id == mixed_projects.old).values(last_active_at=long_ago)
    )
    platform_db.session.commit()

    def last_active() -> str:
        return platform_db.session.execute(
            text("SELECT last_active_at FROM projects WHERE id = :i"), {"i": mixed_projects.old}
        ).scalar_one()

    before = last_active()
    assert before.startswith("2020-01-01")

    reenc.reencrypt_service_envs(platform_db.session)

    assert _decrypt_under(NEW_KEY, platform_db.stored(mixed_projects.old)) == ENV_A
    assert last_active() == before


def test_the_audit_entry_carries_counts_and_no_value(keys, platform_db, mixed_projects):
    reenc.reencrypt_service_envs(platform_db.session)

    entries = platform_db.session.query(AuditLog).filter_by(action="service_env.reencrypted").all()
    assert len(entries) == 1
    assert entries[0].metadata_json == {"rewritten": 1, "already_current": 1, "unreadable": 1}
    serialized = json.dumps(entries[0].metadata_json) + str(entries[0].target)
    for secret in ("secret-a", "pw-a", OLD_KEY, NEW_KEY):
        assert secret not in serialized

    reenc.reencrypt_service_envs(platform_db.session)  # rewrote nothing: nothing to record
    assert platform_db.session.query(AuditLog).filter_by(action="service_env.reencrypted").count() == 1


# --- a row that changes under the command --------------------------------------------


def _change_row_when_read(monkeypatch, platform_db, ciphertext, new_value):
    """Another writer gets in between the command's read of `ciphertext` and its write."""
    real = reenc.decrypt_service_env_with_provenance

    def _read_then_a_competitor_writes(value):
        result = real(value)
        if value == ciphertext:
            with platform_db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE projects SET service_env_json = :v WHERE service_env_json = :old"),
                    {"v": new_value, "old": ciphertext},
                )
        return result

    monkeypatch.setattr(reenc, "decrypt_service_env_with_provenance", _read_then_a_competitor_writes)


def test_a_row_cleared_while_the_command_ran_is_not_resurrected(keys, platform_db, monkeypatch):
    """An admin's deprovision sets the column to NULL. The command must not
    write a stale copy of a deprovisioned service's credentials back."""
    ciphertext = _encrypt_under(OLD_KEY, ENV_A)
    pid = platform_db.add_project("being-deprovisioned", ciphertext)
    _change_row_when_read(monkeypatch, platform_db, ciphertext, None)

    report = reenc.reencrypt_service_envs(platform_db.session)

    assert report.changed_while_running == [pid]
    assert report.rewritten == 0
    assert report.needs_attention is True
    assert platform_db.stored(pid) is None


def test_a_row_that_changed_but_is_still_under_the_old_key_is_reported_as_such(keys, platform_db, monkeypatch):
    """The compare-and-swap loses; the verification pass, run on the committed rows, is what
    says `_OLD` is still needed."""
    ciphertext = _encrypt_under(OLD_KEY, ENV_A)
    replacement = _encrypt_under(OLD_KEY, {**ENV_A, "REDIS_URL": "redis://:pw@r:6379/0"})
    pid = platform_db.add_project("redeployed-meanwhile", ciphertext)
    _change_row_when_read(monkeypatch, platform_db, ciphertext, replacement)

    report = reenc.reencrypt_service_envs(platform_db.session)

    assert report.changed_while_running == [pid]
    assert report.still_under_old_key == 1
    assert platform_db.stored(pid) == replacement  # the competitor's value, not overwritten

    monkeypatch.undo()  # the competitor is gone; a re-run settles it
    monkeypatch.setattr(settings, "service_env_encryption_key", NEW_KEY)
    monkeypatch.setattr(settings, "service_env_encryption_key_old", OLD_KEY)
    settled = reenc.reencrypt_service_envs(platform_db.session)
    assert settled.rewritten == 1 and settled.still_under_old_key == 0
    assert _decrypt_under(NEW_KEY, platform_db.stored(pid))["REDIS_URL"] == "redis://:pw@r:6379/0"


# --- the command: exit status and what it prints --------------------------------------


def test_a_clean_run_exits_zero_and_says_old_may_be_dropped(keys, platform_db, capsys):
    platform_db.add_project("under-old", _encrypt_under(OLD_KEY, ENV_A))
    platform_db.add_project("under-new", _encrypt_under(NEW_KEY, ENV_B))

    assert reenc.main() == 0

    out = capsys.readouterr().out
    assert "rewritten under the current key:               1" in out
    assert "already under the current key:                 1" in out
    assert "still under the old key after this run:        0" in out
    assert "SERVICE_ENV_ENCRYPTION_KEY_OLD" in out and "may be dropped" in out


def test_an_undecryptable_row_gives_a_non_zero_exit_and_is_named(keys, platform_db, mixed_projects, capsys):
    assert reenc.main() == 1

    out = capsys.readouterr().out
    assert f"project {mixed_projects.lost}" in out
    assert "could not be decrypted under either key:       1" in out
    assert "do not drop it" in out
    assert "may be dropped" not in out  # never tells the operator it is safe while it is not
    # The readable rows were still handled: one failure does not stop the rotation of the rest.
    assert _decrypt_under(NEW_KEY, platform_db.stored(mixed_projects.old)) == ENV_A


def test_a_row_still_under_the_old_key_is_not_done_and_exits_non_zero(keys, platform_db, monkeypatch, capsys):
    ciphertext = _encrypt_under(OLD_KEY, ENV_A)
    platform_db.add_project("redeployed-meanwhile", ciphertext)
    _change_row_when_read(monkeypatch, platform_db, ciphertext, _encrypt_under(OLD_KEY, ENV_B))

    assert reenc.main() == 1

    out = capsys.readouterr().out
    assert "still under the old key after this run:        1" in out
    assert "NOT DONE" in out
    assert "may be dropped" not in out


def test_the_command_refuses_a_key_this_process_generated_for_itself(platform_db, mixed_projects, capsys):
    """Unset key -> a per-process generated one (dev). Re-encrypting under that would
    leave every row readable by nothing that will ever run again."""
    assert settings.service_env_encryption_key_configured is False  # the suite's default
    before = {name: platform_db.stored(pid) for name, pid in vars(mixed_projects).items()}

    assert reenc.main() == 2

    assert "not configured" in capsys.readouterr().err
    assert {name: platform_db.stored(pid) for name, pid in vars(mixed_projects).items()} == before


@pytest.mark.parametrize("which", ["service_env_encryption_key", "service_env_encryption_key_old"])
def test_the_command_refuses_a_key_that_is_not_a_fernet_key(keys, platform_db, monkeypatch, capsys, which):
    monkeypatch.setattr(settings, which, "not-a-fernet-key")
    assert reenc.main() == 2
    assert "not a valid Fernet key" in capsys.readouterr().err


def test_the_command_refuses_a_database_that_is_not_at_the_schema_head(keys, tmp_path, monkeypatch, capsys):
    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", sessionmaker(bind=engine))
    try:
        assert reenc.main() == 2
    finally:
        engine.dispose()
    assert "refused" in capsys.readouterr().err


# --- the real command line -------------------------------------------------------------


def test_the_real_command_line_end_to_end(platform_db, tmp_path):
    """`python -m app.reencrypt_service_env` in a child process, configured only through
    its environment (as an operator runs it): rewrites, exits 0, and a second run rewrites
    nothing. Then a row nothing can read makes it exit non-zero."""
    old_id = platform_db.add_project("under-old", _encrypt_under(OLD_KEY, ENV_A))
    current_id = platform_db.add_project("under-new", _encrypt_under(NEW_KEY, ENV_B))
    env = {k: v for k, v in os.environ.items() if k not in {"AUTH_MODE", "CORS_ORIGINS"}}
    env.update(
        PYTHONPATH=str(BACKEND_DIR),
        FORGE_ENV="dev",
        DATABASE_URL=platform_db.url,
        SERVICE_ENV_ENCRYPTION_KEY=NEW_KEY,
        SERVICE_ENV_ENCRYPTION_KEY_OLD=OLD_KEY,
    )

    def run():
        return subprocess.run(
            [sys.executable, "-m", "app.reencrypt_service_env"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
        )

    first = run()
    assert first.returncode == 0, first.stderr
    assert "rewritten under the current key:               1" in first.stdout
    assert "may be dropped" in first.stdout
    assert _decrypt_under(NEW_KEY, platform_db.stored(old_id)) == ENV_A

    second = run()
    assert second.returncode == 0, second.stderr
    assert "rewritten under the current key:               0" in second.stdout
    assert "already under the current key:                 2" in second.stdout

    lost_id = platform_db.add_project("under-a-third-key", _encrypt_under(THIRD_KEY, ENV_C))
    third = run()
    assert third.returncode == 1
    assert f"project {lost_id}" in third.stdout
    assert platform_db.stored(current_id) is not None
