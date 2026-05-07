"""Tests for `modal_workers.sub_agents.ic_memo.ICMemoRunner`. No network.

The IC memo runner is a synthesis-only sub-agent: no tools, takes the four
specialist outputs + Stage 9 thesis as input, emits a memo conforming to
ic_memo_v1.json.

Run: python -m pytest orchestrator_runtime/tests/test_ic_memo_runner.py -v
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def test_ic_memo_registered_in_role_registry():
    from modal_workers.sub_agents import ROLE_REGISTRY
    assert "ic_memo" in ROLE_REGISTRY


def test_ic_memo_runner_has_no_tools():
    from modal_workers.sub_agents.ic_memo import ICMemoRunner

    runner = ICMemoRunner()
    assert runner.tool_defs == []
    # effective_tool_defs() also returns [] (no shared rag/compute opt-in)
    assert runner.effective_tool_defs() == []


def test_ic_memo_handler_rejects_any_tool_call():
    from modal_workers.sub_agents.ic_memo import ICMemoRunner

    handler = ICMemoRunner().build_handler()
    with pytest.raises(ValueError, match="ic_memo runner has no tools"):
        handler("anything", {})


def test_ic_memo_build_user_content_includes_all_specialists():
    from modal_workers.sub_agents.ic_memo import ICMemoRunner

    runner = ICMemoRunner()
    asset_ctx = {
        "asset": {"ticker": "AXSM", "drug_name": "AXS-05"},
        "specialists": {
            "literature": {"summary": "primary endpoint hit", "papers": []},
            "competitive": {"summary": "no near competition"},
            "regulatory_history": {"summary": "clean AdComm history"},
            "options_microstructure": {"summary": "low IV"},
        },
        "thesis": {
            "direction": "long",
            "conviction_pct": 72,
            "text": "PDUFA approval likely",
        },
        "reference_class_anchor": {"reference_class": "psych_NDA",
                                    "base_rate_pct": 64.0},
    }
    out = runner.build_user_content(
        question="Synthesize the case for AXS-05.",
        asset_context=asset_ctx,
    )
    assert "[literature]" in out
    assert "[competitive]" in out
    assert "[regulatory_history]" in out
    assert "[options_microstructure]" in out
    assert "Stage 9 thesis" in out
    assert "Reference-class anchor" in out
    assert "AXS-05" in out
    assert "ic_memo_v1.json" in out


def test_ic_memo_build_user_content_handles_missing_specialists():
    """When a specialist output is missing, render a placeholder note."""
    from modal_workers.sub_agents.ic_memo import ICMemoRunner

    runner = ICMemoRunner()
    out = runner.build_user_content(
        question="",
        asset_context={
            "asset": {"ticker": "X"},
            "specialists": {"literature": {"summary": "ok"}},  # rest missing
            "thesis": {},
        },
    )
    assert "[literature]" in out
    assert "(no review available" in out  # for the 3 missing ones


def test_ic_memo_validates_against_schema_when_payload_complete():
    """Smoke check that a well-formed memo passes the v1 schema."""
    import jsonschema

    schema_path = (
        "/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/"
        "schemas/ic_memo_v1.json"
    )
    with open(schema_path) as f:
        schema = json.load(f)

    valid_payload: Dict[str, Any] = {
        "schema_version": 1,
        "asset_id": "asset-1",
        "thesis": {
            "direction": "long",
            "headline": "AXS-05 PDUFA approval likely on safety + efficacy strength.",
            "core_claim": "Stage-3 endpoint hit; no FDA briefing red flags.",
        },
        "asymmetry": {
            "upside": "+30%",
            "downside": "-25%",
            "skew": "moderately_skewed",
            "implied_move_vs_options": "Options imply 20% — thesis upside is 1.5x.",
        },
        "kill_conditions": [
            {
                "trigger": "FDA briefing doc surfaces unresolved CMC issue",
                "rationale": "Would force CRL outcome",
                "source": "regulatory_history",
            }
        ],
        "position_sizing_logic": {
            "recommended_band": "medium",
            "rationale": "Conviction 72%, asymmetric 1.2x, average liquidity",
            "liquidity_constraint": None,
        },
        "summary": "Long thesis on AXS-05 ahead of PDUFA Sep 2026.",
        "key_findings": [
            "Stage-3 GEMINI endpoint hit [literature]",
            "No competing CGRP for MDD [competitive]",
        ],
        "uncertainties": ["Unknown if Adcomm convened"],
        "citations": [
            {"ref": "[1]", "source": "literature",
             "document_id": None, "snippet": "GEMINI hit primary"},
        ],
        "confidence": 0.78,
    }
    jsonschema.validate(valid_payload, schema)


def test_ic_memo_schema_rejects_invalid_direction():
    import jsonschema

    schema_path = (
        "/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/"
        "schemas/ic_memo_v1.json"
    )
    with open(schema_path) as f:
        schema = json.load(f)

    bad: Dict[str, Any] = {
        "schema_version": 1,
        "asset_id": "x",
        "thesis": {
            "direction": "rocket_emoji",  # not in enum
            "headline": "h",
        },
        "asymmetry": {"upside": "+x", "downside": "-x", "skew": "symmetric"},
        "kill_conditions": [
            {"trigger": "t", "rationale": "r"}
        ],
        "position_sizing_logic": {"recommended_band": "small", "rationale": "r"},
        "summary": "s",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ic_memo_schema_requires_at_least_one_kill_condition():
    import jsonschema

    schema_path = (
        "/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/"
        "schemas/ic_memo_v1.json"
    )
    with open(schema_path) as f:
        schema = json.load(f)

    bad = {
        "schema_version": 1,
        "asset_id": "x",
        "thesis": {"direction": "long", "headline": "h"},
        "asymmetry": {"upside": "+", "downside": "-", "skew": "symmetric"},
        "kill_conditions": [],  # empty array — should fail minItems: 1
        "position_sizing_logic": {"recommended_band": "small", "rationale": "r"},
        "summary": "s",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
