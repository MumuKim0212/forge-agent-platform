"""ADR 0035 §Decision 3: a proxy container runs ONE dialect with that dialect's one key.

No Docker: the entry point's `build_app(environ)` and `ProxyContainer.start`'s environment are
exercised against fakes. The container-level proof (the packaged image really serves the `openai`
dialect) is `test_proxy_container_openai_integration.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from docker.errors import DockerException
from fastapi.testclient import TestClient

from app import proxy_server
from app.config import Settings
from app.services.proxy_container import ProxyContainer, ProxyStartError
from app.services.proxy_dialects import UnknownDialectError
from tests.openai_proxy_support import ADMIN_SECRET, body

ANTHROPIC_ENV = {"FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET, "FORGE_PROXY_ANTHROPIC_API_KEY": "sk-ant-fake"}
OPENAI_ENV = {
    "FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET,
    "FORGE_PROXY_DIALECT": "openai",
    "FORGE_PROXY_OPENAI_API_KEY": "sk-proj-fake",
}


def _token(client: TestClient) -> str:
    return client.post(
        "/_forge_admin/tokens",
        json={"build_id": "b", "token_budget": 1000, "cost_budget_usd": 1.0},
        headers={"X-Forge-Admin-Secret": ADMIN_SECRET},
    ).json()["token"]


# --- the entry point ----------------------------------------------------------------------------


def test_no_dialect_variable_means_anthropic():
    client = TestClient(proxy_server.build_app(ANTHROPIC_ENV))
    token = _token(client)

    refused = client.post("/v1/responses", headers={"x-api-key": token}, json=body())
    # The Messages endpoint is this dialect's own, so a body-level refusal there (and not an endpoint
    # refusal) proves it is the dialect in force, without forwarding anything to a real upstream.
    mcp = client.post("/v1/messages", headers={"x-api-key": token}, json={"model": "m", "mcp_servers": []})

    assert refused.status_code == 403 and "endpoint POST /v1/responses is not allowed" in refused.json()["detail"]
    assert mcp.status_code == 403 and mcp.json()["detail"] == "tool policy: request field 'mcp_servers' is not allowed"


def test_an_empty_dialect_variable_means_anthropic_too():
    client = TestClient(proxy_server.build_app({**ANTHROPIC_ENV, "FORGE_PROXY_DIALECT": ""}))
    token = _token(client)

    refused = client.post("/v1/responses", headers={"x-api-key": token}, json=body())

    assert refused.status_code == 403 and "detail" in refused.json()  # the Messages API error envelope


def test_the_openai_dialect_forwards_responses_and_refuses_messages_with_its_own_envelope():
    client = TestClient(proxy_server.build_app(OPENAI_ENV))
    token = _token(client)
    headers = {"authorization": f"Bearer {token}"}

    on_messages = client.post("/v1/messages", headers=headers, json={"model": "m", "messages": []})
    web_search = client.post("/v1/responses", headers=headers, json=body(tools=[{"type": "web_search"}]))

    assert on_messages.status_code == 403
    assert set(on_messages.json()) == {"error"}
    assert web_search.status_code == 403
    assert web_search.json()["error"]["message"] == "tool policy: tool type 'web_search' is not allowed"


@pytest.mark.parametrize("name", ["gemini", "OpenAI", "anthropic ", "openai,anthropic", "both"])
def test_an_unknown_dialect_refuses_to_start(name):
    with pytest.raises(UnknownDialectError):
        proxy_server.build_app({**OPENAI_ENV, "FORGE_PROXY_DIALECT": name})


def test_main_exits_with_a_message_for_an_unknown_dialect(monkeypatch):
    monkeypatch.setattr(proxy_server, "uvicorn", MagicMock())
    for key in ("FORGE_PROXY_ANTHROPIC_API_KEY", "FORGE_PROXY_OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FORGE_PROXY_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("FORGE_PROXY_DIALECT", "gemini")

    with pytest.raises(SystemExit) as exit_info:
        proxy_server.main()

    assert "unknown proxy dialect 'gemini'" in str(exit_info.value)
    proxy_server.uvicorn.run.assert_not_called()


def test_a_dialect_reads_only_its_own_key_variable():
    # openai dialect, but only the anthropic key present: the other vendor's key is not a substitute.
    with pytest.raises(KeyError, match="FORGE_PROXY_OPENAI_API_KEY"):
        proxy_server.build_app(
            {"FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET, "FORGE_PROXY_DIALECT": "openai", "FORGE_PROXY_ANTHROPIC_API_KEY": "sk-ant-fake"}
        )
    with pytest.raises(KeyError, match="FORGE_PROXY_ANTHROPIC_API_KEY"):
        proxy_server.build_app({"FORGE_PROXY_ADMIN_SECRET": ADMIN_SECRET, "FORGE_PROXY_OPENAI_API_KEY": "sk-proj-fake"})


def test_a_missing_admin_secret_refuses_to_start():
    with pytest.raises(KeyError, match="FORGE_PROXY_ADMIN_SECRET"):
        proxy_server.build_app({"FORGE_PROXY_ANTHROPIC_API_KEY": "sk-ant-fake"})


# --- the launcher -------------------------------------------------------------------------------


def _start_capturing_env(**start_kwargs) -> dict:
    """Runs `ProxyContainer.start` against a fake Docker client whose `containers.run` records the
    environment it was given and then fails, so no container is needed."""
    client = MagicMock()
    client.containers.run.side_effect = DockerException("stop here")
    # `start` reads the image's capability label first; this one advertises what the worker needs.
    client.images.get.return_value.labels = {"forge.proxy.capabilities": "drain"}
    proxy = ProxyContainer(client=client)
    with pytest.raises(ProxyStartError):
        proxy.start(sandbox_network=MagicMock(), **start_kwargs)
    (call,) = client.containers.run.call_args_list
    assert call.kwargs["image"] == "forge-llm-proxy:latest"  # one image for both dialects
    return call.kwargs["environment"]


def test_start_defaults_to_the_anthropic_dialect_and_its_key_variable():
    env = _start_capturing_env(real_api_key="sk-ant-fake")

    assert env["FORGE_PROXY_DIALECT"] == "anthropic"
    assert env["FORGE_PROXY_ANTHROPIC_API_KEY"] == "sk-ant-fake"
    assert "FORGE_PROXY_OPENAI_API_KEY" not in env


def test_start_with_the_openai_dialect_passes_only_the_openai_key_variable():
    env = _start_capturing_env(real_api_key="sk-proj-fake", dialect="openai", upstream_base_url="http://stub.invalid")

    assert env["FORGE_PROXY_DIALECT"] == "openai"
    assert env["FORGE_PROXY_OPENAI_API_KEY"] == "sk-proj-fake"
    assert "FORGE_PROXY_ANTHROPIC_API_KEY" not in env
    assert env["FORGE_PROXY_UPSTREAM_BASE_URL"] == "http://stub.invalid"
    assert "sk-proj-fake" not in {v for k, v in env.items() if k != "FORGE_PROXY_OPENAI_API_KEY"}


def test_start_with_an_unknown_dialect_never_creates_a_container():
    client = MagicMock()
    proxy = ProxyContainer(client=client)

    with pytest.raises(UnknownDialectError):
        proxy.start(real_api_key="k", sandbox_network=MagicMock(), dialect="gemini")

    client.containers.run.assert_not_called()


# --- the settings ---------------------------------------------------


def test_the_openai_settings_default_to_no_key_and_the_public_upstream(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_UPSTREAM_BASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.openai_api_key is None
    assert settings.openai_upstream_base_url == "https://api.openai.com"


def test_the_openai_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-from-env")
    monkeypatch.setenv("OPENAI_UPSTREAM_BASE_URL", "http://stub.invalid")

    settings = Settings(_env_file=None)

    assert settings.openai_api_key == "sk-proj-from-env"
    assert settings.openai_upstream_base_url == "http://stub.invalid"
