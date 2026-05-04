"""
Tests for the JSON-Schema validator that gates fda_agent_reviews writes.

Tests don't require the cowork-skills repo to be cloned; we point
search_paths at our own fixture directory built from the live schemas.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from modal_workers.shared.fda_agent_validator import (
    SchemaNotFoundError,
    UnknownAgentKindError,
    ValidationResult,
    clear_schema_cache,
    load_schema,
    validate,
)

# Real schemas live next to the cowork-skills repo (sibling). Tests resolve
# them by walking up from CONAN_ROOT (or from this file's location) — the
# default search path covers both.
SCHEMA_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "conan-cowork-skills"
    / "schemas"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_schema_cache()
    yield
    clear_schema_cache()


@pytest.fixture
def schema_paths():
    if not SCHEMA_DIR.is_dir():
        pytest.skip(f"cowork-skills schemas not present at {SCHEMA_DIR}")
    return [SCHEMA_DIR]


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------


def test_unknown_agent_kind_raises():
    with pytest.raises(UnknownAgentKindError):
        validate("astrology", {})


def test_load_schema_caches_per_kind(schema_paths):
    a = load_schema("medical", search_paths=schema_paths)
    b = load_schema("medical")  # second call uses cache
    assert a is not None
    assert b is not None
    assert a == b


def test_load_schema_missing_directory_raises(tmp_path):
    with pytest.raises(SchemaNotFoundError):
        load_schema("medical", search_paths=[tmp_path / "nonexistent"])


# ---------------------------------------------------------------------------
# Medical agent
# ---------------------------------------------------------------------------


def _valid_medical():
    return {
        "endpoint_quality": 4,
        "safety_concerns": ["mild liver enzyme elevations"],
        "effect_size_pp": 12.0,
        "precedent_class_outcome": "approved",
        "fair_probability_modifier": 0.05,
        "citations": [
            {"url": "https://www.fda.gov/x", "quote": "Phase 3 met primary endpoint."},
            {"url": "https://clinicaltrials.gov/y", "quote": "ACTIVE_NOT_RECRUITING"},
            {"url": "https://www.nejm.org/z", "quote": "Effect size of 12pp vs placebo."},
        ],
        "confidence": 0.78,
    }


def test_medical_valid_payload(schema_paths):
    out = validate("medical", _valid_medical(), search_paths=schema_paths)
    assert out.valid
    assert out.errors == []
    assert out.normalized_payload is not None
    assert out.agent_kind == "medical"


def test_medical_rejects_out_of_bound_modifier(schema_paths):
    payload = _valid_medical()
    payload["fair_probability_modifier"] = 0.5  # bound is 0.10
    out = validate("medical", payload, search_paths=schema_paths)
    assert not out.valid
    assert any("fair_probability_modifier" in e for e in out.errors)


def test_medical_rejects_under_three_citations(schema_paths):
    payload = _valid_medical()
    payload["citations"] = payload["citations"][:2]
    out = validate("medical", payload, search_paths=schema_paths)
    assert not out.valid
    assert any("citations" in e for e in out.errors)


def test_medical_rejects_bad_precedent_enum(schema_paths):
    payload = _valid_medical()
    payload["precedent_class_outcome"] = "maybe"
    out = validate("medical", payload, search_paths=schema_paths)
    assert not out.valid


def test_medical_rejects_unknown_field(schema_paths):
    payload = _valid_medical()
    payload["bonus_field"] = "anything"
    out = validate("medical", payload, search_paths=schema_paths)
    assert not out.valid
    assert any("additionalProperties" in e or "bonus_field" in e for e in out.errors)


def test_medical_rejects_non_object_payload(schema_paths):
    out = validate("medical", ["not", "a", "dict"], search_paths=schema_paths)
    assert not out.valid
    assert any("must be a JSON object" in e for e in out.errors)


def test_medical_endpoint_quality_must_be_integer(schema_paths):
    payload = _valid_medical()
    payload["endpoint_quality"] = 4.5
    out = validate("medical", payload, search_paths=schema_paths)
    assert not out.valid


# ---------------------------------------------------------------------------
# Regulatory agent
# ---------------------------------------------------------------------------


def _valid_regulatory():
    return {
        "adcom_risk_score": 3,
        "crl_precedent": False,
        "resubmission_pathway": "smooth",
        "staff_review_redflags": [],
        "evidence_confidence_boost": 0.10,
        "regulatory_confidence": 0.7,
        "citations": [
            {"url": "https://www.fda.gov/briefing", "quote": "FDA briefing book section 4.2"},
            {"url": "https://www.federalregister.gov/2026", "quote": "AdCom scheduled for May 12"},
            {"url": "https://www.fda.gov/adcom-precedents", "quote": "Class precedent: 6 of 8 approved"},
        ],
        "confidence": 0.65,
    }


def test_regulatory_valid_payload(schema_paths):
    out = validate("regulatory", _valid_regulatory(), search_paths=schema_paths)
    assert out.valid


def test_regulatory_rejects_out_of_bound_boost(schema_paths):
    payload = _valid_regulatory()
    payload["evidence_confidence_boost"] = 0.99
    out = validate("regulatory", payload, search_paths=schema_paths)
    assert not out.valid


def test_regulatory_rejects_bad_pathway_enum(schema_paths):
    payload = _valid_regulatory()
    payload["resubmission_pathway"] = "very_difficult"
    out = validate("regulatory", payload, search_paths=schema_paths)
    assert not out.valid


# ---------------------------------------------------------------------------
# Microstructure agent
# ---------------------------------------------------------------------------


def _valid_microstructure():
    return {
        "options_liquidity_score": 3.5,
        "implied_move_pct": 18.0,
        "borrow_cost_bps": 250.0,
        "crowding_score": 2.5,
        "event_window_open_interest": 8200,
        "citations": [
            {"url": "https://polygon.io/x", "quote": "ATM straddle pricing"},
            {"url": "https://finra.org/short", "quote": "Short interest 14%"},
            {"url": "https://ibkr.com/borrow", "quote": "Borrow cost 250bps"},
        ],
        "confidence": 0.6,
    }


def test_microstructure_valid_payload(schema_paths):
    out = validate("microstructure", _valid_microstructure(), search_paths=schema_paths)
    assert out.valid


def test_microstructure_rejects_negative_implied_move(schema_paths):
    payload = _valid_microstructure()
    payload["implied_move_pct"] = -5.0
    out = validate("microstructure", payload, search_paths=schema_paths)
    assert not out.valid


def test_microstructure_borrow_cost_can_be_null(schema_paths):
    payload = _valid_microstructure()
    payload["borrow_cost_bps"] = None
    out = validate("microstructure", payload, search_paths=schema_paths)
    assert out.valid


def test_microstructure_rejects_oi_below_zero(schema_paths):
    payload = _valid_microstructure()
    payload["event_window_open_interest"] = -1
    out = validate("microstructure", payload, search_paths=schema_paths)
    assert not out.valid


# ---------------------------------------------------------------------------
# Search-path overrides
# ---------------------------------------------------------------------------


def test_env_var_search_path_takes_precedence(monkeypatch, tmp_path, schema_paths):
    # Copy the real medical schema into tmp_path/schemas/, mutate it, and
    # confirm CONAN_COWORK_SKILLS env var wins.
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    real = json.loads((schema_paths[0] / "fda_agent_medical.json").read_text())
    real["title"] = "Mutated for test"
    real["properties"]["endpoint_quality"]["maximum"] = 99
    (schemas_dir / "fda_agent_medical.json").write_text(json.dumps(real))

    monkeypatch.setenv("CONAN_COWORK_SKILLS", str(tmp_path))
    clear_schema_cache()

    schema = load_schema("medical")
    assert schema["title"] == "Mutated for test"
    # And the loosened bound applies during validation
    payload = _valid_medical()
    payload["endpoint_quality"] = 50
    out = validate("medical", payload)
    assert out.valid
