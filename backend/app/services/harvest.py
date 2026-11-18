"""The sanitising copy for an agent's harvested output (ADR 0032).

`SandboxRunner._harvest_workspace` extracts the sandbox's `/workspace` into a
fresh `forge-harvest-*` directory. Those bytes were authored in L3, so they
are untrusted input to everything on the host that would touch them: `git`
(a `.git/hooks/pre-commit`, a `.git/config` with `core.fsmonitor`), `docker
build` (a symlink in the build context) and the policy checks (a symlink a
check would open). Host tooling therefore never runs inside the extracted
tree. `sanitise_harvest` copies it into a fresh, host-created directory by an
allowlist, and that copy is what gets published, assembled into an image and
checked by the policy gate.

What it does with each entry (ADR 0032 §Decision 2):
  - regular file: copied, content and mode bits (never ownership);
  - directory: recreated (its mode is not copied);
  - an entry named `.git` AS A FILESYSTEM SEES THE NAME (`is_git_name`: any
    case, trailing dots and spaces, NTFS short name `GIT~1`, an NTFS stream
    suffix), a file (the gitlink form) or a directory, at any depth: skipped,
    never read, never descended into. This is defence in depth; publish does
    not depend on it (`publish.init_platform_repo`: the platform's repository
    lives outside the published tree and is never discovered from it);
  - compiled bytecode (`is_bytecode_name`: any `__pycache__`, any `*.pyc` / `*.pyo`,
    at any depth, casefolded): skipped silently with a logged count, never read or
    descended into (ADR 0032 §Amendment 2026-10-05). Create-build staging
    excludes it from the skeleton copy for the same reason;
  - `.forge` at the root: skipped. It is the platform's own channel into and
    out of the sandbox (`task_input.json`, `agent_runner.py`, `result.json`).
    A `.forge` anywhere else is the agent's ordinary directory and is copied;
  - anything else (symbolic link, hard link, device, FIFO, socket): the whole
    build is refused with `UnsafeHarvestEntry`. The tree is scanned in full
    before anything is copied, and an entry that is not a regular file or a
    directory is never opened, listed or followed -- classification is by
    `lstat` alone.

What reaches this function. Measured on the dev host (Windows, Python 3.14)
for the `tarfile` `data` filter that the runner extracts with: a symlink
or hard link that points outside the extraction root, and a device or FIFO, is
refused by the filter itself, so the runner's harvest fails first (a
`HARVEST_ERROR` with no directory). An in-tree hard link arrives as two
regular files with `st_nlink == 2`. An in-tree symlink arrives as a regular
file copy on Windows (the host has no symlink privilege, so `tarfile` copies
the target) but as a real symlink on Linux. So the function must refuse what
the filter lets through -- in-tree links -- and it refuses every kind anyway:
the runner's filter is a different layer, and this function does not depend on
it.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
import tarfile
from collections.abc import Callable, Iterable

logger = logging.getLogger("app.harvest")

# The refusal kinds, in the order they are reported. Fixed vocabulary: the
# executor puts these words (and counts) into a build's failure detail, so
# they are platform text and never derived from an entry's name.
SYMLINK = "symbolic link"
HARDLINK = "hard link"
DEVICE = "device file"
FIFO = "FIFO"
SOCKET = "socket"
OTHER = "special file"
_KINDS = (SYMLINK, HARDLINK, DEVICE, FIFO, SOCKET, OTHER)


class UnsafeHarvestEntry(Exception):
    """The harvested tree holds entries that are neither regular files nor
    directories. `counts` maps each kind found to how many. The message names
    kinds and counts only -- never a path, which is L3-authored text."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = {kind: counts[kind] for kind in _KINDS if counts.get(kind)}
        super().__init__(
            "the harvested output contains entries that cannot be published: "
            + describe_counts(self.counts)
        )


def describe_counts(counts: dict[str, int]) -> str:
    """e.g. `2 symbolic link(s), 1 FIFO(s)`. Platform vocabulary and integers."""
    return ", ".join(f"{counts[kind]} {kind}(s)" for kind in _KINDS if counts.get(kind))


def is_git_name(name: str) -> bool:
    """True if `name` is, to a filesystem that might hold it, a `.git` entry.

    `.GIT`/`.Git` are `.git` on NTFS, APFS and other case-insensitive
    filesystems; Windows strips trailing dots and spaces from a name (so `.git.`
    and `.git ` are `.git`); NTFS also knows a directory `.git` as `GIT~1`, and a
    `:` introduces an alternate-data-stream suffix (`.git::$INDEX_ALLOCATION`
    names the directory itself). On a case-sensitive filesystem this over-skips
    `.GIT`, which is harmless: a generated web tool has no use for either."""
    base = name.split(":", 1)[0].rstrip(" .").casefold()
    return base in (".git", "git~1")


def is_bytecode_name(name: str) -> bool:
    """True if `name` is compiled Python bytecode as a filesystem might hold it: a
    `__pycache__` entry or a `*.pyc` / `*.pyo` entry, case-folded and with the
    trailing dots and spaces Windows strips (ADR 0032 §Amendment 2026-10-05). A
    source-less `.pyc` is importable in place of the source it claims to be, so
    bytecode is never published; the runtime recompiles from source."""
    base = name.split(":", 1)[0].rstrip(" .").casefold()
    return base == "__pycache__" or base.endswith((".pyc", ".pyo"))


def has_bytecode_component(rel_path: str) -> bool:
    """True if any component of the `/`- or `os.sep`-separated relative path is
    bytecode by `is_bytecode_name`."""
    return any(is_bytecode_name(part) for part in rel_path.replace("\\", "/").split("/") if part)


def _unsafe_kind(st: os.stat_result) -> str | None:
    """The refusal kind for an `lstat` result, or None for a regular file with
    one link or a directory."""
    mode = st.st_mode
    # A Windows directory junction or other reparse point reads as a directory
    # (or file) to `S_ISDIR`/`S_ISREG`, and is followed by the OS when opened.
    attributes = getattr(st, "st_file_attributes", None) or 0  # Windows only; None if synthesised
    if stat.S_ISLNK(mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return SYMLINK
    if stat.S_ISREG(mode):
        return HARDLINK if st.st_nlink > 1 else None
    if stat.S_ISDIR(mode):
        return None
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return DEVICE
    if stat.S_ISFIFO(mode):
        return FIFO
    if stat.S_ISSOCK(mode):
        return SOCKET
    return OTHER


def sanitise_harvest(
    src: str,
    dst: str,
    *,
    lstat: Callable[[str], os.stat_result] = os.lstat,
) -> None:
    """Copies the harvested tree `src` into `dst` by the rules in the module
    docstring. `dst` must already exist (the caller creates it with
    `tempfile.mkdtemp`) and be empty.

    Raises `UnsafeHarvestEntry` -- before copying anything -- if any entry is
    not a regular file or directory. Other `OSError`s propagate.

    `lstat` is a seam so the link cases can be driven on a host that cannot
    create symlinks (Windows without the privilege); production never passes
    it. It is called once per entry, on the entry itself.
    """
    dirs: list[str] = []
    files: list[str] = []
    unsafe: dict[str, int] = {}

    # Iterative, not recursive: a hostile tree can be deeper than the
    # interpreter's recursion limit. Never `os.walk(followlinks=True)`, and an
    # entry is only listed (`os.listdir`) once `lstat` has said it is a real
    # directory.
    pending = [""]
    skipped_bytecode = 0
    while pending:
        rel_dir = pending.pop()
        for name in sorted(os.listdir(os.path.join(src, rel_dir) if rel_dir else src)):
            if is_git_name(name):
                continue
            if is_bytecode_name(name):
                skipped_bytecode += 1
                continue
            if not rel_dir and name == ".forge":
                continue
            rel = os.path.join(rel_dir, name)
            st = lstat(os.path.join(src, rel))
            kind = _unsafe_kind(st)
            if kind is not None:
                unsafe[kind] = unsafe.get(kind, 0) + 1
            elif stat.S_ISDIR(st.st_mode):
                dirs.append(rel)
                pending.append(rel)
            else:
                files.append(rel)

    if skipped_bytecode:
        logger.info("harvest: %d compiled-bytecode entr(ies) were not copied", skipped_bytecode)

    if unsafe:
        raise UnsafeHarvestEntry(unsafe)

    # `dirs` lists a directory before anything inside it (a parent is appended
    # before it is popped for listing).
    for rel in dirs:
        os.mkdir(os.path.join(dst, rel))
    for rel in files:
        target = os.path.join(dst, rel)
        shutil.copyfile(os.path.join(src, rel), target)
        shutil.copymode(os.path.join(src, rel), target)


def remove_tree(path: str | None, *, what: str) -> None:
    """Best-effort removal of one of the build's temporary directories. Never
    raises; logs (path and `what`, no file names) if anything is left behind.

    Clears a read-only bit and retries once: Windows will not delete a
    read-only file, and an ignored failure here is a leaked directory.
    """
    if path is None:
        return
    failures: list[str] = []

    def _retry(func, failed_path, _exc) -> None:
        try:
            os.chmod(failed_path, 0o700)
            func(failed_path)
        except OSError as exc:
            failures.append(type(exc).__name__)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:  # pragma: no cover - `onexc` is 3.12+, and this runs on 3.14
            shutil.rmtree(path, onerror=_retry)
    except OSError as exc:
        failures.append(type(exc).__name__)
    if os.path.lexists(path):
        logger.warning(
            "could not remove the %s directory %s (%s)",
            what,
            path,
            ", ".join(sorted(set(failures))) or "still present",
        )


# -- the bounded archive --------------------------------------------
#
# The workspace comes out of the sandbox as a tar stream the Docker daemon
# builds by reading the files. A file's apparent size is what the stream
# carries, so a sparse file (`truncate -s 200G`), which costs the sandbox
# nothing, becomes 200 GiB of zeros in the stream. Reading that stream into
# memory, or extracting it unchecked, lets one build take the worker (the only
# one) down with it. The harvest therefore has two bounds of its own, whatever
# the sandbox's disk cap does: a byte limit on the stream, and an entry-count
# ceiling. Crossing either is `HarvestLimitExceeded`, which the runner reports
# as `RESOURCE_LIMIT`.

# The entry ceiling. The generic-webtool template is 13 files and 7 directories
# (27 KB); a generated service of real size is a few hundred entries. 20,000 is
# about a thousand times the template and fifty to a hundred times a large
# service, so no legitimate build comes near it, while a million-file tree
# (which costs a filesystem its inodes long before it costs bytes) is stopped
# early. A constant, not a setting: nothing here is worth an operator's tuning.
HARVEST_MAX_ENTRIES = 20_000

_MIB = 1024 * 1024


class HarvestLimitExceeded(Exception):
    """The workspace archive crossed a harvest bound. `kind` is `"bytes"` or
    `"entries"` and `limit` the bound it crossed. The message is platform-authored
    (a limit and nothing the sandbox wrote), so it may be shown as a failure
    detail."""

    def __init__(self, kind: str, limit: int) -> None:
        self.kind = kind
        self.limit = limit
        if kind == "bytes":
            message = f"the build's output is larger than the {limit / _MIB:g} MiB limit"
        else:
            message = f"the build's output has more than {limit} files and directories"
        super().__init__(message)


def write_stream_bounded(chunks: Iterable[bytes], path: str, *, max_bytes: int) -> int:
    """Writes `chunks` to the file `path`, never holding more than one chunk in
    memory, and returns the number of bytes written. Raises `HarvestLimitExceeded`
    ("bytes") as soon as the running total would pass `max_bytes`: the chunk that
    would cross it is not written, so the file never exceeds the bound, and the
    iterator is closed (a docker-py archive stream is a generator over an open HTTP
    response, which this releases instead of leaving it to the garbage collector).
    The partial file is the caller's to remove -- it is in the caller's temp tree."""
    written = 0
    try:
        with open(path, "wb") as out:
            for chunk in chunks:
                written += len(chunk)
                if written > max_bytes:
                    raise HarvestLimitExceeded("bytes", max_bytes)
                out.write(chunk)
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()
    return written


def extract_bounded(archive_path: str, dest: str, *, max_bytes: int, max_entries: int) -> None:
    """Extracts the tar at `archive_path` into `dest` with the `data` filter, but
    only after a first pass over its headers has shown that extraction cannot write
    more than `max_bytes` or create more than `max_entries` entries. The first pass
    reads no member data (headers only), counts every entry, and sums each regular
    member's LOGICAL size -- `tarfile` reports a sparse member's real size, which
    is what extraction then writes -- plus, for every link member, the size of the
    LARGEST regular member in the archive. Raises `HarvestLimitExceeded` before
    anything is written. Uncompressed tars only: a compressed archive is refused by
    `tarfile` (`ReadError`) rather than inflated.

    **Why every link costs the largest file, and not its own target's size.** Where
    the host cannot make a link (a symlink without the privilege on Windows, a hard
    link on a filesystem that has none) `tarfile` extracts a COPY of the member the
    link resolves to, and the `data` filter does not stop that: it checks that a
    link's target stays inside the destination, not that it names a regular file. So
    a link can name another link (`l1 -> l0 -> file`: each link in the chain is
    another copy of `file`), and a name can occur twice in one archive, where a hard
    link resolves to the nearest EARLIER member of that name and a symlink to the
    LAST one. Counting each link at its own resolved target means reproducing those
    rules exactly, and a count that differs from `tarfile`'s by one rule is a hole
    (the first version of this function resolved by name, last-wins, and stopped at
    one hop: a chain of N links wrote N+2 copies and counted two). The largest
    regular member bounds the copy a link can make, whatever it resolves to and
    however the archive names things, so this is an upper bound that does not depend
    on `tarfile`'s resolution. It costs a legitimate build nothing: the sanitising
    copy (`sanitise_harvest`) refuses every link as `HARVEST_ERROR` anyway, so the
    only archives it can turn into `RESOURCE_LIMIT` are ones with links in them whose
    copies would not fit the cap.

    The `data` filter is what keeps a member from landing outside `dest` or
    carrying a device, FIFO or out-of-tree link; this function adds the one thing
    it does not do, a bound on how much it writes."""
    with tarfile.open(archive_path, mode="r:") as tar:
        entries = 0
        declared = 0
        largest = 0  # the biggest regular member's logical size: what one copied link can cost
        links = 0
        for member in tar:
            entries += 1
            if entries > max_entries:
                raise HarvestLimitExceeded("entries", max_entries)
            if member.isreg():
                size = max(member.size, 0)
                largest = max(largest, size)
                declared += size
                if declared > max_bytes:
                    raise HarvestLimitExceeded("bytes", max_bytes)
            elif member.islnk() or member.issym():
                links += 1
        declared += links * largest
        if declared > max_bytes:
            raise HarvestLimitExceeded("bytes", max_bytes)
        tar.extractall(dest, filter="data")
