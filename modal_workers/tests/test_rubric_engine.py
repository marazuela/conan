"""
Tests for rubric_engine — the load-bearing scoring module.

These fixtures cover each profile × each auto-cap × band-threshold crossings, per
spec.md §10.1 ("~90 hand-crafted fixtures compensating for the shrunken replay test").
Values are computed from WEIGHTS directly, not hardcoded outputs — if any weight is
accidentally edited, the expected totals will surface the drift.

Run: python -m pytest modal_workers/tests/test_rubric_engine.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.shared.rubric_engine import (
    WEIGHTS,
    UnknownScoringProfile,
    apply_auto_caps,
    build_scoring_meta,
    classify_band,
    rescore_with_dims,
    score_signal,
    dimensions_with_provenance,
    weighted_total,
)


# ----------------------------------------------------------------------
# WEIGHTS dict integrity — exact match against the verbatim source.
# ----------------------------------------------------------------------

def test_weights_has_six_profiles():
    assert set(WEIGHTS.keys()) == {
        "merger_arb", "activist_governance", "binary_catalyst",
        "short_positioning", "litigation", "takeover_candidate",
    }


def test_merger_arb_weights():
    assert WEIGHTS["merger_arb"] == {
        "spread_size": 3.0, "deal_certainty": 2.5, "annualized_return": 2.0,
        "break_risk": 1.5, "liquidity": 1.0,
    }


def test_activist_governance_weights():
    assert WEIGHTS["activist_governance"] == {
        "signal_strength": 2.0, "information_asymmetry": 2.0,
        "activist_track_record": 1.5, "risk_reward": 1.5,
        "catalyst_clarity": 1.0, "edge_decay": 1.0, "liquidity": 1.0,
    }


def test_binary_catalyst_weights():
    assert WEIGHTS["binary_catalyst"] == {
        "approval_probability": 2.5, "market_mispricing": 2.5, "magnitude": 1.5,
        "competitive_landscape": 1.5, "catalyst_timeline": 1.0, "liquidity": 1.0,
    }


def test_short_positioning_weights():
    assert WEIGHTS["short_positioning"] == {
        "crowding_intensity": 2.5, "trend_direction": 2.0, "catalyst_proximity": 2.0,
        "size_vs_float": 1.5, "historical_analog": 1.0, "liquidity": 1.0,
    }


def test_litigation_weights():
    assert WEIGHTS["litigation"] == {
        "financial_materiality": 3.0, "legal_outcome_probability": 2.0,
        "market_pricing": 2.0, "resolution_timeline": 1.5, "liquidity": 1.0,
        "party_resolution_confidence": 0.5,
    }


def test_takeover_candidate_weights():
    assert WEIGHTS["takeover_candidate"] == {
        "setup_strength": 3.0, "edge_freshness": 2.0, "valuation_cushion": 2.0,
        "strategic_buyer_clarity": 2.0, "liquidity": 1.0,
    }


# ----------------------------------------------------------------------
# weighted_total
# ----------------------------------------------------------------------

def test_weighted_total_all_fives_merger_arb():
    # Max possible merger_arb: 5 * (3 + 2.5 + 2 + 1.5 + 1) = 50
    dims = {k: 5 for k in WEIGHTS["merger_arb"]}
    assert weighted_total(dims, "merger_arb") == 50.0


def test_weighted_total_all_ones_merger_arb():
    dims = {k: 1 for k in WEIGHTS["merger_arb"]}
    assert weighted_total(dims, "merger_arb") == 10.0


def test_weighted_total_missing_dim_defaults_to_zero_in_lookup():
    # weighted_total itself reads missing dims as 0. score_signal short-circuits
    # before reaching here when dims are missing (returns unscored), so this path
    # is only exercised by callers that construct dims maps directly.
    dims = {"spread_size": 5}
    # Only spread_size contributes: 5 * 3 = 15
    assert weighted_total(dims, "merger_arb") == 15.0


def test_dimensions_with_provenance_adds_marker_without_mutating_scores():
    dims = {"spread_size": 5, "deal_certainty": 4}
    payload = dimensions_with_provenance(dims, "heuristic")
    assert payload["spread_size"] == 5
    assert payload["deal_certainty"] == 4
    assert payload["_provenance"] == "heuristic"


def test_build_scoring_meta_captures_resolution_state():
    meta = build_scoring_meta(
        provenance="heuristic",
        supported_dims=["spread_size", "deal_certainty"],
        defaulted_dims=["annualized_return"],
        requires_resolution=True,
        missing_dimensions=["annualized_return"],
    )
    assert meta == {
        "provenance": "heuristic",
        "supported_dims": ["spread_size", "deal_certainty"],
        "defaulted_dims": ["annualized_return"],
        "requires_resolution": True,
        "missing_dimensions": ["annualized_return"],
    }


# ----------------------------------------------------------------------
# classify_band — exact threshold boundaries (35 / 25 / 15)
# ----------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (50.0, "immediate"),
    (35.0, "immediate"),     # lower bound inclusive
    (34.99, "watchlist"),
    (25.0, "watchlist"),     # lower bound inclusive
    (24.99, "archive"),
    (15.0, "archive"),       # lower bound inclusive
    (14.99, "discard"),
    (0.0, "discard"),
])
def test_classify_band_thresholds(score, expected):
    assert classify_band(score) == expected


# ----------------------------------------------------------------------
# apply_auto_caps — one test per rule_id, per branch
# ----------------------------------------------------------------------

class TestMergerArbCaps:
    def test_rule_A_sub_scale_return_caps_immediate_to_watchlist(self):
        # Rule A: annualized return < (RISK_FREE_RATE * 100 + 3) = 7.3%
        signal = {"raw_data": {"annualized_return_pct": 5.0}}
        dims = {"break_risk": 3, "deal_certainty": 3}
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "immediate")
        assert band == "watchlist"
        assert caps == ["merger_arb.rule_A_sub_scale_return"]

    def test_rule_A_does_not_cap_when_return_above_threshold(self):
        signal = {"raw_data": {"annualized_return_pct": 20.0}}
        dims = {"break_risk": 3, "deal_certainty": 3}
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "immediate")
        assert band == "immediate"
        assert caps == []

    def test_rule_A_does_not_apply_when_band_is_watchlist(self):
        signal = {"raw_data": {"annualized_return_pct": 2.0}}
        dims = {"break_risk": 3, "deal_certainty": 3}
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "watchlist")
        assert band == "watchlist"
        assert caps == []

    def test_rule_B_break_risk_dominance_caps_immediate_to_watchlist(self):
        # Rule B: break_risk==1 AND deal_certainty<=2
        signal = {"raw_data": {}}
        dims = {"break_risk": 1, "deal_certainty": 2}
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "immediate")
        assert band == "watchlist"
        assert caps == ["merger_arb.rule_B_break_risk_dominance"]

    def test_rule_B_does_not_trigger_when_deal_certainty_high(self):
        signal = {"raw_data": {}}
        dims = {"break_risk": 1, "deal_certainty": 3}
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "immediate")
        assert band == "immediate"
        assert caps == []

    def test_both_rules_a_and_b_trigger_simultaneously(self):
        signal = {"raw_data": {"annualized_return_pct": 2.0}}
        dims = {"break_risk": 1, "deal_certainty": 2}
        # Rule A fires first (band -> watchlist). Rule B's gate requires band='immediate'
        # to trigger, so it won't fire on the second pass. Both caps land or only A?
        # v1 behavior: A fires, then B's `if band == "immediate"` check fails so B doesn't
        # fire. Expected: only rule_A caps.
        band, caps = apply_auto_caps(signal, dims, "merger_arb", "immediate")
        assert band == "watchlist"
        assert caps == ["merger_arb.rule_A_sub_scale_return"]


class TestBinaryCatalystEvFloor:
    def test_ev_floor_caps_immediate_when_ev_below_5(self):
        # EV = 0.3 * 20 - 0.7 * 20 = 6 - 14 = -8 (well below 5%)
        signal = {"raw_data": {"approval_probability": 0.3, "upside_pct": 20, "downside_pct": 20}}
        band, caps = apply_auto_caps(signal, {}, "binary_catalyst", "immediate")
        assert band == "watchlist"
        assert any("binary_catalyst.ev_floor" in c for c in caps)

    def test_ev_floor_does_not_cap_when_ev_above_5(self):
        # EV = 0.9 * 50 - 0.1 * 10 = 45 - 1 = 44 (well above 5%)
        signal = {"raw_data": {"approval_probability": 0.9, "upside_pct": 50, "downside_pct": 10}}
        band, caps = apply_auto_caps(signal, {}, "binary_catalyst", "immediate")
        assert band == "immediate"
        assert caps == []

    def test_ev_floor_skipped_when_any_input_missing(self):
        signal = {"raw_data": {"approval_probability": 0.3, "upside_pct": 20}}  # downside missing
        band, caps = apply_auto_caps(signal, {}, "binary_catalyst", "immediate")
        assert band == "immediate"
        assert caps == []


class TestLitigationPartyConfidence:
    def test_party_confidence_below_3_caps_immediate_to_archive(self):
        band, caps = apply_auto_caps({"raw_data": {}}, {"party_resolution_confidence": 1}, "litigation", "immediate")
        assert band == "archive"
        assert caps == ["litigation.party_confidence_cap"]

    def test_party_confidence_below_3_caps_watchlist_to_archive(self):
        band, caps = apply_auto_caps({"raw_data": {}}, {"party_resolution_confidence": 2}, "litigation", "watchlist")
        assert band == "archive"
        assert caps == ["litigation.party_confidence_cap"]

    def test_party_confidence_at_3_does_not_cap(self):
        band, caps = apply_auto_caps({"raw_data": {}}, {"party_resolution_confidence": 3}, "litigation", "immediate")
        assert band == "immediate"
        assert caps == []


class TestTakeoverCandidateCaps:
    def test_post_edge_disqualifier_returns_discard_immediately(self):
        signal = {"raw_data": {"definitive_merger_agreement": True, "patterns_hit": 5}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "immediate")
        assert band == "discard"
        assert caps == ["takeover_candidate.post_edge_disqualified"]

    def test_prior_rejection_caps_watchlist_to_archive(self):
        signal = {"raw_data": {"rejected_prior_offer_6mo": True, "patterns_hit": 3}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "archive"
        assert caps == ["takeover_candidate.prior_rejection_cap"]

    def test_going_concern_caps_immediate_to_watchlist(self):
        signal = {"raw_data": {"going_concern_warning": True, "patterns_hit": 3}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "immediate")
        assert band == "watchlist"
        assert caps == ["takeover_candidate.going_concern_cap"]

    def test_below_triage_gate_returns_discard(self):
        signal = {"raw_data": {"patterns_hit": 1}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "discard"
        assert any("below_triage_gate" in c for c in caps)

    def test_patterns_hit_2_does_not_trigger_below_triage_gate(self):
        signal = {"raw_data": {"patterns_hit": 2}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "watchlist"
        assert caps == []

    def test_post_edge_disqualifier_takes_precedence_over_other_caps(self):
        # Even if other flags would trigger, post_edge is terminal (returns early).
        signal = {"raw_data": {
            "definitive_merger_agreement": True,
            "rejected_prior_offer_6mo": True,
            "going_concern_warning": True,
            "patterns_hit": 5,
        }}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "immediate")
        assert band == "discard"
        assert caps == ["takeover_candidate.post_edge_disqualified"]

    def test_patterns_hit_missing_treated_as_zero(self):
        # Key absent → collapses to 0 → below_triage_gate fires.
        signal = {"raw_data": {}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "discard"
        assert any("below_triage_gate (patterns=0)" in c for c in caps)

    def test_patterns_hit_explicit_none_treated_as_zero(self):
        # Key present with value None must behave identically to missing.
        # Prior to the 2026 normalisation patch, `.get(default=0)` returned
        # None (not 0) on an explicit null and `isinstance(None, int)` is
        # False, so the cap was silently skipped — the signal survived.
        signal = {"raw_data": {"patterns_hit": None}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "discard"
        assert any("below_triage_gate (patterns=0)" in c for c in caps)

    def test_patterns_hit_boolean_treated_as_zero(self):
        # `isinstance(True, int)` is True in Python; the raw code path would have
        # fired the cap with rule_id "below_triage_gate (patterns=True)". After
        # coercion, booleans collapse to 0 and emit a numeric rule_id.
        signal = {"raw_data": {"patterns_hit": True}}
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "discard"
        assert any("below_triage_gate (patterns=0)" in c for c in caps)

    def test_patterns_hit_float_coerced_to_int(self):
        signal = {"raw_data": {"patterns_hit": 2.9}}  # truncates to 2 → no cap
        band, caps = apply_auto_caps(signal, {}, "takeover_candidate", "watchlist")
        assert band == "watchlist"
        assert caps == []


class TestProfilesWithoutAutoCaps:
    def test_activist_governance_has_no_auto_caps(self):
        band, caps = apply_auto_caps({"raw_data": {}}, {}, "activist_governance", "immediate")
        assert band == "immediate"
        assert caps == []

    def test_short_positioning_has_no_auto_caps(self):
        band, caps = apply_auto_caps({"raw_data": {}}, {}, "short_positioning", "immediate")
        assert band == "immediate"
        assert caps == []


# ----------------------------------------------------------------------
# score_signal — end-to-end integration
# ----------------------------------------------------------------------

def test_score_signal_missing_profile_defaults_to_activist_governance():
    # Provide a full activist_governance dim set so the signal scores rather than
    # being returned unscored.
    full = {k: 3 for k in WEIGHTS["activist_governance"]}
    signal = {"raw_data": {"dimensions": full}}
    out = score_signal(signal)
    assert out["scoring_profile"] == "activist_governance"


def test_score_signal_unknown_profile_defaults_to_activist_governance():
    full = {k: 3 for k in WEIGHTS["activist_governance"]}
    signal = {"scoring_profile": "nonexistent", "raw_data": {"dimensions": full}}
    out = score_signal(signal)
    assert out["scoring_profile"] == "activist_governance"


def test_score_signal_clamps_dimensions_to_one_five():
    signal = {
        "scoring_profile": "merger_arb",
        "raw_data": {"dimensions": {"spread_size": 99, "deal_certainty": -5,
                                    "annualized_return": 3, "break_risk": 3, "liquidity": 0}},
    }
    out = score_signal(signal)
    assert out["dimensions"]["spread_size"] == 5
    assert out["dimensions"]["deal_certainty"] == 1
    assert out["dimensions"]["liquidity"] == 1


def test_score_signal_missing_dimensions_returns_unscored():
    """Previously a fully-empty dimensions dict defaulted every dim to 3 and
    produced a fake 30 (every profile's weights sum to 10). Now it returns
    unscored (score=None, band=None) so the UI and DB can tell the difference
    between a genuine 30 and a scanner that never supplied dimensions."""
    signal = {"scoring_profile": "merger_arb", "raw_data": {"dimensions": {}}}
    out = score_signal(signal)
    assert out["score"] is None
    assert out["band"] is None
    assert out["dimensions"] == {}
    assert out["auto_caps_triggered"] == []
    assert set(out["missing_dimensions"]) == set(WEIGHTS["merger_arb"].keys())


def test_score_signal_partial_dimensions_returns_unscored():
    signal = {
        "scoring_profile": "merger_arb",
        "raw_data": {"dimensions": {"spread_size": 5, "deal_certainty": 5}},
    }
    out = score_signal(signal)
    assert out["score"] is None
    assert out["band"] is None
    assert set(out["missing_dimensions"]) == {"annualized_return", "break_risk", "liquidity"}


def test_score_signal_produces_expected_shape():
    signal = {"scoring_profile": "merger_arb",
              "raw_data": {"dimensions": {"spread_size": 4, "deal_certainty": 4,
                                          "annualized_return": 4, "break_risk": 4, "liquidity": 4}}}
    out = score_signal(signal)
    assert set(out.keys()) == {"scoring_profile", "dimensions", "score", "band", "auto_caps_triggered"}
    # 4 * (3 + 2.5 + 2 + 1.5 + 1) = 4 * 10 = 40
    assert out["score"] == 40.0
    assert out["band"] == "immediate"


def test_score_signal_litigation_with_party_confidence_cap():
    # Litigation signal that would score Immediate but gets capped to Archive by party confidence.
    signal = {
        "scoring_profile": "litigation",
        "raw_data": {"dimensions": {
            "financial_materiality": 5, "legal_outcome_probability": 5, "market_pricing": 5,
            "resolution_timeline": 5, "liquidity": 5, "party_resolution_confidence": 2,
        }},
    }
    out = score_signal(signal)
    # score = 5*(3+2+2+1.5+1) + 2*0.5 = 47.5 + 1.0 = 48.5
    assert out["score"] == 48.5
    # Raw band = immediate (≥35), but party_confidence_cap (prc<3) downgrades to archive.
    assert out["band"] == "archive"
    assert out["auto_caps_triggered"] == ["litigation.party_confidence_cap"]


def test_score_signal_takeover_candidate_discard_via_post_edge():
    signal = {
        "scoring_profile": "takeover_candidate",
        "raw_data": {
            "definitive_merger_agreement": True,
            "patterns_hit": 5,
            "dimensions": {"setup_strength": 5, "edge_freshness": 5, "valuation_cushion": 5,
                           "strategic_buyer_clarity": 5, "liquidity": 5},
        },
    }
    out = score_signal(signal)
    # Score is still 50 (rubric doesn't know about the cap), but band becomes discard.
    assert out["score"] == 50.0
    assert out["band"] == "discard"
    assert out["auto_caps_triggered"] == ["takeover_candidate.post_edge_disqualified"]


# ----------------------------------------------------------------------
# rescore_with_dims — wrapper used by signal_resolver skill
# ----------------------------------------------------------------------

def test_rescore_with_dims_matches_score_signal_for_equivalent_inputs():
    """rescore_with_dims must produce the same score/band/auto_caps as a
    direct score_signal call on the equivalent merged payload."""
    dims = {"signal_strength": 4, "information_asymmetry": 5,
            "activist_track_record": 4, "risk_reward": 4,
            "catalyst_clarity": 3, "edge_decay": 4, "liquidity": 3}
    raw_payload = {"keyword": "activist_13d", "cik": "0001234567"}

    direct = score_signal({
        "scoring_profile": "activist_governance",
        "raw_data": {**raw_payload, "dimensions": dims},
    })
    wrapped = rescore_with_dims("activist_governance", raw_payload, dims)

    assert wrapped["score"] == direct["score"]
    assert wrapped["band"] == direct["band"]
    assert wrapped["auto_caps_triggered"] == direct["auto_caps_triggered"]
    assert wrapped["dimensions"] == direct["dimensions"]


def test_rescore_with_dims_adds_provenance_to_dims_copy():
    """The `dimensions_with_provenance` field is the dims dict plus a
    `_provenance` key — callers persist this as signals.dimensions JSONB."""
    dims = {k: 3 for k in WEIGHTS["merger_arb"]}
    out = rescore_with_dims("merger_arb", {}, dims, provenance="ai_resolved")
    assert out["dimensions_with_provenance"]["_provenance"] == "ai_resolved"
    # Original dims field (used by downstream convergence) must stay pure ints.
    assert "_provenance" not in out["dimensions"]
    for k in WEIGHTS["merger_arb"]:
        assert out["dimensions_with_provenance"][k] == 3


def test_rescore_with_dims_default_provenance_is_ai_resolved():
    dims = {k: 3 for k in WEIGHTS["litigation"]}
    out = rescore_with_dims("litigation", {}, dims)
    assert out["dimensions_with_provenance"]["_provenance"] == "ai_resolved"


def test_rescore_with_dims_honours_auto_caps_in_raw_payload():
    """rescore_with_dims must pass raw_payload to score_signal so auto-caps
    that depend on non-dim fields (e.g. takeover_candidate's definitive_merger_agreement)
    still fire."""
    dims = {k: 5 for k in WEIGHTS["takeover_candidate"]}
    raw_payload = {"definitive_merger_agreement": True, "patterns_hit": 5}
    out = rescore_with_dims("takeover_candidate", raw_payload, dims)
    assert out["band"] == "discard"  # capped by post_edge_disqualified
    assert "takeover_candidate.post_edge_disqualified" in out["auto_caps_triggered"]


def test_rescore_with_dims_litigation_party_confidence_cap_fires():
    dims = {"financial_materiality": 5, "legal_outcome_probability": 5,
            "market_pricing": 5, "resolution_timeline": 5, "liquidity": 5,
            "party_resolution_confidence": 2}
    out = rescore_with_dims("litigation", {}, dims)
    # Raw score 48.5 → immediate, but prc<3 caps to archive.
    assert out["score"] == 48.5
    assert out["band"] == "archive"
    assert out["auto_caps_triggered"] == ["litigation.party_confidence_cap"]


def test_rescore_with_dims_missing_dim_returns_unscored():
    """If the skill hands in an incomplete dims dict, rescore_with_dims still
    short-circuits to unscored rather than silently filling — same Option-1
    semantics as score_signal."""
    out = rescore_with_dims("merger_arb", {}, {"spread_size": 5})
    assert out["score"] is None
    assert out["band"] is None


def test_rescore_with_dims_unknown_profile_raises():
    """rescore_with_dims enforces a stricter contract than score_signal — a
    skill caller has already resolved profile from the DB, so a typo or schema
    drift surfaces as an exception rather than a silent activist_governance
    mis-score."""
    dims = {k: 3 for k in WEIGHTS["merger_arb"]}
    with pytest.raises(UnknownScoringProfile):
        rescore_with_dims("merger_arbitrage_typo", {}, dims)


# ======================================================================
# Convergence audit reference (spec §7.6.3) — pure-Python re-implementation
# that the convergence_qa Modal function uses to verify reactor decisions.
# ======================================================================

from modal_workers.shared.rubric_engine import (  # noqa: E402
    convergence_reference,
    signal_fingerprint,
    window_days,
)


def _sig(sid, profile, direction, score, hash_="h-abc"):
    return {
        "signal_id": sid,
        "scoring_profile": profile,
        "thesis_direction": direction,
        "score": score,
        "source_content_hash": hash_,
    }


def test_convergence_empty_group():
    v = convergence_reference([])
    assert v == {"bonus": 0, "type": "single", "winner_signal_id": None, "unique_signal_ids": []}


def test_convergence_single_signal():
    v = convergence_reference([_sig("s1", "merger_arb", "long", 30, "h1")])
    assert v["bonus"] == 0
    assert v["type"] == "single"
    assert v["winner_signal_id"] == "s1"


def test_convergence_two_same_direction_same_profile():
    v = convergence_reference([
        _sig("s1", "merger_arb", "long", 25, "h1"),
        _sig("s2", "merger_arb", "long", 31, "h2"),
    ])
    assert v["bonus"] == 5
    assert v["type"] == "same_direction"
    assert v["winner_signal_id"] == "s2"  # higher score


def test_convergence_three_plus_same_direction_bumps_to_ten():
    v = convergence_reference([
        _sig("s1", "merger_arb", "long", 25, "h1"),
        _sig("s2", "merger_arb", "long", 28, "h2"),
        _sig("s3", "merger_arb", "long", 22, "h3"),
    ])
    assert v["bonus"] == 10
    assert v["type"] == "same_direction"


def test_convergence_orthogonal_two_profiles():
    v = convergence_reference([
        _sig("s1", "merger_arb", "long", 25, "h1"),
        _sig("s2", "activist_governance", "long", 32, "h2"),
    ])
    assert v["bonus"] == 5
    assert v["type"] == "orthogonal"
    assert v["winner_signal_id"] == "s2"


def test_convergence_contradiction_zero_bonus():
    v = convergence_reference([
        _sig("s1", "activist_governance", "long", 30, "h1"),
        _sig("s2", "short_positioning", "short", 28, "h2"),
    ])
    assert v["bonus"] == 0
    assert v["type"] == "contradiction"
    assert v["winner_signal_id"] == "s1"  # highest score wins, regardless of direction


def test_convergence_dedup_collapses_cross_listing_echoes():
    # Same source_content_hash across exchanges — e.g. same 8-K cross-listed —
    # should collapse to one unique signal (bonus=0, type=single).
    v = convergence_reference([
        _sig("s1-us", "merger_arb", "long", 25, "same-hash"),
        _sig("s2-uk", "merger_arb", "long", 31, "same-hash"),
    ])
    assert v["bonus"] == 0
    assert v["type"] == "single"
    assert v["winner_signal_id"] == "s2-uk"  # higher score wins within dedup
    assert len(v["unique_signal_ids"]) == 1


def test_convergence_dedup_keeps_highest_score_per_hash():
    v = convergence_reference([
        _sig("s1-low", "merger_arb", "long", 10, "h1"),
        _sig("s1-high", "merger_arb", "long", 32, "h1"),
        _sig("s2", "merger_arb", "long", 28, "h2"),
    ])
    assert v["bonus"] == 5
    assert v["type"] == "same_direction"
    assert v["winner_signal_id"] == "s1-high"  # 32 > 28 across the unique set


def test_convergence_missing_content_hash_treated_unique():
    v = convergence_reference([
        {"signal_id": "s1", "scoring_profile": "merger_arb", "thesis_direction": "long", "score": 30},
        {"signal_id": "s2", "scoring_profile": "merger_arb", "thesis_direction": "long", "score": 25},
    ])
    # No hash → both treated as distinct; same-direction → bonus=5.
    assert v["bonus"] == 5
    assert v["type"] == "same_direction"


def test_convergence_empty_content_hash_treated_unique():
    # Empty-string hash should behave identically to null/missing — keyed per
    # signal_id so they don't silently collapse into a single bucket.
    v = convergence_reference([
        _sig("s1", "merger_arb", "long", 30, hash_=""),
        _sig("s2", "merger_arb", "long", 25, hash_=""),
    ])
    assert v["bonus"] == 5
    assert v["type"] == "same_direction"
    assert set(v["unique_signal_ids"]) == {"s1", "s2"}


def test_convergence_pure_neutral_group_gets_no_bonus():
    # Two neutral-direction filings (e.g. board_change + strategic_review) on
    # the same entity used to emit same_direction +5 simply because neither was
    # 'long' and neither was 'short'. Post-fix: bonus suppressed, type=single.
    v = convergence_reference([
        _sig("s1", "activist_governance", "neutral", 30, "h1"),
        _sig("s2", "activist_governance", "neutral", 28, "h2"),
    ])
    assert v["bonus"] == 0
    assert v["type"] == "single"
    assert v["winner_signal_id"] == "s1"


def test_convergence_pure_null_direction_gets_no_bonus():
    v = convergence_reference([
        _sig("s1", "activist_governance", None, 30, "h1"),
        _sig("s2", "activist_governance", None, 28, "h2"),
        _sig("s3", "activist_governance", None, 26, "h3"),
    ])
    # 3 signals would normally earn +10 — directionless group gets 0.
    assert v["bonus"] == 0
    assert v["type"] == "single"


def test_convergence_neutral_signal_rides_directional_siblings_bonus():
    # A neutral signal in a group with at least one long sibling still counts
    # toward the 2-or-3+ unique threshold. Bonus is awarded on the directional
    # dimension, but neutral informational signals aren't discarded.
    v = convergence_reference([
        _sig("s1", "merger_arb", "long", 30, "h1"),
        _sig("s2", "activist_governance", "neutral", 26, "h2"),
    ])
    assert v["bonus"] == 5
    assert v["type"] == "orthogonal"
    assert v["winner_signal_id"] == "s1"


def test_window_days_litigation_triggers_thirty():
    assert window_days(["merger_arb"]) == 14
    assert window_days(["merger_arb", "activist_governance"]) == 14
    assert window_days(["merger_arb", "litigation"]) == 30
    assert window_days([]) == 14


def test_signal_fingerprint_is_deterministic_sha256():
    fp1 = signal_fingerprint("abc123", "merger_arb")
    fp2 = signal_fingerprint("abc123", "merger_arb")
    assert fp1 == fp2
    assert len(fp1) == 64  # hex sha256
    # Changing profile or hash must change the fingerprint.
    assert signal_fingerprint("abc123", "activist_governance") != fp1
    assert signal_fingerprint("xyz789", "merger_arb") != fp1
