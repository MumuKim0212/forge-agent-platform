"""`app/services/protected_paths.py` -- the protected-path model.

(a) restore + drift, measured against the build's own INPUT tree;
(b) the schema check that refuses a modify build.

Pure filesystem logic over `tmp_path` trees: no git, no Docker, no DB.
"""

from __future__ import annotations

import os

import pytest

from app.services.protected_paths import (
    SchemaChangeRefused,
    changed_files,
    is_case_insensitive,
    normalise_entry,
    refuse_schema_changes,
    restore_protected_paths,
)

SCHEMA = ["app/migrations/", "app/alembic.ini"]
MARKER = "AGENT-CHOSEN-NAME-7f3a"


def _write(root, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def trees(tmp_path):
    """(skeleton, input, output) -- three empty sibling directories."""
    out = []
    for name in ("skeleton", "input", "output"):
        d = tmp_path / name
        d.mkdir()
        out.append(d)
    out[0].joinpath("Dockerfile").write_text("FROM pinned\n")
    out[0].joinpath("compose.yaml").write_text("services: pinned\n")
    return tuple(out)


def _restore(trees, protected=("Dockerfile", "compose.yaml"), **kw):
    skeleton, input_dir, output = trees
    return restore_protected_paths(
        workspace_dir=str(output),
        input_dir=str(input_dir),
        template_skeleton_dir=str(skeleton),
        protected_paths=list(protected),
        **kw,
    )


# --- (a) restore and drift ----------------------------------------------------------


def test_a1_a_create_shaped_build_reports_no_drift_and_still_gets_the_pinned_files(trees):
    skeleton, input_dir, output = trees
    _write(input_dir, "app/app.py", "seed\n")  # the template's app/ -- no manifests
    _write(output, "app/app.py", "edited\n")  # neither side has a Dockerfile or compose.yaml

    drift = _restore(trees)

    assert drift == []
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"
    assert (output / "compose.yaml").read_text() == "services: pinned\n"


def test_a2_an_edited_manifest_that_was_in_the_input_is_drift_and_restored(trees):
    skeleton, input_dir, output = trees
    _write(input_dir, "Dockerfile", "FROM pinned\n")
    _write(input_dir, "compose.yaml", "services: pinned\n")
    _write(output, "Dockerfile", "FROM agent-edited\n")
    _write(output, "compose.yaml", "services: pinned\n")  # left alone

    drift = _restore(trees)

    assert drift == ["Dockerfile"]
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"


def test_a3_a_manifest_the_agent_added_to_a_build_whose_input_had_none_is_drift_and_restored(trees):
    skeleton, input_dir, output = trees
    _write(output, "Dockerfile", "FROM agent-added\n")

    drift = _restore(trees)

    assert drift == ["Dockerfile"]
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"
    assert (output / "compose.yaml").read_text() == "services: pinned\n"  # restored too, not drift


def test_a4_a_deleted_manifest_that_was_in_the_input_is_drift_and_restored(trees):
    skeleton, input_dir, output = trees
    _write(input_dir, "Dockerfile", "FROM pinned\n")
    _write(input_dir, "compose.yaml", "services: pinned\n")
    _write(output, "Dockerfile", "FROM pinned\n")  # compose.yaml deleted by the agent

    drift = _restore(trees)

    assert drift == ["compose.yaml"]
    assert (output / "compose.yaml").read_text() == "services: pinned\n"


def test_a5_an_older_pinned_manifest_the_agent_did_not_touch_is_not_drift_and_is_refreshed(trees):
    """The template changed since the confirmed ref: the input carries the OLD pinned
    Dockerfile. Untouched by the agent, it is not drift, yet the published file is the
    CURRENT pinned one."""
    skeleton, input_dir, output = trees
    _write(input_dir, "Dockerfile", "FROM pinned-OLD\n")
    _write(output, "Dockerfile", "FROM pinned-OLD\n")

    drift = _restore(trees)

    assert drift == []
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"


def test_an_entry_the_template_pins_nothing_for_is_skipped(trees):
    skeleton, input_dir, output = trees
    _write(output, "notpinned.txt", "agent\n")

    drift = _restore(trees, protected=("notpinned.txt",))

    assert drift == []
    assert (output / "notpinned.txt").read_text() == "agent\n"  # left as it is


def test_a_directory_where_a_pinned_file_goes_does_not_stop_the_restore(trees):
    skeleton, input_dir, output = trees
    _write(output, "Dockerfile/inner.txt", "agent made a directory\n")

    drift = _restore(trees, protected=("Dockerfile",))

    assert (output / "Dockerfile").is_file()
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"
    assert drift == ["Dockerfile"]


def test_a6_a_prefix_entry_covers_nested_and_new_files_and_restores_the_pinned_set(trees):
    skeleton, input_dir, output = trees
    _write(skeleton, "x/a.txt", "pinned a\n")
    _write(skeleton, "x/sub/b.txt", "pinned b\n")
    for root in (input_dir, output):
        _write(root, "x/a.txt", "pinned a\n")
        _write(root, "x/sub/b.txt", "pinned b\n")
    _write(output, "x/sub/b.txt", "agent edit\n")  # nested edit
    _write(output, "x/sub/new.txt", "agent new\n")  # a file that did not exist
    _write(output, "xylophone.txt", "outside the prefix\n")  # same leading letters, not under x/

    drift = _restore(trees, protected=("x/",))

    assert drift == ["x/"]  # the declared entry, never the agent's file name
    assert (output / "x/a.txt").read_text() == "pinned a\n"
    assert (output / "x/sub/b.txt").read_text() == "pinned b\n"
    assert not (output / "x/sub/new.txt").exists()
    assert (output / "xylophone.txt").exists()


def test_a_prefix_entry_without_a_trailing_slash_is_one_exact_file(trees):
    skeleton, input_dir, output = trees
    _write(skeleton, "x/a.txt", "pinned a\n")
    _write(output, "x/a.txt", "agent\n")

    drift = _restore(trees, protected=("x",))  # no file named x in the template

    assert drift == []
    assert (output / "x/a.txt").read_text() == "agent\n"


def test_a6_case_variant_spellings_are_matched_when_case_is_folded(trees):
    """The logic, with the fold forced on: runs on every host."""
    skeleton, input_dir, output = trees
    _write(skeleton, "x/a.txt", "pinned\n")
    _write(input_dir, "x/a.txt", "pinned\n")
    _write(output, "X/A.TXT", "agent\n")  # a case variant of the same file

    drift = _restore(trees, protected=("x/",), case_insensitive=True)

    assert drift == ["x/"]
    assert (output / "x/a.txt").read_text() == "pinned\n"
    assert not (output / "X/A.TXT").exists() or (output / "X/A.TXT").read_text() == "pinned\n"


def test_a6_case_variant_spellings_are_distinct_files_when_case_is_not_folded(trees):
    if _measured_case_insensitive(trees[2]):
        pytest.skip("this filesystem folds case: `X/A.TXT` and `x/a.txt` cannot coexist here")
    skeleton, input_dir, output = trees
    _write(skeleton, "x/a.txt", "pinned\n")
    _write(input_dir, "x/a.txt", "pinned\n")
    _write(output, "x/a.txt", "pinned\n")
    _write(output, "X/A.TXT", "agent\n")

    drift = _restore(trees, protected=("x/",), case_insensitive=False)

    assert drift == []  # X/A.TXT is not under x/ on a filesystem that tells them apart


def _measured_case_insensitive(directory) -> bool:
    """An independent measurement, so the test does not share the code's probe."""
    (directory / "casemeasure").write_text("")
    try:
        return (directory / "CASEMEASURE").exists()
    finally:
        (directory / "casemeasure").unlink()


def test_is_case_insensitive_agrees_with_an_independent_measurement(tmp_path):
    assert is_case_insensitive(str(tmp_path)) is _measured_case_insensitive(tmp_path)
    assert os.listdir(tmp_path) == []  # the probe is removed


def test_a6_on_this_filesystem_a_case_variant_manifest_is_one_file_that_ends_up_pinned(trees):
    if not _measured_case_insensitive(trees[2]):
        pytest.skip("this filesystem is case-sensitive: `dockerfile` and `Dockerfile` are different files here")
    skeleton, input_dir, output = trees
    _write(input_dir, "Dockerfile", "FROM pinned\n")
    _write(output, "dockerfile", "FROM agent\n")  # same file as Dockerfile on this filesystem

    drift = _restore(trees, protected=("Dockerfile",))

    assert drift == ["Dockerfile"]
    names = [n for n in os.listdir(output) if n.casefold() == "dockerfile"]
    assert names == ["Dockerfile"]
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"


def test_entries_are_normalised():
    assert normalise_entry("  ./app\\migrations//  ") == "app/migrations/"
    assert normalise_entry("/Dockerfile") == "Dockerfile"


# --- (b) the schema check -----------------------------------------------------------


@pytest.fixture()
def modify_trees(tmp_path):
    """An input with a schema tree, and an output that starts as an exact copy."""
    input_dir, output = tmp_path / "input", tmp_path / "output"
    for root in (input_dir, output):
        _write(root, "app/app.py", "code\n")
        _write(root, "app/alembic.ini", "[alembic]\n")
        _write(root, "app/migrations/env.py", "env\n")
        _write(root, "app/migrations/versions/aaaa_create_items.py", "create items\n")
    return input_dir, output


def _check(modify_trees, **kw):
    input_dir, output = modify_trees
    refuse_schema_changes(input_dir=str(input_dir), output_dir=str(output), entries=SCHEMA, **kw)


def test_b1_a_new_migration_is_refused(modify_trees):
    _write(modify_trees[1], "app/migrations/versions/bbbb_add_column.py", "add column\n")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert raised.value.count == 1


def test_b2_an_edited_existing_migration_is_refused(modify_trees):
    _write(modify_trees[1], "app/migrations/versions/aaaa_create_items.py", "create items EDITED\n")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert raised.value.count == 1


def test_b3_a_deleted_migration_is_refused(modify_trees):
    os.remove(modify_trees[1] / "app/migrations/versions/aaaa_create_items.py")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert raised.value.count == 1


def test_b4_an_edited_alembic_ini_is_refused(modify_trees):
    _write(modify_trees[1], "app/alembic.ini", "[alembic]\nscript_location = elsewhere\n")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert raised.value.count == 1


def test_b5_an_untouched_schema_tree_with_code_changes_elsewhere_passes(modify_trees):
    _write(modify_trees[1], "app/app.py", "new code\n")
    _write(modify_trees[1], "app/feature.py", "a new module\n")

    _check(modify_trees)  # does not raise


def test_a_deleted_code_file_next_to_an_untouched_schema_tree_passes(modify_trees):
    os.remove(modify_trees[1] / "app/app.py")

    _check(modify_trees)


def test_a_name_that_merely_starts_like_the_prefix_is_not_under_it(modify_trees):
    _write(modify_trees[1], "app/migrations_notes.md", "not a migration\n")
    _write(modify_trees[1], "app/alembic.ini.bak", "not the config\n")

    _check(modify_trees)


def test_the_count_is_every_changed_file_and_the_message_names_only_the_declared_prefixes(modify_trees):
    out = modify_trees[1]
    _write(out, f"app/migrations/versions/{MARKER}_one.py", "x\n")
    _write(out, f"app/migrations/{MARKER}/two.py", "y\n")
    _write(out, "app/alembic.ini", "[changed]\n")
    os.remove(out / "app/migrations/env.py")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert raised.value.count == 4
    assert str(raised.value) == (
        "the agent changed the database schema on a self-modify build "
        "(4 file(s) under app/migrations/, app/alembic.ini); a schema change is not "
        "allowed on a self-modify build and must take the developer path"
    )


def test_b7_the_refusal_contains_no_agent_chosen_file_name(modify_trees):
    out = modify_trees[1]
    _write(out, f"app/migrations/versions/{MARKER}.py", "x\n")
    _write(out, f"app/migrations/{MARKER}/deep/{MARKER}.sql", "y\n")

    with pytest.raises(SchemaChangeRefused) as raised:
        _check(modify_trees)

    assert MARKER not in str(raised.value)
    assert MARKER not in repr(raised.value)
    assert MARKER not in repr(raised.value.args)
    assert all(MARKER not in p for p in raised.value.prefixes)


def test_a_case_variant_of_a_schema_path_counts_when_case_is_folded(modify_trees):
    _write(modify_trees[1], "App/Migrations/versions/bbbb_add_column.py", "add column\n")

    with pytest.raises(SchemaChangeRefused):
        _check(modify_trees, case_insensitive=True)


def test_a_case_variant_of_a_schema_path_is_not_under_it_when_case_is_not_folded(modify_trees):
    if _measured_case_insensitive(modify_trees[1]):
        pytest.skip("this filesystem folds case: `App/Migrations/...` is `app/migrations/...` here")
    _write(modify_trees[1], "App/Migrations/versions/bbbb_add_column.py", "add column\n")

    _check(modify_trees, case_insensitive=False)


def test_changed_files_with_no_schema_entries_is_empty(modify_trees):
    _write(modify_trees[1], "app/migrations/versions/bbbb_add_column.py", "x\n")

    assert changed_files(
        input_dir=str(modify_trees[0]), output_dir=str(modify_trees[1]), entries=[]
    ) == []


def test_bytecode_on_either_side_is_not_a_schema_change(modify_trees):
    """ADR 0032 §Amendment 2026-10-05: bytecode is never published, so an input cloned
    from an older confirmed ref that still carries some is not a "deletion", and
    bytecode a sandbox wrote is not an "addition"."""
    input_dir, output = modify_trees
    _write(input_dir, "app/migrations/versions/__pycache__/old.cpython-314.pyc", "old")
    _write(input_dir, "app/migrations/stray.pyc", "old")
    _write(output, "app/migrations/versions/__pycache__/new.cpython-311.pyc", "new")
    _write(output, "app/alembic.PYO", "new")

    _check(modify_trees)  # does not raise
