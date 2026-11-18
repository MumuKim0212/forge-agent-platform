"""Gate: no git-tracked text file holds a raw invisible or direction-changing character.

Why a guard: the platform's whole defence against a file name or a summary that reads as
something it is not is to make these
characters VISIBLE. A source file that holds one raw is the same hazard pointed at a
reviewer -- the line renders reordered, or a name shows a gap that is not there -- and it has
happened more than once in this repository, each time by the escape text of an editing tool
being turned into the real character (a four-hex-digit u-escape passed through the file-write
tool or a command line arrives as the character itself). So a raw one fails here, naming
file:line and code point. Write such a character as a braced escape, or build it from its
code point (`chr()`, `String.fromCodePoint`), never as itself.

The set is the one the frontend marks (`frontend/src/components/ui/visibleControls.ts`):
the bidirectional controls, the C0 and C1 controls and DEL other than tab and newline, the
zero-width and other default-ignorable characters, the tag block, and the few more groups
that render as nothing. `test_the_set_is_the_one_the_frontend_marks` reads that file and
compares, so the two lists cannot drift apart unnoticed. Two rules mirror the frontend's
too, and `test_the_zero_width_joiner...` / `test_a_crlf_pair...` pin them:

* a CR+LF pair is a line ending (a Windows checkout writes them), a lone CR is flagged;
* U+200D between two emoji is the glue of one emoji (a family, a skin tone) and is left
  alone, anywhere else it is flagged.

U+0000 is in the set so that it equals the frontend's, but a file holding a NUL byte is
binary and is not scanned at all, so the guard cannot see it.

Exceptions are an explicit allowlist of (path, code point) pairs with a reason each. It is
empty: nothing needs one today. A pair that no longer occurs in its file fails too, so the
list can only shrink to what is real.

Every character below is built from its code point: this file must not hold one.

The repo-wide test needs a git checkout (it asks git for the tracked files). A container
that holds only `backend/` is not one, and the test then SKIPS with that reason rather
than failing -- `parents[2]` of this file would be some unrelated directory there.

On CI that skip would be a silent hole: the guard would stop guarding and everything stay
green. The backend job in `.github/workflows/ci.yml` therefore sets `FORGE_REQUIRE_REPO_GUARD=1`,
and with it present a "no checkout" outcome is a FAILURE, not a skip. Without it (a developer's
machine, a backend-only container) the behaviour is unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
from bisect import bisect_right
from collections.abc import Mapping
from pathlib import Path

import pytest

# --- the set -----------------------------------------------------------------------------

# (first, last), inclusive, grouped the way the spec words them. `test_the_set_is_the_one_the_frontend_marks`
# compares the result with the frontend's, and `PINNED_RANGES` below pins it again as literals.
INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0008), (0x000B, 0x001F),  # C0 controls, but not tab (09) or newline (0A); CR (0D) see `_findings_in_file`
    (0x007F, 0x009F),  # DEL and the C1 controls
    (0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069),  # bidirectional controls
    (0x200B, 0x200D), (0x2060, 0x2064), (0xFEFF, 0xFEFF), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x180E, 0x180E), (0xE0000, 0xE007F),  # zero-width and other default-ignorable characters, the tag block
    (0x2065, 0x2065), (0x206A, 0x206F),  # the rest of the invisible-formatting block
    (0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0),  # the Hangul fillers: blank Korean letters
)
INVISIBLE = frozenset(cp for first, last in INVISIBLE_RANGES for cp in range(first, last + 1))
# Built from code points, never from raw characters: `\U` + 8 hex digits is the re module's own escape.
INVISIBLE_PATTERN = re.compile("[" + "".join(f"\\U{first:08x}-\\U{last:08x}" for first, last in INVISIBLE_RANGES) + "]")

ZERO_WIDTH_JOINER = 0x200D
# What may sit between an emoji and the joiner: the emoji presentation selector or a skin-tone modifier.
EMOJI_MODIFIERS = frozenset([0xFE0F, *range(0x1F3FB, 0x1F400)])
# Unicode's Extended_Pictographic (emoji-data.txt), as ranges. Python has no property for it.
# Generated from Node 22's `/\p{Extended_Pictographic}/u` (Unicode 16.0), not typed from memory.
# It can lag the browser's by the emoji of a newer Unicode release: a ZWJ sequence made of those
# would then be FLAGGED (it fails closed) until a range is added here.
EXTENDED_PICTOGRAPHIC: tuple[tuple[int, int], ...] = (
    (0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0),
    (0x25FB, 0x25FE), (0x2600, 0x2605), (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734),
    (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F), (0x1F12F, 0x1F12F),
    (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5),
    (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F3FA), (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F),
    (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)
_PICTOGRAPHIC_FIRSTS = [first for first, _ in EXTENDED_PICTOGRAPHIC]

_THIS_FILE = Path(__file__).resolve()

# Set by the backend job of `.github/workflows/ci.yml`: "this run must have a checkout".
REQUIRE_ENV = "FORGE_REQUIRE_REPO_GUARD"

# The one place the frontend's lists are read from.
FRONTEND_SOURCE = "frontend/src/components/ui/visibleControls.ts"

# (path relative to the repository root, code point) -> why this file may hold it. Empty on purpose.
ALLOWED: Mapping[tuple[str, int], str] = {}


def skip_or_fail(reason: str, environ: Mapping[str, str] | None = None) -> None:
    """The guard's "cannot look" outcome: a skip with `reason`, unless `REQUIRE_ENV` is set
    (non-empty and not "0"), in which case it is a failure carrying the same reason.
    `environ` is the seam; the default is the process environment."""
    env = os.environ if environ is None else environ
    if env.get(REQUIRE_ENV, "") not in ("", "0"):
        pytest.fail(f"{REQUIRE_ENV} is set, so this must not skip: {reason}", pytrace=False)
    pytest.skip(reason)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def tracked_files(cwd: Path) -> tuple[Path, list[str]] | None:
    """(repo root, tracked file names relative to it) for the git checkout holding `cwd`,
    or None when `cwd` is not in one (or git is not installed)."""
    try:
        top = _git(["rev-parse", "--show-toplevel"], cwd)
    except OSError:
        return None
    if top.returncode != 0:
        return None
    root = Path(top.stdout.decode("utf-8", errors="replace").strip())
    listed = _git(["ls-files", "-z"], root)
    if listed.returncode != 0:
        return None
    names = [n.decode("utf-8", errors="replace") for n in listed.stdout.split(b"\0") if n]
    return root, names


# --- the scan -----------------------------------------------------------------------------


def is_pictographic(ch: str) -> bool:
    cp = ord(ch)
    i = bisect_right(_PICTOGRAPHIC_FIRSTS, cp) - 1
    return i >= 0 and cp <= EXTENDED_PICTOGRAPHIC[i][1]


def zwj_joins_emoji(line: str, at: int) -> bool:
    """True for the ZWJ at `line[at]` when it sits between two emoji: an emoji (with at most one
    emoji presentation selector or skin-tone modifier after it) right before, an emoji right
    after. The frontend's rule exactly, so a defined-sequence check is not made here either."""
    if at < 1 or at + 1 >= len(line) or not is_pictographic(line[at + 1]):
        return False
    before = line[at - 1]
    if is_pictographic(before):
        return True
    return ord(before) in EMOJI_MODIFIERS and at >= 2 and is_pictographic(line[at - 2])


def _findings_in_file(name: str, text: str, allowed: Mapping[tuple[str, int], str], seen: set[tuple[str, int]]) -> list[str]:
    findings: list[str] = []
    # CR+LF is a line break, not two characters; a CR on its own stays and is flagged.
    for lineno, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        for match in INVISIBLE_PATTERN.finditer(line):
            cp = ord(match.group())
            if cp == ZERO_WIDTH_JOINER and zwj_joins_emoji(line, match.start()):
                continue
            if (name, cp) in allowed:
                seen.add((name, cp))
                continue
            findings.append(f"{name}:{lineno}: U+{cp:04X}")
    return findings


def raw_invisible_findings(root: Path, names: list[str], allowed: Mapping[tuple[str, int], str] = ALLOWED) -> list[str]:
    """`file:line: U+XXXX` for every raw invisible character in the text files among `names`
    (relative to `root`), and a line for each allowlist entry that matched nothing. A binary file
    (a NUL byte, or not UTF-8) is skipped, and so is a tracked name that is not a regular file on
    disk (deleted, a symlink, a submodule). Lines are split on LF only, so the number is the one
    an editor shows."""
    findings: list[str] = []
    seen: set[tuple[str, int]] = set()
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_findings_in_file(name, text, allowed, seen))
    for name, cp in sorted(allowed):
        if (name, cp) not in seen:
            findings.append(f"{name}: U+{cp:04X} is allowlisted but is not in the file (remove the entry)")
    return findings


# --- the guard ---------------------------------------------------------------------------


def repo_checkout(start: Path, environ: Mapping[str, str] | None = None) -> tuple[Path, list[str]]:
    """(root, tracked names) of the repository holding `start`, or a skip (a failure when
    `REQUIRE_ENV` is set) when there is no such repository to look at."""
    found = tracked_files(start)
    if found is None:
        skip_or_fail("not inside a git checkout (a backend-only container): there is no tracked-file list to scan", environ)
    root, names = found
    if not (root / "backend" / "tests" / _THIS_FILE.name).is_file():
        skip_or_fail(
            f"the git checkout at {root} is not this repository's root: backend/tests/{_THIS_FILE.name} is not in it",
            environ,
        )
    assert names, "git reported no tracked files: the scan would pass on nothing"
    return root, names


def repo_findings(start: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """The guard itself: the raw-invisible-character findings in the repository holding `start`,
    or a skip (a failure when `REQUIRE_ENV` is set) when there is no such repository to look at."""
    root, names = repo_checkout(start, environ)
    return raw_invisible_findings(root, names)


def test_no_tracked_text_file_holds_a_raw_invisible_character():
    findings = repo_findings(_THIS_FILE.parent)

    assert findings == [], (
        "raw invisible or direction-changing characters in tracked files (write them as escapes, "
        "or build them from their code point):\n  " + "\n  ".join(findings)
    )


def test_every_allowlist_entry_says_why():
    for (name, cp), reason in ALLOWED.items():
        assert name and cp in INVISIBLE, (name, cp)
        assert len(reason.strip()) >= 20, f"{name} U+{cp:04X}: an exception needs a real reason"


# --- the frontend's lists, and this one --------------------------------------------------

_ESC = r"\\u(?:\{([0-9A-Fa-f]+)\}|([0-9A-Fa-f]{4}))"
_CLASS_ITEM = re.compile(rf"{_ESC}(?:-{_ESC})?")


def class_code_points(body: str) -> set[int]:
    """The code points a regex character class spells with braced or four-digit u-escapes and
    ranges of them. Anything else in the class (a raw character, a shorthand) is an error: the
    comparison must not quietly read less than the frontend holds."""
    points: set[int] = set()
    pos = 0
    while pos < len(body):
        item = _CLASS_ITEM.match(body, pos)
        assert item, f"cannot read the character class at {body[pos:pos + 12]!r}: teach class_code_points about it"
        first = int(item.group(1) or item.group(2), 16)
        last = int(item.group(3) or item.group(4), 16) if (item.group(3) or item.group(4)) else first
        points.update(range(first, last + 1))
        pos = item.end()
    return points


def frontend_rules(source: str) -> tuple[set[int], int, set[int]]:
    """(the marked set, the ZWJ, the characters allowed between an emoji and its ZWJ) read from the
    source of `visibleControls.ts`. A shape this does not recognise fails with a message naming
    what to update: a silent partial read is the failure to avoid."""
    shapes = {
        "const INVISIBLE": r"const INVISIBLE =\s*/\[([^\]]*)\]/gu;",
        "const ZERO_WIDTH_JOINER": r'const ZERO_WIDTH_JOINER = "\\u\{([0-9A-Fa-f]+)\}";',
        "const EMOJI_ENDING": r"const EMOJI_ENDING = /\\p\{Extended_Pictographic\}\[([^\]]*)\]\?\$/u;",
        "const EMOJI_STARTING": r"const EMOJI_STARTING = /\^\\p\{Extended_Pictographic\}/u;",
    }
    found = {}
    for label, pattern in shapes.items():
        match = re.search(pattern, source)
        assert match, f"{FRONTEND_SOURCE} no longer has `{label}` in the shape this test reads: update the parser and the guard's rule together"
        found[label] = match
    return (
        class_code_points(found["const INVISIBLE"].group(1)),
        int(found["const ZERO_WIDTH_JOINER"].group(1), 16),
        class_code_points(found["const EMOJI_ENDING"].group(1)),
    )


def describe_drift(frontend: set[int], backend: set[int]) -> list[str]:
    def spans(points: set[int]) -> str:
        ordered = sorted(points)
        runs: list[list[int]] = []
        for cp in ordered:
            if runs and cp == runs[-1][1] + 1:
                runs[-1][1] = cp
            else:
                runs.append([cp, cp])
        return ", ".join(f"U+{a:04X}" if a == b else f"U+{a:04X}-U+{b:04X}" for a, b in runs)

    lines = []
    if frontend - backend:
        lines.append(f"marked by the frontend but not flagged here: {spans(frontend - backend)}")
    if backend - frontend:
        lines.append(f"flagged here but not marked by the frontend: {spans(backend - frontend)}")
    return lines


def test_the_set_is_the_one_the_frontend_marks():
    root, _ = repo_checkout(_THIS_FILE.parent)
    source = root / FRONTEND_SOURCE
    if not source.is_file():
        skip_or_fail(f"{FRONTEND_SOURCE} is not in this checkout, so the two lists cannot be compared")

    marked, joiner, modifiers = frontend_rules(source.read_text(encoding="utf-8"))

    assert describe_drift(marked, set(INVISIBLE)) == [], (
        "the guard's set and the frontend's visibleControls set have drifted: change both together"
    )
    assert joiner == ZERO_WIDTH_JOINER
    assert modifiers == EMOJI_MODIFIERS


_BS = chr(92)


def _esc(cp: int) -> str:
    """The braced escape text for `cp` (backslash, u, {hex}), as the frontend writes most of its set."""
    return f"{_BS}u{{{cp:X}}}"


def _esc4(cp: int) -> str:
    """The four-digit escape text for `cp`, as the frontend writes the C0 and C1 ranges."""
    return f"{_BS}u{cp:04X}"


def test_the_drift_comparison_reads_a_source_and_goes_red_when_either_side_moves():
    """The parser on a hand-built source in the frontend's shape; and the comparison on a set
    that lost the tag block and one that gained a character."""
    source = "\n".join(
        [
            "const INVISIBLE =",
            "  /[" + _esc4(0) + "-" + _esc4(8) + _esc(0xAD) + _esc(0xE0000) + "-" + _esc(0xE007F) + "]/gu;",
            'const ZERO_WIDTH_JOINER = "' + _esc(0x200D) + '";',
            f"const EMOJI_ENDING = /{_BS}p{{Extended_Pictographic}}[" + _esc(0xFE0F) + _esc(0x1F3FB) + "-" + _esc(0x1F3FF) + "]?$/u;",
            f"const EMOJI_STARTING = /^{_BS}p{{Extended_Pictographic}}/u;",
        ]
    )

    marked, joiner, modifiers = frontend_rules(source)

    assert marked == {*range(0, 9), 0xAD, *range(0xE0000, 0xE0080)}
    assert joiner == 0x200D
    assert modifiers == {0xFE0F, *range(0x1F3FB, 0x1F400)}
    assert describe_drift(marked, marked) == []
    lost_tags = describe_drift(marked, marked - set(range(0xE0000, 0xE0080)))
    assert lost_tags == ["marked by the frontend but not flagged here: U+E0000-U+E007F"]
    gained = describe_drift(marked, marked | {0x2028})
    assert gained == ["flagged here but not marked by the frontend: U+2028"]


@pytest.mark.parametrize(
    "body",
    [
        _esc4(0) + "-" + _esc4(8) + "a",  # a raw letter in the class
        _esc4(0) + "-" + _esc4(8) + _BS + "s",  # a shorthand
        _BS + "x00-" + _BS + "x08",  # an escape form this parser does not read
    ],
)
def test_a_character_class_the_parser_cannot_read_is_an_error_not_a_smaller_set(body):
    with pytest.raises(AssertionError, match="cannot read the character class"):
        class_code_points(body)


def test_a_source_that_lost_one_of_the_rules_is_an_error():
    source = "\n".join(
        [
            "const INVISIBLE = /[" + _esc(0xAD) + "]/gu;",
            'const ZERO_WIDTH_JOINER = "' + _esc(0x200D) + '";',
            f"const EMOJI_ENDING = /{_BS}p{{Extended_Pictographic}}[" + _esc(0xFE0F) + "]?$/u;",
        ]
    )

    with pytest.raises(AssertionError, match="no longer has `const EMOJI_STARTING`"):
        frontend_rules(source)


# --- the set, pinned as literals -----------------------------------------------------------
# One independent statement of the set, as maximal ranges, so that it is pinned even where the
# frontend file is not there to compare with. Its neighbours (first - 1, last + 1) are outside.

PINNED_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x061C, 0x061C), (0x115F, 0x1160), (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xE0000, 0xE007F),
)


def test_the_set_is_exactly_the_pinned_ranges():
    assert INVISIBLE == {cp for first, last in PINNED_RANGES for cp in range(first, last + 1)}
    assert len(INVISIBLE) == 226


def _flagged(tmp_path, cp: int) -> list[str]:
    """What the scan says about one character `cp` in the middle of ordinary text."""
    (tmp_path / "f.txt").write_bytes(("name" + chr(cp) + "txt\n").encode("utf-8"))
    return raw_invisible_findings(tmp_path, ["f.txt"], allowed={})


@pytest.mark.parametrize("first,last", [(max(first, 1), last) for first, last in PINNED_RANGES])
def test_each_range_is_flagged_at_both_ends_and_not_one_past_either(tmp_path, first, last):
    """The first range starts at U+0001 here: a file with a NUL byte is binary and is never scanned."""
    assert _flagged(tmp_path, first) == [f"f.txt:1: U+{first:04X}"]
    assert _flagged(tmp_path, last) == [f"f.txt:1: U+{last:04X}"]
    assert _flagged(tmp_path, last + 1) == []
    if first > 1:
        assert _flagged(tmp_path, first - 1) == []


def test_the_scan_finds_every_code_point_of_the_set_and_names_file_line_and_point(tmp_path):
    cps = sorted(INVISIBLE - {0})
    text = "".join("name" + chr(cp) + "txt\n" for cp in cps)
    (tmp_path / "f.txt").write_bytes(text.encode("utf-8"))

    found = raw_invisible_findings(tmp_path, ["f.txt"], allowed={})

    assert found == [f"f.txt:{n}: U+{cp:04X}" for n, cp in enumerate(cps, start=1)]


def test_the_scan_leaves_clean_text_binary_files_and_missing_names_alone(tmp_path):
    (tmp_path / "clean.md").write_text("plain, 한글, emoji, escape text U+202E and a backslash-u form\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2" + chr(0x202E).encode("utf-8"))
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9 " + chr(0x202E).encode("utf-8"))  # not UTF-8: treated as binary

    assert raw_invisible_findings(tmp_path, ["clean.md", "blob.bin", "latin1.txt", "deleted.py"], allowed={}) == []


def test_ordinary_text_the_frontend_leaves_alone_is_left_alone(tmp_path):
    """Tab, newline, no-break space, the emoji presentation selector, combining marks, decomposed Hangul,
    a keycap, the line and paragraph separators: none is marked by the frontend, none is flagged here."""
    leave_alone = "\t\n".join(
        [
            "a" + chr(0xA0) + "b",
            chr(0x1F6E1) + chr(0xFE0F) + " shield",
            "e" + chr(0x301) + " a" + chr(0x30A),
            chr(0x1100) + chr(0x1161) + chr(0x11A8),
            "1" + chr(0xFE0F) + chr(0x20E3),
            "x" + chr(0x2028) + "y" + chr(0x2029) + "z",
        ]
    )
    (tmp_path / "f.txt").write_bytes((leave_alone + "\n").encode("utf-8"))

    assert raw_invisible_findings(tmp_path, ["f.txt"], allowed={}) == []


def test_the_scan_counts_lines_the_way_an_editor_does(tmp_path):
    # U+2028 is a line separator to str.splitlines() but not to an editor's line number.
    (tmp_path / "f.txt").write_text("a" + chr(0x2028) + "b\n" + chr(0x2067) + "\n", encoding="utf-8")

    assert raw_invisible_findings(tmp_path, ["f.txt"], allowed={}) == ["f.txt:2: U+2067"]


# --- the two rules it mirrors ---------------------------------------------------------------


def test_a_crlf_pair_is_a_line_ending_and_a_lone_cr_is_flagged(tmp_path):
    (tmp_path / "windows.txt").write_bytes(b"one\r\ntwo\r\nthree")
    (tmp_path / "lone.txt").write_bytes(b"one\r\ntw" + b"\r" + b"o\r\n")

    assert raw_invisible_findings(tmp_path, ["windows.txt"], allowed={}) == []
    # the line number is the editor's: the CR is on line 2, and the CRLF before it did not shift it
    assert raw_invisible_findings(tmp_path, ["lone.txt"], allowed={}) == ["lone.txt:2: U+000D"]


def _emoji(*cps: int) -> str:
    return "".join(chr(cp) for cp in cps)


ZWJ = 0x200D
SPARED = {
    "family": _emoji(0x1F468, ZWJ, 0x1F469, ZWJ, 0x1F467),
    "heart on fire (selector before the joiner)": _emoji(0x2764, 0xFE0F, ZWJ, 0x1F525),
    "skin tone before the joiner": _emoji(0x1F9D1, 0x1F3FD, ZWJ, 0x1F4BB),
    "rainbow flag": _emoji(0x1F3F3, 0xFE0F, ZWJ, 0x1F308),
    "pirate flag (selector after the second emoji)": _emoji(0x1F3F4, ZWJ, 0x2620, 0xFE0F),
}
FLAGGED = {
    "between letters": "a" + chr(ZWJ) + "b",
    "beside a path separator": "app/" + chr(ZWJ) + "main.py",
    "at the start of a line": chr(ZWJ) + _emoji(0x1F468),
    "at the end of a line": _emoji(0x1F468) + chr(ZWJ),
    "emoji on the left only": _emoji(0x1F468, ZWJ) + "x",
    "emoji on the right only": "x" + _emoji(ZWJ, 0x1F469),
    "a selector with no emoji under it": "x" + _emoji(0xFE0F, ZWJ, 0x1F469),
    "a skin tone with no emoji under it": "x" + _emoji(0x1F3FD, ZWJ, 0x1F469),
    "two joiners in a row": _emoji(0x1F468, ZWJ, ZWJ, 0x1F469),
}


@pytest.mark.parametrize("label", sorted(SPARED))
def test_the_zero_width_joiner_inside_an_emoji_sequence_is_left_alone(tmp_path, label):
    (tmp_path / "f.md").write_bytes(("한글 문장 " + SPARED[label] + " 끝\n").encode("utf-8"))

    assert raw_invisible_findings(tmp_path, ["f.md"], allowed={}) == []


@pytest.mark.parametrize("label", sorted(FLAGGED))
def test_the_zero_width_joiner_anywhere_else_is_flagged(tmp_path, label):
    text = FLAGGED[label]
    (tmp_path / "f.md").write_bytes(("ok\n" + text + "\n").encode("utf-8"))

    found = raw_invisible_findings(tmp_path, ["f.md"], allowed={})

    assert found == ["f.md:2: U+200D"] * text.count(chr(ZWJ)), (label, found)


def test_a_lone_joiner_is_flagged_on_a_line_that_also_holds_a_real_sequence(tmp_path):
    line = SPARED["family"] + " and " + "a" + chr(ZWJ) + "b"
    (tmp_path / "f.md").write_bytes((line + "\n").encode("utf-8"))

    assert raw_invisible_findings(tmp_path, ["f.md"], allowed={}) == ["f.md:1: U+200D"]


# --- the allowlist ------------------------------------------------------------------------


def test_an_allowlisted_pair_is_skipped_and_nothing_else_is(tmp_path):
    (tmp_path / "a.md").write_bytes(("x" + chr(0xFEFF) + "y" + chr(0x200B) + "\n").encode("utf-8"))
    (tmp_path / "b.md").write_bytes(("x" + chr(0xFEFF) + "\n").encode("utf-8"))
    allowed = {("a.md", 0xFEFF): "a fixture that needs the byte-order mark, for this test"}

    found = raw_invisible_findings(tmp_path, ["a.md", "b.md"], allowed=allowed)

    # the same character in another file, and another character in the same file, are still found
    assert found == ["a.md:1: U+200B", "b.md:1: U+FEFF"]


def test_an_allowlist_entry_that_matches_nothing_is_itself_a_finding(tmp_path):
    (tmp_path / "a.md").write_bytes(b"clean\n")
    allowed = {("a.md", 0xFEFF): "once held a byte-order mark, since removed"}

    found = raw_invisible_findings(tmp_path, ["a.md"], allowed=allowed)

    assert found == ["a.md: U+FEFF is allowlisted but is not in the file (remove the entry)"]


# --- CI must not be able to skip it ----------------------------------------------------


def _outcome(start: Path, environ: dict[str, str]) -> tuple[str, str]:
    """What `repo_findings` does at `start` under `environ`: ("skip" | "fail" | "ran", message).
    Both pytest outcomes are caught here, so a broken seam cannot turn one of these tests
    into a SKIP of its own: the assertion on the kind is what decides."""
    try:
        repo_findings(start, environ=environ)
    except pytest.skip.Exception as exc:
        return "skip", str(exc)
    except pytest.fail.Exception as exc:
        return "fail", str(exc)
    return "ran", ""


def test_without_the_ci_variable_a_missing_checkout_is_a_skip_with_its_reason(tmp_path):
    if tracked_files(tmp_path) is not None:
        pytest.skip("the temp directory is itself inside a git checkout on this host")
    for environ in ({}, {REQUIRE_ENV: ""}, {REQUIRE_ENV: "0"}):
        kind, message = _outcome(tmp_path, environ)
        assert kind == "skip", (environ, kind, message)
        assert "not inside a git checkout" in message


def test_with_the_ci_variable_a_missing_checkout_is_a_failure_carrying_the_reason(tmp_path):
    if tracked_files(tmp_path) is not None:
        pytest.skip("the temp directory is itself inside a git checkout on this host")
    kind, message = _outcome(tmp_path, {REQUIRE_ENV: "1"})

    assert kind == "fail"
    assert "must not skip: not inside a git checkout" in message


def test_with_the_ci_variable_a_checkout_that_is_not_this_repository_is_a_failure_too(tmp_path):
    """The second skip of the guard: a git checkout whose root lacks this test file."""
    if _git(["--version"], tmp_path).returncode != 0:
        pytest.skip("git is not available")
    assert _git(["init", "-q"], tmp_path).returncode == 0
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(["add", "x.py"], tmp_path).returncode == 0

    kind, message = _outcome(tmp_path, {})
    assert kind == "skip" and "is not this repository's root" in message
    kind, message = _outcome(tmp_path, {REQUIRE_ENV: "1"})
    assert kind == "fail" and "must not skip: the git checkout at" in message


def test_the_ci_backend_job_sets_the_variable():
    """The other half of the contract. Where the workflow file is reachable (a checkout), the
    backend job -- the text between `backend:` and `frontend:` -- sets the variable to 1."""
    found = tracked_files(_THIS_FILE.parent)
    workflow = found[0] / ".github" / "workflows" / "ci.yml" if found else None
    if workflow is None or not workflow.is_file():
        skip_or_fail("no checkout with .github/workflows/ci.yml to read (a backend-only container)")
    job = workflow.read_text(encoding="utf-8").split("\n  backend:", 1)[1].split("\n  frontend:", 1)[0]
    assert f'{REQUIRE_ENV}: "1"' in job


# --- it bites, through git itself ------------------------------------------------------


def test_a_tracked_file_with_a_raw_character_is_found_through_git_itself(tmp_path):
    """The whole path the guard takes, in a throwaway repository: git lists the tracked
    file, the scan reads it, and the character is reported."""
    if _git(["--version"], tmp_path).returncode != 0:
        pytest.skip("git is not available")
    assert _git(["init", "-q"], tmp_path).returncode == 0
    (tmp_path / "bad.py").write_text("x = 1  # " + chr(0x202E) + chr(0x200B) + chr(0xE0041) + "\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(["add", "bad.py", "good.py"], tmp_path).returncode == 0

    found = tracked_files(tmp_path)

    assert found is not None
    root, names = found
    assert sorted(names) == ["bad.py", "good.py"]
    assert raw_invisible_findings(root, names, allowed={}) == ["bad.py:1: U+202E", "bad.py:1: U+200B", "bad.py:1: U+E0041"]


def test_a_directory_that_is_not_a_git_checkout_has_no_tracked_list(tmp_path):
    """The skip path of the guard: no checkout -> None (the guard turns that into a skip)."""
    probe = tmp_path / "work" / "tests"
    probe.mkdir(parents=True)
    # tmp_path is normally outside any repository; if this host's temp dir is inside one, say so.
    if tracked_files(probe) is not None:
        pytest.skip("the temp directory is itself inside a git checkout on this host")

    assert tracked_files(probe) is None
