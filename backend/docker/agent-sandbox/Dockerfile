# Agent-run sandbox image (ADR 0014).
#
# The L3 sandbox image used for builds that run a real agent. Distinct from
# the generic `sandbox_image` (plain python:3.11-slim, no agent runtime)
# — this image has the Claude Agent SDK preinstalled so the adapter's
# in-sandbox runner script (agent_runner.py, staged in alongside the
# workspace) can start immediately without a package-registry round trip.
#
# Pinned explicitly by `Settings.agent_sandbox_image`; operators rebuild and
# re-tag deliberately, never floating `:latest` implicitly at runtime.
#
# The Claude Agent SDK's Python package shells out to the Claude Code CLI
# (Node.js), so both the CLI and a Node runtime are preinstalled alongside
# the Python SDK package.
#
# Both versions are pinned EXACTLY (ADR 0029 §4; tests/test_agent_sandbox_dockerfile.py
# fails on a range, a missing version, or `latest`). Tool-option semantics are
# version-dependent, so the tool-set and config-source behaviour that was verified
# holds only for these versions. claude-agent-sdk 0.2.162 declares
# `__cli_version__ = "2.1.285"` (src/claude_agent_sdk/_cli_version.py), so the
# CLI pin below is the CLI that SDK drives. Upgrading either one is an explicit
# change that re-runs that verification (the engine-up capture test).
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.285 \
    && pip install --no-cache-dir claude-agent-sdk==0.2.162

WORKDIR /workspace
