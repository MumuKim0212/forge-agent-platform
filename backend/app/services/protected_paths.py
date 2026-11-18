"""Protected paths (ADR 0009
§Amendment 2026-10-05).

Two sets of paths, both DECLARED BY THE PLATFORM (`Settings.protected_paths`,
`Settings.protected_schema_paths`), never by the agent:

  - TEMPLATE-OWNED MANIFESTS (every build type): restored from the pinned
    template, and reported as drift only when the agent changed them relative to
    the build's own INPUT tree (`restore_protected_paths`);
  - SCHEMA PATHS (modify-type builds only): a build whose output adds, edits or
    deletes a file under them is REFUSED (`refuse_schema_changes`), because
    dropping the change while publishing the code that depends on it would ship a
    service that fails against a schema that was never applied.

An entry is an exact file (`Dockerfile`) unless it ends in `/`, in which case it is
a PREFIX: every file under it, including files that did not exist before.

Case. The tree compared is whatever a filesystem presents, and a case-insensitive
filesystem presents `App/Migrations/x.py` and `app/migrations/x.py` as the same
file. So paths are compared casefolded exactly when the filesystem folds case, and
that is MEASURED on the directory in question (`is_case_insensitive`) rather than
assumed from the platform: NTFS and default APFS fold, ext4 and overlayfs do not,
and a directory can be configured either way.

Compiled bytecode (`harvest.has_bytecode_component`) is on neither side of any comparison:
the sanitising copy never publishes it, so a `.pyc` under a schema prefix in an OLD
confirmed ref is not a "deletion" and one a sandbox wrote is not an "addition".

Everything here is pure filesystem logic over two directories: no git, no Docker.
Nothing it returns or raises carries a file name the agent chose: drift
lists the DECLARED entries that drifted, and a refusal carries a count.
"""

from __future__ import annotations

import filecmp
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable

from app.services.harvest import has_bytecode_component


class SchemaChangeRefused(Exception):
    """A modify-type build changed a schema path. `count` files differ from the
    input tree under `prefixes` (the declared entries). The message is platform
    text -- the count and the declared entries, never a path the agent chose -- so
    it may be used as the build's failure detail as it is."""

    def __init__(self, count: int, prefixes: Iterable[str]) -> None:
        self.count = count
        self.prefixes = tuple(prefixes)
        super().__init__(
            f"the agent changed the database schema on a self-modify build "
            f"({count} file(s) under {', '.join(self.prefixes)}); a schema change is not "
            "allowed on a self-modify build and must take the developer path"
        )


def normalise_entry(entry: str) -> str:
    """A declared entry with forward slashes, no leading `./` or `/`, and runs of
    slashes collapsed. A trailing `/` is kept: it is what makes an entry a prefix."""
    e = re.sub(r"/+", "/", entry.strip().replace("\\", "/"))
    while e.startswith("./"):
        e = e[2:]
    return e.lstrip("/")


def is_case_insensitive(directory: str) -> bool:
    """True when `directory`'s filesystem treats names that differ only in case as
    the same file. Measured: a probe file with a mixed-case name is created in
    `directory` and looked up under the swapped-case spelling, then removed. The
    probe's name is random, so it cannot collide with anything already there."""
    fd, probe = tempfile.mkstemp(prefix="ForgeCaseProbe", dir=directory)
    os.close(fd)
    try:
        name = os.path.basename(probe)
        swapped = name.swapcase()
        return swapped != name and os.path.exists(os.path.join(directory, swapped))
    finally:
        os.unlink(probe)


def tree_files(root: str) -> dict[str, str]:
    """Every REGULAR file under `root`: its relative path (forward slashes, the
    spelling on disk) -> absolute path. Links, devices and the like are not files
    here and never followed. A missing `root` is an empty tree."""
    out: dict[str, str] = {}
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
            except OSError:
                continue
            out[os.path.relpath(path, root).replace(os.sep, "/")] = path
    return out


def _fold(path: str, fold: bool) -> str:
    return path.casefold() if fold else path


def _covered(entries: list[str], key: str) -> bool:
    return any(key.startswith(e) if e.endswith("/") else key == e for e in entries)


def _scoped(root: str, entries: list[str], fold: bool) -> dict[str, tuple[str, str]]:
    """Files under `root` covered by `entries` (already normalised and folded as
    needed): comparison key -> (spelling on disk, absolute path)."""
    out: dict[str, tuple[str, str]] = {}
    for rel, abs_path in tree_files(root).items():
        if has_bytecode_component(rel):
            # Never published (ADR 0032 §Amendment 2026-10-05), so never a change: an input
            # tree cloned from an older confirmed ref may still carry some.
            continue
        key = _fold(rel, fold)
        if _covered(entries, key):
            out[key] = (rel, abs_path)
    return out


def _same_bytes(a: str, b: str) -> bool:
    return filecmp.cmp(a, b, shallow=False)


def changed_files(
    *, input_dir: str, output_dir: str, entries: list[str], case_insensitive: bool | None = None
) -> list[str]:
    """Comparison keys of the files covered by `entries` that were ADDED, EDITED
    (byte-different) or DELETED between `input_dir` and `output_dir`. Sorted. These
    are paths the agent may have chosen: the caller counts them and nothing more."""
    fold = is_case_insensitive(output_dir) if case_insensitive is None else case_insensitive
    declared = [_fold(e, fold) for e in (normalise_entry(x) for x in entries) if e]
    before = _scoped(input_dir, declared, fold)
    after = _scoped(output_dir, declared, fold)
    changed = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before or key not in after or not _same_bytes(before[key][1], after[key][1]):
            changed.append(key)
    return changed


def refuse_schema_changes(
    *, input_dir: str, output_dir: str, entries: list[str], case_insensitive: bool | None = None
) -> None:
    """Raises `SchemaChangeRefused` when the output differs from the input anywhere
    under the schema `entries`; returns None when it does not. The caller decides
    whether the build type calls for the check at all (modify only)."""
    changed = changed_files(
        input_dir=input_dir, output_dir=output_dir, entries=entries, case_insensitive=case_insensitive
    )
    if changed:
        declared = [e for e in (normalise_entry(x) for x in entries) if e]
        raise SchemaChangeRefused(len(changed), declared)


def restore_protected_paths(
    *,
    workspace_dir: str,
    input_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    case_insensitive: bool | None = None,
) -> list[str]:
    """Makes `workspace_dir` (the tree to be published) carry the PINNED template
    copy of every protected path, whatever the agent did, and returns the declared
    entries on which the agent drifted.

    Drift is relative to the build's own INPUT tree (`input_dir`: the template's
    `app/` for a create build, the confirmed ref for a modify build), not to the
    template. For each entry, the files it covers in `workspace_dir` are compared
    with the ones it covers in `input_dir`: any file added, edited or deleted is
    drift. So a create build whose agent never touched the Dockerfile reports no
    drift (its input never contained one) and still publishes the pinned copy, and
    an input carrying an OLDER pinned file that the agent left alone reports none
    either.

    Restore is unconditional and does not look at the drift verdict: the covered
    files in `workspace_dir` are removed (including ones the agent added under a
    prefix) and the template's are copied in. An entry the template pins nothing for
    is skipped entirely (not this build's drift, nothing to restore from). The list
    holds the entries as declared, never an agent-chosen path."""
    fold = is_case_insensitive(workspace_dir) if case_insensitive is None else case_insensitive
    template_files = tree_files(template_skeleton_dir)
    drifted: list[str] = []

    for declared in protected_paths:
        entry = normalise_entry(declared)
        if not entry:
            continue
        key_entry, pinned = _pinned_for(entry, template_files, fold)
        if not pinned:
            continue  # nothing pinned to restore from -- not this build's drift

        if _entry_drifted(key_entry, pinned, input_dir, workspace_dir, fold):
            drifted.append(entry)

        after = _scoped(workspace_dir, key_entry, fold)
        for _spelling, abs_path in after.values():
            os.remove(abs_path)
        for rel, abs_path in pinned.items():
            _place(abs_path, workspace_dir, rel)

    return drifted


def protected_drift(
    *,
    workspace_dir: str,
    input_dir: str,
    template_skeleton_dir: str,
    protected_paths: list[str],
    case_insensitive: bool | None = None,
) -> list[str]:
    """The declared entries `restore_protected_paths` would report as drift for this
    `workspace_dir`, WITHOUT touching it. The same per-entry judgement, so the
    two cannot disagree on a single entry; the one difference is that restore judges
    an entry after restoring the ones declared before it, so two declared entries that
    overlap could differ. The default entries (exact files) do not overlap.

    Used where the drift must be known although nothing is restored: a build refused
    for a schema change never reaches restore, yet its approver and requester still
    need to see what the agent tried to change."""
    fold = is_case_insensitive(workspace_dir) if case_insensitive is None else case_insensitive
    template_files = tree_files(template_skeleton_dir)
    drifted: list[str] = []
    for declared in protected_paths:
        entry = normalise_entry(declared)
        if not entry:
            continue
        key_entry, pinned = _pinned_for(entry, template_files, fold)
        if pinned and _entry_drifted(key_entry, pinned, input_dir, workspace_dir, fold):
            drifted.append(entry)
    return drifted


def _pinned_for(
    entry: str, template_files: dict[str, str], fold: bool
) -> tuple[list[str], dict[str, str]]:
    """(the entry as a comparison key list, the template's files it covers)."""
    key_entry = [_fold(entry, fold)]
    pinned = {
        rel: abs_path
        for rel, abs_path in template_files.items()
        if _covered(key_entry, _fold(rel, fold))
    }
    return key_entry, pinned


def _entry_drifted(
    key_entry: list[str], pinned: dict[str, str], input_dir: str, workspace_dir: str, fold: bool
) -> bool:
    before = _scoped(input_dir, key_entry, fold)
    after = _scoped(workspace_dir, key_entry, fold)
    return (
        before.keys() != after.keys()
        or any(not _same_bytes(before[k][1], after[k][1]) for k in before)
        or any(_obstructed(workspace_dir, rel) for rel in pinned)
    )


def _obstructed(workspace_dir: str, rel: str) -> bool:
    """Something that is not a regular file stands where the pinned file `rel` goes:
    a directory at the file's own path, or a file (or link) where a directory is
    needed. Only an agent puts it there, so it is drift even though no covered
    regular file differs."""
    parts = rel.split("/")
    current = workspace_dir
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current) and (os.path.islink(current) or not os.path.isdir(current)):
            return True
    dest = os.path.join(workspace_dir, *parts)
    return os.path.lexists(dest) and (os.path.islink(dest) or not os.path.isfile(dest))


def _place(pinned_path: str, workspace_dir: str, rel: str) -> None:
    """Copies `pinned_path` to `workspace_dir/rel`, clearing whatever the agent put
    in the way: a file standing where a directory is needed, or a directory where
    the file goes."""
    parts = rel.split("/")
    current = workspace_dir
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current) and (os.path.islink(current) or not os.path.isdir(current)):
            os.remove(current)
    dest = os.path.join(workspace_dir, *parts)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isdir(dest) and not os.path.islink(dest):
        shutil.rmtree(dest)
    shutil.copyfile(pinned_path, dest)
