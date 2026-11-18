"""Integration tests for `app/services/image_assembly.py`
against a real Docker Engine. Skips
cleanly when Docker is unavailable.
"""

from __future__ import annotations

import io
import tarfile

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.services.image_assembly import ImageAssemblyError, assemble_image, build_tag_for  # noqa: E402
from app.services.local_git_provider import LocalGitProvider  # noqa: E402
from app.services.publish import publish_build  # noqa: E402
from tests.test_publish_ignore_rules import (  # noqa: E402,F401  (the fixture is used by name)
    EVERY_FILE,
    TREE,
    _git,
    _tree_files,
    _write_tree,
    hostile_git_host,
)


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker Engine not available"),
]


@pytest.fixture()
def docker_client():
    return docker.from_env()


_LABELS = {"forge.role": "build-image"}  # what a caller must pass; these tests are not about it


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_build_tag_for_is_deterministic():
    assert build_tag_for("proj-1", "build-1") == "forge-build-proj-1-build-1"


def test_assemble_image_builds_from_pinned_dockerfile(tmp_path, docker_client):
    skeleton = tmp_path / "skeleton"
    # A pinned Dockerfile the agent must not be able to override.
    _write(skeleton / "Dockerfile", "FROM alpine:3.19\nCMD [\"echo\", \"pinned-image-ran\"]\n")

    workspace = tmp_path / "workspace"
    # Agent-produced app source, plus a tampered Dockerfile that must be
    # ignored — assemble_image always re-copies the pinned one.
    _write(workspace / "app.py", "print('hello')\n")
    _write(workspace / "Dockerfile", "FROM alpine:3.19\nCMD [\"echo\", \"AGENT TAMPERED\"]\n")

    tag = build_tag_for("proj-it", "build-it-1")
    try:
        result_tag = assemble_image(
            workspace_dir=str(workspace),
            template_skeleton_dir=str(skeleton),
            tag=tag,
            labels=_LABELS,
            docker_client=docker_client,
        )
        assert result_tag == tag

        image = docker_client.images.get(tag)
        assert image is not None

        output = docker_client.containers.run(tag, remove=True)
        assert b"pinned-image-ran" in output
        assert b"AGENT TAMPERED" not in output
    finally:
        try:
            docker_client.images.remove(tag, force=True)
        except ImageNotFound:
            pass


def test_assemble_image_raises_on_missing_pinned_dockerfile(tmp_path, docker_client):
    skeleton = tmp_path / "skeleton"  # no Dockerfile here
    skeleton.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ImageAssemblyError):
        assemble_image(
            workspace_dir=str(workspace),
            template_skeleton_dir=str(skeleton),
            tag=build_tag_for("proj-missing", "build-missing"),
            labels=_LABELS,
            docker_client=docker_client,
        )


def test_assemble_image_raises_on_broken_dockerfile(tmp_path, docker_client):
    skeleton = tmp_path / "skeleton"
    _write(skeleton / "Dockerfile", "THIS IS NOT VALID DOCKERFILE SYNTAX @@@\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ImageAssemblyError):
        assemble_image(
            workspace_dir=str(workspace),
            template_skeleton_dir=str(skeleton),
            tag=build_tag_for("proj-broken", "build-broken"),
            labels=_LABELS,
            docker_client=docker_client,
        )


# --- ADR 0032 §Amendment 2026-10-17: the image is built from the same files the commit holds ---

# The pinned Dockerfile of these tests copies the WHOLE build context, so what is in the image is
# exactly what Docker was given: a file Docker left out of the context shows up as a missing file.
# `FROM scratch` needs no registry; a container is only created, never started.
_COPY_EVERYTHING = "FROM scratch\nCOPY . /ws/\n"


def _write_exact(path, content: str) -> None:
    """The exact bytes: `Path.write_text` writes CRLF on Windows, and these tests compare bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def _image_files(docker_client, tag: str) -> dict[str, bytes]:
    """Every file under /ws in the image, name -> bytes."""
    container = docker_client.containers.create(tag, command=["/never-started"])
    try:
        stream, _stat = container.get_archive("/ws")
        data = b"".join(stream)
    finally:
        container.remove(v=True, force=True)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.isfile():
                files[member.name.removeprefix("ws/")] = archive.extractfile(member).read()
    return files


def _remove_image(docker_client, tag: str) -> None:
    try:
        docker_client.images.remove(tag, force=True)
    except ImageNotFound:
        pass


def test_a_dockerignore_in_the_tree_cannot_drop_a_file_from_the_image(tmp_path, docker_client):
    """The Docker-side twin of `.gitignore`: the agent writes `.dockerignore`, Docker leaves out
    what it lists, and the file the reviewer reads in the PR is absent from the service that runs."""
    skeleton = tmp_path / "skeleton"
    _write_exact(skeleton / "Dockerfile", _COPY_EVERYTHING)
    workspace = tmp_path / "workspace"
    _write_exact(workspace / "app" / "app.py", "print('app')\n")
    _write_exact(workspace / "app" / "guard.py", "print('the security check the PR shows')\n")
    _write_exact(workspace / ".dockerignore", "app/guard.py\n")

    # The control, bypassing `assemble_image`: the same tree built by Docker alone DOES lose the file.
    control_tag = build_tag_for("proj-caseah", "control")
    _write_exact(workspace / "Dockerfile", _COPY_EVERYTHING)
    tag = build_tag_for("proj-caseah", "ignored")
    try:
        docker_client.images.build(path=str(workspace), tag=control_tag, rm=True)
        assert "app/guard.py" not in _image_files(docker_client, control_tag)

        assemble_image(
            workspace_dir=str(workspace), template_skeleton_dir=str(skeleton), tag=tag, labels=_LABELS, docker_client=docker_client
        )
        files = _image_files(docker_client, tag)
    finally:
        _remove_image(docker_client, control_tag)
        _remove_image(docker_client, tag)

    assert files["app/guard.py"] == b"print('the security check the PR shows')\n"
    assert set(files) == {"Dockerfile", "app/app.py", "app/guard.py", ".dockerignore"}  # the file stays, emptied
    assert files[".dockerignore"] == b""


def test_the_files_in_the_published_commit_and_the_files_in_the_image_are_the_same_set(
    tmp_path, docker_client, hostile_git_host
):
    """The invariant itself, through the real steps in the executor's order: publish commits the
    harvested directory (a hostile host, the tree's own `.gitignore`, a `.dockerignore`), then
    image assembly builds from that directory. The two file sets are compared, not each against
    a list."""
    tree = {**TREE, ".dockerignore": "app/app.py\n*.secret\nignored-dir\nfrom-template.txt\n"}
    skeleton = tmp_path / "skeleton"
    _write_exact(skeleton / "Dockerfile", _COPY_EVERYTHING)
    workspace = tmp_path / "workspace"
    _write_tree(workspace, tree)
    provider = LocalGitProvider(root=str(tmp_path / "git-root"))
    repo = provider.create_repo(name="proj-caseah")
    tag = build_tag_for("proj-caseah", "sameset")

    try:
        publish_build(
            provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"),
            template_skeleton_dir=str(skeleton), protected_paths=["Dockerfile"], branch="forge-build-sameset",
            base_branch="main", pr_title="t", pr_body="b",
        )
        assemble_image(
            workspace_dir=str(workspace), template_skeleton_dir=str(skeleton), tag=tag, labels=_LABELS, docker_client=docker_client
        )
        image = _image_files(docker_client, tag)
    finally:
        _remove_image(docker_client, tag)

    committed = _tree_files(repo.clone_url, "refs/heads/forge-build-sameset")
    assert committed == set(image)
    assert committed == EVERY_FILE | {".dockerignore"}  # and neither side lost a file an ignore rule named
    assert image["app/hook.py"] == tree["app/hook.py"].encode()
    assert image["token.secret"] == tree["token.secret"].encode()
    # The commit holds the agent's `.dockerignore` as written.
    assert _git(repo.clone_url, "show", "refs/heads/forge-build-sameset:.dockerignore").stdout == tree[".dockerignore"]


def test_the_bytes_in_the_published_commit_and_the_bytes_in_the_image_are_the_same(
    tmp_path, docker_client, monkeypatch
):
    """ADR 0032 §Amendment 2026-10-28: the same files AND the same bytes. The tree's own
    `.gitattributes` asks for CRLF conversion, a host filter and an `$Id$` expansion, and the host
    sets `core.autocrlf=true`; the commit and the image must both hold the files as written."""
    from tests.test_publish_attributes import _committed

    config = tmp_path / "host-gitconfig"
    config.write_text(
        '[core]\n\tautocrlf = true\n[filter "x"]\n\tclean = tr a-z A-Z\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    tree = {
        ".gitattributes": b"*.txt text eol=crlf\n*.py filter=x\n*.id ident\n",
        "app/a.txt": b"one\r\ntwo\nthree\r\n",
        "app/b.py": b"print('lower case')\n",
        "c.id": b"$Id: 0123456789abcdef $\n",
    }
    skeleton = tmp_path / "skeleton"
    _write_exact(skeleton / "Dockerfile", _COPY_EVERYTHING)
    workspace = tmp_path / "workspace"
    for name, content in tree.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_bytes(content)
    provider = LocalGitProvider(root=str(tmp_path / "git-root"))
    repo = provider.create_repo(name="proj-caseao")
    tag = build_tag_for("proj-caseao", "samebytes")

    try:
        publish_build(
            provider=provider, repo=repo, workspace_dir=str(workspace), input_dir=str(tmp_path / "input"),
            template_skeleton_dir=str(skeleton), protected_paths=["Dockerfile"], branch="forge-build-samebytes",
            base_branch="main", pr_title="t", pr_body="b",
        )
        assemble_image(
            workspace_dir=str(workspace), template_skeleton_dir=str(skeleton), tag=tag, labels=_LABELS, docker_client=docker_client
        )
        image = _image_files(docker_client, tag)
    finally:
        _remove_image(docker_client, tag)

    committed = _committed(repo.clone_url, "refs/heads/forge-build-samebytes")
    assert committed == image  # every file, every byte
    assert {name: image[name] for name in tree} == tree  # and both are what the agent wrote


# --- every build image carries the ADR 0037 labels, built with forcerm ---


def test_a_real_build_image_carries_the_adr_0037_labels(tmp_path, docker_client):
    """Through the executor's own label construction (`platform_labels`), on a real build: the
    image the engine stores holds the instance, the role and both ids, and nothing else of ours."""
    import uuid

    from app.services.platform_labels import Role, platform_labels
    from tests.instance_scope import TEST_INSTANCE_ID

    run = uuid.uuid4().hex[:10]
    skeleton = tmp_path / "skeleton"
    _write_exact(skeleton / "Dockerfile", _COPY_EVERYTHING)
    workspace = tmp_path / "workspace"
    _write_exact(workspace / "app.py", "print('x')\n")
    tag = build_tag_for(f"proj-casear-{run}", f"build-casear-{run}")
    try:
        assemble_image(
            workspace_dir=str(workspace),
            template_skeleton_dir=str(skeleton),
            tag=tag,
            labels=platform_labels(Role.BUILD_IMAGE, build_id=f"build-casear-{run}", project_id=f"proj-casear-{run}"),
            docker_client=docker_client,
        )
        labels = docker_client.images.get(tag).labels
    finally:
        _remove_image(docker_client, tag)

    assert labels == {
        "forge.instance": TEST_INSTANCE_ID,
        "forge.role": "build-image",
        "forge.build_id": f"build-casear-{run}",
        "forge.project_id": f"proj-casear-{run}",
    }
