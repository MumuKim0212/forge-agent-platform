"""Unit tests for the modify-type base-ref staging
(`SandboxBuildExecutor._stage_workspace` / `_stage_modify_workspace`):
modify-type builds base on `Project.confirmed_ref` when set
, cloned from the project repo at
that exact ref. With no confirmed ref a modify build is refused, never staged empty.
Uses a local git repo as the "host" — no Docker, no
network (same approach as test_publish.py's local-bare-repo testing).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from app.models.build import Build
from app.models.project import Project
from app.services.git_provider import RepoRef
from app.services.sandbox_build_executor import NoConfirmedRefError, SandboxBuildExecutor
from app.services.status import BuildStatus, ProjectStatus


class LocalRepoProvider:
    """Provider double whose clone_url is a plain local path — `git clone`
    accepts it, so the staging path runs for real without any host."""

    def __init__(self, repo_path: str) -> None:
        self._repo_path = repo_path

    def create_repo(self, *, name: str) -> RepoRef:
        return RepoRef(full_name=f"local/{name}", clone_url=self._repo_path, default_branch="main")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def local_repo(tmp_path):
    """A local git repo with one commit; returns (path, commit_sha)."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "a@b.c"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "app").mkdir()
    (repo / "app" / "confirmed.py").write_text("print('confirmed code')\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "confirmed state"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return str(repo), sha


def _make_executor(provider=None):
    # A dummy docker client: staging never touches Docker; the ctor only
    # stores the client on the runner/preview-manager it builds.
    return SandboxBuildExecutor(docker_client=object(), git_provider=provider)


def _make_project_and_build(db, seeded_ids, *, confirmed_ref=None, build_type="modify"):
    project = Project(
        name="staging-project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
        confirmed_ref=confirmed_ref,
    )
    db.add(project)
    db.flush()
    build = Build(project_id=project.id, type=build_type, status=BuildStatus.RUNNING)
    db.add(build)
    db.commit()
    return project, build


def test_modify_build_stages_from_confirmed_ref(db_session, seeded_ids, local_repo, tmp_path):
    repo_path, sha = local_repo
    _, build = _make_project_and_build(db_session, seeded_ids, confirmed_ref=sha)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    executor = _make_executor(LocalRepoProvider(repo_path))
    executor._stage_workspace(build, str(workspace))

    assert (workspace / "app" / "confirmed.py").read_text() == "print('confirmed code')\n"
    # .git is dropped — publish re-inits a fresh repo from the harvest.
    assert not (workspace / ".git").exists()


@pytest.mark.parametrize("unset_ref", [None, ""], ids=["null", "empty-string"])
def test_modify_build_without_confirmed_ref_is_refused_not_staged_empty(
    db_session, seeded_ids, tmp_path, unset_ref
):
    """this used to return with the workspace empty (and the agent then rewrote the app
    from nothing, ending SUCCEEDED). It raises a named error now, and stages nothing."""
    _, build = _make_project_and_build(db_session, seeded_ids, confirmed_ref=unset_ref)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    executor = _make_executor()
    with pytest.raises(NoConfirmedRefError):
        executor._stage_workspace(build, str(workspace))

    assert os.listdir(workspace) == []


class _Network:
    def remove(self):
        pass


class _RecordingAdapter:
    def __init__(self) -> None:
        self.ran = False

    def run(self, adapter_input):
        self.ran = True
        raise AssertionError("the agent must not run for a build with nothing to modify")


def _executor_over_fakes(monkeypatch, adapter):
    """A real `execute` (real staging) over a fake proxy and network: no Docker."""
    from types import SimpleNamespace

    import app.services.sandbox_build_executor as sbe

    class _Proxy:
        base_url_for_sandbox = "http://proxy"

        def __init__(self, *a, **k):
            pass

        def start(self, **k):
            pass

        def issue_token(self, *a, **k):
            return "tok"

        def drain(self, token, *, timeout_seconds):
            return True

        def usage(self, token):
            return {
                "tokens": 0,
                "cost_usd": 0.0,
                "budget_exceeded": False,
                "policy_refusals": [],
                "unmetered": False,
                "unmetered_reason": None,
            }

        def revoke(self, *a):
            pass

        def stop(self):
            pass

    made: list[str] = []
    real_mkdtemp = sbe.tempfile.mkdtemp

    def _recording_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        made.append(path)
        return path

    monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
    monkeypatch.setattr(sbe.tempfile, "mkdtemp", _recording_mkdtemp)
    docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
    return SandboxBuildExecutor(docker_client=docker, adapter=adapter, git_provider=object()), made


def test_executing_a_modify_build_with_no_confirmed_ref_fails_it_with_a_named_reason(
    monkeypatch, db_session, make_build_with_request
):
    """Through the worker's own path: a modify build queued before the endpoint refused
    such requests ends FAILED with the platform's detail, the agent never runs, and the staged
    directory is removed. Planting the old `return` back in `_stage_modify_workspace` makes the
    agent run on an empty workspace, which this test catches (`ran`, and the status)."""
    from app.services.status import BuildFailureReason
    from app.worker import run_one_build

    adapter = _RecordingAdapter()
    executor, made = _executor_over_fakes(monkeypatch, adapter)
    build = make_build_with_request(build_type="modify")
    assert db_session.get(Project, build.project_id).confirmed_ref is None  # the premise
    build.status = BuildStatus.RUNNING
    db_session.commit()

    done = run_one_build(db_session, build, executor)

    assert adapter.ran is False
    assert done.status == BuildStatus.FAILED
    assert done.failure_reason == BuildFailureReason.INFRA_ERROR
    assert "no confirmed ref" in done.failure_detail
    assert "create request" in done.failure_detail  # it names the next step
    assert len(made) == 1 and not os.path.exists(made[0])  # the staged directory was removed


def test_create_build_still_stages_template_skeleton(db_session, seeded_ids, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "template_skeleton_dir", "templates/generic-webtool")
    _, build = _make_project_and_build(db_session, seeded_ids, build_type="create")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    executor = _make_executor()
    executor._stage_workspace(build, str(workspace))

    assert (workspace / "app" / "app.py").exists()


def test_clone_failure_error_never_contains_clone_url(tmp_path):
    """The clone URL can embed a provider token — a git failure must not
    leak it into the raised error."""
    secret_url = "https://x-access-token:ghp_secret_token@github.test/no/repo.git"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError) as exc_info:
        SandboxBuildExecutor._clone_at_ref(secret_url, "deadbeef", str(workspace))

    assert "ghp_secret_token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None  # no chained exception carrying argv


def test_create_staging_leaves_out_compiled_bytecode_even_when_the_skeleton_has_some(
    db_session, seeded_ids, tmp_path, monkeypatch
):
    """ADR 0032 §Amendment 2026-10-05: the host imports the template (the suite does),
    leaving `__pycache__` in it; none of it may reach a first commit."""
    from app.config import settings

    skeleton = tmp_path / "skeleton"
    for rel in (
        "app/app.py",
        "app/migrations/versions/m.py",
        "app/__pycache__/app.cpython-314.pyc",
        "app/migrations/versions/__pycache__/m.cpython-314.pyc",
        "app/stray.pyc",
        "app/STRAY2.PYO",
    ):
        path = skeleton / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    monkeypatch.setattr(settings, "template_skeleton_dir", str(skeleton))
    _, build = _make_project_and_build(db_session, seeded_ids, build_type="create")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _make_executor()._stage_workspace(build, str(workspace))

    staged = sorted(
        os.path.relpath(os.path.join(d, f), workspace).replace(os.sep, "/")
        for d, _dirs, files in os.walk(workspace)
        for f in files
    )
    assert staged == ["app/app.py", "app/migrations/versions/m.py"]
    assert not any(d == "__pycache__" for _d, dirs, _f in os.walk(workspace) for d in dirs)


# -- L2's own git in the build path is bounded --------------------------------------


def test_clone_from_a_host_that_never_answers_times_out_as_the_same_error_a_failed_clone_raises(
    tmp_path, monkeypatch, silent_git_remote, returns_within
):
    """`_clone_at_ref` used to be a plain `subprocess.run` with no timeout: a git host that accepts
    the connection and then says nothing held the worker -- one build at a time -- forever. Real
    git against a server that goes quiet; the step's own `RuntimeError` (the worker's existing
    INFRA_ERROR path), the limit named, no credential, nothing chained."""
    import app.services.publish as publish_module

    monkeypatch.setattr(publish_module, "GIT_TIMEOUT_SECONDS", 1.0)
    url = silent_git_remote.replace("http://", "http://x-access-token:ghs_SECRETTOKENVALUE123@") + "/org/p.git"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError) as raised:
        returns_within(20, lambda: SandboxBuildExecutor._clone_at_ref(url, "deadbeef", str(workspace)))

    assert "staging workspace at confirmed ref deadbeef failed (git clone)" in str(raised.value)
    assert "timed out after 1s" in str(raised.value)
    assert "ghs_SECRETTOKENVALUE123" not in str(raised.value)
    assert url not in str(raised.value)
    assert raised.value.__cause__ is None  # the exception whose argv carries the URL is not chained


def test_clone_git_never_asks_a_terminal_for_a_password(tmp_path, monkeypatch):
    """`GIT_TERMINAL_PROMPT=0` reaches every git `_clone_at_ref` starts, and the runner is the
    bounded one (`publish.run_git_process`), so a credential question fails the command."""
    import app.services.sandbox_build_executor as sbe

    seen: list[dict] = []

    def fake_run_git_process(args, *, cwd=None, env=None, input=None, check=False):
        seen.append({"args": args, "env": env, "check": check})
        raise subprocess.CalledProcessError(128, args, output="", stderr="fatal: could not read Username")

    monkeypatch.setattr(sbe, "run_git_process", fake_run_git_process)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError):
        SandboxBuildExecutor._clone_at_ref("https://example.invalid/org/p.git", "deadbeef", str(workspace))

    (call,) = seen  # the clone failed, so the checkout was never attempted
    assert call["args"][:2] == ["git", "clone"]
    assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert call["check"] is True
