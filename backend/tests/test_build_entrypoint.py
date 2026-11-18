"""a build whose output has no app
entrypoint FAILS (`MISSING_ENTRYPOINT`) instead of succeeding.

Two seams, no Docker:
  * `service_entrypoint` itself, against fixture templates written to `tmp_path`: the
    required path is read from the template's Dockerfile, so changing the template changes
    what is required;
  * the executor, through the `Harness` of the harvest tests, which records every
    outward call (git publish, image assembly, policy gate) and here also the preview start.
"""

from __future__ import annotations

import os

import pytest

from app.config import settings
from app.services.build_queue import mark_failed
from app.services.service_entrypoint import (
    EntrypointUndeterminable,
    missing_entrypoint,
    service_entrypoint_paths,
)
from app.services.status import BuildFailureReason
from tests.harvest_support import make_output_dir
from tests.test_sandbox_build_executor_harvest import Harness

# A build's shape: protected manifests and a package marker, no app module.
PILOT_SHAPE = {
    "Dockerfile": b"FROM x\n",
    "compose.yaml": b"services: {}\n",
    "app/__init__.py": b"",
}
WITH_ENTRYPOINT = {**PILOT_SHAPE, "app/app.py": b"app = 1\n"}


def _template(tmp_path, *, workdir="/srv", copy="COPY src/ /srv/", cmd='CMD ["sh", "-c", "uvicorn {mod}:api --host 0.0.0.0 --port 8000"]', mod="server"):
    """A fixture template whose service lives somewhere the real template does not."""
    root = tmp_path / "tpl"
    root.mkdir(exist_ok=True)
    lines = ["FROM python:3.11-slim", f"WORKDIR {workdir}", copy, cmd.format(mod=mod)]
    (root / "Dockerfile").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return str(root)


# --- the path is read from the template -------------------------------------------------


def test_the_real_template_requires_its_app_module():
    assert service_entrypoint_paths(settings.template_skeleton_dir) == ["app/app.py", "app/app/__init__.py"]


def test_a_renamed_module_changes_what_is_required(tmp_path):
    assert service_entrypoint_paths(_template(tmp_path, mod="server")) == ["src/server.py", "src/server/__init__.py"]
    assert service_entrypoint_paths(_template(tmp_path, mod="main")) == ["src/main.py", "src/main/__init__.py"]


def test_a_different_copy_and_workdir_change_it_too(tmp_path):
    template = _template(tmp_path, workdir="/opt/svc", copy="COPY code/ ./", mod="entry")
    assert service_entrypoint_paths(template) == ["code/entry.py", "code/entry/__init__.py"]


def test_a_dotted_module_is_a_nested_path(tmp_path):
    assert service_entrypoint_paths(_template(tmp_path, mod="pkg.web"))[0] == "src/pkg/web.py"


def test_shell_form_cmd_and_a_bind_address_are_read_correctly(tmp_path):
    template = _template(tmp_path, cmd="CMD uvicorn --host 0.0.0.0:8000 {mod}:api --port 8000", mod="server")
    assert service_entrypoint_paths(template)[0] == "src/server.py"


def test_a_check_against_the_template_passes_and_fails_with_the_file(tmp_path):
    template = _template(tmp_path, mod="server")
    present = tmp_path / "present"
    (present / "src").mkdir(parents=True)
    (present / "src" / "server.py").write_text("x", encoding="utf-8")
    absent = tmp_path / "absent"
    (absent / "src").mkdir(parents=True)
    (absent / "src" / "app.py").write_text("the REAL template's name, not this template's", encoding="utf-8")

    assert missing_entrypoint(str(present), template) is None
    assert missing_entrypoint(str(absent), template) == ["src/server.py", "src/server/__init__.py"]


def test_a_directory_in_place_of_the_file_is_not_the_file(tmp_path):
    template = _template(tmp_path, mod="server")
    workspace = tmp_path / "ws"
    (workspace / "src" / "server.py").mkdir(parents=True)

    assert missing_entrypoint(str(workspace), template) is not None


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM x\nWORKDIR /app\nCOPY app/ /app/\n",  # no CMD
        "FROM x\nWORKDIR /app\nCOPY app/ /app/\nCMD [\"python\", \"-m\", \"http.server\"]\n",  # no module:attribute
        "FROM x\nWORKDIR /app\nCOPY other/ /other/\nCMD uvicorn app:app\n",  # the module is not copied in
        "FROM x\nWORKDIR /app\nCOPY --from=build /out/ /app/\nCMD uvicorn app:app\n",  # another stage's files
    ],
)
def test_a_template_that_does_not_say_is_undeterminable(tmp_path, dockerfile):
    root = tmp_path / "tpl"
    root.mkdir()
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")

    with pytest.raises(EntrypointUndeterminable):
        service_entrypoint_paths(str(root))


def test_a_template_with_no_dockerfile_is_undeterminable(tmp_path):
    with pytest.raises(EntrypointUndeterminable):
        service_entrypoint_paths(str(tmp_path / "nowhere"))


def test_only_the_templates_dockerfile_is_read_never_the_workspaces(tmp_path):
    """An agent can rewrite the workspace's Dockerfile to start any module it likes."""
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "evil.py").write_text("x", encoding="utf-8")
    (workspace / "Dockerfile").write_text("FROM x\nWORKDIR /srv\nCOPY src/ /srv/\nCMD uvicorn evil:api\n", encoding="utf-8")

    assert missing_entrypoint(str(workspace), _template(tmp_path, mod="server")) is not None


# --- the executor ------------------------------------------------------------------------


class _Preview:
    def __init__(self, h: Harness) -> None:
        self.starts: list[str] = []
        h.executor._start_preview = lambda build: self.starts.append(build.id) or ""


def _run(monkeypatch, make_build_with_request, *, files, build_type="create", staged=None, **harness_kwargs):
    h = Harness(monkeypatch, output_files=files, staged_files=staged, **harness_kwargs)
    preview = _Preview(h)
    build = make_build_with_request(build_type=build_type)
    return h, preview, build, h.executor.execute(build)


def _nothing_assembled(h: Harness, preview: _Preview, build) -> None:
    assert h.publish_calls == []
    assert h.assemble_calls == []
    assert h.gate_calls == []
    assert preview.starts == []
    assert build.diff_ref is None and build.image_ref is None


def test_a_build_without_the_entrypoint_fails_and_assembles_nothing(monkeypatch, make_build_with_request):
    h, preview, build, result = _run(monkeypatch, make_build_with_request, files=PILOT_SHAPE)

    assert result.ok is False
    assert result.reason == BuildFailureReason.MISSING_ENTRYPOINT == "MISSING_ENTRYPOINT"
    _nothing_assembled(h, preview, build)
    assert h.sanitise_calls  # it got as far as the sanitising copy, and no further
    h.assert_every_temp_tree_is_gone(expect_publish_dir=True)


def test_the_failure_says_what_is_wrong_and_names_the_templates_path_only(
    monkeypatch, make_build_with_request, db_session
):
    files = {**PILOT_SHAPE, "app/AGENT-CHOSEN-NAME-77aa.py": b"x\n"}
    _h, _preview, build, result = _run(monkeypatch, make_build_with_request, files=files)

    mark_failed(db_session, build, reason=result.reason, detail=result.summary)

    assert build.failure_reason == "MISSING_ENTRYPOINT"
    assert "app/app.py" in build.failure_detail
    assert "entrypoint" in build.failure_detail
    assert "worker log" not in build.failure_detail
    assert "AGENT-CHOSEN-NAME-77aa" not in build.failure_detail


def test_a_modify_build_that_deleted_the_entrypoint_fails_the_same_way(monkeypatch, make_build_with_request):
    staged = {**WITH_ENTRYPOINT, "app/alembic.ini": b"[alembic]\n"}
    deleted = {k: v for k, v in staged.items() if k != "app/app.py"}
    h, preview, build, result = _run(
        monkeypatch, make_build_with_request, files=deleted, staged=staged, build_type="modify"
    )

    assert result.ok is False and result.reason == BuildFailureReason.MISSING_ENTRYPOINT
    _nothing_assembled(h, preview, build)
    # The change set still records what the agent did (it is measured before the check).
    assert {"path": "app/app.py", "change": "deleted"} in build.changed_files["entries"]


def test_a_build_with_the_entrypoint_behaves_as_before(monkeypatch, make_build_with_request):
    h, preview, build, result = _run(monkeypatch, make_build_with_request, files=WITH_ENTRYPOINT)

    assert result.ok is True, result.summary
    assert len(h.publish_calls) == len(h.assemble_calls) == len(h.gate_calls) == 1
    assert preview.starts == [build.id]


def test_a_package_entrypoint_satisfies_it(monkeypatch, make_build_with_request):
    files = {**PILOT_SHAPE, "app/app/__init__.py": b"app = 1\n"}
    _h, _preview, _build, result = _run(monkeypatch, make_build_with_request, files=files)

    assert result.ok is True, result.summary


def test_the_executor_follows_the_template_not_a_literal(monkeypatch, make_build_with_request, tmp_path):
    """The same output passes or fails by what the TEMPLATE's Dockerfile starts."""
    monkeypatch.setattr(settings, "template_skeleton_dir", _template(tmp_path, mod="server"))

    h, preview, build, result = _run(monkeypatch, make_build_with_request, files=WITH_ENTRYPOINT)
    assert result.ok is False and result.reason == BuildFailureReason.MISSING_ENTRYPOINT
    _nothing_assembled(h, preview, build)

    renamed = {**PILOT_SHAPE, "src/server.py": b"api = 1\n"}
    _h, _preview, _build, result = _run(monkeypatch, make_build_with_request, files=renamed)
    assert result.ok is True, result.summary


def test_a_template_with_no_entrypoint_fails_the_build_as_infrastructure(monkeypatch, make_build_with_request, tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM x\nCMD [\"true\"]\n", encoding="utf-8")
    monkeypatch.setattr(settings, "template_skeleton_dir", str(root))

    h, preview, build, result = _run(monkeypatch, make_build_with_request, files=WITH_ENTRYPOINT)

    assert result.ok is False and result.reason == BuildFailureReason.INFRA_ERROR
    assert "entrypoint" in result.summary
    _nothing_assembled(h, preview, build)


def test_other_publish_failures_keep_their_reason(monkeypatch, make_build_with_request):
    from app.services.publish import PublishError

    h = Harness(monkeypatch, output_files=WITH_ENTRYPOINT)
    h.publish_error = PublishError("push refused")
    _Preview(h)
    build = make_build_with_request()

    result = h.executor.execute(build)

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    assert h.assemble_calls == []


def test_the_sanitised_copy_is_what_is_checked(monkeypatch, make_build_with_request):
    """The entrypoint is a link in the harvested tree: the sanitising copy refuses the link
    first (HARVEST_ERROR), so a link can never stand in for the file."""

    def _link(root: str) -> None:
        os.remove(os.path.join(root, "app", "app.py"))
        os.link(os.path.join(root, "Dockerfile"), os.path.join(root, "app", "app.py"))

    h, preview, build, result = _run(
        monkeypatch, make_build_with_request, files=WITH_ENTRYPOINT, shape_output=_link
    )

    assert result.ok is False and result.reason == BuildFailureReason.HARVEST_ERROR
    _nothing_assembled(h, preview, build)


def test_make_output_dir_is_the_real_one_the_harness_uses():
    """Guards the fixture: `Harness` writes the files it is given, so a test that leaves the
    entrypoint out really does hand the executor a tree without it."""
    root = make_output_dir(PILOT_SHAPE)
    try:
        assert not os.path.exists(os.path.join(root, "app", "app.py"))
        assert os.path.isfile(os.path.join(root, "app", "__init__.py"))
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

