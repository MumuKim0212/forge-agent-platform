"""The agent's change set (measured facts and agent claims).

What the agent added, modified or deleted, measured by the platform between a build's
INPUT tree (the staged directory: the template's `app/` for a create build, the
confirmed ref for a modify build) and the SANITISED copy of what the agent handed
back -- before protected-path restore, so an attempted change to a protected file is
part of it (`protected_paths.restore_protected_paths` then drops it from what is
published and reports it as drift).

Pure filesystem logic over two directories: no git, no Docker, no DB.

Not part of the change set, on either side:
  - compiled bytecode (never published, ADR 0032 §Amendment 2026-10-05; an input cloned
    from an older confirmed ref may still carry some);
  - `.git` in any spelling and the adapter's root `.forge/` (the sanitiser never copies
    them, and the staged input carries the latter).

The paths are chosen by the agent. They are data, not platform text: a consumer shows
them literally and bounded, and nothing here
interpolates one into a sentence. At most `MAX_CHANGED_FILES` entries are kept, in path
order; the totals are always the true ones.
"""

from __future__ import annotations

import filecmp
from dataclasses import dataclass, field

from app.services.harvest import has_bytecode_component, is_git_name
from app.services.protected_paths import is_case_insensitive, tree_files

MAX_CHANGED_FILES = 500

ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"
CHANGE_KINDS = (ADDED, MODIFIED, DELETED)


@dataclass(frozen=True)
class ChangeSet:
    """`entries`: at most `MAX_CHANGED_FILES` `{path, change}` dicts, sorted by path.
    `totals`: the true count per change kind (never truncated). `truncated`: True when
    more files changed than `entries` holds."""

    entries: list[dict[str, str]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=lambda: {k: 0 for k in CHANGE_KINDS})
    truncated: bool = False

    def to_json(self) -> dict:
        """The shape stored in `Build.changed_files`."""
        return {"entries": list(self.entries), "totals": dict(self.totals), "truncated": self.truncated}


def _measured(root: str, fold: bool) -> dict[str, tuple[str, str]]:
    """comparison key -> (spelling on disk, absolute path) for the files that can be
    part of a change set."""
    out: dict[str, tuple[str, str]] = {}
    for rel, abs_path in tree_files(root).items():
        parts = rel.split("/")
        if has_bytecode_component(rel) or parts[0] == ".forge" or any(is_git_name(p) for p in parts):
            continue
        out[rel.casefold() if fold else rel] = (rel, abs_path)
    return out


def changed_paths(
    *, input_dir: str, output_dir: str, case_insensitive: bool | None = None
) -> list[tuple[str, str, str | None, str | None]]:
    """`(path, change, input file, output file)` for every file ADDED to, EDITED
    (byte-different) in or DELETED from `input_dir` in producing `output_dir`, ordered
    by path. The file entries are absolute paths, None on the side that has no such
    file. This is THE comparison: `measure_changes` summarises it and the build trace's
    diff (`build_trace.compute_diff`) reads the same list, so the two can never disagree
    about which files changed."""
    fold = is_case_insensitive(output_dir) if case_insensitive is None else case_insensitive
    before = _measured(input_dir, fold)
    after = _measured(output_dir, fold)

    changes: list[tuple[str, str, str | None, str | None]] = []
    for key in before.keys() | after.keys():
        if key not in before:
            changes.append((after[key][0], ADDED, None, after[key][1]))
        elif key not in after:
            changes.append((before[key][0], DELETED, before[key][1], None))
        elif not filecmp.cmp(before[key][1], after[key][1], shallow=False):
            changes.append((after[key][0], MODIFIED, before[key][1], after[key][1]))
    changes.sort(key=lambda change: (change[0], change[1]))
    return changes


def measure_changes(
    *, input_dir: str, output_dir: str, case_insensitive: bool | None = None
) -> ChangeSet:
    """The files ADDED to, EDITED (byte-different) in or DELETED from `input_dir` in
    producing `output_dir`. Byte-identical files are not listed. Paths use forward
    slashes and the spelling on disk (the output's for an added or modified file, the
    input's for a deleted one); on a case-insensitive filesystem two spellings of one
    name are one file, as in `protected_paths`. Ordered by path, so the result is
    deterministic."""
    changes = [
        (path, change)
        for path, change, _before, _after in changed_paths(
            input_dir=input_dir, output_dir=output_dir, case_insensitive=case_insensitive
        )
    ]

    totals = {k: 0 for k in CHANGE_KINDS}
    for _path, change in changes:
        totals[change] += 1
    return ChangeSet(
        entries=[{"path": path, "change": change} for path, change in changes[:MAX_CHANGED_FILES]],
        totals=totals,
        truncated=len(changes) > MAX_CHANGED_FILES,
    )
