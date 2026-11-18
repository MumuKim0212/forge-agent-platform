# Test-only stub agent sandbox image. NOT the real pinned
# `Settings.agent_sandbox_image` — used exclusively by
# tests/test_sandbox_build_executor_integration.py to exercise the full
# claim -> sandbox -> agent-command -> harvest -> terminal-state path
# without needing a real Claude Agent SDK install or a real Anthropic API
# key.
#
# Installs a fake `claude_agent_sdk` module (fake_claude_agent_sdk.py,
# importable as `claude_agent_sdk` via a matching filename) so
# app/agent_runner.py runs completely unmodified against it.
FROM python:3.11-slim

COPY fake_claude_agent_sdk.py /usr/local/lib/python3.11/site-packages/claude_agent_sdk.py

WORKDIR /workspace
