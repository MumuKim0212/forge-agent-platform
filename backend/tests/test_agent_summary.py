"""the agent's own summary is persisted on a SUCCEEDED build, bounded, as received,
and is never an input to any decision.

- (a) backend: what is stored, when, and that the failure path carries no agent text;
- (a5) an `ast` source scan: `agent_summary` is mentioned exactly as often as expected in each file (an exact count, not an allowlist), and no whole-object serialisation sits outside the serialisation layer;
- (a6) the migration preserves a row both ways;
- (b) the API: the timeline build entry and `GET /approvals/{id}` carry it, the list does not.

Unit seam, no Docker: the `Harness` of the harvest tests (the executor over fakes).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.models.build import Build
from app.models.change_request import ChangeRequest
from app.models.policy_verdict import PolicyVerdict
from app.models.project import Project
from app.models.user import User
from app.schema_check import alembic_config
from app.services.agent_adapter import AgentAdapterResult
from app.services.approval import create_approval_request_for_verdict
from app.services.build_queue import (
    AGENT_SUMMARY_MAX_CHARS,
    AGENT_SUMMARY_TRUNCATION_MARKER,
    mark_succeeded,
)
from app.services.policy_checks import PolicyRiskTier
from app.services.preview_manager import PreviewStartError
from app.services.status import BuildFailureReason, BuildStatus, ProjectStatus
from app.worker import run_one_build
from tests.test_sandbox_build_executor_harvest import Harness
from tests.test_sandbox_build_executor_schema import INPUT, SCHEMA_CHANGES

AGENT_TEXT = (
    "I added the inventory page.\nSecond line, with a [link](https://evil.example) and https://evil.example/x."
)


# --- (a1) a successful build persists exactly the adapter's text -------------------


def test_a1_a_successful_build_persists_the_agents_text_with_no_platform_sentence(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=AGENT_TEXT))
    h.publish_drift = ["Dockerfile"]  # what the old drift sentence was built from
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.SUCCEEDED
    assert done.agent_summary == AGENT_TEXT  # byte for byte: nothing appended, nothing stripped
    assert done.protected_path_drift == ["Dockerfile"]  # the drift is its own fact, not text


def test_a1_a_failed_preview_start_is_no_preview_link_not_a_sentence_in_the_summary(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=AGENT_TEXT))
    # Back to the real method, over a preview manager that fails. Through `monkeypatch`, not a bare `del`:
    # the Harness set this name with `monkeypatch.setattr`, and pytest 9.2 undoes that by `delattr` (the
    # name was not in the instance `__dict__`), which raises if the test already removed it -- and the
    # raise stops `undo()` part-way, leaving every earlier patch applied for the rest of the session.
    monkeypatch.delattr(h.executor, "_start_preview")

    def _boom(**k):
        raise PreviewStartError("docker said no")

    h.executor._preview_manager = SimpleNamespace(start_preview=_boom)
    build = make_build_with_request()

    done = run_one_build(db_session, build, h.executor)

    assert done.status == BuildStatus.SUCCEEDED
    assert done.agent_summary == AGENT_TEXT
    assert done.preview_url is None


def test_a1_the_text_is_stored_as_received_no_stripping_no_escaping(
    monkeypatch, make_build_with_request, db_session
):
    raw = "  \n<script>alert(1)</script> \u202e override \x07 bell\n  "
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=raw))

    done = run_one_build(db_session, make_build_with_request(), h.executor)

    assert done.agent_summary == raw


@pytest.mark.parametrize("given", [None, ""])
def test_a1_no_text_stores_null(monkeypatch, make_build_with_request, db_session, given):
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=given))

    done = run_one_build(db_session, make_build_with_request(), h.executor)

    assert done.status == BuildStatus.SUCCEEDED and done.agent_summary is None


def test_a4_the_pr_body_is_still_the_agents_text(monkeypatch, make_build_with_request, db_session):
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=AGENT_TEXT))
    h.publish_drift = ["Dockerfile"]
    run_one_build(db_session, make_build_with_request(), h.executor)
    assert h.publish_calls[0]["pr_body"] == AGENT_TEXT

    h2 = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=None))
    run_one_build(db_session, make_build_with_request(), h2.executor)
    assert h2.publish_calls[0]["pr_body"] == "(no agent summary)"


# --- (a2) a failed build persists NULL; its failure detail is not this task's --------


def test_a2_an_adapter_failure_persists_null_and_the_failure_detail_is_platform_text(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(
        monkeypatch,
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT, summary=AGENT_TEXT),
    )

    done = run_one_build(db_session, make_build_with_request(), h.executor)

    assert done.status == BuildStatus.FAILED and done.failure_reason == BuildFailureReason.TIMEOUT
    assert done.agent_summary is None
    assert "inventory page" not in done.failure_detail and "evil.example" not in done.failure_detail


def test_a2_a_schema_refusal_persists_null_and_names_no_agent_text(
    monkeypatch, make_build_with_request, db_session
):
    h = Harness(
        monkeypatch,
        output_files=SCHEMA_CHANGES["adds-a-migration"],
        staged_files=INPUT,
        adapter_result=AgentAdapterResult(ok=True, summary=AGENT_TEXT),
    )

    done = run_one_build(db_session, make_build_with_request(build_type="modify"), h.executor)

    assert done.status == BuildStatus.FAILED and done.failure_reason == BuildFailureReason.AGENT_ERROR
    assert done.agent_summary is None
    assert "inventory page" not in done.failure_detail


def test_a2_a_publish_failure_persists_null_and_names_only_the_stage_and_class(
    monkeypatch, make_build_with_request, db_session
):
    from app.services.publish import PublishError

    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=AGENT_TEXT))
    h.publish_error = PublishError("git push failed: remote said no")

    done = run_one_build(db_session, make_build_with_request(), h.executor)

    assert done.status == BuildStatus.FAILED and done.failure_reason == BuildFailureReason.HARVEST_ERROR
    assert done.agent_summary is None
    assert done.failure_detail == "harvesting the build's output failed at publish (PublishError); see the worker log"


def test_a2_the_executor_result_on_a_failure_carries_no_agent_text(monkeypatch, make_build_with_request):
    h = Harness(
        monkeypatch,
        adapter_result=AgentAdapterResult(ok=False, reason=BuildFailureReason.TIMEOUT, summary=AGENT_TEXT),
    )

    result = h.executor.execute(make_build_with_request())

    assert result.agent_summary is None and AGENT_TEXT not in (result.summary or "")


# --- (a3) bounded --------------------------------------------------------------------


def test_a3_a_ten_thousand_character_summary_is_stored_truncated_with_the_marker(
    monkeypatch, make_build_with_request, db_session
):
    long = "x" * 10_000
    h = Harness(monkeypatch, adapter_result=AgentAdapterResult(ok=True, summary=long))

    done = run_one_build(db_session, make_build_with_request(), h.executor)

    assert len(done.agent_summary) == 4000  # a literal: the bound is part of the contract
    assert done.agent_summary.endswith("…[truncated]")
    assert done.agent_summary == "x" * (4000 - len("…[truncated]")) + "…[truncated]"


def test_a3_the_bound_is_inclusive_a_text_exactly_at_the_limit_is_untouched(db_session, make_build_with_request):
    assert AGENT_SUMMARY_MAX_CHARS == 4000 and AGENT_SUMMARY_TRUNCATION_MARKER == "…[truncated]"
    build = make_build_with_request()
    exact = "y" * 4000

    assert mark_succeeded(db_session, build, agent_summary=exact).agent_summary == exact


def test_mark_succeeded_without_a_summary_leaves_null(db_session, make_build_with_request):
    assert mark_succeeded(db_session, make_build_with_request()).agent_summary is None


# --- (a5) nothing reads it: an `ast` scan over app/ ----------------------------------

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# EXACT mention counts per file. A file holds the name only as
# often as listed; any other file holds it zero times. A new mention anywhere -- in a
# file that is allowed some, as much as in one that is not -- fails the test and must be
# argued for here. That is what makes "no decision consumes it" something the suite
# checks rather than something a future edit has to remember: a per-file allowlist would
# let `worker.py` or `build_queue.py` grow a read without a sound.
#
# What is counted is what `_mentions` finds: an attribute access, a string constant (a
# `getattr` / dict key), a bare name (a read, an annotated field, a keyword's value) or an
# import alias. A function parameter and a keyword argument's own name are not mentions.
EXPECTED_MENTIONS = {
    "models/build.py": 1,  # the column
    "schemas/build.py": 1,  # BuildOut's field
    "schemas/approval.py": 1,  # the detail schema's field
    "routers/projects.py": 2,  # the timeline: the dict key and `build.agent_summary`
    "routers/approvals.py": 2,  # the detail: the dict key and `build.agent_summary`
    "services/build_queue.py": 2,  # mark_succeeded: `build.agent_summary = bound_agent_summary(agent_summary)`
    "services/build_executor.py": 1,  # ExecutionResult's field
    "worker.py": 1,  # `result.agent_summary`, handed to mark_succeeded and not looked at
    "demo_data.py": 2,  # the seeding helpers' pass-through
}

# Whole-object serialisation would carry the summary into whatever consumes the object
# without ever naming it, so the name scan cannot see it. The cheap check: these calls
# (`build.__dict__`, `.model_dump()`, `vars(x)`, `asdict(x)`, `jsonable_encoder(x)`,
# `X.model_validate(x)`) may appear only in the serialisation layer. LIMIT: this does not
# follow an object through a variable or a helper in `routers/` or `schemas/`, and it does
# not see a decision that is itself written there; the layers above are not decision code.
SERIALISATION_LAYER = ("routers/", "schemas/")
_WHOLE_OBJECT_ATTRS = {"__dict__", "model_dump", "model_dump_json", "model_validate", "model_validate_json"}
_WHOLE_OBJECT_CALLS = {"vars", "asdict", "jsonable_encoder"}


def _mentions(path: Path) -> list[int]:
    """Lines where the source reads or names `agent_summary`: an attribute access, a string
    constant (a `getattr`/dict key), an import alias or a bare name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "agent_summary":
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == "agent_summary":
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "agent_summary":
            lines.append(node.lineno)
        elif isinstance(node, ast.alias) and node.name == "agent_summary":
            lines.append(node.lineno)
    return lines


def mention_deviations(app_dir: Path, expected: dict[str, int]) -> dict[str, str]:
    """Every file under `app_dir` whose mention count is not exactly what `expected` says
    (absent = 0), and every expected file that is missing."""
    found: dict[str, list[int]] = {}
    for path in sorted(app_dir.rglob("*.py")):
        lines = _mentions(path)
        if lines:
            found[path.relative_to(app_dir).as_posix()] = lines
    out: dict[str, str] = {}
    for rel in sorted(set(found) | set(expected)):
        want = expected.get(rel, 0)
        got = len(found.get(rel, []))
        if got != want:
            out[rel] = f"expected {want} mention(s), found {got} (lines {found.get(rel, [])})"
    return out


def whole_object_serialisation(app_dir: Path) -> dict[str, list[int]]:
    """Lines of whole-object serialisation outside the serialisation layer."""
    out: dict[str, list[int]] = {}
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(app_dir).as_posix()
        if rel.startswith(SERIALISATION_LAYER):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _WHOLE_OBJECT_ATTRS:
                lines.append(node.lineno)
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in _WHOLE_OBJECT_CALLS:
                    lines.append(node.lineno)
        if lines:
            out[rel] = sorted(set(lines))
    return out


def test_a5_agent_summary_is_mentioned_exactly_as_often_as_expected_in_each_file():
    deviations = mention_deviations(APP_DIR, EXPECTED_MENTIONS)
    assert deviations == {}, (
        f"agent_summary is an unverified claim and must not be read here; change EXPECTED_MENTIONS only "
        f"with a reason: {deviations}"
    )


def test_a5_no_whole_object_serialisation_outside_the_serialisation_layer():
    assert whole_object_serialisation(APP_DIR) == {}


def test_a5_the_scan_can_see_a_read_of_each_shape(tmp_path):
    """The scan reaches what it names: each way of reading the attribute is found."""
    for i, source in enumerate(
        [
            "x = build.agent_summary",
            "x = getattr(build, 'agent_summary')",
            "x = data['agent_summary']",
            "from m import agent_summary",
        ]
    ):
        f = tmp_path / f"probe{i}.py"
        f.write_text(source + "\n", encoding="utf-8")
        assert _mentions(f) == [1], source


def test_a5_one_more_mention_in_an_allowed_file_fails_and_so_does_one_fewer(tmp_path):
    """The exact count bites in the files that are allowed some: `worker.py` with its one
    hand-off plus a second read is a deviation, a file that lost its mention is one, and a
    file that is not listed at all is held to zero."""
    (tmp_path / "worker.py").write_text(
        "def f(result, db):\n    keep = result.agent_summary\n    if 'x' in result.agent_summary:\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "build.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "policy_gate.py").write_text("y = build.agent_summary\n", encoding="utf-8")

    deviations = mention_deviations(
        tmp_path, {"worker.py": 1, "models/build.py": 1}
    )

    assert set(deviations) == {"worker.py", "models/build.py", "services/policy_gate.py"}
    assert deviations["worker.py"].startswith("expected 1 mention(s), found 2")
    assert deviations["models/build.py"].startswith("expected 1 mention(s), found 0")
    assert deviations["services/policy_gate.py"].startswith("expected 0 mention(s), found 1")
    # And the exact table is satisfied by an exact match.
    (tmp_path / "worker.py").write_text("def f(result):\n    return result.agent_summary\n", encoding="utf-8")
    (tmp_path / "models" / "build.py").write_text("agent_summary: str\n", encoding="utf-8")
    (tmp_path / "services" / "policy_gate.py").write_text("y = 1\n", encoding="utf-8")
    assert mention_deviations(tmp_path, {"worker.py": 1, "models/build.py": 1}) == {}


def test_a5_the_whole_object_scan_sees_each_shape_and_exempts_the_serialisation_layer(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "routers").mkdir()
    (tmp_path / "services" / "a.py").write_text(
        "d = build.__dict__\ne = out.model_dump()\nf = vars(build)\ng = asdict(build)\nh = jsonable_encoder(build)\n"
        "i = BuildOut.model_validate(build)\n",
        encoding="utf-8",
    )
    (tmp_path / "routers" / "b.py").write_text("d = build.__dict__\ne = out.model_dump()\n", encoding="utf-8")

    assert whole_object_serialisation(tmp_path) == {"services/a.py": [1, 2, 3, 4, 5, 6]}


# --- (a6) the migration preserves a row, both ways ----------------------------------


def test_a6_the_migration_round_trip_preserves_a_row(tmp_path):
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "c55d7a2e9f10")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO builds (id, created_at, project_id, type, status, diff_ref) "
                "VALUES ('b-1', '2026-10-01 00:00:00', 'p-1', 'create', 'SUCCEEDED', 'forge-build-b-1#1')"
            )
        )

    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        row = conn.execute(text("SELECT status, diff_ref, agent_summary FROM builds WHERE id='b-1'")).one()
        assert tuple(row) == ("SUCCEEDED", "forge-build-b-1#1", None)  # the old row reads as NULL, not invented
        conn.execute(text("UPDATE builds SET agent_summary = 'wrote it' WHERE id='b-1'"))

    command.downgrade(cfg, "c55d7a2e9f10")
    with engine.begin() as conn:
        columns = [r[1] for r in conn.execute(text("PRAGMA table_info(builds)"))]
        assert "agent_summary" not in columns
        row = conn.execute(text("SELECT status, diff_ref FROM builds WHERE id='b-1'")).one()
        assert tuple(row) == ("SUCCEEDED", "forge-build-b-1#1")
    engine.dispose()


# --- (b) the API ---------------------------------------------------------------------


@pytest.fixture()
def project_row(db_session, seeded_ids):
    project = Project(
        name="Agent Summary Project",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _succeeded_build_with_approval(db_session, project, seeded_ids, summary):
    build = Build(project_id=project.id, type="create", status=BuildStatus.SUCCEEDED, agent_summary=summary)
    db_session.add(build)
    db_session.flush()
    db_session.add(
        ChangeRequest(
            project_id=project.id, requester_id=seeded_ids["user_id"], nl_request="build it", build_id=build.id
        )
    )
    verdict = PolicyVerdict(build_id=build.id, risk_tier=PolicyRiskTier.MEDIUM)
    db_session.add(verdict)
    db_session.flush()
    request = create_approval_request_for_verdict(
        db_session, build=build, verdict=verdict, project_id=project.id, requester_id=project.owner_id
    )
    db_session.commit()
    return build, request


def _as(user_id):
    return {"X-Forge-User-Id": user_id}


def test_b1_the_timeline_build_entry_and_the_approval_detail_carry_it(client, db_session, project_row, seeded_ids):
    build, request = _succeeded_build_with_approval(db_session, project_row, seeded_ids, AGENT_TEXT)

    timeline = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"])).json()
    (entry,) = [e for e in timeline if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["agent_summary"] == AGENT_TEXT

    detail = client.get(f"/api/approvals/{request.id}", headers=_as(seeded_ids["user_id"])).json()
    assert detail["agent_summary"] == AGENT_TEXT


def test_b1_a_build_with_no_summary_is_null_on_both(client, db_session, project_row, seeded_ids):
    build, request = _succeeded_build_with_approval(db_session, project_row, seeded_ids, None)

    timeline = client.get(f"/api/projects/{project_row.id}/timeline", headers=_as(seeded_ids["user_id"])).json()
    (entry,) = [e for e in timeline if e["kind"] == "build" and e["data"]["id"] == build.id]
    assert entry["data"]["agent_summary"] is None
    detail = client.get(f"/api/approvals/{request.id}", headers=_as(seeded_ids["user_id"])).json()
    assert detail["agent_summary"] is None


def test_b2_the_pending_list_does_not_carry_it(client, db_session, project_row, seeded_ids):
    _succeeded_build_with_approval(db_session, project_row, seeded_ids, AGENT_TEXT)
    approver = db_session.query(User).filter_by(role="approver").first()  # ADR 0048: the list is an approver's

    resp = client.get("/api/approvals", headers=_as(approver.id))

    assert resp.status_code == 200
    (item,) = resp.json()
    assert "agent_summary" not in item
    assert "inventory page" not in resp.text
