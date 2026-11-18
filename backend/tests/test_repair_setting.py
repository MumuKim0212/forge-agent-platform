"""`BUILD_REPAIR_MAX_ATTEMPTS`.

The default is what ships (1), the allowed range is 0 to 3 and anything else refuses to start, and
the setting is not a credential. (The `.env.example` / `.env.example.ko` entries are checked
entry for entry by `test_config.py`.)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_the_production_default_is_one_repair_attempt():
    assert _settings().build_repair_max_attempts == 1


@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_zero_to_three_is_accepted(value):
    assert _settings(build_repair_max_attempts=value).build_repair_max_attempts == value


@pytest.mark.parametrize("value", [-1, 4, 100])
def test_a_value_outside_zero_to_three_refuses_to_start(value):
    with pytest.raises(ValidationError):
        _settings(build_repair_max_attempts=value)


def test_it_is_read_from_the_environment_by_its_documented_name(monkeypatch):
    monkeypatch.setenv("BUILD_REPAIR_MAX_ATTEMPTS", "2")

    assert _settings().build_repair_max_attempts == 2


def test_it_is_not_a_credential_field():
    from tests.credential_fields import credential_looking_fields, credential_valued_fields

    assert "build_repair_max_attempts" not in credential_valued_fields(Settings)
    assert "build_repair_max_attempts" not in credential_looking_fields(Settings)


# --- the column and its API schema -----------------------------------------------------


def test_a_new_build_has_no_repair_attempts_and_the_count_is_in_its_api_schema(make_build_with_request, db_session):
    from app.schemas.build import BuildOut

    build = make_build_with_request()
    db_session.flush()

    assert build.repair_attempts == 0
    assert BuildOut.model_validate(build).repair_attempts == 0

    build.repair_attempts = 2
    db_session.flush()

    assert BuildOut.model_validate(build).model_dump()["repair_attempts"] == 2
