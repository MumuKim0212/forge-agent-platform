"""the harvest is bounded on its
own (`harvest.write_stream_bounded`, `harvest.extract_bounded`).

No Docker. The workspace archive is the Docker daemon's tar of the sandbox's `/workspace`,
and its bytes are the files' APPARENT sizes: a sparse file the sandbox made with
`truncate -s 200G` costs the sandbox nothing and arrives as 200 GiB. These tests are the
host-side half of that: the stream is cut at a byte bound without being held in memory, an
archive with too many entries or too many declared bytes is refused BEFORE extraction writes
anything, and what extraction would write -- sparse members, copied links -- is counted.
The runner-level behaviour (RESOURCE_LIMIT, nothing left behind) is in
`test_sandbox_runner_unit.py`; the real engine is `test_sandbox_runner_bounds_integration.py`.
"""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from app.services import harvest
from app.services.harvest import (
    HARVEST_MAX_ENTRIES,
    HarvestLimitExceeded,
    extract_bounded,
    write_stream_bounded,
)

MIB = 1024 * 1024


def _tar(path, members) -> None:
    """Writes a tar of `members`: (name, bytes) for a file, (name, ("symlink", target)) or
    (name, ("hardlink", target)) for a link."""
    with tarfile.open(path, "w") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if isinstance(content, tuple):
                kind, target = content
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = target
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))


def _sparse_member(path, name: str, real_size: int) -> None:
    """A PAX-1.0 sparse member: a one-block sparse map saying 'no data blocks', with
    `GNU.sparse.realsize` carrying the size the extracted file will have. `tarfile` cannot
    WRITE these, but it reads them, and the daemon's tar of a sparse file is why they matter."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(f"GNUSparseFile.0/{name}")
        info.pax_headers = {
            "GNU.sparse.major": "1",
            "GNU.sparse.minor": "0",
            "GNU.sparse.name": name,
            "GNU.sparse.realsize": str(real_size),
        }
        map_block = b"0\n".ljust(512, b"\0")  # zero map entries
        info.size = len(map_block)
        tar.addfile(info, io.BytesIO(map_block))


# --- write_stream_bounded ------------------------------------------------------------------


def test_a_stream_within_the_bound_is_written_in_full(tmp_path):
    target = tmp_path / "a.tar"

    written = write_stream_bounded(iter([b"a" * 10, b"b" * 10]), str(target), max_bytes=20)

    assert written == 20
    assert target.read_bytes() == b"a" * 10 + b"b" * 10


def test_a_stream_one_byte_over_the_bound_is_refused_and_never_written_past_it(tmp_path):
    target = tmp_path / "a.tar"

    with pytest.raises(HarvestLimitExceeded) as raised:
        write_stream_bounded(iter([b"a" * 10, b"b" * 10, b"c"]), str(target), max_bytes=20)

    assert raised.value.kind == "bytes" and raised.value.limit == 20
    assert target.stat().st_size <= 20  # the chunk that would cross the bound is not written


def test_the_stream_is_cut_at_the_bound_not_read_to_its_end(tmp_path):
    """The point of streaming: a huge source costs `bound / chunk + 1` reads, never more, and
    the host file stays inside the bound. A buffering implementation reads it all first -- here
    that is 100 MiB of the same chunk, which the pull count catches. (The source is FINITE on
    purpose: with the bound removed, this test must fail by counting, not by filling the disk.)"""
    pulled = []
    chunk = b"\0" * MIB

    def source():
        for _ in range(100):
            pulled.append(1)
            yield chunk

    with pytest.raises(HarvestLimitExceeded):
        write_stream_bounded(source(), str(tmp_path / "a.tar"), max_bytes=8 * MIB)

    assert len(pulled) <= 8 + 1
    assert (tmp_path / "a.tar").stat().st_size <= 8 * MIB


def test_the_source_is_closed_when_the_bound_is_hit(tmp_path):
    """A docker-py archive stream is a generator over an open HTTP response; closing it is
    what releases the connection instead of leaving it to the garbage collector."""
    state = {"closed": False, "pulled": 0}

    def source():
        try:
            for _ in range(100_000):  # finite (100 MiB): see the note on the test above
                state["pulled"] += 1
                yield b"x" * 1024
        finally:
            state["closed"] = True

    with pytest.raises(HarvestLimitExceeded):
        write_stream_bounded(source(), str(tmp_path / "a.tar"), max_bytes=4096)

    assert state["closed"] is True
    assert state["pulled"] <= 4096 // 1024 + 1  # closed because the bound was hit, not because the source ran out


def test_the_source_is_closed_on_a_normal_finish_too(tmp_path):
    state = {"closed": False}

    def source():
        try:
            yield b"x"
        finally:
            state["closed"] = True

    write_stream_bounded(source(), str(tmp_path / "a.tar"), max_bytes=10)

    assert state["closed"] is True


# --- extract_bounded -----------------------------------------------------------------------


def test_an_archive_within_both_bounds_is_extracted(tmp_path):
    archive = tmp_path / "ws.tar"
    _tar(archive, [("workspace/a.txt", b"hello"), ("workspace/d/b.txt", b"world")])
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=10, max_entries=10)

    assert (dest / "workspace" / "a.txt").read_bytes() == b"hello"
    assert (dest / "workspace" / "d" / "b.txt").read_bytes() == b"world"


def test_too_many_entries_is_refused_before_anything_is_extracted(tmp_path):
    archive = tmp_path / "ws.tar"
    _tar(archive, [(f"workspace/f{i}", b"") for i in range(11)])
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=MIB, max_entries=10)

    assert raised.value.kind == "entries" and raised.value.limit == 10
    assert os.listdir(dest) == []


def test_exactly_the_entry_ceiling_is_allowed(tmp_path):
    archive = tmp_path / "ws.tar"
    _tar(archive, [(f"workspace/f{i}", b"") for i in range(10)])
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=MIB, max_entries=10)

    assert len(os.listdir(dest / "workspace")) == 10


def test_declared_member_sizes_over_the_bound_are_refused_before_extraction(tmp_path):
    archive = tmp_path / "ws.tar"
    _tar(archive, [("workspace/a", b"x" * 600), ("workspace/b", b"x" * 600)])
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=1000, max_entries=10)

    assert raised.value.kind == "bytes"
    assert os.listdir(dest) == []


def test_a_sparse_member_is_counted_at_its_logical_size_not_its_stream_size(tmp_path):
    """The archive file is a few KB; extraction would write `realsize` bytes. `tarfile`
    reports a sparse member's real size, and that is what is counted -- so a small tar
    cannot carry a large extraction. (3 MiB here, not 3 GiB: the point is the comparison.)"""
    archive = tmp_path / "ws.tar"
    _sparse_member(archive, "workspace/big", 3 * MIB)
    assert archive.stat().st_size < MIB // 10  # positive control: the tar itself is tiny, far below what it extracts to
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=MIB, max_entries=10)

    assert raised.value.kind == "bytes"
    assert os.listdir(dest) == []


def test_a_sparse_member_within_the_bound_is_extracted_at_its_logical_size(tmp_path):
    """The non-vacuity half of the test above: the same archive, under a bound that admits it,
    extracts to a file of exactly the logical size -- so the count is what extraction writes."""
    archive = tmp_path / "ws.tar"
    _sparse_member(archive, "workspace/big", 3 * MIB)
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=4 * MIB, max_entries=10)

    assert (dest / "workspace" / "big").stat().st_size == 3 * MIB


@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
@pytest.mark.parametrize("link_sorts", ["before", "after"])
def test_links_the_host_would_copy_are_counted_at_their_targets_size(tmp_path, kind, link_sorts):
    """Where a link cannot be made (a symlink without the privilege on Windows, a hard
    link on a filesystem without them) `tarfile` extracts a COPY of the member it names. So
    a tar of one 600-byte file and two links to it can write 1800 bytes, and is counted that
    way -- whichever of the file and its links comes first in the archive."""
    target_name = "workspace/data" if kind == "hardlink" else "workspace/d/data"
    link_target = "workspace/data" if kind == "hardlink" else "data"  # a symlink is relative to its own directory
    file_member = (target_name, b"x" * 600)
    links = [(f"workspace/d/link{i}" if kind == "symlink" else f"workspace/link{i}", (kind, link_target)) for i in range(2)]
    members = [file_member, *links] if link_sorts == "after" else [*links, file_member]
    archive = tmp_path / "ws.tar"
    _tar(archive, members)
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=1500, max_entries=10)

    assert raised.value.kind == "bytes"
    assert os.listdir(dest) == []


def _can_symlink(tmp_path) -> bool:
    try:
        os.symlink(__file__, str(tmp_path / "probe-link"))
    except (OSError, NotImplementedError):
        return False
    return True


def test_extraction_really_does_copy_a_link_target_where_the_host_cannot_link(tmp_path):
    """Why the link accounting above is not paranoia: on a host that cannot create a symlink
    (this Windows dev host, without the privilege), extracting N symlinks to one file writes N
    COPIES of it -- more bytes than the archive's regular members add up to. On a host that can
    (Linux, production) the links are links and this test has nothing to show."""
    if _can_symlink(tmp_path):
        pytest.skip("this host creates real symlinks, so extraction copies nothing")
    archive = tmp_path / "ws.tar"
    _tar(
        archive,
        [("workspace/data", b"x" * 600), *[(f"workspace/link{i}", ("symlink", "data")) for i in range(3)]],
    )
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=10_000, max_entries=10)

    written = sum(os.path.getsize(dest / "workspace" / name) for name in os.listdir(dest / "workspace"))
    assert written == 4 * 600  # the file and three copies, from a tar whose only regular member is 600 bytes


def test_a_dangling_link_whose_copy_would_fit_is_not_refused(tmp_path):
    """The over-refusal guard for the link count: one 10-byte file and a link that names nothing (a copy of
    the largest file, 10 bytes, is the most it can cost) inside a 100-byte bound is extracted."""
    archive = tmp_path / "ws.tar"
    _tar(archive, [("workspace/a", b"x" * 10), ("workspace/dangling", ("symlink", "nowhere"))])
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=100, max_entries=10)  # no HarvestLimitExceeded

    assert (dest / "workspace" / "a").exists()


def test_the_data_filter_still_applies_to_what_is_within_the_bounds(tmp_path):
    """Bounds are added to the extraction, not substituted for the filter: a member that
    would land outside the destination is still refused."""
    archive = tmp_path / "ws.tar"
    _tar(archive, [("../escape.txt", b"x")])
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(tarfile.TarError):
        extract_bounded(str(archive), str(dest), max_bytes=MIB, max_entries=10)

    assert not (tmp_path / "escape.txt").exists()


def test_a_compressed_archive_is_not_inflated(tmp_path):
    archive = tmp_path / "ws.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("workspace/a")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(tarfile.ReadError):
        extract_bounded(str(archive), str(dest), max_bytes=MIB, max_entries=10)


# -- links are counted whatever they resolve to ------------------------------------------
#
# Archive-level, no filesystem needed to REFUSE: `extract_bounded` raises before it extracts anything, so
# these run the same on Linux CI (where links are real and nothing is copied) as on this host (where they
# are copied). What they pin is the count, which must not depend on how `tarfile` happens to resolve a link:
# the first version resolved by name, last-wins, one hop, and was wrong for a chain and for a repeated name.


def _chain_members(kind: str, hops: int):
    """One 600-byte file, `l0` linking to it, and `l1 .. l<hops>` each linking to `l0`."""
    if kind == "symlink":  # a symlink's target is relative to its own directory
        first, rest = "f", "l0"
    else:  # a hard link names an archive path
        first, rest = "workspace/f", "workspace/l0"
    members = [("workspace/f", b"x" * 600), ("workspace/l0", (kind, first))]
    members += [(f"workspace/l{i}", (kind, rest)) for i in range(1, hops + 1)]
    return members


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_chain_of_links_is_refused_when_its_copies_would_not_fit(tmp_path, kind):
    """The reviewer's case: a 600-byte file, `l0 -> file`, `l1` and `l2 -> l0`, a 1500-byte bound. A host that
    cannot link writes four copies of the file (2400 bytes); counting only links that name a regular member
    says 1200 and admits it."""
    archive = tmp_path / "ws.tar"
    _tar(archive, _chain_members(kind, hops=2))
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=1500, max_entries=100)

    assert raised.value.kind == "bytes"
    assert os.listdir(dest) == []  # refused before anything was extracted


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_long_chain_costs_a_copy_per_link(tmp_path, kind):
    """Ten links hanging off `l0`: twelve copies' worth against a bound of eleven, a chain the one-hop count
    would see as two."""
    archive = tmp_path / "ws.tar"
    _tar(archive, _chain_members(kind, hops=10))
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded):
        extract_bounded(str(archive), str(dest), max_bytes=11 * 600, max_entries=100)
    extract_bounded(str(archive), str(dest), max_bytes=12 * 600, max_entries=100)  # exactly the copies: admitted

    assert os.path.lexists(dest / "workspace" / "l10")


def test_a_hard_link_to_a_name_that_is_redefined_later_is_counted_at_the_earlier_member(tmp_path):
    """A hard link resolves to the nearest member of that name BEFORE it; a symlink to the LAST. A big file, three
    hard links to it, then a 1-byte file of the same name: the links copy the big one (1800 bytes), and a count
    keyed by name with the last member winning sees 1 byte per link."""
    archive = tmp_path / "ws.tar"
    _tar(
        archive,
        [
            ("workspace/f", b"x" * 600),
            *[(f"workspace/h{i}", ("hardlink", "workspace/f")) for i in (1, 2, 3)],
            ("workspace/f", b"y"),
        ],
    )
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded) as raised:
        extract_bounded(str(archive), str(dest), max_bytes=1500, max_entries=100)

    assert raised.value.kind == "bytes"
    assert os.listdir(dest) == []


def test_a_symlink_to_a_name_that_is_redefined_later_is_counted_too(tmp_path):
    """The other direction: here the symlinks resolve to the LAST member of the name (1 byte), so the real copies
    are small; the count is an upper bound and says 600 per link. Refused, and said to be the bound, not an exact
    figure, so a later change to the rule is a deliberate one."""
    archive = tmp_path / "ws.tar"
    _tar(
        archive,
        [
            ("workspace/f", b"x" * 600),
            *[(f"workspace/s{i}", ("symlink", "f")) for i in (1, 2, 3)],
            ("workspace/f", b"y"),
        ],
    )
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded):
        extract_bounded(str(archive), str(dest), max_bytes=1500, max_entries=100)


def test_a_link_that_resolves_to_nothing_in_the_archive_still_costs_the_largest_file(tmp_path):
    """Nothing here resolves the link, so a target the archive does not hold cannot make it free."""
    archive = tmp_path / "ws.tar"
    _tar(archive, [("workspace/a", b"x" * 600), *[(f"workspace/d{i}", ("symlink", "nowhere")) for i in range(3)]])
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(HarvestLimitExceeded):
        extract_bounded(str(archive), str(dest), max_bytes=1500, max_entries=100)


def test_an_archive_with_links_and_no_regular_file_costs_nothing(tmp_path):
    archive = tmp_path / "ws.tar"
    _tar(archive, [("workspace/l", ("symlink", "elsewhere")), ("workspace/m", ("symlink", "l"))])
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=0, max_entries=100)  # nothing to copy, nothing refused


def test_extraction_really_copies_through_a_chain_where_the_host_cannot_link(tmp_path):
    """Why the chain is counted: on a host without symlinks, `l1 -> l0 -> file` writes a copy of `file` for EVERY
    link, the chain included, from a tar whose only regular member is 600 bytes. (On a host that makes real
    symlinks, Linux, this test has nothing to show.)"""
    if _can_symlink(tmp_path):
        pytest.skip("this host creates real symlinks, so extraction copies nothing")
    archive = tmp_path / "ws.tar"
    _tar(archive, _chain_members("symlink", hops=2))
    dest = tmp_path / "out"
    dest.mkdir()

    extract_bounded(str(archive), str(dest), max_bytes=10_000, max_entries=100)

    written = sum(os.path.getsize(dest / "workspace" / name) for name in os.listdir(dest / "workspace"))
    assert written == 4 * 600  # the file, l0, l1 and l2: the one-hop count said 1200


# --- the constants --------------------------------------------------------------------------


def test_the_entry_ceiling_is_pinned_and_clears_the_real_template_with_a_wide_margin():
    """A literal, so changing it is a deliberate edit (the comment in harvest.py says why this
    number). The real template is 11 files and 4 directories (more on a working tree holding
    __pycache__); the ceiling is read against it, not against itself."""
    assert HARVEST_MAX_ENTRIES == 20_000
    template = os.path.join(os.path.dirname(os.path.dirname(harvest.__file__)), "..", "templates", "generic-webtool")
    template = os.path.normpath(template)
    entries = sum(len(dirs) + len(files) for _, dirs, files in os.walk(template))
    assert 0 < entries <= HARVEST_MAX_ENTRIES // 500


def test_the_limit_message_is_platform_text_naming_the_limit_only():
    assert str(HarvestLimitExceeded("bytes", 64 * MIB)) == "the build's output is larger than the 64 MiB limit"
    assert str(HarvestLimitExceeded("entries", 20_000)) == "the build's output has more than 20000 files and directories"
