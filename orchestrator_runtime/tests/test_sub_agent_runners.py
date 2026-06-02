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


def test_role_registry_has_six_roles():
    # commercial_opportunity was added as the 5th specialist (Phase 2C, #177);
    # ic_memo is the synthesis role. Keep this in sync with sub_agents/__init__.py.
    assert set(ROLE_REGISTRY.keys()) == {
        "literature", "competitive", "regulatory_history", "options_microstructure",
        "commercial_opportunity", "ic_memo",
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


def test_options_handler_degraded_when_polygon_key_missing(monkeypatch):
    """Without POLYGON_API_KEY, every tool returns status='degraded' instead
    of raising. Mirrors the polygon_mcp degraded-mode contract for the
    in-process runner path used by the Tier 1 orchestrator."""
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    # Reset module-level lazy state so the missing-key check fires fresh.
    import modal_workers.sub_agents.options_microstructure as opt_mod
    monkeypatch.setattr(opt_mod, "_provider", None)
    monkeypatch.setattr(opt_mod, "_init_error", None)

    runner = OptionsMicrostructureRunner()
    handler = runner.build_handler()

    out_chain = handler("polygon_get_chain", {"ticker": "AXSM"})
    assert out_chain["status"] == "degraded"
    assert "POLYGON_API_KEY" in out_chain["reason"]
    assert out_chain["count"] == 0
    assert out_chain["chain"] == []

    out_straddle = handler("polygon_straddle_implied_move", {
        "ticker": "AXSM", "event_date": "2026-09-15",
    })
    assert out_straddle["status"] == "degraded"
    assert out_straddle["straddle_implied_move_pct"] is None

    out_liq = handler("polygon_event_window_liquidity", {
        "ticker": "AXSM", "event_date": "2026-09-15",
    })
    assert out_liq["status"] == "degraded"
    assert out_liq["score"] == 0


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


# ---------- literature degraded fallback (budget/turn exhaustion) ----------


def test_literature_degraded_payload_is_schema_valid():
    """The literature degraded shape must validate against literature_review_v1.json
    (papers=[] is allowed by minItems 0; partial_output flags the truncation)."""
    from modal_workers.sub_agents.runtime import _load_schema, _validate

    runner = LiteratureRunner()
    payload = runner.build_degraded_payload(
        asset_context={"asset_id": "00000000-0000-0000-0000-000000000009"},
        question="pivotal trial data for drug X?",
        tool_log=[{"name": "pubmed_search", "input": {"query": "drug X phase 3"}, "turn": 0}],
        errors=["[]: 'papers' is a required property"],
    )
    assert payload["partial_output"] is True
    assert payload["papers"] == []
    assert payload["query_used"]  # non-empty (schema minLength 1)
    assert payload["asset_id"] == "00000000-0000-0000-0000-000000000009"
    assert _validate(payload, _load_schema("literature_review_v1.json")) == []


def test_literature_returns_degraded_instead_of_raising():
    """Empty/non-JSON model output, retry also empty → run() emits the
    schema-valid degraded payload rather than raising SubAgentSchemaError."""
    runner = LiteratureRunner()
    runner.skill_path = None
    runner.internal_rag_default_corpus = None  # skip rag tool chaining in-test

    empty_msg = _FakeMessage([_TextBlock("")], stop_reason="end_turn")
    fake = MagicMock(
        text="", input_tokens=100, output_tokens=20,
        thinking_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
        cost_usd=0.001, latency_ms=100, model="sonnet", raw_message=empty_msg,
    )
    # call 1 = initial turn (empty), call 2 = forced-synthesis retry (still empty)
    with patch.object(runner._client, "call", side_effect=[fake, fake]):
        result = runner.run(
            question="pivotal data?",
            asset_context={"asset_id": "00000000-0000-0000-0000-000000000009"},
        )

    assert result.schema_pass is True
    assert result.output["partial_output"] is True
    assert result.output["papers"] == []
    assert result.schema_retries == 1


def test_commercial_opportunity_degraded_payload_is_schema_valid():
    """commercial_opportunity shares literature's empty-{} failure class; its
    degraded shape must validate against commercial_opportunity_v1.json
    (regulatory_incentives non-empty, unmet_need 1-5, tam_estimate present)."""
    from modal_workers.sub_agents.runtime import _load_schema, _validate
    from modal_workers.sub_agents.commercial_opportunity import CommercialOpportunityRunner

    runner = CommercialOpportunityRunner()
    payload = runner.build_degraded_payload(
        asset_context={
            "asset_id": "00000000-0000-0000-0000-000000000009",
            "indication": "biliary tract cancer",
        },
        question="commercial opportunity for drug X?",
        tool_log=[],
        errors=["[]: 'tam_estimate' is a required property"],
    )
    assert payload["partial_output"] is True
    assert payload["regulatory_incentives"] == ["none"]
    assert payload["standard_of_care"] == []
    assert 1 <= payload["unmet_need_severity_1_5"] <= 5
    assert payload["mcap_to_peak_revenue_ratio"] is None  # nullable, present for robustness
    assert _validate(payload, _load_schema("commercial_opportunity_v1.json")) == []
