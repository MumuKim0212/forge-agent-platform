"""encrypts `Project.service_env_json` at rest.

**Scope, stated once so it cannot be oversold from further away.** This
protects a copy of the platform database that leaves the host — a backup, a
dump, a restored snapshot on a developer's laptop, an ad-hoc `SELECT`. It
does **not** protect against host or control-plane compromise: ADR 0002
already made the host the trust boundary, and a process that can read
`SERVICE_ENV_ENCRYPTION_KEY` from its own environment (the platform process
itself) can decrypt this column just as easily as the platform can. Do not
extend this module's docstrings, or any comment near its call sites, to
imply a stronger guarantee than that.

This is the ONE seam that turns ciphertext into the plaintext dict the rest
of the platform works with. `services.deploy.ensure_service_env` is the
only production caller — it, not this module, owns the mode-dependent
(prod-raises / dev-re-issues) decision on a decryption failure, because
that decision is about the row's LIFECYCLE (do we keep it, or treat it as
never issued?), not about the cryptography. The Alembic migration
(`migrations/versions/*_encrypt_service_env_json.py`) is the other caller,
via `encrypt_rows_for_migration`/`decrypt_rows_for_migration` below.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class ServiceEnvDecryptionError(RuntimeError):
    """A stored `service_env_json` ciphertext could not be decrypted with
    `SERVICE_ENV_ENCRYPTION_KEY` or (if set) `SERVICE_ENV_ENCRYPTION_KEY_OLD`.
    States the fact only — mode-dependent handling (prod fails the
    operation, dev treats the row as absent and re-issues) is the caller's
    job, per this module's docstring."""


def _fernet(key: str) -> Fernet:
    return Fernet(key.encode("ascii"))


def encrypt_service_env(env: dict[str, str]) -> str:
    """Serializes `env` to JSON and encrypts it under the CURRENT key only.
    Rotation re-encrypts under the
    current key and then drops `_OLD` — nothing is ever written under
    `_OLD`, which is why there is no `key` parameter here to choose one."""
    token = _fernet(settings.service_env_encryption_key).encrypt(
        json.dumps(env).encode("utf-8")
    )
    return token.decode("ascii")


def decrypt_service_env_with_provenance(ciphertext: str) -> tuple[dict[str, str], bool]:
    """Tries the CURRENT key first, and `SERVICE_ENV_ENCRYPTION_KEY_OLD`
    only if the current key fails AND `_OLD` is set (`_OLD` is
    decrypt-only, and only a fallback — never tried first, so a value
    written under the current key is unaffected by whatever `_OLD` holds).
    Returns `(plaintext, used_old_key)`: the flag is what the re-encrypt
    command (`app/reencrypt_service_env.py`) needs to tell a row that
    is already current from one that still depends on `_OLD`. Raises
    `ServiceEnvDecryptionError` if neither key can read it."""
    raw = ciphertext.encode("ascii")
    used_old_key = False
    try:
        plaintext = _fernet(settings.service_env_encryption_key).decrypt(raw)
    except InvalidToken:
        if not settings.service_env_encryption_key_old:
            raise ServiceEnvDecryptionError(
                "service_env_json could not be decrypted with the current "
                "SERVICE_ENV_ENCRYPTION_KEY, and SERVICE_ENV_ENCRYPTION_KEY_OLD "
                "is not set."
            ) from None
        try:
            plaintext = _fernet(settings.service_env_encryption_key_old).decrypt(raw)
        except InvalidToken as exc:
            raise ServiceEnvDecryptionError(
                "service_env_json could not be decrypted with the current "
                "SERVICE_ENV_ENCRYPTION_KEY or with SERVICE_ENV_ENCRYPTION_KEY_OLD."
            ) from exc
        used_old_key = True
    return json.loads(plaintext.decode("utf-8")), used_old_key


def decrypt_service_env(ciphertext: str) -> dict[str, str]:
    """The plaintext of a stored `service_env_json`, under the current key or
    (fallback) `_OLD`; see `decrypt_service_env_with_provenance`. Raises
    `ServiceEnvDecryptionError` if neither key can read it."""
    return decrypt_service_env_with_provenance(ciphertext)[0]


# -- Alembic migration support ---------------------------------------
#
# The migration process constructs its OWN `Settings()` (`migrations/env.py`
# imports `app.config.settings`), a different process than the app that
# will read these rows back — so it must never encrypt under a key it only
# just generated for itself (`settings.service_env_encryption_key_configured`
# is the signal). Kept here, not inline in the migration file, so this
# logic is importable by an ordinary pytest test rather than only
# verifiable by hand-running `alembic upgrade`/`downgrade` (the rest of
# `migrations/` is verified that way — see the migration file's docstring
# and this task's `## Result`).


def encrypt_rows_for_migration(
    rows: list[tuple[str, object]],
) -> dict[str, str]:
    """`rows` are `(project_id, raw_value)` pairs read from
    `projects.service_env_json` while it is still the earlier plaintext
    JSON column. Returns `{project_id: ciphertext}` for every row that
    needs writing back.

    - A `None` value (never issued) is skipped — there is no secret on
      that row, so skipping it is safe.
    - A non-`None` value that is not valid JSON aborts the WHOLE migration
      with a `RuntimeError` naming the row: this refuses to guess at a row
      it cannot read, since guessing wrong would carry corrupt data
      forward silently under a column that is no longer inspectable once
      it is ciphertext.
    - Raises `RuntimeError` up front, before encrypting anything, if there
      is at least one row to encrypt but `SERVICE_ENV_ENCRYPTION_KEY` was
      not explicitly configured for this process — see the module
      docstring above.
    """
    to_migrate = [(project_id, value) for project_id, value in rows if value is not None]
    if not to_migrate:
        return {}

    if not settings.service_env_encryption_key_configured:
        raise RuntimeError(
            f"migration: projects.service_env_json holds {len(to_migrate)} "
            "row(s) to encrypt, but SERVICE_ENV_ENCRYPTION_KEY is not configured "
            "for this process. A generated key is per-process, and this "
            "migration process is not the app process that will read these "
            "rows back afterwards -- refusing to encrypt under a key nothing "
            "will ever see again. Set SERVICE_ENV_ENCRYPTION_KEY in the "
            "environment this migration runs with, then retry."
        )

    updates: dict[str, str] = {}
    for project_id, raw_value in to_migrate:
        if isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "migration: projects.service_env_json for project "
                    f"{project_id!r} is not valid JSON -- refusing to guess at "
                    "a row it cannot read. Investigate and fix the row, then "
                    "retry."
                ) from exc
        else:
            # Some DBAPI/dialect combinations already hand back a decoded
            # dict for a native JSON column (e.g. some psycopg configurations).
            parsed = raw_value
        updates[project_id] = encrypt_service_env(parsed)
    return updates


def decrypt_rows_for_migration(rows: list[tuple[str, object]]) -> dict[str, str]:
    """Downgrade-path counterpart of `encrypt_rows_for_migration`: given
    `(project_id, ciphertext)` pairs, returns `{project_id: json_text}` for
    every non-`None` row, ready to write back into the restored JSON
    column. Propagates `ServiceEnvDecryptionError` rather than guessing at
    a row that will not decrypt — the downgrade aborts loudly instead of
    silently dropping a service's stored secret."""
    updates: dict[str, str] = {}
    for project_id, raw_value in rows:
        if raw_value is None:
            continue
        plaintext = decrypt_service_env(raw_value)
        updates[project_id] = json.dumps(plaintext)
    return updates
