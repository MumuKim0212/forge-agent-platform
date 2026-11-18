"""the LLM proxy image's dependencies are pinned by `backend/constraints.txt`, and
the image runs as a non-root user.

The proxy container holds the real API key and the sandbox can reach it, so what runs in it is not
allowed to float. The image used to run `pip install fastapi 'uvicorn[standard]' httpx pydantic`
on `python:3.11-slim`: whatever was newest on the day it was built, on an interpreter the pins were
never resolved for.

Pure text test, no Docker and no network. The scans are functions over Dockerfile text so each
weakening can be shown, on a synthetic file, to be caught (`test_each_weakening_is_detected`); the
same functions guard the real file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
DOCKERFILE = BACKEND / "docker" / "llm-proxy" / "Dockerfile"
CONSTRAINTS = BACKEND / "constraints.txt"


def _code(text: str) -> str:
    """The Dockerfile without comment lines, with backslash continuations joined."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\\\s*\n", " ", "\n".join(lines))


def _pins(constraints_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in constraints_text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)\s*$", line.strip())
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def _constraints_python_minor(constraints_text: str) -> str:
    m = re.search(r"python:(\d+\.\d+)-slim", constraints_text)
    assert m, "constraints.txt's header no longer says which Python image it was resolved on"
    return m.group(1)


def problems(dockerfile_text: str, constraints_text: str) -> list[str]:
    """Everything wrong with the image's dependency pinning and user, as sentences."""
    code = _code(dockerfile_text)
    found: list[str] = []

    froms = re.findall(r"(?im)^FROM\s+(\S+)", code)
    if len(froms) != 1:
        found.append(f"expected exactly one FROM, found {len(froms)}")
    else:
        image = froms[0]
        m = re.fullmatch(r"python:(\d+\.\d+)-slim", image)
        if not m:
            found.append(f"base image {image!r} is not an explicit python:<minor>-slim tag")
        elif m.group(1) != _constraints_python_minor(constraints_text):
            found.append(
                f"base image Python {m.group(1)} is not the Python constraints.txt was resolved on "
                f"({_constraints_python_minor(constraints_text)})"
            )

    if not re.search(r"(?im)^COPY\s+constraints\.txt\s+\S+", code):
        found.append("constraints.txt is not copied into the image")

    installs = re.findall(r"pip3?\s+install\b([^\n&;]*)", code)
    if not installs:
        found.append("no pip install line found")
    pins = _pins(constraints_text)
    for args in installs:
        if not re.search(r"(?:^|\s)(?:-c|--constraint)[\s=]+\S*constraints\.txt\b", args):
            found.append(f"pip install without -c constraints.txt: {args.strip()!r}")
        for token in args.split():
            if token.startswith("-") or "constraints.txt" in token:
                continue
            name = re.split(r"[\[=<>!~;]", token.strip("'\""), maxsplit=1)[0].lower().replace("_", "-")
            if "==" in token:
                continue  # an inline exact pin is its own pin
            if name and name not in pins:
                found.append(f"{name!r} is installed but has no exact pin in constraints.txt")

    users = re.findall(r"(?im)^USER\s+(\S+)", code)
    if not users:
        found.append("no USER directive: the image would run as root")
    elif users[-1].split(":")[0] in ("0", "root"):
        found.append(f"the final USER is {users[-1]!r}, which is root")
    elif not re.fullmatch(r"\d+(?::\d+)?", users[-1]):
        found.append(f"the final USER {users[-1]!r} is not numeric")

    return found


def test_the_real_dockerfile_has_no_problems():
    assert problems(DOCKERFILE.read_text(encoding="utf-8"), CONSTRAINTS.read_text(encoding="utf-8")) == []


def test_the_real_dockerfile_installs_what_it_is_known_to_need_and_pins_each():
    """The set the proxy needs, as literals (a contract of the image, not derived from it)."""
    pins = _pins(CONSTRAINTS.read_text(encoding="utf-8"))
    install = re.findall(r"pip3?\s+install\b([^\n&;]*)", _code(DOCKERFILE.read_text(encoding="utf-8")))
    assert len(install) == 1
    for name in ("fastapi", "uvicorn", "httpx", "pydantic"):
        assert name in install[0]
        assert name in pins


def test_the_real_dockerfile_declares_the_non_root_user_as_a_literal():
    assert "USER 65534:65534" in DOCKERFILE.read_text(encoding="utf-8")


_GOOD = (
    "FROM python:3.14-slim\n"
    "WORKDIR /app\n"
    "COPY constraints.txt /app/constraints.txt\n"
    "RUN pip install --no-cache-dir -c /app/constraints.txt fastapi 'uvicorn[standard]' httpx pydantic\n"
    "COPY app /app/app\n"
    "USER 65534:65534\n"
    'ENTRYPOINT ["python", "-m", "app.proxy_server"]\n'
)
_CONSTRAINTS = (
    "# Generated 2026-10-05 in a throwaway python:3.14-slim container (Python 3.14.8)\n"
    "# --- pins ---\n"
    "fastapi==0.142.2\nuvicorn==0.54.0\nhttpx==0.28.1\npydantic==2.13.5\n"
)


def test_the_scan_accepts_a_fully_pinned_non_root_file():
    assert problems(_GOOD, _CONSTRAINTS) == []


@pytest.mark.parametrize(
    "old,new,expect",
    [
        ("FROM python:3.14-slim", "FROM python:3.11-slim", "is not the Python constraints.txt was resolved on"),
        ("FROM python:3.14-slim", "FROM python:latest", "is not an explicit python:<minor>-slim tag"),
        ("FROM python:3.14-slim", "FROM python:3.14", "is not an explicit python:<minor>-slim tag"),
        ("-c /app/constraints.txt ", "", "pip install without -c constraints.txt"),
        ("COPY constraints.txt /app/constraints.txt\n", "", "constraints.txt is not copied"),
        ("httpx pydantic", "httpx pydantic requests", "'requests' is installed but has no exact pin"),
        ("USER 65534:65534\n", "", "no USER directive"),
        ("USER 65534:65534", "USER root", "which is root"),
        ("USER 65534:65534", "USER 0", "which is root"),
        ("USER 65534:65534", "USER nobody", "is not numeric"),
    ],
)
def test_each_weakening_is_detected(old, new, expect):
    assert old in _GOOD
    weakened = _GOOD.replace(old, new)

    assert any(expect in p for p in problems(weakened, _CONSTRAINTS)), problems(weakened, _CONSTRAINTS)


def test_an_install_that_pins_a_package_the_constraints_do_not_cover_is_detected():
    thin = _CONSTRAINTS.replace("httpx==0.28.1\n", "")

    assert any("'httpx' is installed but has no exact pin" in p for p in problems(_GOOD, thin))
