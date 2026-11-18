"""ADR 0035 §Decision 7: the secrets check knows the OpenAI key shapes.

The fixtures are made-up strings of the documented SHAPES (`sk-proj-` plus a long
token, `sk-` plus a long alphanumeric run), not keys. A hit is a `hardcoded_secret`
finding carrying the pattern's name and a redacted match, never the key.
"""

from __future__ import annotations

import pytest

from app.services.policy_checks import PolicyCheckStatus, check_secrets_pii

PROJECT_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcdEFGH"
LEGACY_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4"  # sk- + 48 alphanumerics
ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"


def _scan(tmp_path, line: str):
    target = tmp_path / "app" / "settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(line + "\n", encoding="utf-8")
    return check_secrets_pii(str(tmp_path))


@pytest.mark.parametrize(
    ("secret", "pattern"),
    [
        (PROJECT_KEY, "openai_project_key"),
        ("sk-svcacct-" + "x9Y8z7W6v5U4t3S2r1Q0pOnMlKjIhGfEdCbA", "openai_project_key"),
        (LEGACY_KEY, "openai_key"),
    ],
)
def test_an_openai_key_shape_is_flagged_by_name_and_never_stored_whole(tmp_path, secret, pattern):
    # Not named `api_key = ...`, so only the shape patterns can see it.
    result = _scan(tmp_path, f'CLIENT_CREDENTIAL = "{secret}"')

    assert result.result == PolicyCheckStatus.FLAG
    assert [f["pattern"] for f in result.findings] == [pattern]
    assert result.findings[0]["type"] == "hardcoded_secret"
    assert secret not in str(result.findings)


@pytest.mark.parametrize(
    "text",
    [
        "the risk-assessment-and-task-planning notes",  # words ending in "sk"
        "sk-short",  # too short to be a key
        "sk-proj-tooshort",  # the project prefix with a short token
        "sk-" + "a" * 20,  # a legacy-looking run below the 32-character floor
        "disk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",  # `sk-` is inside a word, not a key prefix
    ],
)
def test_text_that_is_not_an_openai_key_is_not_flagged(tmp_path, text):
    result = _scan(tmp_path, f"# {text}")

    assert result.result == PolicyCheckStatus.PASS
    assert result.findings == []


def test_an_anthropic_key_is_still_matched_as_anthropic_only(tmp_path):
    """The new legacy-form pattern must not double-report the existing Anthropic one."""
    result = _scan(tmp_path, f'CLIENT_CREDENTIAL = "{ANTHROPIC_KEY}"')

    assert [f["pattern"] for f in result.findings] == ["anthropic_key"]
