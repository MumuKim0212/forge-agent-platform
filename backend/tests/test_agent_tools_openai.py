"""ADR 0029 §1, ADR 0035 §Decision 2: the OpenAI engine's declared tool set.

Literals throughout: the names are a contract with the runner's `IMPLEMENTATIONS`
(`test_agent_runner_openai.py` pins that side), so a rename on one side is red on the other.
"""

from __future__ import annotations

import json

import pytest

from app.services import agent_tools
from app.services.agent_tools import declared_agent_tools

OPENAI_TOOL_NAMES = ["read_file", "write_file", "edit_file", "glob", "grep", "shell"]


def test_the_openai_set_is_the_six_function_tools_in_order():
    assert declared_agent_tools("openai") == OPENAI_TOOL_NAMES


def test_the_anthropic_set_is_unchanged_and_still_the_default():
    assert declared_agent_tools() == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    assert declared_agent_tools("anthropic") == declared_agent_tools()


def test_no_declared_name_can_be_a_hosted_or_server_side_tool_on_either_engine():
    forbidden = {"web_search", "web_search_preview", "file_search", "code_interpreter", "image_generation", "mcp",
                 "computer", "WebSearch", "WebFetch"}
    for engine in ("anthropic", "openai"):
        names = declared_agent_tools(engine)
        assert forbidden.isdisjoint(names)
        assert [n for n in names if n.startswith("mcp")] == []


def test_each_call_returns_a_fresh_json_list():
    first = declared_agent_tools("openai")
    first.append("web_search")

    assert declared_agent_tools("openai") == OPENAI_TOOL_NAMES
    assert json.loads(json.dumps(declared_agent_tools("openai"))) == OPENAI_TOOL_NAMES


@pytest.mark.parametrize("engine", ["gemini", "", "OPENAI"])
def test_an_engine_without_a_declared_set_raises_instead_of_defaulting(engine):
    with pytest.raises(ValueError, match="no declared tool set"):
        declared_agent_tools(engine)


def test_the_two_sets_do_not_share_a_name():
    assert set(agent_tools.AGENT_TOOLS).isdisjoint(agent_tools.OPENAI_AGENT_TOOLS)
