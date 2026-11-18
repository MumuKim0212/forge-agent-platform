"""The OpenAI agent image pins exact SDK versions (ADR 0035 §Decision 2).

Same reason and same shape as `test_agent_sandbox_dockerfile.py` for the Claude image: the
request shapes the runner sends (`store`, `service_tier`, the tool schema) are what the proxy's
`openai` dialect admits, and they are version-dependent, so a floating install line silently
voids the engine-up test's recorded shapes on the next rebuild.

Pure text test -- no Docker, no network. The scan is a function over Dockerfile text so each
install can be unpinned independently in a synthetic Dockerfile.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "agent-sandbox-openai" / "Dockerfile"

_PIP_EXACT = re.compile(r"^==\d+\.\d+\.\d+$")
# The package name must end at the specifier, so `openai` does not also match `openai-agents`.
_PIP_PACKAGES = {
    "openai-agents": re.compile(r"(?<![\w-])openai-agents(?P<spec>[^\s&]*)"),
    "openai": re.compile(r"(?<![\w-])openai(?![\w-])(?P<spec>[^\s&]*)"),
}
_PIP_INSTALL = re.compile(r"pip3?\s+install\b(?P<args>[^\n&]*)")


def _without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def unpinned_installs(text: str) -> list[str]:
    """Names of the SDK installs that are absent or not pinned exactly."""
    install_args = " ".join(m.group("args") for m in _PIP_INSTALL.finditer(_without_comments(text)))
    problems: list[str] = []
    for name, pattern in _PIP_PACKAGES.items():
        matches = list(pattern.finditer(install_args))
        if not matches:
            problems.append(f"{name}: no install found")
        for m in matches:
            if not _PIP_EXACT.match(m.group("spec")):
                problems.append(f"{name}: {m.group('spec')!r} is not an exact version pin")
    return problems


_GOOD = "FROM python:3.11-slim\nRUN pip install --no-cache-dir openai-agents==0.23.1 openai==3.28.0\n"


def test_real_dockerfile_pins_both_installs() -> None:
    assert unpinned_installs(DOCKERFILE.read_text(encoding="utf-8")) == []


def test_real_dockerfile_pins_literal_versions() -> None:
    # Literals, so a bump is a deliberate two-sided edit that re-runs the engine-up test.
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "openai-agents==0.23.1" in text
    assert "openai==3.28.0" in text
    assert text.startswith("#") and "\nFROM python:3.11-slim\n" in text


def test_the_scan_accepts_a_fully_pinned_file() -> None:
    assert unpinned_installs(_GOOD) == []


@pytest.mark.parametrize(
    "old,new,expect_name",
    [
        ("openai-agents==0.23.1", "openai-agents", "openai-agents"),
        ("openai-agents==0.23.1", "openai-agents>=0.23.1", "openai-agents"),
        ("openai-agents==0.23.1", "openai-agents~=0.23.1", "openai-agents"),
        ("openai-agents==0.23.1", "openai-agents==0.23.*", "openai-agents"),
        ("openai-agents==0.23.1", "openai-agents<1", "openai-agents"),
        ("openai==3.28.0", "openai", "openai"),
        ("openai==3.28.0", "openai>=3", "openai"),
        ("openai==3.28.0", "openai==3.*", "openai"),
    ],
)
def test_each_unpinned_form_is_detected(old: str, new: str, expect_name: str) -> None:
    """Unpin ONE install at a time: the other stays pinned, and only that one is reported."""
    assert old in _GOOD
    problems = unpinned_installs(_GOOD.replace(old, new))
    assert problems, f"{new!r} slipped through"
    assert all(p.startswith(f"{expect_name}:") for p in problems), problems


def test_a_missing_install_fails() -> None:
    assert unpinned_installs("FROM python:3.11-slim\n")
    assert any("openai: no install" in p for p in unpinned_installs(_GOOD.replace("openai==3.28.0", "requests==2.0.0")))


def test_a_commented_out_pin_does_not_count() -> None:
    text = "# pip install openai-agents==0.23.1 openai==3.28.0\n"
    assert len(unpinned_installs(text)) == 2
