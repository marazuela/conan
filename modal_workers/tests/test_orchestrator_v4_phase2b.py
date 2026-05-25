"""Phase 2b tests: commercial_opportunity sub-agent registration + schema.

Validates the four-piece sub-agent contract (runner class, ROLE_REGISTRY entry,
JSON schema in sibling repo, skill markdown) for the new commercial_opportunity
sub-agent. Does NOT exercise the Anthropic tool-use loop end-to-end (those
tests live in test_sub_agent_runtime.py and run against mocked Anthropic).
Phase 2b tests lock down the surface so prompt + dispatch wiring can't silently
drift.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 2b).

Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase2b.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_commercial_opportunity_runner_registered_in_role_registry():
    from modal_workers.sub_agents import (
        ROLE_REGISTRY,
        CommercialOpportunityRunner,
    )

    assert "commercial_opportunity" in ROLE_REGISTRY, (
        "Phase 2b: commercial_opportunity must be in ROLE_REGISTRY so the "
        "dispatcher can route to it"
    )
    assert ROLE_REGISTRY["commercial_opportunity"] is CommercialOpportunityRunner


def test_commercial_opportunity_runner_metadata():
    from modal_workers.sub_agents import CommercialOpportunityRunner

    assert CommercialOpportunityRunner.role == "commercial_opportunity"
    assert CommercialOpportunityRunner.schema_filename == "commercial_opportunity_v1.json"
    # Skill path must exist on disk (the runtime reads it at first invocation).
    assert CommercialOpportunityRunner.skill_path.exists(), (
        f"skill markdown missing at {CommercialOpportunityRunner.skill_path}"
    )


def test_dispatcher_enum_includes_commercial_opportunity():
    """The Stage 1 model picks the role from DISPATCH_TOOL_DEF.input_schema.role.enum.
    If commercial_opportunity isn't in the enum, the model can't call it even
    though the runner is registered."""
    from orchestrator_runtime.sub_agent_dispatcher import DISPATCH_TOOL_DEF

    enum = DISPATCH_TOOL_DEF["input_schema"]["properties"]["role"]["enum"]
    assert "commercial_opportunity" in enum, (
        f"dispatcher enum missing commercial_opportunity: {enum}"
    )

    # And the description must mention it so the model knows when to call it.
    assert "commercial_opportunity" in DISPATCH_TOOL_DEF["description"]


# ---------------------------------------------------------------------------
# Schema file
# ---------------------------------------------------------------------------

def _schema_dir() -> Path:
    """Resolve the conan-cowork-skills/schemas/ directory the runtime uses.

    Mirrors `modal_workers.sub_agents.runtime.SCHEMA_DIR` resolution so the
    test sees the same file the production code does.
    """
    from modal_workers.sub_agents.runtime import SCHEMA_DIR
    return SCHEMA_DIR


def test_commercial_opportunity_schema_file_exists():
    schema_path = _schema_dir() / "commercial_opportunity_v1.json"
    assert schema_path.exists(), (
        f"Phase 2b schema missing at {schema_path} — runner will FileNotFoundError"
    )


def test_commercial_opportunity_schema_is_valid_jsonschema():
    """Schema file is well-formed Draft-7 JSON Schema."""
    import jsonschema

    schema_path = _schema_dir() / "commercial_opportunity_v1.json"
    with schema_path.open() as f:
        schema = json.load(f)

    # No exception = pass.
    jsonschema.Draft7Validator.check_schema(schema)

    # Sanity: the title + required fields match what Phase 2 expects.
    assert schema["title"] == "Commercial Opportunity Sub-Agent Output (v1)"
    for required_field in (
        "schema_version",
        "asset_id",
        "indication",
        "tam_estimate",
        "mcap_to_peak_revenue_ratio",
        "standard_of_care",
        "soc_limitations",
        "soc_side_effects",
        "unmet_need_severity_1_5",
        "regulatory_incentives",
        "competitive_landscape_summary",
        "sourcing_completeness_pct",
        "retrieved_at",
    ):
        assert required_field in schema["required"], (
            f"schema missing required field: {required_field}"
        )


def test_unmet_need_severity_constrained_to_1_through_5():
    """Spot-check the load-bearing severity enum: 1-5 integer, no other values."""
    import jsonschema

    schema_path = _schema_dir() / "commercial_opportunity_v1.json"
    with schema_path.open() as f:
        schema = json.load(f)

    sev = schema["properties"]["unmet_need_severity_1_5"]
    assert sev["type"] == "integer"
    assert sev["minimum"] == 1
    assert sev["maximum"] == 5

    # Validate that out-of-range fails.
    validator = jsonschema.Draft7Validator(schema)
    bad = {
        "schema_version": 1,
        "asset_id": "00000000-0000-0000-0000-000000000000",
        "indication": "test",
        "tam_estimate": {"low_usd": 0, "high_usd": 0, "is_inferred": True, "rationale": "x"},
        "mcap_to_peak_revenue_ratio": None,
        "standard_of_care": [],
        "soc_limitations": [],
        "soc_side_effects": [],
        "unmet_need_severity_1_5": 0,  # out of range
        "regulatory_incentives": ["none"],
        "competitive_landscape_summary": {
            "headline": "x",
            "n_known_competitors": 0,
            "differentiation_assessment": "unknown",
        },
        "sourcing_completeness_pct": 0,
        "retrieved_at": "2026-05-25T00:00:00Z",
    }
    errors = list(validator.iter_errors(bad))
    assert any("unmet_need_severity_1_5" in str(e.absolute_path) for e in errors), (
        "schema must reject unmet_need_severity_1_5=0"
    )


def test_regulatory_incentives_never_empty():
    """Empty array would lose the 'we checked and there are none' signal —
    schema must force at least ['none']."""
    import jsonschema

    schema_path = _schema_dir() / "commercial_opportunity_v1.json"
    with schema_path.open() as f:
        schema = json.load(f)

    incentives = schema["properties"]["regulatory_incentives"]
    assert incentives["minItems"] == 1
    assert "none" in incentives["items"]["enum"]


# ---------------------------------------------------------------------------
# Runner tool surface
# ---------------------------------------------------------------------------

def test_runner_exposes_minimum_viable_tool_set():
    """Phase 2b MVP tools: openFDA (label lookups by indication + by drug) +
    PubMed (search + fetch). If any drops, regression risk."""
    from modal_workers.sub_agents import CommercialOpportunityRunner

    tool_names = {t["name"] for t in CommercialOpportunityRunner.tool_defs}
    expected = {
        "openfda_labels_for_indication",
        "openfda_label_by_drug",
        "pubmed_search",
        "pubmed_fetch_abstracts",
    }
    assert expected.issubset(tool_names), (
        f"missing tools: {expected - tool_names}"
    )


def test_runner_handler_rejects_unknown_tool():
    """build_handler() must surface unknown tool names as errors so the
    Anthropic loop sees is_error=True rather than a silent no-op."""
    import pytest

    from modal_workers.sub_agents import CommercialOpportunityRunner

    handler = CommercialOpportunityRunner().build_handler()
    with pytest.raises(ValueError, match="unknown tool"):
        handler("does_not_exist", {})
