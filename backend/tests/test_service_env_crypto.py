"""`app/services/service_env_crypto.py`.

Covers the crypto seam directly (no DB, no `ensure_service_env`) plus the
Alembic migration's data-migration support functions
(`encrypt_rows_for_migration` / `decrypt_rows_for_migration`) — kept
importable here specifically so the migration's abort rule is covered by an
ordinary pytest test rather than only verifiable by hand-running `alembic
upgrade` (see the migration file's docstring and this task's `## Result`).

`services.deploy.ensure_service_env`'s mode-dependent (prod-raises /
dev-re-issues) behaviour on a decryption failure is covered in
`tests/test_deploy_service.py`, not here — that decision belongs to
`ensure_service_env`, not to this module (see both modules' docstrings).
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services.service_env_crypto import (
    ServiceEnvDecryptionError,
    decrypt_rows_for_migration,
    decrypt_service_env,
    decrypt_service_env_with_provenance,
    encrypt_rows_for_migration,
    encrypt_service_env,
)

SAMPLE_ENV = {"FORGE_SERVICE_ID": "svc-1", "FORGE_APP_SECRET_KEY": "sekrit-value-123"}


def test_encrypt_then_decrypt_round_trips():
    ciphertext = encrypt_service_env(SAMPLE_ENV)
    assert isinstance(ciphertext, str)
    assert SAMPLE_ENV["FORGE_APP_SECRET_KEY"] not in ciphertext
    assert decrypt_service_env(ciphertext) == SAMPLE_ENV


def test_decrypt_with_wrong_key_and_no_old_key_raises():
    """(g1) Pinned on MESSAGE, not just type — `ServiceEnvDecryptionError`
    is raised from two distinct branches in `decrypt_service_env` (no
    `_OLD` configured at all vs. `_OLD` configured but also wrong), and a
    type-only assertion cannot tell them apart. The two messages share
    "could not be decrypted with the current SERVICE_ENV_ENCRYPTION_KEY"
    and the bare token "SERVICE_ENV_ENCRYPTION_KEY_OLD" — checked here
    against the sibling test below, not assumed: the exclusive phrase for
    THIS branch is "is not set", which the both-keys-wrong message never
    contains ("...or with SERVICE_ENV_ENCRYPTION_KEY_OLD.")."""
    ciphertext = encrypt_service_env(SAMPLE_ENV)
    # `object()` not settings.service_env_encryption_key: prove the failure
    # is on the KEY, not something else about the ciphertext.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
        mp.setattr(settings, "service_env_encryption_key_old", None)
        with pytest.raises(ServiceEnvDecryptionError) as exc_info:
            decrypt_service_env(ciphertext)
    message = str(exc_info.value)
    assert "is not set" in message
    assert "or with SERVICE_ENV_ENCRYPTION_KEY_OLD" not in message


def test_decrypt_with_both_keys_wrong_raises():
    """(g2, crypto-seam level) The branch this task's review found
    unguarded: current key wrong, `_OLD` SET but ALSO wrong. Before this
    test, nothing in the suite exercised this branch at all — a mutation
    collapsing it to `except InvalidToken: return {}` left the full suite
    green, which is exactly the "silently re-issues"-shaped regression
    the task's own criterion calls out as the one most worth catching.
    Message pinned the same way as the sibling above, in the other
    direction: the exclusive phrase for THIS branch is "or with
    SERVICE_ENV_ENCRYPTION_KEY_OLD", which the no-`_OLD` message never
    contains (it has "and SERVICE_ENV_ENCRYPTION_KEY_OLD is not set", not
    "or with")."""
    ciphertext = encrypt_service_env(SAMPLE_ENV)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
        mp.setattr(settings, "service_env_encryption_key_old", Fernet.generate_key().decode())
        with pytest.raises(ServiceEnvDecryptionError) as exc_info:
            decrypt_service_env(ciphertext)
    message = str(exc_info.value)
    assert "or with SERVICE_ENV_ENCRYPTION_KEY_OLD" in message
    assert "is not set" not in message


def test_decrypt_falls_back_to_old_key_only_when_current_key_fails():
    original_key = settings.service_env_encryption_key
    ciphertext = encrypt_service_env(SAMPLE_ENV)  # written under original_key

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
        mp.setattr(settings, "service_env_encryption_key_old", original_key)
        assert decrypt_service_env(ciphertext) == SAMPLE_ENV


def test_provenance_says_which_key_read_the_value():
    """the flag the re-encrypt command (`app/reencrypt_service_env.py`) is built
    on -- a value under the current key says False and one that needed `_OLD` says
    True, so "already current" and "still depends on the old key" can be told apart.
    The keys and ciphertexts are made with Fernet directly, not through the code under
    test."""
    current, old = Fernet.generate_key(), Fernet.generate_key()
    under_current = Fernet(current).encrypt(json.dumps(SAMPLE_ENV).encode()).decode()
    under_old = Fernet(old).encrypt(json.dumps(SAMPLE_ENV).encode()).decode()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "service_env_encryption_key", current.decode())
        mp.setattr(settings, "service_env_encryption_key_old", old.decode())
        assert decrypt_service_env_with_provenance(under_current) == (SAMPLE_ENV, False)
        assert decrypt_service_env_with_provenance(under_old) == (SAMPLE_ENV, True)
        assert decrypt_service_env(under_old) == SAMPLE_ENV  # the plain reader is unchanged

        mp.setattr(settings, "service_env_encryption_key_old", None)
        assert decrypt_service_env_with_provenance(under_current) == (SAMPLE_ENV, False)
        with pytest.raises(ServiceEnvDecryptionError):
            decrypt_service_env_with_provenance(under_old)


# -- Alembic migration support: encrypt_rows_for_migration ------------


def test_encrypt_rows_for_migration_skips_none_rows():
    updates = encrypt_rows_for_migration([("proj-a", None), ("proj-b", None)])
    assert updates == {}


def test_encrypt_rows_for_migration_aborts_without_a_configured_key():
    """The gap `migrations/env.py:6` opens: a generated (not operator-
    configured) key is per-process, so the migration must refuse to write
    ciphertext under one — this must fire only when there is ACTUALLY a row
    to encrypt (a fresh/empty projects table needs no key at all)."""
    assert settings.service_env_encryption_key_configured is False  # test-suite default

    with pytest.raises(RuntimeError) as exc_info:
        encrypt_rows_for_migration([("proj-a", json.dumps(SAMPLE_ENV))])
    assert "SERVICE_ENV_ENCRYPTION_KEY is not configured" in str(exc_info.value)


def test_encrypt_rows_for_migration_does_not_abort_when_nothing_needs_encrypting():
    """No row has a value -> no key is ever needed, even though
    `service_env_encryption_key_configured` is False in the test suite by
    default -- this must NOT raise."""
    assert settings.service_env_encryption_key_configured is False
    assert encrypt_rows_for_migration([("proj-a", None)]) == {}


def test_encrypt_rows_for_migration_succeeds_with_a_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "_service_env_encryption_key_configured", True)

    raw_rows = [
        ("proj-a", json.dumps(SAMPLE_ENV)),
        ("proj-b", None),
        ("proj-c", {"FORGE_SERVICE_ID": "svc-c"}),  # already-decoded dict form
    ]
    updates = encrypt_rows_for_migration(raw_rows)

    assert set(updates.keys()) == {"proj-a", "proj-c"}
    assert decrypt_service_env(updates["proj-a"]) == SAMPLE_ENV
    assert decrypt_service_env(updates["proj-c"]) == {"FORGE_SERVICE_ID": "svc-c"}
    # The stored bytes really are ciphertext, not the plaintext JSON text.
    assert json.dumps(SAMPLE_ENV) not in updates["proj-a"]


def test_encrypt_rows_for_migration_aborts_on_a_row_it_cannot_read(monkeypatch):
    """A non-null, non-JSON value: the migration refuses to guess."""
    monkeypatch.setattr(settings, "_service_env_encryption_key_configured", True)

    with pytest.raises(RuntimeError) as exc_info:
        encrypt_rows_for_migration([("proj-corrupt", "not-valid-json{{{")])
    assert "proj-corrupt" in str(exc_info.value)
    assert "not valid JSON" in str(exc_info.value)


# -- Alembic migration support: decrypt_rows_for_migration ------------


def test_decrypt_rows_for_migration_round_trips_encrypt_rows_for_migration(monkeypatch):
    monkeypatch.setattr(settings, "_service_env_encryption_key_configured", True)

    encrypted = encrypt_rows_for_migration([("proj-a", json.dumps(SAMPLE_ENV))])
    decrypted = decrypt_rows_for_migration([("proj-a", encrypted["proj-a"]), ("proj-b", None)])

    assert set(decrypted.keys()) == {"proj-a"}
    assert json.loads(decrypted["proj-a"]) == SAMPLE_ENV


def test_decrypt_rows_for_migration_propagates_a_decrypt_failure_instead_of_dropping_the_row():
    """(g3) `decrypt_rows_for_migration` (the downgrade path) calls
    `decrypt_service_env` directly with no try/except of its own, so the
    same both-keys-wrong regression `test_decrypt_with_both_keys_wrong_raises`
    guards at the crypto seam would ALSO silently drop a row here (the
    corrupted/unreadable value would vanish from the returned dict rather
    than aborting the downgrade) if that branch were ever collapsed to
    swallow-and-return-empty. This proves the CURRENT code does not do
    that: an undecryptable row raises out of `decrypt_rows_for_migration`,
    it does not disappear from the result."""
    ciphertext = encrypt_service_env(SAMPLE_ENV)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "service_env_encryption_key", Fernet.generate_key().decode())
        mp.setattr(settings, "service_env_encryption_key_old", None)
        with pytest.raises(ServiceEnvDecryptionError):
            decrypt_rows_for_migration([("proj-undecryptable", ciphertext)])
