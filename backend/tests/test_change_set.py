"""`app/services/change_set.py` -- the agent's measured change set.

Pure filesystem logic over `tmp_path` trees: no git, no Docker, no DB.
"""

from __future__ import annotations

import json

import pytest

from app.services.change_set import CHANGE_KINDS, MAX_CHANGED_FILES, measure_changes
from app.services.protected_paths import protected_drift, restore_protected_paths


def _write(root, rel: str, content: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


@pytest.fixture()
def trees(tmp_path):
    """(skeleton, input, output): empty sibling directories, the skeleton pinning two manifests."""
    out = []
    for name in ("skeleton", "input", "output"):
        d = tmp_path / name
        d.mkdir()
        out.append(d)
    _write(out[0], "Dockerfile", "FROM pinned\n")  # bytes: no CRLF translation on Windows
    _write(out[0], "compose.yaml", "services: pinned\n")
    return tuple(out)


def _measure(trees, **kw):
    _skeleton, input_dir, output = trees
    return measure_changes(input_dir=str(input_dir), output_dir=str(output), **kw)


# --- (a1) classification ------------------------------------------------------------


def test_a1_added_modified_and_deleted_are_classified_and_identical_files_are_not_listed(trees):
    _skeleton, input_dir, output = trees
    _write(input_dir, "app/same.py", "same\n")
    _write(input_dir, "app/edited.py", "before\n")
    _write(input_dir, "app/gone.py", "gone\n")
    _write(output, "app/same.py", "same\n")
    _write(output, "app/edited.py", "after\n")
    _write(output, "app/brand_new.py", "new\n")

    result = _measure(trees)

    assert result.entries == [
        {"path": "app/brand_new.py", "change": "added"},
        {"path": "app/edited.py", "change": "modified"},
        {"path": "app/gone.py", "change": "deleted"},
    ]
    assert result.totals == {"added": 1, "modified": 1, "deleted": 1}
    assert result.truncated is False


def test_a1_an_unchanged_tree_is_measured_as_nothing_not_as_unmeasured(trees):
    _skeleton, input_dir, output = trees
    _write(input_dir, "app/same.py", "same\n")
    _write(output, "app/same.py", "same\n")

    assert _measure(trees).to_json() == {
        "entries": [],
        "totals": {"added": 0, "modified": 0, "deleted": 0},
        "truncated": False,
    }


def test_a1_git_forge_and_bytecode_are_never_listed_on_either_side(trees):
    _skeleton, input_dir, output = trees
    # The staged input carries the adapter's `.forge/`; an old confirmed ref carries bytecode.
    _write(input_dir, ".forge/task_input.json", "{}")
    _write(input_dir, "app/__pycache__/x.cpython-314.pyc", b"OLD")
    # Absent from a sanitised tree -- asserted anyway, as if the sanitiser had missed them.
    _write(output, ".git/HEAD", "ref\n")
    _write(output, ".GIT/config", "x\n")
    _write(output, "vendor/.git", "gitdir: x\n")
    _write(output, ".forge/result.json", "{}")
    _write(output, "app/__pycache__/y.cpython-311.pyc", b"NEW")
    _write(output, "app/loose.pyc", b"NEW")
    _write(output, "app/real.py", "x\n")

    result = _measure(trees)

    assert result.entries == [{"path": "app/real.py", "change": "added"}]
    assert result.totals == {"added": 1, "modified": 0, "deleted": 0}


def test_a1_a_forge_directory_below_the_root_is_an_ordinary_path(trees):
    _skeleton, _input_dir, output = trees
    _write(output, "app/.forge/x.txt", "x\n")

    assert _measure(trees).entries == [{"path": "app/.forge/x.txt", "change": "added"}]


# --- (a2) a protected-path edit is measured, and still restored ---------------------


def test_a2_an_edit_to_the_dockerfile_is_in_the_change_set_and_in_drift_and_the_pinned_copy_is_published(trees):
    skeleton, input_dir, output = trees
    _write(input_dir, "Dockerfile", "FROM pinned\n")  # a confirmed ref carries the pinned files
    _write(input_dir, "compose.yaml", "services: pinned\n")
    _write(input_dir, "app/app.py", "code\n")
    _write(output, "Dockerfile", "FROM evil\n")  # the agent edited it
    _write(output, "compose.yaml", "services: pinned\n")
    _write(output, "app/app.py", "code\n")
    protected = ["Dockerfile", "compose.yaml"]

    measured = _measure(trees)  # BEFORE restore, as the executor does
    drift_before = protected_drift(
        workspace_dir=str(output),
        input_dir=str(input_dir),
        template_skeleton_dir=str(skeleton),
        protected_paths=protected,
    )
    drift = restore_protected_paths(
        workspace_dir=str(output),
        input_dir=str(input_dir),
        template_skeleton_dir=str(skeleton),
        protected_paths=protected,
    )

    assert measured.entries == [{"path": "Dockerfile", "change": "modified"}]
    assert drift == ["Dockerfile"]
    assert drift_before == drift  # the non-mutating judgement is restore's judgement
    assert (output / "Dockerfile").read_text() == "FROM pinned\n"  # unchanged
    # Measured AFTER restore it would have been lost: the order is the point.
    assert _measure(trees).entries == []


def test_a2_protected_drift_does_not_touch_the_workspace(trees):
    skeleton, input_dir, output = trees
    _write(output, "Dockerfile", "FROM evil\n")

    drift = protected_drift(
        workspace_dir=str(output),
        input_dir=str(input_dir),
        template_skeleton_dir=str(skeleton),
        protected_paths=["Dockerfile", "compose.yaml"],
    )

    assert drift == ["Dockerfile"]
    assert (output / "Dockerfile").read_text() == "FROM evil\n"
    assert not (output / "compose.yaml").exists()


# --- (a3) the bound -----------------------------------------------------------------


def test_a3_more_than_the_bound_is_stored_as_the_bound_with_true_totals(trees):
    _skeleton, input_dir, output = trees
    n = MAX_CHANGED_FILES + 7
    for i in range(n):
        _write(output, f"app/gen/f{i:04d}.txt", "x\n")
    _write(input_dir, "app/old.py", "old\n")  # one deletion on top

    result = _measure(trees)

    assert MAX_CHANGED_FILES == 500
    assert len(result.entries) == 500
    assert result.totals == {"added": n, "modified": 0, "deleted": 1}
    assert result.truncated is True
    # The kept entries are the first 500 in path order, not an arbitrary 500.
    assert [e["path"] for e in result.entries] == sorted(e["path"] for e in result.entries)
    assert result.entries[0]["path"] == "app/gen/f0000.txt"


def test_a3_exactly_the_bound_is_not_truncated(trees):
    _skeleton, _input_dir, output = trees
    for i in range(MAX_CHANGED_FILES):
        _write(output, f"f{i:04d}.txt", "x\n")

    result = _measure(trees)

    assert len(result.entries) == 500 and result.truncated is False


# --- (a4) ordering and separators ---------------------------------------------------


def test_a4_order_is_by_path_and_paths_use_forward_slashes(trees):
    _skeleton, input_dir, output = trees
    for rel in ("z.txt", "app/b/two.py", "app/a/one.py", "Aaa.txt", "app/a/zz/deep.py"):
        _write(output, rel, "x\n")
    _write(input_dir, "app/c.py", "x\n")

    first = _measure(trees)
    second = _measure(trees)

    assert [e["path"] for e in first.entries] == [
        "Aaa.txt",
        "app/a/one.py",
        "app/a/zz/deep.py",
        "app/b/two.py",
        "app/c.py",
        "z.txt",
    ]
    assert first == second
    assert not any("\\" in e["path"] for e in first.entries)


def test_a4_names_differing_only_in_case_are_one_file_exactly_when_folding_is_on(trees):
    _skeleton, input_dir, output = trees
    _write(input_dir, "app/Readme.md", "x\n")
    _write(output, "app/readme.md", "x\n")

    assert _measure(trees, case_insensitive=False).totals == {"added": 1, "modified": 0, "deleted": 1}
    assert _measure(trees, case_insensitive=True).totals == {"added": 0, "modified": 0, "deleted": 0}


def test_the_stored_shape_is_json_serialisable_with_the_documented_keys(trees):
    _skeleton, _input_dir, output = trees
    _write(output, "a.txt", "x\n")

    stored = json.loads(json.dumps(_measure(trees).to_json()))

    assert sorted(stored) == ["entries", "totals", "truncated"]
    assert sorted(stored["totals"]) == ["added", "deleted", "modified"]
    assert CHANGE_KINDS == ("added", "modified", "deleted")


def test_hostile_looking_names_are_kept_as_data_unaltered(trees):
    _skeleton, _input_dir, output = trees
    name = "app/b http%3A%2F%2Fevil.example x&y=1;(alert).js"
    _write(output, name, "x\n")

    assert _measure(trees).entries == [{"path": name, "change": "added"}]
