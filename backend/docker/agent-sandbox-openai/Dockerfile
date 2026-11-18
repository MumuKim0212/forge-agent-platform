# OpenAI-engine agent sandbox image (ADR 0035 §Decision 1-2).
#
# The L3 sandbox image for builds that resolved to the `openai` engine: the OpenAI Agents
# SDK preinstalled, so the in-sandbox runner (agent_runner_openai.py, staged alongside the
# workspace per run) starts without a package-registry round trip. Unlike the Claude image
# there is no Node runtime and no CLI: the SDK is a plain Python HTTP client, and the
# runner implements the declared tools itself.
#
# Pinned explicitly by `Settings.openai_agent_sandbox_image`; operators rebuild and re-tag
# deliberately, never floating `:latest` implicitly at runtime. Build from backend/:
#   docker build -t forge-agent-sandbox-openai:latest -f docker/agent-sandbox-openai/Dockerfile .
#
# Both versions are pinned EXACTLY (tests/test_agent_sandbox_openai_dockerfile.py fails on a
# range, a missing version, or `latest`), as for the Claude image (ADR 0029 §4): the request
# shapes the runner sends (store, service_tier, tool schema) are what the proxy's `openai`
# dialect admits, and they are version-dependent. openai-agents 0.23.1 requires
# `openai>=3.0.0,<4`; `openai` is pinned to the release it resolved to when chosen
# (2026-10-12). Upgrading either is an explicit change that re-runs the engine-up test
# (tests/test_openai_engine_up_integration.py), which records the shapes actually sent.
FROM python:3.11-slim

RUN pip install --no-cache-dir openai-agents==0.23.1 openai==3.28.0

WORKDIR /workspace
