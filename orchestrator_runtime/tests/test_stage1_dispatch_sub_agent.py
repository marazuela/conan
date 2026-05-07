"""Stream 3.6 — Stage 1 with sub-agent dispatch tool-use loop.

Run: python -m pytest orchestrator_runtime/tests/test_stage1_dispatch_sub_agent.py -v
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.runtime import (
    DISPATCH_TOOL_DEF,
    stage_1_synthesize,
)


# ---------- helpers ----------


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeMsg:
    def __init__(self, content_blocks, stop_reason="end_turn"):
        self.content = content_blocks
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, id, name, inp):
        self.id = id
        self.name = name
        self.input = inp


def _ctx() -> Dict[str, Any]:
    return {
        "asset": {
            "id": "00000000-0000-0000-0000-000000000aaa",
            "ticker": "FOO",
            "drug_name": "Drug X",
            "indication": "indication-X",
            "indication_normalized": "indication-x",
            "reference_class_signature": "phase3_oncology",
            "reviewer_panel_id": None,
        },
        "facts": [],
        "documents": [],
        "memory_text": None,
        "memory_blobs": MagicMock(is_empty=lambda: True, as_text=lambda: ""),
        "asset_doc_links": [],
        "reference_class_anchor": None,
        "pre_premortem_conviction": None,
    }


def _fake_call_result(text, raw_message, tokens=(100, 50)):
    return MagicMock(
        text=text, input_tokens=tokens[0], output_tokens=tokens[1],
        thinking_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
        cost_usd=0.001, latency_ms=300, model="sonnet", raw_message=raw_message,
    )


# ---------- tests ----------


def test_dispatch_tool_def_present_in_runtime():
    """The dispatch tool definition is importable from runtime."""
    assert DISPATCH_TOOL_DEF["name"] == "dispatch_sub_agent"
    enum_roles = DISPATCH_TOOL_DEF["input_schema"]["properties"]["role"]["enum"]
    assert "literature" in enum_roles


def test_stage_1_default_path_does_not_pass_tools():
    """When enable_sub_agents is False (default), no tools param is sent."""
    a_client = MagicMock()
    final_msg = _FakeMsg([_Text("cited prose [F:abc123] result")])
    a_client.call.return_value = _fake_call_result("cited prose result", final_msg)

    cited_prose, metric = stage_1_synthesize(a_client, _ctx(), "claude-sonnet-4-5")

    a_client.call.assert_called_once()
    # The default path does NOT pass tools
    kwargs = a_client.call.call_args.kwargs
    assert "tools" not in kwargs or kwargs.get("tools") is None
    assert cited_prose == "cited prose result"


def test_stage_1_enable_sub_agents_passes_dispatch_tool():
    """With enable_sub_agents=True, the call passes tools=[DISPATCH_TOOL_DEF]."""
    a_client = MagicMock()
    final_msg = _FakeMsg([_Text("done")], stop_reason="end_turn")
    a_client.call.return_value = _fake_call_result("done", final_msg)

    cited_prose, metric = stage_1_synthesize(
        a_client, _ctx(), "claude-sonnet-4-5",
        enable_sub_agents=True,
    )
    kwargs = a_client.call.call_args.kwargs
    assert kwargs["tools"] == [DISPATCH_TOOL_DEF]


def test_stage_1_loop_dispatches_sub_agent_and_completes():
    """Tool-use turn → user feedback → end_turn turn → final cited prose."""
    a_client = MagicMock()

    # Turn 1: assistant emits dispatch_sub_agent tool_use
    msg1 = _FakeMsg(
        [_ToolUse("tu1", "dispatch_sub_agent",
                  {"role": "literature", "question": "find papers"})],
        stop_reason="tool_use",
    )
    # Turn 2: assistant emits final cited prose
    msg2 = _FakeMsg([_Text("Final prose with [F:abc123] cite.")], stop_reason="end_turn")

    a_client.call.side_effect = [
        _fake_call_result("", msg1),
        _fake_call_result("Final prose with [F:abc123] cite.", msg2),
    ]

    fake_dispatch = MagicMock(return_value={
        "role": "literature", "schema_pass": True, "errors": [],
        "output": {"papers": []}, "metadata": {"sub_agent_call_id": "c1"},
    })
    with patch("orchestrator_runtime.runtime.dispatch_sub_agent_tool", fake_dispatch):
        cited_prose, metric = stage_1_synthesize(
            a_client, _ctx(), "claude-sonnet-4-5",
            enable_sub_agents=True,
            assessment_id="asmt-1",
        )

    assert cited_prose == "Final prose with [F:abc123] cite."
    assert a_client.call.call_count == 2
    fake_dispatch.assert_called_once()
    # Verify dispatch was called with the right role
    assert fake_dispatch.call_args.args[0]["role"] == "literature"
    # Metric notes capture the dispatches
    assert metric.notes["sub_agent_dispatches"][0]["role"] == "literature"
    assert metric.notes["loop_turns"] == 2


def test_stage_1_loop_handles_unknown_tool_gracefully():
    """If Claude calls an unknown tool, return error tool_result; don't crash."""
    a_client = MagicMock()
    msg1 = _FakeMsg(
        [_ToolUse("tu1", "wrong_tool_name", {})],
        stop_reason="tool_use",
    )
    msg2 = _FakeMsg([_Text("text after error")], stop_reason="end_turn")
    a_client.call.side_effect = [
        _fake_call_result("", msg1),
        _fake_call_result("text after error", msg2),
    ]

    cited_prose, metric = stage_1_synthesize(
        a_client, _ctx(), "claude-sonnet-4-5",
        enable_sub_agents=True,
    )
    assert cited_prose == "text after error"
    # The unknown tool should NOT appear in dispatch_log (only dispatch_sub_agent does)
    assert metric.notes["sub_agent_dispatches"] == []
