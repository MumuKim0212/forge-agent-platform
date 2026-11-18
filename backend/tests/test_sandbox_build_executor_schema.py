"""ADR 0009 §Amendment 2026-10-05: the executor refuses a modify-type
build that changes a schema path, before anything is published, and passes the
build's input tree to publish so drift is measured against it.

Unit seam, no Docker: the `Harness` of the harvest tests (a fake proxy, a
fake adapter returning a real `output_dir`, a stubbed staging step, recorders
for publish / image assembly / the policy gate). The engine-up version is in
`test_sandbox_build_executor_integration.py`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.policy_verdict import PolicyVerdict
from app.services.status import BuildFailureReason
from tests.test_sandbox_build_executor_harvest import Harness

MARKER = "AGENT-CHOSEN-NAME-91c2"
CREATE_INITIAL = "create items\n"

# The confirmed ref a modify build starts from: code, the Alembic config, a migration chain.
INPUT = {
    "app/app.py": b"code\n",
    "app/alembic.ini": b"[alembic]\n",
    "app/migrations/env.py": b"env\n",
    "app/migrations/versions/aaaa_create_items.py": CREATE_INITIAL.encode(),
}
# What a well-behaved agent hands back: the same tree, with a code edit and a new code file.
CODE_ONLY = {**INPUT, "app/app.py": b"edited code\n", "app/feature.py": b"a new module\n"}


def _without(files: dict, path: str) -> dict:
    return {k: v for k, v in files.items() if k != path}


SCHEMA_CHANGES = {
    "adds-a-migration": {**CODE_ONLY, f"app/migrations/versions/{MARKER}_add_column.py": b"add column\n"},
    "edits-a-migration": {**CODE_ONLY, "app/migrations/versions/aaaa_create_items.py": b"EDITED\n"},
    "deletes-a-migration": _without(CODE_ONLY, "app/migrations/versions/aaaa_create_items.py"),
    "edits-alembic-ini": {**CODE_ONLY, "app/alembic.ini": b"[alembic]\nscript_location = elsewhere\n"},
}


class _Repos:
    """Records whether the executor reached for the git host at all."""

    def __init__(self, harness: Harness) -> None:
        self.created: list = []
        harness.executor._ensure_repo = lambda *a, **k: self.created.append(True) or SimpleNamespace(full_name="o/p")


def _nothing_was_published(h: Harness, build, repos: _Repos) -> None:
    assert h.publish_calls == [] and h.assemble_calls == [] and h.gate_calls == []
    assert repos.created == []  # not even the repository was created
    assert build.diff_ref is None and build.image_ref is None


# --- (c1) a modify build that changes the schema is refused ------------------------


@pytest.mark.parametrize("change", sorted(SCHEMA_CHANGES))
def test_c1_a_modify_build_that_changes_the_schema_ends_agent_error_and_publishes_nothing(
    monkeypatch, make_build_with_request, db_session, change
):
    h = Harness(monkeypatch, output_files=SCHEMA_CHANGES[change], staged_files=INPUT)
    repos = _Repos(h)
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.AGENT_ERROR == "AGENT_ERROR"
    assert result.summary == (
        "the agent changed the database schema on a self-modify build "
        "(1 file(s) under app/migrations/, app/alembic.ini); a schema change is not "
        "allowed on a self-modify build and must take the developer path"
    )
    assert MARKER not in result.summary
    _nothing_was_published(h, build, repos)
    assert db_session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 0
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)  # (c3)


def test_the_refusal_counts_every_changed_schema_file(monkeypatch, make_build_with_request):
    files = {
        **SCHEMA_CHANGES["adds-a-migration"],
        **{k: v for k, v in SCHEMA_CHANGES["edits-alembic-ini"].items() if k == "app/alembic.ini"},
    }
    h = Harness(monkeypatch, output_files=_without(files, "app/migrations/env.py"), staged_files=INPUT)

    result = h.executor.execute(make_build_with_request(build_type="modify"))

    assert result.ok is False and "(3 file(s) under" in result.summary


# --- the same output on a create build is not refused (b6, c2) ---------------------


@pytest.mark.parametrize("change", sorted(SCHEMA_CHANGES))
def test_b6_the_same_changes_on_a_create_build_publish(monkeypatch, make_build_with_request, change):
    h = Harness(monkeypatch, output_files=SCHEMA_CHANGES[change], staged_files=INPUT)
    build = make_build_with_request(build_type="create")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert len(h.publish_calls) == len(h.assemble_calls) == len(h.gate_calls) == 1


def test_c2_a_create_build_adding_a_migration_publishes_it(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, output_files=SCHEMA_CHANGES["adds-a-migration"], staged_files=INPUT)
    build = make_build_with_request(build_type="create")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    published = h.publish_calls[0]["files"]  # the commit content handed to publish
    assert published[f"app/migrations/versions/{MARKER}_add_column.py"] == b"add column\n"
    assert published["app/migrations/versions/aaaa_create_items.py"] == CREATE_INITIAL.encode()
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)  # (c3)


# --- a modify build that leaves the schema alone is published ----------------------


def test_b5_a_modify_build_with_code_changes_only_publishes(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert h.publish_calls[0]["files"]["app/feature.py"] == b"a new module\n"
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


def test_the_staged_input_is_what_publish_receives_and_it_is_still_there(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)

    h.executor.execute(make_build_with_request(build_type="modify"))

    call = h.publish_calls[0]
    assert call["input_dir"] == h.staged_dir
    assert call["input_dir"] != call["workspace_dir"]
    assert call["input_files"] == INPUT  # the pre-agent bytes, not the agent's edit
    assert not os.path.exists(h.staged_dir)  # removed by `execute`'s finally, after publish


def test_the_schema_check_follows_the_declared_paths(monkeypatch, make_build_with_request):
    """The declaration is the platform's: with no schema path declared, a migration
    edit on a modify build is not a schema change."""
    monkeypatch.setattr(settings, "protected_schema_paths", "")
    h = Harness(monkeypatch, output_files=SCHEMA_CHANGES["edits-a-migration"], staged_files=INPUT)

    result = h.executor.execute(make_build_with_request(build_type="modify"))

    assert result.ok is True, result.summary


def test_a_check_that_cannot_read_a_file_fails_closed_naming_only_the_stage(monkeypatch, make_build_with_request):
    import app.services.sandbox_build_executor as sbe

    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=INPUT)

    def _boom(**k):
        raise OSError("C:\\secret\\path\\the-l3-chose: unreadable")

    monkeypatch.setattr(sbe, "refuse_schema_changes", _boom)

    result = h.executor.execute(make_build_with_request(build_type="modify"))

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert result.summary == (
        "harvesting the build's output failed at checking the schema paths (OSError); see the worker log"
    )
    assert "secret" not in result.summary
    assert h.publish_calls == []


# --- ADR 0032 §Amendment 2026-10-05: bytecode never counts, and never publishes ------


def test_g4_a_modify_build_that_only_adds_sandbox_bytecode_under_the_schema_prefix_publishes(
    monkeypatch, make_build_with_request
):
    """A 3.11 sandbox that imports a migration writes `cpython-311.pyc` under it. That is
    not a schema change: the sanitiser drops it before the check sees the output."""
    files = {**CODE_ONLY, "app/migrations/versions/__pycache__/aaaa_create_items.cpython-311.pyc": b"BYTECODE"}
    h = Harness(monkeypatch, output_files=files, staged_files=INPUT)
    build = make_build_with_request(build_type="modify")

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    published = h.publish_calls[0]["files"]
    assert not any("pycache" in p or p.endswith(".pyc") for p in published)
    assert published["app/migrations/versions/aaaa_create_items.py"] == CREATE_INITIAL.encode()


def test_g4_a_source_less_pyc_is_not_refused_and_is_absent_from_what_is_published(
    monkeypatch, make_build_with_request
):
    files = {**CODE_ONLY, "app/migrations/versions/evil.pyc": b"SOURCE-LESS BYTECODE"}
    h = Harness(monkeypatch, output_files=files, staged_files=INPUT)

    result = h.executor.execute(make_build_with_request(build_type="modify"))

    assert result.ok is True, result.summary
    for seen in (h.publish_calls[0]["files"], h.assemble_calls[0]["files"], h.gate_calls[0]["files"]):
        assert "app/migrations/versions/evil.pyc" not in seen
        assert not any(p.endswith((".pyc", ".pyo")) for p in seen)


def test_a_modify_build_whose_confirmed_ref_still_carries_old_bytecode_publishes(
    monkeypatch, make_build_with_request
):
    """Every confirmed ref made before the amendment holds the template's `cpython-314.pyc`
    files; the sanitised output has none, which must not read as deleted migration files."""
    staged = {**INPUT, "app/migrations/versions/__pycache__/aaaa_create_items.cpython-314.pyc": b"OLD"}
    h = Harness(monkeypatch, output_files=CODE_ONLY, staged_files=staged)

    result = h.executor.execute(make_build_with_request(build_type="modify"))

    assert result.ok is True, result.summary
