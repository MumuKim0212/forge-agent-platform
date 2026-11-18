"""encrypt service env json

Revision ID: e188503deab6
Revises: 752f15b718ce
Create Date: 2026-09-02 17:06:22.337973

`projects.service_env_json` stops being a
plaintext JSON column and becomes Fernet ciphertext (Text). Every existing
non-NULL row is encrypted in place under `SERVICE_ENV_ENCRYPTION_KEY`; the
column type change (`JSON` -> `Text`) happens BEFORE the rows are
overwritten, so the ciphertext is never asked to round-trip through a JSON
column's validity check (Postgres' native `json`/`jsonb` types reject a
value that isn't syntactically valid JSON, and a Fernet token is not).

**What this does with a row it cannot read** (task acceptance criterion):

- A row whose value is NULL (never issued) is left NULL. Safe because
  there is no secret on that row to protect.
- A row whose non-NULL value is not valid JSON aborts the WHOLE migration
  with a `RuntimeError` naming the offending project id. This migration
  refuses to guess at a row it cannot read — guessing wrong here would
  silently carry corrupt data forward under a column that is no longer
  inspectable once it is ciphertext. In practice this should never fire:
  the pre-encryption column is a native SQLAlchemy `JSON` type, so every row
  written by the application is well-formed JSON by construction.

**Why this can abort on "no configured key" even outside prod** (task
acceptance criterion — the gap `migrations/env.py:6` opens): this process
constructs its OWN `app.config.settings` — a DIFFERENT process than the
app that will read these rows back afterwards. In dev, an unset
SERVICE_ENV_ENCRYPTION_KEY is happily generated per-process by
`Settings.model_post_init` rather than raising (unlike prod, which already
refuses to start without one) — so if this migration encrypted under that
generated key, it would encrypt every row under a key nothing will ever
see again, and the app would silently treat every project's
`service_env_json` as absent and re-issue on its next start (the dev
decrypt-failure branch). The fix: read
`settings.service_env_encryption_key_configured` (true only when the key
came from an env var / `.env` file / explicit config, never from
generation) and refuse to write anything if it is false AND there is at
least one row that needs encrypting. See
`app/services/service_env_crypto.encrypt_rows_for_migration`, which is
where this rule actually lives (kept out of this file so it is importable
by an ordinary pytest test — see `tests/test_service_env_crypto.py` — the
rest of `migrations/` is verified by hand-running `alembic upgrade`/
`downgrade` instead, per this project's established practice for data
migrations).

Verified by hand against a scratch SQLite database with a pre-existing
plaintext row (created against `752f15b718ce`, the parent revision): see
this task's `## Result` for the exact commands and observed output.
`op.batch_alter_table` is used for the column-type change for the same
reason as every other migration here (SQLite has no native `ALTER
COLUMN`); on Postgres the same call executes as a plain `ALTER TABLE ...
ALTER COLUMN ... TYPE ... USING ...` (batch mode only recreates the table
on dialects that need it).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e188503deab6'
down_revision: Union[str, None] = '752f15b718ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.services.service_env_crypto import encrypt_rows_for_migration

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, service_env_json FROM projects")
    ).fetchall()
    # Computed BEFORE the column-type change below (and before anything is
    # written) so an aborting condition (unconfigured key / unreadable row)
    # never leaves the schema half-migrated.
    updates = encrypt_rows_for_migration([(row[0], row[1]) for row in rows])

    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column(
            "service_env_json",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="service_env_json::text",
        )

    for project_id, ciphertext in updates.items():
        bind.execute(
            sa.text("UPDATE projects SET service_env_json = :v WHERE id = :id"),
            {"v": ciphertext, "id": project_id},
        )


def downgrade() -> None:
    from app.services.service_env_crypto import decrypt_rows_for_migration

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, service_env_json FROM projects")
    ).fetchall()
    # Decrypted BEFORE the column-type change for the same reason as above
    # (a `ServiceEnvDecryptionError` here aborts before anything is written).
    updates = decrypt_rows_for_migration([(row[0], row[1]) for row in rows])

    for project_id, plaintext_json in updates.items():
        bind.execute(
            sa.text("UPDATE projects SET service_env_json = :v WHERE id = :id"),
            {"v": plaintext_json, "id": project_id},
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column(
            "service_env_json",
            existing_type=sa.Text(),
            type_=sa.JSON(),
            existing_nullable=True,
            postgresql_using="service_env_json::json",
        )
