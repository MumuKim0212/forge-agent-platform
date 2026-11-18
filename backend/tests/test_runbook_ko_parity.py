"""the Korean install runbook is a translation of the prose only.

`deploy/linux/README.ko.md` must follow `deploy/linux/README.md` section for section, and every
fenced code block (a command an operator pastes) must be byte-identical to the English one. A
translated command or a changed placeholder is a runbook that fails on the host, so it fails here.
Inline code spans (setting names, paths, commands in prose) are compared as a multiset too.
"""

import re
from collections import Counter
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "linux"
ENGLISH = DEPLOY / "README.md"
KOREAN = DEPLOY / "README.ko.md"

FENCE = re.compile(r"^```")
HEADING = re.compile(r"^(#{2,3}) ")
INLINE = re.compile(r"`([^`\n]+)`")


def _split(text: str) -> tuple[list[str], list[str], list[str]]:
    """(heading levels in order, fenced blocks in order, prose lines outside fences)."""
    headings: list[str] = []
    blocks: list[str] = []
    prose: list[str] = []
    current: list[str] | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            if current is None:
                current = [line]
            else:
                current.append(line)
                blocks.append("\n".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
            continue
        match = HEADING.match(line)
        if match:
            headings.append(match.group(1))
        prose.append(line)
    assert current is None, "unterminated code fence"
    return headings, blocks, prose


def _load(path: Path):
    return _split(path.read_bytes().decode("utf-8"))


def test_same_headings_in_the_same_order():
    en_headings, _, _ = _load(ENGLISH)
    ko_headings, _, _ = _load(KOREAN)
    assert len(en_headings) > 10  # the parser found the runbook's sections at all
    assert ko_headings == en_headings


def test_fenced_code_blocks_are_identical_byte_for_byte():
    _, en_blocks, _ = _load(ENGLISH)
    _, ko_blocks, _ = _load(KOREAN)
    assert len(en_blocks) > 10
    assert len(ko_blocks) == len(en_blocks)
    for index, (en, ko) in enumerate(zip(en_blocks, ko_blocks)):
        assert ko.encode("utf-8") == en.encode("utf-8"), f"code block {index + 1} differs"


def test_inline_code_spans_are_the_same_multiset():
    _, _, en_prose = _load(ENGLISH)
    _, _, ko_prose = _load(KOREAN)
    en_spans = Counter(span for line in en_prose for span in INLINE.findall(line))
    ko_spans = Counter(span for line in ko_prose for span in INLINE.findall(line))
    assert ko_spans - en_spans == Counter(), "spans only in the Korean file"
    assert en_spans - ko_spans == Counter(), "spans only in the English file"
