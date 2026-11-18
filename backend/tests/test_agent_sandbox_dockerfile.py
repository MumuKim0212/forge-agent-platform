"""The real agent image pins exact SDK and CLI versions (ADR 0029 §4).

Tool-option semantics are version-dependent: the tool-set / config-source
behaviour that was verified holds only for the pinned versions. A floating
install line silently voids that verification on the next image rebuild.

Pure text test -- no Docker, no network. The scan is a function over Dockerfile
text so each install line can be unpinned independently in a synthetic Dockerfile
(see `test_each_unpinned_form_is_detected`), and the same function guards the real file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "agent-sandbox" / "Dockerfile"

# One exact dotted version: no range operator, no tag, no wildcard.
_PIP_EXACT = re.compile(r"^==\d+\.\d+\.\d+$")
_NPM_EXACT = re.compile(r"^@\d+\.\d+\.\d+$")

_PIP_LINE = re.compile(r"pip3?\s+install\b[^\n&]*?\bclaude-agent-sdk(?P<spec>[^\s\&]*)")
_NPM_LINE = re.compile(r"npm\s+(?:install|i)\b[^\n&]*?@anthropic-ai/claude-code(?P<spec>[^\s\&]*)")


def _without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def unpinned_installs(text: str) -> list[str]:
    """Names of the agent-runtime installs that are absent or not pinned exactly."""
    code = _without_comments(text)
    problems: list[str] = []
    for name, line_re, exact in (
        ("claude-agent-sdk", _PIP_LINE, _PIP_EXACT),
        ("@anthropic-ai/claude-code", _NPM_LINE, _NPM_EXACT),
    ):
        matches = list(line_re.finditer(code))
        if not matches:
            problems.append(f"{name}: no install line found")
        for m in matches:
            if not exact.match(m.group("spec")):
                problems.append(f"{name}: {m.group('spec')!r} is not an exact version pin")
    return problems


_GOOD = (
    "FROM python:3.11-slim\n"
    "RUN npm install -g @anthropic-ai/claude-code@2.1.285 \\n"
    "    && pip install --no-cache-dir claude-agent-sdk==0.2.162\n"
)


def test_real_dockerfile_pins_both_installs() -> None:
    assert unpinned_installs(DOCKERFILE.read_text(encoding="utf-8")) == []


def test_real_dockerfile_pins_literal_versions() -> None:
    # A literal, so a bump is a deliberate two-sided edit (ADR 0029 §4:
    # upgrading re-runs the verification).
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "claude-agent-sdk==0.2.162" in text
    assert "@anthropic-ai/claude-code@2.1.285" in text


def test_the_scan_accepts_a_fully_pinned_file() -> None:
    assert unpinned_installs(_GOOD) == []


@pytest.mark.parametrize(
    "old,new,expect_name",
    [
        # SDK line, each way of not pinning
        ("claude-agent-sdk==0.2.162", "claude-agent-sdk", "claude-agent-sdk"),
        ("claude-agent-sdk==0.2.162", "claude-agent-sdk>=0.2.162", "claude-agent-sdk"),
        ("claude-agent-sdk==0.2.162", "claude-agent-sdk~=0.2.162", "claude-agent-sdk"),
        ("claude-agent-sdk==0.2.162", "claude-agent-sdk==0.2.*", "claude-agent-sdk"),
        ("claude-agent-sdk==0.2.162", "claude-agent-sdk<1", "claude-agent-sdk"),
        # CLI line, each way of not pinning
        ("@anthropic-ai/claude-code@2.1.285", "@anthropic-ai/claude-code", "@anthropic-ai/claude-code"),
        ("@anthropic-ai/claude-code@2.1.285", "@anthropic-ai/claude-code@latest", "@anthropic-ai/claude-code"),
        ("@anthropic-ai/claude-code@2.1.285", "@anthropic-ai/claude-code@^2.1.285", "@anthropic-ai/claude-code"),
        ("@anthropic-ai/claude-code@2.1.285", "@anthropic-ai/claude-code@2", "@anthropic-ai/claude-code"),
    ],
)
def test_each_unpinned_form_is_detected(old: str, new: str, expect_name: str) -> None:
    """Unpin ONE line at a time: the other stays pinned, and only that one is reported."""
    assert old in _GOOD
    problems = unpinned_installs(_GOOD.replace(old, new))
    assert problems, f"{new!r} slipped through"
    assert all(p.startswith(expect_name) for p in problems), problems


def test_a_missing_install_line_fails() -> None:
    assert unpinned_installs("FROM python:3.11-slim\n")  # neither line present
    assert any("no install line" in p for p in unpinned_installs(_GOOD.replace("claude-agent-sdk==0.2.162", "requests")))


def test_a_commented_out_pin_does_not_count() -> None:
    text = "# pip install claude-agent-sdk==0.2.162\n# npm install -g @anthropic-ai/claude-code@2.1.285\n"
    assert len(unpinned_installs(text)) == 2
