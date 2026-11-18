"""Password hashing for `local` auth mode (ADR 0022 §3).

bcrypt only -- a purpose-built password hash function, never reversible.
The hash is the same handling class as `Project.service_env_json`
: never logged, never audited, never
present in any response schema, never included in an export.

This module is the only place `bcrypt` is imported in `app/` -- every
caller goes through `hash_password`/`verify_password` rather than touching
the library directly, so the hashing scheme has one seam to change later.
"""

from __future__ import annotations

import bcrypt

# the hash function reads only
# the first 72 bytes of its input. The pinned bcrypt (5.0.0) raises past them
# and older versions silently truncate, so a longer password is refused where it
# is SET (the request schemas and `Settings`) rather than left to become a 500
# here. The limit is bytes in UTF-8, not characters: a 25-character Korean
# passphrase is 75 bytes. This module is the one declaration site; the schemas
# and `config.py` import it.
PASSWORD_MAX_BYTES = 72

# Attempt limiting ("the miss paths cost what the
# hit paths cost"): a real bcrypt hash, at the same cost factor `gensalt()` gives
# every stored hash (12), of a random value nobody holds. `verify_password`
# checks a candidate against it whenever there is no real hash to check -- an
# unknown email, a row with no password, a locked account -- so the work done is
# the same as for a wrong password on a real account. It is a constant, not
# computed at import, because a cost-12 hash takes a few hundred milliseconds
# and every process (the worker, alembic) imports this module. Not a credential:
# nothing it matches is known, and the result is masked in any case.
DUMMY_PASSWORD_HASH = "$2b$12$OYA0/iw1qP2UsJm4AtwYr.ENkbZF1nGsAZ8LH7EjSz9xkrGEXRuEK"


def password_byte_length(password: str) -> int:
    return len(password.encode("utf-8"))


def fits_hash_limit(password: str) -> bool:
    """True when `password` is within `PASSWORD_MAX_BYTES` UTF-8 bytes."""
    return password_byte_length(password) <= PASSWORD_MAX_BYTES


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """`password_hash IS NULL` can never authenticate (ADR 0022 §3):
    a row that never set a password is not a row with an
    empty password -- fail closed here rather than making every caller
    add its own `is None` guard.

    **The hash check runs whether or not there is a hash**: with no
    hash, the candidate is checked against `DUMMY_PASSWORD_HASH` and the
    answer is thrown away. A caller with no row to offer (unknown email) or a
    row with no password passes `None` and pays the same cost as a wrong
    password on a real account, so the response time is not an
    account-existence oracle. The result is `matched and <there was a real
    hash>`, which is why a dummy match could never authenticate anyone.

    A malformed/foreign-format hash (e.g. a truncated value) also fails
    closed rather than raising, so a corrupt row can never 500 its way into
    an information leak or a crash on the login path. So does a candidate
    over `PASSWORD_MAX_BYTES`, which the library refuses to hash."""
    target = password_hash or DUMMY_PASSWORD_HASH
    try:
        matched = bcrypt.checkpw(password.encode("utf-8"), target.encode("utf-8"))
    except ValueError:
        return False
    return matched and bool(password_hash)
