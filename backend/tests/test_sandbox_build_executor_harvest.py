"""ADR 0032: the executor publishes the agent's HARVESTED output,
through a sanitising copy, and removes every temporary tree it made.

Unit seam, no Docker: `SandboxBuildExecutor.execute` is driven with a fake
`ProxyContainer`, a fake adapter that returns a real `output_dir` on disk, and a
staged directory the test fills with a recognisably different content. The
publish step runs for real up to its outward calls: `publish_build` and
`assemble_image` are replaced by recorders that snapshot the directory they are
handed (it is deleted when `execute` returns, so a path alone proves nothing),
and the policy gate is the real one, wrapped by a recorder.

The real-runner, real-image, real-git version of the same claims is
`test_sandbox_build_executor_integration.py` (engine-up).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.models.policy_verdict import PolicyVerdict
from app.services import sandbox_build_executor as sbe
from app.services.agent_adapter import AgentAdapterResult
from app.services.publish import PublishError
from app.services.status import BuildFailureReason
from tests.harvest_support import make_output_dir

STAGED = {"app/app.py": b"STAGED-ORIGINAL\n", "app/staged_only.py": b"staged only\n"}
AGENT = {
    "app/app.py": b"AGENT-EDITED\n",
    "app/agent_marker.txt": b"AGENT-MARKER\n",
    "app/agent_new.py": b"print('x')\n",
    # What the platform and the agent's own git metadata must never carry across:
    ".forge/result.json": b'{"ok": true}',
    ".forge/task_input.json": b'{"system_prompt": "PLATFORM-PROMPT"}',
    ".forge/agent_runner.py": b"# runner",
    ".git/HEAD": b"ref: refs/heads/main\n",
    ".git/hooks/pre-commit": b"#!/bin/sh\nexit 0\n",
    "vendor/lib/.git": b"gitdir: ../../elsewhere\n",
    "vendor/lib/code.py": b"code",
}


def _snapshot(root: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = f.read()
    return out


class _Network:
    def remove(self):
        pass


class Harness:
    """One executor over fakes, recording the directories it touches."""

    def __init__(
        self,
        monkeypatch,
        *,
        output_files=AGENT,
        adapter_result=None,
        usage=None,
        make_output=True,
        shape_output=None,
        git_provider=None,
        staged_files=None,
    ):
        """`git_provider`: when given, the REAL `publish_build` runs (against that
        provider) instead of the recorder, and the repo comes from the provider.
        `staged_files`: what the stubbed staging puts in the input
        directory; default `STAGED`."""
        self.staged_dir: str | None = None
        self.output_dir: str | None = None
        self.sanitise_calls: list[tuple[str, str]] = []
        self.publish_calls: list[dict] = []
        self.assemble_calls: list[dict] = []
        self.gate_calls: list[dict] = []
        self.publish_error: Exception | None = None
        self.publish_drift: list[str] = []  # what the recorder reports restore found
        harness = self

        class _Proxy:
            base_url_for_sandbox = "http://proxy"

            def __init__(self, *a, **k):
                pass

            def start(self, **k):
                pass

            def issue_token(self, *a, **k):
                return "tok"

            def drain(self, token, *, timeout_seconds):
                return True  # nothing of the token is still running

            def usage(self, token):
                return usage or {
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

        class _Adapter:
            def run(self, adapter_input):
                harness.staged_dir = adapter_input.workspace_dir
                if make_output:
                    harness.output_dir = make_output_dir(output_files)
                    if shape_output is not None:
                        shape_output(harness.output_dir)
                base = adapter_result or AgentAdapterResult(ok=True, summary="agent summary")
                import dataclasses

                return dataclasses.replace(base, output_dir=harness.output_dir)

        def _stage(build, workspace_dir):
            for rel, data in (STAGED if staged_files is None else staged_files).items():
                path = os.path.join(workspace_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)

        real_sanitise = sbe.sanitise_harvest

        def _sanitise(src, dst, **k):
            harness.sanitise_calls.append((src, dst))
            return real_sanitise(src, dst, **k)

        def _publish_build(**kwargs):
            harness.publish_calls.append(
                {
                    **kwargs,
                    "files": _snapshot(kwargs["workspace_dir"]),
                    "input_files": _snapshot(kwargs["input_dir"]),  # it must still be there
                }
            )
            if harness.publish_error is not None:
                raise harness.publish_error
            return SimpleNamespace(diff_ref="forge-build-x#1", drift=list(harness.publish_drift), branch="b", pr=None)

        def _assemble_image(**kwargs):
            harness.assemble_calls.append({**kwargs, "files": _snapshot(kwargs["workspace_dir"])})
            return "tag"

        real_gate = sbe.run_and_persist_policy_gate

        def _gate(session, build, **kwargs):
            harness.gate_calls.append({**kwargs, "files": _snapshot(kwargs["workspace_dir"])})
            return real_gate(session, build, **kwargs)

        docker = SimpleNamespace(networks=SimpleNamespace(create=lambda *a, **k: _Network()))
        monkeypatch.setattr(sbe, "ProxyContainer", _Proxy)
        monkeypatch.setattr(sbe, "sanitise_harvest", _sanitise)
        if git_provider is None:
            monkeypatch.setattr(sbe, "publish_build", _publish_build)
        monkeypatch.setattr(sbe, "assemble_image", _assemble_image)
        monkeypatch.setattr(sbe, "run_and_persist_policy_gate", _gate)
        self.executor = sbe.SandboxBuildExecutor(docker_client=docker, adapter=_Adapter(), git_provider=git_provider or object())
        monkeypatch.setattr(self.executor, "_stage_workspace", _stage)
        if git_provider is None:
            monkeypatch.setattr(self.executor, "_ensure_repo", lambda *a, **k: SimpleNamespace(full_name="o/p"))
        monkeypatch.setattr(self.executor, "_base_branch_for", lambda *a, **k: "main")
        monkeypatch.setattr(self.executor, "_start_preview", lambda build: "")

    @property
    def publish_dir(self) -> str | None:
        return self.sanitise_calls[-1][1] if self.sanitise_calls else None

    def assert_every_temp_tree_is_gone(self, *, expect_publish_dir: bool) -> None:
        assert self.staged_dir is not None and self.output_dir is not None
        assert not os.path.exists(self.staged_dir), "staged directory leaked"
        assert not os.path.exists(self.output_dir), "harvested directory leaked"
        if expect_publish_dir:
            assert self.publish_dir is not None and not os.path.exists(self.publish_dir), "sanitised directory leaked"
        else:
            assert self.sanitise_calls == []


# --- (b1) publish, image assembly and the gate receive the sanitised directory ----


def test_publish_image_and_gate_all_get_the_sanitised_agent_output(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert len(h.publish_calls) == len(h.assemble_calls) == len(h.gate_calls) == 1
    dirs = {h.publish_calls[0]["workspace_dir"], h.assemble_calls[0]["workspace_dir"], h.gate_calls[0]["workspace_dir"]}
    assert len(dirs) == 1, "all three must see the same tree"
    (published,) = dirs
    assert published == h.publish_dir
    # Neither the staged input nor the harvested directory itself.
    assert published not in (h.staged_dir, h.output_dir)
    assert os.path.basename(published).startswith("forge-publish-")

    expected = {
        "app/app.py": b"AGENT-EDITED\n",
        "app/agent_marker.txt": b"AGENT-MARKER\n",
        "app/agent_new.py": b"print('x')\n",
        "vendor/lib/code.py": b"code",
    }
    for seen in (h.publish_calls[0]["files"], h.assemble_calls[0]["files"], h.gate_calls[0]["files"]):
        assert seen == expected  # the agent's bytes: its edit, its new files...
        assert b"STAGED-ORIGINAL\n" not in seen.values()  # ...not the staged template
        assert "app/staged_only.py" not in seen
        # ...and none of the platform's channel or the agent's git metadata.
        assert not any(p == ".forge" or p.startswith(".forge/") for p in seen)
        assert not any(".git" in p.split("/") for p in seen)


def test_the_sanitising_copy_is_made_from_the_adapters_output_dir(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch)

    h.executor.execute(make_build_with_request())

    assert h.sanitise_calls == [(h.output_dir, h.publish_dir)]
    assert h.output_dir != h.staged_dir


def test_the_real_gate_ran_over_the_agent_s_bytes(monkeypatch, make_build_with_request, db_session):
    """A secret in the AGENT's output (not in the staged input) is what the gate
    flags: the verdict is about the build's output."""
    files = {**AGENT, "app/leaked.py": b"AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n"}
    h = Harness(monkeypatch, output_files=files)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True
    verdict = db_session.query(PolicyVerdict).filter_by(build_id=build.id).one()
    assert verdict.risk_tier == "high"


# --- (b2) every temporary tree is removed, on every exit ---------------------------


def test_every_temp_tree_is_removed_after_success(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch)

    result = h.executor.execute(make_build_with_request())

    assert result.ok is True
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


def test_every_temp_tree_is_removed_after_an_adapter_failure(monkeypatch, make_build_with_request):
    h = Harness(
        monkeypatch,
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT, summary="L3 text"),
    )

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == BuildFailureReason.TIMEOUT
    # The sandbox produced a harvest even though the run failed; it is removed,
    # and nothing was sanitised or published from it.
    h.assert_every_temp_tree_is_gone(expect_publish_dir=False)
    assert h.publish_calls == []


def _add_hard_link(root: str) -> None:
    """A hard link pair in the harvested directory (real, so it works on a
    Windows host without the symlink privilege)."""
    os.link(os.path.join(root, "app", "agent_marker.txt"), os.path.join(root, "app", "SECRET-NAME-L3-CHOSE"))


def test_every_temp_tree_is_removed_after_an_unsafe_harvest_entry(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch, shape_output=_add_hard_link)

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


def test_every_temp_tree_is_removed_after_a_publish_exception(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch)
    h.publish_error = PublishError("git push failed: remote said no")

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert len(h.publish_calls) == 1  # it was reached, and it raised
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


def test_every_temp_tree_is_removed_when_the_publish_step_raises_something_unexpected(
    monkeypatch, make_build_with_request
):
    h = Harness(monkeypatch)
    h.publish_error = RuntimeError("not one of the errors _publish catches")

    with pytest.raises(RuntimeError):
        h.executor.execute(make_build_with_request())

    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


# --- (b3) an unsafe entry refuses the build; nothing is published ------------------


def test_an_unsafe_entry_fails_the_build_with_a_platform_template_detail_and_publishes_nothing(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(monkeypatch, shape_output=_add_hard_link)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is False
    assert result.reason == BuildFailureReason.HARVEST_ERROR == "HARVEST_ERROR"
    assert result.summary == (
        "the agent's output contains entries that cannot be published "
        "(2 hard link(s)); links and special files are not allowed"
    )
    assert "SECRET-NAME-L3-CHOSE" not in result.summary
    assert h.publish_calls == [] and h.assemble_calls == [] and h.gate_calls == []  # no push, image or verdict
    assert build.diff_ref is None and build.image_ref is None
    assert db_session.query(PolicyVerdict).filter_by(build_id=build.id).count() == 0


def test_a_copy_failure_is_a_harvest_error_naming_only_the_exception_class(monkeypatch, make_build_with_request):
    h = Harness(monkeypatch)

    def _boom(src, dst, **k):
        raise OSError("C:\\secret\\path\\the-l3-chose: disk on fire")

    monkeypatch.setattr(sbe, "sanitise_harvest", _boom)

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert result.summary == (
        "harvesting the build's output failed at sanitising the output (OSError); see the worker log"
    )
    assert "disk on fire" not in result.summary and "secret" not in result.summary
    assert h.publish_calls == []


def test_a_success_with_no_harvested_directory_fails_closed(monkeypatch, make_build_with_request):
    """Never fall back to publishing the staged directory."""
    h = Harness(monkeypatch, make_output=False)

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert result.summary == "the sandbox run produced no harvested output to publish"
    assert h.publish_calls == [] and h.sanitise_calls == []
    assert h.staged_dir is not None and not os.path.exists(h.staged_dir)


# --- precedence: every earlier failure still wins, and still cleans up --------------


@pytest.mark.parametrize(
    "usage, expected_reason",
    [
        ({"policy_refusals": ["tool policy: request field 'mcp_servers' is not allowed"]}, "AGENT_ERROR"),
        ({"budget_exceeded": True}, "BUDGET_EXCEEDED"),
        ({"unmetered": True, "unmetered_reason": "no usage"}, "BUDGET_EXCEEDED"),
    ],
)
def test_earlier_failures_win_before_anything_is_sanitised_and_the_harvest_is_removed(
    monkeypatch, make_build_with_request, usage, expected_reason
):
    base = {
        "tokens": 1,
        "cost_usd": 0.0,
        "budget_exceeded": False,
        "policy_refusals": [],
        "unmetered": False,
        "unmetered_reason": None,
    }
    h = Harness(monkeypatch, usage={**base, **usage})

    result = h.executor.execute(make_build_with_request())

    assert result.ok is False and result.reason == expected_reason
    h.assert_every_temp_tree_is_gone(expect_publish_dir=False)
    assert h.publish_calls == []


# --- a complete repository under any spelling of .git ---------------


def _plant_hostile_repo(dirname: str):
    """A shape_output hook that puts a COMPLETE repository (made by `git init`) with
    a `pre-commit` hook and a hostile config at `<output>/<dirname>`."""
    import subprocess

    def shape(root: str) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
        git = os.path.join(root, ".git")
        hook = os.path.join(git, "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as f:
            f.write('#!/bin/sh\n[ -n "$FORGE_TEST_HOOK_MARKER" ] && echo ran > "$FORGE_TEST_HOOK_MARKER"\nexit 0\n')
        os.chmod(hook, 0o755)
        with open(os.path.join(git, "config"), "a", newline="\n") as f:
            f.write("[core]\n\tfsmonitor = echo hostile\n[user]\n\tname = Evil\n\temail = evil@example.invalid\n")
        if dirname != ".git":
            os.rename(git, os.path.join(root, dirname))

    return shape


@pytest.mark.parametrize("dirname", [".git", ".GIT", ".Git"])
def test_a_complete_repository_in_the_agent_s_output_never_reaches_host_git(
    monkeypatch, make_build_with_request, tmp_path, dirname
):
    """Through the real executor, real sanitiser,
    real publish and a real local bare repo: the host marker is absent, the pushed
    commit is authored by Forge Bot and holds the agent's files, and no
    `forge-gitdir-*` / `forge-publish-*` tree is left in the temp dir."""
    import subprocess
    import tempfile

    from tests.test_publish import LocalBareRepoProvider, _init_bare_repo

    marker = tmp_path / "hook-ran-on-the-host"
    monkeypatch.setenv("FORGE_TEST_HOOK_MARKER", str(marker))
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    bare = tmp_path / "remote.git"
    _init_bare_repo(bare)
    provider = LocalBareRepoProvider(bare)
    h = Harness(monkeypatch, shape_output=_plant_hostile_repo(dirname), git_provider=provider)
    monkeypatch.setattr(sbe, "assemble_image", lambda **k: "tag")
    monkeypatch.setattr(h.executor, "_start_preview", lambda build: "")
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is True, result.summary
    assert not marker.exists(), "host git ran a hook that came out of the sandbox"
    sha = provider.resolve_ref(repo=provider.create_repo(name="x"), branch=f"forge-build-{build.id}")
    git = lambda *a: subprocess.run(["git", *a], cwd=bare, check=True, capture_output=True, text=True).stdout  # noqa: E731
    assert git("log", "-1", "--format=%an <%ae>", sha).strip() == "Forge Bot <forge-bot@forge.local>"
    paths = git("ls-tree", "-r", "--name-only", sha).split()
    assert "app/agent_marker.txt" in paths
    assert not any(p.casefold().startswith(".git") and p.casefold() not in (".gitignore",) for p in paths), paths
    assert os.listdir(systemp) == []  # staged, harvested, sanitised AND the platform repo are all gone
