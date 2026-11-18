"""a test never sees an ambient credential, whichever field it is in.

What went wrong: `settings` is built at import from `backend/.env` and the process environment, and
the suite only neutralised `service_postgres_admin_dsn`. On 2026-10-26 the owner set
`SERVICE_ENV_ENCRYPTION_KEY` and three tests that assert "the key was not configured" went red on that
machine while CI, with no `.env`, stayed green.

The guard is `conftest._session_credentials_are_unset` over `tests/ambient_credentials.py`; the fields
it covers are whatever `tests/credential_fields.py` discovers, never a list kept beside it. This file
proves it three ways:

1. in this process (whatever the machine holds): no credential field is in a state a credential-free
   `Settings` would not have, and no credential variable is left in the environment;
2. in a child pytest started with a marker planted in EVERY credential environment variable, so the
   singleton carried a value at import for every field (a control shows it did): the same check, run
   there, finds nothing;
3. a mutation inside the suite: leave one field out of the neutralisation and the checker names that
   field, and prints no value.

Every assert is on a list of NAMES or on a local boolean: no assert here
has a credential as an operand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings, settings
from tests.ambient_credentials import (
    OWNED_BY_THE_DB_GUARD,
    credential_env_names,
    credential_free_settings,
    fields_still_ambient,
    marker_for,
    marker_value,
    neutralise_ambient_credentials,
    neutralise_other_settings,
    neutralised_names,
    other_names,
    other_settings_still_ambient,
    plantable_values,
    settings_env_names,
)
from tests.credential_fields import credential_valued_fields

BACKEND_DIR = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 180
CHILD_FLAG = "T105_CHILD_HAS_MARKERS"  # set only in the child; its presence switches the markers on


def _markers() -> dict[str, str]:
    return {name: marker_for(name) for name in credential_valued_fields(Settings)}


def _markers_if_child() -> dict[str, str]:
    return _markers() if os.environ.get(CHILD_FLAG) == "1" else {}


def _child_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in settings_env_names(Settings):  # nothing of the real one is passed on
        env.pop(name, None)
    for name in credential_valued_fields(Settings):
        env[name.upper()] = marker_value(name)
    for name, value in plantable_values(Settings).items():
        env[name.upper()] = value
    env[CHILD_FLAG] = "1"
    env["FORGE_ENV"] = "dev"
    env["AUTH_MODE"] = "dev"
    return env


# -- 1 and 2: the property itself (runs in this process, and in the child below) --------------------


def test_no_credential_field_is_ambient_for_a_test():
    leaked = fields_still_ambient(Settings, settings, _markers_if_child())
    assert leaked == [], "credential fields a test can still see ambiently (names only)"


def test_no_other_setting_is_ambient_for_a_test():
    # Every field that is not a credential equals what a `.env`-free `Settings` reads.
    differing = other_settings_still_ambient(Settings, settings)
    assert differing == [], "settings a test can still see ambiently (names only)"


def test_no_settings_variable_is_left_in_the_environment():
    # Credentials and the rest alike; `FORGE_ENV`, which the suite sets itself, is the one exception.
    assert settings_env_names(Settings) == []
    assert credential_env_names(Settings) == []


def test_the_encryption_key_reads_as_not_supplied():
    # The three tests of 2026-10-26 assert exactly this; here as a local boolean.
    configured = settings.service_env_encryption_key_configured
    assert configured is False


def test_the_signing_secrets_and_the_key_are_still_usable_in_dev():
    # "Unset" for a field with a dev-generated default is a generated value, not None: a session cookie
    # or a preview link is signed with it, and a None would make the signer refuse to run.
    usable = {
        name: bool(getattr(settings, name))
        for name in ("session_signing_secret", "preview_signing_secret", "service_env_encryption_key")
    }
    assert usable == {"session_signing_secret": True, "preview_signing_secret": True, "service_env_encryption_key": True}


def test_the_discovery_covers_the_fields_this_guard_is_about():
    # Not a second list of what to neutralise: a canary that discovery still finds the ones this task was
    # opened for, so an edit to `credential_fields.py` that stops seeing them is noticed here.
    discovered = set(neutralised_names(Settings))
    assert {"service_env_encryption_key", "service_env_encryption_key_old", "github_token"} <= discovered
    assert set(OWNED_BY_THE_DB_GUARD) <= set(credential_valued_fields(Settings))


def _stand_in_plan() -> tuple[dict[str, str], dict[str, str]]:
    """Markers and planted values for a stand-in `Settings`, minus `database_url`: the credential guard
    leaves that one to `_session_db_guard` (tested in `test_db_guard.py`, and on the real singleton in
    the child below)."""
    names = [name for name in credential_valued_fields(Settings) if name not in OWNED_BY_THE_DB_GUARD]
    return {n: marker_for(n) for n in names}, {n: marker_value(n) for n in names}


# -- 3: the checker sees a field the neutralisation leaves out --------------------------------------


@pytest.mark.parametrize("left_out", ["service_env_encryption_key_old", "github_token", "session_signing_secret"])
def test_a_field_left_out_of_the_neutralisation_is_named_without_its_value(monkeypatch, left_out):
    markers, planted = _stand_in_plan()
    stand_in = Settings(_env_file=None, forge_env="dev", auth_mode="dev", **planted)
    # The control: with nothing neutralised, the checker sees the planted state.
    before = fields_still_ambient(Settings, stand_in, markers)
    assert left_out in before

    names = [name for name in neutralised_names(Settings) if name != left_out]
    neutralise_ambient_credentials(monkeypatch, Settings, stand_in, names=names)
    after = fields_still_ambient(Settings, stand_in, markers)
    printed_a_value = any(marker in " ".join(after) for marker in markers.values())
    assert after == [left_out]
    assert printed_a_value is False


def test_the_whole_neutralisation_leaves_the_stand_in_clean(monkeypatch):
    markers, planted = _stand_in_plan()
    stand_in = Settings(_env_file=None, forge_env="dev", auth_mode="dev", **planted)
    set_by_the_guard = neutralise_ambient_credentials(monkeypatch, Settings, stand_in)
    assert set_by_the_guard == neutralised_names(Settings)
    leaked = fields_still_ambient(Settings, stand_in, markers)
    # `database_url` is the DB guard's; the rest are all back to the credential-free state.
    assert leaked == []


def test_the_other_settings_are_reset_and_a_field_left_out_is_named(monkeypatch):
    plan = plantable_values(Settings)
    assert len(plan) >= len(other_names(Settings)) // 2  # the plan reaches most of them
    stand_in = Settings(_env_file=None, forge_env="dev", auth_mode="dev", **plan)
    before = other_settings_still_ambient(Settings, stand_in)
    assert sorted(before) == sorted(plan)  # the control: every planted field differs from the defaults

    left_out = sorted(plan)[0]
    kept = [name for name in other_names(Settings) if name != left_out]
    from tests import ambient_credentials

    mutation = pytest.MonkeyPatch()  # a second one: the checker below must see every name, only the neutraliser is cut
    mutation.setattr(ambient_credentials, "other_names", lambda cls: kept)
    try:
        neutralise_other_settings(monkeypatch, Settings, stand_in)
    finally:
        mutation.undo()
    assert other_settings_still_ambient(Settings, stand_in) == [left_out]


def test_the_other_settings_neutralisation_leaves_the_stand_in_clean(monkeypatch):
    stand_in = Settings(_env_file=None, forge_env="dev", auth_mode="dev", **plantable_values(Settings))
    reset = neutralise_other_settings(monkeypatch, Settings, stand_in)
    assert reset == other_names(Settings)
    assert other_settings_still_ambient(Settings, stand_in) == []


def test_the_credential_free_settings_supplies_no_key():
    unset = credential_free_settings(Settings)
    supplied = unset.service_env_encryption_key_configured
    assert supplied is False


# -- 2: a child process that starts with every credential planted -----------------------------------


def _run_child(*args: str, code: str | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, *(["-c", code] if code else ["-m", "pytest", *args])]
    return subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


def test_the_control_shows_every_credential_field_carries_its_marker_at_import():
    """Without the guard, a process started this way has a planted value in each credential field: the
    child below is not vacuous. Prints booleans per field name, never a value."""
    code = (
        "import json, os\n"
        "from app.config import Settings, settings\n"
        "from tests.ambient_credentials import marker_for\n"
        "from tests.credential_fields import credential_valued_fields\n"
        "out = {n: (marker_for(n) in (getattr(settings, n) or '')) for n in credential_valued_fields(Settings)}\n"
        "print('CONTROL=' + json.dumps(out))\n"
        "from tests.ambient_credentials import plantable_values\n"
        "planted = plantable_values(Settings)\n"
        "carried = {n: (str(getattr(settings, n)) == v) for n, v in planted.items()}\n"
        "print('CONTROL_OTHER=' + json.dumps(carried))\n"
    )
    result = _run_child(code=code)
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("CONTROL=")), None)
    assert line is not None, result.stderr[-1500:]
    carried = json.loads(line.removeprefix("CONTROL="))
    assert sorted(name for name, ok in carried.items() if not ok) == []
    assert sorted(carried) == sorted(credential_valued_fields(Settings))

    other_line = next((ln for ln in result.stdout.splitlines() if ln.startswith("CONTROL_OTHER=")), None)
    assert other_line is not None, result.stderr[-1500:]
    other_carried = json.loads(other_line.removeprefix("CONTROL_OTHER="))
    assert sorted(name for name, ok in other_carried.items() if not ok) == []
    # The plan reaches most non-credential fields (a field a validator refuses is dropped, and that
    # is visible here as a count, not a silent shrink).
    assert len(other_carried) >= len(other_names(Settings)) // 2


def test_a_test_in_a_process_started_with_every_credential_planted_sees_none_of_them():
    if os.environ.get(CHILD_FLAG) == "1":
        pytest.skip("this is the child: the property above is what it runs")
    node = f"tests/{Path(__file__).name}::test_no_credential_field_is_ambient_for_a_test"
    others = [
        f"tests/{Path(__file__).name}::test_no_other_setting_is_ambient_for_a_test",
        f"tests/{Path(__file__).name}::test_no_settings_variable_is_left_in_the_environment",
        f"tests/{Path(__file__).name}::test_the_encryption_key_reads_as_not_supplied",
        f"tests/{Path(__file__).name}::test_the_signing_secrets_and_the_key_are_still_usable_in_dev",
    ]
    result = _run_child(node, *others, "-p", "no:cacheprovider", "-q")
    tail = result.stdout[-2000:]
    ran_all = "5 passed" in tail
    assert (result.returncode, ran_all) == (0, True), tail
