"""Tests for `app/services/policy_gate.py`: the persistence + audit
shell around the pure policy checks. Uses the standard in-memory SQLite
`db_session` fixture (tests/conftest.py) — no Docker, no network."""

from __future__ import annotations

import os

import pytest

from app.models.audit import AuditLog
from app.models.build import Build
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.services.policy_checks import PolicyCheckStatus, PolicyRiskTier
from app.services.policy_gate import run_and_persist_policy_gate
from app.services.status import ProjectStatus

MEDIUM_CHECKS = ["dependency_vulnerabilities", "resource_limits", "template_compliance"]
HIGH_CHECKS = ["secrets_pii", "egress_detection"]


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.fixture()
def template_dir(tmp_path):
    d = tmp_path / "template"
    _write(str(d / "Dockerfile"), "FROM python:3.11-slim\nCOPY app/ /app/\n")
    _write(
        str(d / "compose.yaml"),
        "services:\n  app:\n    build: .\n    deploy:\n      resources:\n        limits:\n          cpus: '1.0'\n          memory: 512M\n",
    )
    return str(d)


@pytest.fixture()
def workspace_dir(tmp_path, template_dir):
    import shutil

    d = tmp_path / "workspace"
    d.mkdir()
    shutil.copyfile(os.path.join(template_dir, "Dockerfile"), str(d / "Dockerfile"))
    shutil.copyfile(os.path.join(template_dir, "compose.yaml"), str(d / "compose.yaml"))
    return str(d)


@pytest.fixture()
def build_row(db_session, seeded_ids):
    project = Project(
        name="policy-gate-test",
        owner_id=seeded_ids["user_id"],
        team_id=seeded_ids["team_id"],
        template_id=seeded_ids["template_id"],
        status=ProjectStatus.SUBMITTED,
    )
    db_session.add(project)
    db_session.flush()
    build = Build(project_id=project.id, type="create")
    db_session.add(build)
    db_session.commit()
    return build


def test_persists_verdict_and_six_check_results(db_session, build_row, workspace_dir, template_dir):
    _write(os.path.join(workspace_dir, "app", "app.py"), "print('clean app')\n")

    verdict = run_and_persist_policy_gate(
        db_session,
        build_row,
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=["Dockerfile", "compose.yaml"],
        medium_tier_checks=MEDIUM_CHECKS,
        high_tier_checks=HIGH_CHECKS,
        network_available=True,
    )
    db_session.commit()

    assert verdict.risk_tier == PolicyRiskTier.LOW
    stored = db_session.get(PolicyVerdict, verdict.id)
    assert stored is not None
    assert stored.build_id == build_row.id

    checks = db_session.query(PolicyCheckResult).filter_by(verdict_id=verdict.id).all()
    assert len(checks) == 6
    assert all(c.result == PolicyCheckStatus.PASS for c in checks)


def test_flagged_check_persists_findings_and_high_tier(db_session, build_row, workspace_dir, template_dir):
    _write(
        os.path.join(workspace_dir, "app", "app.py"),
        "AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n",
    )

    verdict = run_and_persist_policy_gate(
        db_session,
        build_row,
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=["Dockerfile", "compose.yaml"],
        medium_tier_checks=MEDIUM_CHECKS,
        high_tier_checks=HIGH_CHECKS,
        network_available=True,
    )
    db_session.commit()

    assert verdict.risk_tier == PolicyRiskTier.HIGH
    checks = db_session.query(PolicyCheckResult).filter_by(verdict_id=verdict.id).all()
    secrets_check = next(c for c in checks if c.check_name == "secrets_pii")
    assert secrets_check.result == PolicyCheckStatus.FLAG
    assert secrets_check.findings
    assert "AKIAABCDEFGHIJKLMNOP" not in str(secrets_check.findings)


def test_verdict_creation_is_audit_logged(db_session, build_row, workspace_dir, template_dir):
    run_and_persist_policy_gate(
        db_session,
        build_row,
        workspace_dir=workspace_dir,
        template_skeleton_dir=template_dir,
        protected_paths=["Dockerfile", "compose.yaml"],
        medium_tier_checks=MEDIUM_CHECKS,
        high_tier_checks=HIGH_CHECKS,
        network_available=True,
    )
    db_session.commit()

    entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "policy_gate.verdict", AuditLog.target == f"build:{build_row.id}")
        .all()
    )
    assert len(entries) == 1
    assert entries[0].metadata_json["risk_tier"] in (
        PolicyRiskTier.LOW,
        PolicyRiskTier.MEDIUM,
        PolicyRiskTier.HIGH,
    )
