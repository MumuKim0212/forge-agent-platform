"""`constraints.txt` must pin every direct dependency `pyproject.toml` names.

`backend/constraints.txt` is what CI, `dev.ps1` and `test.ps1` install with
(`pip install -c constraints.txt -e ".[dev]"`); `pyproject.toml` keeps the `>=`
bounds. A dependency added to the second without being pinned in the first would
quietly float again -- the exact drift that was once caught only by accident.

Pure text test -- no pip, no network, no Docker. The checks are functions over
text so each failure shape can be shown on a synthetic file
(`test_each_failure_shape_is_detected`), and the same functions guard the real
files. Only *direct* dependencies are checked here: the transitive half is
covered by the generation procedure, not by a test that
would have to re-resolve the dependency graph.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
PYPROJECT = BACKEND / "pyproject.toml"
CONSTRAINTS = BACKEND / "constraints.txt"

# PEP 503 normalisation: runs of `-`, `_`, `.` are equivalent, case-insensitive.
_SEPARATORS = re.compile(r"[-_.]+")
# A requirement string starts with the distribution name; extras, a specifier or
# an environment marker follow it.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
# One exact pin: `name==1.2.3` (a bare `==` release; no `>=`, no wildcard, no extras).
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\d[A-Za-z0-9.!+_-]*)\s*(?:#.*)?$")


def _normalise(name: str) -> str:
    return _SEPARATORS.sub("-", name).lower()


def direct_dependency_names(pyproject_text: str) -> set[str]:
    """Normalised names of the core and every optional-extra dependency."""
    project = tomllib.loads(pyproject_text)["project"]
    requirements = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirements.extend(extra_requirements)
    names = set()
    for requirement in requirements:
        match = _REQ_NAME.match(requirement)
        assert match, f"cannot read a distribution name from {requirement!r}"
        names.add(_normalise(match.group(1)))
    return names


def pinned_versions(constraints_text: str) -> dict[str, str]:
    """`{normalised name: version}` for every line of the constraints file.

    A line that is neither blank, a comment, nor an exact `name==version` pin
    raises: a `>=` or an editable line in this file would be a pin that is not one.
    """
    pins: dict[str, str] = {}
    for number, raw in enumerate(constraints_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match, f"constraints.txt line {number} is not an exact `name==version` pin: {raw!r}"
        pins[_normalise(match.group(1))] = match.group(2)
    return pins


def unpinned_direct_dependencies(pyproject_text: str, constraints_text: str) -> list[str]:
    pins = pinned_versions(constraints_text)
    return sorted(name for name in direct_dependency_names(pyproject_text) if name not in pins)


def test_every_direct_dependency_has_an_exact_pin():
    missing = unpinned_direct_dependencies(
        PYPROJECT.read_text(encoding="utf-8"), CONSTRAINTS.read_text(encoding="utf-8")
    )
    assert not missing, (
        f"direct dependencies without an exact pin in backend/constraints.txt: {missing}. "
        "Regenerate backend/constraints.txt so every direct dependency has an exact pin."
    )


def test_constraints_does_not_pin_the_project_itself():
    # `forge-backend` is installed editable from the checkout; a `==0.1.0` pin on it
    # would make `pip install -c constraints.txt -e .` fail the day the version moves.
    project_name = _normalise(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"])
    assert project_name not in pinned_versions(CONSTRAINTS.read_text(encoding="utf-8"))


_PYPROJECT = """
[project]
name = "demo"
dependencies = ["Fast_API>=0.1", "uvicorn[standard]>=0.3", 'pyjwt[crypto]>=2.9']
[project.optional-dependencies]
dev = ["pytest>=8"]
"""

_CONSTRAINTS = """\
# header
fastapi==0.1.2
uvicorn==0.3.1
PyJWT==2.9.0
pytest==8.0.0
"""


def test_names_are_normalised_and_extras_stripped():
    assert direct_dependency_names(_PYPROJECT) == {"fast-api", "uvicorn", "pyjwt", "pytest"}


@pytest.mark.parametrize(
    "constraints, expected_missing",
    [
        # `Fast_API` normalises to `fast-api`, not `fastapi`: a genuinely different distribution.
        (_CONSTRAINTS, ["fast-api"]),
        # a pin deleted
        (_CONSTRAINTS.replace("pytest==8.0.0\n", "").replace("fastapi", "fast_api"), ["pytest"]),
    ],
)
def test_each_failure_shape_is_detected(constraints, expected_missing):
    assert unpinned_direct_dependencies(_PYPROJECT, constraints) == expected_missing


def test_a_dependency_added_without_a_pin_is_detected():
    grown = _PYPROJECT.replace('"pytest>=8"', '"pytest>=8", "newlib>=1"')
    full = _CONSTRAINTS.replace("fastapi", "fast_api")
    assert unpinned_direct_dependencies(_PYPROJECT, full) == []
    assert unpinned_direct_dependencies(grown, full) == ["newlib"]


@pytest.mark.parametrize("bad_line", ["fast_api>=0.1", "fast_api", "-e .", "fast_api==0.*", "fast_api~=0.1"])
def test_a_line_that_is_not_an_exact_pin_is_rejected(bad_line):
    with pytest.raises(AssertionError):
        pinned_versions(_CONSTRAINTS + bad_line + "\n")
