"""ADR 0035 §Decision 5: `services/engines.py` is the one place an engine is decided.

Pure functions over a `Settings`-shaped object and a DB session that nothing reads yet (engine
selection makes it the source). No Docker.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import engines
from app.services.engines import (
    EngineChoice,
    Unavailable,
    available_engines,
    engine_config,
    resolve_engine,
    sandbox_executor_active,
)


def _settings(**overrides):
    base = dict(
        use_sandbox_executor=True,
        anthropic_api_key=None,
        openai_api_key=None,
        agent_engine_default=None,
        anthropic_upstream_base_url="https://anthropic.example",
        openai_upstream_base_url="https://openai.example",
        agent_sandbox_image="claude-image:1",
        openai_agent_sandbox_image="openai-image:1",
        openai_model="gpt-5-mini",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- availability is "the key is configured" ----------------------------------------


@pytest.mark.parametrize(
    ("anthropic", "openai", "expected"),
    [
        (None, None, ()),
        ("a-key", None, ("anthropic",)),
        (None, "o-key", ("openai",)),
        ("a-key", "o-key", ("anthropic", "openai")),
        ("   ", "", ()),  # blank is not configured
    ],
)
def test_an_engine_is_available_when_its_key_is_configured(anthropic, openai, expected):
    assert available_engines(_settings(anthropic_api_key=anthropic, openai_api_key=openai)) == expected


# --- the engine-neutral opt-in ---------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "anthropic", "openai", "active"),
    [
        (True, "a", None, True),
        (True, None, "o", True),  # an OpenAI-only deployment is active
        (True, "a", "o", True),
        (True, None, None, False),  # the flag alone changes nothing
        (False, "a", "o", False),  # keys alone change nothing
    ],
)
def test_the_executor_is_active_only_with_the_flag_and_at_least_one_engine(flag, anthropic, openai, active):
    settings = _settings(use_sandbox_executor=flag, anthropic_api_key=anthropic, openai_api_key=openai)

    assert sandbox_executor_active(settings) is active


# --- resolve_engine ---------------------------------------------------------------------


def test_the_selected_engine_wins_when_both_are_available(db_session):
    s = _settings(anthropic_api_key="a", openai_api_key="o", agent_engine_default="openai")

    choice = resolve_engine(db_session, s)

    assert isinstance(choice, EngineChoice) and choice.engine == "openai"


def test_with_nothing_selected_both_available_means_anthropic(db_session):
    choice = resolve_engine(db_session, _settings(anthropic_api_key="a", openai_api_key="o"))

    assert isinstance(choice, EngineChoice) and choice.engine == "anthropic"


def test_with_nothing_selected_the_only_available_engine_is_the_default(db_session):
    assert resolve_engine(db_session, _settings(openai_api_key="o")).engine == "openai"
    assert resolve_engine(db_session, _settings(anthropic_api_key="a")).engine == "anthropic"


def test_the_openai_choice_carries_its_model_image_dialect_key_and_upstream(db_session):
    choice = resolve_engine(
        db_session, _settings(openai_api_key="o-key", agent_engine_default="openai", openai_model="gpt-5-nano")
    )

    assert choice == EngineChoice(
        engine="openai",
        model="gpt-5-nano",
        image="openai-image:1",
        dialect="openai",
        api_key="o-key",
        upstream_base_url="https://openai.example",
    )


def test_the_anthropic_choice_has_no_configured_model_because_the_claude_runner_passes_none(db_session):
    choice = resolve_engine(db_session, _settings(anthropic_api_key="a-key", agent_engine_default="anthropic"))

    assert choice == EngineChoice(
        engine="anthropic",
        model=None,
        image="claude-image:1",
        dialect="anthropic",
        api_key="a-key",
        upstream_base_url="https://anthropic.example",
    )


def test_a_selected_engine_without_its_key_is_unavailable_and_never_the_other_one(db_session):
    """The no-fallback rule: OpenAI is selected, only Anthropic has a key. The result names
    OpenAI as unavailable; it does not hand back the Anthropic choice."""
    s = _settings(anthropic_api_key="a-key", agent_engine_default="openai")

    result = resolve_engine(db_session, s)

    assert isinstance(result, Unavailable)
    assert result.engine == "openai"
    assert "openai" in result.detail and "OPENAI_API_KEY" in result.detail
    assert "not run on another engine" in result.detail
    assert "a-key" not in result.detail


def test_nothing_configured_and_nothing_selected_is_anthropic_unavailable(db_session):
    result = resolve_engine(db_session, _settings())

    assert isinstance(result, Unavailable) and result.engine == "anthropic"
    assert "ANTHROPIC_API_KEY" in result.detail


def test_the_choice_never_shows_its_key_in_its_repr(db_session):
    choice = resolve_engine(db_session, _settings(openai_api_key="SECRET-VALUE-123", agent_engine_default="openai"))

    assert "SECRET-VALUE-123" not in repr(choice)


# --- engine_config: configuration without a decision --------------------------------------


def test_engine_config_reads_a_build_s_engine_without_checking_availability():
    config = engine_config("openai", _settings())  # no key configured

    assert config.engine == "openai" and config.api_key == "" and config.dialect == "openai"


def test_engine_config_refuses_an_engine_outside_the_closed_set():
    with pytest.raises(ValueError, match="unknown engine"):
        engine_config("gemini", _settings())


def test_the_closed_set_is_exactly_two_engines():
    assert engines.ENGINES == ("anthropic", "openai")


# --- the real Settings: the new fields and their blank handling --------------------------


def test_settings_defaults_for_the_new_fields(monkeypatch):
    for name in ("OPENAI_MODEL", "OPENAI_AGENT_SANDBOX_IMAGE", "AGENT_ENGINE_DEFAULT"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)

    assert s.openai_model == "gpt-5-mini"
    assert s.openai_agent_sandbox_image == "forge-agent-sandbox-openai:latest"
    assert s.agent_engine_default is None


@pytest.mark.parametrize(("raw", "expected"), [("openai", "openai"), ("anthropic", "anthropic"), ("", None), ("  ", None)])
def test_agent_engine_default_accepts_the_two_engines_and_blank(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_ENGINE_DEFAULT", raw)

    assert Settings(_env_file=None).agent_engine_default == expected


def test_agent_engine_default_refuses_any_other_value(monkeypatch):
    monkeypatch.setenv("AGENT_ENGINE_DEFAULT", "gemini")

    with pytest.raises(ValueError):
        Settings(_env_file=None)
