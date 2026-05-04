"""
Phase 2 tests for the deterministic feature builder.

Covers:
  - EV math: expected_value_pct, pricing_edge
  - Probability composition: base + designation modifiers, clamped to [0,1]
  - market-implied probability fallback when straddle missing
  - magnitude defaults by mcap bucket
  - capped Kelly-lite-style scoring (placeholder uses weighted blend)
  - reproducibility: same inputs -> same hash
  - implied_move -> market_p inversion math
  - no-mispricing-no-Immediate: when market_p is missing the bridge must block
    Immediate; this module surfaces market_p=None and the bridge enforces it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from modal_workers.scanners.fda_event_features import (
    AgentModifiers,
    BAND_THRESHOLDS_DEFAULT,
    DESIGNATION_MODIFIERS_DEFAULT,
    MEDICAL_FAIR_PROBABILITY_MODIFIER_BOUND,
    REGULATORY_CONFIDENCE_BOOST_BOUND,
    FeatureInputs,
    apply_designation_modifiers,
    base_probability,
    canonical_inputs_hash,
    compose_features,
    compute_score,
    derive_band,
    evidence_confidence,
    expected_value_pct,
    implied_move_to_market_probability,
    magnitude_defaults_for_mcap,
    map_indication_to_base_key,
    parse_agent_modifiers,
    pricing_edge,
)


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_expected_value_pct_basic():
    # AXSM example from rubric: P=0.65, U=40, D=-25 => +17.25
    assert expected_value_pct(0.65, 40.0, -25.0) == pytest.approx(17.25)


def test_expected_value_pct_negative_when_pessimistic():
    # P=0.20, U=30, D=-40 => 0.2*30 - 0.8*40 = 6 - 32 = -26
    assert expected_value_pct(0.20, 30.0, -40.0) == pytest.approx(-26.0)


def test_pricing_edge_returns_none_when_market_p_missing():
    assert pricing_edge(0.7, None) is None


def test_pricing_edge_signed():
    assert pricing_edge(0.7, 0.4) == pytest.approx(0.30)
    assert pricing_edge(0.3, 0.6) == pytest.approx(-0.30)


def test_designation_modifiers_capped_at_unit_interval():
    # Stack everything: base=0.5 + 0.05 + 0.04 + 0.03 + 0.02 + 0.02 - 0.10 = 0.56
    p = apply_designation_modifiers(0.5, {
        "priority_review": True, "breakthrough": True, "accelerated": True,
        "rtor": True, "fast_track": True, "is_resubmission": True,
    })
    assert p == pytest.approx(0.56)
    # Above-1 base clamped
    p_high = apply_designation_modifiers(0.99, {"priority_review": True})
    assert 0.0 <= p_high <= 1.0


def test_designation_modifiers_resubmission_penalty():
    p = apply_designation_modifiers(0.50, {"is_resubmission": True})
    assert p == pytest.approx(0.40)


def test_base_probability_falls_back_to_default():
    rates = {"default": 0.58, "oncology_solid_tumor": 0.45}
    # No matching indication -> default
    assert base_probability("rare unmappable thing", rates) == 0.58
    # Match
    assert base_probability("metastatic carcinoma", rates) == 0.45


def test_base_probability_no_default_uses_constant():
    rates = {"oncology_solid_tumor": 0.45}
    # No 'default' key, no match -> DEFAULT_APPROVAL_PROB
    assert base_probability("nothing", rates) == pytest.approx(0.58)


def test_magnitude_defaults_megacap_smallcap():
    assert magnitude_defaults_for_mcap(80_000_000_000) == (4.0, 3.0)
    assert magnitude_defaults_for_mcap(500_000_000) == (35.0, 25.0)
    assert magnitude_defaults_for_mcap(50_000_000) == (60.0, 40.0)
    # Unknown -> mid-conservative
    assert magnitude_defaults_for_mcap(None) == (35.0, 25.0)


def test_implied_move_to_market_probability_asymmetric():
    # AXSM-style payoff U=40, D=25 -> implied_move = 25 + 15p
    # implied_move=25 -> p=0
    # implied_move=40 -> p=1
    # implied_move=32.5 -> p=0.5
    assert implied_move_to_market_probability(25.0, 40.0, -25.0) == pytest.approx(0.0)
    assert implied_move_to_market_probability(40.0, 40.0, -25.0) == pytest.approx(1.0)
    assert implied_move_to_market_probability(32.5, 40.0, -25.0) == pytest.approx(0.5)


def test_implied_move_clamped_outside_range():
    # implied_move below D -> p<0 -> clamped to 0
    assert implied_move_to_market_probability(10.0, 40.0, -25.0) == 0.0
    # implied_move above U -> p>1 -> clamped to 1
    assert implied_move_to_market_probability(60.0, 40.0, -25.0) == 1.0


def test_implied_move_symmetric_payoff_returns_none():
    # When U == D the straddle can't distinguish probability — undefined.
    assert implied_move_to_market_probability(20.0, 20.0, -20.0) is None


def test_derive_band_thresholds():
    assert derive_band(40.0) == "immediate"
    assert derive_band(30.0) == "watchlist"
    assert derive_band(20.0) == "archive"
    assert derive_band(10.0) == "discard"


def test_derive_band_custom_thresholds():
    custom = {"immediate": 40.0, "watchlist": 30.0, "archive": 20.0}
    assert derive_band(35.0, thresholds=custom) == "watchlist"
    assert derive_band(45.0, thresholds=custom) == "immediate"


def test_evidence_confidence_blends_count_and_agents():
    # No evidence, no agents -> 0
    assert evidence_confidence(evidence_count=0, agent_confidences=[]) == 0.0
    # 6+ raw sources without agents saturates at 0.6
    c = evidence_confidence(evidence_count=10, agent_confidences=[])
    assert c == pytest.approx(0.6)
    # Add agent confidence 1.0 across 3 agents -> +0.4
    c = evidence_confidence(evidence_count=6, agent_confidences=[1.0, 1.0, 1.0])
    assert c == pytest.approx(1.0)


def test_canonical_inputs_hash_stable_across_dict_order():
    a = {"alpha": 1, "beta": [3, 2, 1]}
    b = {"beta": [3, 2, 1], "alpha": 1}
    assert canonical_inputs_hash(a) == canonical_inputs_hash(b)


def test_canonical_inputs_hash_differs_when_inputs_differ():
    a = {"x": 1}
    b = {"x": 2}
    assert canonical_inputs_hash(a) != canonical_inputs_hash(b)


def test_map_indication_to_base_key():
    assert map_indication_to_base_key("Agitation in Alzheimer's") in {"psychiatry_agitation", "neurology_alzheimers"}
    assert map_indication_to_base_key("metastatic breast cancer") == "oncology_solid_tumor"
    assert map_indication_to_base_key("focal segmental glomerulosclerosis (FSGS)") == "nephrology_rare"
    assert map_indication_to_base_key("nothing matches") is None


def test_compute_score_within_zero_to_fifty():
    # high-conviction, well-priced, big move, near-term, liquid
    s = compute_score(
        fair_probability=0.85, pricing_edge_value=0.25,
        upside_pct=60.0, downside_pct=40.0,
        expected_value=30.0, days_to_event=10,
        adv_usd=200_000_000, options_liquidity_score=5.0,
    )
    assert 40.0 <= s <= 50.0
    # all-low: small mcap, no edge, far out
    s2 = compute_score(
        fair_probability=0.30, pricing_edge_value=0.01,
        upside_pct=10.0, downside_pct=8.0,
        expected_value=-5.0, days_to_event=120,
        adv_usd=50_000, options_liquidity_score=0.0,
    )
    assert 0.0 <= s2 <= 25.0


# ---------------------------------------------------------------------------
# compose_features: end-to-end determinism + shapes
# ---------------------------------------------------------------------------


def _build_inputs(**overrides):
    base = dict(
        indication="Agitation associated with Alzheimer's disease",
        designations={"priority_review": True},
        event_date=date(2026, 9, 15),
        snapshot_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        base_rates={"default": 0.58, "psychiatry_agitation": 0.55},
        market_cap_usd=2_000_000_000,
        adv_usd=15_000_000,
        straddle={
            "underlying_price": 50.0,
            "expiry": "2026-09-19",
            "call_mid": 4.5, "put_mid": 4.0,
            "straddle_price": 8.5,
            "implied_move_pct": 17.0,
            "call_iv": 0.92, "put_iv": 0.95,
        },
        options_liquidity={
            "contract_count": 18, "total_open_interest": 6500, "liquidity_score": 4.0,
        },
        evidence_count=4,
        agent_confidences=[],
    )
    base.update(overrides)
    return FeatureInputs(**base)


def test_compose_features_deterministic_under_repeated_calls():
    a = compose_features(_build_inputs())
    b = compose_features(_build_inputs())
    assert a.score == b.score
    assert a.band == b.band
    assert a.expected_value_pct == b.expected_value_pct
    assert a.inputs_hash == b.inputs_hash


def test_compose_features_no_mispricing_no_immediate_when_straddle_missing():
    inputs = _build_inputs(straddle=None)
    out = compose_features(inputs)
    assert out.market_implied_probability is None
    assert out.pricing_edge is None
    # The bridge enforces "no auto-Immediate without market_implied_p". This
    # module can still emit any band; the bridge applies the gate. We only
    # assert that the missing market_p propagates.
    assert out.implied_move_pct is None


def test_compose_features_raw_inputs_capture_all_inputs_for_replay():
    out = compose_features(_build_inputs())
    keys = set(out.raw_inputs.keys())
    expected_subset = {
        "indication", "designations", "event_date", "snapshot_at",
        "market_cap_usd", "adv_usd", "straddle", "options_liquidity",
        "evidence_count", "agent_confidences", "fair_probability",
        "implied_move_pct", "market_implied_probability",
        "upside_pct", "downside_pct", "band_thresholds",
    }
    assert expected_subset.issubset(keys)


def test_compose_features_megacap_uses_megacap_magnitude():
    out = compose_features(_build_inputs(market_cap_usd=80_000_000_000, straddle=None))
    assert out.upside_pct == 4.0
    assert out.downside_pct == 3.0


def test_compose_features_smallcap_high_volatility_default():
    out = compose_features(_build_inputs(market_cap_usd=200_000_000, straddle=None))
    assert out.upside_pct == 60.0
    assert out.downside_pct == 40.0


def test_compose_features_score_in_valid_range():
    out = compose_features(_build_inputs())
    assert 0.0 <= out.score <= 50.0


def test_compose_features_band_matches_threshold():
    out = compose_features(_build_inputs())
    assert out.band == derive_band(out.score)


def test_designation_priority_review_lifts_probability():
    no_pr = compose_features(_build_inputs(designations={}))
    pr = compose_features(_build_inputs(designations={"priority_review": True}))
    assert pr.fair_probability > no_pr.fair_probability
    assert pr.fair_probability - no_pr.fair_probability == pytest.approx(0.05)


def test_resubmission_penalty_lowers_probability():
    base = compose_features(_build_inputs(designations={}))
    resub = compose_features(_build_inputs(designations={"is_resubmission": True}))
    assert resub.fair_probability == pytest.approx(base.fair_probability - 0.10)


def test_market_p_clamped_when_implied_move_extreme():
    # Insanely large implied move should clamp to 1.0
    inputs = _build_inputs(
        market_cap_usd=200_000_000,  # smallcap -> defaults 60/40
        straddle={"implied_move_pct": 200.0},
    )
    out = compose_features(inputs)
    assert out.market_implied_probability == 1.0


# ---------------------------------------------------------------------------
# Phase 5 — specialist agent modifiers
# ---------------------------------------------------------------------------


def _agent_evidence(source, payload, *, fetched_at="2026-04-30T12:00:00Z", status="active"):
    return {
        "source": source,
        "evidence_type": "agent_review",
        "payload": payload,
        "fetched_at": fetched_at,
        "evidence_status": status,
    }


def test_parse_agent_modifiers_empty():
    mods = parse_agent_modifiers([])
    assert mods.medical_fair_probability_modifier == 0.0
    assert mods.regulatory_evidence_confidence_boost == 0.0
    assert mods.microstructure_options_liquidity_score is None
    assert mods.microstructure_implied_move_pct is None


def test_parse_agent_modifiers_clamps_medical_modifier():
    rows = [_agent_evidence("agent_medical", {"fair_probability_modifier": 0.50, "confidence": 0.9})]
    mods = parse_agent_modifiers(rows)
    # Bound is ±0.10
    assert mods.medical_fair_probability_modifier == MEDICAL_FAIR_PROBABILITY_MODIFIER_BOUND


def test_parse_agent_modifiers_clamps_regulatory_boost():
    rows = [_agent_evidence("agent_regulatory", {"evidence_confidence_boost": -0.99})]
    mods = parse_agent_modifiers(rows)
    assert mods.regulatory_evidence_confidence_boost == -REGULATORY_CONFIDENCE_BOOST_BOUND


def test_parse_agent_modifiers_microstructure_overrides():
    rows = [
        _agent_evidence(
            "agent_microstructure",
            {
                "options_liquidity_score": 4.5,
                "implied_move_pct": 18.5,
                "borrow_cost_bps": 150,
                "crowding_score": 3.0,
            },
        )
    ]
    mods = parse_agent_modifiers(rows)
    assert mods.microstructure_options_liquidity_score == 4.5
    assert mods.microstructure_implied_move_pct == 18.5
    assert mods.microstructure_borrow_cost_bps == 150.0
    assert mods.microstructure_crowding_score == 3.0


def test_parse_agent_modifiers_skips_rejected_evidence():
    rows = [
        _agent_evidence(
            "agent_medical",
            {"fair_probability_modifier": 0.07},
            status="rejected",
        )
    ]
    mods = parse_agent_modifiers(rows)
    assert mods.medical_fair_probability_modifier == 0.0


def test_parse_agent_modifiers_picks_latest_per_kind():
    rows = [
        _agent_evidence("agent_medical", {"fair_probability_modifier": 0.02},
                        fetched_at="2026-04-29T12:00:00Z"),
        _agent_evidence("agent_medical", {"fair_probability_modifier": -0.05},
                        fetched_at="2026-04-30T12:00:00Z"),
    ]
    mods = parse_agent_modifiers(rows)
    assert mods.medical_fair_probability_modifier == -0.05


def test_parse_agent_modifiers_negative_implied_move_ignored():
    rows = [_agent_evidence("agent_microstructure", {"implied_move_pct": -3.0})]
    mods = parse_agent_modifiers(rows)
    # Negative magnitudes are nonsensical for a straddle implied move; skip.
    assert mods.microstructure_implied_move_pct is None


def test_compose_features_medical_modifier_lifts_probability():
    base_inputs = _build_inputs(designations={})
    boosted = _build_inputs(
        designations={},
        agent_modifiers=AgentModifiers(medical_fair_probability_modifier=0.05),
    )
    out_base = compose_features(base_inputs)
    out_boost = compose_features(boosted)
    assert out_boost.fair_probability == pytest.approx(out_base.fair_probability + 0.05, abs=1e-6)


def test_compose_features_medical_modifier_clamped_in_compose_too():
    """Defense-in-depth: even if AgentModifiers somehow carries an out-of-bounds
    value, compose_features clamps again."""
    boosted = _build_inputs(
        designations={},
        agent_modifiers=AgentModifiers(medical_fair_probability_modifier=999.0),
    )
    out = compose_features(boosted)
    assert 0.0 <= out.fair_probability <= 1.0


def test_compose_features_regulatory_boost_raises_confidence():
    base = compose_features(_build_inputs(agent_modifiers=AgentModifiers()))
    boosted = compose_features(
        _build_inputs(agent_modifiers=AgentModifiers(regulatory_evidence_confidence_boost=0.30))
    )
    assert boosted.evidence_confidence > base.evidence_confidence
    assert 0.0 <= boosted.evidence_confidence <= 1.0


def test_compose_features_microstructure_implied_move_fallback_when_polygon_missing():
    """Microstructure agent's implied_move_pct fills in for missing Polygon data."""
    inputs = _build_inputs(
        straddle=None,  # Polygon unavailable
        agent_modifiers=AgentModifiers(microstructure_implied_move_pct=15.0),
    )
    out = compose_features(inputs)
    assert out.implied_move_pct == 15.0
    assert out.market_implied_probability is not None
    assert out.raw_inputs.get("implied_move_source") == "agent_microstructure"


def test_compose_features_polygon_wins_when_both_present():
    """Polygon data takes precedence; agent override only fills gaps."""
    inputs = _build_inputs(
        straddle={"implied_move_pct": 17.0},
        agent_modifiers=AgentModifiers(microstructure_implied_move_pct=99.0),
    )
    out = compose_features(inputs)
    assert out.implied_move_pct == 17.0
    assert out.raw_inputs.get("implied_move_source") == "polygon_straddle"


def test_compose_features_microstructure_liquidity_fallback():
    inputs = _build_inputs(
        options_liquidity=None,  # Polygon unavailable
        agent_modifiers=AgentModifiers(microstructure_options_liquidity_score=3.5),
    )
    out = compose_features(inputs)
    assert out.options_liquidity_score == 3.5
    assert out.raw_inputs.get("options_liquidity_source") == "agent_microstructure"


def test_compose_features_agent_modifiers_captured_in_raw_inputs():
    mods = AgentModifiers(
        medical_fair_probability_modifier=0.05,
        medical_safety_concerns=["mild liver enzyme elevations"],
        regulatory_evidence_confidence_boost=0.1,
        regulatory_resubmission_pathway="smooth",
    )
    out = compose_features(_build_inputs(agent_modifiers=mods))
    captured = out.raw_inputs["agent_modifiers"]
    assert captured["medical_fair_probability_modifier"] == 0.05
    assert captured["medical_safety_concerns"] == ["mild liver enzyme elevations"]
    assert captured["regulatory_evidence_confidence_boost"] == 0.1
    assert captured["regulatory_resubmission_pathway"] == "smooth"


def test_compose_features_replay_includes_agent_modifiers_in_hash():
    """Two snapshots with different modifiers must produce different hashes."""
    a = compose_features(_build_inputs(agent_modifiers=AgentModifiers()))
    b = compose_features(
        _build_inputs(agent_modifiers=AgentModifiers(medical_fair_probability_modifier=0.05))
    )
    assert a.inputs_hash != b.inputs_hash


def test_replay_with_same_canonical_inputs_yields_same_hash():
    out_a = compose_features(_build_inputs())
    rebuilt = FeatureInputs(
        indication=out_a.raw_inputs["indication"],
        designations=out_a.raw_inputs["designations"],
        event_date=date.fromisoformat(out_a.raw_inputs["event_date"]),
        snapshot_at=datetime.fromisoformat(out_a.raw_inputs["snapshot_at"]),
        base_rates={"default": 0.58, "psychiatry_agitation": 0.55},
        market_cap_usd=out_a.raw_inputs["market_cap_usd"],
        adv_usd=out_a.raw_inputs["adv_usd"],
        straddle=out_a.raw_inputs["straddle"],
        options_liquidity=out_a.raw_inputs["options_liquidity"],
        evidence_count=out_a.raw_inputs["evidence_count"],
        agent_confidences=out_a.raw_inputs["agent_confidences"],
    )
    out_b = compose_features(rebuilt)
    assert out_a.inputs_hash == out_b.inputs_hash
    assert out_a.score == out_b.score
