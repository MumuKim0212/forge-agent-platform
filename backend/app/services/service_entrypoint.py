"""the template's service entrypoint.

A build whose output holds none of the files the template's service starts from cannot
run: the image builds (a `COPY` of whatever is there), the preview container exits at
once and a deployment fails its health gate. So the build is checked for it after
harvest and before anything is assembled, and fails with `MISSING_ENTRYPOINT`.

**The entrypoint is derived from the template, never restated here.** It is the
`module:attribute` the template's pinned `Dockerfile` `CMD` starts (`uvicorn app:app`),
placed in the *workspace* through that same Dockerfile's `WORKDIR` and `COPY` lines
(`COPY app/ /app/` puts the workspace's `app/` at `/app/`, so module `app` is
`app/app.py`). Rename the skeleton's module and the Dockerfile that starts it, and the
check follows; nothing in this file names `app.py`.

Only the skeleton's Dockerfile is read, never the workspace's copy (an agent could have
edited that one, and publish restores the pinned one over it anyway).

A template whose Dockerfile does not yield an entrypoint (no `module:attribute` in its
`CMD`, or the module's file is not under any `COPY`) raises `EntrypointUndeterminable`:
that is the platform's own template being unusable, not the build's fault, and the caller
fails the build as such rather than letting every build through unchecked.
"""

from __future__ import annotations

import json
import os
import posixpath
import re

# `module:attribute`, the ASGI/WSGI app reference. A bind address such as `0.0.0.0:8000`
# does not match: a module name does not start with a digit.
_APP_REFERENCE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)$")


class EntrypointUndeterminable(Exception):
    """The template's Dockerfile does not say which file the service starts from."""


def _instructions(dockerfile_text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs: comments dropped, `\\` continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending.strip())
    out: list[tuple[str, str]] = []
    for statement in joined:
        head, _, rest = statement.partition(" ")
        out.append((head.upper(), rest.strip()))
    return out


def _cmd_words(arguments: str) -> list[str]:
    """The words of a `CMD`, whether exec form (a JSON array) or shell form."""
    if arguments.startswith("["):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(w, str) for w in parsed):
            return [piece for word in parsed for piece in word.split()]
    return arguments.split()


def _app_module(instructions: list[tuple[str, str]]) -> str:
    cmd = [args for name, args in instructions if name == "CMD"]
    if not cmd:
        raise EntrypointUndeterminable("the template's Dockerfile has no CMD")
    for word in _cmd_words(cmd[-1]):
        match = _APP_REFERENCE.match(word)
        if match:
            return match.group(1)
    raise EntrypointUndeterminable("the template's Dockerfile CMD names no module:attribute to start")


def _copy_mappings(instructions: list[tuple[str, str]], skeleton_dir: str) -> list[tuple[str, str, bool]]:
    """(workspace source, absolute container destination, source is a directory) for each
    `COPY` from the build context. `COPY --from=...` copies from another stage, not from
    the workspace, so it is skipped."""
    workdir = "/"
    mappings: list[tuple[str, str, bool]] = []
    for name, arguments in instructions:
        if name == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, arguments.strip().strip('"')))
        elif name == "COPY":
            words = arguments.split()
            flags = [w for w in words if w.startswith("--")]
            if any(f.startswith("--from") for f in flags):
                continue
            paths = [w for w in words if not w.startswith("--")]
            if len(paths) < 2:
                continue
            *sources, destination = paths
            absolute = posixpath.normpath(posixpath.join(workdir, destination))
            for source in sources:
                is_dir = (
                    source.endswith("/")
                    or posixpath.normpath(source) == "."
                    or os.path.isdir(os.path.join(skeleton_dir, *source.split("/")))
                )
                mappings.append((posixpath.normpath(source), absolute, is_dir))
    return mappings


def _workdir(instructions: list[tuple[str, str]]) -> str:
    workdir = "/"
    for name, arguments in instructions:
        if name == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, arguments.strip().strip('"')))
    return workdir


def service_entrypoint_paths(template_skeleton_dir: str) -> list[str]:
    """The workspace paths (POSIX, relative to the workspace root) any ONE of which
    satisfies the template's entrypoint: the module as a file, or as a package.

    Raises `EntrypointUndeterminable` when the template's Dockerfile cannot be read or
    does not say."""
    dockerfile = os.path.join(template_skeleton_dir, "Dockerfile")
    try:
        with open(dockerfile, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise EntrypointUndeterminable("the template's Dockerfile could not be read") from exc

    instructions = _instructions(text)
    module_path = _app_module(instructions).replace(".", "/")
    workdir = _workdir(instructions)
    mappings = _copy_mappings(instructions, template_skeleton_dir)

    found: list[str] = []
    for container_path in (
        posixpath.normpath(posixpath.join(workdir, module_path + ".py")),
        posixpath.normpath(posixpath.join(workdir, module_path, "__init__.py")),
    ):
        for source, destination, is_dir in mappings:
            if is_dir:
                if container_path == destination or not container_path.startswith(destination.rstrip("/") + "/"):
                    continue
                relative = container_path[len(destination.rstrip("/")) + 1 :]
                workspace_path = posixpath.normpath(posixpath.join(source, relative))
            else:
                if container_path != destination:
                    continue
                workspace_path = source
            found.append(workspace_path)
            break
    if not found:
        raise EntrypointUndeterminable("the template's start module is not copied into the image from the workspace")
    return found


def missing_entrypoint(workspace_dir: str, template_skeleton_dir: str) -> list[str] | None:
    """`None` when the workspace holds the template's entrypoint; otherwise the paths
    that would each have satisfied it (platform-known, template-derived values)."""
    required = service_entrypoint_paths(template_skeleton_dir)
    for path in required:
        if os.path.isfile(os.path.join(workspace_dir, *path.split("/"))):
            return None
    return required
