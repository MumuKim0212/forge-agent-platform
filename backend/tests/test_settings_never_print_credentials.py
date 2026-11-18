"""`Settings` never prints a credential.

The hazard. `Settings` used to have no redaction in its `repr`. pytest's assertion rewriting prints
`repr(obj)` in the `where` clause of any failing `assert obj.x ...`, and a traceback or a log line
that formats the object prints it too. The process-wide `settings` holds the developer's `backend/.env`:
the API keys, the signing secrets, the encryption key, the dev Postgres admin DSN. About 80 assert
lines in 18 test files are headed by `settings.`, so one failing assert would have printed all of it
(an earlier change only neutralised the admin DSN).

The fix is `Field(repr=False)` on every credential-valued field, and `hide_input_in_errors` so a
`ValidationError` (a misspelled credential key in `.env` is an "extra input" error) does not carry
the value either. This file proves it with MARKER values only:

- every `Settings` here is built with `_env_file=None` and marker values; nothing in this file reads
  `app.config.settings`, the ambient `.env`, or anything else that could hold a real value;
- the markers are derived from the same discovery rule the blank-is-unset guard uses
  (`tests/credential_fields.py`), so a credential field added later is given a marker here without
  anyone remembering to;
- a control for each check shows the check CAN see a leak: a subclass whose field is left in the
  repr, and a config that shows input, are made to leak and the same probe finds the marker.

What it does not cover (also pinned, below): an assert written directly on a credential attribute
prints that operand, because that value is what the assert compares. The rule is to
compute a local first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import Field, ValidationError, create_model
from pydantic_settings import SettingsConfigDict

from app.config import Settings
from tests.credential_fields import (
    credential_looking_fields,
    credential_valued_fields,
    fields_missing_redaction,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 120

# The explicit verdict, as a literal (not derived from `config.py`): every field that is left out of
# `repr`. The ten blank-is-unset credential settings, plus `database_url` (a production URL
# is `postgresql://user:password@host/db`). Adding a field here without a credential reason, or
# adding a credential field without joining this list, fails the first test below.
REDACTED_FIELDS = {
    "session_signing_secret",
    "preview_signing_secret",
    "service_env_encryption_key",
    "service_env_encryption_key_old",
    "anthropic_api_key",
    "openai_api_key",
    "github_token",
    "oidc_client_secret",
    "bootstrap_admin_password",
    "service_postgres_admin_dsn",
    "database_url",
    "notify_webhook_url",  # a webhook URL is a bearer secret
    "notify_smtp_password",
}


def _token(name: str) -> str:
    """A distinctive marker for one field; the field name makes a leak attributable."""
    return f"caseal-marker-{name}-7f3a91"


def _value(name: str, token: str) -> str:
    """The value given to the field. A URL-shaped field gets a URL with the token as its password,
    so the check covers both the whole value and the password inside it."""
    if name == "notify_webhook_url":
        return f"https://casealuser:{token}@fake-host.invalid/hook"  # `Settings` validates its shape
    if name.endswith("_dsn") or name.endswith("_url"):
        return f"postgresql://casealuser:{token}@fake-host.invalid/forge"
    return token


def _marked_settings() -> tuple[Settings, dict[str, tuple[str, str]]]:
    """A dev `Settings` with a marker in EVERY credential-valued field, and `{name: (value, token)}`."""
    marks = {name: (_value(name, _token(name)), _token(name)) for name in credential_valued_fields(Settings)}
    settings = Settings(_env_file=None, forge_env="dev", **{name: value for name, (value, _) in marks.items()})
    return settings, marks


def _leaked(text: str, marks: dict[str, tuple[str, str]]) -> list[str]:
    """Names of the fields whose value, or whose token, appears in `text`. Names only, so a failing
    assertion built on this prints no value either."""
    return [name for name, (value, token) in marks.items() if value in text or token in text]


# --- 1. the set is pinned, and the scan notices a field that joins without the redaction -----------


def test_exactly_the_credential_fields_are_left_out_of_the_repr():
    left_out = {name for name, field in Settings.model_fields.items() if not field.repr}
    assert left_out == REDACTED_FIELDS


def test_every_credential_looking_field_is_redacted():
    """Each of these names says it holds a credential. A new one without
    `Field(repr=False)` fails here by name."""
    assert fields_missing_redaction(Settings) == []
    # Not vacuous: the scan really does reach every pinned field, `database_url` (named by the
    # explicit rule, not by a credential word) included.
    assert set(credential_valued_fields(Settings)) >= REDACTED_FIELDS


@pytest.mark.parametrize(
    "name",
    ["payment_secret", "vendor_api_key", "partner_token", "ldap_password", "audit_dsn"],
)
def test_the_scan_fails_for_a_new_credential_field_without_the_redaction(name):
    """Planted in a throwaway subclass, never in `config.py`: the scan names the field. The same
    field with `repr=False` passes, so the failure is about the redaction and nothing else."""
    # Relative to what `Settings` itself is missing (nothing, today), so a real regression there
    # is reported by the tests above and does not turn every row of this one red as well.
    already_missing = fields_missing_redaction(Settings)
    planted = create_model("PlantedSettings", __base__=Settings, **{name: (str | None, None)})
    assert [n for n in fields_missing_redaction(planted) if n not in already_missing] == [name]
    redacted = create_model(
        "RedactedSettings", __base__=Settings, **{name: (str | None, Field(default=None, repr=False))}
    )
    assert name not in fields_missing_redaction(redacted)


def test_the_scan_leaves_alone_a_field_that_is_not_a_credential():
    """An integer named like a credential (the real `build_token_budget` is one) and a string that
    is not named like one are neither flagged: the guard is about credentials, not every field."""
    planted = create_model(
        "NotCredentialSettings", __base__=Settings, retry_token_count=(int, 3), display_name=(str, "x")
    )
    flagged = fields_missing_redaction(planted)
    assert "retry_token_count" not in flagged and "display_name" not in flagged
    assert "build_token_budget" not in credential_looking_fields(Settings)


# --- 2. no marker in repr / str -----------------------------------------------------------------------


@pytest.mark.parametrize("render", [repr, str], ids=["repr", "str"])
def test_a_settings_built_with_markers_prints_none_of_them(render):
    settings, marks = _marked_settings()
    # The markers really are in the object (a validator that read one as unset would make the
    # check below pass for the wrong reason).
    held = {name: getattr(settings, name) for name in marks}
    assert held == {name: value for name, (value, _) in marks.items()}

    text = render(settings)
    assert _leaked(text, marks) == []
    # Not vacuous: the rest of the object is still printed.
    assert "forge_env='dev'" in text


def test_the_probe_can_see_a_leak_a_field_left_in_the_repr_shows_up():
    """The control for the test above: the same probe, on a subclass whose new field was not
    redacted, finds its marker in both `repr` and `str`."""
    token = "caseal-control-leaky-9d2e"
    leaky_cls = create_model("LeakySettings", __base__=Settings, leaky_api_key=(str | None, None))
    leaky = leaky_cls(_env_file=None, forge_env="dev", leaky_api_key=token)
    assert token in repr(leaky)
    assert token in str(leaky)


# --- 3. no marker in pytest's own assertion output ------------------------------------------------------


def _show_whole_repr_in_assertion_messages(request, monkeypatch):
    """At the default verbosity pytest cuts the repr it prints in a `where` clause to about 240
    characters, so a field in the middle of it would not show and this file would pass without
    proving that field redacted. Raising the verbosity (`request.config.option.verbose`, the value
    `-vv` sets) lifts the cut for the asserts in the test that calls this."""
    monkeypatch.setattr(request.config.option, "verbose", 2)


def test_a_failed_assert_prints_no_credential_from_the_settings_object(request, monkeypatch):
    """The path of the incident: `assert settings.<x> is None` fails, and pytest's message carries
    `where ... = Settings(...).<x>`. (A `==` between two strings gets pytest's own diff, which
    drops the `where` clause, so the probe uses `is None`.)"""
    _show_whole_repr_in_assertion_messages(request, monkeypatch)
    settings, marks = _marked_settings()
    with pytest.raises(AssertionError) as exc_info:
        assert settings.forge_env is None  # fails on purpose
    message = str(exc_info.value)
    # The WHOLE repr reached the message, so any field left in it would be there to find.
    assert repr(settings) in message
    assert _leaked(message, marks) == []


def test_a_failed_assert_on_a_field_left_in_the_repr_does_print_it(request, monkeypatch):
    """The control: with one field left in the repr, the same failed assert prints the marker."""
    _show_whole_repr_in_assertion_messages(request, monkeypatch)
    token = "caseal-control-leaky-9d2e"
    leaky_cls = create_model("LeakySettings", __base__=Settings, leaky_api_key=(str | None, None))
    leaky = leaky_cls(_env_file=None, forge_env="dev", leaky_api_key=token)
    with pytest.raises(AssertionError) as exc_info:
        assert leaky.forge_env is None  # fails on purpose
    assert token in str(exc_info.value)


def test_a_failing_assert_written_directly_on_a_credential_prints_that_operand():
    """What redaction cannot do, pinned so the guidance has its evidence: the value an
    assert compares is printed, whatever the repr hides. The way out is a local computed first."""
    settings, marks = _marked_settings()
    _, token = marks["anthropic_api_key"]

    with pytest.raises(AssertionError) as exc_info:
        assert settings.anthropic_api_key is None  # fails on purpose
    assert token in str(exc_info.value)

    with pytest.raises(AssertionError) as exc_info:
        key_is_unset = settings.anthropic_api_key is None
        assert key_is_unset, "ANTHROPIC_API_KEY is set"  # fails on purpose; the operand is a bool
    assert _leaked(str(exc_info.value), marks) == []


def _child_env() -> dict[str, str]:
    """This process's environment without anything that could hold a real credential or change
    the mode, and `FORGE_ENV=dev`. `PYTHONPATH` carries `backend/`: the cwd is a temp directory."""
    credential_vars = {name.upper() for name in credential_valued_fields(Settings)}
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in credential_vars and k not in {"FORGE_ENV", "AUTH_MODE", "CORS_ORIGINS"}
    }
    env["FORGE_ENV"] = "dev"
    env["PYTHONPATH"] = str(BACKEND_DIR)
    return env


_CHILD_TESTS = '''\
import json
import os

from pydantic import create_model

from app.config import Settings

MARKERS = json.loads(os.environ["T102_MARKERS"])
CONTROL = os.environ["T102_CONTROL"]


def test_redacted_fails_on_purpose():
    settings = Settings(_env_file=None, forge_env="dev", **MARKERS)
    assert settings.forge_env is None


def test_leaky_control_fails_on_purpose():
    leaky_cls = create_model("LeakySettings", __base__=Settings, leaky_api_key=(str | None, None))
    settings = leaky_cls(_env_file=None, forge_env="dev", leaky_api_key=CONTROL)
    assert settings.forge_env is None
'''


def _run_child_pytest(tmp_path: Path, test_name: str, marks, control: str) -> str:
    """Run one deliberately failing test in a fresh `pytest -vv -l` (unlimited repr, locals shown)
    and return everything it printed. The cwd is empty, so no `.env` is found."""
    (tmp_path / "test_child.py").write_text(_CHILD_TESTS, encoding="utf-8")
    env = _child_env()
    env["T102_MARKERS"] = json.dumps({name: value for name, (value, _) in marks.items()})
    env["T102_CONTROL"] = control
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-vv", "-l", "-p", "no:cacheprovider",
            f"--rootdir={tmp_path}", f"test_child.py::{test_name}",
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    assert result.returncode == 1, "the child test should FAIL (exit 1), not error out:\n" + _tail(result)
    return result.stdout + result.stderr


def _tail(result) -> str:
    # A diagnostic for the assertion above: the child ran only marker values, but print nothing
    # more than the tail, so a surprise stays short.
    return (result.stdout + result.stderr)[-1500:]


def test_pytest_vv_output_for_a_failed_assert_carries_no_credential(tmp_path):
    """The un-truncated version of the path above, as a real `pytest -vv -l` prints it: the `where`
    clause AND the traceback's locals show the whole `Settings` repr."""
    _, marks = _marked_settings()
    control = "caseal-control-leaky-9d2e"
    output = _run_child_pytest(tmp_path, "test_redacted_fails_on_purpose", marks, control)
    assert "Settings(" in output  # the repr is in the output, so the check is not vacuous
    assert "assert 'dev' is None" in output  # and it is the deliberate failure that printed it
    assert _leaked(output, marks) == []


def test_pytest_vv_output_would_show_a_field_left_in_the_repr(tmp_path):
    """The control for the test above, through the same child-process path."""
    _, marks = _marked_settings()
    control = "caseal-control-leaky-9d2e"
    output = _run_child_pytest(tmp_path, "test_leaky_control_fails_on_purpose", marks, control)
    assert "LeakySettings(" in output
    assert control in output


# --- 4. a validation error does not carry the value either ---------------------------------------------


def test_a_validation_error_from_an_env_file_never_carries_the_credential(tmp_path):
    """`Settings` rejects an unknown key, so `ANTHROPIC_API_KEYY=<real key>` (a typo) in `.env` is
    an "extra input" error, and by default pydantic prints `input_value=<the value>` in it. An earlier case
    had this shape: a `backend/.env` holding a key the class did not know stopped pytest at
    import."""
    token = "caseal-marker-typo-4c1d"
    env_file = tmp_path / "typo.env"
    env_file.write_text(f"FORGE_ENV=dev\nANTHROPIC_API_KEYY={token}\n", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=env_file)
    text = str(exc_info.value)
    # It failed for the reason under test, and the key's NAME is still there to act on.
    assert "anthropic_api_keyy" in text and "extra_forbidden" in text
    assert token not in text
    assert token not in repr(exc_info.value)


def test_the_probe_can_see_an_input_value_in_a_validation_error(tmp_path):
    """The control: the same file under a config that shows input prints the marker."""

    class ShowsInput(Settings):
        model_config = SettingsConfigDict(hide_input_in_errors=False)

    token = "caseal-marker-typo-4c1d"
    env_file = tmp_path / "typo.env"
    env_file.write_text(f"FORGE_ENV=dev\nANTHROPIC_API_KEYY={token}\n", encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        ShowsInput(_env_file=env_file)
    assert token in str(exc_info.value)
