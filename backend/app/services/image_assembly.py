"""OCI image assembly.

Builds a local OCI image for a successful build from the harvested source
(the executor's sanitised copy of it, ADR 0032)
(protected paths already restored to the pinned template copies by
`publish.py` before this runs) using the *pinned* template Dockerfile —
never an agent-modified one, even if drift-restore in `publish.py` were
somehow bypassed: this module always copies the pinned Dockerfile over the
harvested one before invoking the Docker build, as a second, independent
guarantee.

Image assembly happens in L2 (this process), not inside any L3 container —
ADR 0003 invariant 2 (no Docker socket inside L3).
"""

from __future__ import annotations

import logging
import os
import shutil

import docker
from docker.errors import BuildError, DockerException

logger = logging.getLogger("app.image_assembly")


# ADR 0032 §Amendment 2026-10-17: the Docker-side twin of `.gitignore`. Docker leaves
# every path an ignore file lists out of the build context, and the agent writes this file.
_DOCKER_IGNORE_FILES = (".dockerignore", "Dockerfile.dockerignore")


class ImageAssemblyError(RuntimeError):
    """Raised when the OCI image build fails. Callers map this to
    HARVEST_ERROR — never
    swallowed."""


def assemble_image(
    *,
    workspace_dir: str,
    template_skeleton_dir: str,
    tag: str,
    labels: dict[str, str],
    docker_client: docker.DockerClient | None = None,
) -> str:
    """Builds an OCI image tagged `tag` from `workspace_dir`, using the
    pinned `Dockerfile` from `template_skeleton_dir` (protected path,
    restored by the caller — this function additionally re-copies it itself
    so image assembly can never build from an agent-modified Dockerfile
    regardless of upstream restore behavior).

    **the image is built from the same file set the commit holds.** Publish commits
    every file of the directory whatever its ignore rules say (`publish.commit_workspace`), so
    an agent-written `.dockerignore` must not be able to drop one from the image either: a file
    the reviewer reads in the PR would be absent from the service that runs. Its contents are
    emptied (the file stays, so the set of files is the same) before the build. The commit was
    made before this runs, so it still holds the file the agent wrote.

    **`labels` is required** -- the build image's ADR 0037 label set
    (`platform_labels(Role.BUILD_IMAGE, build_id=..., project_id=...)`), which is the only thing
    the retention sweep selects by; an unlabelled image would be invisible to it for good. The
    build runs with `forcerm=True`, so a failed build leaves no intermediate container behind.

    Returns the image tag on success. Raises `ImageAssemblyError` on any
    build failure.
    """
    client = docker_client or docker.from_env()

    pinned_dockerfile = f"{template_skeleton_dir}/Dockerfile"
    try:
        shutil.copyfile(pinned_dockerfile, f"{workspace_dir}/Dockerfile")
    except OSError as exc:
        raise ImageAssemblyError(f"could not stage pinned Dockerfile: {exc}") from exc

    _neutralise_ignore_files(workspace_dir)

    try:
        _, build_logs = client.images.build(path=workspace_dir, tag=tag, rm=True, forcerm=True, labels=labels)
        for chunk in build_logs:
            if "stream" in chunk:
                logger.debug("image build [%s]: %s", tag, chunk["stream"].rstrip())
    except (BuildError, DockerException) as exc:
        raise ImageAssemblyError(f"image build failed for tag {tag!r}: {exc}") from exc

    return tag


def _neutralise_ignore_files(workspace_dir: str) -> None:
    """Empties `.dockerignore` and `Dockerfile.dockerignore` in the build context, if the agent
    wrote them (see `assemble_image`). A regular file only: the harvested tree holds nothing else
    (ADR 0032), and anything that is not one is left for Docker to meet as it would have."""
    for name in _DOCKER_IGNORE_FILES:
        path = os.path.join(workspace_dir, name)
        try:
            if os.path.isfile(path) and not os.path.islink(path):
                with open(path, "w", encoding="utf-8"):
                    pass
        except OSError as exc:
            raise ImageAssemblyError(f"could not neutralise {name}: {exc}") from exc


def build_tag_for(project_id: str, build_id: str) -> str:
    """Deterministic image tag for a build (spec: `forge-build-<project>-<build>`)."""
    return f"forge-build-{project_id}-{build_id}"
