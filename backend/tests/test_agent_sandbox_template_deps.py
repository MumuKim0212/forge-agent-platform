"""The agent sandbox images carry the template's own dependencies, pinned exactly.

Why: the generated service is `templates/generic-webtool`, whose Dockerfile installs fastapi,
sqlalchemy and the rest. The agent is told to import the app it writes before it finishes; in a
sandbox without those packages that import fails on `fastapi`, so the check proves nothing, and an
agent that meets the error edits its imports to get past it (a build that reported wrapping FastAPI in a
try/except to pass it was observed). The two sandbox images (Claude and OpenAI engines) therefore
install the same packages.

The template floats on `>=` bounds; the sandboxes pin exactly, like their SDKs (ADR 0029 §4), to the
versions in `constraints.txt`, so there is one answer to "which FastAPI does the agent run against".
This test is what keeps the three files from drifting: a template dependency that a sandbox does not
install, a pin that is not exact, a pin outside the template's bound, or a pin that is not the one
in `constraints.txt`.

Pure text test -- no Docker, no network. The scans are functions over Dockerfile text so each rule can
be broken independently in a synthetic file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

BACKEND = Path(__file__).resolve().parent.parent
TEMPLATE_DOCKERFILE = BACKEND / "templates" / "generic-webtool" / "Dockerfile"
SANDBOX_DOCKERFILES = {
    "claude": BACKEND / "docker" / "agent-sandbox" / "Dockerfile",
    "openai": BACKEND / "docker" / "agent-sandbox-openai" / "Dockerfile",
}
CONSTRAINTS = BACKEND / "constraints.txt"

# Packages a sandbox installs on top of the template's list, each for a stated reason (see the
# Dockerfiles). Anything else extra is a decision somebody should make on purpose.
ALLOWED_EXTRAS = {"httpx"}

_REQUIREMENT = re.compile(
    r"""["']?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[(?P<extras>[^\]]*)\])?(?P<spec>(?:[<>=!~]=?[^\s"'\\]+)?)["']?"""
)
_EXACT = re.compile(r"^==\d+(\.\d+)*$")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _install_args(text: str) -> list[str]:
    """The arguments of every `pip install` in a Dockerfile, continuation lines joined, comments
    dropped, flags (`--no-cache-dir`, `-c file`) removed."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\s*\n", " ", "\n".join(lines))
    args: list[str] = []
    for match in re.finditer(r"pip3?\s+install\b(?P<rest>[^\n&]*)", joined):
        tokens = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", match.group("rest"))
        args.extend(token for token in tokens if not token.startswith("-"))
    return args


def requirements(text: str) -> dict[str, str]:
    """name (normalised, no extras) -> specifier ('' if none) for everything a Dockerfile pip-installs."""
    found: dict[str, str] = {}
    for token in _install_args(text):
        match = _REQUIREMENT.fullmatch(token)
        if match:
            found[_normalise(match.group("name"))] = match.group("spec")
    return found


def constraints_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, version = line.split("==", 1)
            pins[_normalise(name)] = version.strip()
    return pins


def problems(sandbox_text: str, template_text: str, pins: dict[str, str]) -> list[str]:
    """What is wrong with a sandbox Dockerfile relative to the template and constraints.txt."""
    template = requirements(template_text)
    sandbox = requirements(sandbox_text)
    found: list[str] = []
    for name, bound in sorted(template.items()):
        if name not in sandbox:
            found.append(f"{name}: a template dependency the sandbox does not install")
            continue
        spec = sandbox[name]
        if not _EXACT.match(spec):
            found.append(f"{name}: {spec!r} is not an exact pin")
            continue
        version = spec[2:]
        if bound and Version(version) not in SpecifierSet(bound):
            found.append(f"{name}: {version} is outside the template's own bound {bound}")
        if pins.get(name) != version:
            found.append(f"{name}: {version} is not the version in constraints.txt ({pins.get(name)})")
    return found


def test_the_template_declares_the_dependencies_this_test_is_about():
    template = requirements(TEMPLATE_DOCKERFILE.read_text(encoding="utf-8"))

    assert {"fastapi", "uvicorn", "sqlalchemy", "alembic", "psycopg", "redis"} <= set(template)


@pytest.mark.parametrize("engine", sorted(SANDBOX_DOCKERFILES))
def test_each_sandbox_image_installs_every_template_dependency_pinned_inside_its_bound(engine):
    problem_list = problems(
        SANDBOX_DOCKERFILES[engine].read_text(encoding="utf-8"),
        TEMPLATE_DOCKERFILE.read_text(encoding="utf-8"),
        constraints_pins(),
    )

    assert problem_list == []


def test_the_two_sandbox_images_install_the_same_template_packages_at_the_same_versions():
    template = set(requirements(TEMPLATE_DOCKERFILE.read_text(encoding="utf-8")))
    installs = {
        engine: {name: spec for name, spec in requirements(path.read_text(encoding="utf-8")).items() if name in template | ALLOWED_EXTRAS}
        for engine, path in SANDBOX_DOCKERFILES.items()
    }

    assert installs["claude"] == installs["openai"]
    assert ALLOWED_EXTRAS <= set(installs["claude"])


@pytest.mark.parametrize("engine", sorted(SANDBOX_DOCKERFILES))
def test_httpx_the_one_extra_is_pinned_exactly(engine):
    spec = requirements(SANDBOX_DOCKERFILES[engine].read_text(encoding="utf-8"))["httpx"]

    assert _EXACT.match(spec) and spec[2:] == constraints_pins()["httpx"]


# --- the scan itself, over synthetic Dockerfiles -----------------------------------------------

_TEMPLATE = (
    'RUN pip install --no-cache-dir \\\n    "fastapi>=0.115" \\\n    "uvicorn[standard]>=0.32" \\\n'
    '    "psycopg[binary]>=3.1" \\\n    "redis>=5.0"\n'
)
_PINS = {"fastapi": "0.142.2", "uvicorn": "0.54.0", "psycopg": "3.3.6", "redis": "8.1.0"}
_GOOD = (
    "RUN pip install --no-cache-dir fastapi==0.142.2 uvicorn==0.54.0 \\\n"
    '    "psycopg[binary]==3.3.6" redis==8.1.0\n'
)


def test_the_scan_reads_the_template_with_extras_quotes_and_continuations():
    assert requirements(_TEMPLATE) == {"fastapi": ">=0.115", "uvicorn": ">=0.32", "psycopg": ">=3.1", "redis": ">=5.0"}


def test_the_scan_accepts_a_file_that_matches():
    assert problems(_GOOD, _TEMPLATE, _PINS) == []


@pytest.mark.parametrize(
    "old, new, fragment",
    [
        ("redis==8.1.0", "", "redis: a template dependency the sandbox does not install"),
        ("fastapi==0.142.2", "fastapi", "fastapi: '' is not an exact pin"),
        ("fastapi==0.142.2", "fastapi>=0.142.2", "fastapi: '>=0.142.2' is not an exact pin"),
        ("fastapi==0.142.2", "fastapi==0.142.*", "is not an exact pin"),
        ("fastapi==0.142.2", "fastapi==0.100.0", "fastapi: 0.100.0 is outside the template's own bound >=0.115"),
        ("uvicorn==0.54.0", "uvicorn==0.55.0", "uvicorn: 0.55.0 is not the version in constraints.txt (0.54.0)"),
    ],
)
def test_each_way_to_drift_is_detected(old, new, fragment):
    broken = _GOOD.replace(old, new)

    assert any(fragment in problem for problem in problems(broken, _TEMPLATE, _PINS)), problems(broken, _TEMPLATE, _PINS)
