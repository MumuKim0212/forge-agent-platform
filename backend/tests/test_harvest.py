"""ADR 0032 §Decision 2: the sanitising copy of an agent's harvested
output. Pure filesystem tests -- `tmp_path`, no Docker.

Links and special files are driven two ways. A real one is made where the host
allows it (hard links everywhere, symlinks and FIFOs on POSIX or a privileged
Windows) and its test says why it skipped when it cannot. Because the dev host
is Windows without the symlink privilege, every kind is *also* driven through
the function's `lstat` seam, which runs everywhere: a plain file is reported as
a symlink/FIFO/... and the assertions are about what the function then does.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess

import pytest

from app.services import harvest
from app.services.harvest import UnsafeHarvestEntry, remove_tree, sanitise_harvest


@pytest.fixture()
def dirs(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


def _tree(root) -> dict[str, bytes | None]:
    """Relative POSIX path -> bytes (None for a directory), for everything under `root`."""
    out: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            out[os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = f.read()
    return out


def _write(path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_lstat(overrides: dict[str, int]):
    """An `lstat` that reports the given st_mode for the given paths (a regular
    file on disk can then stand in for a symlink/FIFO/...) and the real answer
    for every other path."""

    def lstat(path):
        real = os.lstat(path)
        if path in overrides:
            return os.stat_result(
                (overrides[path], real.st_ino, real.st_dev, 1, real.st_uid, real.st_gid, 0, 0, 0, 0)
            )
        return real

    return lstat


# --- (a1) regular files and directories ---------------------------------------


def test_regular_files_and_directories_are_copied_byte_for_byte(dirs):
    src, dst = dirs
    binary = bytes(range(256)) * 40
    _write(src / "app" / "app.py", b"print('hi')\n")
    _write(src / "app" / "deep" / "er" / "data.bin", binary)
    _write(src / "empty.txt", b"")
    _write(src / "crlf.txt", b"a\r\nb\r\n")
    (src / "emptydir").mkdir()

    sanitise_harvest(str(src), str(dst))

    assert _tree(dst) == {
        "app": None,
        "app/app.py": b"print('hi')\n",
        "app/deep": None,
        "app/deep/er": None,
        "app/deep/er/data.bin": binary,
        "empty.txt": b"",
        "crlf.txt": b"a\r\nb\r\n",
        "emptydir": None,
    }


def test_mode_bits_are_copied(dirs):
    src, dst = dirs
    _write(src / "run.sh", b"#!/bin/sh\n")
    _write(src / "locked.txt", b"ro")
    os.chmod(src / "run.sh", 0o755)
    os.chmod(src / "locked.txt", 0o444)
    try:
        sanitise_harvest(str(src), str(dst))

        for name in ("run.sh", "locked.txt"):
            assert stat.S_IMODE(os.stat(dst / name).st_mode) == stat.S_IMODE(os.stat(src / name).st_mode)
        # The read-only bit travels (it is the one mode bit Windows has), so this is
        # not the default mode a plain copy would have produced.
        assert stat.S_IMODE(os.stat(dst / "locked.txt").st_mode) & 0o222 == 0
        assert stat.S_IMODE(os.stat(dst / "run.sh").st_mode) & 0o200  # (mode bits, not os.access: root ignores them)
    finally:
        os.chmod(src / "locked.txt", 0o644)
        os.chmod(dst / "locked.txt", 0o644)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no execute bit to carry")
def test_the_execute_bit_is_copied(dirs):
    src, dst = dirs
    _write(src / "run.sh", b"#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)

    sanitise_harvest(str(src), str(dst))

    assert stat.S_IMODE(os.stat(dst / "run.sh").st_mode) == 0o755


# --- (a2) .git is never copied, at any depth, file or directory ----------------


def test_git_directories_and_gitlink_files_are_skipped_at_any_depth(dirs):
    src, dst = dirs
    _write(src / "keep.txt", b"keep")
    # The root one is a complete, valid repository with a hook: what host git
    # would otherwise run.
    _write(src / ".git" / "HEAD", b"ref: refs/heads/main\n")
    _write(src / ".git" / "hooks" / "pre-commit", b"#!/bin/sh\nexit 0\n")
    _write(src / ".git" / "config", b"[core]\n\tfsmonitor = evil\n")
    # A nested repository.
    _write(src / "vendor" / "lib" / ".git" / "hooks" / "pre-commit", b"#!/bin/sh\n")
    _write(src / "vendor" / "lib" / "code.py", b"code")
    # The gitlink form: `.git` is a FILE pointing elsewhere.
    _write(src / "sub" / ".git", b"gitdir: ../../elsewhere\n")
    _write(src / "sub" / "code.py", b"code")
    _write(src / ".gitignore", b"*.pyc\n")  # not named `.git`: copied

    sanitise_harvest(str(src), str(dst))

    assert _tree(dst) == {
        "keep.txt": b"keep",
        "vendor": None,
        "vendor/lib": None,
        "vendor/lib/code.py": b"code",
        "sub": None,
        "sub/code.py": b"code",
        ".gitignore": b"*.pyc\n",
    }


def test_a_git_entry_is_skipped_without_being_inspected(dirs):
    """Skipping is by name: a `.git` that is a symlink is not a refusal and is not
    looked at (host code never touches L3 git metadata, ADR 0032)."""
    src, dst = dirs
    _write(src / "keep.txt", b"keep")
    _write(src / ".git", b"stands in for a symlink")
    seen: list[str] = []

    def lstat(path):
        seen.append(path)
        return os.lstat(path)

    sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert _tree(dst) == {"keep.txt": b"keep"}
    assert seen == [os.path.join(str(src), "keep.txt")]


# --- (a3) only the root .forge is the platform's channel ------------------------


def test_the_root_forge_directory_is_skipped_but_a_nested_one_is_copied(dirs):
    src, dst = dirs
    _write(src / ".forge" / "result.json", b'{"ok": true}')
    _write(src / ".forge" / "task_input.json", b"{}")
    _write(src / ".forge" / "agent_runner.py", b"# runner")
    _write(src / "app" / ".forge" / "settings.json", b"{}")  # the agent's own directory
    _write(src / ".forge-notes.txt", b"decoy")  # only the exact name is excluded
    _write(src / ".forged", b"decoy")

    sanitise_harvest(str(src), str(dst))

    assert _tree(dst) == {
        "app": None,
        "app/.forge": None,
        "app/.forge/settings.json": b"{}",
        ".forge-notes.txt": b"decoy",
        ".forged": b"decoy",
    }


def test_a_root_forge_that_is_a_file_is_skipped_too(dirs):
    src, dst = dirs
    _write(src / ".forge", b"a file named .forge")
    _write(src / "keep.txt", b"keep")

    sanitise_harvest(str(src), str(dst))

    assert _tree(dst) == {"keep.txt": b"keep"}


# --- (a4) links and special files refuse the build, and are never read ---------


class _Spy:
    """Records every path the function opens (`shutil.copyfile`) or lists
    (`os.listdir`), so a test can assert a refused entry was never touched."""

    def __init__(self, monkeypatch):
        self.copied: list[str] = []
        self.listed: list[str] = []
        real_copy, real_listdir = harvest.shutil.copyfile, harvest.os.listdir

        def copyfile(s, d, *a, **k):
            self.copied.append(str(s))
            return real_copy(s, d, *a, **k)

        def listdir(p="."):
            self.listed.append(str(p))
            return real_listdir(p)

        monkeypatch.setattr(harvest.shutil, "copyfile", copyfile)
        monkeypatch.setattr(harvest.os, "listdir", listdir)


@pytest.mark.parametrize(
    "link_mode, kind",
    [
        (stat.S_IFLNK | 0o777, "symbolic link"),
        (stat.S_IFCHR | 0o666, "device file"),
        (stat.S_IFBLK | 0o660, "device file"),
        (stat.S_IFIFO | 0o644, "FIFO"),
        (stat.S_IFSOCK | 0o755, "socket"),
    ],
)
def test_a_link_or_special_file_refuses_and_is_never_read(dirs, monkeypatch, link_mode, kind):
    src, dst = dirs
    _write(src / "ok.txt", b"fine")
    # A real file whose bytes would be observable in dst if it were copied.
    _write(src / "link", b"HOST-SECRET-IF-READ")
    spy = _Spy(monkeypatch)
    lstat = _fake_lstat({os.path.join(str(src), "link"): link_mode})

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert raised.value.counts == {kind: 1}
    assert spy.copied == []  # nothing is copied once something is refused
    assert os.listdir(dst) == []
    assert os.path.join(str(src), "link") not in spy.listed


def test_a_symlink_to_a_directory_is_refused_and_not_descended_into(dirs, monkeypatch):
    src, dst = dirs
    _write(src / "elsewhere" / "inside.txt", b"HOST-SECRET-IF-READ")
    (src / "dirlink").mkdir()
    _write(src / "dirlink" / "reachable.txt", b"HOST-SECRET-IF-READ")
    spy = _Spy(monkeypatch)
    lstat = _fake_lstat({os.path.join(str(src), "dirlink"): stat.S_IFLNK | 0o777})

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert raised.value.counts == {"symbolic link": 1}
    assert os.path.join(str(src), "dirlink") not in spy.listed  # never listed, so never followed
    assert spy.copied == []


def test_a_symlink_pointing_outside_the_tree_is_refused_and_the_target_is_not_read(dirs, tmp_path, monkeypatch):
    src, dst = dirs
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"HOST-SECRET-IF-READ")
    # The seam stands in for the symlink (see the module docstring); the
    # "target" is a real file outside src.
    _write(src / "escape", b"link body")
    spy = _Spy(monkeypatch)
    lstat = _fake_lstat({os.path.join(str(src), "escape"): stat.S_IFLNK | 0o777})

    with pytest.raises(UnsafeHarvestEntry):
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert str(outside) not in spy.copied and spy.copied == []
    assert os.listdir(dst) == []


def _can_symlink(tmp_path) -> str | None:
    """None if this host can make a symlink, else the reason it cannot."""
    probe = tmp_path / "probe-target"
    probe.write_bytes(b"")
    try:
        os.symlink(probe, tmp_path / "probe-link")
    except (OSError, NotImplementedError) as exc:
        return f"this host cannot create symlinks ({type(exc).__name__}: no privilege); the lstat-seam tests above cover the same behaviour"
    return None


def test_real_symlinks_are_refused_and_their_targets_never_read(dirs, tmp_path, monkeypatch):
    reason = _can_symlink(tmp_path)
    if reason:
        pytest.skip(reason)
    src, dst = dirs
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"HOST-SECRET-IF-READ")
    outside_dir = tmp_path / "outside-dir"
    _write(outside_dir / "inside.txt", b"HOST-SECRET-IF-READ")
    _write(src / "real.txt", b"in-tree target")
    os.symlink(src / "real.txt", src / "to-file")  # symlink to a file in the tree
    os.symlink(outside_dir, src / "to-dir", target_is_directory=True)  # symlink to a directory
    os.symlink(outside, src / "to-outside")  # symlink pointing outside the tree
    spy = _Spy(monkeypatch)

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst))

    assert raised.value.counts == {"symbolic link": 3}
    assert spy.copied == []
    assert str(src / "to-dir") not in spy.listed
    assert os.listdir(dst) == []


# --- hard links (real everywhere: NTFS and POSIX both have them) ----------------


def test_a_hard_link_refuses_the_build(dirs):
    src, dst = dirs
    _write(src / "a.txt", b"shared")
    os.link(src / "a.txt", src / "b.txt")
    _write(src / "ok.txt", b"fine")

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst))

    # Both names of a two-name link have st_nlink == 2: the function sees two entries.
    assert raised.value.counts == {"hard link": 2}
    assert os.listdir(dst) == []


def test_a_hard_link_to_a_file_outside_the_tree_refuses_too(dirs, tmp_path):
    src, dst = dirs
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    os.link(outside, src / "inside-name")

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst))

    assert raised.value.counts == {"hard link": 1}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo does not exist on this platform")
def test_a_real_fifo_refuses_and_is_never_opened(dirs):
    """Opening a FIFO for read blocks forever with no writer, so a copy attempt
    would hang this test rather than fail it."""
    src, dst = dirs
    os.mkfifo(src / "pipe")

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst))

    assert raised.value.counts == {"FIFO": 1}


# --- (a5) each kind has its own message; messages carry no path ------------------


def test_each_refusal_kind_has_a_distinct_message_without_any_path(dirs):
    src, dst = dirs
    messages: dict[str, str] = {}
    cases = {
        "symbolic link": stat.S_IFLNK | 0o777,
        "device file": stat.S_IFCHR | 0o666,
        "FIFO": stat.S_IFIFO | 0o644,
        "socket": stat.S_IFSOCK | 0o755,
        # Not a type the stat module names (a BSD whiteout, a Solaris door): it
        # matches none of the tests above and falls to the generic bucket.
        "special file": 0o160000 | 0o644,
    }
    for kind, mode in cases.items():
        sub = src / kind.replace(" ", "_")
        sub.mkdir()
        _write(sub / "SECRET-NAME-L3-CHOSE", b"x")
        lstat = _fake_lstat({os.path.join(str(sub), "SECRET-NAME-L3-CHOSE"): mode})
        with pytest.raises(UnsafeHarvestEntry) as raised:
            sanitise_harvest(str(sub), str(dst), lstat=lstat)
        messages[kind] = str(raised.value)
        assert raised.value.counts == {kind: 1}

    # Hard link: real.
    hl = src / "hl"
    hl.mkdir()
    _write(hl / "a", b"x")
    os.link(hl / "a", hl / "b")
    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(hl), str(dst))
    messages["hard link"] = str(raised.value)

    assert len(set(messages.values())) == len(messages) == 6
    for kind, message in messages.items():
        assert kind in message
        assert "SECRET-NAME-L3-CHOSE" not in message
        assert str(src) not in message


def test_the_counts_cover_every_kind_found_and_are_reported_in_a_fixed_order(dirs):
    src, dst = dirs
    for name in ("l1", "l2", "p1"):
        _write(src / name, b"x")
    lstat = _fake_lstat(
        {
            os.path.join(str(src), "l1"): stat.S_IFLNK | 0o777,
            os.path.join(str(src), "l2"): stat.S_IFLNK | 0o777,
            os.path.join(str(src), "p1"): stat.S_IFIFO | 0o644,
        }
    )

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert raised.value.counts == {"symbolic link": 2, "FIFO": 1}
    assert str(raised.value).endswith("2 symbolic link(s), 1 FIFO(s)")


class _Stat:
    """An lstat result carrying `st_file_attributes` (only Windows stat results have it)."""

    def __init__(self, st_mode, st_nlink, st_file_attributes):
        self.st_mode, self.st_nlink, self.st_file_attributes = st_mode, st_nlink, st_file_attributes


def test_a_junction_style_reparse_point_is_a_link(dirs):
    """Windows directory junctions read as directories to S_ISDIR; the reparse
    attribute is what gives them away."""
    src, dst = dirs
    (src / "junction").mkdir()
    attrs = stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT

    def lstat(path):
        if path.endswith("junction"):
            return _Stat(stat.S_IFDIR | 0o777, 1, attrs)
        return os.lstat(path)

    with pytest.raises(UnsafeHarvestEntry) as raised:
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert raised.value.counts == {"symbolic link": 1}


# --- a clean tree is untouched by the scan; the source is never modified ---------


def test_the_source_tree_is_not_modified(dirs):
    src, dst = dirs
    _write(src / "a" / "b.txt", b"b")
    _write(src / ".git" / "HEAD", b"ref")
    before = _tree(src)

    sanitise_harvest(str(src), str(dst))

    assert _tree(src) == before


# --- remove_tree -------------------------------------------------------------------


def test_remove_tree_removes_a_tree_with_a_read_only_file(tmp_path):
    victim = tmp_path / "victim"
    _write(victim / "sub" / "ro.txt", b"x")
    os.chmod(victim / "sub" / "ro.txt", 0o444)

    remove_tree(str(victim), what="test")

    assert not victim.exists()


def test_remove_tree_accepts_none_and_a_missing_path(tmp_path):
    remove_tree(None, what="test")
    remove_tree(str(tmp_path / "never-existed"), what="test")


def test_remove_tree_logs_when_something_is_left_behind(tmp_path, monkeypatch, caplog):
    victim = tmp_path / "victim"
    _write(victim / "f.txt", b"x")
    monkeypatch.setattr(harvest.shutil, "rmtree", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="app.harvest"):
        remove_tree(str(victim), what="sanitised output")

    assert victim.exists()
    assert any("sanitised output" in r.getMessage() and str(victim) in r.getMessage() for r in caplog.records)


# --- the `.git` name rule is the filesystem's, not an exact match ----------


@pytest.mark.parametrize(
    "name",
    [
        ".git",
        ".GIT",
        ".Git",
        ".gIt",
        ".git.",  # Windows strips trailing dots and spaces from a name
        ".git ",
        ".git. .",
        "GIT~1",  # the NTFS short name of `.git`
        ".git::$INDEX_ALLOCATION",  # NTFS: a stream suffix naming the directory itself
        ".git:stream",
    ],
)
def test_is_git_name_recognises_every_spelling_a_filesystem_may_fold_to_dot_git(name):
    assert harvest.is_git_name(name) is True


@pytest.mark.parametrize(
    "name", [".gitignore", ".gitattributes", ".gitmodules", ".github", ".git-foo", "x.git", "git", ".forge", ".gi", "..git"]
)
def test_is_git_name_leaves_ordinary_names_alone(name):
    assert harvest.is_git_name(name) is False


def _make_or_skip(path, *, directory: bool) -> None:
    try:
        if directory:
            (path / "hooks").mkdir(parents=True)
            (path / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\nexit 0\n")
            (path / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        else:
            path.write_bytes(b"gitdir: ../elsewhere\n")
    except OSError as exc:
        pytest.skip(f"this host cannot create {path.name!r} ({type(exc).__name__}); is_git_name covers the spelling")


@pytest.mark.parametrize("name", [".GIT", ".Git", ".git.", ".git ", "GIT~1"])
@pytest.mark.parametrize("directory", [True, False], ids=["directory", "gitlink-file"])
@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
def test_a_case_or_dot_variant_of_git_is_skipped_at_every_depth_without_being_inspected(dirs, name, directory, nested):
    """Real filesystem entries, so what this host can create it creates:
    `.GIT`/`.Git` everywhere; on NTFS the trailing-dot/space forms are folded to
    `.git` by the OS itself (the skip still has to hold), on Linux they are distinct
    names that only the rule recognises. Names a host refuses skip with that reason."""
    src, dst = dirs
    base = src / "sub" if nested else src
    base.mkdir(exist_ok=True)
    _write(src / "keep.txt", b"keep")
    _make_or_skip(base / name, directory=directory)
    seen: list[str] = []

    def lstat(path):
        seen.append(os.path.basename(path))
        return os.lstat(path)

    sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert _tree(dst) in ({"keep.txt": b"keep"}, {"keep.txt": b"keep", "sub": None})
    assert not any(harvest.is_git_name(n) for n in seen)  # never even lstat'd, let alone read or listed


def test_a_complete_repository_under_a_case_variant_is_not_copied(dirs):
    src, dst = dirs
    _write(src / "app.py", b"print(1)\n")
    subprocess.run(["git", "init", "--quiet"], cwd=src, check=True, capture_output=True)
    os.rename(src / ".git", src / ".GIT")

    sanitise_harvest(str(src), str(dst))

    assert _tree(dst) == {"app.py": b"print(1)\n"}


# --- ADR 0032 §Amendment 2026-10-05: compiled bytecode is never published ----


def test_bytecode_is_dropped_at_any_depth_and_in_any_case_while_source_is_kept(dirs):
    src, dst = dirs
    for rel in (
        "__pycache__/top.cpython-311.pyc",
        "app/__pycache__/app.cpython-311.pyc",
        "app/migrations/versions/__pycache__/m.cpython-311.pyc",
        "app/__PyCache__/upper.cpython-311.pyc",
        "app/evil.pyc",
        "app/EVIL2.PYC",
        "app/legacy.pyo",
        "deep/er/est/compiled.Pyc",
    ):
        _write(src / rel, b"BYTECODE")
    # Names that merely resemble bytecode, and a file inside a normally named directory.
    for rel in (
        "app/app.py",
        "app/pyc.py",
        "app/notes.pyc.txt",
        "app/mypyc",
        "app/pycache/readme.md",
        "app/migrations/versions/m.py",
    ):
        _write(src / rel, b"KEEP")

    sanitise_harvest(str(src), str(dst))

    assert sorted(p for p, data in _tree(dst).items() if data is not None) == [
        "app/app.py",
        "app/migrations/versions/m.py",
        "app/mypyc",
        "app/notes.pyc.txt",
        "app/pyc.py",
        "app/pycache/readme.md",
    ]
    assert not any(os.path.basename(p).casefold() == "__pycache__" for p in _tree(dst))


def test_a_bytecode_entry_is_skipped_without_being_inspected_and_the_count_is_logged(dirs, caplog):
    """Skipped by NAME, like `.git`: a link named `x.pyc` is neither refused nor followed."""
    src, dst = dirs
    _write(src / "app" / "x.pyc", b"BYTECODE")
    _write(src / "app" / "__pycache__" / "y.pyc", b"BYTECODE")
    _write(src / "app" / "ok.py", b"ok")
    inspected: list[str] = []

    def lstat(path):
        inspected.append(os.path.basename(path))
        return os.lstat(path)

    with caplog.at_level(logging.INFO, logger="app.harvest"):
        sanitise_harvest(str(src), str(dst), lstat=lstat)

    assert "x.pyc" not in inspected and "__pycache__" not in inspected
    assert any("2 compiled-bytecode" in r.getMessage() for r in caplog.records)
    assert _tree(dst) == {"app": None, "app/ok.py": b"ok"}
