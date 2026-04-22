"""
Tests for rubric_engine.validate_scoring_meta.

The validator is the write-side guard for the invariant reactor/UI depend on.
Without it, a future refactor of build_scoring_meta could silently drop a
required key, reactor `isProvisionalHeuristic` would misclassify rows, and
nothing would catch it until a production bug report.
"""
from __future__ import annotations

import pytest

from modal_workers.shared.rubric_engine import build_scoring_meta, validate_scoring_meta


# ----------------------------------------------------------------------
# Positive cases — valid shapes must return []
# ----------------------------------------------------------------------

def test_valid_heuristic_meta_returns_no_errors():
    meta = build_scoring_meta(
        provenance="heuristic",
        supported_dims=["crowding_intensity", "trend_direction"],
        defaulted_dims=["catalyst_proximity"],
        requires_resolution=True,
        missing_dimensions=["catalyst_proximity"],
    )
    assert validate_scoring_meta(meta) == []


def test_valid_scanner_meta_without_missing_dimensions():
    meta = build_scoring_meta(
        provenance="scanner",
        supported_dims=["setup_strength", "edge_freshness"],
        defaulted_dims=[],
        requires_resolution=False,
    )
    assert validate_scoring_meta(meta) == []


def test_valid_meta_with_data_freshness_block():
    meta = build_scoring_meta(
        provenance="heuristic",
        supported_dims=["liquidity"],
        defaulted_dims=["setup_strength"],
        requires_resolution=True,
        data_freshness={"market_snapshot": {"status": "live", "age_seconds": 30, "source": "yfinance"}},
    )
    assert validate_scoring_meta(meta) == []


# ----------------------------------------------------------------------
# Negative cases — each asserts a specific contract violation surfaces
# ----------------------------------------------------------------------

def test_non_dict_input_returns_single_error():
    errors = validate_scoring_meta("not a dict")
    assert errors == ["scoring_meta must be a dict"]


def test_missing_required_keys_are_each_reported():
    errors = validate_scoring_meta({"provenance": "heuristic"})
    assert any("supported_dims" in e for e in errors)
    assert any("defaulted_dims" in e for e in errors)
    assert any("requires_resolution" in e for e in errors)


def test_invalid_provenance_enum_value():
    meta = {
        "provenance": "guessed",
        "supported_dims": [],
        "defaulted_dims": [],
        "requires_resolution": False,
    }
    errors = validate_scoring_meta(meta)
    assert any("invalid provenance" in e for e in errors)


def test_supported_dims_not_a_list():
    meta = {
        "provenance": "heuristic",
        "supported_dims": "crowding_intensity",
        "defaulted_dims": [],
        "requires_resolution": False,
    }
    errors = validate_scoring_meta(meta)
    assert any("supported_dims must be a list" in e for e in errors)


def test_supported_and_defaulted_overlap_detected():
    meta = {
        "provenance": "heuristic",
        "supported_dims": ["liquidity", "trend_direction"],
        "defaulted_dims": ["trend_direction"],
        "requires_resolution": True,
    }
    errors = validate_scoring_meta(meta)
    assert any("overlap" in e for e in errors)


def test_missing_dimensions_not_subset_of_defaulted():
    meta = {
        "provenance": "heuristic",
        "supported_dims": [],
        "defaulted_dims": ["catalyst_proximity"],
        "requires_resolution": True,
        "missing_dimensions": ["market_mispricing"],  # not in defaulted
    }
    errors = validate_scoring_meta(meta)
    assert any("missing_dimensions" in e and "not in defaulted_dims" in e for e in errors)


def test_requires_resolution_not_bool():
    meta = {
        "provenance": "scanner",
        "supported_dims": [],
        "defaulted_dims": [],
        "requires_resolution": "yes",
    }
    errors = validate_scoring_meta(meta)
    assert any("requires_resolution must be bool" in e for e in errors)
