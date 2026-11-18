"""which `Settings` fields hold a credential.

One discovery rule, shared by two guards so a future credential field cannot slip past one
of them:

- `test_config_fail_safe.py::test_every_secret_looking_setting_reads_blank_as_unset`:
  a blank value of such a field must read as unset;
- `test_settings_never_print_credentials.py`: such a field must be left out of
  `repr`/`str`, so a failing assert, a traceback or a log line that formats the settings object
  cannot print it.

The rule is a heuristic on the NAME (and the annotation): it makes
the common case (a field named like a credential) arrive with a failing test, not a proof that
nothing else is a credential. Whatever it does not see, the pinned set in
`test_settings_never_print_credentials.py` is the explicit verdict.
"""

from __future__ import annotations

CREDENTIAL_WORDS = ("secret", "key", "token", "password", "dsn")

# Credential-valued fields whose name carries none of the words above. `database_url` is the
# platform's own database; in production it is `postgresql://user:password@host/db`.
# `notify_webhook_url` -- a Slack incoming-webhook URL is its own bearer secret.
CREDENTIAL_FIELDS_BY_NAME = ("database_url", "notify_webhook_url")


def credential_looking_fields(settings_cls) -> list[str]:
    """String-typed fields whose name contains a credential word. A non-string
    field such as the integer `build_token_budget` is not a credential."""
    return [
        name
        for name, field in settings_cls.model_fields.items()
        if any(word in name for word in CREDENTIAL_WORDS) and "str" in str(field.annotation)
    ]


def credential_valued_fields(settings_cls) -> list[str]:
    """`credential_looking_fields` plus the fields named in `CREDENTIAL_FIELDS_BY_NAME`."""
    names = credential_looking_fields(settings_cls)
    for name in CREDENTIAL_FIELDS_BY_NAME:
        if name in settings_cls.model_fields and name not in names:
            names.append(name)
    return names


def fields_missing_redaction(settings_cls) -> list[str]:
    """Credential-valued fields of `settings_cls` that are still part of its `repr`."""
    return [name for name in credential_valued_fields(settings_cls) if settings_cls.model_fields[name].repr]
