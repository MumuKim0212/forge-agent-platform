"""the suite never sees an ambient credential.

`app.config.settings` is built at import from `backend/.env` and from the process environment, before
any fixture can run, and `conftest.py` switches dotenv off only afterwards. So a credential the owner
(or a CI secret) put in either place reached every test through that singleton, and through every
`Settings(...)` built without an explicit value (the environment source is a separate, still-active
source). The admin DSN was neutralised on its own; this module is the same thing for EVERY field
`tests/credential_fields.py` discovers, by construction: no field is named here.

The same module also covers every OTHER field (`neutralise_other_settings`): measured with a `.env`
that sets every key of `.env.example`, 241 tests failed on the developer's kind of machine (an image name
that cannot be pulled, a memory limit of 4 MB, a port that collides with the proxy's admin port, a
changed URL scheme), all green on CI. The singleton therefore starts as a whole equal to a `Settings`
that read no `.env` and no settings variable, exactly what CI runs.

What "unset" means is not a literal. Most credential fields read `None` when unset; a few
(`session_signing_secret`, `preview_signing_secret`, `service_env_encryption_key`) are filled in by
`Settings.model_post_init` with a per-process random value in dev, and the encryption key also records
that it was NOT supplied (`_service_env_encryption_key_configured`). So the unset state is whatever a
`Settings` built with no credential anywhere reads: `credential_free_settings` builds exactly that, and
the guard copies its credential fields (and its underscore-prefixed bookkeeping) onto the singleton.
"""

from __future__ import annotations

import os

from tests.credential_fields import credential_valued_fields

# `database_url` is credential-valued (a prod URL carries a password) but its unset value is the dev
# SQLite file, i.e. the developer's data. `conftest._session_db_guard` owns it and points it at a throwaway
# file instead, so the credential guard leaves it alone.
OWNED_BY_THE_DB_GUARD = ("database_url",)


# `FORGE_ENV` is the one settings variable the suite sets itself, explicitly and permanently
# (`conftest.py`: there is no default, so removing it would make every
# `Settings(...)` raise). Every other field's variable is removed.
# `build_repair_max_attempts` is the second. Production defaults it to 1 (the repair loop on) and
# `conftest.py::_repair_loop_is_off_unless_a_test_turns_it_on` starts every test with it 0, because the
# older executor tests drive the single-attempt seam; a test of the loop sets it itself. The default
# is pinned by `tests/test_repair_setting.py` on a fresh `Settings`, not by the singleton.
SET_BY_THE_SUITE = ("forge_env", "build_repair_max_attempts")


def credential_env_names(settings_cls) -> list[str]:
    """The environment variables that can carry a credential field, matched the way the settings source
    matches them (case-insensitively, whatever the platform's own rule for variable names)."""
    fields = set(credential_valued_fields(settings_cls))
    return [name for name in os.environ if name.lower() in fields]


def settings_env_names(settings_cls) -> list[str]:
    """The environment variables that can carry ANY settings field, except the one the suite sets."""
    fields = set(settings_cls.model_fields) - set(SET_BY_THE_SUITE)
    return [name for name in os.environ if name.lower() in fields]


def strip_credential_env(monkeypatch, settings_cls) -> int:
    """Remove every settings-carrying variable (credentials and the rest) from the process environment
    (restored by `monkeypatch`). Returns how many it removed, never which values."""
    names = settings_env_names(settings_cls)
    for name in names:
        monkeypatch.delenv(name)
    return len(names)


def credential_free_settings(settings_cls):
    """A `Settings` that read no credential from anywhere: dotenv off and, the caller having stripped
    them, no credential variable in the environment. Mode is pinned the way `conftest.py` pins it for
    the singleton, so an ambient `AUTH_MODE=local` cannot make this construction demand a bootstrap
    password."""
    return settings_cls(_env_file=None, forge_env="dev", auth_mode="dev")


def neutralised_names(settings_cls) -> list[str]:
    return [name for name in credential_valued_fields(settings_cls) if name not in OWNED_BY_THE_DB_GUARD]


def neutralise_ambient_credentials(monkeypatch, settings_cls, singleton, names=None) -> list[str]:
    """Put every credential field of `singleton` (and the bookkeeping `model_post_init` derives from
    them) back to the unset state, through `monkeypatch`. Returns the field names it set. `names` is
    for the guard test only: it lets a test leave one field out and watch the checker name it."""
    strip_credential_env(monkeypatch, settings_cls)
    unset = credential_free_settings(settings_cls)
    names = neutralised_names(settings_cls) if names is None else list(names)
    for name in names:
        monkeypatch.setattr(singleton, name, getattr(unset, name))
    for name, value in vars(unset).items():
        if name.startswith("_"):
            monkeypatch.setattr(singleton, name, value)
    return names


def other_names(settings_cls) -> list[str]:
    """Every field that is neither a credential nor one the suite or the DB guard owns."""
    credentials = set(credential_valued_fields(settings_cls))
    return [
        name
        for name in settings_cls.model_fields
        if name not in credentials and name not in SET_BY_THE_SUITE and name not in OWNED_BY_THE_DB_GUARD
    ]


def neutralise_other_settings(monkeypatch, settings_cls, singleton) -> list[str]:
    """Every field that is not a credential goes back to the value a `.env`-free, variable-free
    `Settings` reads. Returns the names it set (never values: none is a credential, but the habit is
    the rule)."""
    unset = credential_free_settings(settings_cls)
    names = other_names(settings_cls)
    for name in names:
        monkeypatch.setattr(singleton, name, getattr(unset, name))
    return names


def other_settings_still_ambient(settings_cls, singleton) -> list[str]:
    """Names of the non-credential fields of `singleton` that differ from a `.env`-free `Settings`."""
    unset = credential_free_settings(settings_cls)
    return [name for name in other_names(settings_cls) if getattr(singleton, name) != getattr(unset, name)]


def plantable_values(settings_cls) -> dict[str, str]:
    """A non-default value, as the text of an environment variable, for every non-credential field that
    accepts one and reads it back unchanged. Derived from the fields and their types (a bool is
    flipped, a number is moved, a string is a marker) and then FILTERED by building a real `Settings`
    from it, so a field with a validator that refuses an arbitrary value is dropped rather than
    guessed at. Not a list of fields: a new setting is planted without anyone remembering to."""
    planted = {}
    for name in other_names(settings_cls):
        field = settings_cls.model_fields[name]
        annotation, default = str(field.annotation), field.default
        if "bool" in annotation:
            value = not default if isinstance(default, bool) else True
        elif "float" in annotation:
            value = (default + 1.5) if isinstance(default, (int, float)) else 7.5
        elif "int" in annotation:
            value = (default + 1) if isinstance(default, int) else 7
        elif annotation == "<class 'str'>" or "str" in annotation:
            value = f"caseam-{name}"
        else:
            continue
        try:
            built = settings_cls(_env_file=None, forge_env="dev", auth_mode="dev", **{name: value})
        except Exception:  # noqa: BLE001 - a validator refused the guess: drop the field, do not guess harder
            continue
        if str(getattr(built, name)) == str(value):
            planted[name] = str(value)
    return planted


def marker_for(name: str) -> str:
    """A distinctive, attributable stand-in for a credential, for the tests that plant one."""
    return f"caseam-marker-{name}-7f3a91"


def marker_value(name: str) -> str:
    """The value planted for `name` in an environment variable. URL-shaped fields get a URL, because
    `app.db` builds an engine from `database_url` at import and a DSN is parsed for its port; none of
    these can connect (`.invalid`, or a SQLite file that is never opened)."""
    if name == "database_url":
        return f"sqlite:///{marker_for(name)}.db"
    if name == "notify_webhook_url":
        return f"https://fake-host.invalid/hook/{marker_for(name)}"  # Settings validates its shape
    if name.endswith("_dsn"):
        return f"postgresql://caseamuser:{marker_for(name)}@fake-host.invalid/forge"
    return marker_for(name)


def fields_still_ambient(settings_cls, singleton, markers: dict[str, str] | None = None) -> list[str]:
    """Names of the credential fields of `singleton` that are NOT in the unset state. Two checks, both
    reported as names only (a value is never returned, so a failing assert on the result cannot print one):

    - set-ness agrees with the credential-free `Settings` (`None` stays `None`, a generated value stays
      generated);
    - none of `markers` (field name -> a planted value) appears in ANY credential field, so a planted
      value that landed in a different field, or survived in a generated one, is seen too.
    """
    unset = credential_free_settings(settings_cls)
    leaked = []
    for name in credential_valued_fields(settings_cls):
        if name in OWNED_BY_THE_DB_GUARD:
            continue
        if (getattr(singleton, name) is None) != (getattr(unset, name) is None):
            leaked.append(name)
    if getattr(singleton, "service_env_encryption_key_configured", False):
        leaked.append("service_env_encryption_key_configured")
    for name in credential_valued_fields(settings_cls):
        seen = getattr(singleton, name) or ""
        if any(marker in seen for marker in (markers or {}).values()) and name not in leaked:
            leaked.append(name)
    return leaked
