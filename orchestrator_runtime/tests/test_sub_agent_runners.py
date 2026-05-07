"""Tests for modal_workers.sub_agents.* — runner shape + tool routing.

Run: python -m pytest orchestrator_runtime/tests/test_sub_agent_runners.py -v
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
os.environ.setdefault("POLYGON_API_KEY", "x")

from modal_workers.sub_agents import (
    LiteratureRunner,
    CompetitiveRunner,
    RegulatoryHistoryRunner,
    OptionsMicrostructureRunner,
    ROLE_REGISTRY,
)
from modal_workers.sub_agents.runtime import SubAgentSchemaError


# ---------- registry ----------


def test_role_registry_has_five_roles():
    assert set(ROLE_REGISTRY.keys()) == {
        "literature", "competitive", "regulatory_history", "options_microstructure",
        "ic_memo",
    }


# ---------- handler routing ----------


def test_literature_handler_routes_pubmed_search():
    runner = LiteratureRunner()
    handler = runner.build_handler()
    with patch("modal_workers.providers.pubmed.eutils.search", return_value=["111", "222"]):
        out = handler("pubmed_search", {"query": "test", "limit": 10})
    assert out["count"] == 2
    assert out["pmids"] == ["111", "222"]


def test_literature_handler_routes_biorxiv_stub():
    runner = LiteratureRunner()
    handler = runner.build_handler()
    out = handler("biorxiv_search", {"query": "test"})
    assert out["count"] == 0


def test_literature_handler_unknown_tool_raises():
    runner = LiteratureRunner()
    handler = runner.build_handler()
    with pytest.raises(ValueError, match="unknown tool"):
        handler("nonexistent_tool", {})


def test_competitive_handler_routes_clinicaltrials_search():
    runner = CompetitiveRunner()
    handler = runner.build_handler()
    fake_body = {"studies": [{"id": "NCT12345"}]}
    # Patch where the symbol is bound (the runner module), not the source.
    with patch("modal_workers.sub_agents.competitive._ct_get", return_value=fake_body):
        out = handler("clinicaltrials_search", {"query_term": "BRAF inhibitor"})
    assert out["count"] == 1
    assert out["studies"][0]["id"] == "NCT12345"


def test_regulatory_handler_routes_openfda_drugsfda():
    runner = RegulatoryHistoryRunner()
    handler = runner.build_handler()
    fake_body = {"results": [{"application_number": "NDA215877"}]}
    with patch("modal_workers.sub_agents.regulatory_history._openfda_get", return_value=fake_body):
        out = handler("openfda_drugsfda_approvals", {"sponsor_search": "AXSOME"})
    assert out["count"] == 1
    assert out["applications"][0]["application_number"] == "NDA215877"


def test_options_handler_routes_polygon_straddle():
    runner = OptionsMicrostructureRunner()
    handler = runner.build_handler()
    fake_provider = MagicMock()
    fake_provider.get_straddle_implied_move.return_value = {
        "ticker": "FOO", "straddle_implied_move_pct": 18.5,
    }
    with patch("modal_workers.sub_agents.options_microstructure._provider", fake_provider):
        out = handler("polygon_straddle_implied_move", {
            "ticker": "FOO", "event_date": "2026-09-15",
        })
    assert out["straddle_implied_move_pct"] == 18.5


# ---------- end-to-end run() with mocked Anthropic client ----------


class _FakeUsage:
    input_tokens = 100
    output_tokens = 200
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeMessage:
    """Bare anthropic Message lookalike."""

    def __init__(self, content_blocks, stop_reason="end_turn"):
        self.content = content_blocks
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


def test_runner_completes_loop_and_validates_payload(tmp_path):
    """Runner: tool_use turn → end_turn turn → schema-valid JSON → result."""
    runner = OptionsMicrostructureRunner()
    runner.skill_path = None  # use fallback minimal system prompt

    valid_payload = {
        "schema_version": 1,
        "asset_id": "00000000-0000-0000-0000-000000000001",
        "ticker": "FOO",
        "computed_at": "2026-05-07T12:00:00Z",
        "event_window_liquidity_score": 3,
        "position_inferred": "long_vol",
    }

    # Two-turn conversation: first response uses a tool, second returns final JSON.
    msg_1 = _FakeMessage([
        _ToolUseBlock("tu1", "polygon_straddle_implied_move",
                      {"ticker": "FOO", "event_date": "2026-09-15"}),
    ], stop_reason="tool_use")
    msg_2 = _FakeMessage([_TextBlock(json.dumps(valid_payload))], stop_reason="end_turn")

    fake_call_results = [
        MagicMock(text="", input_tokens=100, output_tokens=50,
                  thinking_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
                  cost_usd=0.001, latency_ms=300, model="sonnet", raw_message=msg_1),
        MagicMock(text=json.dumps(valid_payload), input_tokens=200, output_tokens=100,
                  thinking_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
                  cost_usd=0.002, latency_ms=400, model="sonnet", raw_message=msg_2),
    ]

    fake_provider = MagicMock()
    fake_provider.get_straddle_implied_move.return_value = {
        "ticker": "FOO", "straddle_implied_move_pct": 18.0,
    }

    with patch.object(runner._client, "call", side_effect=fake_call_results), \
         patch("modal_workers.sub_agents.options_microstructure._provider", fake_provider):
        result = runner.run(question="implied move?", asset_context={"ticker": "FOO"})

    assert result.schema_pass is True
    assert result.output["schema_version"] == 1
    assert result.tokens_input == 300
    assert result.tokens_output == 150
    assert len(result.tool_call_log) == 1
    assert result.tool_call_log[0]["name"] == "polygon_straddle_implied_move"


def test_runner_raises_on_schema_failure():
    runner = OptionsMicrostructureRunner()
    runner.skill_path = None

    invalid_payload = {"schema_version": 1}  # missing required fields

    msg = _FakeMessage([_TextBlock(json.dumps(invalid_payload))], stop_reason="end_turn")
    fake_call_result = MagicMock(
        text=json.dumps(invalid_payload), input_tokens=50, output_tokens=20,
        thinking_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
        cost_usd=0.001, latency_ms=200, model="sonnet", raw_message=msg,
    )
    with patch.object(runner._client, "call", return_value=fake_call_result):
        with pytest.raises(SubAgentSchemaError) as exc_info:
            runner.run(question="x", asset_context={})
    assert exc_info.value.role == "options_microstructure"
    assert exc_info.value.errors  # non-empty
