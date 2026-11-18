"""Re-encrypt every stored service env under the current key.

    python -m app.reencrypt_service_env

The key change is: set `SERVICE_ENV_ENCRYPTION_KEY_OLD` to the previous
key -> set `SERVICE_ENV_ENCRYPTION_KEY` to the new one -> **run this** -> drop
`_OLD`. Until this existed the third step was a sentence with nothing behind
it: an operator who followed the procedure dropped `_OLD` while rows were still
encrypted under it, and every deploy of those services then failed.

Per row of `projects.service_env_json` (the column holds Fernet ciphertext):

- no stored env (NULL) -> skipped, nothing to encrypt;
- decrypts under the CURRENT key -> already current, left untouched;
- decrypts only under `_OLD` -> **rewritten** under the current key;
- decrypts under neither -> **unreadable**: reported by project id, left
  untouched, and the command exits non-zero. The cause is usually a wrong or
  mistyped `_OLD`; never guess at it.

**Idempotent.** A second run finds every readable row already current and
rewrites none. Plaintext never changes, so no service restarts and nothing
redeploys. Before a row is rewritten it is read again under a row lock
(`SELECT ... FOR UPDATE`, which Postgres honours and SQLite, a single-writer
database, does not need) and compared with the ciphertext that was decrypted; a
row that changed meanwhile (a deploy upgrading its env, an admin's deprovision
clearing it) is reported and left alone rather than overwritten with a stale
copy -- re-run to settle it.

The final report ends with a verification pass over the committed rows: **"still
under the old key"** is the number of rows that decrypt ONLY under `_OLD` after
this run. `_OLD` may be dropped only when it is zero. Run it with the same
environment the platform runs with (it refuses to run on a key it generated for
itself, because that key is per-process and nothing would ever read the result).

Exit status: 0 when nothing needs attention; 1 when a row is unreadable, a row
changed while the command ran, or any row is still under the old key; 2 when it
refused to run (key not configured or not a valid Fernet key, schema not at the
Alembic head). Nothing it prints is a secret: project ids and counts only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as app_db
from app.config import settings
from app.models.project import Project
from app.schema_check import SchemaNotAtHeadError, assert_schema_at_head
from app.services.audit import write_audit_log
from app.services.service_env_crypto import (
    ServiceEnvDecryptionError,
    decrypt_service_env_with_provenance,
    encrypt_service_env,
)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_REFUSED = 2


@dataclass
class ReencryptReport:
    """What one run did. Counts and project ids only -- never a value."""

    rewritten: int = 0
    already_current: int = 0
    no_env: int = 0
    unreadable: list[str] = field(default_factory=list)
    changed_while_running: list[str] = field(default_factory=list)
    # Measured on a fresh read AFTER the commit: rows that decrypt only under `_OLD`.
    still_under_old_key: int = 0

    @property
    def needs_attention(self) -> bool:
        return bool(self.unreadable or self.changed_while_running or self.still_under_old_key)


def reencrypt_service_envs(db: Session) -> ReencryptReport:
    """Re-encrypt every readable row under the current key; commit; verify.

    One transaction: a failure part-way writes nothing, and the next run starts
    over (it is idempotent)."""
    report = ReencryptReport()

    # A snapshot of (id, ciphertext) rather than ORM objects: the compare-and-swap
    # below is against exactly the ciphertext that was decrypted.
    for project_id, ciphertext in db.execute(
        select(Project.id, Project.service_env_json).order_by(Project.id)
    ).all():
        if not ciphertext:
            report.no_env += 1
            continue
        try:
            env, used_old_key = decrypt_service_env_with_provenance(ciphertext)
        except (ServiceEnvDecryptionError, ValueError):
            # ValueError: ciphertext that is not ASCII, or valid ciphertext whose
            # plaintext is not JSON -- unreadable in the sense that matters here.
            report.unreadable.append(project_id)
            continue
        if not used_old_key:
            report.already_current += 1
            continue
        # Re-read under a row lock and compare: only a row still holding exactly the
        # ciphertext that was decrypted is rewritten (a deprovisioned row, now NULL, is
        # not resurrected with a stale copy of its credentials).
        row = db.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None or row.service_env_json != ciphertext:
            report.changed_while_running.append(project_id)
            continue
        row.service_env_json = encrypt_service_env(env)
        db.flush()
        report.rewritten += 1

    if report.rewritten:
        # Counts only, like every other service-env audit entry (key NAMES at most,
        # never a value).
        write_audit_log(
            db,
            actor_id=None,
            action="service_env.reencrypted",
            target="service_env:all",
            metadata={
                "rewritten": report.rewritten,
                "already_current": report.already_current,
                "unreadable": len(report.unreadable),
            },
        )
    db.commit()

    for (ciphertext,) in db.execute(select(Project.service_env_json)).all():
        if not ciphertext:
            continue
        try:
            _env, used_old_key = decrypt_service_env_with_provenance(ciphertext)
        except (ServiceEnvDecryptionError, ValueError):
            continue  # already reported as unreadable (or it changed to something that is)
        if used_old_key:
            report.still_under_old_key += 1
    return report


def format_report(report: ReencryptReport) -> str:
    old_key_set = bool(settings.service_env_encryption_key_old)
    lines = [
        "service env re-encryption:",
        f"  rewritten under the current key:               {report.rewritten}",
        f"  already under the current key:                 {report.already_current}",
        f"  no stored env (skipped):                       {report.no_env}",
        f"  could not be decrypted under either key:       {len(report.unreadable)}",
    ]
    lines += [f"      project {project_id}" for project_id in report.unreadable]
    lines.append(f"  changed while the command ran (re-run it):     {len(report.changed_while_running)}")
    lines += [f"      project {project_id}" for project_id in report.changed_while_running]
    lines.append(f"  still under the old key after this run:        {report.still_under_old_key}")
    if report.unreadable:
        lines.append(
            "ATTENTION: rows above cannot be read with the configured keys. A wrong or "
            "mistyped SERVICE_ENV_ENCRYPTION_KEY_OLD looks exactly like this; do not drop it "
            "until you know why. Those rows were left untouched."
        )
    if report.changed_while_running:
        lines.append("ATTENTION: rows above changed while this ran and were left alone; run the command again.")
    if report.still_under_old_key:
        lines.append(
            "NOT DONE: rows still depend on SERVICE_ENV_ENCRYPTION_KEY_OLD. Run the command "
            "again; keep the old key until this count is zero."
        )
    if not report.needs_attention:
        lines.append(
            "OK: no row depends on SERVICE_ENV_ENCRYPTION_KEY_OLD any more; it may be dropped."
            if old_key_set
            else "OK: every stored env is under the current key (SERVICE_ENV_ENCRYPTION_KEY_OLD is not set)."
        )
    return "\n".join(lines)


def _refusal() -> str | None:
    """Why this process must not run the command, or None. Checked before the
    database is opened."""
    if not settings.service_env_encryption_key_configured:
        return (
            "SERVICE_ENV_ENCRYPTION_KEY is not configured for this process, so the key it would "
            "re-encrypt under is one this process just generated for itself and nothing will "
            "ever read the result. Set it (the same environment the platform runs with) and retry."
        )
    for name, key in (
        ("SERVICE_ENV_ENCRYPTION_KEY", settings.service_env_encryption_key),
        ("SERVICE_ENV_ENCRYPTION_KEY_OLD", settings.service_env_encryption_key_old),
    ):
        if not key:
            continue
        try:
            Fernet(key.encode("ascii"))
        except ValueError:
            return f"{name} is not a valid Fernet key (32 url-safe base64-encoded bytes)."
    return None


def main() -> int:
    reason = _refusal()
    if reason is not None:
        print(f"reencrypt_service_env: refused -- {reason}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        # Read at call time through the module, like the server and the worker.
        assert_schema_at_head(app_db.engine)
    except SchemaNotAtHeadError as exc:
        print(f"reencrypt_service_env: refused -- {exc}", file=sys.stderr)
        return EXIT_REFUSED

    db = app_db.SessionLocal()
    try:
        report = reencrypt_service_envs(db)
    finally:
        db.close()
    print(format_report(report))
    return EXIT_ATTENTION if report.needs_attention else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
