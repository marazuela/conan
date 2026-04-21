"""
Tests for dim_estimator.

Three paths per estimable profile:
  - full evidence → all dims produced, well-differentiated
  - partial evidence → produces dims with conservative-3 fills
  - no evidence → returns None (signal stays unscored)

Profiles without estimators (activist_governance, merger_arb, litigation)
always return None regardless of payload content.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from modal_workers.shared.dim_estimator import DimensionEstimate, estimate_dimensions
from modal_workers.shared.rubric_engine import WEIGHTS


def _estimate(profile: str, payload: dict) -> DimensionEstimate:
    estimate = estimate_dimensions(profile, payload)
    assert estimate is not None
    return estimate


# ----------------------------------------------------------------------
# Dispatch contract
# ----------------------------------------------------------------------

def test_unknown_profile_returns_none():
    assert estimate_dimensions("nonexistent", {"foo": "bar"}) is None


def test_unscored_profiles_return_none():
    for profile in ("activist_governance", "merger_arb", "litigation"):
        assert estimate_dimensions(profile, {"anything": "here"}) is None


def test_empty_payload_always_returns_none():
    for profile in WEIGHTS:
        assert estimate_dimensions(profile, {}) is None


# ----------------------------------------------------------------------
# short_positioning
# ----------------------------------------------------------------------

def test_short_positioning_high_crowding_high_trend():
    payload = {
        "total_disclosed_pct": 12.0,
        "position_pct": 4.0,
        "change_pct": 1.0,
        "regulators": ["FCA", "BaFin", "AMF"],
    }
    estimate = _estimate("short_positioning", payload)
    assert estimate.dimensions["crowding_intensity"] == 5
    assert estimate.dimensions["trend_direction"] == 5
    assert estimate.dimensions["size_vs_float"] == 5
    assert estimate.dimensions["catalyst_proximity"] == 3
    assert estimate.dimensions["historical_analog"] == 3
    assert estimate.dimensions["liquidity"] == 3
    assert estimate.defaulted_dims == [
        "catalyst_proximity",
        "historical_analog",
        "liquidity",
    ]
    assert estimate.requires_resolution is True


def test_short_positioning_aggregate_crowding_payload_derives_more_than_neutral():
    today = datetime.now(timezone.utc).date()
    payload = {
        "holder_count": 4,
        "total_disclosed_pct": 6.4,
        "regulators": ["FCA", "BaFin"],
        "holders": [
            {"position_pct": 1.6, "position_date": today.isoformat()},
            {"position_pct": 1.8, "position_date": (today - timedelta(days=2)).isoformat()},
            {"position_pct": 1.5, "position_date": (today - timedelta(days=4)).isoformat()},
            {"position_pct": 1.5, "position_date": (today - timedelta(days=8)).isoformat()},
        ],
    }
    estimate = _estimate("short_positioning", payload)
    assert estimate.dimensions["crowding_intensity"] == 5
    assert estimate.dimensions["trend_direction"] == 5
    assert estimate.dimensions["size_vs_float"] == 4
    assert estimate.defaulted_dims == [
        "catalyst_proximity",
        "historical_analog",
        "liquidity",
    ]


def test_short_positioning_unwinding_position():
    estimate = _estimate("short_positioning", {"position_pct": 0.6, "change_pct": -0.8})
    assert estimate.dimensions["crowding_intensity"] == 1
    assert estimate.dimensions["trend_direction"] == 1
    assert estimate.dimensions["size_vs_float"] == 2


def test_short_positioning_no_position_data_returns_none():
    payload = {"regulator": "FCA", "holder_name": "Acme Capital"}
    assert estimate_dimensions("short_positioning", payload) is None


def test_short_positioning_all_dims_present():
    estimate = _estimate("short_positioning", {"position_pct": 2.0, "change_pct": 0.0})
    assert set(estimate.dimensions.keys()) == set(WEIGHTS["short_positioning"].keys())


def test_short_positioning_relative_bumper_behaviour():
    estimate = _estimate(
        "short_positioning",
        {"position_pct": 0.8, "previous_position_pct": 0.5, "change_pct": 0.3},
    )
    assert estimate.dimensions["trend_direction"] == 4

    estimate = _estimate(
        "short_positioning",
        {"position_pct": 2.5, "previous_position_pct": 5.0, "change_pct": -2.5},
    )
    assert estimate.dimensions["trend_direction"] == 1


# ----------------------------------------------------------------------
# takeover_candidate
# ----------------------------------------------------------------------

def test_takeover_candidate_strong_setup_marks_missing_dims_as_defaulted():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "patterns_hit": 4,
        "pattern_names": ["strategic_review", "pe_take_private"],
        "primary_filing": {"file_date": today},
        "pe_filer_type": "strategic",
        "pe_filer_name": "BigCorp Industries",
    }
    estimate = _estimate("takeover_candidate", payload)
    assert estimate.dimensions["setup_strength"] == 5
    assert estimate.dimensions["edge_freshness"] == 5
    assert estimate.dimensions["strategic_buyer_clarity"] == 5
    assert estimate.dimensions["valuation_cushion"] == 3
    assert estimate.dimensions["liquidity"] == 3
    assert estimate.defaulted_dims == ["valuation_cushion", "liquidity"]


def test_takeover_candidate_uses_optional_valuation_and_adv():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "patterns_hit": 3,
        "pattern_names": ["strategic_review"],
        "primary_filing": {"file_date": today},
        "pe_filer_type": "pe",
        "pe_filer_name": "Sector Buyer",
        "valuation_discount_pct": 22,
        "adv_usd": 18_000_000,
    }
    estimate = _estimate("takeover_candidate", payload)
    assert estimate.dimensions["valuation_cushion"] == 4
    assert estimate.dimensions["liquidity"] == 4
    assert estimate.defaulted_dims == []
    assert estimate.requires_resolution is False


def test_takeover_candidate_missing_patterns_returns_none():
    assert estimate_dimensions("takeover_candidate", {"pe_filer_name": "X"}) is None


# ----------------------------------------------------------------------
# binary_catalyst
# ----------------------------------------------------------------------

def test_binary_catalyst_imminent_with_strong_adcom_dict():
    payload = {
        "days_until_pdufa": 10,
        "adcom_vote": {"yes": 12, "no": 2},
        "is_resubmission": False,
    }
    estimate = _estimate("binary_catalyst", payload)
    assert estimate.dimensions["catalyst_timeline"] == 5
    assert estimate.dimensions["approval_probability"] == 5
    assert "market_mispricing" in estimate.defaulted_dims


def test_binary_catalyst_string_vote_and_support_ratio_are_honoured():
    estimate = _estimate(
        "binary_catalyst",
        {"days_until_pdufa": 20, "adcom_vote": "10-2"},
    )
    assert estimate.dimensions["approval_probability"] == 5
    assert estimate.dimensions["catalyst_timeline"] == 4

    estimate = _estimate(
        "binary_catalyst",
        {"days_until_pdufa": 20, "adcom_support_ratio": 0.92},
    )
    assert estimate.dimensions["approval_probability"] == 5


def test_binary_catalyst_supports_pre_phase3_probability_and_readout_window():
    estimate = _estimate(
        "binary_catalyst",
        {
            "days_until_readout": 48,
            "approval_probability": 0.76,
            "upside_pct": 50.0,
            "downside_pct": 35.0,
        },
    )
    assert estimate.dimensions["approval_probability"] == 5
    assert estimate.dimensions["catalyst_timeline"] == 3
    assert estimate.dimensions["magnitude"] == 4
    assert estimate.defaulted_dims == [
        "market_mispricing",
        "competitive_landscape",
        "liquidity",
    ]


def test_binary_catalyst_resubmission_with_crl_history():
    estimate = _estimate(
        "binary_catalyst",
        {
            "days_until_pdufa": 90,
            "is_resubmission": True,
            "status": "resolved_crl",
        },
    )
    assert estimate.dimensions["catalyst_timeline"] == 2
    assert estimate.dimensions["approval_probability"] == 1


def test_binary_catalyst_negative_adcom_vote():
    estimate = _estimate(
        "binary_catalyst",
        {"days_until_pdufa": 45, "adcom_vote": {"yes": 2, "no": 10}},
    )
    assert estimate.dimensions["approval_probability"] == 1
    assert estimate.dimensions["catalyst_timeline"] == 3


def test_binary_catalyst_no_timing_returns_none():
    assert estimate_dimensions("binary_catalyst", {"ticker": "ABCD"}) is None


# ----------------------------------------------------------------------
# End-to-end: estimator + rubric_engine produce scored output
# ----------------------------------------------------------------------

def test_estimator_to_rubric_engine_produces_scored_signal():
    from modal_workers.shared.rubric_engine import score_signal

    payload = {
        "holder_count": 4,
        "total_disclosed_pct": 8.0,
        "regulators": ["FCA", "BaFin"],
        "holders": [
            {"position_pct": 2.5, "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            {"position_pct": 2.0, "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            {"position_pct": 1.8, "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            {"position_pct": 1.7, "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
        ],
    }
    estimate = _estimate("short_positioning", payload)
    out = score_signal(
        {
            "scoring_profile": "short_positioning",
            "raw_data": {**payload, "dimensions": estimate.dimensions},
        }
    )
    assert out["score"] is not None
    assert out["band"] is not None


def test_unscored_profile_leaves_signal_unscored():
    from modal_workers.shared.rubric_engine import score_signal

    dims = estimate_dimensions("activist_governance", {"keyword": "activist_13d"})
    assert dims is None
    signal = {"scoring_profile": "activist_governance", "raw_data": {"keyword": "activist_13d"}}
    out = score_signal(signal)
    assert out["score"] is None
    assert out["band"] is None
