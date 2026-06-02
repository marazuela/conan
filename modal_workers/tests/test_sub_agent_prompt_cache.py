"""Prompt-cache wiring on the sub-agent runtime.

The static skill prompt and tool definitions are re-sent on every turn of the
sub-agent tool-use loop (up to 12 turns by default). Marking the last system
block and last tool with cache_control lets Anthropic serve cached prefix at
~10% of input-token cost. These tests lock in:
  - System is built as a list with cache_control on the (final) block.
  - The last tool definition is mutated with cache_control via a copy, not
    in-place (so dispatcher-shared tool_defs lists are not corrupted).
  - The opt-out env flag turns both off cleanly.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.sub_agents.runtime import SubAgentRunner  # noqa: E402


class _Runner(SubAgentRunner):
    role = "test"
    schema_filename = "literature_review_v1.json"

    def __init__(self):
        # Bypass __init__ that creates an Anthropic client we don't need here.
        self._client = None


def test_build_cached_system_wraps_skill_in_ephemeral_block():
    runner = _Runner()
    skill = "static skill markdown"
    system = runner._build_cached_system(skill)
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0]["type"] == "text"
    # _build_cached_system appends the literal JSON-schema contract after the
    # skill (added 2026-05-23, commit 532813c) so the model sees the exact
    # output contract. Skill stays the prefix; the schema block follows.
    assert system[0]["text"].startswith(skill)
    assert "Runtime JSON Schema Contract" in system[0]["text"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_disable_env_flag_omits_cache_control(monkeypatch):
    monkeypatch.setenv("ORCH_SUB_AGENT_DISABLE_PROMPT_CACHE", "1")
    runner = _Runner()
    system = runner._build_cached_system("skill")
    assert "cache_control" not in system[0]

    tools = [{"name": "t1", "input_schema": {}}]
    out = runner._tools_with_cache_control(tools)
    assert "cache_control" not in out[-1]


def test_tools_with_cache_control_marks_last_tool_and_copies():
    runner = _Runner()
    tools = [
        {"name": "t1", "description": "first", "input_schema": {}},
        {"name": "t2", "description": "second", "input_schema": {}},
    ]
    out = runner._tools_with_cache_control(tools)
    assert out is not None
    assert len(out) == 2
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    # Caller list and last element must not have been mutated. Dispatcher
    # shares tool_defs across roles; mutation would leak cache markers.
    assert "cache_control" not in tools[-1]
    assert tools is not out


def test_tools_with_cache_control_handles_none_and_empty():
    runner = _Runner()
    assert runner._tools_with_cache_control(None) is None
    assert runner._tools_with_cache_control([]) == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
